"""Слой 2 — машина состояний: 3 бита + фаза ставки (docs/CONTRACT.md §4, REGIME.md §2).

Состояния — это ВОРОТА РИСКА, а не веса. Непрерывная модуляция весов состоянием
(модель M3) проиграла walk-forward с OOS IC −0,002; бинарные ворота на уровне
отдельных сигналов — работают. Отсюда вся конструкция файла: биты грубые (тоньше
не позволяет выборка), а решения принимаются по ячейке (trend, vol, bond).

Главное правило, ради которого машина и строилась: шип волатильности покупаем,
ТОЛЬКО если облигации не падают вместе с акциями (+1,4…+3,8%/мес против −2,9%/мес
в тройной токсичной ячейке).

Пороги и статистика ячеек — из constants.py, здесь они не пересчитываются:
CELL_STATS получены на 2004–2026 и меняются только реколибровкой.

ДВА НАБОРА БИТОВ живут рядом с 02.09.2026, и это не дублирование:
  * СЫРЫЕ биты (`_bits`) — «цена выше MA200», «вола выше p80», «RGBI глубже −4 %».
    На них посчитаны CELL_STATS, лента ячеек и второй ряд; трогать их нельзя.
  * биты С ГИСТЕРЕЗИСОМ (`_bits_hyst`) — те же три флага, но с двумя порогами:
    включаются как прежде, а снимаются позже (тренд ±2 % к MA200, вола ниже p60,
    RGBI выше −3 %). На них стоят ВОРОТА позиции (compute/decision.py): без
    гистерезиса ворота хлопали на каждом касании порога, и аудит 02.09.2026 (S2_1)
    показал, что дребезг стоил больше, чем опоздание на выходе.
"""

import math

try:
    from ..lib import calc, constants, dates as datelib
except ImportError:
    from lib import calc, constants, dates as datelib

__all__ = ["compute_states", "cell_code", "gate_open_series", "regime_of", "hyst_bits"]

# Левая граница статистики режимов — окно валидации (как CELL_STATS и health).
STATS_START = "2004-01-01"
# Токсичное сочетание флагов (trend, vol, bond): единственное, при котором ворота
# закрыты, — cells режима toxic в constants.REGIMES.
TOXIC = (0, 1, 1)

# Порог для словесного вердикта сигнала второго ряда. Это ВИТРИНА, а не статистика:
# |z| > 0.5 по 252-дневному окну = «заметно отклонился от своей нормы».
VERDICT_Z = 0.5
SIGNAL_Z_WINDOW = 252
SIGNAL_Z_MIN = 120


def _bit_word(name, v):
    """Человеческое слово для бита (для текста «почему сигнал включён»)."""
    if v is None:
        return f"{constants.STATE_RULES[name]['label']}: нет данных"
    if name == "rate_phase":
        return f"{constants.STATE_RULES[name]['label']}: " \
               f"{constants.STATE_RULES[name]['values'].get(v, '?')}"
    words = {"trend": ("медведь", "бык"), "vol": ("спокойно", "стресс"),
             "bond": ("ок", "стресс")}
    return f"{constants.STATE_RULES[name]['label']}: {words[name][int(v)]}"


def cell_code(trend, vol, bond):
    """'bear|stress|ok' — код ячейки в терминах constants.STATE_RULES."""
    parts = []
    for name, bit in (("trend", trend), ("vol", vol), ("bond", bond)):
        rule = constants.STATE_RULES[name]
        parts.append(rule["on"] if bit else rule["off"])
    return "|".join(parts)


def _n(v, nd=1, plus=False):
    """Число с типографским минусом И РУССКОЙ ЗАПЯТОЙ.

    Менять минус во всей строке нельзя: рядом живут дефисы «80-й перцентиль» и
    «252-дневный максимум» — они не минусы. Поэтому правится только само число.

    Запятая добавлена 14.08.2026: эти подписи — единственное место, где панель
    писала «RGBI −6.2%», тогда как соседние блоки той же страницы показывают
    «+3,78%» и «2 232,14». Строка уезжает ещё и в телеграм, где рядом с ней стоят
    числа из общего словаря формулировок, — разнобой был виден в одном сообщении.
    """
    s = f"{v:+.{nd}f}" if plus else f"{v:.{nd}f}"
    return s.replace("-", "−").replace(".", ",")


