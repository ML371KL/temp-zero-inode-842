"""Тень: сигналы, которые считаются каждый прогон и копят историю, но на позицию не влияют.

ПОЧЕМУ тень, а не ещё один ряд карточек. Аудит 02.09.2026 нашёл десяток кандидатов
с многообещающей статистикой на короткой выборке: IC +0,32 при n=73, «8 эпизодов с
2015», «IC +0,90 при n=17». Ни один не выдерживает планки, по которой сигнал попадает
в панель (валидация на 2004–2026 с поправкой на множественность), и ни один нельзя
честно перепроверить задним числом — данные молодые, а гипотезы найдены на них же.
Единственный способ узнать, стоят ли они чего-то, — считать их вслепую двенадцать
месяцев и потом сверить с фактом. Для этого блок пишет свои значения в стор рядами
shadow_<id> (registry, role="shadow"): история копится ВНЕ выборки, на которой
сигналы были найдены. Панель показывает их отдельным разделом с пометкой
constants.SHADOW_NOTE и никуда не подмешивает.

ПОЧЕМУ каждый сигнал в своём try. Тень — самая молодая часть панели и самая
зависимая от чужих рядов: платный HI2, календарь дивидендов, СЧА одного фонда.
Отказ одного не имеет права погасить остальные, а сам блок — уронить прогон
(run.py оборачивает compute_shadow ещё раз и при ошибке кладёт {"error": …}).

Теневая ПОЗИЦИЯ (shadow_position) — «пакет P2 плюс бит репрайсинга, читаемый на
конце месяца» (ступень P3m аудита). Автомат тот же, что у боевой позиции
(decision.run_automaton с массивом es), поэтому decision.py импортируется ЛЕНИВО:
модуль пишется параллельно, и пока его нет, тень считает всё остальное и честно
отдаёт {"status": "unavailable"} вместо позиции.

Стандартная библиотека и lib/calc — как во всём pipeline/ (docs/CONTRACT.md §0).
"""

import importlib
import math
from bisect import bisect_right
from datetime import date, timedelta

from pipeline.lib import calc, constants

__all__ = ["compute_shadow", "REPRICING_PP", "SIGNALS"]

# ---- пороги сигналов (аудит 02.09.2026, results/S_shadow). Это ГИПОТЕЗЫ, не
# калибровка: менять их можно, но тогда накопленная история shadow_<id> относится к
# другому правилу — стирать её или начинать новый ряд.
REPRICING_PP = 0.25          # Δ21 спреда (год ОФЗ − ключ) больше +0,25 п.п. → бит
BREADTH_EARLY = 0.40         # доля бумаг выше MA200 ниже 40 % → ранний бит ворот
VOLUME_Z, VOLUME_DD = 1.0, -0.10   # z120 лог-оборота > 1 при лог-просадке глубже −10 %
FUTOI_GROSS_Z = 1.0          # брутто-вовлечённость физлиц: z120 > 1
DIV_WINDOW_DAYS = 30         # горизонт окна отсечек и окна реинвеста
DIV_SEASON_MIN_GAP_PCT = 1.0   # суммарный гэп индекса за окно ≥ 1 % → сезон отсечек
DIV_LARGE_CUT_PCT = 0.5      # одна отсечка с гэпом индекса ≥ 0,5 % открывает окно реинвеста
ROTATION_DAYS = 5            # СЧА фонда падает столько дней подряд
ROTATION_SPREAD_PP = -2.0    # …при switch_spread выше −2 п.п.
HI2_MA, HI2_Z_WINDOW, HI2_Z_MIN = 21, 252, 126
GROSS_FFILL = 3              # как panel.FFILL_LIMITS["futoi"]
HISTORY_POINTS = 24          # месячных точек в истории каждого сигнала
POSITION_START = "2004-01-06"   # как у боевой позиции (спецификация §4)
YEARS_5 = 1260               # торговых дней для «смен в год за 5 лет»

DEFAULT_NOTE = ("Тень: считается и копит историю, на позицию не влияет "
                "(аудит 02.09.2026, наблюдение 12 месяцев)")

