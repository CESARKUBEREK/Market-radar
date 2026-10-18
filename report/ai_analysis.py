"""Análisis con IA para el informe diario: Hechos, Interpretación e Hipótesis.

Tres pasos, para que la IA no pueda inventar datos ni causas:

1. Evidencia (determinista, sin IA). Se reúnen con un identificador cada uno:
   M = movimientos de mercado de la última sesión (vistas de Supabase),
   A = alertas, C = eventos del calendario (publicados y próximos),
   N = noticias oficiales guardadas, P = patrones de movimientos conjuntos
   detectados por reglas (p. ej. «suben los rendimientos y cae el oro»), con los
   temas que conviene buscar (inflación, política monetaria, dólar…).
2. Investigación (IA + búsqueda web, opcional). Claude busca noticias sobre esos
   temas en medios y organismos de la lista ALLOWED_DOMAINS. Cada fuente web que
   cite recibe un identificador W.
3. Redacción estructurada (IA, sin herramientas, salida JSON con esquema). Cada
   hecho debe citar identificadores de evidencia; el código descarta los que no
   lo hagan, así como las interpretaciones e hipótesis sin apoyo.

Reglas que se imponen en la instrucción y se comprueban donde se puede:
- Un hecho solo es lo que dicen los datos o una fuente citada.
- No se afirma que una noticia causó un movimiento salvo que una fuente lo diga
  expresamente; en ese caso se atribuye («según Reuters…»). Si no, se habla de
  coincidencia temporal.
- Las hipótesis son condicionales («si…, entonces…») e incluyen qué las invalidaría.
- Nada de recomendaciones de compra o venta.

Variables de entorno:
  ANTHROPIC_API_KEY        obligatoria para activar el análisis
  ANALYSIS_MODEL           por defecto claude-sonnet-5-5
  ANALYSIS_WEB_SEARCH      'false' para no buscar en la web (por defecto se busca)
  ANALYSIS_MAX_SEARCHES    por defecto 5
"""

from __future__ import annotations

import datetime as dt
import html
import json
import os
import time

import requests

API = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-sonnet-5-5"
ALLOWED_DOMAINS = [
    "reuters.com", "apnews.com", "bloomberg.com", "ft.com", "wsj.com", "cnbc.com", "marketwatch.com",
    "economist.com", "nikkei.com", "scmp.com", "handelsblatt.com", "lesechos.fr", "expansion.com",
    "federalreserve.gov", "ecb.europa.eu", "bankofengland.co.uk", "treasury.gov", "bls.gov", "bea.gov",
    "imf.org", "eia.gov", "opec.org", "cmegroup.com", "sec.gov", "ambito.com", "lanacion.com.ar",
]
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
        unit = "pp" if s["is_rate"] else "%"
        moves.append({
            "instrumento": s["instrument"], "instrument_id": s["instrument_id"], "categoria": s["category"],
            "fecha": s["last_date"], "ultimo": float(s["last_value"]),
            "cambio_1d": round(float(s["chg_1d"]), 4), "unidad_cambio": unit,
            "cambio_1s": None if s.get("chg_1w") is None else round(float(s["chg_1w"]), 4),
            "z_1d": None if z is None else round(z, 2), "fuente": _source_of(s["series_id"]),
        })
    moves.sort(key=lambda m: abs(m["z_1d"] or 0), reverse=True)
    top = [m for m in moves if abs(m["z_1d"] or 0) >= 1.5][:10] or moves[:6]
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
            patterns.append({"patron": desc, "temas_a_buscar": topics})
    for i, p in enumerate(patterns, 1):
        ev[f"P{i}"] = {"tipo": "patrón detectado por reglas (no es una causa)", **p}

    for i, a in enumerate((data.get("alerts") or [])[:12], 1):
        ev[f"A{i}"] = {"tipo": "alerta", "fecha": a["as_of"], "regla": a["rule_id"], "texto": a["title"]}

    since = (report_date - dt.timedelta(days=4)).isoformat()
    until = (report_date + dt.timedelta(days=7)).isoformat()
    events = [e for e in (data.get("events_window") or []) if since <= e["event_at"][:10] <= until]
    for i, e in enumerate(events[:20], 1):
        ev[f"C{i}"] = {"tipo": "evento de calendario", "fecha": e["event_at"][:16],
                       "ya_ocurrido": e["event_at"][:10] < report_date.isoformat(),
                       "clase": EVENT_LABEL.get(e["kind"], e["kind"]), "texto": e.get("notes")}

    for i, n in enumerate((data.get("news_ai") or [])[:20], 1):
        ev[f"N{i}"] = {"tipo": "noticia oficial", "fecha": n["published_at"][:16], "fuente": n["source_name"],
                       "titular": n["title"], "resumen": (n.get("summary") or "")[:300], "url": n["url"]}
    return ev


