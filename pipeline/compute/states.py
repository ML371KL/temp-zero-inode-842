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
"""

import math

try:
    from ..lib import calc, constants, dates as datelib
except ImportError:
    from lib import calc, constants, dates as datelib

__all__ = ["compute_states", "cell_code"]

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
    """-> {"current","since","cell","distances","active_signals","cells","series"}"""
    dates = panel.get("dates") or []
    cols = panel.get("cols") or {}
    if not dates:
        return {"current": {}, "since": {}, "cell": None, "distances": [],
                "active_signals": [], "cells": [], "series": []}

    bits = _bits(dates, cols)
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

    return {
        "current": current,
        "since": since,
        "cell": cell,
        "distances": _distances(cols, current),
        "active_signals": _active_signals(dates, cols, current),
        "cells": _cells(key),
        "series": _ribbon(dates, bits),
        "asof": dates[-1],
    }


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


def _distances(cols, current):
    """Насколько далеко до переключения каждого бита — в числах и словами."""
    out = []

    px, ma = _last_pair(cols.get("imoex"), cols.get("ma200"))
    if calc.is_num(px) and calc.is_num(ma) and ma > 0:
        pct = (px / ma - 1.0) * 100.0
        side = "выше" if pct >= 0 else "ниже"
        out.append({
            "id": "trend", "label": constants.STATE_RULES["trend"]["label"],
            "value": round(pct, 2), "threshold": 0.0, "gap_pct": round(abs(pct), 2),
            "text": f"индекс на {_n(abs(pct))}% {side} MA200 "
                    f"({px:.0f} против {ma:.0f})",
        })

    rv, thr = _last_pair(cols.get("realized_vol_21"), cols.get("vol_thresh80"))
    if calc.is_num(rv) and calc.is_num(thr):
        gap = (rv - thr) * 100.0
        side = "выше" if gap >= 0 else "ниже"
        out.append({
            "id": "vol", "label": constants.STATE_RULES["vol"]["label"],
            "value": round(rv * 100, 1), "threshold": round(thr * 100, 1),
            "gap_pct": round(abs(gap), 1),
            "text": f"реализованная вола {_n(rv * 100)}% — на {_n(abs(gap))} п.п. "
                    f"{side} порога {_n(thr * 100)}% (80-й перцентиль за 3 года)",
        })

    _, dd = calc.last_valid(cols.get("rgbi_dd") or [])
    if calc.is_num(dd):
        # rgbi_dd — ЛОГАРИФМИЧЕСКАЯ просадка (так её считала валидация, и на этой мере
        # стоят CELL_STATS — бит трогать нельзя). Но пользователь читает «−5,9% от
        # максимума» как просадку котировки, а котировка просела на −5,7%; в 2022-м
        # разрыв доходил до 6 п.п. (−37,5% лог против −31,3% фактических). Поэтому в
        # витрину отдаём обычные проценты — И значение, И порог, чтобы сравнение
        # осталось честным: exp монотонна, момент переключения флага не сдвигается.
        thr_pct = (math.exp(constants.STATE_RULES["bond"]["threshold"]) - 1.0) * 100.0
        val = (math.exp(dd) - 1.0) * 100.0
        verb = "снимется" if current.get("bond") else "включится"
        out.append({
            "id": "bond", "label": constants.STATE_RULES["bond"]["label"],
            "value": round(val, 1), "threshold": round(thr_pct, 1),
            "gap_pct": round(abs(val - thr_pct), 1),
            "text": f"RGBI {_n(val)}% от 252-дневного максимума; "
                    f"флаг {verb} при {_n(thr_pct)}%",
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
