"""Utilidades compartidas por los procesos de ingesta de Market Radar."""

from __future__ import annotations

import datetime as dt
import json
import os
import sys

import requests

HISTORY_START = "2020-01-01"   # primera fecha de observación que se guarda
UPSERT_CHUNK = 1000


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"Falta la variable de entorno {name}")
    return value


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() == "true"


class Supabase:
    """Cliente mínimo de PostgREST con la clave de servicio."""

    def __init__(self, url: str, service_key: str) -> None:
        self.base = url.rstrip("/") + "/rest/v1"
        self.session = requests.Session()
        self.session.headers.update(
            {
                "apikey": service_key,
                "Authorization": f"Bearer {service_key}",
                "Content-Type": "application/json",
            }
        )

    @classmethod
    def from_env(cls) -> "Supabase":
        return cls(require_env("SUPABASE_URL"), require_env("SUPABASE_SERVICE_ROLE_KEY"))

    def select(self, table: str, params: dict) -> list[dict]:
        r = self.session.get(f"{self.base}/{table}", params=params, timeout=60)
        r.raise_for_status()
        return r.json()

    def insert(self, table: str, rows: list[dict] | dict, *, on_conflict: str | None = None,
               returning: bool = False):
        prefer = []
        if on_conflict:
            prefer.append("resolution=merge-duplicates")
        prefer.append("return=representation" if returning else "return=minimal")
        params = {"on_conflict": on_conflict} if on_conflict else None
        r = self.session.post(
            f"{self.base}/{table}",
            params=params,
            headers={"Prefer": ",".join(prefer)},
            data=json.dumps(rows),
            timeout=120,
        )
        r.raise_for_status()
        return r.json() if returning else None

    def update(self, table: str, match: dict, body: dict) -> None:
        r = self.session.patch(
            f"{self.base}/{table}",
            params=match,
            headers={"Prefer": "return=minimal"},
            data=json.dumps(body),
            timeout=60,
        )
        r.raise_for_status()

    # --- helpers de alto nivel ------------------------------------------- #
    def catalog(self, source_id: str, wanted: str = "") -> list[dict]:
        """Series del catálogo de una fuente, opcionalmente filtradas por ID."""
        rows = self.select("series", {
            "select": "id,source_code,frequency,unit,attributes",
            "source_id": f"eq.{source_id}", "source_code": "not.is.null", "order": "id",
        })
        ids = {s.strip() for s in wanted.split(",") if s.strip()}
        if ids:
            unknown = ids - {s["id"] for s in rows}
            if unknown:
                print(f"Aviso: series no encontradas en el catálogo de {source_id}: "
                      f"{', '.join(sorted(unknown))}")
            rows = [s for s in rows if s["id"] in ids]
        return rows

    def upsert_observations(self, rows: list[dict]) -> None:
        for i in range(0, len(rows), UPSERT_CHUNK):
            self.insert("observations", rows[i:i + UPSERT_CHUNK],
                        on_conflict="series_id,as_of,vintage")

    def start_log(self, source_id: str, mode: str) -> int:
        row = self.insert("provider_sync_logs",
                          {"source_id": source_id, "status": "running", "notes": mode},
                          returning=True)[0]
        return row["id"]

    def finish_log(self, log_id: int, mode: str, results: dict, errors: dict) -> str:
        status = "ok" if not errors else ("failed" if not results else "partial")
        self.update("provider_sync_logs", {"id": f"eq.{log_id}"}, {
            "finished_at": now_utc().isoformat(),
            "status": status,
            "rows_imported": sum(r["rows"] for r in results.values()),
            "error": json.dumps(errors, ensure_ascii=False) if errors else None,
            "notes": json.dumps({"mode": mode, "series": results}, ensure_ascii=False),
        })
        return status


def summarize(rows: list[dict]) -> dict:
    return {
        "rows": len(rows),
        "missing": sum(r["quality_status"] == "missing" for r in rows),
        "first": min((r["as_of"] for r in rows), default=None),
        "last": max((r["as_of"] for r in rows), default=None),
    }


def run_catalog(db: Supabase, source_id: str, mode: str, catalog: list[dict], ingest_one) -> int:
    """Ejecuta ingest_one(serie) para cada serie, registra el resultado y devuelve el código de salida."""
    if not catalog:
        print(f"No hay series de {source_id} que descargar.")
        return 0
    log_id = db.start_log(source_id, mode)
    results, errors = {}, {}
    for s in catalog:
        try:
            r = ingest_one(s)
            results[s["id"]] = r
            print(f"OK   {s['id']:<22} {r['rows']:>7} filas  {r['first']} → {r['last']}")
        except Exception as exc:  # una serie que falla no detiene las demás
            errors[s["id"]] = str(exc)[:500]
            print(f"FAIL {s['id']:<22} {exc}")
    status = db.finish_log(log_id, mode, results, errors)
    print(f"{source_id}: {status}. Series correctas: {len(results)}, con error: {len(errors)}.")
    return 1 if errors else 0