# id, подпись, функция (по имени — чтобы тесты могли подменить одну), что писать в
# стор ("value" | "state"), группа, единица.
SIGNALS = [
    ("repricing", "Репрайсинг ожиданий по ставке", "_sig_repricing", "value", "rates", "п.п."),
    ("usd_ma200", "Курс к своей MA200 (теневая нога)", "_sig_usd_ma200", "value", "legs", "z"),
    ("brent_usd_gap", "Brent к своей MA504, $ (теневая нога)", "_sig_brent_usd_gap",
     "value", "legs", "z"),
    ("hi2_nf21z", "Концентрация нетто-потока HI2", "_sig_hi2_nf21z", "value", "flows", "z"),
    ("breadth_early", "Ранний бит ширины рынка", "_sig_breadth_early", "state", "gate", "доля"),
    ("volume_capitulation", "Капитуляция по обороту", "_sig_volume_capitulation",
     "state", "retail_era", "z"),
    ("futoi_gross", "Брутто-вовлечённость физлиц в MX", "_sig_futoi_gross",
     "state", "retail_era", "z"),
    ("dividend_season", "Дивидендный сезон", "_sig_dividend_season",
     "value", "retail_era", "%"),
    ("rotation_trigger", "Большая ротация из фондов ликвидности", "_sig_rotation_trigger",
     "state", "retail_era", "млрд ₽"),
    ("trades_contrarian", "Число сделок тяжеловесов (контрариан)", "_sig_trades_contrarian",
     None, "retail_era", None),
]


# ------------------------------------------------------------------ утилиты
def _load(store, sid):
    """Ряд из стора любого вида (модуль, объект, словарь); None при любом сбое."""
    if store is None:
        return None
    getter = getattr(store, "load_series", None)
    if callable(getter):
        try:
            return getter(sid)
        except Exception:  # noqa: BLE001 — граница изоляции: битый ряд = «ряда нет»
            return None
    if isinstance(store, dict):
        return store.get(sid)
    return None


def _points(store, sid):
    ser = _load(store, sid)
    pts = (ser or {}).get("points") if isinstance(ser, dict) else None
    return pts if isinstance(pts, dict) else {}


def _points_sub(store, ids, subkeys):
    """Подряд: отдельный series_id (hi2_market_nf) либо словарь в точке — как panel._points_sub."""
    for sid in ids:
        pts = _points(store, sid)
        if not pts:
            continue
        sample = next(iter(pts.values()), None)
        if isinstance(sample, dict):
            for key in subkeys:
                if key in sample:
                    return {d: v.get(key) for d, v in pts.items() if isinstance(v, dict)}
        else:
            return pts
    return {}


def _sorted(points):
    """[(дата, float)] по возрастанию, только годные числа."""
    return sorted((d, float(v)) for d, v in points.items() if calc.is_num(v))


def _on_dates(points, dates, limit=None):
    """Словарь точек → список по календарю панели (точное совпадение + протяжка)."""
    exact = [float(points[d]) if calc.is_num(points.get(d)) else None for d in dates]
    return calc.ffill(exact, limit)


def _month_hist(labels, values, n=HISTORY_POINTS, nd=3):
    out = [[d, round(v, nd)] for d, v in zip(labels, values) if calc.is_num(v)]
    return out[-n:]


def _closed_month_end(dates):
    """Индекс последнего торгового дня последнего ЗАКРЫТОГО месяца или None."""
    cur = dates[-1][:7]
    for i in reversed(calc.month_end_indices(dates)):
        if dates[i][:7] != cur:
            return i
    return None


def _prev_month_key(key):
    y, m = int(key[:4]), int(key[5:7])
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


def _z_monthly(dates, leg):
    """Месячный z одной ноги ровно как в ядре: окно 60 мес, min 24, обрезка ±3.

    Последняя метка — незакрытый месяц, его значение = сегодняшнее; z этой точки и
    есть «z сегодня» (то же, что core.compute_core показывает как дневное число).
    """
    labels, vals = calc.resample_month_end(dates, leg)
    z = calc.zscore_rolling(vals, constants.Z_WINDOW_MONTHS, constants.Z_MIN_MONTHS,
                            clip=constants.Z_CLIP)
    return labels, vals, z


def _sig(value=None, asof=None, state=None, note="", history=None, **extra):
    out = {"value": value, "asof": asof, "state": state, "note": note,
           "history": history or []}
    out.update(extra)
    return out


def _r(v, nd=3):
    return round(float(v), nd) if calc.is_num(v) else None


