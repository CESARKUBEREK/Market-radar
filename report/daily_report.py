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
import layout  # noqa: E402
import hypotheses  # noqa: E402
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
    return (f'<h2 style="font-size:16px;margin:22px 0 8px;color:{INK};font-weight:700">{esc(title)}</h2>{body}')


def table(headers: list[str], rows: list[list[str]], align_right_from: int = 1) -> str:
    return layout.table(headers, rows, align_right_from=align_right_from, size=12)


def _old_table(headers: list[str], rows: list[list[str]], align_right_from: int = 1) -> str:
    th = "".join(
        f'<th style="text-align:{"right" if i >= align_right_from else "left"};padding:4px 8px;'
        f'font-weight:600;color:{GREY};font-size:12px;border-bottom:1px solid #d0d7de">{h}</th>'
        for i, h in enumerate(headers))
    trs = []
    for r in rows:
        tds = "".join(
            f'<td style="text-align:{"right" if i >= align_right_from else "left"};padding:4px 8px;'
            f'font-size:13px;border-bottom:1px solid #f0f2f4;vertical-align:top;'
            f'{"white-space:nowrap" if len(re.sub("<[^>]+>", "", str(c))) < 40 else "line-height:1.4"}">{c}</td>'
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
        "series_sources": {r["id"]: r["source_id"] for r in db.select("series", {"select": "id,source_id"})},
        "sync": db.select("provider_sync_logs", {
            "select": "source_id,status,started_at,error", "order": "started_at.desc", "limit": "30"}),
    }


