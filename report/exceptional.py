"""Detector de situaciones excepcionales (reglas fijas, sin IA). Versión exceptional-v1.1.

Capa DESCRIPTIVA de la Vía A, sin validación estadística. No es el radar de fragilidad de la
Vía B (Anexo A: puntuaciones F de combustible y S de chispa, umbrales congelados, presupuesto de
alertas e histéresis), que solo entra en producción si supera el backtest B1. Los datos que usa
(COT, financiación, interés abierto, flujos de ETF, volumen) sirven de base para ese backtest.

A. Posicionamiento extremo (COT de la CFTC): posición neta de fondos gestionados, fondos
   apalancados o gestoras en percentil ≥ 95 o ≤ 5 de los últimos 5 años, o un cambio semanal
   inusual. No anticipa la dirección del precio: indica sensibilidad a noticias.
B. Posible liquidación de posiciones:
   - «confirmada por fuente» solo si OKX publica liquidaciones muy por encima de lo normal
     (y solo para esa plataforma);
   - «compatible con» cuando caen a la vez precio e interés abierto (cripto diario, futuros
     semanal con el COT), sin confirmación de cierres forzosos.
C. Exceso de apalancamiento: financiación de perpetuos BTC en percentiles altos con interés
   abierto creciente y precio sin avanzar; en futuros, posición especulativa e interés
   abierto ambos en percentiles altos.
D. Flujos de capital en ETF (SPY, GLD, IBIT): flujo neto = variación de acciones en
   circulación × valor liquidativo (no es volumen negociado ni efecto precio). Salidas
   persistentes, y deterioro simultáneo si el precio pierde la media de 50 sesiones o el
   mínimo de 20.
E. Movimientos anómalos: volumen ≥ 3 veces su media de 20 sesiones, movimiento ≥ 3
   desviaciones típicas, salto de volatilidad, ruptura de máximos o mínimos de 52 semanas,
   cruce de la media de 200 y tensión en el diferencial de crédito high yield. Se combinan
   por instrumento para distinguir un movimiento ordinario de uno con señales de estrés.
"""

from __future__ import annotations

import datetime as dt
import math
import statistics

VERSION = "exceptional-v1.1"
PAGE = 1000
COT_LABEL = {"mm": "fondos gestionados", "lf": "fondos apalancados", "am": "gestoras"}
NAMES = {"gold": "Oro", "silver": "Plata", "copper": "Cobre", "wti": "Petróleo WTI", "spx": "S&P 500",
         "ndx": "Nasdaq-100", "rut": "Russell 2000", "us10y": "Bono a 10 años", "us02y": "Bono a 2 años",
         "eurusd": "Euro", "dxy": "Dólar (DXY)", "btc": "Bitcoin", "brent": "Brent"}
VOL_TICKER = {"spx": "spy", "ndx": "qqq", "rut": "iwm", "gold": "gld", "silver": "slv", "wti": "uso",
              "us10y": "tlt", "dxy": "uup", "btc": "ibit", "rtx": "rtx", "ypf": "ypf", "alibaba": "baba"}


def load(db, pattern: str) -> dict[str, list[tuple[str, float]]]:
    """Lee de la tabla observations (la vista materializada solo se refresca con la ingesta de precios).
    Si hay varias versiones de un dato, se queda la última."""
    latest: dict[tuple[str, str], tuple[int, float]] = {}
    offset = 0
    while True:
        rows = db.select("observations", {
            "select": "series_id,as_of,vintage,value", "series_id": f"like.{pattern}", "quality_status": "eq.ok",
            "order": "series_id,as_of,vintage", "limit": str(PAGE), "offset": str(offset)})
        for r in rows:
            if r["value"] is not None:
                latest[(r["series_id"], r["as_of"])] = (r["vintage"], float(r["value"]))
        if len(rows) < PAGE:
            break
        offset += PAGE
    out: dict[str, list[tuple[str, float]]] = {}
    for (sid, day), (_, v) in sorted(latest.items()):
        out.setdefault(sid, []).append((day, v))
    return out


