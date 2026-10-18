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

import market_context
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

    # Tendencia e historia de cada instrumento (T) y contexto macro (X), calculados sin IA
    closes = data.get("closes") or {}
    info = {x["instrument_id"]: x for x in summary}
    order = [i for i in market_context.CORE if i in closes] + sorted(i for i in closes if i not in market_context.CORE)
    for iid in order:
        series = closes[iid]
        if len(series) < 210:
            continue
        meta = info.get(iid, {})
        ctx = market_context.instrument_context(series, bool(meta.get("is_rate")))
        ev[f"T_{iid}"] = {"tipo": "tendencia e historia (calculado)", "instrumento": meta.get("instrument", iid),
                          "instrument_id": iid, "fuente": _source_of(meta.get("series_id", iid)), **ctx}
    macro = market_context.macro_context(data.get("macro") or {}, closes, data.get("correlations") or [])
    for k, v in macro.items():
        ev[f"X_{k}"] = {"tipo": "contexto macro (calculado)", "datos": v}

    # Situaciones excepcionales detectadas por reglas (S) y posicionamiento COT de todos los mercados
    for i, sg in enumerate((data.get("signals") or [])[:15], 1):
        ev[f"S{i}"] = {"tipo": "situación excepcional (regla fija)", "clase": sg["tipo"], "titulo": sg["titulo"],
                       "detalle": sg["detalle"], "naturaleza": sg["naturaleza"], "severidad": sg["severidad"],
                       "fuentes_del_dato": sg["fuentes"], "fecha_dato": sg["fecha_dato"], "metricas": sg["metricas"]}
    if data.get("cot_overview"):
        ev["X_posicionamiento_cot"] = {"tipo": "contexto macro (calculado)", "datos": [
            {k: o.get(k) for k in ("instrumento", "categoria", "neta_contratos", "percentil_5_años",
                                   "cambio_semanal_contratos", "neta_sobre_interes_abierto_%",
                                   "interes_abierto_percentil_5_años", "fecha_posiciones")}
            for o in data["cot_overview"]]}

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
personal. Eres un analista macro y de mercados riguroso. Separa estrictamente estos niveles:

EVIDENCIA disponible (identificadores entre corchetes, solo en las listas de fuentes):
- M…: movimientos de la última sesión. T_<instrumento>: tendencia e historia desde 2020 calculadas sin IA \
(variaciones a 1 semana-12 meses, distancia a las medias de 50 y 200 sesiones, último cruce de medias, \
tendencia_calculada, máximos y mínimos con fecha, percentil del nivel desde 2020, régimen de volatilidad, rachas). \
X_…: contexto macro (liquidez neta de la Fed, M2, fondos federales, NFCI, curva 2-10 años, correlaciones). \
A: alertas. C: calendario. N: noticias oficiales. P: patrones de movimientos conjuntos detectados por reglas. \
S…: situaciones excepcionales detectadas por reglas fijas (A posicionamiento extremo, B posible liquidación, \
C exceso de apalancamiento, D flujos de ETF, E movimiento anómalo). X_posicionamiento_cot: posicionamiento COT \
(neta y percentil de 5 años) de todos los mercados.

SITUACIONES EXCEPCIONALES: una entrada por cada S, citando su identificador. Explica qué significa y qué NO \
significa (p. ej. un posicionamiento extremo no anticipa la dirección del precio; indica sensibilidad a noticias). \
Respeta su «naturaleza»: «confirmado por fuente» se puede afirmar; «compatible con» solo se describe como \
compatible, nunca como confirmado. Relaciónala con la tendencia (T) y el macro (X) si procede. El COT refleja \
posiciones del martes publicadas el viernes: no describe operaciones en tiempo real. Los flujos de ETF son \
creaciones y reembolsos de acciones, no volumen negociado.

PANORAMA: una entrada por cada instrumento de la lista obligatoria que recibes, citando su T_<instrumento>. \
En 1 o 2 frases: dirección y fuerza de la tendencia (usa la tendencia_calculada, no la contradigas), dónde está el \
nivel en perspectiva histórica (percentil desde 2020, distancia al máximo o mínimo, con fechas), momento reciente \
(1 y 3 meses) y volatilidad si es anómala. Para tipos, en puntos básicos.