# --------------------------------------------------------------------------- #
# Composición
# --------------------------------------------------------------------------- #
def compose(data: dict, report_date: dt.date, dashboard_url: str,
            analysis: dict | None = None, analysis_error: str | None = None) -> tuple[str, str, dict]:
    """Informe en tres niveles: «En 30 segundos», lectura del día y anexo de detalle."""
    L = layout
    summary = data["summary"]
    by_id = {s["instrument_id"]: s for s in summary}
    stale = sorted(s["instrument"] for s in summary if (s.get("days_since_last") or 0) > STALE_DAYS)
    latest_by_source: dict[str, dict] = {}
    for lg in data["sync"]:  # ordenados de más reciente a más antiguo
        latest_by_source.setdefault(lg["source_id"], lg)
    failed = sorted(s for s, lg in latest_by_source.items() if lg["status"] in ("failed", "partial"))
    res = (analysis or {}).get("result") or {}
    src = (analysis or {}).get("sources") or {}

    def z(s):
        vol = s.get("vol_20") or 0
        daily = (float(vol) / (100 if not s["is_rate"] else 1)) / (252 ** 0.5) if vol else 0
        c = float(s.get("chg_1d") or 0) / (100 if not s["is_rate"] else 1)
        return abs(c / daily) if daily else 0
    movers = sorted((s for s in summary if s.get("chg_1d") is not None), key=z, reverse=True)[:3]
    recent_alerts = list(data["alerts"])

    def refs(r):
        return ai_analysis._refs_html(r, src) if src else ""

    # Cabecera
    dates = sorted((s["last_date"] for s in summary if s["instrument_id"] != "btc"), reverse=True)
    ref_date = max(set(dates), key=dates.count) if dates else None
    status = L.para(f"Datos al cierre del {fmt_day(ref_date + 'T12:00:00+00:00')}" if ref_date else "",
                    size=13, color=L.GREY, margin="0")
    warn = []
    if stale:
        warn.append(f"datos atrasados: {esc(', '.join(stale))}")
    if failed:
        warn.append(f"fuentes con errores: {esc(', '.join(failed))}")
    if warn:
        status += L.para("⚠ " + "; ".join(warn) + ".", size=13, color=L.RED, margin="6px 0 0")

    body = []

    # 1. En 30 segundos
    inner = ""
    if res.get("titular"):
        inner += f'<p style="margin:0 0 10px;font-size:18px;font-weight:700;line-height:1.4">{esc(res["titular"])}</p>'
    if res.get("puntos"):
        inner += L.bullets([esc(p["texto"]) for p in res["puntos"]])
    if not inner and movers:
        inner = L.para("Mayores movimientos de la sesión, en relación con su volatilidad: "
                       + ", ".join(f"{esc(m['instrument'])} {chg(m['chg_1d'], m['is_rate'])}" for m in movers) + ".")
    if analysis_error and not analysis:
        inner += L.para(f"El análisis con IA no está disponible hoy ({esc(analysis_error[:100])}).", 13, L.GREY)
    key = []
    for iid in ("spx", "ndx", "us10y", "dxy", "gold", "brent", "btc", "eurusd", "move"):
        s = by_id.get(iid)
        if not s:
            continue
        val = (num(s["last_value"], 2) + " %") if s["is_rate"] else price(s["last_value"])
        delta = L.signed(float(s["chg_1d"]) * 100, 0, " pb") if s["is_rate"] and s.get("chg_1d") is not None \
            else L.signed(s.get("chg_1d"), 2, " %")
        key.append((s["instrument"], val, delta + f' <span style="color:{L.GREY};font-size:11px">'
                    f'{s["last_date"][8:10]}/{s["last_date"][5:7]}</span>'))
    body.append(L.block("En 30 segundos", L.BLUE, (L.card(inner) if inner else "") + L.chips(key[:6])))

    # 2. Próximos días: a vigilar + agenda
    upcoming = []
    for v in res.get("a_vigilar", []):
        upcoming.append(f"<b>{esc(v['texto'])}</b>")
    end = (report_date + dt.timedelta(days=AGENDA_DAYS)).isoformat()
    for e in (data.get("events") or []):
        if e["event_at"][:10] < end and e["kind"] != "holiday":
            upcoming.append(f'{esc(fmt_day(e["event_at"], True))} · {esc(e.get("notes") or "")}')
    holidays = [e for e in (data.get("events") or []) if e["kind"] == "holiday" and e["event_at"][:10] < end]
    if holidays:
        upcoming.append(L.small("Festivos: " + "; ".join(f'{fmt_day(e["event_at"])} {e.get("notes", "").replace("Festivo: ", "")}'
                                                            for e in holidays)))
    if upcoming:
        body.append(L.block("Próximos días", L.AMBER, L.bullets(upcoming[:10], 14), note="hora de París"))

    # 3. Lectura del día (IA)
    if res:
        inner = ""
        if res.get("hechos"):
            inner += L.sub("Qué ha pasado") + L.bullets([esc(h["texto"]) + refs(h["fuentes"]) for h in res["hechos"]])
        conf = {"alta": L.GREEN, "media": L.AMBER, "baja": L.GREY}
        if res.get("interpretaciones"):
            inner += L.sub("Por qué podría importar", L.BLUE) + L.bullets([
                esc(i["texto"]) + f' <span style="font-size:11px;font-weight:700;color:{conf.get(i["confianza"], L.GREY)}">'
                f'confianza {esc(i["confianza"])}</span>' + refs(i["se_apoya_en"]) for i in res["interpretaciones"]])
        if res.get("hipotesis"):
            inner += L.sub("Hipótesis", L.PURPLE) + L.bullets([
                f'<b>Si</b> {esc(ai_analysis._no_si(h["si"]))} <b>→</b> {esc(h["entonces"])}<br>'
                + L.small(f'{esc(h["horizonte"])} · se invalida si {esc(ai_analysis._no_si(h["invalidacion"]))}')
                for h in res["hipotesis"]])
        body.append(L.block("Lectura del día", L.INK, inner, note=f"IA ({esc(analysis['model'])}), verificada con los datos"))

    # 4. Señales: situaciones excepcionales + hacia dónde va el dinero + alertas
    sig_items = []
    readings = ai_analysis.signal_readings(analysis)
    for i, sg in enumerate((data.get("signals") or [])[:8]):
        color = L.RED if sg["severidad"] == "alta" else L.AMBER
        sig_items.append(f'<span style="color:{color}">●</span> <b>{esc(sg["titulo"])}</b><br>'
                         f'{esc(readings.get(i) or sg["detalle"])}<br>'
                         + L.small(f'{esc(EXC_TYPES.get(sg["tipo"], sg["tipo"]))} · {esc(sg["naturaleza"])} · '
                                   f'{esc(", ".join(sg["fuentes"]))}, dato del {sg["fecha_dato"][8:10]}/{sg["fecha_dato"][5:7]}'))
    to, out = flow_directions(data.get("cot_overview") or [], data.get("positioning_extras"))
    inner = ""
    if sig_items:
        inner += L.sub("Situaciones excepcionales") + L.bullets(sig_items, 14) + L.small(
            "Reglas fijas descriptivas, sin validación estadística; no es el radar de la Vía B.")
    if to or out:
        cot_date = max((o["fecha_posiciones"] for o in data.get("cot_overview") or []), default="")
        inner += L.sub("Hacia dónde va el dinero", L.GREEN) + L.bullets([esc(t) for t in to] or
                                                                         [L.small("Sin movimientos claros.")], 14)
        inner += L.sub("De dónde sale", L.RED) + L.bullets([esc(t) for t in out] or [L.small("Sin movimientos claros.")], 14)
        inner += L.small(f"COT del {cot_date[8:10]}/{cot_date[5:7]}, flujos de ETF y reservas del FMI. "
                         "Detalle por grupos en el anexo.")
    if recent_alerts:
        inner += L.sub("Alertas de los últimos 4 días") + L.bullets(
            [f'{esc(a["as_of"][8:10])}/{esc(a["as_of"][5:7])} · {esc(RULE_LABELS.get(a["rule_id"], a["rule_id"]))}: '
             f'{esc(a["title"])}' for a in recent_alerts[:8]], 14)
    if inner:
        body.append(L.block("Señales", L.RED, inner))

    # 5. Mercados: una tabla con todo, agrupada por categoría, con tendencia
    pano = {p["instrument_id"]: p["lectura"] for p in res.get("panorama", [])}
    rows, groups = [], {}
    for cat, label in CATEGORIES:
        items = sorted((s for s in summary if s["category"] == cat), key=lambda s: s["instrument"])
        if not items:
            continue
        groups[len(rows)] = label
        for s in items:
            t = src.get(f"T_{s['instrument_id']}", {})
            r = s["is_rate"]
            name = f'<b>{esc(s["instrument"])}</b>'
            badge = L.trend_badge(t.get("tendencia_calculada"), t.get("percentil_del_nivel_desde_2020"))
            if badge:
                name += "<br>" + badge
            if pano.get(s["instrument_id"]):
                name += f'<br><span style="font-size:12px;color:{L.GREY};white-space:normal">{esc(pano[s["instrument_id"]])}</span>'
            if (s.get("days_since_last") or 0) > STALE_DAYS:
                name += f' <span style="font-size:11px;color:{L.RED}">atrasado</span>'

            def ch(x):
                return (L.signed(float(x) * 100, 0, " pb") if x is not None else L.signed(None)) if r \
                    else L.signed(x, 1, " %")
            rows.append([name, (num(s["last_value"], 2) + " %") if r else price(s["last_value"]),
                         ch(s.get("chg_1d")), ch(s.get("chg_1m")), ch(s.get("chg_ytd"))])
    if rows:
        body.append(L.block("Mercados", L.GREEN, L.table(["Activo", "Último", "Día", "Mes", "Año"], rows,
                                                        group_rows=groups, size=13),
                            note="tendencia por regla fija · pct. = percentil del nivel desde 2020"))

    # 6. Noticias
    if data.get("news"):
        body.append(L.block("Noticias oficiales", L.GREY, L.bullets([
            f'<a href="{esc(n["url"])}" style="color:{L.INK}">{esc(n["title"])}</a> '
            + L.small(f'{esc(n["source_name"])} · {esc(fmt_day(n["published_at"]))}') for n in data["news"][:5]], 14)))

    # 7. Anexo: detalle para consultar
    annex = []
    hyp_html = compose_hypotheses(data.get("hyp_tracking"))
    if hyp_html:
        annex.append(hyp_html)
    pos_html = compose_positioning(data.get("cot_overview") or [], data.get("positioning_extras"), directions=False)
    if pos_html:
        annex.append(pos_html)
    liq = []
    if data["liquidity"]:
        lq = data["liquidity"][0]
        liq.append(f"<b>Liquidez neta de la Fed</b>: {num(lq['net_liquidity_b'], 0)} mm $ "
                   f"({chg_abs(lq.get('chg_4w_b'))} en 4 semanas, {chg_abs(lq.get('chg_13w_b'))} en 13) · {lq['as_of'][8:10]}/{lq['as_of'][5:7]}")
    if data["m2"]:
        m = data["m2"][0]
        liq.append(f"<b>M2</b>: {num(m['m2_b'], 0)} mm $, {num(m['chg_yoy_pct'], 1)} % interanual · {m['as_of'][5:7]}/{m['as_of'][:4]}")
    if data["fedfunds"]:
        f = data["fedfunds"]
        liq.append(f"<b>Fondos federales</b>: {num(f[0]['value'])} % · {f[0]['as_of'][5:7]}/{f[0]['as_of'][:4]}")
    if data["nfci"]:
        n = data["nfci"][0]
        liq.append(f"<b>Condiciones financieras (NFCI)</b>: {num(n['value'], 3)} (negativo = más laxas que la media)")
    if liq:
        annex.append(section("Liquidez y política monetaria (EE. UU.)", L.bullets(liq, 14)))
    corr_rows = []
    for c in data["correlations"]:
        cur, lo, hi = (float(c[k]) if c.get(k) is not None else None for k in ("corr_60", "min_1y", "max_1y"))
        flag = ""
        if cur is not None and lo is not None and hi is not None and hi > lo:
            pos = (cur - lo) / (hi - lo)
            flag = "mín. año" if pos <= 0.05 else "máx. año" if pos >= 0.95 else ""
        corr_rows.append([esc(c["label"]) + (f'<br><span style="color:{L.RED};font-weight:700;font-size:11px">{flag}</span>'
                                             if flag else ""),
                          num(cur), num(c.get("corr_3m_ago")), f"{num(lo)} a {num(hi)}", flag])
    if corr_rows:
        corr_rows.sort(key=lambda r: r[4] == "")
        annex.append(section("Correlaciones de 60 sesiones", L.table(
            ["Par", "Actual", "Hace 3 m", "Rango 1 año"], [r[:4] for r in corr_rows], size=13)))
    imf_section = compose_imf(data.get("imf") or [], report_date)
    if imf_section:
        annex.append(imf_section)
    lim = []
    lag = (src.get("X_fechas_de_cierre", {}).get("datos") or {}).get("activos_con_cierre_anterior") or {}
    if lag:
        by_date: dict[str, list[str]] = {}
        for k, v in lag.items():
            by_date.setdefault(v, []).append(k)
        lim.append("Cierres de días anteriores: " + "; ".join(
            f"{', '.join(v)} ({d[8:10]}/{d[5:7]})" for d, v in sorted(by_date.items())) + ".")
    lim += [esc(x) for x in res.get("limitaciones", [])]
    if lim:
        annex.append(section("Limitaciones de hoy", L.bullets(lim, 13)))
    if annex:
        body.append(L.block("Anexo", L.GREY, "".join(annex), note="detalle para consultar"))

    footer = ("Variaciones en %, salvo tipos de interés (pb). Cobre, MOVE, DXY, divisas, Rheinmetall, MERVAL y TA-35 "
              "proceden de Yahoo, fuente no oficial. La lectura del día la redacta una IA solo con estos datos; las "
              "frases con cifras que no cuadran con su fuente se eliminan. No es una recomendación de inversión."
              + (f' <a href="{esc(dashboard_url)}" style="color:{L.BLUE}">Abrir el dashboard</a>.' if dashboard_url else ""))
    title = f"Market Radar · {report_date.strftime('%d/%m/%Y')}"
    date_line = L.long_date(report_date).capitalize()
    html_doc = L.document("Market Radar · informe diario", date_line, status, "".join(body), footer)
    content = {"movers": [m["instrument"] for m in movers], "alerts": len(recent_alerts),
               "stale": stale, "failed_sources": failed}
    if analysis:
        content["ai"] = {k: analysis[k] for k in ("model", "result", "dropped", "usage", "web_search")}
    if data.get("hyp_tracking"):
        content["seguimiento_hipotesis"] = {k: data["hyp_tracking"][k] for k in ("recuento", "tasa_acierto", "decididas")}
    return title, html_doc, content