def _bits(dates, cols):
    """Четыре ряда состояний по всей истории.

    None — «бит не определён» (не набралось окно, нет источника). Валидация
    подставляла тут 0 через .astype(float), но на витрине «нет данных» и «спокойно»
    — разные вещи, поэтому ленты состояний за такие дни просто не рисуем.
    """
    n = len(dates)
    empty = [None] * n
    px = cols.get("imoex", empty)
    ma = cols.get("ma200", empty)
    rv = cols.get("realized_vol_21", empty)
    thr = cols.get("vol_thresh80", empty)
    dd = cols.get("rgbi_dd", empty)

    trend = [None if not (calc.is_num(p) and calc.is_num(m)) else int(p > m)
             for p, m in zip(px, ma)]
    vol = [None if not (calc.is_num(a) and calc.is_num(b)) else int(a > b)
           for a, b in zip(rv, thr)]
    thresh = constants.STATE_RULES["bond"]["threshold"]
    bond = [None if not calc.is_num(v) else int(v < thresh) for v in dd]
    return {"trend": trend, "vol": vol, "bond": bond,
            "rate_phase": _rate_phase(cols.get("key_rate", empty))}


def _hyst(x, init, on, off, greater):
    """Бит с двумя порогами (семантика P_lib.Market.bit_hyst аудита 02.09.2026).

    Состояние стартует из СЫРОГО бита в первый день, где определены и сырьё, и бит:
    выдумывать стартовое состояние из одного порога значило бы включить флаг там, где
    прод его не показывал, и ворота на первом же дне истории разошлись бы с ячейкой.
    Дни без сырья держат прежнее значение (None до старта): флаг — это состояние, а
    не наблюдение, и пропуск в ряду его не снимает.
    """
    out, cur = [], None
    for v, b in zip(x, init):
        if not calc.is_num(v):
            out.append(cur)
            continue
        if cur is None:
            if b is not None:
                cur = int(b)
            else:
                cur = int((v > on) if greater else (v < on))
        if greater:
            if v > on:
                cur = 1
            elif v < off:
                cur = 0
        else:
            if v < on:
                cur = 1
            elif v > off:
                cur = 0
        out.append(cur)
    return out


def _bits_hyst(dates, cols, raw=None):
    """Три флага ворот с гистерезисом — ровно как P_lib.Market.bits(spec=hyst).

    Порог ВКЛЮЧЕНИЯ у каждого флага тот же, что у сырого бита (ячейки и ворота
    зажигаются одновременно), порог ВЫКЛЮЧЕНИЯ — дальше: тренд снимается ниже −2 % к
    MA200, вола — ниже 60-го перцентиля, RGBI — выше −3 %. Колонки vol_thresh60 и
    px_ma_ratio считает панель; если панель старая (фикстуры тестов), считаем их тут
    тем же способом — иначе ворота на такой панели были бы вечно закрыты.
    """
    n = len(dates)
    empty = [None] * n
    raw = raw or _bits(dates, cols)
    d = constants.DECISION
    px, ma = cols.get("imoex", empty), cols.get("ma200", empty)
    ratio = cols.get("px_ma_ratio")
    if not ratio:
        ratio = [None if not (calc.is_num(p) and calc.is_num(m) and m > 0) else p / m - 1.0
                 for p, m in zip(px, ma)]
    band = d["trend_band"]
    trend = _hyst(ratio, raw["trend"], band, -band, True)

    rv = cols.get("realized_vol_21", empty)
    q80 = cols.get("vol_thresh80", empty)
    q60 = cols.get("vol_thresh60")
    if not q60:
        q60 = calc.rolling_quantile(rv, constants.STATE_RULES["vol"]["lookback"],
                                    d["vol_off_quantile"], min_periods=252)
    vol, cur = [], None
    for a, hi, lo, b in zip(rv, q80, q60, raw["vol"]):
        if not (calc.is_num(a) and calc.is_num(hi) and calc.is_num(lo)):
            vol.append(cur)
            continue
        if cur is None:
            cur = int(b) if b is not None else int(a > hi)
        if a > hi:
            cur = 1
        elif a < lo:
            cur = 0
        vol.append(cur)

    bond = _hyst(cols.get("rgbi_dd", empty), raw["bond"], d["bond_on"], d["bond_off"], False)
    return {"trend": trend, "vol": vol, "bond": bond}