def pct_rank(values: list[float], x: float) -> int:
    return round(100 * sum(v <= x for v in values) / len(values)) if values else 50


def value_at(series: list[tuple[str, float]], day: str) -> float | None:
    prev = None
    for d, v in series:
        if d > day:
            break
        prev = v
    return prev


def fmt(x: float, dec: int = 0) -> str:
    s = f"{x:,.{dec}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return s


def sig(tipo, instrumento, titulo, detalle, naturaleza, severidad, fuentes, fecha, metricas, serie):
    return {"tipo": tipo, "instrumento": instrumento, "titulo": titulo, "detalle": detalle,
            "naturaleza": naturaleza, "severidad": severidad, "fuentes": fuentes, "fecha_dato": fecha,
            "metricas": metricas, "series_id": serie}


# --- A y C (futuros): COT -------------------------------------------------- #
def cot_signals(cot: dict, closes: dict, overview: list | None = None) -> list[dict]:
    out = []
    groups: dict[tuple[str, str], dict] = {}
    for sid, s in cot.items():
        parts = sid.split("_")             # cftc_<inst>_<grp>_<side> | cftc_<inst>_oi
        if parts[-1] in ("long", "short"):
            groups.setdefault((parts[1], parts[2]), {})[parts[-1]] = dict(s)
    for (inst, grp), sides in groups.items():
        if "long" not in sides or "short" not in sides:
            continue
        dates = sorted(set(sides["long"]) & set(sides["short"]))
        if len(dates) < 104:
            continue
        net = [(d, sides["long"][d] - sides["short"][d]) for d in dates]
        oi = dict(cot.get(f"cftc_{inst}_oi", []))
        last_d, last = net[-1]
        window = [v for d, v in net if d >= (dt.date.fromisoformat(last_d) - dt.timedelta(days=5 * 365)).isoformat()]
        years = round(len(window) / 52, 1)
        p = pct_rank(window, last)
        chg = last - net[-2][1]
        diffs = [net[i][1] - net[i - 1][1] for i in range(max(1, len(net) - 156), len(net))]
        sd = statistics.pstdev(diffs[:-1]) if len(diffs) > 10 else None
        z = chg / sd if sd else 0
        oi_last = oi.get(last_d)
        oi_prev = oi.get(net[-2][0])
        oi_window = [v for d, v in oi.items() if d >= (dt.date.fromisoformat(last_d) - dt.timedelta(days=5 * 365)).isoformat()]
        oi_p = pct_rank(oi_window, oi_last) if oi_last else None
        price = closes.get(inst)
        pchg = None
        if price:
            a, b = value_at(price, net[-2][0]), value_at(price, last_d)
            if a and b:
                pchg = (b - a) * 100 if inst in ("us10y", "us02y") else (b / a - 1) * 100
        who = COT_LABEL[grp]
        name = NAMES.get(inst, inst)
        metr = {"neta_contratos": round(last), "percentil_5_años": p, "historial_años": years,
                "cambio_semanal_contratos": round(chg), "cambio_semanal_desviaciones": round(z, 1),
                "largas": round(sides["long"][last_d]), "cortas": round(sides["short"][last_d]),
                "neta_sobre_interes_abierto_%": round(100 * last / oi_last, 1) if oi_last else None,
                "interes_abierto": oi_last, "interes_abierto_percentil_5_años": oi_p,
                "interes_abierto_cambio_semanal_%": round((oi_last / oi_prev - 1) * 100, 1) if oi_last and oi_prev else None,
                ("precio_cambio_semana_pb" if inst in ("us10y", "us02y") else "precio_cambio_semana_%"):
                    None if pchg is None else round(pchg, 1 if inst not in ("us10y", "us02y") else 0),
                "fecha_posiciones": last_d, "publicado": "viernes siguiente"}
        src = [f"CFTC COT ({'desagregado' if grp == 'mm' else 'TFF'})"]
        if overview is not None:
            overview.append({"instrumento": inst, "categoria": who, **metr})
        if p >= 95 or p <= 5:
            side = "larga" if last > 0 else "corta"
            extreme = "máximo" if p >= 95 else "mínimo"
            out.append(sig("A", inst, f"{name}: posicionamiento de {who} en percentil {p} ({extreme} de {years:g} años)",
                           f"Posición neta {side} de {fmt(abs(last))} contratos al {last_d}, percentil {p} de los últimos "
                           f"{years:g} años. No anticipa la dirección del precio: indica una exposición poco habitual que "
                           "puede aumentar la sensibilidad a noticias y movimientos de precio.",
                           "dato publicado", "alta" if p >= 98 or p <= 2 else "atención", src, last_d, metr,
                           f"cftc_{inst}_{grp}_long"))
        if abs(z) >= 2.5:
            out.append(sig("A", inst, f"{name}: cambio semanal inusual del posicionamiento de {who}",
                           f"La posición neta varió {fmt(chg)} contratos en la semana al {last_d} "
                           f"({fmt(z, 1)} desviaciones típicas de sus cambios semanales habituales).",
                           "dato publicado", "atención", src, last_d, metr, f"cftc_{inst}_{grp}_short"))
        # B semanal: precio e interés abierto caen a la vez con reducción de netos largos (o subida con cierre de cortos)
        oi_chg = metr["interes_abierto_cambio_semanal_%"]
        threshold = 10 if inst in ("us10y", "us02y") else 3
        if pchg is not None and oi_chg is not None and oi_chg <= -5:
            if pchg <= -threshold and chg < 0 and last_d:
                out.append(sig("B", inst, f"{name}: caída de precio e interés abierto con reducción de largos de {who}",
                               f"En la semana al {last_d}, el precio varió {fmt(pchg, 1)}{' pb' if threshold == 10 else ' %'}, "
                               f"el interés abierto {fmt(oi_chg, 1)} % y la posición neta de {who} {fmt(chg)} contratos. "
                               "Es compatible con cierre de posiciones largas; el COT no confirma que fueran cierres forzosos.",
                               "compatible con", "atención", src, last_d, metr, f"cftc_{inst}_oi"))
            elif pchg >= threshold and chg > 0:
                out.append(sig("B", inst, f"{name}: subida de precio con caída del interés abierto (cierre de cortos)",
                               f"En la semana al {last_d}, el precio varió +{fmt(pchg, 1)}{' pb' if threshold == 10 else ' %'}, "
                               f"el interés abierto {fmt(oi_chg, 1)} % y la neta de {who} +{fmt(chg)} contratos. "
                               "Es compatible con recompra de cortos, sin confirmación de cierres forzosos.",
                               "compatible con", "atención", src, last_d, metr, f"cftc_{inst}_oi"))
        # C futuros: posición especulativa e interés abierto ambos altos
        if grp in ("mm", "lf") and oi_p is not None and oi_p >= 90 and (p >= 95 or p <= 5):
            out.append(sig("C", inst, f"{name}: posición de {who} extrema con interés abierto en percentil {oi_p}",
                           "Posicionamiento concentrado y mercado muy cargado de contratos: vulnerable a movimientos "
                           "bruscos si se deshacen posiciones. No demuestra que vaya a ocurrir.",
                           "compatible con", "atención", src, last_d, metr, f"cftc_{inst}_oi"))
    return out


