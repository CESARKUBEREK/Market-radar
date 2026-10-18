#!/usr/bin/env python3
"""Derivados cripto, flujos de ETF y volumen para el detector de situaciones excepcionales.

1. Derivados de bitcoin (APIs públicas, sin clave; Binance y Bybit bloquean las IP de GitHub):
   - OKX: interés abierto y volumen diarios de todos sus contratos BTC (rubik), financiación
     del perpetuo BTC-USDT y liquidaciones del perpetuo BTC-USDT de las últimas 24 horas.
   - Deribit: financiación horaria del perpetuo (historial desde 2020) e interés abierto actual.
   - Hyperliquid: interés abierto y financiación actuales.
   La financiación se guarda anualizada en %. Las liquidaciones son solo de OKX: confirman
   cierres forzosos en esa plataforma, no en todo el mercado.
2. Flujos de ETF: acciones en circulación y valor liquidativo publicados por el emisor
   (State Street: SPY y GLD, historial completo). Flujo neto = variación de acciones ×
   valor liquidativo. Es distinto del volumen negociado y de los cambios de patrimonio por
   precio. IBIT (iShares): acciones en circulación leídas de la web del fondo cada día.
3. Volumen diario (Tiingo) de ETF representativos y de las acciones seguidas.

Variables de entorno: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, TIINGO_API_KEY.
Opcional: INGEST_BACKFILL=true.
"""

from __future__ import annotations

import datetime as dt
import io
import re
import statistics
import sys
import time
from zoneinfo import ZoneInfo

import requests

from common import HISTORY_START, Supabase, env_flag, now_utc, require_env

UA = {"User-Agent": "Mozilla/5.0 (market-radar personal)"}
NY = ZoneInfo("America/New_York")
SOURCE = "derivados"

ETF_SSGA = {"SPY": "spx", "GLD": "gold"}
VOLUME = {"SPY": "spx", "QQQ": "ndx", "IWM": "rut", "GLD": "gold", "SLV": "silver", "USO": "wti",
          "TLT": "us10y", "UUP": "dxy", "IBIT": "btc", "RTX": "rtx", "YPF": "ypf", "BABA": "alibaba"}


def catalog() -> list[dict]:
    rows = []
    lev = {"family": "leverage", "frequency": "daily", "instrument_id": "btc"}
    rows += [
        {**lev, "id": "okx_btc_oi_usd", "name": "OKX: interés abierto de futuros y perpetuos BTC", "unit": "USD",
         "source_id": "okx", "source_code": "rubik/stat/contracts/open-interest-volume?ccy=BTC"},
        {**lev, "id": "okx_btc_volume_usd", "name": "OKX: volumen de futuros y perpetuos BTC", "unit": "USD",
         "source_id": "okx", "source_code": "rubik/stat/contracts/open-interest-volume?ccy=BTC"},
        {**lev, "id": "okx_btc_funding_ann", "name": "OKX: financiación BTC-USDT perpetuo (media diaria, anualizada)",
         "unit": "% anual", "source_id": "okx", "source_code": "public/funding-rate-history?instId=BTC-USDT-SWAP"},
        {**lev, "id": "okx_btc_liq_long_usd", "name": "OKX: liquidaciones de largos BTC-USDT perpetuo (24 h)",
         "unit": "USD", "source_id": "okx", "source_code": "public/liquidation-orders?uly=BTC-USDT"},
        {**lev, "id": "okx_btc_liq_short_usd", "name": "OKX: liquidaciones de cortos BTC-USDT perpetuo (24 h)",
         "unit": "USD", "source_id": "okx", "source_code": "public/liquidation-orders?uly=BTC-USDT"},
        {**lev, "id": "deribit_btc_funding_ann", "name": "Deribit: financiación BTC perpetuo (media diaria, anualizada)",
         "unit": "% anual", "source_id": "deribit", "source_code": "public/get_funding_rate_history?BTC-PERPETUAL"},
        {**lev, "id": "deribit_btc_oi_usd", "name": "Deribit: interés abierto BTC perpetuo", "unit": "USD",
         "source_id": "deribit", "source_code": "public/get_book_summary_by_instrument?BTC-PERPETUAL"},
        {**lev, "id": "hl_btc_oi_usd", "name": "Hyperliquid: interés abierto BTC perpetuo", "unit": "USD",
         "source_id": "hyperliquid", "source_code": "info:metaAndAssetCtxs"},
        {**lev, "id": "hl_btc_funding_ann", "name": "Hyperliquid: financiación BTC perpetuo (anualizada)",
         "unit": "% anual", "source_id": "hyperliquid", "source_code": "info:metaAndAssetCtxs"},
    ]
    for t, inst in {**ETF_SSGA, "IBIT": "btc"}.items():
        src = "ssga" if t in ETF_SSGA else "ishares"
        base = {"family": "flows", "frequency": "daily", "instrument_id": inst, "source_id": src,
                "attributes": {"etf": t}}
        rows.append({**base, "id": f"etf_{t.lower()}_shares", "name": f"ETF {t}: acciones en circulación",
                     "unit": "acciones", "source_code": t})
        rows.append({**base, "id": f"etf_{t.lower()}_flow_usd", "name": f"ETF {t}: flujo neto estimado "
                     "(variación de acciones × valor liquidativo)", "unit": "USD", "source_code": t})
        if t in ETF_SSGA:
            rows.append({**base, "id": f"etf_{t.lower()}_nav", "name": f"ETF {t}: valor liquidativo", "unit": "USD",
                         "source_code": t})
    for t, inst in VOLUME.items():
        rows.append({"id": f"vol_{t.lower()}", "name": f"Volumen diario de {t}", "family": "flows",
                     "frequency": "daily", "instrument_id": inst, "source_id": "tiingo", "unit": "acciones",
                     "source_code": t, "attributes": {"metric": "volume", "ticker": t}})
    return rows