COT_NAMES = {"gold": "Oro", "silver": "Plata", "copper": "Cobre", "wti": "Petróleo WTI", "spx": "S&P 500",
             "ndx": "Nasdaq-100", "rut": "Russell 2000", "us10y": "Bono a 10 años", "us02y": "Bono a 2 años",
             "eurusd": "Euro", "dxy": "Dólar (DXY)", "btc": "Bitcoin (CME)"}
COT_ORDER = list(COT_NAMES)


def load_positioning_extras(db) -> dict:
    """Flujos de ETF (20 sesiones y 3 meses) y variación de reservas de oro oficiales (3 y 12 meses)."""
    out = {"etf": {}, "oro_oficial": {}}
    for t in ("spy", "gld", "ibit"):
        rows = db.select("observations", {"select": "as_of,value", "series_id": f"eq.etf_{t}_flow_usd",
                                          "order": "as_of.desc", "limit": "70"})
        if rows:
            vals = [float(r["value"]) for r in rows if r["value"] is not None]
            out["etf"][t.upper()] = {"fecha": rows[0]["as_of"], "20": sum(vals[:20]) if len(vals) >= 20 else None,
                                     "63": sum(vals[:63]) if len(vals) >= 63 else None}
    for c in ("CHN", "RUS", "IND", "USA", "DEU", "JPN"):
        rows = db.select("observations", {"select": "as_of,value", "series_id": f"eq.wgc_gold_tonnes_{c}",
                                          "order": "as_of.desc", "limit": "13"})
        if len(rows) >= 13:
            v = [float(r["value"]) for r in rows]
            out["oro_oficial"][c] = {"fecha": rows[0]["as_of"], "3m": v[0] - v[3], "12m": v[0] - v[12]}
    return out