# --- llamadas a la API ------------------------------------------------------ #
def _call(payload: dict, key: str) -> dict:
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    for attempt in range(4):
        r = requests.post(API, headers=headers, data=json.dumps(payload), timeout=300)
        if r.status_code in (429, 500, 502, 503, 529):
            time.sleep(15 * 2 ** attempt)
            continue
        if not r.ok:
            raise RuntimeError(f"API de Anthropic HTTP {r.status_code}: {r.text[:300]}")
        return r.json()
    raise RuntimeError("La API de Anthropic no respondió tras 4 intentos")


RESEARCH_SYSTEM = """Eres un analista de mercados que documenta, no que opina. Recibes evidencia numerada \
(datos de mercado M, patrones P, alertas A, calendario C, noticias oficiales N) del cierre más reciente.

Tu tarea: buscar en la web noticias de los últimos 3 días que puedan estar relacionadas con los movimientos \
más fuertes y con los patrones P (usa sus «temas_a_buscar»). Prioriza agencias y organismos oficiales.

Escribe notas breves en español, una por tema, diciendo qué publican las fuentes y con qué fecha. \
Distingue siempre entre lo que una fuente AFIRMA (p. ej. «Reuters atribuye la subida a…») y una mera \
coincidencia temporal. Si no encuentras nada relevante para un tema, dilo. No inventes cifras; las cifras \
de mercado ya están en M."""

STRUCTURE_SYSTEM = """Redactas la sección «Análisis» del informe diario de Market Radar, en español, para uso \
personal. Separa estrictamente tres niveles:

HECHOS: qué ha ocurrido y qué fuentes lo confirman. Solo afirmaciones verificables en la evidencia (M, A, C, N, \
P solo como descripción de movimientos) o en fuentes web (W). Cada hecho cita sus identificadores en «fuentes». \
Cifras exactas tal como aparecen en la evidencia, con fecha. Un patrón P no es un hecho causal.

INTERPRETACIONES: por qué podría ser relevante para el mercado. Usa condicional («podría», «es coherente con»). \
Cada una se apoya en identificadores de hechos o evidencia. NO afirmes que una noticia provocó un movimiento salvo \
que una fuente W o N lo diga expresamente; entonces atribúyelo («según …»). Si varias explicaciones son posibles, \
dilo. Indica la confianza: baja, media o alta.

HIPÓTESIS: consecuencias posibles si se cumplen condiciones observables. Formato «si … entonces …», con \
horizonte temporal y qué la invalidaría. Vincúlalas a eventos del calendario (C) cuando existan.

Además, en «limitaciones», señala lo que no se ha podido confirmar o las fuentes que faltan.
Nada de recomendaciones de compra o venta. Máximo 6 hechos, 4 interpretaciones y 3 hipótesis. Frases cortas."""

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