# ------------------------------------------------------------------ сигналы
def _sig_repricing(ctx):
    """Бит «репрайсинг ожиданий»: спред год ОФЗ − ключ вырос больше чем на +0,25 п.п. за 21 день.

    Два состояния — дневное и на конце закрытого месяца: тестировалась (P3m)
    именно вторая форма, «бит читается на последнем торговом дне месяца и держится
    весь следующий», поэтому es для теневой позиции строится из неё.
    """
    dates, cols = ctx["dates"], ctx["cols"]
    n = len(dates)
    y1, key = cols.get("y1") or [None] * n, cols.get("key_rate") or [None] * n
    spread = [a - b if (calc.is_num(a) and calc.is_num(b)) else None for a, b in zip(y1, key)]
    d21 = calc.diff(spread, 21)
    bit = [None if not calc.is_num(v) else int(v > REPRICING_PP) for v in d21]
    j, v = calc.last_valid(d21)
    i_close = _closed_month_end(dates)
    state_me = bit[i_close] if i_close is not None else None
    # es: в месяце M действует бит последнего торгового дня месяца M−1.
    by_month = {dates[i][:7]: bit[i] for i in calc.month_end_indices(dates)}
    es = [int(by_month.get(_prev_month_key(d[:7])) or 0) for d in dates]
    ctx["es"] = es
    labels, vals = calc.resample_month_end(dates, d21)
    return _sig(value=_r(v), asof=dates[j] if j is not None else None,
                state=state_me, state_daily=bit[j] if j is not None else None,
                state_month_end=state_me,
                month_end_date=dates[i_close] if i_close is not None else None,
                threshold_pp=REPRICING_PP, spread_pp=_r(spread[j]) if j is not None else None,
                history=_month_hist(labels, vals),
                note="Δ21 спреда (год ОФЗ − ключ) больше +0,25 п.п. — детектор турбулентности; "
                     "бит читается на конце месяца и держится месяц (P3m). Как уровень спред "
                     "направление не предсказывает.")


def _leg(ctx, series, window, sign, label_note):
    dates = ctx["dates"]
    ma = calc.rolling_mean(series, window)
    leg = [math.log(a / b) if (calc.is_num(a) and calc.is_num(b) and a > 0 and b > 0) else None
           for a, b in zip(series, ma)]
    labels, _vals, z = _z_monthly(dates, leg)
    j, zv = calc.last_valid(z)
    k, raw = calc.last_valid(leg)
    return _sig(value=_r(zv), asof=labels[j] if j is not None else None, state=None,
                sign=sign, contrib=_r(sign * zv) if calc.is_num(zv) else None,
                raw=_r(raw, 5), raw_asof=dates[k] if k is not None else None,
                history=_month_hist(labels, z), note=label_note)


def _sig_usd_ma200(ctx):
    """Теневая нога: log(USD / MA200(USD)) вместо usd_mom63, z по месячному окну ядра, знак +."""
    n = len(ctx["dates"])
    return _leg(ctx, ctx["cols"].get("usd") or [None] * n, 200, +1,
                "Кандидат на замену usd_mom63: та же девальвационная механика, но к "
                "200-дневной средней; в композит не входит, сравнивается с ядром 12 месяцев.")


def _sig_brent_usd_gap(ctx):
    """Теневая нога: log(Brent / MA504(Brent)) в ДОЛЛАРАХ (не рублёвая бочка), знак −."""
    n = len(ctx["dates"])
    return _leg(ctx, ctx["cols"].get("brent") or [None] * n, 504, -1,
                "Долларовый гэп нефти к двухлетнему тренду (контрариан): проверяет, "
                "не была ли рублёвая бочка ядра сигналом курса, а не нефти.")