# --- B y C (cripto) --------------------------------------------------------- #
def crypto_signals(lev: dict, closes: dict) -> list[dict]:
    out = []
    btc = closes.get("btc") or []
    if len(btc) < 30:
        return out
    last_d, last_p = btc[-1]
    p1 = (last_p / btc[-2][1] - 1) * 100
    p7 = (last_p / value_at(btc, (dt.date.fromisoformat(last_d) - dt.timedelta(days=7)).isoformat()) - 1) * 100
    oi = lev.get("okx_btc_oi_usd") or []
    oi1 = oi7 = None
    if len(oi) >= 8:
        oi1 = (oi[-1][1] / oi[-2][1] - 1) * 100
        oi7 = (oi[-1][1] / oi[-8][1] - 1) * 100
    liq_l, liq_s = lev.get("okx_btc_liq_long_usd") or [], lev.get("okx_btc_liq_short_usd") or []
    base = {"precio_btc_cambio_1d_%": round(p1, 2), "precio_btc_cambio_7d_%": round(p7, 2),
            "oi_okx_cambio_1d_%": None if oi1 is None else round(oi1, 1),
            "oi_okx_cambio_7d_%": None if oi7 is None else round(oi7, 1)}
    for series, side in ((liq_l, "largos"), (liq_s, "cortos")):
        if len(series) >= 7:
            med = statistics.median(v for _, v in series[-31:-1]) or 1
            cur = series[-1][1]
            if cur >= 3 * med and cur >= 5e6:
                out.append(sig("B", "btc", f"Bitcoin: liquidaciones de {side} {fmt(cur / med, 1)} veces lo habitual en OKX",
                               f"OKX registró {fmt(cur / 1e6, 1)} millones de USD en liquidaciones de {side} en 24 horas "
                               f"(mediana de 30 días: {fmt(med / 1e6, 1)} millones). Confirmado solo para OKX.",
                               "confirmado por fuente", "alta", ["OKX liquidaciones"], series[-1][0],
                               {**base, "liquidaciones_usd": round(cur), "mediana_30d_usd": round(med)},
                               "okx_btc_liq_long_usd" if side == "largos" else "okx_btc_liq_short_usd"))
    if oi1 is not None and p1 <= -4 and oi1 <= -5:
        out.append(sig("B", "btc", "Bitcoin: caída de precio con fuerte descenso del interés abierto",
                       f"El precio cayó {fmt(p1, 1)} % y el interés abierto en OKX {fmt(oi1, 1)} % en un día. Es compatible "
                       "con una reducción de apalancamiento; solo las liquidaciones publicadas confirmarían cierres forzosos.",
                       "compatible con", "alta", ["OKX interés abierto", "Coinbase (precio)"], last_d, base, "okx_btc_oi_usd"))
    for sid, name in (("deribit_btc_funding_ann", "Deribit"), ("okx_btc_funding_ann", "OKX")):
        f = lev.get(sid) or []
        if len(f) < 60:
            continue
        avg7 = statistics.mean(v for _, v in f[-7:])
        hist7 = [statistics.mean(x for _, x in f[i - 7:i]) for i in range(7, len(f) + 1)]
        p = pct_rank(hist7, avg7)
        m = {**base, "financiacion_media_7d_anual_%": round(avg7, 1), "percentil": p,
             "historial_dias": len(f), "plataforma": name}
        if p >= 90 and oi7 is not None and oi7 >= 5 and abs(p7) < 3:
            out.append(sig("C", "btc", f"Bitcoin: financiación alta ({name}), interés abierto al alza y precio estancado",
                           f"Financiación media de 7 días del {fmt(avg7, 1)} % anual (percentil {p}), interés abierto en OKX "
                           f"+{fmt(oi7, 1)} % en 7 días y precio {fmt(p7, 1)} %. Podría indicar exceso de largos apalancados; "
                           "no demuestra que el mercado vaya a corregir.", "compatible con", "alta",
                           [f"{name} financiación", "OKX interés abierto"], f[-1][0], m, sid))
        elif p >= 95 or p <= 5:
            out.append(sig("C", "btc", f"Bitcoin: financiación de perpetuos en percentil {p} ({name})",
                           f"Financiación media de 7 días del {fmt(avg7, 1)} % anual, percentil {p} de su historial. "
                           + ("Los largos pagan una prima inusual." if p >= 95 else "Los cortos pagan: posicionamiento bajista inusual."),
                           "dato publicado", "atención", [f"{name} financiación"], f[-1][0], m, sid))
    return out


