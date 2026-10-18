#!/usr/bin/env python3
"""Posicionamiento del Commitments of Traders (CFTC) en Market Radar.

Fuente oficial: API pública de la CFTC (publicreporting.cftc.gov, Socrata), sin clave.
- Informe desagregado (materias primas): «managed money» (fondos gestionados).
- Informe Traders in Financial Futures (financieros): «leveraged funds» (fondos
  apalancados) y «asset managers» (gestoras).

Para cada mercado y categoría se guardan posiciones largas y cortas (contratos) y el
interés abierto total. La posición neta y los percentiles se calculan después.

Fechas: as_of = martes de las posiciones; published_at = viernes siguiente a las 15:30
de Nueva York (publicación habitual; los festivos pueden retrasarla). vintage = 1.
Los informes no identifican fondos concretos ni muestran posiciones en tiempo real.

Variables de entorno: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY.
Opcionales: INGEST_BACKFILL=true (desde 2020), INGEST_SERIES no se usa.
"""

from __future__ import annotations

import datetime as dt
import sys
from zoneinfo import ZoneInfo

import requests

from common import HISTORY_START, Supabase, env_flag, now_utc

API = "https://publicreporting.cftc.gov/resource"
DISAGG, TFF = "72hh-3qpy", "gpe5-46if"
NY = ZoneInfo("America/New_York")
UA = {"User-Agent": "Mozilla/5.0 (market-radar personal)"}

# código CFTC, instrumento, nombre, informe
MARKETS = [
    ("088691", "gold", "Oro (COMEX)", DISAGG),
    ("084691", "silver", "Plata (COMEX)", DISAGG),
    ("085692", "copper", "Cobre (COMEX)", DISAGG),
    ("067651", "wti", "Petróleo WTI (NYMEX)", DISAGG),
    ("13874A", "spx", "E-mini S&P 500 (CME)", TFF),
    ("209742", "ndx", "E-mini Nasdaq-100 (CME)", TFF),
    ("239742", "rut", "E-mini Russell 2000 (CME)", TFF),
    ("043602", "us10y", "Bono a 10 años (CBOT)", TFF),
    ("042601", "us02y", "Bono a 2 años (CBOT)", TFF),
    ("099741", "eurusd", "Euro FX (CME)", TFF),
    ("098662", "dxy", "Índice dólar (ICE US)", TFF),
    ("133741", "btc", "Bitcoin (CME)", TFF),
]
GROUPS = {
    DISAGG: [("mm", "fondos gestionados (managed money)", "m_money_positions_long_all", "m_money_positions_short_all")],
    TFF: [("lf", "fondos apalancados (leveraged funds)", "lev_money_positions_long", "lev_money_positions_short"),
          ("am", "gestoras (asset managers)", "asset_mgr_positions_long", "asset_mgr_positions_short")],
}


def series_catalog() -> list[dict]:
    rows = []
    for code, inst, name, report in MARKETS:
        base = {"instrument_id": inst, "family": "positioning", "frequency": "weekly", "source_id": "cftc",
                "unit": "contratos"}
        rows.append({**base, "id": f"cftc_{inst}_oi", "name": f"COT {name}: interés abierto",
                     "source_code": f"{report}:{code}:open_interest_all",
                     "attributes": {"report": "desagregado" if report == DISAGG else "TFF", "market_code": code}})
        for grp, label, long_f, short_f in GROUPS[report]:
            for side, field in (("long", long_f), ("short", short_f)):
                rows.append({**base, "id": f"cftc_{inst}_{grp}_{side}",
                             "name": f"COT {name}: {label}, posiciones {'largas' if side == 'long' else 'cortas'}",
                             "source_code": f"{report}:{code}:{field}",
                             "attributes": {"report": "desagregado" if report == DISAGG else "TFF",
                                            "market_code": code, "group": grp, "group_label": label, "side": side}})
    return rows


def fetch(report: str, codes: list[str], since: str) -> list[dict]:
    out, offset = [], 0
    where = f"cftc_contract_market_code in({','.join(repr(c) for c in codes)}) AND report_date_as_yyyy_mm_dd >= '{since}'"
    while True:
        r = requests.get(f"{API}/{report}.json", headers=UA, timeout=120,
                         params={"$where": where, "$order": "report_date_as_yyyy_mm_dd", "$limit": 5000,
                                 "$offset": offset})
        r.raise_for_status()
        batch = r.json()
        out += batch
        if len(batch) < 5000:
            return out
        offset += 5000


def publish_time(tuesday: dt.date) -> str:
    friday = tuesday + dt.timedelta(days=3)
    return dt.datetime.combine(friday, dt.time(15, 30), NY).isoformat()


def main() -> int:
    db = Supabase.from_env()
    backfill = env_flag("INGEST_BACKFILL")
    db.ensure_source("cftc", "CFTC Commitments of Traders", "https://publicreporting.cftc.gov",
                     "Desagregado (managed money) y TFF (leveraged funds, asset managers). Martes, publicado el viernes.")
    db.update("data_sources", {"id": "eq.cftc"}, {"status": "VERIFICADA", "verified_at": now_utc().isoformat()})
    catalog = series_catalog()
    db.ensure_series(catalog)
    since = HISTORY_START if backfill else (now_utc().date() - dt.timedelta(days=35)).isoformat()
    log_id = db.start_log("cftc", "backfill" if backfill else "incremental")
    results, errors = {}, {}
    by_source = {s["source_code"]: s["id"] for s in catalog}
    for report in (DISAGG, TFF):
        codes = [c for c, _, _, rep in MARKETS if rep == report]
        try:
            data = fetch(report, codes, since)
            rows = []
            for d in data:
                code = d.get("cftc_contract_market_code")
                tuesday = dt.date.fromisoformat(d["report_date_as_yyyy_mm_dd"][:10])
                for src, sid in by_source.items():
                    rep, c, field = src.split(":")
                    if rep != report or c != code:
                        continue
                    if field not in d:
                        raise KeyError(f"Falta el campo {field} en el informe {report} ({code})")
                    rows.append({"series_id": sid, "as_of": tuesday.isoformat(), "vintage": 1,
                                 "published_at": publish_time(tuesday), "value": d[field], "quality_status": "ok"})
            db.upsert_observations(rows)
            missing = set(codes) - {d.get("cftc_contract_market_code") for d in data}
            results[report] = {"rows": len(rows), "missing": len(missing),
                               "first": min((r["as_of"] for r in rows), default=None),
                               "last": max((r["as_of"] for r in rows), default=None)}
            print(f"OK   {report} {len(rows)} observaciones; mercados sin datos: {sorted(missing) or 'ninguno'}")
            if missing:
                print(f"::warning::COT sin datos para {sorted(missing)} en {report}")
        except Exception as exc:
            errors[report] = str(exc)[:500]
            print(f"::error::cftc {report}: {str(exc)[:300]}")
    status = db.finish_log(log_id, "backfill" if backfill else "incremental", results, errors)
    print(f"cftc: {status}.")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
