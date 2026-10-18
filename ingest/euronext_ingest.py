#!/usr/bin/env python3
"""Ingesta de cierres diarios de Euronext en Market Radar.

Usa la descarga CSV de la web oficial de Euronext (live.euronext.com), un
acceso no documentado que podría cambiar. source_code = ISIN-MIC, por ejemplo
'FR0000121329-XPAR' (Thales en París).

Convenciones:
- Se guarda la columna Close (cierre oficial), en la divisa de cotización.
- Se pide por tramos de un año para no depender del límite de la descarga.
- published_at = fecha de la sesión a las 18:00 hora de París. vintage = 1.

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
from zoneinfo import ZoneInfo

import requests

from common import HISTORY_START, Supabase, env_flag, run_catalog, summarize

EURONEXT_URL = "https://live.euronext.com/en/ajax/AwlHistoricalPrice/getFullDownloadAjax"
SOURCE_ID = "euronext"
PARIS = ZoneInfo("Europe/Paris")
UA = {"User-Agent": "Mozilla/5.0 (market-radar personal use)"}
INCREMENTAL_LOOKBACK_DAYS = 10


def euronext_get(isin_mic: str, start: dt.date, end: dt.date) -> list[dict]:
    params = {"format": "csv", "decimal_separator": ".", "date_form": "d/m/Y", "op": "",
              "adjusted": "", "base100": "", "startdate": start.isoformat(),
              "enddate": end.isoformat()}
    for attempt in range(4):
        r = requests.get(f"{EURONEXT_URL}/{isin_mic}", params=params, headers=UA, timeout=60)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(10 * 2 ** attempt)
            continue
        r.raise_for_status()
        lines = r.text.lstrip("﻿").splitlines()
        header = next((i for i, l in enumerate(lines) if l.startswith("Date;")), None)
        if header is None:
            raise ValueError(f"Formato inesperado de Euronext: {' | '.join(lines[:4])[:200]}")
        return list(csv.DictReader(io.StringIO("\n".join(lines[header:])), delimiter=";"))
    raise RuntimeError("Euronext no respondió tras 4 intentos")


def to_row(series_id: str, item: dict) -> dict:
    day = dt.datetime.strptime(item["Date"].strip(), "%d/%m/%Y").date()
    raw = (item.get("Close") or "").strip().replace(",", "")
    missing = raw in ("", "-")
    return {
        "series_id": series_id,
        "as_of": day.isoformat(),
        "vintage": 1,
        "published_at": dt.datetime.combine(day, dt.time(18, 0), PARIS).isoformat(),
        "value": None if missing else raw,
        "quality_status": "missing" if missing else "ok",
    }


def ingest_series(db: Supabase, series: dict, backfill: bool) -> dict:
    today = dt.date.today()
    start = dt.date.fromisoformat(HISTORY_START) if backfill else max(
        dt.date.fromisoformat(HISTORY_START), today - dt.timedelta(days=INCREMENTAL_LOOKBACK_DAYS))
    rows: dict[str, dict] = {}
    chunk_start = start
    while chunk_start <= today:
        chunk_end = min(today, chunk_start + dt.timedelta(days=364))
        for item in euronext_get(series["source_code"], chunk_start, chunk_end):
            row = to_row(series["id"], item)
            if HISTORY_START <= row["as_of"] < today.isoformat():
                rows[row["as_of"]] = row
        chunk_start = chunk_end + dt.timedelta(days=1)
        time.sleep(1)
    out = sorted(rows.values(), key=lambda r: r["as_of"])
    db.upsert_observations(out)
    return summarize(out)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Ingesta de Euronext en Market Radar")
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
