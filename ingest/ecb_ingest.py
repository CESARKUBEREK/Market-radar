#!/usr/bin/env python3
"""Ingesta de tipos de cambio de referencia del BCE en Market Radar.

Lee del catálogo las series con source_id = 'ecb'. source_code es la clave
SDMX del BCE (p. ej. 'EXR/D.CHF.EUR.SP00.A', francos suizos por euro).

Si la serie tiene attributes.invert = true, se guarda 1 / valor (para CHF/EUR,
euros por franco).

Convenciones:
- El BCE publica los tipos de referencia hacia las 16:00 hora de Fráncfort.
  published_at = fecha a las 16:30 hora de Fráncfort; vintage = 1.
- No requiere clave de API.

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
from decimal import Decimal, getcontext
from zoneinfo import ZoneInfo

import requests

from common import HISTORY_START, Supabase, env_flag, run_catalog, summarize

ECB_API = "https://data-api.ecb.europa.eu/service/data"
SOURCE_ID = "ecb"
FRANKFURT = ZoneInfo("Europe/Berlin")
PUBLISH_TIME = dt.time(16, 30)
INCREMENTAL_LOOKBACK_DAYS = 10
getcontext().prec = 28


def ecb_get(key: str, start: str) -> list[dict]:
    flow, _, series_key = key.partition("/")
    url = f"{ECB_API}/{flow}/{series_key}"
    for attempt in range(4):
        r = requests.get(url, params={"startPeriod": start, "format": "csvdata"}, timeout=60)
        if r.status_code == 404:          # el BCE responde 404 cuando no hay datos en el periodo
            return []
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(10 * 2 ** attempt)
            continue
        r.raise_for_status()
        rows = list(csv.DictReader(io.StringIO(r.text)))
        if rows and not {"TIME_PERIOD", "OBS_VALUE"} <= rows[0].keys():
            raise ValueError(f"Formato inesperado del BCE: columnas {list(rows[0].keys())[:10]}")
        return rows
    raise RuntimeError("El BCE no respondió tras 4 intentos")


def to_row(series_id: str, item: dict, invert: bool) -> dict:
    as_of = dt.date.fromisoformat(item["TIME_PERIOD"])
    raw = (item.get("OBS_VALUE") or "").strip()
    missing = raw in ("", "NaN")
    value = None
    if not missing:
        number = Decimal(raw)
        value = str(round(Decimal(1) / number, 10) if invert else number)
    return {
        "series_id": series_id,
        "as_of": as_of.isoformat(),
        "vintage": 1,
        "published_at": dt.datetime.combine(as_of, PUBLISH_TIME, FRANKFURT).isoformat(),
        "value": value,
        "quality_status": "missing" if missing else "ok",
    }


def ingest_series(db: Supabase, series: dict, backfill: bool) -> dict:
    start = HISTORY_START if backfill else max(
        HISTORY_START, (dt.date.today() - dt.timedelta(days=INCREMENTAL_LOOKBACK_DAYS)).isoformat())
    invert = bool((series.get("attributes") or {}).get("invert"))
    rows = [to_row(series["id"], d, invert) for d in ecb_get(series["source_code"], start)]
    db.upsert_observations(rows)
    return summarize(rows)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Ingesta del BCE en Market Radar")
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
