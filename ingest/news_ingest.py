#!/usr/bin/env python3
"""Noticias oficiales para Market Radar (tablas news y news_instruments).

Solo fuentes que publican RSS/Atom para uso automatizado:
- Reserva Federal: comunicados y discursos.
- BCE: comunicados, discursos y decisiones.
- Banco de Inglaterra: noticias.
- SEC EDGAR: hechos relevantes (8-K) de RTX y 6-K de YPF y Alibaba. La SEC exige un
  User-Agent con contacto: solo se consulta si existe la variable SEC_CONTACT.

Cada noticia guarda título, fuente, URL (clave única), published_at del propio feed, hora de
ingesta, categorías y una relevancia de 0 a 100 asignada con reglas fijas por palabras clave.
Las noticias no generan alertas: sirven para explicar las que generan los datos.

Variables de entorno: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY. Opcional: SEC_CONTACT.
"""

from __future__ import annotations

import datetime as dt
import email.utils
import html
import os
import re
import sys
import xml.etree.ElementTree as ET

import requests

from common import Supabase, now_utc

UA = {"User-Agent": "Mozilla/5.0 (market-radar personal news reader)"}
LOOKBACK_DAYS = 30

FEEDS = [
    # id, fuente, URL, instrumentos por defecto
    ("fed", "Reserva Federal", "https://www.federalreserve.gov/feeds/press_all.xml", ["us02y", "us10y", "dxy"]),
    ("fed", "Reserva Federal (discursos)", "https://www.federalreserve.gov/feeds/speeches.xml", ["us02y", "us10y"]),
    ("ecb", "BCE", "https://www.ecb.europa.eu/rss/press.html", ["eurusd"]),
    ("boe", "Banco de Inglaterra", "https://www.bankofengland.co.uk/rss/news", []),
]
SEC_COMPANIES = [
    # CIK, formulario, instrumento
    ("0000101829", "8-K", "rtx"),
    ("0000904851", "6-K", "ypf"),
    ("0001577552", "6-K", "alibaba"),
]

# (patrón, categoría, relevancia): gana la regla de mayor relevancia que encaje.
RULES = [
    (r"FOMC statement|Monetary policy decisions|Bank Rate|Monetary Policy Summary|interest rate decision",
     "politica_monetaria", 95),
    (r"Minutes of the Federal Open Market Committee|account of the monetary policy|Monetary Policy Report",
     "politica_monetaria", 75),
    (r"Summary of Economic Projections|projections", "politica_monetaria", 70),
    (r"Financial Stability|stress test|systemic", "estabilidad_financiera", 60),
    (r"\b8-K\b|\b6-K\b", "empresa", 55),
    (r"Powell|Jefferson|Lagarde|Bailey|Schnabel|Lane\b", "discurso", 50),
    (r"speech|remarks|testimony|interview|statement by", "discurso", 35),
    (r"discount rate", "politica_monetaria", 30),
    (r"enforcement|termination of|approval of application|bank merger", "supervision", 5),
]
KEYWORDS = {
    "gold": r"\bgold\b", "wti": r"\b(crude|oil)\b", "brent": r"\b(crude|oil)\b",
    "eurusd": r"\beuro\b|\bECB\b", "btc": r"\bbitcoin|crypto", "copper": r"\bcopper\b",
}


def classify(title: str, summary: str) -> tuple[list[str], int]:
    text = f"{title} {summary}"
    hits = [(score, cat) for pattern, cat, score in RULES if re.search(pattern, text, re.I)]
    if not hits:
        return ["otros"], 20
    score, top = max(hits)
    return list(dict.fromkeys([top] + [c for _, c in hits])), score


def clean(s: str | None, limit: int) -> str:
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    return re.sub(r"\s+", " ", s).strip()[:limit]


def parse_date(raw: str | None) -> dt.datetime | None:
    if not raw:
        return None
    raw = raw.strip()
    try:
        d = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        try:
            d = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def parse_feed(text: str) -> list[dict]:
    """RSS 2.0, RSS 1.0 (RDF) o Atom; se ignoran los espacios de nombres."""
    root = ET.fromstring(text.encode("utf-8") if isinstance(text, str) else text)
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    out = []
    for it in [*root.iter("item"), *root.iter("entry")]:
        link = it.findtext("link") or ""
        if not link.strip():
            l = it.find("link")
            link = (l.get("href") if l is not None else "") or ""
        out.append({
            "title": clean(it.findtext("title"), 300),
            "url": link.strip(),
            "published": parse_date(it.findtext("pubDate") or it.findtext("date") or it.findtext("updated")
                                    or it.findtext("published")),
            "summary": clean(it.findtext("description") or it.findtext("summary") or it.findtext("content"), 600),
        })
    return [o for o in out if o["title"] and o["url"]]