def _sig_hi2_nf21z(ctx):
    """Концентрация нетто-потока HI2: 21-дневное среднее nf, z по 252 дням (min 126), знак +."""
    nf = _points_sub(ctx["store"], ("hi2_market_nf", "hi2_market"), ("nf",))
    pts = _sorted(nf)
    note = ("IC к 21 дню +0,32 (n=73) по аудиту; ловит дно на 10–50 дней раньше ворот. "
            "Ряд платный (ALGOPACK), истории с 2020 — тень.")
    if not pts:
        return _sig(status="no_series", note="ряд hi2_market пуст: нужен ключ ALGOPACK "
                    "(env MOEX_ALGOPACK_TOKEN), бесплатного дублёра нет. " + note)
    days = [d for d, _ in pts]
    vals = [v for _, v in pts]
    nf21 = calc.rolling_mean(vals, HI2_MA)
    z = calc.zscore_rolling(nf21, HI2_Z_WINDOW, min_periods=HI2_Z_MIN)
    j, zv = calc.last_valid(z)
    labels, zm = calc.resample_month_end(days, z)
    if j is None:
        return _sig(status="warming", asof=days[-1], n_days=len(days),
                    note=f"истории {len(days)} дней, для z нужно {HI2_Z_MIN}. " + note)
    return _sig(value=_r(zv), asof=days[j], state=None, sign=+1, contrib=_r(zv),
                nf21=_r(nf21[j]), n_days=len(days), history=_month_hist(labels, zm),
                note=note)


def _sig_breadth_early(ctx):
    """Ранний бит ворот: доля бумаг выше MA200 ниже 40 %."""
    dates = ctx["dates"]
    b = ctx["cols"].get("breadth") or [None] * len(dates)
    j, v = calc.last_valid(b)
    labels, vals = calc.resample_month_end(dates, b)
    return _sig(value=_r(v), asof=dates[j] if j is not None else None,
                state=None if v is None else int(v < BREADTH_EARLY),
                threshold=BREADTH_EARLY, history=_month_hist(labels, vals),
                note="ранний бит ворот, 8 эпизодов с 2015 — тень; знак ширины как "
                     "сигнала зависит от эры (подтверждение до 2022, контрариан 2025–26).")


def _sig_volume_capitulation(ctx):
    """Маркер дна по объёму: z120 лог-оборота индекса > 1 при просадке глубже −10 %."""
    dates, cols, store = ctx["dates"], ctx["cols"], ctx["store"]
    val = _on_dates(_points(store, "imoex_value"), dates)
    lv = [math.log(v) if (calc.is_num(v) and v > 0) else None for v in val]
    z = calc.zscore_rolling(lv, 120, min_periods=60)
    dd = cols.get("dd252") or [None] * len(dates)
    both = [int(a > VOLUME_Z and b < VOLUME_DD) if (calc.is_num(a) and calc.is_num(b)) else None
            for a, b in zip(z, dd)]
    j, st = calc.last_valid(both)
    labels, zm = calc.resample_month_end(dates, z)
    return _sig(value=_r(z[j]) if j is not None else None,
                asof=dates[j] if j is not None else None, state=st,
                dd252=_r(dd[j]) if j is not None else None,
                history=_month_hist(labels, zm),
                note="маркер дна по объёму: вход в день дна в 2017/2020/2022, Шарп-нейтрален; "
                     "просадка — логарифмическая, как у ворот.")


def _sig_futoi_gross(ctx):
    """Брутто-вовлечённость физлиц в MX: (|long| + |short|), z120 > 1 → 1."""
    dates, store = ctx["dates"], ctx["store"]
    lng = _points_sub(store, ("futoi_mx_long", "futoi_mx"), ("pos_long", "fiz_pos_long"))
    sht = _points_sub(store, ("futoi_mx_short", "futoi_mx"), ("pos_short", "fiz_pos_short"))
    gross = {d: abs(float(a)) + abs(float(sht[d])) for d, a in lng.items()
             if calc.is_num(a) and calc.is_num(sht.get(d))}
    note = "IC в стрессе +0,90 при n=17 — гипотеза; брутто, а не нетто: важна вовлечённость."
    if not gross:
        return _sig(status="no_series", note="рядов futoi_mx_long/short нет. " + note)
    g = _on_dates(gross, dates, GROSS_FFILL)
    z = calc.zscore_rolling(g, 120, min_periods=60)
    j, zv = calc.last_valid(z)
    labels, zm = calc.resample_month_end(dates, z)
    return _sig(value=_r(zv), asof=dates[j] if j is not None else None,
                state=None if zv is None else int(zv > FUTOI_GROSS_Z),
                gross=_r(g[j], 0) if j is not None else None,
                history=_month_hist(labels, zm), note=note)