# Fondos apalancados en bonos, índices y bitcoin de CME: en buena parte operación de base (no direccional).
BASIS_LF = {"us10y", "us02y", "spx", "ndx", "rut", "btc"}
FLOW_MIN_OI_PCT = 2.5     # cambio de 4 semanas ≥ 2,5 % del interés abierto para considerarlo un movimiento claro
ETF_MIN_USD = 1e9         # flujo de 20 sesiones ≥ 1.000 M$
GOLD_MIN_T = 10           # variación de reservas en 12 meses ≥ 10 t


def flow_directions(overview: list[dict], extras: dict | None) -> tuple[list[str], list[str]]:
    """«Hacia dónde va» y «de dónde sale», con reglas fijas sobre COT, flujos de ETF y oro oficial."""
    to, out = [], []
    for o in overview:
        inst, grp = o["instrumento"], o["grupo"]
        if grp == "lf" and inst in BASIS_LF:
            continue
        c4, oi = o.get("cambio_4_semanas"), o.get("interes_abierto")
        if c4 is None or not oi:
            continue
        share = 100 * c4 / float(oi)
        if abs(share) < FLOW_MIN_OI_PCT:
            continue
        name = COT_NAMES.get(inst, inst)
        who = o["categoria"].split(" (")[0]
        net = o["neta_contratos"]
        if c4 > 0:
            verb = "reducen cortos en" if net < 0 else "aumentan largos en"
        else:
            verb = "aumentan cortos en" if net <= 0 else "reducen largos en"
        p13 = o.get("precio_cambio_13_semanas")
        unit = " pb" if inst in ("us10y", "us02y") else " %"
        price = "" if p13 is None else (f"; {'rendimiento' if unit == ' pb' else 'precio'} en 13 semanas "
                                        f"{'+' if p13 > 0 else ''}{num(p13, 0 if unit == ' pb' else 1)}{unit}")
        text = (f"{name}: {who} {verb} {num(abs(c4), 0)} contratos en 4 semanas "
                f"({num(abs(share), 1)} % del interés abierto; percentil {o['percentil_5_años']} de 5 años{price}).")
        (to if c4 > 0 else out).append((abs(share), text))
    extras = extras or {}
    for t, v in (extras.get("etf") or {}).items():
        f20 = v.get("20")
        if f20 is not None and abs(f20) >= ETF_MIN_USD:
            text = f"ETF {t}: {'entradas' if f20 > 0 else 'salidas'} netas de {num(abs(f20) / 1e6, 0)} M$ en 20 sesiones."
            (to if f20 > 0 else out).append((-1, text))
    names = {"CHN": "China", "RUS": "Rusia", "IND": "India", "USA": "EE. UU.", "DEU": "Alemania", "JPN": "Japón"}
    for c, v in (extras.get("oro_oficial") or {}).items():
        if abs(v["12m"]) >= GOLD_MIN_T:
            text = (f"Oro, banco central de {names.get(c, c)}: {'+' if v['12m'] > 0 else ''}{num(v['12m'], 0)} t "
                    f"en 12 meses ({'+' if v['3m'] > 0 else ''}{num(v['3m'], 0)} t en 3 meses).")
            (to if v["12m"] > 0 else out).append((-2, text))
    to.sort(key=lambda x: -x[0])      # primero el COT (por intensidad), después ETF y oro oficial
    out.sort(key=lambda x: -x[0])
    cot_to = [x for x in to if x[0] >= 0][:5] + [x for x in to if x[0] < 0]
    cot_out = [x for x in out if x[0] >= 0][:5] + [x for x in out if x[0] < 0]
    return [t for _, t in cot_to], [t for _, t in cot_out]


