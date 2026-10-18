"""Análisis con IA para el informe diario: Hechos, Interpretación e Hipótesis.

Dos pasos, para que la IA no pueda inventar datos ni causas:

1. Evidencia (determinista, sin IA). Se reúnen con un identificador cada uno:
   M = movimientos de mercado de la última sesión (vistas de Supabase),
   A = alertas, C = eventos del calendario (publicados y próximos),
   N = noticias oficiales guardadas (Fed, BCE, Banco de Inglaterra),
   P = patrones de movimientos conjuntos detectados por reglas (p. ej. «suben los
   rendimientos y cae el oro»), con los temas que conviene contrastar.
2. Redacción estructurada con la API de OpenAI (Responses API, salida JSON con
   esquema estricto, sin búsqueda web). Cada hecho debe citar identificadores de
   evidencia; el código descarta los que no lo hagan, así como las
   interpretaciones e hipótesis sin apoyo.

Reglas que se imponen en la instrucción y se comprueban donde se puede:
- Un hecho solo es lo que dicen los datos o una noticia citada.
- No se afirma que una noticia causó un movimiento salvo que la propia noticia lo
  diga; si no, se habla de coincidencia temporal.
- Las hipótesis son condicionales («si…, entonces…») e incluyen qué las invalidaría.
- Nada de recomendaciones de compra o venta.

Variables de entorno:
  OPENAI_API_KEY      obligatoria para activar el análisis
  ANALYSIS_MODEL      por defecto gpt-5.4-mini
  ANALYSIS_EFFORT     esfuerzo de razonamiento: none, low (por defecto), medium, high
"""

from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import time

import requests
from zoneinfo import ZoneInfo

API = "https://api.openai.com/v1/responses"
DEFAULT_MODEL = "gpt-5.4-mini"
PARIS = ZoneInfo("Europe/Paris")
SOURCE_LABEL = {"fred": "FRED", "tiingo": "Tiingo", "ecb": "BCE", "yahoo": "Yahoo (no oficial)",
                "euronext": "Euronext", "imf": "FMI", "manual": "manual"}
EVENT_LABEL = {"fomc": "Fed", "cpi": "IPC", "employment": "Empleo", "other": "Macro", "holiday": "Festivo",
               "expiry": "Vencimiento", "rebalance": "Rebalanceo", "roll": "WTI"}

# Patrones de movimientos conjuntos: (id, descripción, condición, temas a buscar)
Z_MOVE = 1.0   # un movimiento cuenta como «subida/bajada» si supera 1 desviación típica diaria


def _z(s: dict) -> float | None:
    vol, c = s.get("vol_20"), s.get("chg_1d")
    if vol in (None, 0) or c is None:
        return None
    scale = 1 if s["is_rate"] else 100
    daily = float(vol) / scale / (252 ** 0.5)
    return (float(c) / scale) / daily if daily else None


def _round_level(x: float, is_rate: bool) -> float:
    if is_rate:
        return round(x, 2)
    return round(x, 4 if abs(x) < 10 else 2 if abs(x) < 10000 else 0)


def _paris(iso: str) -> str:
    d = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return d.astimezone(PARIS).strftime("%Y-%m-%d %H:%M") + " (París)"


def _source_of(series_id: str) -> str:
    return SOURCE_LABEL.get(series_id.split("_", 1)[0], series_id.split("_", 1)[0])