def _sig_dividend_season(ctx):
    """Календарный флаг: окно отсечек (гэп индекса ≥1 % за 30 дней) или окно реинвеста
    (30 дней после крупной отсечки). Значение — ожидаемый гэп индекса, %."""
    store, today = ctx["store"], ctx["today"]
    meta = ((_load(store, "dividends") or {}).get("meta") or {}) if store is not None else {}
    items = meta.get("items") if isinstance(meta.get("items"), list) else None
    note = ("календарь отсечек, статистики за этим нет: механический гэп индекса и "
            "возврат дивидендов в рынок — тень.")
    if not items:
        return _sig(status="no_data", note="в календаре дивидендов нет строк с весами. " + note)
    ahead, recent_large = 0.0, []
    for it in items:
        if not isinstance(it, dict):
            continue
        ex = str(it.get("ex_date") or it.get("date") or "")[:10]
        drag = it.get("index_drag_pct")
        if not ex or not calc.is_num(drag):
            continue
        try:
            ex_d = date.fromisoformat(ex)
        except ValueError:
            continue
        if today < ex_d <= today + timedelta(days=DIV_WINDOW_DAYS):
            ahead += float(drag)
        elif today - timedelta(days=DIV_WINDOW_DAYS) <= ex_d <= today and drag >= DIV_LARGE_CUT_PCT:
            recent_large.append(ex)
    if ahead >= DIV_SEASON_MIN_GAP_PCT:
        phase = "ex_window"
    elif recent_large:
        phase = "reinvest"
    else:
        phase = "off"
    return _sig(value=_r(ahead), asof=today.isoformat(), state=int(phase != "off"),
                phase=phase, reinvest_after=recent_large[-1] if recent_large else None,
                note=note)


def _sig_rotation_trigger(ctx):
    """«Большая ротация»: СЧА фонда ликвидности падает 5 дней подряд при росте индекса за те
    же дни и switch_spread выше −2 п.п. Истории у сигнала 16 точек — накапливаем."""
    dates, cols, store = ctx["dates"], ctx["cols"], ctx["store"]
    pts = _sorted(_points(store, "lqdt_aum"))
    note = ("накапливаем: в исследовании 16 точек, срабатываний не было; переток из фондов "
            "ликвидности в акции — гипотеза.")
    if len(pts) < ROTATION_DAYS + 1:
        return _sig(status="no_data", n_points=len(pts),
                    note=f"у ряда lqdt_aum {len(pts)} точек, нужно {ROTATION_DAYS + 1}. " + note)
    tail = pts[-(ROTATION_DAYS + 1):]
    falling = all(b < a for (_, a), (_, b) in zip(tail, tail[1:]))
    px = cols.get("imoex") or []
    keys = list(dates)

    def px_at(day):
        k = bisect_right(keys, day) - 1
        return px[k] if 0 <= k < len(px) else None

    p0, p1 = px_at(tail[0][0]), px_at(tail[-1][0])
    rising = calc.is_num(p0) and calc.is_num(p1) and p1 > p0
    _, sp = calc.last_valid(cols.get("switch_spread") or [])
    spread_ok = calc.is_num(sp) and sp > ROTATION_SPREAD_PP
    state = int(falling and rising and spread_ok)
    return _sig(value=_r(tail[-1][1] - tail[0][1], 1), asof=tail[-1][0], state=state,
                falling_days=sum(1 for (_, a), (_, b) in zip(tail, tail[1:]) if b < a),
                imoex_up=bool(rising), switch_spread_pp=_r(sp, 2), note=note)


def _sig_trades_contrarian(ctx):
    """Число сделок тяжеловесов (находка E аудита): ряда NUMTRADES в сторе нет — не заводим
    в этой итерации, сигнал честно говорит «no_series»."""
    return _sig(status="no_series",
                note="ряд NUMTRADES по SBER/LKOH/GAZP (ISS history) в сторе не заведён — "
                     "бесплатная находка E аудита 02.09.2026, ждёт своей итерации.")


# ------------------------------------------------------------- теневая позиция
def _decision_days(dates):
    """Последний торговый день каждой недели W-FRI (суббота открывает новую неделю)."""
    keys = []
    for d in dates:
        dt = date.fromisoformat(d[:10])
        keys.append(dt + timedelta(days=(4 - dt.weekday()) % 7))
    n = len(dates)
    return [i + 1 == n or keys[i + 1] != keys[i] for i in range(n)]


