#!/usr/bin/env python3
"""Informe diario de Market Radar: lo compone desde las vistas de Supabase,
lo guarda en `daily_reports` y lo envía por correo con Gmail (SMTP).

Todo el contenido es determinista (vistas y alertas ya calculadas), salvo la
sección «Análisis», que redacta un modelo de OpenAI solo con la evidencia del propio
informe (ai_analysis.py). Sin OPENAI_API_KEY esa sección se omite.

Se envía de lunes a viernes. El sábado no se envía salvo con --force.

Variables de entorno:
  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
  GMAIL_USER, GMAIL_APP_PASSWORD   (contraseña de aplicación de Google)
  REPORT_TO        destinatario (por defecto, GMAIL_USER)
  DASHBOARD_URL    opcional, enlace al dashboard
  REPORT_FORCE=true  enviar aunque sea fin de semana
  REPORT_DRY_RUN=true  componer y guardar, sin enviar
  OPENAI_API_KEY, ANALYSIS_MODEL, ANALYSIS_EFFORT  análisis con IA (opcional)
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import os
import re
import smtplib
import sys
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ingest"))
from common import Supabase, env_flag, now_utc, require_env  # noqa: E402

import ai_analysis  # noqa: E402
import exceptional  # noqa: E402
import market_context  # noqa: E402

PARIS = ZoneInfo("Europe/Paris")
STALE_DAYS = 4          # un dato diario con más días de antigüedad se marca como atrasado

CATEGORIES = [
    ("indices", "Índices"),
    ("acciones", "Acciones"),
    ("metales_materias_primas", "Materias primas"),
    ("cripto", "Cripto"),
    ("divisas", "Divisas"),
    ("tipos_indicadores", "Tipos e indicadores"),
]
RULE_LABELS = {
    "move_3sd": "Movimiento extremo", "high_52w": "Máximo 52 semanas", "low_52w": "Mínimo 52 semanas",
    "vol_regime": "Volatilidad", "rate_move": "Tipos", "net_liquidity_move": "Liquidez",
    "fedfunds_change": "Política monetaria", "macro_release": "Nuevo dato",
    "exc_A": "Posicionamiento extremo", "exc_B": "Posible liquidación", "exc_C": "Apalancamiento",
    "exc_D": "Flujos ETF", "exc_E": "Movimiento anómalo",
}
GREEN, RED, GREY, INK = "#1a7f37", "#cf222e", "#6e7781", "#1f2328"


# --------------------------------------------------------------------------- #
# Formato
# --------------------------------------------------------------------------- #
def num(x, dec=2) -> str:
    """Número con formato español: miles con punto, decimales con coma."""
    if x is None:
        return "—"
    s = f"{float(x):,.{dec}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def price(x) -> str:
    if x is None:
        return "—"
    v = abs(float(x))
    return num(x, 4 if v < 10 else 2 if v < 100000 else 0)


def chg(x, is_rate: bool) -> str:
    if x is None:
        return f'<span style="color:{GREY}">—</span>'
    x = float(x)
    color = GREEN if x > 0 else RED if x < 0 else GREY
    text = f"{'+' if x > 0 else ''}{num(x, 2)} {'pp' if is_rate else '%'}"
    return f'<span style="color:{color}">{text}</span>'


def esc(s) -> str:
    return html.escape(str(s or ""))


def section(title: str, body: str) -> str:
    return (f'<h2 style="font-size:16px;margin:28px 0 8px;color:{INK};'
            f'border-bottom:1px solid #d0d7de;padding-bottom:4px">{esc(title)}</h2>{body}')


def table(headers: list[str], rows: list[list[str]], align_right_from: int = 1) -> str:
    th = "".join(
        f'<th style="text-align:{"right" if i >= align_right_from else "left"};padding:4px 8px;'
        f'font-weight:600;color:{GREY};font-size:12px;border-bottom:1px solid #d0d7de">{h}</th>'
        for i, h in enumerate(headers))
    trs = []
    for r in rows:
        tds = "".join(
            f'<td style="text-align:{"right" if i >= align_right_from else "left"};padding:4px 8px;'
            f'font-size:13px;border-bottom:1px solid #f0f2f4;white-space:nowrap">{c}</td>'
            for i, c in enumerate(r))
        trs.append(f"<tr>{tds}</tr>")
    return (f'<table style="border-collapse:collapse;width:100%;font-family:Arial,Helvetica,sans-serif">'
            f"<thead><tr>{th}</tr></thead><tbody>{''.join(trs)}</tbody></table>")


# --------------------------------------------------------------------------- #
# Datos
# --------------------------------------------------------------------------- #
def load(db: Supabase, report_date: dt.date) -> dict:
    since = (report_date - dt.timedelta(days=4)).isoformat()
    return {
        "summary": db.select("v_market_summary", {"select": "*"}),
        "liquidity": db.select("v_net_liquidity", {"select": "*", "order": "as_of.desc", "limit": "2"}),
        "m2": db.select("v_m2", {"select": "*", "order": "as_of.desc", "limit": "1"}),
        "fedfunds": db.select("v_observations_latest", {
            "select": "as_of,value", "series_id": "eq.fred_FEDFUNDS", "quality_status": "eq.ok",
            "order": "as_of.desc", "limit": "2"}),
        "nfci": db.select("v_observations_latest", {
            "select": "as_of,value", "series_id": "eq.fred_NFCI", "order": "as_of.desc", "limit": "1"}),
        "correlations": db.select("v_correlations_latest", {"select": "*", "order": "pair_id"}),
        # FMI: solo los últimos dos años de oro (mensual) y toda la deuda (anual, con proyecciones).
        "imf": db.select("v_observations_latest", {
            "select": "series_id,as_of,value,is_forecast,published_at",
            "or": f"(and(series_id.like.wgc_gold_tonnes_*,as_of.gte.{report_date.year - 2}-01-01),"
                  "series_id.like.imf_debt_gdp_*)",
            "quality_status": "eq.ok", "order": "series_id,as_of", "limit": "2000"}),
        "alerts": db.select("alerts", {
            "select": "as_of,rule_id,title,kind", "as_of": f"gte.{since}", "rule_id": "not.like.exc_*",
            "order": "as_of.desc,rule_id"}),
        "events": db.select("events_calendar", {
            "select": "event_at,kind,notes,country_code", "event_at": f"gte.{report_date.isoformat()}",
            "order": "event_at", "limit": "200"}),
        "news": db.select("news", {
            "select": "title,source_name,url,published_at,relevance",
            "published_at": f"gte.{(report_date - dt.timedelta(days=3)).isoformat()}",
            "relevance": "gte.50", "order": "relevance.desc,published_at.desc", "limit": "8"}),
        "events_window": db.select("events_calendar", {
            "select": "event_at,kind,notes,country_code",
            "and": f"(event_at.gte.{(report_date - dt.timedelta(days=4)).isoformat()},"
                   f"event_at.lte.{(report_date + dt.timedelta(days=8)).isoformat()})",
            "order": "event_at", "limit": "100"}),
        "news_ai": db.select("news", {
            "select": "title,source_name,url,published_at,summary,relevance",
            "published_at": f"gte.{(report_date - dt.timedelta(days=3)).isoformat()}",
            "relevance": "gte.35", "order": "relevance.desc,published_at.desc", "limit": "20"}),
        "sync": db.select("provider_sync_logs", {
            "select": "source_id,status,started_at,error", "order": "started_at.desc", "limit": "30"}),
    }


# --------------------------------------------------------------------------- #
# Composición
# --------------------------------------------------------------------------- #
def compose(data: dict, report_date: dt.date, dashboard_url: str,
            analysis: dict | None = None, analysis_error: str | None = None) -> tuple[str, str, dict]:
    summary = data["summary"]
    stale = sorted(s["instrument"] for s in summary if (s.get("days_since_last") or 0) > STALE_DAYS)
    latest_by_source: dict[str, dict] = {}
    for l in data["sync"]:  # ordenados de más reciente a más antiguo
        latest_by_source.setdefault(l["source_id"], l)
    failed = sorted(s for s, l in latest_by_source.items() if l["status"] in ("failed", "partial"))

    # Resumen: los tres mayores movimientos del último día, medidos frente a su volatilidad
    def z(s):
        vol = s.get("vol_20") or 0
        daily = (float(vol) / (100 if not s["is_rate"] else 1)) / (252 ** 0.5) if vol else 0
        c = float(s.get("chg_1d") or 0) / (100 if not s["is_rate"] else 1)
        return abs(c / daily) if daily else 0
    movers = sorted((s for s in summary if s.get("chg_1d") is not None), key=z, reverse=True)[:3]
    recent_alerts = [a for a in data["alerts"]]

    lines = []
    if movers:
        parts = [f"{esc(m['instrument'])} {chg(m['chg_1d'], m['is_rate'])}" for m in movers]
        lines.append("Mayores movimientos de la última sesión, en relación con su volatilidad: " + ", ".join(parts) + ".")
    lines.append(f"{len(recent_alerts)} alertas en los últimos 4 días." if recent_alerts
                 else "Sin alertas en los últimos 4 días.")
    if stale:
        lines.append(f'<span style="color:{RED}">Datos atrasados: {esc(", ".join(stale))}.</span>')
    if failed:
        lines.append(f'<span style="color:{RED}">Fuentes con errores en la última descarga: {esc(", ".join(failed))}.</span>')
    body = [section("Resumen", "".join(f'<p style="margin:4px 0;font-size:14px">{l}</p>' for l in lines))]

    # Situaciones excepcionales (reglas fijas)
    exc_html = compose_exceptional(data.get("signals") or [])
    if exc_html:
        body.append(exc_html)

    # Análisis con IA: hechos, interpretación e hipótesis
    if analysis:
        body.append(section("Análisis", ai_analysis.render(analysis)))
    elif analysis_error:
        body.append(section("Análisis", f'<p style="font-size:13px;color:{GREY}">El análisis con IA no está '
                                        f"disponible hoy ({esc(analysis_error[:120])}).</p>"))

    # Alertas
    if recent_alerts:
        rows = [[esc(a["as_of"]), esc(RULE_LABELS.get(a["rule_id"], a["rule_id"])), esc(a["title"])]
                for a in recent_alerts]
        body.append(section("Alertas", table(["Fecha", "Tipo", "Detalle"], rows, align_right_from=99)))

    # Agenda de la semana y noticias oficiales relevantes
    agenda = compose_agenda(data.get("events") or [], report_date)
    if agenda:
        body.append(agenda)
    if data.get("news"):
        rows = [[esc(fmt_day(n["published_at"])), esc(n["source_name"]),
                 f'<a href="{esc(n["url"])}" style="color:{INK}">{esc(n["title"])}</a>']
                for n in data["news"]]
        body.append(section("Noticias oficiales relevantes", table(["Fecha", "Fuente", "Titular"], rows,
                                                                    align_right_from=99)))

    # Mercados por categoría
    for cat, label in CATEGORIES:
        items = sorted((s for s in summary if s["category"] == cat), key=lambda s: s["instrument"])
        if not items:
            continue
        rows = [[esc(s["instrument"]) + (' <span style="color:%s">(atrasado)</span>' % RED
                                         if (s.get("days_since_last") or 0) > STALE_DAYS else ""),
                 (num(s["last_value"], 2) + " %") if s["is_rate"] else price(s["last_value"]),
                 chg(s.get("chg_1d"), s["is_rate"]), chg(s.get("chg_1w"), s["is_rate"]),
                 chg(s.get("chg_1m"), s["is_rate"]), chg(s.get("chg_ytd"), s["is_rate"]),
                 num(s.get("vol_20"), 1) + (" pp" if s["is_rate"] else " %"),
                 esc(s["last_date"][5:])]
                for s in items]
        body.append(section(label, table(
            ["Instrumento", "Último", "1 día", "1 semana", "1 mes", "Año", "Vol. 20d", "Fecha"], rows)))

    # Liquidez y política monetaria
    liq_rows = []
    if data["liquidity"]:
        l = data["liquidity"][0]
        liq_rows.append(["Liquidez neta de la Fed (mm USD)", num(l["net_liquidity_b"], 0),
                         chg_abs(l.get("chg_4w_b")), chg_abs(l.get("chg_13w_b")), esc(l["as_of"])])
        liq_rows.append(["  Balance de la Fed", num(l["fed_balance_b"], 0), "", "", ""])
        liq_rows.append(["  Cuenta del Tesoro (TGA)", num(l["tga_b"], 0), "", "", ""])
        liq_rows.append(["  Repos inversos", num(l["rrp_b"], 0), "", "", ""])
    if data["m2"]:
        m = data["m2"][0]
        liq_rows.append(["M2 (mm USD)", num(m["m2_b"], 0), f"{num(m['chg_mom_pct'])} % mes",
                         f"{num(m['chg_yoy_pct'])} % año", esc(m["as_of"][:7])])
    if data["fedfunds"]:
        f = data["fedfunds"]
        delta = (float(f[0]["value"]) - float(f[1]["value"])) if len(f) > 1 else None
        liq_rows.append(["Fondos federales (%)", num(f[0]["value"]), chg(delta, True) if delta is not None else "—",
                         "", esc(f[0]["as_of"][:7])])
    if data["nfci"]:
        n = data["nfci"][0]
        liq_rows.append(["Condiciones financieras (NFCI)", num(n["value"]), "", "", esc(n["as_of"])])
    if liq_rows:
        body.append(section("Liquidez y política monetaria (EE. UU.)", table(
            ["Indicador", "Nivel", "Cambio", "Cambio largo", "Dato de"], liq_rows)))

    # FMI: solo cuando hay datos publicados en los últimos días (el oro es mensual; la deuda, semestral).
    imf_section = compose_imf(data.get("imf") or [], report_date)
    if imf_section:
        body.append(imf_section)

    # Correlaciones: se destacan las que están en el extremo de su rango anual
    corr_rows = []
    for c in data["correlations"]:
        cur, lo, hi = (float(c[k]) if c.get(k) is not None else None for k in ("corr_60", "min_1y", "max_1y"))
        flag = ""
        if cur is not None and lo is not None and hi is not None and hi > lo:
            pos = (cur - lo) / (hi - lo)
            flag = "mínimo del año" if pos <= 0.05 else "máximo del año" if pos >= 0.95 else ""
        corr_rows.append([esc(c["label"]), num(cur), num(c.get("corr_3m_ago")),
                          f"{num(lo)} a {num(hi)}", f'<span style="color:{RED}">{flag}</span>' if flag else ""])
    if corr_rows:
        body.append(section("Correlaciones (60 sesiones)", table(
            ["Par", "Actual", "Hace 3 meses", "Rango 1 año", ""], corr_rows)))

    footer = (f'<p style="font-size:12px;color:{GREY};margin-top:28px">Datos al cierre más reciente de cada '
              f"fuente. Variaciones en %, salvo tipos (pp, puntos porcentuales). Volatilidad anualizada. "
              f"Cobre, MOVE, DXY, divisas, Rheinmetall, MERVAL y TA-35 proceden de Yahoo, fuente no oficial."
              + (f' <a href="{esc(dashboard_url)}">Abrir el dashboard</a>.' if dashboard_url else "") + "</p>")
    title = f"Market Radar · {report_date.strftime('%d/%m/%Y')}"
    html_doc = (f'<!doctype html><html><body style="margin:0;padding:16px;background:#ffffff">'
                f'<div style="max-width:760px;margin:0 auto;font-family:Arial,Helvetica,sans-serif;color:{INK}">'
                f'<h1 style="font-size:20px;margin:0 0 4px">{esc(title)}</h1>'
                f'<p style="margin:0;color:{GREY};font-size:13px">Informe diario · uso personal</p>'
                + "".join(body) + footer + "</div></body></html>")
    content = {"movers": [m["instrument"] for m in movers], "alerts": len(recent_alerts),
               "stale": stale, "failed_sources": failed}
    if analysis:
        content["ai"] = {k: analysis[k] for k in ("model", "result", "dropped", "usage", "web_search")}
    return title, html_doc, content


EXC_TYPES = {"A": "Posicionamiento extremo", "B": "Posible liquidación", "C": "Exceso de apalancamiento",
             "D": "Flujos de capital", "E": "Movimiento anómalo"}


def compose_exceptional(signals: list[dict]) -> str:
    if not signals:
        return section("Situaciones excepcionales", f'<p style="font-size:13px;color:{GREY}">Ninguna hoy según las '
                                                     "reglas exceptional-v1.0.</p>")
    rows = []
    for s in signals[:15]:
        color = RED if s["severidad"] == "alta" else "#9a6700"
        rows.append([f'<span style="color:{color};font-weight:600">{esc(EXC_TYPES.get(s["tipo"], s["tipo"]))}</span>'
                     f'<br><span style="font-size:11px;color:{GREY}">{esc(s["naturaleza"])}</span>',
                     f'<b>{esc(s["titulo"])}</b><br><span style="font-size:12px">{esc(s["detalle"])}</span>'
                     f'<br><span style="font-size:11px;color:{GREY}">Fuente: {esc(", ".join(s["fuentes"]))} · '
                     f'dato del {esc(s["fecha_dato"][:10])}</span>'])
    note = (f'<p style="font-size:11px;color:{GREY};margin:6px 0 0">Reglas fijas (exceptional-v1.0). «Compatible con» '
            "indica un patrón coherente con esa situación, no su confirmación. El COT refleja posiciones del martes, "
            "publicadas el viernes.</p>")
    return section("Situaciones excepcionales", table(["Tipo", "Señal"], rows, align_right_from=99) + note)


AGENDA_DAYS = 7
AGENDA_KINDS = {"fomc": "Fed", "cpi": "Inflación", "employment": "Empleo", "other": "Macro",
                "holiday": "Festivo", "expiry": "Vencimiento", "rebalance": "Rebalanceo", "roll": "WTI"}
WEEKDAYS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]


def fmt_day(iso: str, with_time: bool = False) -> str:
    d = dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(PARIS)
    out = f"{WEEKDAYS[d.weekday()]} {d.day:02d}/{d.month:02d}"
    return out + (f" {d.hour:02d}:{d.minute:02d}" if with_time else "")


def compose_agenda(events: list[dict], report_date: dt.date) -> str:
    end = (report_date + dt.timedelta(days=AGENDA_DAYS)).isoformat()
    rows = [[esc(fmt_day(e["event_at"], e["kind"] != "holiday")), esc(AGENDA_KINDS.get(e["kind"], e["kind"])),
             esc(e.get("notes") or "")]
            for e in events if e["event_at"][:10] < end]
    if not rows:
        return ""
    return section(f"Agenda de los próximos {AGENDA_DAYS} días (hora de París)",
                   table(["Cuándo", "Tipo", "Evento"], rows, align_right_from=99))


COUNTRY = {"USA": "Estados Unidos", "DEU": "Alemania", "ITA": "Italia", "FRA": "Francia", "CHN": "China",
           "RUS": "Rusia", "IND": "India", "CHE": "Suiza", "JPN": "Japón", "GBR": "Reino Unido",
           "ARG": "Argentina"}
IMF_NEW_DAYS = 3


def compose_imf(rows: list[dict], report_date: dt.date) -> str:
    cutoff = (report_date - dt.timedelta(days=IMF_NEW_DAYS)).isoformat()
    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault(r["series_id"], []).append(r)
    gold_rows, debt_rows = [], []
    for sid, ps in sorted(by.items()):
        if not any((p.get("published_at") or "") >= cutoff for p in ps):
            continue
        code = sid[-3:]
        name = esc(COUNTRY.get(code, code))
        if sid.startswith("wgc_gold_tonnes_"):
            last = ps[-1]
            prev = ps[-2] if len(ps) > 1 else None
            delta = float(last["value"]) - float(prev["value"]) if prev else None
            gold_rows.append([name, num(last["value"], 0), chg_abs(delta), esc(last["as_of"][:7])])
        else:
            y = {int(p["as_of"][:4]): p for p in ps}
            cur, nxt = y.get(report_date.year), y.get(report_date.year + 1)
            debt_rows.append([name, num(cur["value"], 0) if cur else "—", num(nxt["value"], 0) if nxt else "—",
                              num(y[2020]["value"], 0) if 2020 in y else "—"])
    parts = []
    if gold_rows:
        parts.append(table(["País", "Toneladas", "Cambio mensual", "Dato de"], gold_rows))
    if debt_rows:
        parts.append(f'<p style="margin:12px 0 4px;font-size:13px;color:{GREY}">Deuda pública bruta, % del PIB '
                     f"(previsiones del FMI)</p>"
                     + table(["País", str(report_date.year), str(report_date.year + 1), "2020"], debt_rows))
    return section("Datos nuevos del FMI: reservas de oro y deuda", "".join(parts)) if parts else ""


def chg_abs(x) -> str:
    if x is None:
        return "—"
    x = float(x)
    color = GREEN if x > 0 else RED if x < 0 else GREY
    return f'<span style="color:{color}">{"+" if x > 0 else ""}{num(x, 0)}</span>'


# --------------------------------------------------------------------------- #
# Envío
# --------------------------------------------------------------------------- #
def send(subject: str, html_doc: str) -> None:
    user = require_env("GMAIL_USER")
    password = require_env("GMAIL_APP_PASSWORD").replace(" ", "")
    to = os.environ.get("REPORT_TO", "").strip() or user
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, f"Market Radar <{user}>", to
    msg.set_content("Este informe necesita un cliente de correo con HTML.")
    msg.add_alternative(html_doc, subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as smtp:
        smtp.login(user, password)
        smtp.send_message(msg)


def main() -> int:
    p = argparse.ArgumentParser(description="Informe diario de Market Radar")
    p.add_argument("--force", action="store_true", default=env_flag("REPORT_FORCE"))
    p.add_argument("--dry-run", action="store_true", default=env_flag("REPORT_DRY_RUN"))
    args = p.parse_args()

    report_date = dt.datetime.now(PARIS).date()
    if report_date.weekday() >= 5 and not args.force:
        print("Fin de semana: no se envía informe.")
        return 0

    db = Supabase.from_env()
    data = load(db, report_date)
    data["closes"] = market_context.load_closes(db)
    data["macro"] = market_context.load_macro(db)
    try:
        rates = {s["instrument_id"] for s in data["summary"] if s["is_rate"]}
        data["signals"], data["cot_overview"] = exceptional.detect(db, data["closes"], rates)
        try:
            saved = exceptional.store(db, data["signals"], {s["instrument_id"]: s["series_id"] for s in data["summary"]})
        except Exception as exc:
            saved = 0
            print(f"::warning::No se pudieron guardar las señales en alertas: {str(exc)[:300]}")
        print(f"::notice::Situaciones excepcionales: {len(data['signals'])} señales "
              f"({saved} nuevas en alertas): " + " | ".join(x["titulo"] for x in data["signals"])[:3000])
    except Exception as exc:
        data["signals"], data["cot_overview"] = [], []
        print(f"::warning::Detector de situaciones excepcionales: {str(exc)[:300]}")
    analysis, analysis_error = None, None
    try:
        analysis = ai_analysis.run(data, report_date)
        if analysis:
            u = analysis["usage"]
            print(f"::notice::Análisis IA: {len(analysis['result']['hechos'])} hechos, "
                  f"{len(analysis['result']['interpretaciones'])} interpretaciones, "
                  f"{len(analysis['result']['hipotesis'])} hipótesis; descartados {len(analysis['dropped'])}; "
                  f"tokens {u.get('input_tokens')}/{u.get('output_tokens')}")
        else:
            print("Análisis IA desactivado: falta OPENAI_API_KEY.")
    except Exception as exc:          # el informe sale igual, sin análisis
        analysis_error = str(exc)
        print(f"::warning::Análisis IA no disponible: {analysis_error[:300]}")
    title, html_doc, content = compose(data, report_date, os.environ.get("DASHBOARD_URL", "").strip(),
                                       analysis, analysis_error)
    row = {"report_date": report_date.isoformat(), "generated_at": now_utc().isoformat(),
           "experimental": True, "content": content, "html": html_doc,
           "stale_sources": content["stale"], "status": "draft"}
    sections = re.findall(r"<h2[^>]*>([^<]+)</h2>", html_doc)
    print(f"::notice::Secciones del informe: {' · '.join(sections)}")
    already_sent = db.select("daily_reports", {"select": "status", "report_date": f"eq.{report_date.isoformat()}",
                                               "status": "eq.sent"})
    if args.dry_run and analysis:
        # Prueba: el análisis se muestra en las anotaciones del job para revisarlo sin abrir el correo.
        res = analysis["result"]
        for title_, items in (("HECHOS", [f"{h['texto']} [{', '.join(h['fuentes'])}]" for h in res["hechos"]]),
                              ("INTERPRETACIÓN", [f"{i['texto']} (conf. {i['confianza']}) [{', '.join(i['se_apoya_en'])}]"
                                                 for i in res["interpretaciones"]]),
                              ("HIPÓTESIS", [f"Si {ai_analysis._no_si(h['si'])} → {h['entonces']} ({h['horizonte']}; invalida: "
                                             f"{h['invalidacion']}) [{', '.join(h['se_apoya_en'])}]" for h in res["hipotesis"]]),
                              ("LIMITACIONES / DESCARTADO", res.get("limitaciones", []) + analysis["dropped"])):
            print(f"::notice::{title_}: " + " || ".join(items)[:3500])
        print("::notice::SITUACIONES: " + " || ".join(f"{x['senal']}: {x['lectura']}"
                                                   for x in res.get("situaciones", []))[:3900])
        print("::notice::PANORAMA: " + " || ".join(f"{p['instrument_id']}: {p['lectura']}"
                                                for p in res.get("panorama", []))[:3900])
    if args.dry_run and already_sent:
        # Una prueba no sobrescribe el informe que ya se envió hoy.
        print("Prueba: el informe de hoy ya se envió; no se guarda el borrador.")
        return 0
    db.insert("daily_reports", row, on_conflict="report_date")

    if args.dry_run:
        out = Path(os.environ.get("REPORT_OUT", "report.html"))
        out.write_text(html_doc, encoding="utf-8")
        print(f"Borrador guardado en daily_reports y en {out}.")
        return 0
    try:
        send(title, html_doc)
    except Exception as exc:
        db.update("daily_reports", {"report_date": f"eq.{report_date.isoformat()}"}, {"status": "failed"})
        print(f"::error::No se pudo enviar el informe: {str(exc)[:300]}")
        return 1
    db.update("daily_reports", {"report_date": f"eq.{report_date.isoformat()}"},
              {"status": "sent", "sent_at": now_utc().isoformat()})
    print(f"Informe enviado: {title}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
