"""Слой 1 — ядро: месячный композит z-скоров (docs/CONTRACT.md §4).

Порт модели M1 из validation/walkforward.py, победившей гонку walk-forward
(OOS IC +0,227 против ~0 у адаптивных весов). Ключевые решения НЕ подлежат
«улучшению на глаз», потому что именно попытка их улучшить и провалилась:

* веса РАВНЫЕ. Оптимизация весов на 271 месяце — самообман (REGIME.md §7);
* состав ФИКСИРОВАННЫЙ. Отбор по скользящей результативности (M2) дал OOS IC −0,018:
  в 2010–2016 он выбирал rgbi_mom и dy ровно перед сменой их знака;
* окно z — 60 месяцев при min 24, обрезка ±3. Устойчивость проверена на 36–72 мес;
* компонент без данных просто выпадает, вес перераспределяется между остальными
  (так же вёл себя M1: comp/nn по числу непустых ног).

Ребаланс мышления месячный, не дневной: дневное значение показываем, но знак
переключаем с гистерезисом, иначе дашборд рассылает «развороты ядра» на шуме.
"""

import math

try:
    from ..lib import calc, constants
except ImportError:
    from lib import calc, constants

try:
    from . import health as health_mod
except ImportError:
    import health as health_mod

__all__ = ["compute_core", "monthly_frame", "core_label"]


def monthly_frame(panel):
    """Месячный срез панели: значения компонентов, их z и форвардный месяц.

    Метка месяца — последний ТОРГОВЫЙ день (в валидации меткой был календарный конец
    месяца, но значение бралось то же — последнее непустое в месяце).
    Последний месяц, как правило, НЕЗАВЕРШЁН: его значение = сегодняшнее, и это
    ровно то, что показывается как «текущее дневное значение ядра».
    """
    dates = panel.get("dates") or []
    cols = panel.get("cols") or {}
    empty = [None] * len(dates)

    labels, px_m = calc.resample_month_end(dates, cols.get("imoex", empty))
    raw, z = {}, {}
    for comp in constants.CORE_COMPONENTS:
        cid = comp["id"]
        _, vals = calc.resample_month_end(dates, cols.get(cid, empty))
        raw[cid] = vals
        z[cid] = calc.zscore_rolling(vals, constants.Z_WINDOW_MONTHS,
                                     constants.Z_MIN_MONTHS, clip=constants.Z_CLIP)

    composite, n_used = [], []
    for i in range(len(labels)):
        s, n = 0.0, 0
        for comp in constants.CORE_COMPONENTS:
            v = z[comp["id"]][i]
            if calc.is_num(v):
                s += comp["sign"] * v
                n += 1
        composite.append(s / n if n else None)
        n_used.append(n)

    fwd1m = [None] * len(labels)
    for i in range(len(labels) - 1):
        a, b = px_m[i], px_m[i + 1]
        if calc.is_num(a) and calc.is_num(b) and a > 0 and b > 0:
            fwd1m[i] = math.log(b / a)

    return {"dates": labels, "raw": raw, "z": z, "composite": composite,
            "n_used": n_used, "imoex": px_m, "fwd1m": fwd1m}


def core_label(value):
    """Словесная метка композита по constants.CORE_LABELS."""
    if not calc.is_num(value):
        return "нет данных"
    for lo, hi, name in constants.CORE_LABELS:
        if lo <= value < hi:
            return name
    return constants.CORE_LABELS[-1][2] if value > 0 else constants.CORE_LABELS[0][2]


def _n(v, nd=1, plus=True):
    """Число с типографским минусом. Заменять минус во всей строке нельзя —
    в тексте живут дефисы вроде «80-й перцентиль» и «252-дневный»."""
    s = f"{v:+.{nd}f}" if plus else f"{v:.{nd}f}"
    return s.replace("-", "−")


def _fmt_raw(fmt, v):
    if not calc.is_num(v):
        return "нет данных"
    if fmt == "pct63":
        return f"{_n(v * 100)}% за 63д"
    if fmt == "pp":
        return f"{_n(v, 2)} п.п."
    if fmt == "pct":
        return f"{_n(v * 100)}%"
    return _n(v, 3)


def _round(v, nd=4):
    return round(v, nd) if calc.is_num(v) else None