def hyst_bits(panel):
    """Публичная обёртка для слоя решения: биты с гистерезисом по всей панели."""
    dates = panel.get("dates") or []
    cols = panel.get("cols") or {}
    return _bits_hyst(dates, cols)


def gate_open_series(raw, hyst):
    """Ворота по дням: True — открыты, False — закрыты (в том числе «нет данных»).

    Закрыты, когда флаги с гистерезисом стоят в токсичном сочетании (0,1,1) ИЛИ
    когда хотя бы один флаг — сырой или с гистерезисом — не определён в этот день.
    Второе условие — cell_ok из эталона аудита: мёртвый ряд RGBI не имеет права
    держать ворота открытыми (аудит 18.08.2026 ловил ровно это на сырых битах).
    """
    out = []
    for t in range(len(hyst["trend"])):
        h = (hyst["trend"][t], hyst["vol"][t], hyst["bond"][t])
        r = (raw["trend"][t], raw["vol"][t], raw["bond"][t])
        if None in h or None in r:
            out.append(False)
        else:
            out.append(h != TOXIC)
    return out


def regime_of(trend, vol, bond):
    """Режим (constants.REGIMES) по трём флагам; None, если флаг не определён."""
    if None in (trend, vol, bond):
        return None
    key = (int(trend), int(vol), int(bond))
    for reg in constants.REGIMES:
        if key in [tuple(c) for c in reg["cells"]]:
            return reg
    return None


def _rate_phase(key):
    """Знак ПОСЛЕДНЕГО изменения ключевой ставки: −1 смягчение / +1 ужесточение.

    Ноль («пауза») не выставляем: в валидации фаза = np.sign последнего ненулевого
    изменения, и маска «easing» это st_rate == −1. Если начать помечать паузы нулём,
    ворота switch_spread закроются в самой интересной части цикла.
    """
    out = []
    cur, prev = None, None
    for v in key:
        if calc.is_num(v):
            if prev is not None and v != prev:
                cur = 1 if v > prev else -1
            prev = v
        out.append(cur)
    return out


def _run_start(series, dates, j):
    v = series[j]
    k = j
    while k - 1 >= 0 and series[k - 1] == v:
        k -= 1
    return dates[k]


def _last_z(series):
    """(индекс, последнее значение, его z по окну 252 дня) — окно как у витринных сигналов."""
    j, v = calc.last_valid(series or [])
    if j is None:
        return None, None, None
    return j, v, calc.zscore_last(series[:j + 1], SIGNAL_Z_WINDOW, SIGNAL_Z_MIN)


def _days_between(a, b):
    """Календарных дней между двумя датами панели; None вместо исключения.

    Считает lib/dates, а не datetime тут же: там разбор дат один на весь проект.
    Глушим ошибку сознательно — возраст числа это подпись на витрине, и кривая дата
    в одном ряду не имеет права ронять прогон (контракт §0)."""
    try:
        return datelib.days_between(a, b)
    except (TypeError, ValueError, AttributeError):
        return None