def build_evidence(data: dict, report_date: dt.date) -> dict:
    summary = data.get("summary") or []
    by_id = {s["instrument_id"]: s for s in summary}
    moves = []
    for s in summary:
        z = _z(s)
        if s.get("chg_1d") is None:
            continue
        moves.append({
            "instrumento": s["instrument"], "instrument_id": s["instrument_id"], "categoria": s["category"],
            "fecha_cierre": s["last_date"], "ultimo": _round_level(float(s["last_value"]), s["is_rate"]),
            # Tipos: variación en puntos básicos; resto: en %.
            "variacion_dia": round(float(s["chg_1d"]) * (100 if s["is_rate"] else 1), 1 if s["is_rate"] else 2),
            "unidad_variacion": "pb" if s["is_rate"] else "%",
            "variacion_semana": None if s.get("chg_1w") is None else
            round(float(s["chg_1w"]) * (100 if s["is_rate"] else 1), 1 if s["is_rate"] else 2),
            "desviaciones_tipicas_dia": None if z is None else round(z, 1), "fuente": _source_of(s["series_id"]),
        })
    moves.sort(key=lambda m: abs(m["desviaciones_tipicas_dia"] or 0), reverse=True)
    top = [m for m in moves if abs(m["desviaciones_tipicas_dia"] or 0) >= 1.5][:10] or moves[:6]
    # Siempre los anclas macro, aunque se hayan movido poco
    anchors = {"us10y", "us02y", "dxy", "gold", "spx", "brent", "eurusd", "btc"}
    chosen = {m["instrument_id"] for m in top} | anchors
    ev: dict[str, dict] = {}
    for i, m in enumerate([m for m in moves if m["instrument_id"] in chosen], 1):
        ev[f"M{i}"] = {"tipo": "dato de mercado", **m}

    def direction(iid: str) -> int:
        s = by_id.get(iid)
        z = _z(s) if s else None
        return 0 if z is None or abs(z) < Z_MOVE else (1 if z > 0 else -1)

    patterns = []
    rules = [
        ("rendimientos suben y oro cae", direction("us10y") > 0 and direction("gold") < 0,
         ["inflación EE. UU.", "expectativas de la Fed", "dólar", "rendimientos del Tesoro"]),
        ("rendimientos y dólar suben a la vez", direction("us10y") > 0 and direction("dxy") > 0,
         ["datos macro EE. UU.", "Fed", "subastas del Tesoro"]),
        ("rendimientos bajan y oro sube", direction("us10y") < 0 and direction("gold") > 0,
         ["aversión al riesgo", "expectativas de recortes de la Fed", "geopolítica"]),
        ("bolsa y rendimientos caen a la vez", direction("spx") < 0 and direction("us10y") < 0,
         ["aversión al riesgo", "crecimiento económico", "geopolítica"]),
        ("bolsa cae y rendimientos suben", direction("spx") < 0 and direction("us10y") > 0,
         ["inflación", "Fed", "valoraciones"]),
        ("petróleo se mueve con fuerza", direction("brent") != 0 or direction("wti") != 0,
         ["OPEP+", "inventarios de crudo", "geopolítica Oriente Medio"]),
        ("dólar se mueve con fuerza", direction("dxy") != 0,
         ["política monetaria Fed y BCE", "datos macro", "flujos de refugio"]),
        ("curva 2-10 años se mueve", (direction("us02y") != direction("us10y")) and
         (direction("us02y") != 0 or direction("us10y") != 0), ["expectativas de tipos", "Fed"]),
    ]
    for desc, ok, topics in rules:
        if ok:
            patterns.append({"patron": desc, "temas_relacionados": topics})
    for i, p in enumerate(patterns, 1):
        ev[f"P{i}"] = {"tipo": "patrón detectado por reglas (no es una causa)", **p}

    for i, a in enumerate((data.get("alerts") or [])[:12], 1):
        ev[f"A{i}"] = {"tipo": "alerta", "fecha": a["as_of"], "regla": a["rule_id"], "texto": a["title"]}

    since = (report_date - dt.timedelta(days=4)).isoformat()
    until = (report_date + dt.timedelta(days=7)).isoformat()
    events = [e for e in (data.get("events_window") or []) if since <= e["event_at"][:10] <= until]
    for i, e in enumerate(events[:20], 1):
        ev[f"C{i}"] = {"tipo": "evento de calendario", "fecha": _paris(e["event_at"]),
                       "ya_ocurrido": e["event_at"][:10] < report_date.isoformat(),
                       "clase": EVENT_LABEL.get(e["kind"], e["kind"]), "texto": e.get("notes")}

    for i, n in enumerate((data.get("news_ai") or [])[:20], 1):
        ev[f"N{i}"] = {"tipo": "noticia oficial", "fecha": _paris(n["published_at"]), "fuente": n["source_name"],
                       "titular": n["title"], "resumen": (n.get("summary") or "")[:300], "url": n["url"]}
    return ev


# --- llamada a la API ------------------------------------------------------- #
def _call(payload: dict, key: str) -> dict:
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    for attempt in range(4):
        r = requests.post(API, headers=headers, data=json.dumps(payload), timeout=300)
        if r.status_code in (429, 500, 502, 503):
            time.sleep(15 * 2 ** attempt)
            continue
        if not r.ok:
            raise RuntimeError(f"API de OpenAI HTTP {r.status_code}: {r.text[:300]}")
        return r.json()
    raise RuntimeError("La API de OpenAI no respondió tras 4 intentos")


