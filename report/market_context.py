"""Contexto histórico y de tendencia, calculado sin IA desde Supabase.

Para cada instrumento con cierres diarios (vista v_close, desde 2020):
- variaciones a 1 semana, 1, 3, 6 y 12 meses y en el año (tipos en puntos básicos);
- medias móviles de 50 y 200 sesiones, distancia del precio a ellas, pendiente de la
  media de 50 y último cruce entre ambas;
- tendencia calculada con una regla fija (ver trend_label);
- máximos y mínimos de 52 semanas y desde 2020, con fecha, y caída desde el máximo;
- percentil del nivel actual en todo el historial desde 2020;
- régimen de volatilidad (20 sesiones frente a la mediana de un año) y racha de días.

Y un bloque macro: liquidez neta de la Fed, M2, fondos federales, NFCI, pendiente de la
curva 2-10 años y correlaciones de 60 sesiones frente a su rango anual.
"""

from __future__ import annotations

import datetime as dt
import math
import statistics

PAGE = 1000

# Instrumentos que el análisis debe cubrir siempre en el panorama de tendencias.
CORE = ["spx", "ndx", "rut", "us10y", "us02y", "dxy", "eurusd", "gold", "silver", "copper", "brent", "wti",
        "btc", "move"]


def load_closes(db) -> dict[str, list[tuple[str, float]]]:
    """Todos los cierres de v_close, paginando (PostgREST devuelve 1.000 filas como máximo)."""
    out: dict[str, list[tuple[str, float]]] = {}
    offset = 0
    while True:
        rows = db.select("v_close", {"select": "instrument_id,as_of,value", "order": "instrument_id,as_of",
                                     "limit": str(PAGE), "offset": str(offset)})
        for r in rows:
            if r["value"] is not None:
                out.setdefault(r["instrument_id"], []).append((r["as_of"], float(r["value"])))
        if len(rows) < PAGE:
            return out
        offset += PAGE


def load_macro(db) -> dict:
    def sel(view, params):
        return db.select(view, params)
    return {
        "liquidity": sel("v_net_liquidity", {"select": "as_of,net_liquidity_b,chg_4w_b,chg_13w_b",
                                             "order": "as_of.desc", "limit": "60"}),
        "m2": sel("v_m2", {"select": "as_of,m2_b,chg_yoy_pct", "order": "as_of.desc", "limit": "13"}),
        "fedfunds": sel("v_observations_latest", {"select": "as_of,value", "series_id": "eq.fred_FEDFUNDS",
                                                  "quality_status": "eq.ok", "order": "as_of.desc", "limit": "25"}),
        "nfci": sel("v_observations_latest", {"select": "as_of,value", "series_id": "eq.fred_NFCI",
                                              "order": "as_of.desc", "limit": "53"}),
    }


def _sma(values: list[float], n: int, end: int | None = None) -> float | None:
    end = len(values) if end is None else end
    if end < n:
        return None
    return sum(values[end - n:end]) / n


def _ago(series: list[tuple[str, float]], days: int) -> float | None:
    """Último valor en o antes de (fecha final − días)."""
    target = (dt.date.fromisoformat(series[-1][0]) - dt.timedelta(days=days)).isoformat()
    prev = None
    for d, v in series:
        if d > target:
            break
        prev = v
    return prev


def _chg(cur: float, old: float | None, is_rate: bool) -> float | None:
    if old is None:
        return None
    if is_rate:
        return round((cur - old) * 100, 1)                      # puntos básicos
    return round((cur / old - 1) * 100, 2) if old else None


def trend_label(price: float, sma50: float | None, sma200: float | None, slope50: float | None) -> str:
    """Regla fija: alcista si precio > MM200, MM50 > MM200 y MM50 subiendo; bajista al revés; si no, lateral."""
    if None in (sma50, sma200, slope50):
        return "sin datos"
    if price > sma200 and sma50 > sma200 and slope50 > 0:
        return "alcista"
    if price < sma200 and sma50 < sma200 and slope50 < 0:
        return "bajista"
    return "lateral o en transición"