# --- D: flujos ETF ------------------------------------------------------------ #
def flow_signals(flows: dict, closes: dict) -> list[dict]:
    out = []
    for etf, inst in (("spy", "spx"), ("gld", "gold"), ("ibit", "btc")):
        f = flows.get(f"etf_{etf}_flow_usd") or []
        shares = flows.get(f"etf_{etf}_shares") or []
        nav = flows.get(f"etf_{etf}_nav") or []
        if len(f) < 5:
            continue
        last5 = sum(v for _, v in f[-5:])
        streak = 0
        for _, v in reversed(f):
            if v < 0:
                streak += 1
            else:
                break
        aum = shares[-1][1] * nav[-1][1] if shares and nav else None
        sums5 = [sum(v for _, v in f[i - 5:i]) for i in range(5, len(f) + 1)]
        p = pct_rank(sums5, last5) if len(sums5) > 50 else None
        price = closes.get(inst) or []
        below50 = broke20 = False
        if len(price) >= 50:
            vals = [v for _, v in price]
            below50 = vals[-1] < sum(vals[-50:]) / 50
            broke20 = vals[-1] <= min(vals[-20:])
        m = {"flujo_5_sesiones_usd": round(last5), "sesiones_seguidas_con_salidas": streak,
             "flujo_5_sesiones_%_patrimonio": round(100 * last5 / aum, 2) if aum else None,
             "percentil_flujo_5_sesiones": p, "precio_bajo_media_50": below50, "precio_en_minimo_20_sesiones": broke20,
             "metodo": "variación de acciones en circulación × valor liquidativo (emisor)"}
        name = etf.upper()
        if streak >= 3 and (below50 or broke20):
            out.append(sig("D", inst, f"{name}: {streak} sesiones de salidas netas y precio perdiendo soporte",
                           f"Salidas netas de {fmt(abs(last5) / 1e6)} millones de USD en 5 sesiones "
                           f"({'precio bajo la media de 50' if below50 else ''}{' y ' if below50 and broke20 else ''}"
                           f"{'en mínimo de 20 sesiones' if broke20 else ''}). Deterioro simultáneo de flujos y precio.",
                           "dato publicado", "alta", [f"Emisor del ETF {name}"], f[-1][0], m, f"etf_{etf}_flow_usd"))
        elif p is not None and (p <= 5 or p >= 95):
            out.append(sig("D", inst, f"{name}: flujo de 5 sesiones en percentil {p}",
                           f"{'Entradas' if last5 > 0 else 'Salidas'} netas de {fmt(abs(last5) / 1e6)} millones de USD en 5 "
                           "sesiones, poco habitual en su historial. Es flujo de creación o reembolso de acciones, no volumen "
                           "negociado.", "dato publicado", "atención", [f"Emisor del ETF {name}"], f[-1][0], m,
                           f"etf_{etf}_flow_usd"))
        elif streak >= 5:
            out.append(sig("D", inst, f"{name}: {streak} sesiones seguidas de salidas netas",
                           f"Salidas persistentes; {fmt(abs(last5) / 1e6)} millones de USD en las últimas 5 sesiones.",
                           "dato publicado", "atención", [f"Emisor del ETF {name}"], f[-1][0], m, f"etf_{etf}_flow_usd"))
    return out


