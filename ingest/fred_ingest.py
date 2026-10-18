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
- Solo se guardan observaciones desde HISTORY_START (2020-01-01).
- Series con attributes.vintages = 'latest' (p. ej. NFCI, que se recalcula
  entero cada semana) guardan una sola versión por fecha: vintage = 0, que se
  sobrescribe en cada ejecución, y published_at = fecha de la última versión.
  Estas series no son aptas para un backtest point-in-time.
- Series con attributes.vintages = 'none' no existen en ALFRED (p. ej. SP500,
  por derechos de autor): se descarga la versión vigente, vintage = 1 y
  published_at se estima como el día de la observación a las 23:59:59 hora de
  Nueva York (el cierre ya era público ese día).

Modos:
- incremental (por defecto): vintages de los últimos días; para series diarias,
  solo observaciones del último año largo.
- --backfill: todo el historial desde HISTORY_START con todas sus vintages.

Variables de entorno: FRED_API_KEY, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY.
Opcionales (para GitHub Actions): INGEST_BACKFILL=true, INGEST_SERIES=id1,id2.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import time
from zoneinfo import ZoneInfo

import requests

from common import HISTORY_START, Supabase, env_flag, now_utc, require_env, run_catalog, summarize

FRED_API = "https://api.stlouisfed.org/fred"
NEW_YORK = ZoneInfo("America/New_York")
SOURCE_ID = "fred"
FRED_PAGE = 100_000
VINTAGE_PAGE = 10_000
MAX_VINTAGES_PER_REQUEST = 2000   # límite de FRED por petición de observaciones
REALTIME_LOOKBACK_DAYS = 10    # modo incremental: vintages publicadas en estos días
DAILY_LOOKBACK_DAYS = 400      # modo incremental: ventana de observaciones de series diarias
FULL_REALTIME_START = "2019-12-01"    # vintages desde un mes antes, por si alguna se publica por adelantado
LATEST_VINTAGE = 0                    # vintage fija de las series que guardan solo la última versión
REALTIME_END = "9999-12-31"


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
def fetch_vintage_dates(api_key: str, code: str, realtime_start: str) -> list[str]:
    """Fechas en que FRED publicó una versión de la serie (ALFRED)."""
    out: list[str] = []
    offset = 0
    while True:
        data = fred_get("series/vintagedates", api_key, series_id=code,
                        realtime_start=realtime_start, realtime_end=REALTIME_END,
                        limit=VINTAGE_PAGE, offset=offset, sort_order="asc")
        page = data.get("vintage_dates", [])
        out.extend(page)
        offset += len(page)
        if not page or offset >= int(data.get("count", len(out))):
            return out


def realtime_windows(vintage_dates: list[str], realtime_start: str) -> list[tuple[str, str]]:
    """Parte el periodo en ventanas de como máximo MAX_VINTAGES_PER_REQUEST vintages.

    FRED rechaza una petición que abarque más de 2000 fechas de vintage. Cada
    ventana empieza en una fecha de vintage y termina el día antes de la
    siguiente ventana, así que no se solapan ni dejan huecos.
    """
    if len(vintage_dates) <= MAX_VINTAGES_PER_REQUEST:
        return [(realtime_start, REALTIME_END)]
    starts = vintage_dates[::MAX_VINTAGES_PER_REQUEST]
    starts[0] = realtime_start
    windows = []
    for i, start in enumerate(starts):
        if i + 1 < len(starts):
            end = (dt.date.fromisoformat(starts[i + 1]) - dt.timedelta(days=1)).isoformat()
        else:
            end = REALTIME_END
        windows.append((start, end))
    return windows