HECHOS: lo más relevante de la sesión y del contexto, solo afirmaciones verificables en la evidencia, con cifras \
y fechas. Incluye comparaciones históricas (p. ej. «el oro está en el percentil 98 desde 2020»). Cada hecho cita \
sus identificadores. Un patrón P no es un hecho causal.

INTERPRETACIONES: por qué podría ser relevante. Condicional («podría», «es coherente con»). Cruza activos y macro: \
rendimientos y dólar frente a oro y bitcoin, petróleo e inflación, curva y ciclo, liquidez y activos de riesgo, \
correlaciones que se salen de su rango. NO afirmes que una noticia provocó un movimiento salvo que la propia \
noticia N lo diga; si no, habla de coincidencia temporal. Si hay varias explicaciones, dilo. Confianza: baja, \
media o alta.

HIPÓTESIS: consecuencias posibles si se cumplen condiciones observables y medibles (niveles, medias, datos del \
calendario). Formato condición → consecuencia, con horizonte y qué la invalidaría.

LIMITACIONES: lo que la evidencia no permite confirmar.

FUENTES: cada elemento de «fuentes» y «se_apoya_en» es un identificador EXACTO de la evidencia, tal cual \
aparece como clave («M3», «T_gold», «X_m2», «S1», «C2»…). Nunca lo dejes vacío. Lo que no puedas apoyar en un \
identificador, no lo escribas.

Nada de recomendaciones de compra o venta. Máximo 8 hechos, 5 interpretaciones y 4 hipótesis; cada lectura \
de situación o de panorama, 2 frases como máximo.

Estilo: frases cortas; números en formato español (coma decimal, punto de miles), 2 decimales como máximo. \
Los NIVELES de tipos (US10Y, US02Y, fondos federales) van en % con 2 decimales («5,10 %»); sus VARIACIONES y \
distancias, en puntos básicos sin decimales («−12 pb»). La liquidez y M2, en miles de millones de dólares \
(«5.872 mm $»), sin decimales. Fechas como «16/10» o «mar. 2022». No escribas nombres de campos ni identificadores dentro del texto. \
En las hipótesis, el campo «si» empieza directamente por la condición, sin la palabra «si»."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["situaciones", "panorama", "hechos", "interpretaciones", "hipotesis", "limitaciones"],
    "properties": {
        "situaciones": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["senal", "lectura", "fuentes"],
            "properties": {"senal": {"type": "string"}, "lectura": {"type": "string"},
                           "fuentes": {"type": "array", "items": {"type": "string"}}}}},
        "panorama": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["instrument_id", "lectura", "fuentes"],
            "properties": {"instrument_id": {"type": "string"}, "lectura": {"type": "string"},
                           "fuentes": {"type": "array", "items": {"type": "string"}}}}},
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
               "input": json.dumps({"instrumentos_obligatorios_en_panorama":
                                    [k[2:] for k in evidence if k.startswith("T_") and k[2:] in market_context.CORE],
                                    "evidencia": evidence}, ensure_ascii=False),
               "text": {"format": {"type": "json_schema", "name": "analisis_diario", "strict": True,
                                   "schema": SCHEMA}},
               "max_output_tokens": 32000}
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


def _fallback_reading(t: dict) -> str:
    """Lectura mínima generada por código cuando la IA omite un instrumento obligatorio."""
    u = t.get("unidad_variaciones", "%")
    return (f"Tendencia {t['tendencia_calculada']}; {t.get(f'variacion_3_meses_{u}')} {u} en 3 meses, "
            f"{t.get(f'distancia_a_media_200_{u}')} {u} frente a la media de 200 sesiones; percentil "
            f"{t['percentil_del_nivel_desde_2020']} desde 2020 (texto automático).")