def instrument_context(series: list[tuple[str, float]], is_rate: bool) -> dict:
    dates = [d for d, _ in series]
    vals = [v for _, v in series]
    cur, last = vals[-1], dates[-1]
    sma50, sma200 = _sma(vals, 50), _sma(vals, 200)
    sma50_prev = _sma(vals, 50, len(vals) - 20)
    slope50 = None if sma50 is None or sma50_prev is None else (sma50 / sma50_prev - 1) * 100
    # Último cruce de medias (dorado: MM50 cruza por encima de MM200)
    cross = None
    for i in range(len(vals) - 1, max(200, len(vals) - 500), -1):
        a, b = _sma(vals, 50, i + 1), _sma(vals, 200, i + 1)
        pa, pb = _sma(vals, 50, i), _sma(vals, 200, i)
        if None in (a, b, pa, pb):
            break
        if (a - b) * (pa - pb) < 0:
            cross = {"tipo": "dorado (MM50 sobre MM200)" if a > b else "de la muerte (MM50 bajo MM200)",
                     "fecha": dates[i]}
            break
    year_start = next((v for d, v in reversed(series) if d < f"{last[:4]}-01-01"), None)
    window52 = [(d, v) for d, v in series if d >= (dt.date.fromisoformat(last) - dt.timedelta(days=365)).isoformat()]
    hi52 = max(window52, key=lambda x: x[1])
    lo52 = min(window52, key=lambda x: x[1])
    hi_all = max(series, key=lambda x: x[1])
    lo_all = min(series, key=lambda x: x[1])
    pct = round(100 * sum(v <= cur for v in vals) / len(vals))
    # Volatilidad: desviación de las variaciones diarias (tipos en pb; resto en log-rendimientos)
    if is_rate:
        rets = [(vals[i] - vals[i - 1]) * 100 for i in range(1, len(vals))]
    else:
        rets = [math.log(vals[i] / vals[i - 1]) * 100 for i in range(1, len(vals)) if vals[i - 1] > 0 and vals[i] > 0]
    vol20 = statistics.pstdev(rets[-20:]) if len(rets) >= 20 else None
    vols_1y = [statistics.pstdev(rets[i - 20:i]) for i in range(max(20, len(rets) - 252), len(rets) + 1, 5)]
    vol_med = statistics.median(vols_1y) if vols_1y else None
    streak = 0
    for i in range(len(vals) - 1, 0, -1):
        step = 1 if vals[i] > vals[i - 1] else -1 if vals[i] < vals[i - 1] else 0
        if step == 0 or (streak and (step > 0) != (streak > 0)):
            break
        streak += step

    def dist(ref):
        if ref is None:
            return None
        return round((cur - ref) * 100, 1) if is_rate else round((cur / ref - 1) * 100, 2)

    unit = "pb" if is_rate else "%"
    return {
        "fecha_cierre": last, "ultimo": round(cur, 4 if abs(cur) < 10 else 2),
        f"variacion_1_semana_{unit}": _chg(cur, _ago(series, 7), is_rate),
        f"variacion_1_mes_{unit}": _chg(cur, _ago(series, 30), is_rate),
        f"variacion_3_meses_{unit}": _chg(cur, _ago(series, 91), is_rate),
        f"variacion_6_meses_{unit}": _chg(cur, _ago(series, 182), is_rate),
        f"variacion_12_meses_{unit}": _chg(cur, _ago(series, 365), is_rate),
        f"variacion_en_el_año_{unit}": _chg(cur, year_start, is_rate),
        f"distancia_a_media_50_{unit}": dist(sma50),
        f"distancia_a_media_200_{unit}": dist(sma200),
        "pendiente_media_50_en_20_sesiones_%": None if slope50 is None else round(slope50, 2),
        "ultimo_cruce_de_medias": cross,
        "tendencia_calculada": trend_label(cur, sma50, sma200, slope50),
        "maximo_52_semanas": {"valor": round(hi52[1], 4), "fecha": hi52[0], f"distancia_{unit}": dist(hi52[1])},
        "minimo_52_semanas": {"valor": round(lo52[1], 4), "fecha": lo52[0], f"distancia_{unit}": dist(lo52[1])},
        "maximo_desde_2020": {"valor": round(hi_all[1], 4), "fecha": hi_all[0], f"distancia_{unit}": dist(hi_all[1])},
        "minimo_desde_2020": {"valor": round(lo_all[1], 4), "fecha": lo_all[0], f"distancia_{unit}": dist(lo_all[1])},
        "percentil_del_nivel_desde_2020": pct,
        "volatilidad_diaria_20_sesiones": None if vol20 is None else round(vol20, 2),
        "volatilidad_mediana_1_año": None if vol_med is None else round(vol_med, 2),
        "regimen_de_volatilidad": None if not (vol20 and vol_med) else
        ("alta" if vol20 > 1.5 * vol_med else "baja" if vol20 < 0.67 * vol_med else "normal"),
        "racha_de_sesiones": f"{abs(streak)} {'al alza' if streak > 0 else 'a la baja'}" if streak else "sin racha",
        "unidad_variaciones": unit,
    }