def fetch_observations(api_key: str, code: str, realtime_start: str,
                       observation_start: str | None,
                       realtime_end: str = REALTIME_END) -> list[dict]:
    """Descarga todas las observaciones (con vintages) de una ventana, paginando."""
    out: list[dict] = []
    offset = 0
    while True:
        params = dict(
            series_id=code,
            realtime_start=realtime_start,
            realtime_end=realtime_end,
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


def compress(rows: list[dict]) -> list[dict]:
    """Elimina duplicados y versiones que repiten el valor de la anterior.

    Al partir el periodo en ventanas, FRED repite al inicio de cada ventana las
    observaciones que seguían vigentes. Esas filas no son revisiones reales:
    tienen el mismo valor que la versión previa, así que se descartan.
    """
    unique: dict[tuple, dict] = {}
    for row in rows:
        unique[(row["series_id"], row["as_of"], row["vintage"])] = row
    kept: list[dict] = []
    previous: dict[tuple, dict] = {}
    for row in sorted(unique.values(), key=lambda r: (r["as_of"], r["vintage"])):
        key = (row["series_id"], row["as_of"])
        last = previous.get(key)
        if last is not None and last["value"] == row["value"]:
            continue
        kept.append(row)
        previous[key] = row
    return kept


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
    vintages_mode = (series.get("attributes") or {}).get("vintages", "all")
    latest_only = vintages_mode == "latest"
    if vintages_mode == "none":
        return ingest_without_vintages(db, api_key, series, backfill)
    if backfill:
        realtime_start, observation_start = FULL_REALTIME_START, HISTORY_START
    else:
        realtime_start = (today - dt.timedelta(days=REALTIME_LOOKBACK_DAYS)).isoformat()
        observation_start = max(HISTORY_START, (today - dt.timedelta(days=DAILY_LOOKBACK_DAYS)).isoformat()) \
            if series["frequency"] == "daily" else HISTORY_START

    # Nunca se rellena con la versión vigente si falla la descarga de vintages:
    # eso fecharía todo el historial como publicado hoy y falsearía el backtest.
    vintage_dates = fetch_vintage_dates(api_key, code, realtime_start)
    if latest_only:
        # Solo la última versión publicada, fechada en su día de publicación.
        windows = [(vintage_dates[-1], vintage_dates[-1])] if vintage_dates else []
    else:
        windows = realtime_windows(vintage_dates, realtime_start)
    raw: list[dict] = []
    for start, end in windows:
        raw.extend(fetch_observations(api_key, code, start, observation_start, end))

    rows = [to_row(series["id"], o) for o in raw]
    if latest_only:
        for row in rows:
            row["vintage"] = LATEST_VINTAGE
    rows = compress(rows)
    db.upsert_observations(rows)

    attrs = check_metadata(api_key, series)
    db.update("series", {"id": f"eq.{series['id']}"}, {"attributes": attrs})

    result = summarize(rows)
    result.update(fred_units=attrs.get("fred_units"), catalog_unit=series.get("unit"))
    if len(windows) > 1:
        result["windows"] = len(windows)
    if latest_only:
        result["vintages"] = "latest"
    return result


def ingest_without_vintages(db: Supabase, api_key: str, series: dict, backfill: bool) -> dict:
    """Series sin historial en ALFRED: versión vigente con fecha de publicación estimada."""
    today = dt.date.today()
    start = HISTORY_START if backfill else max(
        HISTORY_START, (today - dt.timedelta(days=DAILY_LOOKBACK_DAYS)).isoformat())
    raw = []
    offset = 0
    while True:   # sin realtime_start/realtime_end: FRED devuelve la versión vigente
        data = fred_get("series/observations", api_key, series_id=series["source_code"],
                        observation_start=start, sort_order="asc", limit=FRED_PAGE, offset=offset)
        page = data.get("observations", [])
        raw.extend(page)
        offset += len(page)
        if not page or offset >= int(data.get("count", len(raw))):
            break
    rows = []
    for o in raw:
        row = to_row(series["id"], {**o, "realtime_start": o["date"]})
        row["vintage"] = 1
        rows.append(row)
    rows = compress(rows)
    db.upsert_observations(rows)
    attrs = check_metadata(api_key, series)
    attrs["published_at"] = "estimada: día de la observación, 23:59:59 Nueva York"
    db.update("series", {"id": f"eq.{series['id']}"}, {"attributes": attrs})
    result = summarize(rows)
    result.update(fred_units=attrs.get("fred_units"), vintages="none")
    return result


# --------------------------------------------------------------------------- #
# Principal
# --------------------------------------------------------------------------- #
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Ingesta de FRED en Market Radar")
    p.add_argument("--backfill", action="store_true", default=env_flag("INGEST_BACKFILL"),
                   help="descargar todo el historial con todas sus vintages")
    p.add_argument("--series", default=os.environ.get("INGEST_SERIES", ""),
                   help="IDs internos separados por comas (vacío = todas las de FRED)")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    api_key = require_env("FRED_API_KEY")
    db = Supabase.from_env()
    mode = "backfill" if args.backfill else "incremental"
    return run_catalog(db, SOURCE_ID, mode, db.catalog(SOURCE_ID, args.series),
                       lambda s: ingest_series(db, api_key, s, args.backfill))


if __name__ == "__main__":
    sys.exit(main())