def compose_positioning(overview: list[dict], extras: dict | None, directions: bool = True) -> str:
    if not overview:
        return ""
    by = {(o["instrumento"], o["grupo"]): o for o in overview}
    date = max(o["fecha_posiciones"] for o in overview)

    def pct_cell(p):
        color = RED if p is not None and (p >= 95 or p <= 5) else ("#9a6700" if p is not None and (p >= 90 or p <= 10) else INK)
        return f'<span style="color:{color};font-weight:{600 if color != INK else 400}">{p}</span>'

    def signed_int(x):
        return "—" if x is None else ("+" if x > 0 else "") + num(x, 0)

    def rows_for(groups):
        rows = []
        for inst in COT_ORDER:
            for g in groups:
                o = by.get((inst, g))
                if not o:
                    continue
                pchg = o.get("precio_cambio_13_semanas")
                unit = " pb" if inst in ("us10y", "us02y") else " %"
                rows.append([esc(COT_NAMES[inst]) + (f' <span style="color:{GREY};font-size:11px">({esc(o["categoria"])})</span>'
                                                     if len(groups) > 1 else ""),
                             signed_int(o["neta_contratos"]), pct_cell(o["percentil_5_años"]),
                             chg_abs(o.get("cambio_4_semanas")),
                             "—" if pchg is None else ("+" if pchg > 0 else "") + num(pchg, 0 if unit == " pb" else 1) + unit])
        return rows

    head = ["Mercado", "Neta", "Pct.", "4 sem.", "Precio*"]
    inst_rows = rows_for(["am"])
    spec_rows = rows_for(["mm", "lf"])
    body = (f'<p style="font-size:13px;margin:4px 0 6px">COT de la CFTC, posiciones del {date[8:10]}/{date[5:7]} '
            "(publicadas el viernes siguiente).</p>")
    to, out = flow_directions(overview, extras) if directions else ([], [])
    if to or out:
        def col(title, color, items):
            lis = "".join(f'<li style="margin:3px 0;font-size:13px;line-height:1.4">{esc(i)}</li>' for i in items) \
                or f'<li style="font-size:13px;color:{GREY}">Sin movimientos claros.</li>'
            return (f'<td style="vertical-align:top;width:50%;padding:6px 8px;border-top:3px solid {color}">'
                    f'<p style="margin:0 0 4px;font-weight:600;font-size:14px;color:{color}">{title}</p>'
                    f'<ul style="margin:0;padding-left:16px">{lis}</ul></td>')
        body += ('<table style="border-collapse:collapse;width:100%;margin:6px 0 10px"><tr>'
                 + col("Hacia dónde va", GREEN, to) + col("De dónde sale", RED, out) + "</tr></table>"
                 f'<p style="font-size:11px;color:{GREY};margin:0 0 8px">Reglas fijas: cambios de 4 semanas de al menos '
                 f"el {num(FLOW_MIN_OI_PCT, 1)} % del interés abierto (gestoras y fondos especulativos; se excluyen los "
                 "fondos apalancados en bonos, índices y bitcoin por la operación de base), flujos de ETF de al menos "
                 "1.000 M$ en 20 sesiones y variaciones de reservas de oro de al menos 10 t en 12 meses.</p>")
    if inst_rows:
        body += ('<p style="margin:10px 0 4px;font-weight:600;font-size:14px">Dinero institucional: gestoras</p>'
                 + table(head, inst_rows))
    if spec_rows:
        body += ('<p style="margin:12px 0 4px;font-weight:600;font-size:14px">Especulativo: fondos gestionados '
                 "(materias primas) y fondos apalancados (financieros)</p>" + table(head, spec_rows))
    if extras:
        etf = extras.get("etf") or {}
        gold = extras.get("oro_oficial") or {}
        lines = []
        if etf:
            parts = []
            for t, v in etf.items():
                if v.get("20") is not None:
                    parts.append(f"{t} {signed_int(v['20'] / 1e6)} M$ en 20 sesiones"
                                 + (f" ({signed_int(v['63'] / 1e6)} M$ en 3 meses)" if v.get("63") is not None else ""))
            if parts:
                lines.append("Flujos netos de ETF (emisor): " + "; ".join(parts) + ".")
        if gold:
            names = {"CHN": "China", "RUS": "Rusia", "IND": "India", "USA": "EE. UU.", "DEU": "Alemania", "JPN": "Japón"}
            moved = [f"{names[c]} {signed_int(v['12m'])} t en 12 meses" for c, v in gold.items() if abs(v["12m"]) >= 1]
            still = [names[c] for c, v in gold.items() if abs(v["12m"]) < 1]
            lines.append("Reservas de oro de bancos centrales (FMI): " + ("; ".join(moved) if moved else "sin cambios")
                         + (f"; sin cambios: {', '.join(still)}" if still else "") + ".")
        body += "".join(f'<p style="font-size:13px;margin:8px 0 0">{esc(l)}</p>' for l in lines)
    body += (f'<p style="font-size:11px;color:{GREY};margin:8px 0 0">Percentil de la posición neta en 5 años: rojo '
             "≥ 95 o ≤ 5, ámbar ≥ 90 o ≤ 10. En bonos, índices y bitcoin de CME, gestoras largas y fondos apalancados "
             "cortos son en parte la misma operación de base (contado contra futuro), no apuestas opuestas. Las categorías "
             "del COT no identifican «manos fuertes»: describen tipos de participante. Posicionamiento no es dirección. "
             "Pct.: percentil de la neta en 5 años. 4 sem.: cambio de la neta. * Variación del precio en 13 semanas; en bonos, del "
             "rendimiento (sube el rendimiento = baja el precio del bono).</p>")
    return section("Posicionamiento por grupos", body)