def validate(result: dict, known: set[str], evidence: dict | None = None) -> tuple[dict, list[str]]:
    """Descarta lo que no cita evidencia existente. Devuelve el resultado limpio y lo descartado."""
    dropped = []
    evidence = evidence or {}

    def ok_refs(refs: list[str]) -> list[str]:
        """Acepta identificadores exactos o rodeados de texto («[M3]», «M3 (S&P 500)»)."""
        out = []
        for r in refs or []:
            r = str(r).strip()
            if r in known:
                out.append(r)
                continue
            for tok in re.findall(r"[A-Z](?:_[A-Za-z0-9_áéíóúñ\-]+|\d+)", r):
                if tok in known:
                    out.append(tok)
        return list(dict.fromkeys(out))

    # Panorama: una entrada por instrumento obligatorio, con su T_ y la tendencia calculada por código
    panorama, seen = [], set()
    for p in result.get("panorama", []):
        iid = p.get("instrument_id", "")
        tid = f"T_{iid}"
        if tid not in evidence or iid in seen:
            dropped.append(f"panorama sin datos: {iid}")
            continue
        seen.add(iid)
        refs = ok_refs(p.get("fuentes", [])) or [tid]
        if tid not in refs:
            refs.insert(0, tid)
        panorama.append({"instrument_id": iid, "lectura": p.get("lectura", ""), "fuentes": refs})
    for k, t in evidence.items():
        if k.startswith("T_") and t["instrument_id"] in market_context.CORE and t["instrument_id"] not in seen:
            panorama.append({"instrument_id": t["instrument_id"], "lectura": _fallback_reading(t), "fuentes": [k]})
            dropped.append(f"panorama completado por código: {t['instrument_id']}")
    situaciones, seen_s = [], set()
    for x in result.get("situaciones", []):
        sid = x.get("senal", "")
        if sid not in evidence or not sid.startswith("S") or sid in seen_s:
            dropped.append(f"situación sin señal válida: {sid}")
            continue
        seen_s.add(sid)
        refs = ok_refs(x.get("fuentes", [])) or [sid]
        if sid not in refs:
            refs.insert(0, sid)
        situaciones.append({"senal": sid, "lectura": x.get("lectura", ""), "fuentes": refs})
    for k, v in evidence.items():
        if k.startswith("S") and k[1:].isdigit() and k not in seen_s:
            situaciones.append({"senal": k, "lectura": v["detalle"], "fuentes": [k]})
            dropped.append(f"situación completada por código: {k}")
    situaciones.sort(key=lambda x: int(x["senal"][1:]))
    rank = {iid: i for i, iid in enumerate(market_context.CORE)}
    panorama.sort(key=lambda p: rank.get(p["instrument_id"], 99))

    hechos = []
    for h in result.get("hechos", []):
        refs = ok_refs(h.get("fuentes", []))
        if refs:
            hechos.append({**h, "fuentes": refs})
        else:
            dropped.append(f"hecho sin fuente: {h.get('texto', '')[:60]} (citaba {h.get('fuentes')})")
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
    return {"situaciones": situaciones, "panorama": panorama, "hechos": hechos, "interpretaciones": interp, "hipotesis": hyp,
            "limitaciones": result.get("limitaciones", [])}, dropped


def run(data: dict, report_date: dt.date) -> dict | None:
    """Análisis completo, o None si no hay clave. Los errores se propagan al llamador."""
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        return None
    model = os.environ.get("ANALYSIS_MODEL", "").strip() or DEFAULT_MODEL
    effort = os.environ.get("ANALYSIS_EFFORT", "").strip() or "medium"
    evidence = build_evidence(data, report_date)
    result, usage = structure(evidence, model, key, effort)
    clean, dropped = validate(result, set(evidence), evidence)
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
        elif s.get("tipo") == "tendencia e historia (calculado)":
            parts.append(f'histórico {_esc(s.get("instrumento"))} ({_esc(s.get("fuente"))})')
        elif s.get("tipo") == "situación excepcional (regla fija)":
            parts.append(f'{_esc(r)} {", ".join(_esc(f) for f in s.get("fuentes_del_dato", []))}')
        elif s.get("tipo") == "contexto macro (calculado)":
            parts.append(f'macro {_esc(r[2:].replace("_", " "))}')
        elif s.get("tipo") == "evento de calendario":
            parts.append(f'{_esc(r)} calendario {_esc(s.get("fecha", "")[:10])}')
        else:
            parts.append(_esc(r))
    return f'<span style="color:#6e7781;font-size:12px"> · {"; ".join(parts)}</span>' if parts else ""


