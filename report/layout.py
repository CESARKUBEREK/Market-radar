"""Maquetación del informe diario: piezas HTML aptas para correo (estilos en línea, sin CSS externo).

Principios de lectura:
- lo importante arriba y en pocas líneas («En 30 segundos»), el detalle al final («Anexo»);
- una sola columna de 680 px, letra de 15 px con interlineado amplio;
- cada bloque con una etiqueta corta y un color propio;
- cifras alineadas a la derecha, subidas en verde y bajadas en rojo;
- fuentes y notas en gris pequeño, para que no compitan con el texto.
"""

from __future__ import annotations

import html

INK, GREY, LIGHT, LINE = "#1f2328", "#6e7781", "#f6f8fa", "#e5e7eb"
GREEN, RED, AMBER, BLUE, PURPLE = "#1a7f37", "#cf222e", "#9a6700", "#0969da", "#8250df"
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,Helvetica,sans-serif"
WEEKDAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MONTHS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
          "noviembre", "diciembre"]


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def num(x, dec=2) -> str:
    if x is None:
        return "—"
    s = f"{float(x):,.{dec}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def signed(x, dec=2, unit="") -> str:
    """Cifra con signo y color."""
    if x is None:
        return f'<span style="color:{GREY}">—</span>'
    x = float(x)
    color = GREEN if x > 0 else RED if x < 0 else GREY
    return f'<span style="color:{color}">{"+" if x > 0 else ""}{num(x, dec)}{unit}</span>'


def long_date(d) -> str:
    return f"{WEEKDAYS[d.weekday()]} {d.day} de {MONTHS[d.month - 1]} de {d.year}"


# --- bloques ------------------------------------------------------------------ #
def block(label: str, color: str, inner: str, *, note: str = "") -> str:
    """Bloque principal: etiqueta corta en versalitas con barra de color."""
    return (f'<div style="margin:30px 0 0">'
            f'<div style="border-left:4px solid {color};padding:2px 0 2px 10px;margin:0 0 12px">'
            f'<span style="font-size:12px;letter-spacing:.08em;text-transform:uppercase;font-weight:700;'
            f'color:{color}">{esc(label)}</span>'
            + (f'<span style="font-size:12px;color:{GREY}"> · {note}</span>' if note else "")
            + f"</div>{inner}</div>")


def sub(title: str, color: str = INK) -> str:
    return f'<p style="margin:16px 0 6px;font-size:15px;font-weight:700;color:{color}">{esc(title)}</p>'


def bullets(items: list[str], size: int = 15) -> str:
    if not items:
        return ""
    return ('<ul style="margin:0;padding-left:20px">'
            + "".join(f'<li style="margin:0 0 8px;font-size:{size}px;line-height:1.55">{i}</li>' for i in items)
            + "</ul>")


def para(text: str, size: int = 15, color: str = INK, margin: str = "0 0 8px") -> str:
    return f'<p style="margin:{margin};font-size:{size}px;line-height:1.55;color:{color}">{text}</p>'


def small(text: str) -> str:
    return f'<span style="font-size:12px;color:{GREY}">{text}</span>'


def card(inner: str, color: str = BLUE, bg: str = LIGHT) -> str:
    return (f'<div style="background:{bg};border-radius:8px;border-left:4px solid {color};'
            f'padding:14px 16px;margin:0 0 4px">{inner}</div>')


def chips(items: list[tuple[str, str, str]]) -> str:
    """Cifras clave: (nombre, valor, variación HTML). Bloques en línea que se reordenan en el móvil."""
    cells = "".join(
        f'<div style="display:inline-block;vertical-align:top;width:31%;min-width:150px;margin:0 1% 10px 0;'
        f'border:1px solid {LINE};border-radius:8px;padding:8px 10px;box-sizing:border-box">'
        f'<div style="font-size:12px;color:{GREY}">{esc(n)}</div>'
        f'<div style="font-size:16px;font-weight:700;margin:2px 0">{v}</div>'
        f'<div style="font-size:13px">{c}</div></div>'
        for n, v, c in items)
    return f'<div style="margin:12px 0 0">{cells}</div>'