def _comp_state(comp, is_dec, thr):
    """Знак композита: обновляется ТОЛЬКО в день решения, с гистерезисом ±thr."""
    out, s = [], 0
    for v, dec in zip(comp, is_dec):
        if dec and calc.is_num(v):
            if v > thr:
                s = 1
            elif v < -thr:
                s = -1
        out.append(s)
    return out


def _as_list(obj, n):
    """daily_composite может отдать список или словарь с рядом — берём ряд длины n."""
    if isinstance(obj, dict):
        for key in ("composite", "values", "series", "daily"):
            if isinstance(obj.get(key), list) and len(obj[key]) == n:
                return obj[key]
        return None
    return obj if isinstance(obj, list) and len(obj) == n else None


def _rle(dates, pos, start):
    out, prev = [], None
    for d, p in zip(dates, pos):
        if d < start or p is None:
            continue
        if p != prev:
            out.append([d, int(p)])
            prev = p
    return out


def _shadow_position(ctx, decision):
    """Позиция по правилу «P2 + бит репрайсинга на конце месяца» тем же автоматом, что
    боевая. Всё чужое импортируется лениво и в try: тень не имеет права упасть из-за
    того, что decision.py ещё пишется или изменил сигнатуру."""
    dates, cols, panel = ctx["dates"], ctx["cols"], ctx["panel"]
    n = len(dates)
    try:
        # import_module, а не «from … import»: второй берёт атрибут пакета и не
        # смотрит в sys.modules — подменить decision в тесте было бы нельзя.
        dmod = importlib.import_module("pipeline.compute.decision")
    except ImportError as exc:
        return {"status": "unavailable", "reason": f"нет pipeline.compute.decision ({exc})"}
    run = getattr(dmod, "run_automaton", None)
    daily = getattr(dmod, "daily_composite", None)
    if not callable(run) or not callable(daily):
        return {"status": "unavailable",
                "reason": "в decision.py нет run_automaton/daily_composite"}
    from pipeline.compute import core as core_mod, states as states_mod
    mf = core_mod.monthly_frame(panel)
    comp = _as_list(daily(panel, mf), n)
    if comp is None:
        return {"status": "unavailable", "reason": "daily_composite отдал ряд не по календарю панели"}
    hyst = states_mod.hyst_bits(panel)
    raw_fn, gate_fn = getattr(states_mod, "_bits", None), getattr(states_mod, "gate_open_series", None)
    if callable(raw_fn) and callable(gate_fn):
        gate = gate_fn(raw_fn(dates, cols), hyst)
    else:
        gate = [None not in (t, v, b) and (t, v, b) != (0, 1, 1)
                for t, v, b in zip(hyst["trend"], hyst["vol"], hyst["bond"])]
    # Такт и знак — из decision.py, если он их экспортирует: теневая позиция обязана
    # отличаться от боевой ТОЛЬКО битом es, а не своей копией календаря недель.
    dec_fn = getattr(dmod, "decision_days", None)
    is_dec = dec_fn(dates) if callable(dec_fn) else _decision_days(dates)
    thr = (getattr(constants, "DECISION", None) or {}).get("comp_threshold", 0.20)
    cs_fn = getattr(dmod, "comp_state_series", None)
    comp_state = cs_fn(comp, is_dec, thr) if callable(cs_fn) else _comp_state(comp, is_dec, thr)
    if not (isinstance(comp_state, list) and len(comp_state) == n):
        comp_state = _comp_state(comp, is_dec, thr)
    es = ctx.get("es") or [0] * n
    pos, reasons = run(dates, gate, comp_state, is_dec, es)
    pos_main, _ = run(dates, gate, comp_state, is_dec, None)
    if not (isinstance(pos, list) and len(pos) == n):
        return {"status": "unavailable", "reason": "run_automaton отдал ряд не по календарю панели"}
    last = pos[-1]
    if last is None:
        return {"status": "unavailable", "reason": "позиция на последний день не определена"}
    k = n - 1
    while k - 1 >= 0 and pos[k - 1] == last:
        k -= 1
    reason = reasons[k] if (isinstance(reasons, list) and len(reasons) == n) else None
    main_block = decision if isinstance(decision, dict) else {}
    main_block = main_block.get("position", main_block) if isinstance(main_block, dict) else {}
    tail_pos = [p for p in pos[-YEARS_5:] if p is not None]
    switches = sum(1 for a, b in zip(tail_pos, tail_pos[1:]) if a != b)
    diff_days = sum(1 for a, b in zip(pos[-YEARS_5:], pos_main[-YEARS_5:])
                    if a is not None and b is not None and a != b)
    return {
        "status": "ok",
        "state": "long" if last else "flat",
        "since": dates[k],
        "reason": reason,
        "es_now": int(es[-1]),
        "main_state": main_block.get("state") if isinstance(main_block, dict) else None,
        "differs_from_main": bool(pos_main[-1] is not None and pos_main[-1] != last),
        "diff_days_5y": diff_days,
        "switches_per_year": round(switches / 5.0, 2),
        "history": _rle(dates, pos, POSITION_START),
        "note": "если бы бит репрайсинга (на конце месяца) был в решении: выход из акций "
                "любым днём при бите, возврат — в день решения после его снятия.",
    }