def compute_states(panel):
    """-> {"current","since","cell","distances","active_signals","cells","series",
           "gate","regime","regime_stats","series_gate"}"""
    dates = panel.get("dates") or []
    cols = panel.get("cols") or {}
    if not dates:
        return {"current": {}, "since": {}, "cell": None, "distances": [],
                "active_signals": [], "cells": [], "series": [],
                "gate": {}, "regime": None, "regime_stats": {}, "series_gate": []}

    bits = _bits(dates, cols)
    hyst = _bits_hyst(dates, cols, bits)
    current, since, bit_asof = {}, {}, {}
    for name, series in bits.items():
        j, v = calc.last_valid(series)
        current[name] = None if j is None else int(v)
        since[name] = None if j is None else _run_start(series, dates, j)
        # Дата ПОСЛЕДНЕГО НАБЛЮДЕНИЯ, на котором бит стоит. Ряд-питатель живёт с
        # лимитом протяжки (rgbi_dd — 5 строк), но last_valid дальше лимита честно
        # возвращает значение произвольной давности: умерший ряд RGBI показывал
        # «облигации спокойны» как ТЕКУЩЕЕ состояние без всякой датировки, и
        # ворота стояли открытыми, пока RGBI реально падал (аудит 18.08.2026).
        # Модель не трогаем — это подпись: витрина обязана показать возраст бита.
        bit_asof[name] = None if j is None else dates[j]

    era_post22 = dates[-1] >= constants.ERA_POST22_START
    current["era_post22"] = era_post22
    # since дублируется и внутрь current: §3 контракта кладёт его в states.current.since,
    # §4 — рядом. Дублировать три даты дешевле, чем спорить с фронтом.
    current["since"] = dict(since)
    current["bit_asof"] = bit_asof

    key = (current["trend"], current["vol"], current["bond"])
    cell = None
    if None not in key:
        stats = constants.CELL_STATS.get(key, {})
        cell = {
            "key": list(key),
            "code": cell_code(*key),
            "label": stats.get("label", "неизвестная ячейка"),
            "stats": {k: stats[k] for k in ("mean_fwd1m_pct", "n", "hit") if k in stats},
            "rule": constants.CELL_RULES.get(key, ""),
            "words": [_bit_word("trend", key[0]), _bit_word("vol", key[1]),
                      _bit_word("bond", key[2]), _bit_word("rate_phase", current["rate_phase"])],
        }

    gate = _gate_block(dates, bits, hyst)
    reg = regime_of(gate["trend"], gate["vol"], gate["bond"])
    regime = None if reg is None else {
        "id": reg["id"], "label": reg["label"],
        "cells_in_regime": [cell_code(*c) for c in reg["cells"]],
    }
    return {
        "current": current,
        "since": since,
        "cell": cell,
        "distances": _distances(dates, cols, current, gate),
        "active_signals": _active_signals(dates, cols, current),
        "cells": _cells(key),
        "series": _ribbon(dates, bits),
        # Ворота позиции: те же три флага, но с гистерезисом (см. шапку модуля).
        "gate": gate,
        "regime": regime,
        "regime_stats": _regime_stats(dates, cols, hyst, reg["id"] if reg else None),
        "series_gate": _ribbon(dates, hyst),
        "asof": dates[-1],
    }


def _gate_block(dates, raw, hyst):
    """Текущее состояние ворот: три флага с гистерезисом, с какого дня, открыты ли.

    Флаг с гистерезисом после старта не бывает None (пропуск в ряду держит прежнее
    значение), поэтому «текущее» — просто последнее. А вот «открыты» считается по
    gate_open_series и учитывает сырые биты этого дня: мёртвый ряд закрывает ворота.
    """
    n = len(dates)
    cur, since = {}, {}
    for name in ("trend", "vol", "bond"):
        v = hyst[name][-1]
        cur[name] = None if v is None else int(v)
        since[name] = None if v is None else _run_start(hyst[name], dates, n - 1)
    open_series = gate_open_series(raw, hyst)
    key = (cur["trend"], cur["vol"], cur["bond"])
    d = constants.DECISION
    return {
        "trend": cur["trend"], "vol": cur["vol"], "bond": cur["bond"],
        "open": bool(open_series[-1]),
        "open_since": _run_start(open_series, dates, n - 1),
        "since": since,
        "cell_code": cell_code(*key) if None not in key else None,
        "thresholds": {
            "trend_band": d["trend_band"],
            "vol_on_quantile": constants.STATE_RULES["vol"]["quantile"],
            "vol_off_quantile": d["vol_off_quantile"],
            "bond_on": d["bond_on"], "bond_off": d["bond_off"],
        },
        "asof": dates[-1],
    }


# t-квантиль 0,975 для малых выборок; дальше — разложение Корниша–Фишера по z.
_T975_SMALL = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571,
               6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228}


def _t975(df):
    """Квантиль t-распределения без scipy: таблица до 10 степеней свободы, дальше
    разложение по нормальному квантилю (ошибка < 0,001 при df ≥ 11)."""
    if df < 1:
        return None
    if df <= 10:
        return _T975_SMALL[df]
    z = 1.959964
    g1 = (z ** 3 + z) / 4.0
    g2 = (5 * z ** 5 + 16 * z ** 3 + 3 * z) / 96.0
    g3 = (3 * z ** 7 + 19 * z ** 5 + 17 * z ** 3 - 15 * z) / 384.0
    g4 = (79 * z ** 9 + 776 * z ** 7 + 1482 * z ** 5 - 1920 * z ** 3 - 945 * z) / 92160.0
    return z + g1 / df + g2 / df ** 2 + g3 / df ** 3 + g4 / df ** 4