# --- E: movimientos anómalos ----------------------------------------------- #
def abnormal_signals(closes: dict, volumes: dict, rates: set[str], stress: dict) -> list[dict]:
    out = []
    for inst, series in closes.items():
        if len(series) < 260:
            continue
        vals = [v for _, v in series]
        is_rate = inst in rates
        rets = [(vals[i] - vals[i - 1]) * 100 if is_rate else math.log(vals[i] / vals[i - 1]) * 100
                for i in range(1, len(vals)) if is_rate or (vals[i] > 0 and vals[i - 1] > 0)]
        sd = statistics.pstdev(rets[-61:-1])
        z = rets[-1] / sd if sd else 0
        vol20 = statistics.pstdev(rets[-20:])
        med = statistics.median(statistics.pstdev(rets[i - 20:i]) for i in range(len(rets) - 252, len(rets), 5))
        hi52 = max(vals[-253:-1])
        lo52 = min(vals[-253:-1])
        sma200 = sum(vals[-200:]) / 200
        sma200_prev = sum(vals[-203:-3]) / 200
        crossed = (vals[-1] - sma200) * (vals[-4] - sma200_prev) < 0
        vt = VOL_TICKER.get(inst)
        vser = volumes.get(f"vol_{vt}") if vt else None
        vol_ratio = None
        if vser and len(vser) >= 21 and vser[-1][0] >= series[-1][0]:
            avg = statistics.mean(v for _, v in vser[-21:-1])
            vol_ratio = vser[-1][1] / avg if avg else None
        conds = []
        if abs(z) >= 3:
            conds.append(f"movimiento de {fmt(z, 1)} desviaciones típicas")
        if vol_ratio and vol_ratio >= 3:
            conds.append(f"volumen {fmt(vol_ratio, 1)} veces su media de 20 sesiones ({vt.upper()})")
        if med and vol20 >= 2 * med:
            conds.append("volatilidad de 20 sesiones el doble de su mediana anual")
        if vals[-1] > hi52:
            conds.append("máximo de 52 semanas")
        if vals[-1] < lo52:
            conds.append("mínimo de 52 semanas")
        if crossed:
            conds.append("cruce de la media de 200 sesiones")
        if not conds:
            continue
        down = rets[-1] < 0
        strong = sum(1 for c in conds if "desviaciones" in c or "volumen" in c or "volatilidad" in c)
        if len(conds) >= 2 and strong >= 2:
            sev, extra = "alta", (" La combinación de una caída con estas señales es compatible con una reducción de "
                                   "exposición o un episodio de estrés." if down else
                                   " La combinación de una subida con estas señales es compatible con cierre de cortos o "
                                   "entrada de flujo agresivo.")
        elif len(conds) >= 2:
            sev, extra = "atención", ""
        elif strong:
            sev, extra = "atención", ""
        else:
            continue                      # una ruptura técnica aislada ya la recogen las alertas de umbral
        name = NAMES.get(inst, inst)
        out.append(sig("E", inst, f"{name}: movimiento anómalo ({len(conds)} señales)",
                       (lambda t: t[:1].upper() + t[1:])("; ".join(conds)) + "." + extra, "compatible con" if extra else "dato",
                       sev, ["Precios de cierre", "Tiingo (volumen)"] if vol_ratio else ["Precios de cierre"],
                       series[-1][0], {"desviaciones_tipicas_dia": round(z, 1),
                                       "volumen_vs_media_20": None if vol_ratio is None else round(vol_ratio, 1),
                                       "volatilidad_20_vs_mediana_anual": round(vol20 / med, 2) if med else None,
                                       "condiciones": conds}, None))
    hy = stress.get("fred_BAMLH0A0HYM2") or []
    if len(hy) > 300:
        cur = hy[-1][1]
        prev5 = value_at(hy, (dt.date.fromisoformat(hy[-1][0]) - dt.timedelta(days=7)).isoformat())
        chg = (cur - prev5) * 100 if prev5 else 0
        changes = [(hy[i][1] - hy[i - 5][1]) * 100 for i in range(5, len(hy))]
        p = pct_rank(changes, chg)
        if p >= 95 and chg >= 20:
            out.append(sig("E", None, "Crédito high yield: el diferencial se amplía con rapidez",
                           f"El diferencial de bonos high yield de EE. UU. subió {fmt(chg)} pb en una semana hasta "
                           f"{fmt(cur, 2)} %, percentil {p} de sus cambios semanales desde 2020. Señal de tensión financiera.",
                           "dato publicado", "alta", ["FRED (ICE BofA HY OAS)"], hy[-1][0],
                           {"diferencial_%": cur, "cambio_semana_pb": round(chg), "percentil": p}, "fred_BAMLH0A0HYM2"))
    return out