HYP_STATE = {"acierto": ("Acierto", GREEN), "fallo": ("Fallo", RED), "no activada": ("No activada", GREY),
             "pendiente": ("Pendiente", GREY), "en curso": ("En curso", "#9a6700")}


def compose_hypotheses(t: dict | None) -> str:
    if not t or not t["items"]:
        return ""
    c = t["recuento"]
    head = (f'<p style="font-size:13px;margin:4px 0 8px">{c.get("acierto", 0)} aciertos, {c.get("fallo", 0)} fallos, '
            f'{c.get("no activada", 0)} no activadas, {c.get("en curso", 0) + c.get("pendiente", 0)} abiertas'
            + (f' · tasa de acierto {t["tasa_acierto"]} % sobre {t["decididas"]} decididas' if t["decididas"] else "")
            + "</p>")
    rows = []
    for it in t["items"][:10]:
        label, color = HYP_STATE.get(it["estado"], (it["estado"], GREY))
        det = ""
        if "cambio" in it or "cambio_hasta_ahora" in it:
            v = it.get("cambio", it.get("cambio_hasta_ahora"))
            det = f'activada el {it["activada"][8:10]}/{it["activada"][5:7]}; {"+" if v > 0 else ""}{num(v, 0 if it["unidad"] == "pb" else 2)} {it["unidad"]}'
        elif it.get("activada"):
            det = f'activada el {it["activada"][8:10]}/{it["activada"][5:7]}'
        rows.append([esc(f'{it["fecha"][8:10]}/{it["fecha"][5:7]}'),
                     f'<b>Si</b> {esc(ai_analysis._no_si(it.get("si") or ""))} → {esc(it.get("entonces") or "")}',
                     f'<span style="color:{color};font-weight:600">{label}</span>'
                     + (f'<br><span style="font-size:11px;color:{GREY}">{esc(det)}</span>' if det else "")])
    note = (f'<p style="font-size:11px;color:{GREY};margin:6px 0 0">Evaluación automática con los cierres posteriores: '
            "la hipótesis se activa cuando se cumple su condición y acierta si, al final de su horizonte, el activo de "
            "la consecuencia se ha movido en la dirección prevista. Medida sencilla, no un backtest.</p>")
    return section("Seguimiento de hipótesis", head + table(["Fecha", "Hipótesis", "Estado"], rows,
                                                          align_right_from=99) + note)


