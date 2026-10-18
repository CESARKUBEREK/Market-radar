#!/usr/bin/env python3
"""Ingesta de datos del FMI en Market Radar.

Dos tipos de serie, según attributes.kind:

- 'irfcl_gold': reservas oficiales de oro (IRFCL, línea I.A.5, onzas troy finas,
  mensual). source_code = clave SDMX 2.1 del flujo IRFCL, p. ej.
  'IRFCL/USA.IRFCLDT1_IRFCL56V_FTO..M'. Se guarda en toneladas.
- 'weo': deuda bruta del gobierno general en % del PIB (WEO, anual).
  source_code = indicador@país, p. ej. 'GGXWDG_NGDP@JPN'. Los años desde el
  actual en adelante son proyecciones (is_forecast = true).

Punto en el tiempo: el FMI no publica fechas de publicación por observación.
- Valores nuevos o revisados detectados en una ejecución normal se guardan con
  published_at = momento de la descarga; una revisión crea un vintage nuevo.
- En la carga inicial (backfill), published_at es una estimación:
  oro, fin del mes + 45 días; WEO, momento de la descarga (solo se conoce la
  edición vigente; las ediciones anteriores no se reconstruyen).

Variables de entorno: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY.
Opcionales: INGEST_BACKFILL=true, INGEST_SERIES=id1,id2.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import os
import sys
import time
from decimal import Decimal

import requests

from common import HISTORY_START, Supabase, env_flag, now_utc, run_catalog

SOURCE_ID = "imf"
SDMX21 = "https://api.imf.org/external/sdmx/2.1/data"
DATAMAPPER = "https://www.imf.org/external/datamapper/api/v1"
UA = {"User-Agent": "Mozilla/5.0 (market-radar personal data)"}
TROY_OZ_PER_TONNE = Decimal("32150.7466")
GOLD_PUBLISH_LAG_DAYS = 45
# Comprobación de cordura: ningún banco central tiene más de 20.000 t.
GOLD_MAX_TONNES = Decimal(20000)


def http_get(url: str, *, params: dict | None = None, headers: dict | None = None) -> requests.Response:
    for attempt in range(4):
        r = requests.get(url, params=params, headers={**UA, **(headers or {})}, timeout=90)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(10 * 2 ** attempt)
            continue
        r.raise_for_status()
        return r
    raise RuntimeError(f"El FMI no respondió tras 4 intentos ({url})")


# --- oro (IRFCL) ---------------------------------------------------------- #
def scale_factor(raw: str | None) -> Decimal:
    """SCALE puede venir como exponente ('6') o como texto ('Millions')."""
    raw = (raw or "").strip()
    if not raw:
        return Decimal(1)
    if raw.lstrip("-").isdigit():
        return Decimal(10) ** int(raw)
    words = {"units": 0, "thousands": 3, "millions": 6, "billions": 9}
    for w, e in words.items():
        if w in raw.lower():
            return Decimal(10) ** e
    raise ValueError(f"Escala desconocida en IRFCL: {raw!r}")


def month_start(period: str) -> dt.date:
    # '2026-M08', '2026-08' o '2026M08'
    p = period.replace("M", "-").replace("--", "-")
    y, m = p.split("-")[:2]
    return dt.date(int(y), int(m), 1)


def fetch_gold(series: dict) -> list[tuple[dt.date, Decimal]]:
    flow, _, key = series["source_code"].partition("/")
    r = http_get(f"{SDMX21}/{flow}/{key}", params={"startPeriod": HISTORY_START[:7]},
                 headers={"Accept": "application/vnd.sdmx.data+csv;version=1.0.0"})
    rows = list(csv.DictReader(io.StringIO(r.text)))
    if rows and not {"TIME_PERIOD", "OBS_VALUE"} <= rows[0].keys():
        raise ValueError(f"Formato inesperado de IRFCL: columnas {list(rows[0].keys())[:10]}")
    wanted_sector = (series.get("attributes") or {}).get("sector")
    by_sector: dict[str, list[dict]] = {}
    for x in rows:
        if (x.get("OBS_VALUE") or "").strip() in ("", "NaN"):
            continue
        by_sector.setdefault(x.get("SECTOR") or "", []).append(x)
    if not by_sector:
        return []
    if wanted_sector:
        if wanted_sector not in by_sector:
            raise ValueError(f"Sector {wanted_sector} no disponible; hay {sorted(by_sector)}")
        chosen = by_sector[wanted_sector]
    elif len(by_sector) == 1:
        chosen = next(iter(by_sector.values()))
    else:
        raise ValueError(f"Varios sectores en IRFCL {sorted(by_sector)}: fija attributes.sector")
    out = []
    for x in chosen:
        ounces = Decimal(x["OBS_VALUE"].strip()) * scale_factor(x.get("SCALE"))
        tonnes = round(ounces / TROY_OZ_PER_TONNE, 3)
        if tonnes < 0 or tonnes > GOLD_MAX_TONNES:
            raise ValueError(f"Valor fuera de rango: {x['TIME_PERIOD']} = {tonnes} t "
                             f"(OBS_VALUE {x['OBS_VALUE']}, SCALE {x.get('SCALE')})")
        out.append((month_start(x["TIME_PERIOD"]), tonnes))
    return sorted(out)


# --- deuda/PIB (WEO) ------------------------------------------------------ #
def fetch_weo(series: dict) -> list[tuple[dt.date, Decimal]]:
    indicator, _, country = series["source_code"].partition("@")
    r = http_get(f"{DATAMAPPER}/{indicator}/{country}", headers={"Accept": "application/json"})
    try:
        data = r.json()
    except ValueError:
        raise ValueError(f"DataMapper no devolvió JSON (HTTP {r.status_code}, "
                         f"{r.headers.get('content-type', '')}): {r.text[:120]!r}")
    values = (data.get("values") or {}).get(indicator, {}).get(country, {})
    first = int(HISTORY_START[:4])
    return sorted((dt.date(int(y), 1, 1), Decimal(str(v)))
                  for y, v in values.items() if v is not None and int(y) >= first)


# --- común ---------------------------------------------------------------- #
def stored(db: Supabase, series_id: str) -> dict[str, dict]:
    """Último vintage guardado por fecha."""
    rows = db.select("observations", {
        "select": "as_of,vintage,value,is_forecast,published_at", "series_id": f"eq.{series_id}",
        "order": "as_of,vintage", "limit": "10000",
    })
    out: dict[str, dict] = {}
    for x in rows:
        x["value"] = None if x["value"] is None else Decimal(str(x["value"]))
        out[x["as_of"]] = x
    return out


def ingest_series(db: Supabase, series: dict, backfill: bool) -> dict:
    kind = (series.get("attributes") or {}).get("kind")
    if kind == "irfcl_gold":
        points = fetch_gold(series)
    elif kind == "weo":
        points = fetch_weo(series)
    else:
        raise ValueError(f"attributes.kind desconocido: {kind!r}")
    have = stored(db, series["id"])
    now = now_utc()
    this_year = now.year
    rows = []
    for as_of, value in points:
        prev = have.get(as_of.isoformat())
        forecast = kind == "weo" and as_of.year >= this_year
        if prev and prev["value"] == value:
            if bool(prev["is_forecast"]) != forecast:
                # Mismo dato, pero el año ya no es proyección: se corrige la marca.
                db.update("observations", {"series_id": f"eq.{series['id']}",
                                           "as_of": f"eq.{as_of.isoformat()}",
                                           "vintage": f"eq.{prev['vintage']}"},
                          {"is_forecast": forecast})
            continue                         # sin cambios de valor: no hay vintage nuevo
        vintage = 1 if not prev else prev["vintage"] + 1
        if prev is None and backfill and kind == "irfcl_gold":
            nxt = (as_of.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
            published = dt.datetime.combine(nxt + dt.timedelta(days=GOLD_PUBLISH_LAG_DAYS - 1),
                                            dt.time(12), dt.timezone.utc)
            published = min(published, now)
        else:
            published = now
        rows.append({
            "series_id": series["id"],
            "as_of": as_of.isoformat(),
            "vintage": vintage,
            "published_at": published.isoformat(),
            "value": str(value),
            "is_forecast": forecast,
            "quality_status": "ok",
        })
    if rows:
        db.upsert_observations(rows)
    return {
        "rows": len(rows),
        "missing": 0,
        "first": points[0][0].isoformat() if points else None,
        "last": points[-1][0].isoformat() if points else None,
    }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Ingesta del FMI en Market Radar")
    p.add_argument("--backfill", action="store_true", default=env_flag("INGEST_BACKFILL"))
    p.add_argument("--series", default=os.environ.get("INGEST_SERIES", ""))
    return p.parse_args()


def main() -> int:
    args = parse_args()
    db = Supabase.from_env()
    mode = "backfill" if args.backfill else "incremental"
    return run_catalog(db, SOURCE_ID, mode, db.catalog(SOURCE_ID, args.series),
                       lambda s: ingest_series(db, s, args.backfill))


if __name__ == "__main__":
    sys.exit(main())