def detect(db, closes: dict, rates: set[str]) -> tuple[list[dict], list[dict]]:
    """Devuelve las señales y un resumen del posicionamiento COT de todos los mercados."""
    cot = load(db, "cftc_*")
    lev = {**load(db, "okx_*"), **load(db, "deribit_*"), **load(db, "hl_*")}
    flows = load(db, "etf_*")
    volumes = load(db, "vol_*")
    stress = load(db, "fred_BAML*")
    overview: list[dict] = []
    signals = (cot_signals(cot, closes, overview) + crypto_signals(lev, closes) + flow_signals(flows, closes)
               + abnormal_signals(closes, volumes, rates, stress))
    order = {"alta": 0, "atención": 1}
    signals.sort(key=lambda s: (order.get(s["severidad"], 2), s["tipo"]))
    return signals, overview


RULE_PARAMS = {
    "A": "COT: neta (largas − cortas) en percentil ≥ 95 o ≤ 5 de 5 años (≥ 98/≤ 2 = alta), o cambio semanal ≥ 2,5 desv.",
    "B": "OKX liquidaciones ≥ 3 × mediana 30 d y ≥ 5 M USD (confirmado); BTC −4 % y OI OKX −5 % en un día; COT: "
         "precio ±3 % (10 pb en bonos), OI −5 % y neta en la dirección del cierre (compatible)",
    "C": "Financiación 7 d en percentil ≥ 90 con OI +5 % en 7 d y precio |7 d| < 3 %; o percentil ≥ 95/≤ 5; "
         "futuros: neta extrema e interés abierto en percentil ≥ 90",
    "D": "ETF SPY/GLD/IBIT: ≥ 3 sesiones de salidas y precio bajo MM50 o en mínimo de 20; flujo 5 sesiones en "
         "percentil ≤ 5/≥ 95; ≥ 5 sesiones seguidas de salidas",
    "E": "≥ 2 de: movimiento ≥ 3 desv., volumen ≥ 3 × media 20, volatilidad 20 ≥ 2 × mediana anual, máximo/mínimo "
         "52 semanas, cruce de MM200; diferencial HY +20 pb en una semana y percentil ≥ 95",
}


