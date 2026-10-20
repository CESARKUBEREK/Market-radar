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


SERIES_SOURCE: dict[str, str] = {}      # id de serie → fuente (se rellena desde la tabla series)


def _source_of(series_id: str) -> str:
    src = SERIES_SOURCE.get(series_id) or series_id.split("_", 1)[0]
    return SOURCE_LABEL.get(src, src)


def build_evidence(data: dict, report_date: dt.date) -> dict:
    SERIES_SOURCE.update(data.get("series_sources") or {})
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

    # Fechas de cierre: los activos con datos de días anteriores se señalan (FRED publica con un día de retraso)
    # Bitcoin cotiza también en fin de semana: no sirve de referencia para el resto.
    dates = {x["instrument"]: x["last_date"] for x in summary
             if x["instrument_id"] in market_context.CORE and x["instrument_id"] != "btc"}
    if dates:
        latest = max(dates.values())
        lag = {k: v for k, v in dates.items() if v < latest}
        ev["X_fechas_de_cierre"] = {"tipo": "contexto macro (calculado)", "datos": {
            "cierre_mas_reciente": latest, "activos_con_cierre_anterior": lag}}

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

    to, out = data.get("flow_lines") or ([], [])
    if to or out:
        ev["X_flujos"] = {"tipo": "contexto macro (calculado por reglas: COT a 4 semanas, ETF a 20 sesiones, "
                                  "oro de bancos centrales)", "datos": {"entra": to, "sale": out}}

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