def obs(series_id: str, day: dt.date, value, published: dt.datetime) -> dict:
    return {"series_id": series_id, "as_of": day.isoformat(), "vintage": 1,
            "published_at": published.astimezone(dt.timezone.utc).isoformat(),
            "value": None if value is None else str(value),
            "quality_status": "missing" if value is None else "ok"}


def get(url: str, **kw) -> requests.Response:
    for attempt in range(4):
        r = requests.get(url, headers=kw.pop("headers", UA), timeout=60, **kw)
        if r.status_code in (429, 500, 502, 503):
            time.sleep(5 * 2 ** attempt)
            continue
        r.raise_for_status()
        return r
    raise RuntimeError(f"Sin respuesta de {url}")


def utc_day(ms: int | str) -> dt.date:
    return dt.datetime.fromtimestamp(int(ms) / 1000, dt.timezone.utc).date()


# --- cripto ------------------------------------------------------------------ #
def okx_oi() -> list[dict]:
    data = get("https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-volume",
               params={"ccy": "BTC", "period": "1D"}).json()["data"]
    out = []
    today = now_utc().date()
    for ts, oi, vol in data:
        day = utc_day(ts)
        if day >= today:            # el día en curso aún no está cerrado
            continue
        pub = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0, 30), dt.timezone.utc)
        out += [obs("okx_btc_oi_usd", day, round(float(oi)), pub), obs("okx_btc_volume_usd", day, round(float(vol)), pub)]
    return out


def okx_funding() -> list[dict]:
    rates: dict[dt.date, list[float]] = {}
    after = None
    for _ in range(10):
        params = {"instId": "BTC-USDT-SWAP", "limit": 100}
        if after:
            params["after"] = after
        data = get("https://www.okx.com/api/v5/public/funding-rate-history", params=params).json()["data"]
        if not data:
            break
        for d in data:
            rates.setdefault(utc_day(d["fundingTime"]), []).append(float(d.get("realizedRate") or d["fundingRate"]))
        after = data[-1]["fundingTime"]
    today = now_utc().date()
    return [obs("okx_btc_funding_ann", day, round(statistics.mean(v) * 3 * 365 * 100, 3),
                dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0, 30), dt.timezone.utc))
            for day, v in rates.items() if day < today]


def okx_liquidations() -> list[dict]:
    """Suma de las liquidaciones de las últimas 24 h (contrato BTC-USDT: 0,01 BTC por contrato)."""
    cutoff = int((now_utc() - dt.timedelta(hours=24)).timestamp() * 1000)
    longs = shorts = 0.0
    after, pages, complete = None, 0, False
    t0 = time.monotonic()
    while pages < 60 and time.monotonic() - t0 < 120:
        params = {"instType": "SWAP", "uly": "BTC-USDT", "state": "filled", "limit": 100}
        if after:
            params["after"] = after
        data = get("https://www.okx.com/api/v5/public/liquidation-orders", params=params).json()["data"]
        details = [x for blk in data for x in blk.get("details", [])]
        if not details:
            complete = True
            break
        for x in details:
            if int(x["ts"]) < cutoff:
                complete = True
                continue
            usd = float(x["sz"]) * 0.01 * float(x["bkPx"])
            if x.get("posSide") == "long" or (x.get("posSide") == "net" and x.get("side") == "sell"):
                longs += usd
            else:
                shorts += usd
        if complete:
            break
        after = min(int(x["ts"]) for x in details)
        pages += 1
    if not complete:
        print("::warning::Liquidaciones de OKX: no se pudo cubrir 24 h completas (límite de páginas)")
    day, pub = now_utc().date(), now_utc()
    return [obs("okx_btc_liq_long_usd", day, round(longs), pub), obs("okx_btc_liq_short_usd", day, round(shorts), pub)]


