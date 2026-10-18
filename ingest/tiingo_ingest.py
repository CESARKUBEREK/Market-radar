#!/usr/bin/env python3
"""Ingesta de cierres diarios de Tiingo en Market Radar.

Lee del catálogo las series con source_id = 'tiingo' (source_code = ticker) y
guarda el cierre diario sin ajustar en `observations`.

Convenciones:
- Se guarda el cierre sin ajustar (`close`): no cambia cuando hay dividendos o
  splits, así que el dato no se reescribe hacia atrás. El ajustado se puede
  calcular después si hace falta.
- Tiingo no publica la hora exacta de cada dato. published_at se estima como
  la fecha de cotización a las 20:00 hora de Nueva York, después del cierre y
  de las correcciones habituales del día. vintage = 1; si Tiingo corrige un
  cierre, la fila se sobrescribe.
- Solo uso personal e interno, según las condiciones del plan gratuito.

Variables de entorno: TIINGO_API_KEY, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY.
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

from common import HISTORY_START, Supabase, env_flag, require_env, run_catalog, summarize

TIINGO_API = "https://api.tiingo.com/tiingo"
SOURCE_ID = "tiingo"
INCREMENTAL_LOOKBACK_DAYS = 10


class TiingoError(Exception):
    pass


def tiingo_get(ticker: str, api_key: str, start: str, endpoint: str = "daily") -> list[dict]:
    """endpoint = 'daily' (acciones y ETF) o 'fx' (divisas y metales: xauusd, xagusd)."""
    url = f"{TIINGO_API}/{endpoint}/{ticker}/prices"
    headers = {"Authorization": f"Token {api_key}", "Content-Type": "application/json"}
    params = {"startDate": start, "format": "json",
              "resampleFreq": "1day" if endpoint == "fx" else "daily"}
    for attempt in range(4):
        r = requests.get(url, headers=headers, params=params, timeout=60)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(10 * 2 ** attempt)
            continue
        if r.status_code >= 400:
            raise TiingoError(f"HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        if isinstance(data, dict):  # Tiingo devuelve {"detail": "..."} en algunos errores
            raise TiingoError(str(data)[:300])
        return data
    raise TiingoError("Tiingo no respondió tras 4 intentos")


def publish_moment(as_of: dt.date, attrs: dict) -> dt.datetime:
    """Momento estimado en que el cierre era conocido.

    Por defecto, el día de cotización a las 20:00 de Nueva York. Se puede
    cambiar por serie con attributes.publish_tz y attributes.publish_time
    (p. ej. CATL en Shenzhen: Asia/Shanghai, 16:00). En las series FX
    (attributes.endpoint = 'fx') la vela diaria es UTC y se cierra a las
    00:00 UTC del día siguiente.
    """
    if attrs.get("endpoint") == "fx":
        return dt.datetime.combine(as_of + dt.timedelta(days=1), dt.time(0, 30), dt.timezone.utc)
    tz = ZoneInfo(attrs.get("publish_tz", "America/New_York"))
    hh, mm = (int(x) for x in attrs.get("publish_time", "20:00").split(":"))
    return dt.datetime.combine(as_of, dt.time(hh, mm), tz)


def to_row(series_id: str, item: dict, attrs: dict | None = None) -> dict:
    as_of = dt.date.fromisoformat(item["date"][:10])
    close = item.get("close")
    missing = close is None
    return {
        "series_id": series_id,
        "as_of": as_of.isoformat(),
        "vintage": 1,
        "published_at": publish_moment(as_of, attrs or {}).isoformat(),
        "value": None if missing else repr(float(close)),
        "quality_status": "missing" if missing else "ok",
    }


def ingest_series(db: Supabase, api_key: str, series: dict, backfill: bool) -> dict:
    start = HISTORY_START if backfill else max(
        HISTORY_START, (dt.date.today() - dt.timedelta(days=INCREMENTAL_LOOKBACK_DAYS)).isoformat())
    attrs = series.get("attributes") or {}
    data = tiingo_get(series["source_code"], api_key, start, attrs.get("endpoint", "daily"))
    today = dt.date.today().isoformat()
    # Se descarta la vela del día en curso (FX): todavía no está cerrada.
    rows = [to_row(series["id"], d, attrs) for d in data
            if HISTORY_START <= d.get("date", "")[:10] < today]
    db.upsert_observations(rows)
    return summarize(rows)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Ingesta de Tiingo en Market Radar")
    p.add_argument("--backfill", action="store_true", default=env_flag("INGEST_BACKFILL"))
    p.add_argument("--series", default=os.environ.get("INGEST_SERIES", ""))
    return p.parse_args()


def main() -> int:
    args = parse_args()
    api_key = require_env("TIINGO_API_KEY")
    if api_key.lower().startswith("token "):   # tolera que se pegue con el prefijo "Token "
        api_key = api_key[6:].strip()
    if len(api_key) != 40 or any(c not in "0123456789abcdef" for c in api_key.lower()):
        print(f"::warning::TIINGO_API_KEY tiene {len(api_key)} caracteres; un token de Tiingo "
              "suele tener 40 caracteres hexadecimales.")
    db = Supabase.from_env()
    mode = "backfill" if args.backfill else "incremental"
    return run_catalog(db, SOURCE_ID, mode, db.catalog(SOURCE_ID, args.series),
                       lambda s: ingest_series(db, api_key, s, args.backfill))


if __name__ == "__main__":
    sys.exit(main())