STRUCTURE_SYSTEM = """Redactas la sección «Análisis» del informe diario de Market Radar, en español, para uso \
personal. Separa estrictamente tres niveles:

HECHOS: qué ha ocurrido y qué fuentes lo confirman. Solo afirmaciones verificables en la evidencia (M, A, C, N, \
P solo como descripción de movimientos). Cada hecho cita sus identificadores en «fuentes». \
Cifras exactas tal como aparecen en la evidencia, con fecha. Un patrón P no es un hecho causal.

INTERPRETACIONES: por qué podría ser relevante para el mercado. Usa condicional («podría», «es coherente con»). \
Cada una se apoya en identificadores de hechos o evidencia. NO afirmes que una noticia provocó un movimiento salvo \
que la propia noticia N lo diga expresamente; entonces atribúyelo («según …»). Sin esa evidencia, habla de \
coincidencia temporal. Si varias explicaciones son posibles, \
dilo. Indica la confianza: baja, media o alta.

HIPÓTESIS: consecuencias posibles si se cumplen condiciones observables. Formato «si … entonces …», con \
horizonte temporal y qué la invalidaría. Vincúlalas a eventos del calendario (C) cuando existan.

Además, en «limitaciones», señala lo que no se ha podido confirmar o las fuentes que faltan.
Nada de recomendaciones de compra o venta. Máximo 6 hechos, 4 interpretaciones y 3 hipótesis. Frases cortas.

Estilo: números en formato español (coma decimal, punto de miles), con 2 decimales como máximo en precios y \
porcentajes y los tipos en puntos básicos (pb). Fechas como «16/10». No uses nombres técnicos de campos \
(«desviaciones_tipicas_dia», «M3»…) dentro del texto: los identificadores van solo en las listas de fuentes. \
Un movimiento con más de 2 desviaciones típicas puede describirse como «inusualmente amplio». \
En las hipótesis, el campo «si» empieza directamente por la condición, sin la palabra «si»."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["hechos", "interpretaciones", "hipotesis", "limitaciones"],
    "properties": {
        "hechos": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["texto", "fuentes"],
            "properties": {"texto": {"type": "string"}, "fuentes": {"type": "array", "items": {"type": "string"}}}}},
        "interpretaciones": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["texto", "se_apoya_en", "confianza"],
            "properties": {"texto": {"type": "string"},
                           "se_apoya_en": {"type": "array", "items": {"type": "string"}},
                           "confianza": {"type": "string", "enum": ["baja", "media", "alta"]}}}},
        "hipotesis": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["si", "entonces", "horizonte", "invalidacion", "se_apoya_en"],
            "properties": {"si": {"type": "string"}, "entonces": {"type": "string"},
                           "horizonte": {"type": "string"}, "invalidacion": {"type": "string"},
                           "se_apoya_en": {"type": "array", "items": {"type": "string"}}}}},
        "limitaciones": {"type": "array", "items": {"type": "string"}},
    },
}


def structure(evidence: dict, model: str, key: str, effort: str) -> tuple[dict, dict]:
    payload = {"model": model, "instructions": STRUCTURE_SYSTEM,
               "input": json.dumps({"evidencia": evidence}, ensure_ascii=False),
               "text": {"format": {"type": "json_schema", "name": "analisis_diario", "strict": True,
                                   "schema": SCHEMA}},
               "max_output_tokens": 6000}
    if effort and effort != "none":
        payload["reasoning"] = {"effort": effort}
    resp = _call(payload, key)
    if resp.get("status") not in (None, "completed"):
        raise RuntimeError(f"Respuesta incompleta de OpenAI: {resp.get('status')} "
                           f"{(resp.get('incomplete_details') or {}).get('reason', '')}")
    text = ""
    for item in resp.get("output") or []:
        for c in item.get("content") or []:
            if c.get("type") == "refusal":
                raise RuntimeError(f"El modelo rechazó la petición: {c.get('refusal', '')[:200]}")
            if c.get("type") == "output_text":
                text += c.get("text", "")
    return json.loads(text), resp.get("usage") or {}


def validate(result: dict, known: set[str]) -> tuple[dict, list[str]]:
    """Descarta lo que no cita evidencia existente. Devuelve el resultado limpio y lo descartado."""
    dropped = []

    def ok_refs(refs: list[str]) -> list[str]:
        return [r for r in refs if r in known]

    hechos = []
    for h in result.get("hechos", []):
        refs = ok_refs(h.get("fuentes", []))
        if refs:
            hechos.append({**h, "fuentes": refs})
        else:
            dropped.append(f"hecho sin fuente: {h.get('texto', '')[:80]}")
    interp = []
    for it in result.get("interpretaciones", []):
        refs = ok_refs(it.get("se_apoya_en", []))
        if refs:
            interp.append({**it, "se_apoya_en": refs})
        else:
            dropped.append(f"interpretación sin apoyo: {it.get('texto', '')[:80]}")
    hyp = []
    for hp in result.get("hipotesis", []):
        refs = ok_refs(hp.get("se_apoya_en", []))
        if refs:
            hyp.append({**hp, "se_apoya_en": refs})
        else:
            dropped.append(f"hipótesis sin apoyo: {hp.get('si', '')[:80]}")
    return {"hechos": hechos, "interpretaciones": interp, "hipotesis": hyp,
            "limitaciones": result.get("limitaciones", [])}, dropped


def run(data: dict, report_date: dt.date) -> dict | None:
    """Análisis completo, o None si no hay clave. Los errores se propagan al llamador."""
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        return None
    model = os.environ.get("ANALYSIS_MODEL", "").strip() or DEFAULT_MODEL
    effort = os.environ.get("ANALYSIS_EFFORT", "").strip() or "low"
    evidence = build_evidence(data, report_date)
    result, usage = structure(evidence, model, key, effort)
    clean, dropped = validate(result, set(evidence))
    return {"model": model, "result": clean, "dropped": dropped, "sources": evidence,
            "usage": {"input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens")},
            "web_search": False}


# --- HTML -------------------------------------------------------------------- #
def _esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _refs_html(refs: list[str], sources: dict) -> str:
    parts = []
    for r in refs:
        s = sources.get(r, {})
        if s.get("url"):
            label = s.get("fuente") or s.get("titulo") or r
            parts.append(f'<a href="{_esc(s["url"])}" style="color:#6e7781">{_esc(r)} {_esc(label)[:40]}</a>')
        elif s.get("tipo") == "dato de mercado":
            parts.append(f'{_esc(r)} {_esc(s.get("instrumento"))} ({_esc(s.get("fuente"))}, {_esc(s.get("fecha_cierre"))})')
        elif s.get("tipo") == "evento de calendario":
            parts.append(f'{_esc(r)} calendario {_esc(s.get("fecha", "")[:10])}')
        else:
            parts.append(_esc(r))
    return f'<span style="color:#6e7781;font-size:12px"> · {"; ".join(parts)}</span>' if parts else ""


def _no_si(text: str) -> str:
    return re.sub(r"^\s*si\s+", "", text, flags=re.I)


def render(analysis: dict) -> str:
    res, src = analysis["result"], analysis["sources"]
    label = ('<p style="margin:0 0 8px;font-size:12px;color:#6e7781">Generado por IA ({model}) a partir de los '
             'datos del informe{web}. Los hechos sin fuente se descartan automáticamente. Puede contener errores; '
             'no es una recomendación.</p>').format(
        model=_esc(analysis["model"]),
        web=" y de una búsqueda en medios y organismos oficiales" if analysis.get("web_search") else "")

    def block(title: str, color: str, items: list[str]) -> str:
        if not items:
            return ""
        lis = "".join(f'<li style="margin:4px 0;font-size:14px">{i}</li>' for i in items)
        return (f'<p style="margin:10px 0 2px;font-weight:600;color:{color};font-size:14px">{title}</p>'
                f'<ul style="margin:0;padding-left:18px">{lis}</ul>')

    hechos = [_esc(h["texto"]) + _refs_html(h["fuentes"], src) for h in res["hechos"]]
    interp = [f'{_esc(i["texto"])} <span style="color:#6e7781;font-size:12px">(confianza {_esc(i["confianza"])})</span>'
              + _refs_html(i["se_apoya_en"], src) for i in res["interpretaciones"]]
    hyp = [f'<b>Si</b> {_esc(_no_si(h["si"]))}, <b>entonces</b> {_esc(h["entonces"])} '
           f'<span style="color:#6e7781;font-size:12px">Horizonte: {_esc(h["horizonte"])}. '
           f'Se invalida si: {_esc(h["invalidacion"])}.</span>' + _refs_html(h["se_apoya_en"], src)
           for h in res["hipotesis"]]
    lim = [_esc(x) for x in res.get("limitaciones", [])]
    return (label + block("Hechos: qué ha ocurrido y qué lo confirma", "#1f2328", hechos)
            + block("Interpretación: por qué podría importar", "#0550ae", interp)
            + block("Hipótesis: qué podría pasar y bajo qué condiciones", "#8250df", hyp)
            + block("Limitaciones", "#6e7781", lim))