EXC_TITLE = "Situaciones excepcionales (descriptivo, no validado)"
EXC_TYPES = {"A": "Posicionamiento extremo", "B": "Posible liquidación", "C": "Exceso de apalancamiento",
             "D": "Flujos de capital", "E": "Movimiento anómalo"}


def compose_exceptional(signals: list[dict], readings: dict[int, str] | None = None) -> str:
    readings = readings or {}
    if not signals:
        return section(EXC_TITLE, f'<p style="font-size:13px;color:{GREY}">Ninguna hoy según las '
                                                     "reglas exceptional-v1.1.</p>")
    rows = []
    for idx, s in enumerate(signals[:15]):
        color = RED if s["severidad"] == "alta" else "#9a6700"
        rows.append([f'<span style="color:{color};font-weight:600">{esc(EXC_TYPES.get(s["tipo"], s["tipo"]))}</span>'
                     f'<br><span style="font-size:11px;color:{GREY}">{esc(s["naturaleza"])}</span>',
                     f'<b>{esc(s["titulo"])}</b><br><span style="font-size:12px">{esc(readings.get(idx) or s["detalle"])}</span>'
                     f'<br><span style="font-size:11px;color:{GREY}">Fuente: {esc(", ".join(s["fuentes"]))} · '
                     f'dato del {esc(s["fecha_dato"][8:10])}/{esc(s["fecha_dato"][5:7])}'
                     + (" · lectura redactada por IA" if idx in readings else "") + '</span>'])
    note = (f'<p style="font-size:11px;color:{GREY};margin:6px 0 0">Capa descriptiva de la Vía A con reglas fijas '
            "(exceptional-v1.1), sin validación estadística. No es el radar de fragilidad de la Vía B (puntuaciones "
            "de combustible y chispa del Anexo A), que solo se publicará si supera su backtest. «Compatible con» "
            "indica un patrón coherente con esa situación, no su confirmación. El COT refleja posiciones del martes, "
            "publicadas el viernes.</p>")
    return section(EXC_TITLE, table(["Tipo", "Señal"], rows, align_right_from=99) + note)


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
        data["positioning_extras"] = load_positioning_extras(db)
    except Exception as exc:
        data["positioning_extras"] = None
        print(f"::warning::Flujos y reservas para posicionamiento: {str(exc)[:200]}")
    try:
        data["hyp_tracking"] = hypotheses.track(db, data["closes"], report_date)
        t = data["hyp_tracking"]
        print(f"::notice::Seguimiento de hipótesis: {t['recuento']} · tasa de acierto {t['tasa_acierto']}")
    except Exception as exc:
        data["hyp_tracking"] = None
        print(f"::warning::Seguimiento de hipótesis: {str(exc)[:300]}")
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
        print("::notice::LO ESENCIAL: " + res.get("titular", "") + " || " +
              " || ".join(p["texto"] for p in res.get("puntos", [])) + " || A VIGILAR: " +
              " || ".join(v["texto"] for v in res.get("a_vigilar", [])))
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
    try:
        sys.exit(main())
    except Exception:
        import traceback
        tb = traceback.format_exc()
        print(tb)
        print("::error::Informe diario: " + " | ".join(tb.strip().splitlines()[-6:])[:900])
        sys.exit(1)
