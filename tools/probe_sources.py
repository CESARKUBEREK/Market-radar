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


def tiingo_fx_history(pair: str) -> None:
    """Profundidad del historial diario de un par en Tiingo FX."""
    key = os.environ.get("TIINGO_API_KEY", "").strip()
    r = requests.get(f"https://api.tiingo.com/tiingo/fx/{pair}/prices",
                     headers={"Authorization": f"Token {key}"},
                     params={"startDate": "2020-01-01", "endDate": "2020-03-31", "resampleFreq": "1day"}, timeout=60)
    data = r.json() if r.ok else r.text
    ok = r.ok and isinstance(data, list) and bool(data)
    report(f"tiingo fx historial {pair} 2020T1", ok,
           f"{len(data)} días, primero {data[0]['date'][:10]} cierre {data[0].get('close')}" if ok else f"HTTP {r.status_code} {str(data)[:200]}")


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
    params = {"format": "csv", "decimal_separator": ".", "date_form": "Y-m-d", "op": "",
              "adjusted": "", "base100": "", "startdate": START, "enddate": date.today().isoformat()}
    for how in ("get", "post"):
        try:
            if how == "get":
                r = requests.get(url, params=params, headers=UA, timeout=30)
            else:
                r = requests.post(url, params={"format": "csv"}, data=params, headers=UA, timeout=30)
            lines = [l for l in r.text.splitlines() if l.strip()]
            report(f"euronext {how} {isin_mic}", r.ok and len(lines) > 3,
                   f"HTTP {r.status_code}, {len(lines)} líneas; {' | '.join(lines[:5])[:300]}")
        except Exception as exc:
            report(f"euronext {how} {isin_mic}", False, str(exc)[:200])


def imf() -> None:
    """Reservas de oro (IRFCL, onzas troy) y deuda/PIB (WEO)."""
    import csv
    import io
    import re
    gold = ("https://api.imf.org/external/sdmx/2.1/data/IRFCL/"
            "USA+DEU+ITA+FRA+CHN+RUS+IND+CHE+JPN.IRFCLDT1_IRFCL56V_FTO..M")
    try:
        r = requests.get(gold, params={"startPeriod": "2020-01"},
                         headers={**UA, "Accept": "application/vnd.sdmx.data+csv;version=1.0.0"}, timeout=90)
        rows = list(csv.DictReader(io.StringIO(r.text)))
        keys = ["COUNTRY", "INDICATOR", "SECTOR", "FREQUENCY", "TIME_PERIOD", "OBS_VALUE", "SCALE",
                "UNIT", "PUBLICATION_DATE", "UPDATE_DATE", "STATUS"]
        report("imf irfcl fila", bool(rows), str({k: rows[0].get(k) for k in keys})[:800] if rows else r.text[:300])
        by: dict = {}
        for x in rows:
            by.setdefault((x.get("COUNTRY"), x.get("SECTOR")), []).append(x)
        for (c, sec), xs in sorted(by.items(), key=lambda kv: str(kv[0])):
            xs.sort(key=lambda x: x["TIME_PERIOD"])
            report(f"imf irfcl {c} {sec}", True,
                   f"{len(xs)} obs, {xs[0]['TIME_PERIOD']}={xs[0]['OBS_VALUE']} … "
                   f"{xs[-1]['TIME_PERIOD']}={xs[-1]['OBS_VALUE']} scale={xs[-1].get('SCALE')}")
    except Exception as exc:
        report("imf irfcl", False, str(exc)[:200])

    debt = [
        ("datamapper", "https://www.imf.org/external/datamapper/api/v1/GGXWDG_NGDP/USA/JPN",
         {"Accept": "application/json"}),
        ("sdmx3 IMF.RES/WEO", "https://api.imf.org/external/sdmx/3.0/data/dataflow/IMF.RES/WEO/~/USA+JPN.GGXWDG_NGDP.A",
         {"Accept": "text/csv"}),
        ("sdmx21 WEO", "https://api.imf.org/external/sdmx/2.1/data/WEO/USA+JPN.GGXWDG_NGDP.A",
         {"Accept": "application/vnd.sdmx.data+csv;version=1.0.0"}),
    ]
    for name, url, h in debt:
        try:
            r = requests.get(url, headers={**UA, **h}, timeout=60)
            lines = r.text.splitlines()
            report(f"imf deuda {name}", r.ok and len(lines) > 1,
                   f"HTTP {r.status_code} {r.headers.get('content-type', '')[:40]}, {len(lines)} líneas; "
                   + " | ".join(ln[:250] for ln in lines[:2]) + " || " + (lines[-1][:250] if lines else ""))
        except Exception as exc:
            report(f"imf deuda {name}", False, str(exc)[:200])
    try:
        r = requests.get("https://api.imf.org/external/sdmx/2.1/dataflow/all/all/latest",
                         headers={**UA, "Accept": "application/xml"}, timeout=60)
        tags = re.findall(r"<(?:str:|structure:)?Dataflow\b[^>]*>", r.text)
        ids = []
        for t in tags:
            i = re.search(r'\bid="([^"]+)"', t)
            a = re.search(r'agencyID="([^"]+)"', t)
            if i:
                ids.append(f"{a.group(1) if a else '?'}/{i.group(1)}")
        hits = [x for x in ids if any(k in x.upper() for k in ("WEO", "GDD", "DEBT", "GFS", "FM", "HPDD"))]
        report("imf dataflows", bool(ids), f"HTTP {r.status_code}, {len(ids)} flujos; relevantes: {hits[:40]}")
    except Exception as exc:
        report("imf dataflows", False, str(exc)[:200])


def main() -> int:
    group = sys.argv[1] if len(sys.argv) > 1 else "tiingo"
    if group == "tiingo":
        for pair in ("xauusd", "xagusd"):
            tiingo_fx(pair)
        tiingo_fx_history("xauusd")
    elif group == "imf":
        imf()
    elif group == "public":
        euronext("FR0000121329-XPAR")
    return 0


if __name__ == "__main__":
    sys.exit(main())