def _run_start(series, dates, j):
    """Дата начала непрерывного отрезка с тем же значением, что в позиции j."""
    v = series[j]
    k = j
    while k - 1 >= 0 and series[k - 1] == v:
        k -= 1
    return dates[k]


def compute_core(panel, with_health=True):
    """-> {"value","sign","sign_since","components":[…],"series":[…],"health":{…}}"""
    mf = monthly_frame(panel)
    labels, comp_series = mf["dates"], mf["composite"]
    idx, value = calc.last_valid(comp_series)

    if idx is None:  # ни одной ноги с историей ≥24 месяцев — ядро молчит, прогон живёт
        out = {"value": None, "sign": 0, "sign_since": None, "label": "нет данных",
               "degraded": True, "n_components": 0,
               "n_expected": len(constants.CORE_COMPONENTS),
               "asof": labels[-1] if labels else None,
               "components": [_component_stub(c) for c in constants.CORE_COMPONENTS],
               "series": [], "month_end": None}
        if with_health:
            out["health"] = health_mod.compute_health(panel, mf=mf, sign_since=None)
        return out

    # Знак — с гистерезисом: |composite| должен пробить CORE_FLIP_HYSTERESIS, иначе
    # это дребезг около нуля, а не разворот. sign_since — дата начала текущего знака.
    hyst = calc.hysteresis_sign(comp_series, constants.CORE_FLIP_HYSTERESIS)
    sign = hyst[idx] or 0
    sign_since = _run_start(hyst, labels, idx) if hyst[idx] is not None else None

    n_used = mf["n_used"][idx]
    weight = round(1.0 / n_used, 4) if n_used else 0.0
    components = []
    for comp in constants.CORE_COMPONENTS:
        cid = comp["id"]
        zv, rawv = mf["z"][cid][idx], mf["raw"][cid][idx]
        spark = [[labels[j], _round(mf["z"][cid][j], 3)]
                 for j in range(max(0, idx - 23), idx + 1)
                 if calc.is_num(mf["z"][cid][j])]
        components.append({
            "id": cid, "label": comp["label"], "z": _round(zv, 3), "raw": _round(rawv, 6),
            "raw_fmt": _fmt_raw(comp.get("fmt"), rawv), "tier": comp.get("tier"),
            "sign": comp["sign"], "protected": bool(comp.get("protected")),
            "weight": weight if calc.is_num(zv) else 0.0,
            "contrib": _round(comp["sign"] * zv, 3) if calc.is_num(zv) else None,
            "available": calc.is_num(zv),
            "mechanism": comp.get("mechanism", ""), "evidence": comp.get("evidence", ""),
            "spark": spark,
        })

    # Значение последнего ЗАКРЫТОГО месяца: витрина показывает дневное число, но
    # решение принимается на месячном шаге, и сравнивать «сегодня» надо с ним.
    # Текущий месяц почти всегда неполон, поэтому ищем последнюю метку другого месяца.
    month_end = None
    cur_month = labels[idx][:7]
    for j in range(idx, -1, -1):
        if labels[j][:7] != cur_month and calc.is_num(comp_series[j]):
            month_end = {"date": labels[j], "value": _round(comp_series[j], 3),
                         "label": core_label(comp_series[j])}
            break

    out = {
        "value": _round(value, 3),
        "sign": sign,
        "sign_since": sign_since,
        "label": core_label(value),
        "asof": labels[idx],
        # degraded — когда ядро держится на одной ноге: usd_mom63 в одиночку до 2017
        # давал p=0,12, это «сигнал есть, доверия мало», а не полноценный композит.
        "degraded": n_used <= 1,
        "n_components": n_used,
        "n_expected": len(constants.CORE_COMPONENTS),
        "components": components,
        "month_end": month_end,
        "series": [[labels[i], _round(v, 3)] for i, v in enumerate(comp_series)
                   if calc.is_num(v) and labels[i] >= "2004-01-01"],
    }
    if with_health:
        out["health"] = health_mod.compute_health(panel, mf=mf, sign_since=sign_since)
    return out


def _component_stub(comp):
    return {"id": comp["id"], "label": comp["label"], "z": None, "raw": None,
            "raw_fmt": "нет данных", "tier": comp.get("tier"), "sign": comp["sign"],
            "protected": bool(comp.get("protected")), "weight": 0.0, "contrib": None,
            "available": False, "mechanism": comp.get("mechanism", ""),
            "evidence": comp.get("evidence", ""), "spark": []}