def _summary(xs):
    """Среднее, медиана, доля плюсовых и 95 % интервал среднего — в процентах.

    Всё в ЛОГАРИФМИЧЕСКОЙ мере ×100, как fwd1m ядра и mean_fwd1m_pct в CELL_STATS:
    смешивать здесь простые проценты с логарифмическими значило бы повторить
    ловушку, описанную над таблицей ячеек в constants.py.
    """
    n = len(xs)
    if not n:
        return {"n": 0, "mean_pct": None, "median_pct": None, "hit": None, "ci95_pct": None}
    mean = sum(xs) / n
    s = sorted(xs)
    median = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0
    hit = sum(1 for x in xs if x > 0) / n
    ci = None
    if n >= 2:
        sd = math.sqrt(sum((x - mean) ** 2 for x in xs) / (n - 1))
        t = _t975(n - 1)
        half = t * sd / math.sqrt(n)
        ci = [round((mean - half) * 100.0, 2), round((mean + half) * 100.0, 2)]
    return {"n": n, "mean_pct": round(mean * 100.0, 2), "median_pct": round(median * 100.0, 2),
            "hit": round(hit, 3), "ci95_pct": ci}


def _regime_stats(dates, cols, hyst, current_id):
    """Форвардный месяц по трём режимам — по ЦЕНЕ и по ИЗБЫТКУ НАД КЭШЕМ.

    Срез — флаги с гистерезисом на последний торговый день месяца, только ЗАКРЫТЫЕ
    месяцы (две последние пары отброшены, как в health: форвард последнего месяца
    смотрит в будущее). Ставка кэша — вклады топ-10 (cols["deposit"]) на конец
    месяца, /100/12; месяцы без ставки в избытке пропущены, поэтому n у «excess»
    меньше — это честнее, чем подставить ноль и назвать его ставкой.
    """
    empty = [None] * len(dates)
    me = calc.month_end_indices(dates)
    labels, px_m = calc.resample_month_end(dates, cols.get("imoex", empty))
    _, dep_m = calc.resample_month_end(dates, cols.get("deposit", empty))
    by = {r["id"]: {"price": [], "excess": []} for r in constants.REGIMES}
    for i in range(max(0, len(me) - 2)):
        if labels[i] < STATS_START:
            continue
        j = me[i]
        reg = regime_of(hyst["trend"][j], hyst["vol"][j], hyst["bond"][j])
        if reg is None:
            continue
        a, b = px_m[i], px_m[i + 1]
        if not (calc.is_num(a) and calc.is_num(b) and a > 0 and b > 0):
            continue
        fwd = math.log(b / a)
        by[reg["id"]]["price"].append(fwd)
        if calc.is_num(dep_m[i]):
            by[reg["id"]]["excess"].append(fwd - dep_m[i] / 100.0 / 12.0)
    out = {}
    for reg in constants.REGIMES:
        out[reg["id"]] = {
            "id": reg["id"], "label": reg["label"],
            "cells": [cell_code(*c) for c in reg["cells"]],
            "current": reg["id"] == current_id,
            "price": _summary(by[reg["id"]]["price"]),
            "excess": _summary(by[reg["id"]]["excess"]),
        }
    return out


def _last_pair(a, b):
    """Последний день, где ОБА ряда определены.

    Брать последние значения по отдельности нельзя: сравнивать сегодняшнюю цену с
    двухсотдневной средней месячной давности — это выдуманное расстояние до порога.
    """
    a, b = a or [], b or []
    for i in range(min(len(a), len(b)) - 1, -1, -1):
        if calc.is_num(a[i]) and calc.is_num(b[i]):
            return a[i], b[i]
    return None, None


def _vol_off_series(dates, cols):
    """Порог выключения волы (p60): из панели, а на старой панели — считаем тут же."""
    q60 = cols.get("vol_thresh60")
    if q60:
        return q60
    return calc.rolling_quantile(cols.get("realized_vol_21") or [None] * len(dates),
                                 constants.STATE_RULES["vol"]["lookback"],
                                 constants.DECISION["vol_off_quantile"], min_periods=252)