def deribit_funding(backfill: bool) -> list[dict]:
    start = dt.datetime.fromisoformat(HISTORY_START).replace(tzinfo=dt.timezone.utc) if backfill \
        else now_utc() - dt.timedelta(days=10)
    rates: dict[dt.date, list[float]] = {}
    cur = start
    t0 = time.monotonic()
    while cur < now_utc():
        if time.monotonic() - t0 > 300 and now_utc() - cur > dt.timedelta(days=2):
            print(f"::warning::Deribit: historial de financiación cortado en {cur.date()} por tiempo; "
                  "se completará en próximas cargas")
            break
        end = min(cur + dt.timedelta(days=30), now_utc())
        t1 = time.monotonic()
        res = requests.get("https://www.deribit.com/api/v2/public/get_funding_rate_history", headers=UA, timeout=30,
                           params={"instrument_name": "BTC-PERPETUAL", "start_timestamp": int(cur.timestamp() * 1000),
                                   "end_timestamp": int(end.timestamp() * 1000)})
        res.raise_for_status()
        res = res.json()["result"]
        print(f"Deribit {cur.date()}: {len(res)} registros en {time.monotonic() - t1:.1f} s")
        for d in res:
            rates.setdefault(utc_day(d["timestamp"]), []).append(float(d["interest_8h"]))
        cur = end
    today = now_utc().date()
    return [obs("deribit_btc_funding_ann", day, round(statistics.mean(v) * 3 * 365 * 100, 3),
                dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0, 30), dt.timezone.utc))
            for day, v in rates.items() if day < today]


def snapshots() -> list[dict]:
    day, pub = now_utc().date(), now_utc()
    out = []
    d = get("https://www.deribit.com/api/v2/public/get_book_summary_by_instrument",
            params={"instrument_name": "BTC-PERPETUAL"}).json()["result"][0]
    out.append(obs("deribit_btc_oi_usd", day, round(float(d["open_interest"])), pub))
    r = requests.post("https://api.hyperliquid.xyz/info", json={"type": "metaAndAssetCtxs"}, headers=UA, timeout=30)
    r.raise_for_status()
    meta, ctxs = r.json()
    idx = next(i for i, a in enumerate(meta["universe"]) if a["name"] == "BTC")
    c = ctxs[idx]
    out.append(obs("hl_btc_oi_usd", day, round(float(c["openInterest"]) * float(c["markPx"])), pub))
    out.append(obs("hl_btc_funding_ann", day, round(float(c["funding"]) * 24 * 365 * 100, 3), pub))
    return out


# --- ETF -------------------------------------------------------------------- #
def ssga(ticker: str, backfill: bool) -> list[dict]:
    import openpyxl
    url = f"https://www.ssga.com/us/en/intermediary/library-content/products/fund-data/etfs/us/navhist-us-en-{ticker.lower()}.xlsx"
    wb = openpyxl.load_workbook(io.BytesIO(get(url).content), read_only=True)
    rows = []
    started = False
    for row in wb.worksheets[0].iter_rows(values_only=True):
        if row and row[0] == "Date":
            started = True
            continue
        if started and row and row[0] and row[2]:
            try:
                day = dt.datetime.strptime(str(row[0]).strip(), "%d-%b-%Y").date()
                rows.append((day, float(row[1]), float(row[2])))
            except (TypeError, ValueError):
                continue                    # filas con «-» u otros valores no numéricos
    rows.sort()
    first = dt.date.fromisoformat(HISTORY_START) if backfill else now_utc().date() - dt.timedelta(days=15)
    out = []
    t = ticker.lower()
    for i, (day, nav, shares) in enumerate(rows):
        if day < first:
            continue
        pub = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(9, 0), NY)
        out += [obs(f"etf_{t}_shares", day, round(shares), pub), obs(f"etf_{t}_nav", day, round(nav, 6), pub)]
        if i > 0:
            out.append(obs(f"etf_{t}_flow_usd", day, round((shares - rows[i - 1][2]) * nav), pub))
    if not out:
        raise ValueError(f"{ticker}: el archivo de State Street no trae datos reconocibles")
    return out


