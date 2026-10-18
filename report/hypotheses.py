"""Seguimiento automático de las hipótesis del análisis (sin IA).

Cada hipótesis guardada en daily_reports.content.ai.result.hipotesis lleva una versión medible:
activo y nivel de la condición, activo y dirección de la consecuencia, y horizonte en sesiones.

Evaluación, con los cierres posteriores a la fecha del informe:
- «pendiente»: aún no se ha cumplido la condición y quedan sesiones del horizonte;
- «no activada»: el horizonte terminó sin que se cumpliera la condición;
- «en curso»: la condición se cumplió y la consecuencia aún no ha completado su horizonte;
- «acierto» / «fallo»: la condición se cumplió y, al final del horizonte contado desde ese día,
  el activo de la consecuencia se movió (o no) en la dirección prevista.

Es una medida sencilla, no un backtest: sirve para saber si las hipótesis aportan algo.
"""

from __future__ import annotations

import datetime as dt

LOOKBACK_DAYS = 60


def _after(series: list[tuple[str, float]], day: str) -> list[tuple[str, float]]:
    return [(d, v) for d, v in series if d > day]


def _at_or_before(series: list[tuple[str, float]], day: str) -> float | None:
    prev = None
    for d, v in series:
        if d > day:
            break
        prev = v
    return prev


def evaluate(h: dict, made_on: str, closes: dict) -> dict:
    cond = closes.get(h["condicion_activo"]) or []
    cons = closes.get(h["consecuencia_activo"]) or []
    horizon = int(h.get("horizonte_sesiones") or 5)
    window = _after(cond, made_on)[:horizon]
    hit_day = None
    for d, v in window:
        if (h["condicion_operador"] == ">" and v > h["condicion_nivel"]) or \
           (h["condicion_operador"] == "<" and v < h["condicion_nivel"]):
            hit_day = d
            break
    if hit_day is None:
        state = "no activada" if len(window) >= horizon else "pendiente"
        return {"estado": state, "sesiones_observadas": len(window)}
    start = _at_or_before(cons, hit_day)
    path = _after(cons, hit_day)[:horizon]
    if start is None or not path:
        return {"estado": "en curso", "activada": hit_day}
    end_day, end = path[-1]
    move = end - start
    rate = h["consecuencia_activo"] in ("us10y", "us02y")
    chg = round(move * 100) if rate else round((end / start - 1) * 100, 2)
    if len(path) < horizon:
        return {"estado": "en curso", "activada": hit_day, "cambio_hasta_ahora": chg, "unidad": "pb" if rate else "%"}
    ok = (move > 0) if h["consecuencia_direccion"] == "sube" else (move < 0)
    return {"estado": "acierto" if ok else "fallo", "activada": hit_day, "cerrada": end_day,
            "cambio": chg, "unidad": "pb" if rate else "%"}


def track(db, closes: dict, report_date: dt.date) -> dict:
    since = (report_date - dt.timedelta(days=LOOKBACK_DAYS)).isoformat()
    rows = db.select("daily_reports", {"select": "report_date,content", "report_date": f"gte.{since}",
                                       "and": f"(report_date.lt.{report_date.isoformat()})",
                                       "order": "report_date.desc"})
    items = []
    for r in rows:
        hyps = (((r.get("content") or {}).get("ai") or {}).get("result") or {}).get("hipotesis") or []
        for h in hyps:
            if not all(k in h for k in ("condicion_activo", "condicion_operador", "condicion_nivel",
                                        "consecuencia_activo", "consecuencia_direccion")):
                continue                       # hipótesis antiguas, sin versión medible
            items.append({"fecha": r["report_date"], "si": h.get("si"), "entonces": h.get("entonces"),
                          **evaluate(h, r["report_date"], closes)})
    counts: dict[str, int] = {}
    for it in items:
        counts[it["estado"]] = counts.get(it["estado"], 0) + 1
    decided = counts.get("acierto", 0) + counts.get("fallo", 0)
    return {"items": items, "recuento": counts,
            "tasa_acierto": round(100 * counts.get("acierto", 0) / decided) if decided else None,
            "decididas": decided}