def button(url: str, label: str, secondary: tuple[str, str] | None = None) -> str:
    """Botón visible y compatible con correo (tabla con fondo; funciona también en Outlook)."""
    sec = (f'<div style="margin:8px 0 0;font-size:14px"><a href="{esc(secondary[0])}" style="color:{BLUE};'
           f'text-decoration:underline">{esc(secondary[1])}</a></div>' if secondary else "")
    return (f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin:16px 0 0"><tr>'
            f'<td style="background:{BLUE};border-radius:8px">'
            f'<a href="{esc(url)}" style="display:inline-block;padding:11px 22px;font-size:15px;font-weight:700;'
            f'color:#ffffff;text-decoration:none;white-space:nowrap;font-family:{FONT}">{esc(label)} →</a></td>'
            f'</tr></table>{sec}')


def comment(icon: str, label: str, color: str, text: str) -> str:
    """Línea de comentario dentro de «En 30 segundos»: etiqueta de color y una o dos frases."""
    return (f'<div style="margin:12px 0 0;padding-top:10px;border-top:1px solid {LINE};font-size:14px;line-height:1.55">'
            f'<span style="font-weight:700;color:{color}">{icon} {esc(label)}.</span> {text}</div>')


def table(headers: list[str], rows: list[list[str]], *, align_right_from: int = 1, size: int = 14,
          group_rows: dict[int, str] | None = None) -> str:
    """Tabla legible: cabecera discreta, filas alternas y cifras a la derecha.
    group_rows: {índice de fila: título} inserta una fila de grupo antes de esa fila."""
    group_rows = group_rows or {}
    th = "".join(
        f'<th style="text-align:{"right" if i >= align_right_from else "left"};padding:6px 8px;font-weight:600;'
        f'color:{GREY};font-size:12px;border-bottom:1px solid {LINE};white-space:nowrap">{h}</th>'
        for i, h in enumerate(headers))
    trs = []
    for k, r in enumerate(rows):
        if k in group_rows:
            trs.append(f'<tr><td colspan="{len(headers)}" style="padding:12px 8px 4px;font-size:12px;font-weight:700;'
                       f'letter-spacing:.06em;text-transform:uppercase;color:{GREY}">{esc(group_rows[k])}</td></tr>')
        bg = LIGHT if k % 2 else "#ffffff"
        tds = "".join(
            f'<td style="text-align:{"right" if i >= align_right_from else "left"};padding:7px 6px;font-size:{size}px;'
            f'background:{bg};vertical-align:top;line-height:1.45;'
            f'{"white-space:nowrap;" if i >= align_right_from else ""}'
            f'font-variant-numeric:tabular-nums">{c}</td>'
            for i, c in enumerate(r))
        trs.append(f"<tr>{tds}</tr>")
    return (f'<table style="border-collapse:collapse;width:100%;font-family:{FONT}">'
            f"<thead><tr>{th}</tr></thead><tbody>{''.join(trs)}</tbody></table>")


def trend_badge(trend: str | None, pct: int | None = None) -> str:
    if not trend or trend == "sin datos":
        return ""
    color = GREEN if trend == "alcista" else RED if trend == "bajista" else GREY
    label = "lateral" if trend.startswith("lateral") else trend
    return (f'<span style="font-size:11px;color:{color};border:1px solid {color};border-radius:10px;'
            f'padding:0 6px;white-space:nowrap">{esc(label)}</span>'
            + (f'<span style="font-size:11px;color:{GREY}"> pct. {pct}</span>' if pct is not None else ""))


def document(title: str, date_line: str, status: str, body: str, footer: str) -> str:
    return (f'<!doctype html><html><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"></head>'
            f'<body style="margin:0;padding:0;background:#ffffff">'
            f'<div style="max-width:680px;margin:0 auto;padding:20px 16px 32px;font-family:{FONT};color:{INK}">'
            f'<div style="font-size:12px;letter-spacing:.1em;text-transform:uppercase;color:{GREY};font-weight:700">'
            f'{esc(title)}</div>'
            f'<div style="font-size:24px;font-weight:700;margin:4px 0 2px;line-height:1.25">{esc(date_line)}</div>'
            f'{status}{body}'
            f'<div style="margin:36px 0 0;padding-top:12px;border-top:1px solid {LINE};font-size:12px;'
            f'line-height:1.5;color:{GREY}">{footer}</div></div></body></html>')