STRUCTURE_SYSTEM = """Redactas la sección «Análisis» del informe diario de Market Radar, en español, para un \
lector experto que lo lee en 3 minutos. Eres un analista macro y de mercados riguroso y sobrio.

EVIDENCIA (identificadores solo en las listas de fuentes, nunca en el texto):
- M…: movimientos de la última sesión de cada activo (variación del día y de la semana, desviaciones típicas).
- T_<instrumento>: tendencia e historia desde 2020 calculadas sin IA (variaciones, niveles y distancias a las \
medias de 50 y 200 sesiones, último cruce, tendencia_calculada, máximos y mínimos con fecha, percentil desde 2020, \
volatilidad, rachas).
- X_…: macro (liquidez neta de la Fed, M2, fondos federales, NFCI, curva 2-10, diferencial Brent-WTI, \
correlaciones, posicionamiento COT de todos los mercados).
- S…: situaciones excepcionales detectadas por reglas fijas (descriptivas, no validadas).
- A: alertas. C: calendario. N: noticias oficiales (solo titular). P: patrones de co-movimiento (no son causas).

QUÉ ESCRIBIR, en este orden de importancia:
1. LO ESENCIAL: un «titular» de una frase con la idea dominante del día y hasta 3 «puntos» que la sostengan. \
Prioriza lo que cambia la lectura del mercado: movimientos inusuales, rupturas de tendencia, extremos \
históricos, situaciones S, tensiones entre activos. No describas lo que se movió poco.
   Añade dos comentarios ejecutivos de una o dos frases (40 palabras como máximo cada uno):
   - «excepcional»: qué situaciones S destacan hoy y qué sugieren en conjunto, respetando su naturaleza \
(no validadas). Si no hay ninguna S, texto «Ninguna hoy.» y fuentes vacías.
   - «dinero»: hacia dónde entra y de dónde sale el dinero según X_flujos (y X_posicionamiento_cot), nombrando \
activos y quién se mueve; di si el movimiento acompaña o contradice al precio. Si no hay X_flujos, texto \
«Sin movimientos claros.» y fuentes vacías.
2. HECHOS (máx. 6): qué ha pasado en la sesión y en la semana, con cifras y fechas. NO repitas el contexto \
histórico de cada activo: eso ya lo muestra el panorama. Si un hecho compara activos, que estén en la evidencia.
3. INTERPRETACIONES (máx. 4): por qué podría importar, en condicional. Cruza activos y macro. No atribuyas un \
movimiento a una noticia salvo que la noticia lo diga. Si hay explicaciones alternativas, dilas. Confianza: \
«alta» solo si al menos dos datos distintos la apoyan y nada la contradice; nunca si se apoya en patrones P o \
situaciones S (no validadas). Si no, «media» o «baja».
4. HIPÓTESIS (máx. 3): condición observable → consecuencia, con horizonte e invalidación. Los niveles de la \
condición y de la invalidación deben salir de la evidencia (último, máximos, mínimos, niveles de las medias); la \
invalidación no puede solaparse con la condición. Cada hipótesis se evaluará automáticamente después, así que \
rellena también su versión medible: «condicion_activo» (identificador del instrumento, p. ej. «us10y»), \
«condicion_operador» («>» o «<»), «condicion_nivel» (en las unidades del nivel del activo; tipos en %), \
«consecuencia_activo» y «consecuencia_direccion» («sube» o «baja»: lo que debería hacer ese activo si la \
hipótesis es correcta) y «horizonte_sesiones» (entre 1 y 20). Una hipótesis sin consecuencia medible no sirve. \
Los campos «si», «entonces» e «invalidacion» se escriben en español natural y con formato español («el US10Y \
supera el 5,31 %»), nunca como fórmula («us10y > 5.31»): la versión medible ya va en sus propios campos. \
Prefiere hipótesis que relacionen activos distintos (p. ej. tipos → bolsa, dólar → oro).
5. A VIGILAR (máx. 3): eventos del calendario C o niveles concretos de la evidencia para las próximas sesiones.
6. SITUACIONES: una lectura por cada S («senal» = solo su identificador): qué significa y qué no. Respeta su \
naturaleza («compatible con» nunca es «confirmado»). El COT es del martes, publicado el viernes.
7. PANORAMA: una entrada por cada instrumento obligatorio, citando su T_. Una sola frase de 25 palabras como \
máximo con la LECTURA (fuerza de la tendencia, si está extendida o cerca de un nivel clave, si diverge de su \
grupo). No repitas las cifras de 1 mes, 3 meses, media de 200 ni percentil: el informe ya las muestra al lado.
8. LIMITACIONES (máx. 2): solo las específicas de hoy.

REGLAS DE CIFRAS (se comprueban automáticamente; lo que no cuadre con la evidencia citada se elimina):
- Copia las cifras de la evidencia tal cual, redondeadas a 2 decimales como máximo. No calcules cifras nuevas \
(diferencias, sumas, ratios) salvo que ya estén en la evidencia.
- Puntos básicos (pb) SOLO para tipos de interés (US10Y, US02Y, fondos federales, curva). Para el resto de \
activos, variaciones y distancias en %. Los niveles de tipos en % («5,10 %»).
- Cada activo con su fecha de cierre: si no coinciden entre activos, dilo («último cierre disponible, 15/10»).
- Liquidez y M2 en miles de millones de dólares («5.872 mm $»).

Estilo: frases cortas y concretas, sin muletillas («cabe destacar», «en este contexto») ni anglicismos \
innecesarios (escribe «cierre de cortos», «grandes tecnológicas», «activos cíclicos», no «squeeze», «mega caps», \
«beta»). Formato español \
(coma decimal, punto de miles). Fechas «16/10». Nada de recomendaciones de compra o venta. En hipótesis, el campo \
«si» empieza directamente por la condición."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["titular", "puntos", "excepcional", "dinero", "situaciones", "panorama", "hechos",
                 "interpretaciones", "hipotesis", "a_vigilar", "limitaciones"],
    "properties": {
        "titular": {"type": "string"},
        "excepcional": {"type": "object", "additionalProperties": False, "required": ["texto", "fuentes"],
                        "properties": {"texto": {"type": "string"},
                                       "fuentes": {"type": "array", "items": {"type": "string"}}}},
        "dinero": {"type": "object", "additionalProperties": False, "required": ["texto", "fuentes"],
                   "properties": {"texto": {"type": "string"},
                                  "fuentes": {"type": "array", "items": {"type": "string"}}}},
        "puntos": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["texto", "fuentes"],
            "properties": {"texto": {"type": "string"}, "fuentes": {"type": "array", "items": {"type": "string"}}}}},
        "a_vigilar": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["texto", "fuentes"],
            "properties": {"texto": {"type": "string"}, "fuentes": {"type": "array", "items": {"type": "string"}}}}},
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
            "required": ["si", "entonces", "horizonte", "invalidacion", "se_apoya_en", "condicion_activo",
                         "condicion_operador", "condicion_nivel", "consecuencia_activo", "consecuencia_direccion",
                         "horizonte_sesiones"],
            "properties": {"si": {"type": "string"}, "entonces": {"type": "string"},
                           "horizonte": {"type": "string"}, "invalidacion": {"type": "string"},
                           "se_apoya_en": {"type": "array", "items": {"type": "string"}},
                           "condicion_activo": {"type": "string"},
                           "condicion_operador": {"type": "string", "enum": [">", "<"]},
                           "condicion_nivel": {"type": "number"},
                           "consecuencia_activo": {"type": "string"},
                           "consecuencia_direccion": {"type": "string", "enum": ["sube", "baja"]},
                           "horizonte_sesiones": {"type": "integer"}}}},
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


# --- verificación de cifras ---------------------------------------------------- #
# Expresiones que contienen números pero no son cifras de mercado (nombres, fechas, plazos).
_NOT_FIGURES = [
    r"\bUS\d+Y\b", r"\b\d+-\d+\b", r"\bS&P\s*500\b", r"Nasdaq-100", r"Russell\s*2000", r"TA-35", r"\bMM\d+\b",
    r"\bM2\b", r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b", r"\b(?:19|20)\d{2}\b",
    r"\b\d+\s*(?:sesiones|sesión|semanas?|meses|mes|años?|días?|horas?|veces|contratos? de)\b",
    r"medias? de \d+(?: y \d+)?", r"\b\d+\s*(?:y|o)\s*\d+\s*(?:sesiones|meses|semanas)",
    r"\bpercentil(?:es)?\s+\d+\s+de\s+\d+\s+años", r"\b(?:primer|segund|tercer)\w*\b",
]
_NUM = re.compile(r"[-−+]?\d{1,3}(?:\.\d{3})+(?:,\d+)?|[-−+]?\d+(?:,\d+)?")


def _figures(text: str) -> list[float]:
    t = text
    for pat in _NOT_FIGURES:
        t = re.sub(pat, " ", t, flags=re.I)
    out = []
    for m in _NUM.findall(t):
        try:
            out.append(abs(float(m.replace("−", "-").replace(".", "").replace(",", "."))))
        except ValueError:
            pass
    return out


def _numbers_in(obj, acc: set[float]) -> set[float]:
    if isinstance(obj, bool):
        return acc
    if isinstance(obj, (int, float)):
        acc.add(abs(float(obj)))
    elif isinstance(obj, dict):
        for v in obj.values():
            _numbers_in(v, acc)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _numbers_in(v, acc)
    elif isinstance(obj, str):
        for x in _figures(obj):
            acc.add(x)
    return acc


def _matches(x: float, allowed: set[float]) -> bool:
    for v in allowed:
        if abs(x - v) <= max(0.0051, abs(v) * 0.003):
            return True
        for d in (0, 1, 2):            # redondeos admitidos
            if round(v, d) == x:
                return True
        if v >= 1e6 and abs(x * 1e6 - v) <= v * 0.01:     # «4,2 millones»
            return True
        if v >= 1e9 and abs(x * 1e9 - v) <= v * 0.01:
            return True
    return False


def unverified(text: str, refs: list[str], evidence: dict) -> list[float]:
    """Cifras del texto que no aparecen en la evidencia citada (ni en sus redondeos)."""
    allowed: set[float] = set()
    for r in refs:
        _numbers_in(evidence.get(r, {}), allowed)
    return [x for x in _figures(text) if x > 0 and not _matches(x, allowed)]


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
        bad = unverified(p.get("lectura", ""), refs, evidence)
        if bad:
            dropped.append(f"panorama {iid}: cifras no verificadas {bad}; se usa texto automático")
            panorama.append({"instrument_id": iid, "lectura": _fallback_reading(evidence[tid]), "fuentes": [tid]})
            continue
        panorama.append({"instrument_id": iid, "lectura": p.get("lectura", ""), "fuentes": refs})
    for k, t in evidence.items():
        if k.startswith("T_") and t["instrument_id"] in market_context.CORE and t["instrument_id"] not in seen:
            panorama.append({"instrument_id": t["instrument_id"], "lectura": _fallback_reading(t), "fuentes": [k]})
            dropped.append(f"panorama completado por código: {t['instrument_id']}")
    situaciones, seen_s = [], set()
    for x in result.get("situaciones", []):
        sid = x.get("senal", "")
        if sid not in evidence:          # la IA a veces pone el texto: se busca el identificador S en sus campos
            cand = [t for t in re.findall(r"\bS\d+\b", f"{sid} {' '.join(map(str, x.get('fuentes', [])))}")
                    if t in evidence and t not in seen_s]
            sid = cand[0] if cand else sid
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

    def checked(items: list[dict], label: str) -> list[dict]:
        out = []
        for h in items:
            refs = ok_refs(h.get("fuentes", []))
            if not refs:
                dropped.append(f"{label} sin fuente: {h.get('texto', '')[:60]} (citaba {h.get('fuentes')})")
                continue
            bad = unverified(h.get("texto", ""), refs, evidence)
            if bad:
                dropped.append(f"{label} con cifras no verificadas {bad}: {h.get('texto', '')[:70]}")
                continue
            out.append({**h, "fuentes": refs})
        return out

    hechos = checked(result.get("hechos", []), "hecho")
    puntos = checked(result.get("puntos", []), "punto esencial")
    vigilar = checked(result.get("a_vigilar", []), "a vigilar")
    interp = []
    for it in result.get("interpretaciones", []):
        refs = ok_refs(it.get("se_apoya_en", []))
        if refs:
            # Lo que descansa en patrones o en señales no validadas no puede tener confianza alta
            if it.get("confianza") == "alta" and any(r[0] in "PS" and r[1:].isdigit() for r in refs):
                it = {**it, "confianza": "media"}
                dropped.append(f"confianza rebajada a media (se apoya en patrones o señales no validadas): "
                               f"{it.get('texto', '')[:60]}")
            interp.append({**it, "se_apoya_en": refs})
        else:
            dropped.append(f"interpretación sin apoyo: {it.get('texto', '')[:80]}")
    hyp = []
    for hp in result.get("hipotesis", []):
        refs = ok_refs(hp.get("se_apoya_en", []))
        if not refs:
            dropped.append(f"hipótesis sin apoyo: {hp.get('si', '')[:80]}")
            continue
        bad = unverified(f"{hp.get('si', '')} {hp.get('invalidacion', '')}", refs, evidence)
        if bad:
            dropped.append(f"hipótesis con niveles que no están en la evidencia {bad}: {hp.get('si', '')[:70]}")
            continue
        ids = {k[2:] for k in evidence if k.startswith("T_")}
        if hp.get("condicion_activo") not in ids or hp.get("consecuencia_activo") not in ids:
            dropped.append(f"hipótesis con activos no reconocidos ({hp.get('condicion_activo')}, "
                           f"{hp.get('consecuencia_activo')}): {hp.get('si', '')[:60]}")
            continue
        if unverified(str(hp.get("condicion_nivel")).replace(".", ","), [f"T_{hp['condicion_activo']}"], evidence):
            dropped.append(f"hipótesis con nivel medible {hp.get('condicion_nivel')} que no está en la evidencia de "
                           f"{hp['condicion_activo']}")
            continue
        hp = {**hp, "horizonte_sesiones": max(1, min(20, int(hp.get("horizonte_sesiones") or 5)))}
        hyp.append({**hp, "se_apoya_en": refs})
    def comment(key: str) -> dict | None:
        """Comentario ejecutivo: sin fuente solo vale si no lleva cifras («Ninguna hoy.»)."""
        c = result.get(key) or {}
        text = (c.get("texto") or "").strip()
        if not text:
            return None
        refs = ok_refs(c.get("fuentes", []))
        bad = unverified(text, refs, evidence) if refs else [x for x in _figures(text) if x > 0]
        if bad:
            dropped.append(f"comentario «{key}» con cifras no verificadas {bad}: {text[:70]}")
            return None
        return {"texto": text, "fuentes": refs}

    titular = result.get("titular", "").strip()
    if titular and unverified(titular, list(evidence), evidence):
        dropped.append(f"titular con cifras no verificadas: {titular[:80]}")
        titular = re.sub(r"\s*\([^)]*\d[^)]*\)", "", titular)      # se conserva la idea sin la cifra entre paréntesis
        if unverified(titular, list(evidence), evidence):
            titular = ""
    return {"titular": titular, "puntos": puntos[:3],
            "excepcional": comment("excepcional"), "dinero": comment("dinero"), "situaciones": situaciones, "panorama": panorama,
            "hechos": hechos, "interpretaciones": interp, "hipotesis": hyp, "a_vigilar": vigilar[:3],
            "limitaciones": result.get("limitaciones", [])[:2]}, dropped


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


X_PROVIDER = {"liquidez_neta_fed": "FRED", "m2": "FRED", "fondos_federales": "FRED",
              "condiciones_financieras_nfci": "FRED", "curva_2_10_años_pb": "FRED",
              "diferencial_brent_wti_usd": "FRED", "posicionamiento_cot": "CFTC", "correlaciones": "cálculo propio",
              "fechas_de_cierre": "cálculo propio"}


def _refs_html(refs: list[str], sources: dict) -> str:
    """Fuentes compactas: proveedores únicos; las noticias, enlazadas."""
    names: list[str] = []
    for r in refs:
        s = sources.get(r, {})
        tipo = s.get("tipo", "")
        if s.get("url"):
            names.append(f'<a href="{_esc(s["url"])}" style="color:#6e7781">{_esc(s.get("fuente") or "noticia")}</a>')
        elif tipo in ("dato de mercado", "tendencia e historia (calculado)"):
            names.append(_esc(s.get("fuente")))
        elif tipo == "situación excepcional (regla fija)":
            names += [_esc(f) for f in s.get("fuentes_del_dato", [])]
        elif tipo == "contexto macro (calculado)":
            names.append(X_PROVIDER.get(r[2:], "cálculo propio"))
        elif tipo == "evento de calendario":
            names.append("calendario")
        elif tipo == "alerta":
            names.append("alertas")
    names = list(dict.fromkeys(n for n in names if n))
    return f'<span style="color:#8c959f;font-size:11px"> — {", ".join(names)}</span>' if names else ""


def signal_readings(analysis: dict | None) -> dict[int, str]:
    """Lectura de la IA por índice de señal (S1 → 0), para mostrarla dentro de la tabla de situaciones."""
    if not analysis:
        return {}
    return {int(x["senal"][1:]) - 1: x["lectura"] for x in analysis["result"].get("situaciones", [])
            if x["senal"][1:].isdigit()}


def _no_si(text: str) -> str:
    t = re.sub(r"^\s*si\s+", "", text, flags=re.I)
    # minúscula inicial salvo siglas («El DXY» → «el DXY», «US10Y» se queda igual)
    return t[0].lower() + t[1:] if len(t) > 1 and t[0].isupper() and not t[1].isupper() else t


def render(analysis: dict) -> str:
    res, src = analysis["result"], analysis["sources"]
    grey = "#6e7781"

    def h3(title: str, color: str = "#1f2328") -> str:
        return f'<p style="margin:14px 0 4px;font-weight:600;color:{color};font-size:14px">{title}</p>'

    def ul(items: list[str]) -> str:
        return ('<ul style="margin:0;padding-left:18px">'
                + "".join(f'<li style="margin:3px 0;font-size:14px;line-height:1.4">{i}</li>' for i in items) + "</ul>")

    out = []
    # 1. Lo esencial
    if res.get("titular") or res.get("puntos"):
        box = ""
        if res.get("titular"):
            box += f'<p style="margin:0 0 6px;font-size:15px;font-weight:600">{_esc(res["titular"])}</p>'
        if res.get("puntos"):
            box += ul([_esc(p["texto"]) + _refs_html(p["fuentes"], src) for p in res["puntos"]])
        out.append(f'<div style="background:#f6f8fa;border-left:3px solid #0969da;padding:10px 12px;margin:4px 0 6px">'
                   f'{box}</div>')
    # 2. Qué ha pasado
    if res["hechos"]:
        out.append(h3("Qué ha pasado") + ul([_esc(h["texto"]) + _refs_html(h["fuentes"], src) for h in res["hechos"]]))
    # 3. Interpretación
    conf_color = {"alta": "#1a7f37", "media": "#9a6700", "baja": grey}
    if res["interpretaciones"]:
        out.append(h3("Por qué podría importar", "#0550ae") + ul([
            f'<span style="font-size:11px;color:{conf_color.get(i["confianza"], grey)};font-weight:600">'
            f'[confianza {_esc(i["confianza"])}]</span> {_esc(i["texto"])}' + _refs_html(i["se_apoya_en"], src)
            for i in res["interpretaciones"]]))
    # 4. Hipótesis
    if res["hipotesis"]:
        out.append(h3("Hipótesis: qué podría pasar y bajo qué condiciones", "#8250df") + ul([
            f'<b>Si</b> {_esc(_no_si(h["si"]))} → {_esc(h["entonces"])}<br>'
            f'<span style="color:{grey};font-size:12px">Horizonte: {_esc(h["horizonte"])} · '
            f'se invalida si {_esc(_no_si(h["invalidacion"]))}</span>' + _refs_html(h["se_apoya_en"], src)
            for h in res["hipotesis"]]))
    # 5. A vigilar
    if res.get("a_vigilar"):
        out.append(h3("A vigilar") + ul([_esc(v["texto"]) + _refs_html(v["fuentes"], src) for v in res["a_vigilar"]]))
    # 6. Panorama (referencia)
    colors = {"alcista": "#1a7f37", "bajista": "#cf222e"}
    rows = []
    for p in res.get("panorama", []):
        t = src.get(f"T_{p['instrument_id']}", {})
        u = t.get("unidad_variaciones", "%")
        trend = t.get("tendencia_calculada", "")

        def f(x, dec=1):
            if x is None:
                return "—"
            v = f"{x:+.{0 if u == 'pb' else dec}f}".replace(".", ",")
            return v + (" pb" if u == "pb" else " %")
        cell = 'style="padding:5px 6px;font-size:12px;border-bottom:1px solid #eaeef2;text-align:right;white-space:nowrap"'
        rows.append(
            f'<tr><td style="padding:5px 6px;font-size:13px;border-bottom:1px solid #eaeef2;white-space:nowrap">'
            f'<b>{_esc(t.get("instrumento", p["instrument_id"]))}</b><br>'
            f'<span style="color:{colors.get(trend, grey)};font-size:11px">{_esc(trend)}</span></td>'
            f'<td {cell}>{f(t.get(f"variacion_1_mes_{u}"))}</td><td {cell}>{f(t.get(f"variacion_3_meses_{u}"))}</td>'
            f'<td {cell}>{f(t.get(f"distancia_a_media_200_{u}"))}</td>'
            f'<td {cell}>{t.get("percentil_del_nivel_desde_2020", "—")}</td>'
            f'<td style="padding:5px 6px;font-size:12px;border-bottom:1px solid #eaeef2;color:#424a53">'
            f'{_esc(p["lectura"])}</td></tr>')
    if rows:
        th = 'style="padding:4px 6px;font-size:11px;color:#6e7781;text-align:right;border-bottom:1px solid #d0d7de"'
        out.append(h3("Panorama: tendencia y posición histórica")
                   + '<table style="border-collapse:collapse;width:100%"><thead><tr>'
                   f'<th style="padding:4px 6px;font-size:11px;color:#6e7781;text-align:left;border-bottom:1px solid #d0d7de">Activo</th>'
                   f'<th {th}>1 mes</th><th {th}>3 meses</th><th {th}>vs. MM200</th><th {th}>Pct. 2020</th>'
                   f'<th style="padding:4px 6px;font-size:11px;color:#6e7781;text-align:left;border-bottom:1px solid #d0d7de">Lectura</th>'
                   f'</tr></thead><tbody>{"".join(rows)}</tbody></table>'
                   f'<p style="margin:4px 0 0;font-size:11px;color:{grey}">Tendencia por regla fija: alcista si el precio '
                   'y la media de 50 sesiones superan la de 200 y la de 50 sube; bajista al revés. «Pct. 2020»: percentil '
                   'del nivel actual en todo el historial desde 2020. Tipos en puntos básicos.</p>')
    # 7. Limitaciones (automáticas + de la IA) y nota
    lim = []
    lag = (src.get("X_fechas_de_cierre", {}).get("datos") or {}).get("activos_con_cierre_anterior") or {}
    if lag:
        by_date: dict[str, list[str]] = {}
        for k, v in lag.items():
            by_date.setdefault(v, []).append(k)
        lim.append("Cierres de días anteriores: " + "; ".join(
            f"{', '.join(v)} ({d[8:10]}/{d[5:7]})" for d, v in sorted(by_date.items())) + ".")
    lim += [_esc(x) for x in res.get("limitaciones", [])]
    out.append(f'<p style="margin:12px 0 0;font-size:11px;color:{grey}">'
               + (f'<b>Limitaciones:</b> {" ".join(lim)} ' if lim else "")
               + f'Texto generado por IA ({_esc(analysis["model"])}) solo con los datos del informe; las afirmaciones sin '
               'fuente o con cifras que no cuadran con su fuente se eliminan automáticamente. No es una recomendación.</p>')
    return "".join(out)