# ------------------------------------------------------------------- стор
def _persist(store, sid, sig, field, asof, unit):
    """Точка истории в ряд shadow_<id>: дата панели → значение или состояние."""
    fn = getattr(store, "upsert_points", None)
    if not callable(fn) or not asof or not field:
        return False
    v = sig.get(field)
    if not calc.is_num(v):
        return False
    try:
        fn(f"shadow_{sid}", {asof: float(v)},
           {"source": "shadow", "cadence": "derived", "status": "ok", "unit": unit,
            "note": f"{field} теневого сигнала {sid}; пишется compute/shadow.py"})
    except Exception:  # noqa: BLE001 — история не важнее блока
        return False
    return True


def _stored_history(store, sid, n=HISTORY_POINTS):
    """Накопленная в сторе история сигнала помесячно — для календарных сигналов, у
    которых прошлого из панели не восстановить."""
    pts = _sorted(_points(store, f"shadow_{sid}"))
    if not pts:
        return []
    labels, vals = calc.resample_month_end([d for d, _ in pts], [v for _, v in pts])
    return _month_hist(labels, vals, n)


# ------------------------------------------------------------------- вход
def compute_shadow(store, panel, states, decision, now):
    """-> {"note", "asof", "signals": [...], "shadow_position": {...}}.

    `now` — момент прогона (UTC); календарные сигналы считают «сегодня» по МСК от
    него, а не от часов машины: тесты замораживают время, прод передаёт своё.
    """
    dates = (panel or {}).get("dates") or []
    cols = (panel or {}).get("cols") or {}
    note = getattr(constants, "SHADOW_NOTE", DEFAULT_NOTE)
    if not dates:
        return {"note": note, "asof": None, "signals": [],
                "shadow_position": {"status": "unavailable", "reason": "панель пуста"}}
    asof = dates[-1]
    if now is not None:
        today = (now + timedelta(hours=constants.MSK_OFFSET_HOURS)).date()
    else:
        today = date.fromisoformat(asof)
    ctx = {"store": store, "panel": panel, "dates": dates, "cols": cols,
           "states": states, "today": today, "es": None}
    signals = []
    for sid, label, fn_name, field, group, unit in SIGNALS:
        try:
            sig = globals()[fn_name](ctx)
            sig.setdefault("status", "ok")
        except Exception as exc:  # noqa: BLE001 — граница изоляции сигнала
            sig = _sig(status="error", error=f"{type(exc).__name__}: {exc}"[:300])
        sig.update({"id": sid, "label": label, "group": group, "unit": unit})
        sig["history_saved"] = _persist(store, sid, sig, field, asof, unit)
        if not sig.get("history"):
            sig["history"] = _stored_history(store, sid)
        signals.append(sig)
    try:
        position = _shadow_position(ctx, decision)
    except Exception as exc:  # noqa: BLE001 — автомат чужой, падать за него нельзя
        position = {"status": "unavailable", "reason": f"{type(exc).__name__}: {exc}"[:300]}
    if position.get("status") == "ok":
        position["history_saved"] = _persist(
            store, "position", {"state_num": 1 if position["state"] == "long" else 0},
            "state_num", asof, "0/1")
    return {"note": note, "asof": asof, "signals": signals, "shadow_position": position}