def _distances(dates, cols, current, gate=None):
    """Насколько далеко до переключения каждого бита — в числах и словами.

    С 02.09.2026 у каждой оси ДВА порога: `threshold` — включение (на нём стоит
    сырой бит и ячейка), `off_threshold` — выключение флага ворот (гистерезис).
    Витрина показывает второй для включённых флагов: читателю, который ждёт открытия
    ворот, важно не «где флаг зажёгся», а «где он погаснет».
    """
    out = []
    gate = gate or {}
    band = constants.DECISION["trend_band"] * 100.0

    px, ma = _last_pair(cols.get("imoex"), cols.get("ma200"))
    if calc.is_num(px) and calc.is_num(ma) and ma > 0:
        pct = (px / ma - 1.0) * 100.0
        side = "выше" if pct >= 0 else "ниже"
        out.append({
            "id": "trend", "label": constants.STATE_RULES["trend"]["label"],
            "value": round(pct, 2), "threshold": 0.0, "gap_pct": round(abs(pct), 2),
            "text": f"индекс на {_n(abs(pct))}% {side} MA200 "
                    f"({px:.0f} против {ma:.0f})",
            "on_threshold": round(band, 2), "off_threshold": round(-band, 2),
            "text_off": f"для ворот рост засчитывается выше +{_n(band, 0)}% к MA200, "
                        f"снижение — ниже −{_n(band, 0)}%; между ними прежнее состояние",
        })

    rv, thr = _last_pair(cols.get("realized_vol_21"), cols.get("vol_thresh80"))
    if calc.is_num(rv) and calc.is_num(thr):
        gap = (rv - thr) * 100.0
        side = "выше" if gap >= 0 else "ниже"
        row = {
            "id": "vol", "label": constants.STATE_RULES["vol"]["label"],
            "value": round(rv * 100, 1), "threshold": round(thr * 100, 1),
            "gap_pct": round(abs(gap), 1),
            "text": f"реализованная вола {_n(rv * 100)}% — на {_n(abs(gap))} п.п. "
                    f"{side} порога {_n(thr * 100)}% (80-й перцентиль за 3 года)",
        }
        _, q60 = _last_pair(cols.get("realized_vol_21"), _vol_off_series(dates, cols))
        if calc.is_num(q60):
            row["off_threshold"] = round(q60 * 100, 1)
            row["text_off"] = (f"для ворот флаг снимается ниже {_n(q60 * 100)}% "
                               f"(60-й перцентиль за 3 года)")
        out.append(row)

    _, dd = calc.last_valid(cols.get("rgbi_dd") or [])
    if calc.is_num(dd):
        # rgbi_dd — ЛОГАРИФМИЧЕСКАЯ просадка (так её считала валидация, и на этой мере
        # стоят CELL_STATS — бит трогать нельзя). Но пользователь читает «−5,9% от
        # максимума» как просадку котировки, а котировка просела на −5,7%; в 2022-м
        # разрыв доходил до 6 п.п. (−37,5% лог против −31,3% фактических). Поэтому в
        # витрину отдаём обычные проценты — И значение, И порог, чтобы сравнение
        # осталось честным: exp монотонна, момент переключения флага не сдвигается.
        thr_pct = (math.exp(constants.STATE_RULES["bond"]["threshold"]) - 1.0) * 100.0
        off_pct = (math.exp(constants.DECISION["bond_off"]) - 1.0) * 100.0
        val = (math.exp(dd) - 1.0) * 100.0
        verb = "снимется" if current.get("bond") else "включится"
        out.append({
            "id": "bond", "label": constants.STATE_RULES["bond"]["label"],
            "value": round(val, 1), "threshold": round(thr_pct, 1),
            "gap_pct": round(abs(val - thr_pct), 1),
            "text": f"RGBI {_n(val)}% от 252-дневного максимума; "
                    f"флаг {verb} при {_n(thr_pct)}%",
            "off_threshold": round(off_pct, 1),
            "text_off": f"для ворот флаг снимается выше {_n(off_pct)}% "
                        f"(включается ниже {_n(thr_pct)}%)",
        })
    return out


def _gate_ok(cond, current):
    for k, want in (cond or {}).items():
        if k == "era":
            if want == "post22" and not current.get("era_post22"):
                return False
            continue
        if current.get(k) != want:
            return False
    return True


