#!/usr/bin/env python3
"""Calendario de eventos de Market Radar (tabla events_calendar).

Fuentes, todas oficiales o calculadas con reglas públicas:
- FOMC: calendario de reuniones de la web de la Reserva Federal (decisión a las 14:00 de Nueva York).
- Datos macro de EE. UU.: fechas de publicación programadas de FRED (IPC, empleo, PIB, PCE, PPI,
  ventas minoristas, JOLTS).
- BCE: reuniones de política monetaria del calendario del Consejo de Gobierno (decisión 14:15 Fráncfort).
- Festivos de cada bolsa (exchange_calendars): NYSE, Euronext París, Xetra, Shanghái/Shenzhen,
  Tel Aviv y Buenos Aires.
- Vencimientos de opciones (tercer viernes), rebalanceos trimestrales de S&P 500 y Nasdaq-100 y
  último día de negociación del WTI (regla de NYMEX).

Cada ejecución reemplaza los eventos FUTUROS de cada fuente que se ha descargado bien; los pasados
se conservan. Si una fuente falla, sus eventos futuros anteriores se mantienen.

Variables de entorno: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, FRED_API_KEY.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import re
import sys
from zoneinfo import ZoneInfo

import requests

from common import Supabase, now_utc, require_env

UA = {"User-Agent": "Mozilla/5.0 (market-radar personal calendar)"}
NY = ZoneInfo("America/New_York")
FRANKFURT = ZoneInfo("Europe/Berlin")
HORIZON_DAYS = 180

FRED_RELEASES = {
    # release_id: (kind, título, hora de Nueva York)
    10: ("cpi", "IPC de EE. UU.", dt.time(8, 30)),
    50: ("employment", "Informe de empleo de EE. UU. (nóminas y paro)", dt.time(8, 30)),
    192: ("employment", "JOLTS: ofertas de empleo de EE. UU.", dt.time(10, 0)),
    53: ("other", "PIB de EE. UU.", dt.time(8, 30)),
    54: ("other", "Ingresos, gasto y PCE de EE. UU.", dt.time(8, 30)),
    46: ("other", "Precios de producción (PPI) de EE. UU.", dt.time(8, 30)),
    9: ("other", "Ventas minoristas de EE. UU.", dt.time(8, 30)),
}

EXCHANGES = [
    # código exchange_calendars, país, instrumento asociado, nombre, zona horaria
    ("XNYS", "US", None, "NYSE y Nasdaq", "America/New_York"),
    ("XPAR", "FR", "thales", "Euronext París", "Europe/Paris"),
    ("XETR", "DE", "rheinmetall", "Xetra", "Europe/Berlin"),
    ("XSHG", "CN", "catl", "Bolsas de Shanghái y Shenzhen", "Asia/Shanghai"),
    ("XTAE", "IL", "tase", "Bolsa de Tel Aviv", "Asia/Jerusalem"),
    ("XBUE", "AR", "merval", "BYMA (Buenos Aires)", "America/Argentina/Buenos_Aires"),
]

MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july",
                                      "august", "september", "october", "november", "december"], 1)}


def event(kind: str, when: dt.datetime, source: str, title: str, *, instrument: str | None = None,
          country: str | None = None, suppress: bool = False) -> dict:
    return {"event_at": when.astimezone(dt.timezone.utc).isoformat(), "kind": kind, "instrument_id": instrument,
            "country_code": country, "source_id": source, "suppresses_signals": suppress, "notes": title}


# --- FOMC ----------------------------------------------------------------- #
def fomc(today: dt.date) -> list[dict]:
    r = requests.get("https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm", headers=UA, timeout=60)
    r.raise_for_status()
    out = []
    chunks = re.split(r"(\d{4}) FOMC Meetings", r.text)
    for year, body in zip(chunks[1::2], chunks[2::2]):
        pairs = re.findall(r'fomc-meeting__month[^>]*>\s*(?:<strong>)?([^<]+)<.*?'
                           r'fomc-meeting__date[^>]*>\s*([^<]+)<', body, re.S)
        for month_txt, days_txt in pairs:
            month_name = month_txt.strip().split("/")[-1].strip().lower()
            days = re.findall(r"\d{1,2}", days_txt)
            if month_name not in MONTHS or not days or "notation" in days_txt.lower():
                continue
            d = dt.date(int(year), MONTHS[month_name], int(days[-1]))
            sep = "*" in days_txt
            title = "FOMC: decisión de tipos de la Fed" + (" y proyecciones económicas" if sep else "")
            out.append((d, title))
    if not any(d.year == today.year for d, _ in out):
        raise ValueError(f"No se reconocen reuniones del FOMC de {today.year}: ¿cambió la web?")
    return [event("fomc", dt.datetime.combine(d, dt.time(14, 0), NY), "fed", t, country="US")
            for d, t in sorted(set(out)) if d >= today]


# --- FRED ----------------------------------------------------------------- #
def fred_releases(today: dt.date, key: str) -> list[dict]:
    r = requests.get("https://api.stlouisfed.org/fred/releases/dates", params={
        "api_key": key, "file_type": "json", "realtime_start": today.isoformat(),
        "realtime_end": (today + dt.timedelta(days=HORIZON_DAYS)).isoformat(),
        "include_release_dates_with_no_data": "true", "limit": 1000, "sort_order": "asc"}, timeout=60)
    r.raise_for_status()
    out = []
    for d in r.json().get("release_dates", []):
        spec = FRED_RELEASES.get(d.get("release_id"))
        if spec:
            kind, title, t = spec
            out.append(event(kind, dt.datetime.combine(dt.date.fromisoformat(d["date"]), t, NY), "fred",
                             title, country="US"))
    if not out:
        raise ValueError("FRED no devolvió fechas de publicación programadas")
    return out


# --- BCE ------------------------------------------------------------------ #
def ecb_meetings(today: dt.date) -> list[dict]:
    r = requests.get("https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html", headers=UA, timeout=60)
    r.raise_for_status()
    text = html.unescape(re.sub(r"<[^>]+>", " ", r.text))
    days = sorted({dt.datetime.strptime(d, "%d/%m/%Y").date()
                   for d, what in re.findall(r"(\d{2}/\d{2}/\d{4})\s+([^\n]{0,160})", text)
                   if re.search(r"(?<!non-)monetary policy meeting", what, re.I)})
    if not days:
        raise ValueError("No se reconocen reuniones de política monetaria del BCE: ¿cambió la web?")
    # Reuniones de dos días: la decisión se anuncia el segundo.
    decisions = [d for i, d in enumerate(days) if i + 1 == len(days) or (days[i + 1] - d).days > 1]
    return [event("other", dt.datetime.combine(d, dt.time(14, 15), FRANKFURT), "ecb",
                  "BCE: decisión de tipos", country="EA") for d in decisions if d >= today]


# --- bolsas: festivos, vencimientos, rebalanceos, WTI ----------------------- #
def market_events(today: dt.date) -> list[dict]:
    import exchange_calendars as xc
    import pandas as pd
    end = today + dt.timedelta(days=HORIZON_DAYS)
    out = []
    sessions_ny: set[dt.date] = set()
    for code, country, instrument, name, tz in EXCHANGES:
        cal = xc.get_calendar(code, start=(today - dt.timedelta(days=7)).isoformat())
        last = min(cal.last_session.date(), end)
        sessions = {s.date() for s in cal.sessions_in_range(today.isoformat(), last.isoformat())}
        if code == "XNYS":
            sessions_ny = sessions
        for d in pd.date_range(today, last, freq="D"):
            day = d.date()
            if day.weekday() < 5 and day not in sessions:
                out.append(event("holiday", dt.datetime.combine(day, dt.time(9, 0), ZoneInfo(tz)), "mercados",
                                 f"Festivo: {name} cerrado", instrument=instrument, country=country,
                                 suppress=True))
    # Tercer viernes: vencimiento mensual; en marzo, junio, septiembre y diciembre, además rebalanceo.
    month = dt.date(today.year, today.month, 1)
    while month <= end:
        first_friday = month + dt.timedelta(days=(4 - month.weekday()) % 7)
        day = first_friday + dt.timedelta(days=14)
        while sessions_ny and day not in sessions_ny and day > month:
            day -= dt.timedelta(days=1)          # festivo: se adelanta al día hábil anterior
        if day >= today:
            when = dt.datetime.combine(day, dt.time(16, 0), NY)
            if month.month in (3, 6, 9, 12):
                out.append(event("rebalance", when, "mercados",
                                 "Vencimiento trimestral de futuros y opciones y rebalanceo de S&P 500 y Nasdaq-100"
                                 + (" (reconstitución anual del Nasdaq-100)" if month.month == 12 else ""),
                                 instrument="spx", country="US", suppress=True))
            else:
                out.append(event("expiry", when, "mercados", "Vencimiento mensual de opciones (EE. UU.)",
                                 instrument="spx", country="US", suppress=True))
        month = (month.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    # WTI (NYMEX CL): la negociación termina 3 días hábiles antes del día 25 del mes anterior al de entrega
    # (4 días si el 25 no es hábil).
    if sessions_ny:
        month = dt.date(today.year, today.month, 1)
        while month <= end:
            d25 = month.replace(day=25)
            n = 3 if d25 in sessions_ny else 4
            biz = sorted(s for s in sessions_ny if s < d25)
            if len(biz) >= n:
                last_trade = biz[-n]
                if last_trade >= today:
                    delivery = (month.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
                    out.append(event("roll", dt.datetime.combine(last_trade, dt.time(14, 30), NY), "mercados",
                                     f"WTI: último día del contrato de {delivery.strftime('%m/%Y')}",
                                     instrument="wti", country="US", suppress=True))
            month = (month.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    return out


def main() -> int:
    db = Supabase.from_env()
    fred_key = require_env("FRED_API_KEY")
    db.ensure_source("fed", "Reserva Federal (web oficial)", "https://www.federalreserve.gov",
                     "Calendario del FMOC y comunicados de prensa (RSS).")
    db.ensure_source("mercados", "Calendarios de bolsa y reglas públicas", "exchange_calendars (PyPI)",
                     "Festivos por bolsa, vencimientos de opciones, rebalanceos y vencimientos del WTI calculados.")
    db.ensure_source("calendario", "Proceso de calendario de Market Radar", "ingest/calendar_ingest.py",
                     "Registro de ejecuciones del calendario.")
    today = now_utc().date()
    log_id = db.start_log("calendario", "incremental")
    results, errors = {}, {}
    jobs = {"fed": lambda: fomc(today), "fred": lambda: fred_releases(today, fred_key),
            "ecb": lambda: ecb_meetings(today), "mercados": lambda: market_events(today)}
    for source, job in jobs.items():
        try:
            rows = job()
            db.delete("events_calendar", {"source_id": f"eq.{source}", "event_at": f"gte.{now_utc().isoformat()}"})
            future = [r for r in rows if r["event_at"] >= now_utc().isoformat()]
            if future:
                db.insert("events_calendar", future)
            first = min((r["event_at"][:10] for r in future), default=None)
            results[source] = {"rows": len(future), "missing": 0, "first": first,
                               "last": max((r["event_at"][:10] for r in future), default=None)}
            print(f"OK   {source:<10} {len(future):>4} eventos")
        except Exception as exc:
            errors[source] = str(exc)[:500]
            print(f"FAIL {source:<10} {exc}")
            print(f"::error::calendario {source}: {str(exc)[:300]}")
    status = db.finish_log(log_id, "incremental", results, errors)
    upcoming = db.select("events_calendar", {"select": "event_at,kind,notes", "event_at": f"gte.{now_utc().isoformat()}",
                                             "order": "event_at", "limit": "8"})
    print("Próximos: " + json.dumps([f"{u['event_at'][:10]} {u['notes']}" for u in upcoming], ensure_ascii=False))
    print(f"calendario: {status}.")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