def ibit(db: Supabase) -> list[dict]:
    html = get("https://www.ishares.com/us/products/333011/ishares-bitcoin-trust-etf").text
    m = re.search(r"Shares Outstanding.{0,600}?([\d]{1,3}(?:,\d{3}){2,})", html, re.S)
    d = re.search(r"Shares Outstanding.{0,300}?as of ([A-Z][a-z]{2} \d{1,2}, \d{4})", html, re.S)
    if not m:
        raise ValueError("No se encuentra «Shares Outstanding» en la página de IBIT")
    shares = int(m.group(1).replace(",", ""))
    day = dt.datetime.strptime(d.group(1), "%b %d, %Y").date() if d else now_utc().date()
    pub = now_utc()
    out = [obs("etf_ibit_shares", day, shares, pub)]
    prev = db.select("observations", {"select": "as_of,value", "series_id": "eq.etf_ibit_shares",
                                      "as_of": f"lt.{day.isoformat()}", "order": "as_of.desc", "limit": "1"})
    nav_proxy = None
    navs = re.search(r"NAV.{0,300}?\$([\d.]+)", html, re.S)
    if navs:
        nav_proxy = float(navs.group(1))
    if prev and nav_proxy:
        out.append(obs("etf_ibit_flow_usd", day, round((shares - float(prev[0]["value"])) * nav_proxy), pub))
    return out


# --- volumen ----------------------------------------------------------------- #
def volumes(backfill: bool, key: str) -> list[dict]:
    start = HISTORY_START if backfill else (now_utc().date() - dt.timedelta(days=12)).isoformat()
    out = []
    for t in VOLUME:
        r = get(f"https://api.tiingo.com/tiingo/daily/{t}/prices", headers={"Authorization": f"Token {key}"},
                params={"startDate": start})
        today = dt.datetime.now(NY).date()
        for d in r.json():
            day = dt.date.fromisoformat(d["date"][:10])
            if day >= today:
                continue
            out.append(obs(f"vol_{t.lower()}", day, d.get("volume"),
                           dt.datetime.combine(day, dt.time(20, 0), NY)))
    return out


def main() -> int:
    db = Supabase.from_env()
    backfill = env_flag("INGEST_BACKFILL")
    key = require_env("TIINGO_API_KEY").removeprefix("Token ").strip()
    for sid, prov, ep, notes in [
        ("okx", "OKX (API pública)", "https://www.okx.com/api/v5", "Interés abierto, financiación y liquidaciones BTC."),
        ("deribit", "Deribit (API pública)", "https://www.deribit.com/api/v2", "Financiación e interés abierto BTC."),
        ("hyperliquid", "Hyperliquid (API pública)", "https://api.hyperliquid.xyz/info", "Interés abierto y financiación."),
        ("ssga", "State Street Global Advisors", "https://www.ssga.com", "Valor liquidativo y acciones de SPY y GLD."),
        ("ishares", "iShares (BlackRock)", "https://www.ishares.com", "Acciones en circulación de IBIT (web del fondo)."),
        (SOURCE, "Proceso de derivados y flujos de Market Radar", "ingest/derivatives_ingest.py", "Registro."),
    ]:
        db.ensure_source(sid, prov, ep, notes)
    db.ensure_series(catalog())
    mode = "backfill" if backfill else "incremental"
    log_id = db.start_log(SOURCE, mode)
    results, errors = {}, {}
    jobs = {
        "spy": lambda: ssga("SPY", backfill), "gld": lambda: ssga("GLD", backfill), "ibit": lambda: ibit(db),
        "volumen": lambda: volumes(backfill, key), "snapshots": snapshots,
        "okx_oi": okx_oi, "okx_funding": okx_funding, "deribit_funding": lambda: deribit_funding(backfill),
        "okx_liquidaciones": okx_liquidations,
    }
    for name, job in jobs.items():
        try:
            rows = job()
            db.upsert_observations(rows)
            results[name] = {"rows": len(rows), "missing": 0,
                             "first": min((r["as_of"] for r in rows), default=None),
                             "last": max((r["as_of"] for r in rows), default=None)}
            print(f"OK   {name:<18} {len(rows):>6} filas  {results[name]['first']} → {results[name]['last']}")
        except Exception as exc:
            errors[name] = str(exc)[:500]
            level = "warning" if name == "ibit" else "error"
            print(f"::{level}::derivados {name}: {str(exc)[:300]}")
    hard = {k: v for k, v in errors.items() if k != "ibit"}
    status = db.finish_log(log_id, mode, results, errors)
    print(f"derivados: {status}.")
    return 1 if hard else 0


if __name__ == "__main__":
    sys.exit(main())