def macro_context(macro: dict, closes: dict, correlations: list[dict]) -> dict:
    out: dict = {}
    liq = macro.get("liquidity") or []
    if liq:
        cur = liq[0]
        year = liq[min(51, len(liq) - 1)]
        out["liquidez_neta_fed"] = {
            "fecha": cur["as_of"], "nivel_miles_de_millones_usd": round(float(cur["net_liquidity_b"])),
            "cambio_4_semanas": cur.get("chg_4w_b"), "cambio_13_semanas": cur.get("chg_13w_b"),
            "cambio_aprox_1_año": round(float(cur["net_liquidity_b"]) - float(year["net_liquidity_b"])),
            "fuente": "FRED (WALCL − WTREGEN − RRPONTSYD)"}
    m2 = macro.get("m2") or []
    if m2:
        out["m2"] = {"fecha": m2[0]["as_of"], "variacion_interanual_%": m2[0].get("chg_yoy_pct"),
                     "variacion_interanual_hace_6_meses_%": m2[min(6, len(m2) - 1)].get("chg_yoy_pct"),
                     "fuente": "FRED (M2SL)"}
    ff = macro.get("fedfunds") or []
    if ff:
        changes = [(ff[i]["as_of"], round((float(ff[i]["value"]) - float(ff[i + 1]["value"])) * 100))
                   for i in range(len(ff) - 1) if float(ff[i]["value"]) != float(ff[i + 1]["value"])]
        out["fondos_federales"] = {"fecha": ff[0]["as_of"], "nivel_%": float(ff[0]["value"]),
                                   "hace_12_meses_%": float(ff[min(12, len(ff) - 1)]["value"]),
                                   "ultimos_cambios_pb": changes[:4], "fuente": "FRED (FEDFUNDS)"}
    nf = macro.get("nfci") or []
    if nf:
        out["condiciones_financieras_nfci"] = {
            "fecha": nf[0]["as_of"], "valor": float(nf[0]["value"]),
            "hace_3_meses": float(nf[min(13, len(nf) - 1)]["value"]),
            "hace_1_año": float(nf[-1]["value"]), "lectura": "negativo = más laxas que la media histórica",
            "fuente": "FRED (NFCI)"}
    s2, s10 = closes.get("us02y"), closes.get("us10y")
    if s2 and s10:
        d2 = dict(s2)
        common = [(d, v - d2[d]) for d, v in s10 if d in d2]
        if common:
            spread = [(d, round(x * 100)) for d, x in common]
            cur = spread[-1]

            def back(days):
                target = (dt.date.fromisoformat(cur[0]) - dt.timedelta(days=days)).isoformat()
                return next((v for d, v in reversed(spread) if d <= target), None)
            out["curva_2_10_años_pb"] = {"fecha": cur[0], "actual": cur[1], "hace_1_mes": back(30),
                                         "hace_3_meses": back(91), "hace_1_año": back(365),
                                         "minimo_desde_2020": min(v for _, v in spread),
                                         "fuente": "FRED (DGS10 − DGS2)"}
    corr = []
    for c in correlations or []:
        cur, lo, hi = c.get("corr_60"), c.get("min_1y"), c.get("max_1y")
        corr.append({"par": c["label"], "correlacion_60_sesiones": None if cur is None else round(float(cur), 2),
                     "hace_3_meses": None if c.get("corr_3m_ago") is None else round(float(c["corr_3m_ago"]), 2),
                     "rango_1_año": None if lo is None or hi is None else [round(float(lo), 2), round(float(hi), 2)]})
    if corr:
        out["correlaciones"] = corr
    return out
