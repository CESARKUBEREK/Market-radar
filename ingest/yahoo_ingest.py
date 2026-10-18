#!/usr/bin/env python3
"""Ingesta de cierres diarios de Yahoo Finance en Market Radar.

FUENTE NO OFICIAL. Yahoo no ofrece una API pública documentada y puede cambiar
o bloquear este acceso sin aviso. Se usa, por decisión del usuario y solo para
uso personal, en series sin alternativa gratuita: cobre (HG=F, futuro continuo
de primer vencimiento), MOVE (^MOVE) y DXY (DX-Y.NYB).

Convenciones:
- source_code = símbolo de Yahoo. Se guarda el cierre sin ajustar.
- La fecha de cada vela se calcula en la zona horaria de la bolsa que indica
  Yahoo. Se descarta la vela del día en curso, que puede estar incompleta.
- Las velas sin cierre se descartan: Yahoo las emite en días sin sesión
  (aperturas del domingo, viernes de Tel Aviv hasta 2026, festivos).
- published_at se estima como la fecha de la vela a las 23:59 en la zona
  horaria de la bolsa. vintage = 1; si Yahoo corrige un cierre, se sobrescribe.

Variables de entorno: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY.
Opcionales: INGEST_BACKFILL=true, INGEST_SERIES=id1,id2.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import time
from zoneinfo import ZoneInfo

import requests

from common import HISTORY_START, Supabase, env_flag, run_catalog, summarize

YAHOO_API = "https://query1.finance.yahoo.com/v8/finance/chart"
SOURCE_ID = "yahoo"
UA = {"User-Agent": "Mozilla/5.0 (market-radar personal use)"}
INCREMENTAL_LOOKBACK_DAYS = 10


def yahoo_get(symbol: str, start: dt.date) -> dict:
    period1 = int(dt.datetime.combine(start, dt.time(0), dt.timezone.utc).timestamp())
    period2 = int(time.time())
    for attempt in range(5):
        r = requests.get(f"{YAHOO_API}/{symbol}", headers=UA, timeout=60, params={
            "period1": period1, "period2": period2, "interval": "1d", "events": "div,split"})
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(15 * 2 ** attempt)
            continue
        r.raise_for_status()
        chart = r.json().get("chart", {})
        if chart.get("error"):
            raise RuntimeError(f"Yahoo: {chart['error']}")
        return chart["result"][0]
    raise RuntimeError("Yahoo no respondió tras 5 intentos")


def to_rows(series_id: str, result: dict) -> list[dict]:
    tz = ZoneInfo(result["meta"].get("exchangeTimezoneName") or "America/New_York")
    today = dt.datetime.now(tz).date()
    closes = result["indicators"]["quote"][0].get("close") or []
    rows: dict[str, dict] = {}
    for ts, close in zip(result.get("timestamp") or [], closes):
        day = dt.datetime.fromtimestamp(ts, tz).date()
        if day >= today or day.isoformat() < HISTORY_START:
            continue
        if close is None:
            # Yahoo devuelve velas vacías en días sin sesión: aperturas del domingo
            # (DX-Y.NYB), viernes de Tel Aviv antes de 2026, festivos. No son datos.
            continue
        missing = False
        rows[day.isoformat()] = {
            "series_id": series_id,
            "as_of": day.isoformat(),
            "vintage": 1,
            "published_at": dt.datetime.combine(day, dt.time(23, 59), tz).isoformat(),
            "value": None if missing else repr(round(float(close), 6)),
            "quality_status": "missing" if missing else "ok",
        }
    return list(rows.values())


def ingest_series(db: Supabase, series: dict, backfill: bool) -> dict:
    start = dt.date.fromisoformat(HISTORY_START) if backfill else max(
        dt.date.fromisoformat(HISTORY_START),
        dt.date.today() - dt.timedelta(days=INCREMENTAL_LOOKBACK_DAYS))
    rows = to_rows(series["id"], yahoo_get(series["source_code"], start))
    db.upsert_observations(rows)
    time.sleep(2)  # ritmo prudente entre símbolos
    return summarize(rows)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Ingesta de Yahoo Finance en Market Radar")
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