def store(db, signals: list[dict], price_series: dict[str, str]) -> int:
    """Guarda cada señal como alerta informativa (una por regla, serie y fecha)."""
    import json
    r = db.session.post(f"{db.base}/rule_versions", params={"on_conflict": "id"},
                        headers={"Prefer": "resolution=ignore-duplicates,return=minimal"},
                        data=json.dumps({"id": VERSION, "scope": "threshold_alerts", "params": RULE_PARAMS,
                                         "frozen_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                                         "notes": "report/exceptional.py; se ejecuta con el informe diario."}),
                        timeout=60)
    if not r.ok:
        raise RuntimeError(f"rule_versions HTTP {r.status_code}: {r.text[:200]}")
    n = 0
    for s in signals:
        s = {**s, "series_id": s["series_id"] or price_series.get(s["instrumento"] or "")}
        row = {"kind": "informative", "rule_id": f"exc_{s['tipo']}", "rule_version": VERSION,
               "instrument_id": s["instrumento"], "series_id": s["series_id"],
               "as_of": s["fecha_dato"][:10], "title": s["titulo"],
               "details": {k: s[k] for k in ("detalle", "naturaleza", "severidad", "fuentes", "metricas")},
               "triggered_at": dt.datetime.now(dt.timezone.utc).isoformat()}
        try:
            db.insert("alerts", row)
            n += 1
        except Exception as exc:          # ya registrada (índice único) u otro conflicto
            if "409" not in str(exc) and "duplicate" not in str(exc).lower():
                print(f"::warning::No se pudo guardar la señal {s['titulo'][:60]}: {str(exc)[:150]}")
    return n