def research(evidence: dict, model: str, key: str, max_searches: int) -> tuple[str, dict, dict]:
    """Devuelve notas con marcas [Wn], el diccionario de fuentes web y el uso de la API."""
    messages = [{"role": "user", "content": "Evidencia:\n" + json.dumps(evidence, ensure_ascii=False)}]
    payload = {"model": model, "max_tokens": 3000, "system": RESEARCH_SYSTEM, "messages": messages,
               "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": max_searches,
                          "allowed_domains": ALLOWED_DOMAINS}]}
    usage = {"input_tokens": 0, "output_tokens": 0, "web_search_requests": 0}
    content: list = []
    for _ in range(4):                       # pause_turn: se reenvía el turno pausado
        resp = _call(payload, key)
        u = resp.get("usage") or {}
        usage["input_tokens"] += u.get("input_tokens", 0)
        usage["output_tokens"] += u.get("output_tokens", 0)
        usage["web_search_requests"] += (u.get("server_tool_use") or {}).get("web_search_requests", 0)
        content = resp.get("content") or []
        if resp.get("stop_reason") != "pause_turn":
            break
        payload["messages"] = messages + [{"role": "assistant", "content": content}]
    web: dict[str, dict] = {}
    by_url: dict[str, str] = {}
    notes = []
    for block in content:
        if block.get("type") != "text":
            continue
        refs = []
        for c in block.get("citations") or []:
            url = c.get("url")
            if not url:
                continue
            if url not in by_url:
                wid = f"W{len(by_url) + 1}"
                by_url[url] = wid
                web[wid] = {"tipo": "fuente web", "titulo": c.get("title"), "url": url,
                            "cita": c.get("cited_text")}
            refs.append(by_url[url])
        notes.append(block["text"] + (f" [{', '.join(dict.fromkeys(refs))}]" if refs else ""))
    return "".join(notes).strip(), web, usage


def structure(evidence: dict, notes: str, web: dict, model: str, key: str) -> tuple[dict, dict]:
    body = {"evidencia": evidence, "fuentes_web": web, "notas_de_investigacion": notes or "(sin búsqueda web)"}
    payload = {"model": model, "max_tokens": 3000, "system": STRUCTURE_SYSTEM,
               "messages": [{"role": "user", "content": json.dumps(body, ensure_ascii=False)}],
               "output_config": {"format": {"type": "json_schema", "schema": SCHEMA}}}
    resp = _call(payload, key)
    text = "".join(b.get("text", "") for b in resp.get("content") or [] if b.get("type") == "text")
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
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return None
    model = os.environ.get("ANALYSIS_MODEL", "").strip() or DEFAULT_MODEL
    use_web = os.environ.get("ANALYSIS_WEB_SEARCH", "true").strip().lower() != "false"
    max_searches = int(os.environ.get("ANALYSIS_MAX_SEARCHES", "5") or 5)
    evidence = build_evidence(data, report_date)
    notes, web, usage = ("", {}, {"input_tokens": 0, "output_tokens": 0, "web_search_requests": 0})
    if use_web:
        notes, web, usage = research(evidence, model, key, max_searches)
    result, u2 = structure(evidence, notes, web, model, key)
    clean, dropped = validate(result, set(evidence) | set(web))
    usage["input_tokens"] += u2.get("input_tokens", 0)
    usage["output_tokens"] += u2.get("output_tokens", 0)
    return {"model": model, "result": clean, "dropped": dropped, "sources": {**evidence, **web},
            "usage": usage, "web_search": use_web}


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
            parts.append(f'{_esc(r)} {_esc(s.get("instrumento"))} ({_esc(s.get("fuente"))}, {_esc(s.get("fecha"))})')
        elif s.get("tipo") == "evento de calendario":
            parts.append(f'{_esc(r)} calendario {_esc(s.get("fecha", "")[:10])}')
        else:
            parts.append(_esc(r))
    return f'<span style="color:#6e7781;font-size:12px"> · {"; ".join(parts)}</span>' if parts else ""


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
    hyp = [f'<b>Si</b> {_esc(h["si"])}, <b>entonces</b> {_esc(h["entonces"])} '
           f'<span style="color:#6e7781;font-size:12px">Horizonte: {_esc(h["horizonte"])}. '
           f'Se invalida si: {_esc(h["invalidacion"])}.</span>' + _refs_html(h["se_apoya_en"], src)
           for h in res["hipotesis"]]
    lim = [_esc(x) for x in res.get("limitaciones", [])]
    return (label + block("Hechos: qué ha ocurrido y qué lo confirma", "#1f2328", hechos)
            + block("Interpretación: por qué podría importar", "#0550ae", interp)
            + block("Hipótesis: qué podría pasar y bajo qué condiciones", "#8250df", hyp)
            + block("Limitaciones", "#6e7781", lim))

