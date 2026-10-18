#!/usr/bin/env python3
"""Informe diario de Market Radar: lo compone desde las vistas de Supabase,
lo guarda en `daily_reports` y lo envía por correo con Gmail (SMTP).

Todo el contenido es determinista: sale de las vistas y de las alertas ya
calculadas. No interviene ninguna IA.

Se envía de lunes a viernes. El sábado no se envía salvo con --force.

Variables de entorno:
  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
  GMAIL_USER, GMAIL_APP_PASSWORD   (contraseña de aplicación de Google)
  REPORT_TO        destinatario (por defecto, GMAIL_USER)
  DASHBOARD_URL    opcional, enlace al dashboard
  REPORT_FORCE=true  enviar aunque sea fin de semana
  REPORT_DRY_RUN=true  componer y guardar, sin enviar
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import os
import smtplib
import sys
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ingest"))
from common import Supabase, env_flag, now_utc, require_env  # noqa: E402

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
        "alerts": db.select("alerts", {
            "select": "as_of,rule_id,title,kind", "as_of": f"gte.{since}",
            "order": "as_of.desc,rule_id"}),
        "sync": db.select("provider_sync_logs", {
            "select": "source_id,status,started_at,error", "order": "started_at.desc", "limit": "30"}),
    }


# --------------------------------------------------------------------------- #
# Composición
# --------------------------------------------------------------------------- #
def compose(data: dict, report_date: dt.date, dashboard_url: str) -> tuple[str, str, dict]:
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

    # Alertas
    if recent_alerts:
        rows = [[esc(a["as_of"]), esc(RULE_LABELS.get(a["rule_id"], a["rule_id"])), esc(a["title"])]
                for a in recent_alerts]
        body.append(section("Alertas", table(["Fecha", "Tipo", "Detalle"], rows, align_right_from=99)))

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
    return title, html_doc, content


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
    title, html_doc, content = compose(data, report_date, os.environ.get("DASHBOARD_URL", "").strip())
    row = {"report_date": report_date.isoformat(), "generated_at": now_utc().isoformat(),
           "experimental": True, "content": content, "html": html_doc,
           "stale_sources": content["stale"], "status": "draft"}
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