def _no_si(text: str) -> str:
    t = re.sub(r"^\s*si\s+", "", text, flags=re.I)
    # minúscula inicial salvo siglas («El DXY» → «el DXY», «US10Y» se queda igual)
    return t[0].lower() + t[1:] if len(t) > 1 and t[0].isupper() and not t[1].isupper() else t


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
    colors = {"alcista": "#1a7f37", "bajista": "#cf222e"}
    rows = []
    for p in res.get("panorama", []):
        t = src.get(f"T_{p['instrument_id']}", {})
        u = t.get("unidad_variaciones", "%")
        trend = t.get("tendencia_calculada", "")

        def fmt(x):
            return "—" if x is None else f"{x:+.1f}".replace(".", ",") + (" pb" if u == "pb" else " %")
        key = (f"3 m: {fmt(t.get(f'variacion_3_meses_{u}'))} · MM200: {fmt(t.get(f'distancia_a_media_200_{u}'))}"
               f" · pct. {t.get('percentil_del_nivel_desde_2020', '—')}")
        rows.append(f'<tr><td style="padding:4px 6px;font-size:13px;border-bottom:1px solid #eaeef2;white-space:nowrap">'
                    f'<b>{_esc(t.get("instrumento", p["instrument_id"]))}</b><br>'
                    f'<span style="color:{colors.get(trend, "#6e7781")};font-size:12px">{_esc(trend)}</span></td>'
                    f'<td style="padding:4px 6px;font-size:13px;border-bottom:1px solid #eaeef2">{_esc(p["lectura"])}'
                    f'<br><span style="color:#6e7781;font-size:11px">{_esc(key)}</span></td></tr>')
    panorama_html = ""
    if rows:
        panorama_html = ('<p style="margin:10px 0 2px;font-weight:600;color:#1f2328;font-size:14px">Panorama: '
                         'tendencia y contexto histórico (desde 2020)</p>'
                         '<p style="margin:0 0 4px;font-size:11px;color:#6e7781">Tendencia calculada por regla fija: '
                         'alcista si el precio y la media de 50 sesiones están por encima de la de 200 y la de 50 '
                         'sube; bajista al revés. «pct.» = percentil del nivel actual desde 2020.</p>'
                         f'<table style="border-collapse:collapse;width:100%">{"".join(rows)}</table>')
    interp = [f'{_esc(i["texto"])} <span style="color:#6e7781;font-size:12px">(confianza {_esc(i["confianza"])})</span>'
              + _refs_html(i["se_apoya_en"], src) for i in res["interpretaciones"]]
    hyp = [f'<b>Si</b> {_esc(_no_si(h["si"]))}, <b>entonces</b> {_esc(h["entonces"])} '
           f'<span style="color:#6e7781;font-size:12px">Horizonte: {_esc(h["horizonte"])}. '
           f'Se invalida si: {_esc(h["invalidacion"])}.</span>' + _refs_html(h["se_apoya_en"], src)
           for h in res["hipotesis"]]
    lim = [_esc(x) for x in res.get("limitaciones", [])]
    sit = []
    for x in res.get("situaciones", []):
        sg = src.get(x["senal"], {})
        color = "#cf222e" if sg.get("severidad") == "alta" else "#9a6700"
        sit.append(f'<b style="color:{color}">{_esc(sg.get("titulo", ""))}</b> '
                   f'<span style="color:#6e7781;font-size:12px">({_esc(sg.get("naturaleza", ""))})</span><br>'
                   f'{_esc(x["lectura"])}' + _refs_html([r for r in x["fuentes"] if r != x["senal"]], src))
    return (label + block("Situaciones excepcionales: qué significan", "#cf222e", sit)
            + block("Hechos: qué ha ocurrido y qué lo confirma", "#1f2328", hechos) + panorama_html
            + block("Interpretación: por qué podría importar", "#0550ae", interp)
            + block("Hipótesis: qué podría pasar y bajo qué condiciones", "#8250df", hyp)
            + block("Limitaciones", "#6e7781", lim))

