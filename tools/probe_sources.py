#!/usr/bin/env python3
"""Comprueba si una fuente candidata responde con datos (Etapa 0).

Solo lee: no escribe nada en la base de datos. Cada comprobación imprime una
anotación de GitHub (::notice:: o ::warning::) con el resultado, para poder
leerlo desde la API sin descargar los registros.
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta

import requests

UA = {"User-Agent": "Mozilla/5.0 (market-radar personal data probe)"}
START = (date.today() - timedelta(days=30)).isoformat()


def report(name: str, ok: bool, detail: str) -> None:
    level = "notice" if ok else "warning"
    print(f"::{level}::{'OK ' if ok else 'NO '} {name}: {detail}"[:900])


def tiingo_daily(ticker: str) -> None:
    key = os.environ.get("TIINGO_API_KEY", "").strip()
    try:
        r = requests.get(f"https://api.tiingo.com/tiingo/daily/{ticker}/prices",
                         headers={"Authorization": f"Token {key}"},
                         params={"startDate": START}, timeout=30)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text
        if r.ok and isinstance(data, list) and data:
            report(f"tiingo daily {ticker}", True, f"{len(data)} días, último {data[-1]['date'][:10]} cierre {data[-1].get('close')}")
        else:
            report(f"tiingo daily {ticker}", False, f"HTTP {r.status_code} {str(data)[:200]}")
    except Exception as exc:
        report(f"tiingo daily {ticker}", False, str(exc)[:200])


def tiingo_fx(pair: str) -> None:
    key = os.environ.get("TIINGO_API_KEY", "").strip()
    try:
        r = requests.get(f"https://api.tiingo.com/tiingo/fx/{pair}/prices",
                         headers={"Authorization": f"Token {key}"},
                         params={"startDate": START, "resampleFreq": "1day"}, timeout=30)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text
        if r.ok and isinstance(data, list) and data:
            first, last = data[0]["date"][:10], data[-1]
            report(f"tiingo fx {pair}", True, f"{len(data)} días desde {first}, último {last['date'][:10]} cierre {last.get('close')}")
        else:
            report(f"tiingo fx {pair}", False, f"HTTP {r.status_code} {str(data)[:200]}")
    except Exception as exc:
        report(f"tiingo fx {pair}", False, str(exc)[:200])


def yahoo(symbol: str) -> None:
    try:
        r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                         params={"range": "1mo", "interval": "1d"}, headers=UA, timeout=30)
        if not r.ok:
            report(f"yahoo {symbol}", False, f"HTTP {r.status_code}")
            return
        res = r.json()["chart"]["result"][0]
        closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
        report(f"yahoo {symbol}", bool(closes),
               f"{len(closes)} cierres, último {closes[-1] if closes else None}, "
               f"divisa {res['meta'].get('currency')}, bolsa {res['meta'].get('exchangeName')}")
    except Exception as exc:
        report(f"yahoo {symbol}", False, str(exc)[:200])


def euronext(isin_mic: str) -> None:
    url = f"https://live.euronext.com/en/ajax/AwlHistoricalPrice/getFullDownloadAjax/{isin_mic}"
    try:
        r = requests.post(url, headers=UA, timeout=30, data={
            "format": "csv", "decimal_separator": ".", "date_form": "Y-m-d",
            "startdate": START, "enddate": date.today().isoformat(),
        })
        lines = [l for l in r.text.splitlines() if l.strip()]
        report(f"euronext {isin_mic}", r.ok and len(lines) > 3,
               f"HTTP {r.status_code}, {len(lines)} líneas; {' | '.join(lines[:4])[:300]}")
    except Exception as exc:
        report(f"euronext {isin_mic}", False, str(exc)[:200])


def main() -> int:
    group = sys.argv[1] if len(sys.argv) > 1 else "tiingo"
    if group == "tiingo":
        for pair in ("xauusd", "xagusd"):
            tiingo_fx(pair)
        for t in ("300750", "300750.SZ", "HO", "HG"):
            tiingo_daily(t)
    elif group == "public":
        for s in ("^MOVE", "DX-Y.NYB", "HG=F", "HO.PA", "300750.SZ", "GC=F"):
            yahoo(s)
        euronext("FR0000121329-XPAR")
    return 0


if __name__ == "__main__":
    sys.exit(main())
