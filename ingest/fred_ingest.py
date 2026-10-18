#!/usr/bin/env python3
"""Ingesta de series de FRED/ALFRED en Market Radar (Supabase).

Lee del catálogo (`series`) las series con source_id = 'fred', descarga sus
observaciones con fecha de publicación real (realtime_start de ALFRED) y las
guarda en `observations`. Cada ejecución queda registrada en
`provider_sync_logs`.

Convenciones:
- vintage = realtime_start en formato AAAAMMDD. Así cada revisión de FRED es
  una fila distinta y estable, y observations_known_at() puede ordenar por
  vintage.
- published_at = realtime_start a las 23:59:59 hora de Nueva York. FRED solo da
  la fecha de publicación, no la hora; usar el final del día es la opción
  conservadora para el backtest (nunca se usa un dato antes de tiempo).
- Un valor "." de FRED es un dato ausente: value = NULL, quality_status = 'missing'.

Modos:
- incremental (por defecto): vintages de los últimos días; para series diarias,
  solo observaciones del último año largo.
- --backfill: todo el historial con todas sus vintages.

Variables de entorno: FRED_API_KEY, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY.
Opcionales (para GitHub Actions): INGEST_BACKFILL=true, INGEST_SERIES=id1,id2.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from zoneinfo import ZoneInfo

import requests

FRED_API = "https://api.stlouisfed.org/fred"
NEW_YORK = ZoneInfo("America/New_York")
SOURCE_ID = "fred"
UPSERT_CHUNK = 1000
FRED_PAGE = 100_000
REALTIME_LOOKBACK_DAYS = 10    # modo incremental: vintages publicadas en estos días
DAILY_LOOKBACK_DAYS = 400      # modo incremental: ventana de observaciones de series diarias
FULL_REALTIME_START = "1776-07-04"
REALTIME_END = "9999-12-31"


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #
def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"Falta la variable de entorno {name}")
    return value


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


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


class FredError(Exception):
    pass


def fred_get(path: str, api_key: str, **params) -> dict:
    """GET a la API de FRED con reintentos ante límites (429) y errores 5xx."""
    params.update(api_key=api_key, file_type="json")
    last = None
    for attempt in range(5):
        r = requests.get(f"{FRED_API}/{path}", params=params, timeout=90)
        if r.status_code == 429 or r.status_code >= 500:
            last = r
            time.sleep(5 * 2 ** attempt)
            continue
        if r.status_code >= 400:
            try:
                msg = r.json().get("error_message", r.text)
            except ValueError:
                msg = r.text
            raise FredError(f"HTTP {r.status_code}: {msg}")
        return r.json()
    raise FredError(f"FRED no respondió tras 5 intentos (último HTTP {last.status_code if last else '?'})")


# --------------------------------------------------------------------------- #
# Lógica de FRED
# --------------------------------------------------------------------------- #
def fetch_observations(api_key: str, code: str, realtime_start: str,
                       observation_start: str | None) -> list[dict]:
    """Descarga todas las observaciones (con vintages) paginando."""
    out: list[dict] = []
    offset = 0
    while True:
        params = dict(
            series_id=code,
            realtime_start=realtime_start,
            realtime_end=REALTIME_END,
            output_type=1,          # una fila por observación y periodo de vigencia
            sort_order="asc",
            limit=FRED_PAGE,
            offset=offset,
        )
        if observation_start:
            params["observation_start"] = observation_start
        data = fred_get("series/observations", api_key, **params)
        page = data.get("observations", [])
        out.extend(page)
        total = int(data.get("count", len(out)))
        offset += len(page)
        if not page or offset >= total:
            return out


def to_row(series_id: str, obs: dict) -> dict:
    """Convierte una observación de FRED en una fila de `observations`."""
    realtime_start = dt.date.fromisoformat(obs["realtime_start"])
    published_at = dt.datetime.combine(realtime_start, dt.time(23, 59, 59), NEW_YORK)
    raw = (obs.get("value") or "").strip()
    missing = raw in ("", ".")
    return {
        "series_id": series_id,
        "as_of": obs["date"],
        "vintage": int(realtime_start.strftime("%Y%m%d")),
        "published_at": published_at.isoformat(),
        "value": None if missing else raw,      # texto: PostgREST lo guarda como numeric sin perder precisión
        "quality_status": "missing" if missing else "ok",
    }


def dedupe(rows: list[dict]) -> list[dict]:
    seen: dict[tuple, dict] = {}
    for row in rows:
        seen[(row["series_id"], row["as_of"], row["vintage"])] = row
    return list(seen.values())


def check_metadata(api_key: str, series: dict) -> dict:
    """Lee unidades y frecuencia oficiales de FRED para verificar el catálogo."""
    info = fred_get("series", api_key, series_id=series["source_code"])["seriess"][0]
    attrs = dict(series.get("attributes") or {})
    attrs.update(
        fred_title=info.get("title"),
        fred_units=info.get("units"),
        fred_frequency=info.get("frequency"),
        fred_seasonal_adjustment=info.get("seasonal_adjustment_short"),
        fred_observation_start=info.get("observation_start"),
        fred_last_updated=info.get("last_updated"),
        fred_checked_at=now_utc().isoformat(),
    )
    return attrs


def ingest_series(db: Supabase, api_key: str, series: dict, backfill: bool) -> dict:
    code = series["source_code"]
    today = dt.date.today()
    if backfill:
        realtime_start, observation_start = FULL_REALTIME_START, None
    else:
        realtime_start = (today - dt.timedelta(days=REALTIME_LOOKBACK_DAYS)).isoformat()
        observation_start = (
            (today - dt.timedelta(days=DAILY_LOOKBACK_DAYS)).isoformat()
            if series["frequency"] == "daily" else None
        )

    note = None
    try:
        raw = fetch_observations(api_key, code, realtime_start, observation_start)
    except FredError as exc:
        # Algunas series de terceros no admiten historial de vintages: se descarga
        # solo la versión vigente y se deja constancia.
        if not backfill:
            raise
        raw = fetch_observations(api_key, code, today.isoformat(), None)
        note = f"sin vintages históricas ({exc}); solo versión vigente"

    rows = dedupe([to_row(series["id"], o) for o in raw])
    for i in range(0, len(rows), UPSERT_CHUNK):
        db.insert("observations", rows[i:i + UPSERT_CHUNK],
                  on_conflict="series_id,as_of,vintage")

    attrs = check_metadata(api_key, series)
    db.update("series", {"id": f"eq.{series['id']}"}, {"attributes": attrs})

    result = {
        "rows": len(rows),
        "missing": sum(r["quality_status"] == "missing" for r in rows),
        "first": rows[0]["as_of"] if rows else None,
        "last": max(r["as_of"] for r in rows) if rows else None,
        "fred_units": attrs.get("fred_units"),
        "catalog_unit": series.get("unit"),
    }
    if note:
        result["note"] = note
    return result


# --------------------------------------------------------------------------- #
# Principal
# --------------------------------------------------------------------------- #
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Ingesta de FRED en Market Radar")
    p.add_argument("--backfill", action="store_true",
                   default=os.environ.get("INGEST_BACKFILL", "").lower() == "true",
                   help="descargar todo el historial con todas sus vintages")
    p.add_argument("--series", default=os.environ.get("INGEST_SERIES", ""),
                   help="IDs internos separados por comas (vacío = todas las de FRED)")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    api_key = require_env("FRED_API_KEY")
    db = Supabase(require_env("SUPABASE_URL"), require_env("SUPABASE_SERVICE_ROLE_KEY"))

    params = {"select": "id,source_code,frequency,unit,attributes",
              "source_id": f"eq.{SOURCE_ID}", "source_code": "not.is.null", "order": "id"}
    catalog = db.select("series", params)
    wanted = {s.strip() for s in args.series.split(",") if s.strip()}
    if wanted:
        unknown = wanted - {s["id"] for s in catalog}
        if unknown:
            print(f"Aviso: series no encontradas en el catálogo: {', '.join(sorted(unknown))}")
        catalog = [s for s in catalog if s["id"] in wanted]
    if not catalog:
        print("No hay series de FRED que descargar.")
        return 0

    log = db.insert("provider_sync_logs", {"source_id": SOURCE_ID, "status": "running",
                                           "notes": "backfill" if args.backfill else "incremental"},
                    returning=True)[0]

    results, errors = {}, {}
    for s in catalog:
        try:
            results[s["id"]] = ingest_series(db, api_key, s, args.backfill)
            r = results[s["id"]]
            print(f"OK   {s['id']:<22} {r['rows']:>7} filas  {r['first']} → {r['last']}  "
                  f"unidades FRED: {r['fred_units']}")
        except Exception as exc:  # una serie que falla no detiene las demás
            errors[s["id"]] = str(exc)[:500]
            print(f"FAIL {s['id']:<22} {exc}")

    status = "ok" if not errors else ("failed" if not results else "partial")
    db.update("provider_sync_logs", {"id": f"eq.{log['id']}"}, {
        "finished_at": now_utc().isoformat(),
        "status": status,
        "rows_imported": sum(r["rows"] for r in results.values()),
        "error": json.dumps(errors, ensure_ascii=False) if errors else None,
        "notes": json.dumps({"mode": "backfill" if args.backfill else "incremental",
                             "series": results}, ensure_ascii=False),
    })
    print(f"Estado: {status}. Series correctas: {len(results)}, con error: {len(errors)}.")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