def _gate_text(cond):
    parts = []
    for k, want in (cond or {}).items():
        if k == "era":
            parts.append("эра: после 2022")
        else:
            parts.append(_bit_word(k, want))
    return ", ".join(parts)


def _active_signals(dates, cols, current):
    """Сигналы второго ряда, включённые ТЕКУЩЕЙ ячейкой (constants.SECOND_LAYER).

    Вердикт («за лонг» / «против лонга») — витринная нормировка: знак сигнала,
    умноженный на его z по 252 дням. Это НЕ прогноз доходности и не то, на чём
    считался IC: у сигналов второго ряда доказано направление в своём состоянии,
    а не сила на конкретном уровне.

    Каждая запись датируется (asof/lag_days), и вот почему это не косметика:
    futoi_z120 живёт на бесплатном ISS, который публикует позицию физлиц с задержкой
    ~14 дней, а FFILL тянет последнее наблюдение ещё на 3 торговых дня — показанное
    число вообще не соответствует ни одному наблюдению, но подписано «сейчас».
    ВАЖНО: вердикт по возрасту НЕ гасим. SLA источника (registry: iss_daily = 26 ч)
    меряет свежесть ВЫКАЧКИ, а не возраст данных; порог по нему срабатывал бы всегда
    и навсегда убрал бы futoi с панели в 60% месяцев истории — это не датирование,
    а удаление сигнала. Дело витрины — показать дату, дело пользователя — учесть её.
    asof — последний день ПАНЕЛИ, где сигнал определён; у протянутых ffill'ом рядов
    он на несколько дней новее, чем asof самого источника на тайле монитора.
    """
    last_day = dates[-1] if dates else None
    out = []
    for sig in constants.SECOND_LAYER:
        active = _gate_ok(sig.get("when"), current)
        via = sig.get("when")
        if not active and sig.get("alt_when"):
            active = _gate_ok(sig["alt_when"], current)
            via = sig["alt_when"] if active else via
        if not active:
            continue
        j, value, z = _last_z(cols.get(sig["id"]))
        asof = dates[j] if (j is not None and j < len(dates)) else None
        lag_days = _days_between(asof, last_day) if (asof and last_day) else None
        contrib = sig["sign"] * z if calc.is_num(z) else None
        if contrib is None:
            verdict = "нет данных"
        elif contrib > VERDICT_Z:
            verdict = "за лонг"
        elif contrib < -VERDICT_Z:
            verdict = "против лонга"
        else:
            verdict = "нейтрально"
        out.append({
            "id": sig["id"], "label": sig["label"], "tier": sig.get("tier"),
            "sign": sig["sign"],
            "value": round(value, 4) if calc.is_num(value) else None,
            "z": round(z, 2) if calc.is_num(z) else None,
            "asof": asof,
            "lag_days": lag_days,
            "verdict": verdict,
            "why": sig.get("why", ""),
            "gate": _gate_text(via),
        })
    return out


def _cells(key):
    """Все восемь ячеек с исторической статистикой — для таблицы «где мы сейчас»."""
    out = []
    for k, st in constants.CELL_STATS.items():
        out.append({
            "key": list(k), "code": cell_code(*k), "label": st.get("label"),
            "mean_fwd1m_pct": st.get("mean_fwd1m_pct"), "n": st.get("n"),
            "hit": st.get("hit"), "current": (k == key),
        })
    out.sort(key=lambda r: r["mean_fwd1m_pct"] if r["mean_fwd1m_pct"] is not None else 0,
             reverse=True)
    return out


def _ribbon(dates, bits, start="2004-01-01"):
    """Лента состояний: по одной точке на месяц (последний торговый день).

    Дневная лента с 2004 — это 5,6 тыс. точек и лишние 100+ КБ в data.json при
    лимите 250 КБ; месячный шаг совпадает с шагом, на котором считалась статистика
    ячеек, поэтому лента и таблица говорят об одном и том же.
    """
    out = []
    for i in calc.month_end_indices(dates):
        if dates[i] < start:
            continue
        t, v, b = bits["trend"][i], bits["vol"][i], bits["bond"][i]
        if None in (t, v, b):
            continue
        out.append([dates[i], cell_code(t, v, b)])
    return out