def fetch(url: str, headers: dict) -> list[dict]:
    r = requests.get(url, headers=headers, timeout=60)
    r.raise_for_status()
    return parse_feed(r.text)


def link_instruments(item: dict, defaults: list[str]) -> list[str]:
    text = f"{item['title']} {item['summary']}"
    found = set(defaults)
    for inst, pattern in KEYWORDS.items():
        if re.search(pattern, text, re.I):
            found.add(inst)
    return sorted(found)


def store(db: Supabase, source_name: str, items: list[dict], defaults: list[str], since: dt.datetime) -> int:
    rows, links = [], {}
    for it in items:
        pub = it["published"]
        if pub is None or pub < since:
            continue
        cats, relevance = classify(it["title"], it["summary"])
        rows.append({"title": it["title"], "source_name": source_name, "url": it["url"],
                     "published_at": pub.isoformat(), "language": "en", "summary": it["summary"] or None,
                     "categories": cats, "relevance": relevance, "confirmed": True})
        links[it["url"]] = link_instruments(it, it.get("instruments", defaults))
    if not rows:
        return 0
    # ingested_at conserva la primera ingesta: no se envía y la base pone su valor por defecto.
    saved = db.insert("news", rows, on_conflict="url", returning=True)
    pairs = [{"news_id": n["id"], "instrument_id": i} for n in saved for i in links.get(n["url"], [])]
    if pairs:
        db.insert("news_instruments", pairs, on_conflict="news_id,instrument_id")
    return len(rows)


def main() -> int:
    db = Supabase.from_env()
    db.ensure_source("fed", "Reserva Federal (web oficial)", "https://www.federalreserve.gov",
                     "Calendario del FOMC y comunicados de prensa (RSS).")
    db.ensure_source("boe", "Banco de Inglaterra", "https://www.bankofengland.co.uk/rss/news", "Noticias (RSS).")
    db.ensure_source("sec", "SEC EDGAR", "https://www.sec.gov/cgi-bin/browse-edgar",
                     "Hechos relevantes de empresas cotizadas en EE. UU. (Atom). Requiere User-Agent con contacto.")
    db.ensure_source("noticias", "Proceso de noticias de Market Radar", "ingest/news_ingest.py",
                     "Registro de ejecuciones de noticias.")
    since = now_utc() - dt.timedelta(days=LOOKBACK_DAYS)
    log_id = db.start_log("noticias", "incremental")
    results, errors = {}, {}
    for _sid, name, url, defaults in FEEDS:
        try:
            n = store(db, name, fetch(url, UA), defaults, since)
            results[name] = {"rows": n, "missing": 0, "first": None, "last": None}
            print(f"OK   {name:<30} {n:>4} noticias")
        except Exception as exc:
            errors[name] = str(exc)[:500]
            print(f"FAIL {name:<30} {exc}")
            print(f"::error::noticias {name}: {str(exc)[:300]}")
    contact = os.environ.get("SEC_CONTACT", "").strip()
    if contact:
        for cik, form, inst in SEC_COMPANIES:
            name = f"SEC {form} {inst}"
            url = (f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type={form}"
                   f"&dateb=&owner=include&count=20&output=atom")
            try:
                items = fetch(url, {"User-Agent": f"Market Radar personal {contact}"})
                for it in items:
                    it["title"] = f"{inst.upper()}: {it['title']}"
                n = store(db, "SEC EDGAR", items, [inst], since)
                results[name] = {"rows": n, "missing": 0, "first": None, "last": None}
                print(f"OK   {name:<30} {n:>4} noticias")
            except Exception as exc:
                errors[name] = str(exc)[:500]
                print(f"::error::noticias {name}: {str(exc)[:300]}")
    else:
        print("::notice::SEC omitida: define la variable SEC_CONTACT (correo de contacto) para leer EDGAR.")
    status = db.finish_log(log_id, "incremental", results, errors)
    print(f"noticias: {status}.")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
