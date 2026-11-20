"""Справка вокруг решения: насколько сегодняшней позиции можно верить (аудит 20.11.2026).

Аудит 20.11.2026 нашёл, что позиция считается верно, а справочные блоки вокруг неё
местами обещают больше, чем знают (REPORT.md §4). Здесь — блоки, которые отвечают на
вопросы владельца прямо, и НИ ОДИН из них в решение не входит: правило заморожено
(constants.RULE_EDITIONS, tests/test_rule_freeze.py), позицию по-прежнему считает
decision.compute_decision.

* КОНТРОЛЬ В ДЕНЬГАХ (`control`, verdict.control). Прибор «здоровье модели» меряет
  ранговую связь за 24 месяца и не способен отличить работающую модель от сломанной
  (§4.2). Здесь — счёт в деньгах за 12, 24 и 36 месяцев: правило (до запуска панели —
  пересчёт, с запуска — сказанное вживую) против вкладов и акций, и норма из истории:
  как часто и насколько правило отставало на скользящих окнах той же длины с 2010 года.
  Отставание от вкладов глубже худшего на истории — повод пересматривать правило.
* СИЛА СИГНАЛА (`strength`, verdict.strength). Чем дальше оценка рынка от нуля, тем
  больше ход рынка в следующие четыре недели (§3): таблица по семи зонам оценки и
  зона, в которой оценка сейчас.
* ГОЛОСОВАНИЕ НАСТРОЕК (`vote`, verdict.vote). 27 почти равноценных настроек того же
  правила (три измерения нефти × три окна нормировки × три порога): сколько из них
  сегодня «за акции» — мера твёрдости решения, а не второе правило (§6).
* ОЦЕНКА ПО ТЕКУЩЕЙ НЕФТИ (`oil_now`, verdict.position.held_by.oil_now). Нефть входит
  в оценку средней ценой ПРОШЛОГО месяца и обновляется около 5-го числа; блок говорит,
  какой была бы оценка по нефти текущего месяца (§4.3).

Таблицы пересчитываются на каждом суточном прогоне (не заморожены); внутридневной такт
берёт их из прошлого прогона вместе с вердиктом. Чистая стандартная библиотека.
"""

import math
from datetime import date

try:
    from ..lib import calc, constants
    from ..lib.wording import NBSP, num, ru_day, ru_month
except ImportError:
    from lib import calc, constants
    from lib.wording import NBSP, num, ru_day, ru_month

try:
    from . import decision as decision_mod, panel as panel_mod, skills as skills_mod, track
except ImportError:
    import decision as decision_mod
    import panel as panel_mod
    import skills as skills_mod
    import track

__all__ = ["control", "strength", "vote", "oil_now", "cash_rate_period", "attach",
           "STATUS_RANK"]

LONG, FLAT = decision_mod.LONG, decision_mod.FLAT


def _pct(v, nd=1, plus=True):
    return num(v, nd, plus) + "%"


def _pp(v, nd=1):
    return num(v, nd) + NBSP + "п.п."


# ------------------------------------------------------------ контроль в деньгах
# Худший статус окна — статус блока. ok — правило не хуже вкладов; lag — хуже, но в
# пределах худшего отставания на истории; beyond — хуже худшего на истории.
STATUS_RANK = {"ok": 0, "lag": 1, "beyond": 2}
_MONTH_LABELS = {12: "12 месяцев", 24: "24 месяца", 36: "36 месяцев"}
# Меньше стольких скользящих окон — норму не считаем: по десятку окон «худшее на
# истории» ничего не значит.
CONTROL_MIN_WINDOWS = 252


def _rolling_norm(rows, sessions, before=None):
    """Норма из истории: правило против вкладов и против акций на всех скользящих окнах
    длиной sessions строк счёта. -> {"windows", "cash": {...}, "tr": {...}} | None.

    В каждой части: worse_share — доля окон, где правило хуже; worst_pp и p05_pp —
    худшая разница и её 5-й перцентиль по ВСЕМ окнам; worst_prior_pp — худшая разница
    по окнам, закончившимся не позже дня before (начало проверяемого окна), и
    worst_prior_to — день конца этого окна. Статус «хуже худшего на истории» сравнивает
    с worst_prior_pp: проверяемое окно само лежит среди всех окон, и сравнение с
    минимумом, в который оно входит, не сработало бы никогда.

    «Хуже» считается по знаку накопленной ЛОГ-разности, которая копится дневными
    слагаемыми: в окне, где правило всё время сидело во вкладах, она равна нулю ТОЧНО.
    Разность двух отдельно накопленных произведений даёт в таких окнах шум ±1e−16 —
    и годовые окна 2023–2025 годов (правило в деньгах 21 месяц, 5,7% всех окон)
    попадали в «хуже вкладов» по знаку шума.
    """
    n = len(rows)
    if n - sessions + 1 < CONTROL_MIN_WINDOWS:
        return None
    l_rule, l_tr, l_cash, e_cash, e_tr = [0.0], [0.0], [0.0], [0.0], [0.0]
    for _d, _held, rr, rt, rc in rows:
        a, b, c = math.log1p(rr), math.log1p(rt), math.log1p(rc)
        l_rule.append(l_rule[-1] + a)
        l_tr.append(l_tr[-1] + b)
        l_cash.append(l_cash[-1] + c)
        e_cash.append(e_cash[-1] + (a - c))
        e_tr.append(e_tr[-1] + (a - b))
    out = {"windows": n - sessions + 1}
    for key, level, edge in (("cash", l_cash, e_cash), ("tr", l_tr, e_tr)):
        diffs, worse, prior, prior_to = [], 0, None, None
        for i in range(sessions, n + 1):
            j = i - sessions
            rule = math.expm1(l_rule[i] - l_rule[j])
            other = math.expm1(level[i] - level[j])
            d = (rule - other) * 100.0
            diffs.append(d)
            if edge[i] - edge[j] < 0:
                worse += 1
            if before and rows[i - 1][0] <= before and (prior is None or d < prior):
                prior, prior_to = d, rows[i - 1][0]
        out[key] = {"worse_share": round(worse / len(diffs), 3),
                    "worst_pp": round(min(diffs), 1),
                    "p05_pp": round(calc.quantile(diffs, 0.05), 1),
                    "worst_prior_pp": round(prior, 1) if prior is not None else None,
                    "worst_prior_to": prior_to}
    return out


def _control_text(label, win, norm, status):
    head = (f"{label}: правило {_pct(win['rule_pct'])}, вклады {_pct(win['cash_pct'])}, "
            f"акции {_pct(win['tr_pct'])}")
    if status == "ok":
        return head + " — не хуже вкладов"
    lag = _pp(abs(win["vs_cash_pp"]))
    prior = (norm or {}).get("cash", {}).get("worst_prior_pp")
    if status == "beyond":
        if prior >= 0:
            return (head + f" — хуже вкладов на {lag}: с {constants.EVIDENCE_SINCE[:4]} года "
                    "на таких окнах правило от вкладов не отставало")
        return (head + f" — хуже вкладов на {lag}: больше худшего отставания с "
                f"{constants.EVIDENCE_SINCE[:4]} года ({_pp(abs(prior))})")
    if norm is None or prior is None:
        return head + f" — хуже вкладов на {lag}"
    share = num(norm["cash"]["worse_share"] * 100, 0)
    return (head + f" — хуже вкладов на {lag}; с {constants.EVIDENCE_SINCE[:4]} года так было "
            f"в {share}% таких окон, худшее отставание {_pp(abs(prior))}")


def control(dates, pos, tr, cash, said_pos=None, tr_last=None, since=None):
    """verdict.control: счёт в деньгах за 12/24/36 месяцев и норма из истории.

    pos — пересчёт нынешней версией (по нему считается норма); said_pos — ряд «до
    запуска панели — пересчёт, с запуска — сказанное» (track.said_positions), по нему
    считаются окна; без живого журнала окна считаются по пересчёту (rule = "recalc").
    tr_last — последний день с собственной точкой индекса (track.own_last).

    Окна — календарные 12/24/36 месяцев по последний день счёта (те же, что m12 и m24
    журнала); норма — скользящие окна по 252/504/756 сессий на пересчёте с `since`, как
    в аудите 20.11.2026. Статус окна: ok — не хуже вкладов; lag — хуже, но не хуже
    худшего на истории; beyond — хуже худшего на истории, где история — окна,
    закончившиеся до начала проверяемого (_rolling_norm).
    -> dict | None (считать не по чему).
    """
    if not dates:
        return None
    since = since or constants.EVIDENCE_SINCE
    recalc = track.counted_rows(track.daily_rows(dates, pos, tr, cash), tr_last)
    live = (track.counted_rows(track.daily_rows(dates, said_pos, tr, cash), tr_last)
            if said_pos else recalc)
    if not recalc or not live:
        return None
    asof = live[-1][0]
    hist = [r for r in recalc if r[0] >= since]
    windows, worst = [], None
    for months in constants.CONTROL_MONTHS:
        lo = track.months_back(asof, months)
        cur = track.summarize(live, lo, asof, 0.0)
        # История короче окна — окна нет: «за 36 месяцев» по двум годам данных было бы
        # другим числом под тем же именем.
        if cur is None or cur["from"] != lo:
            continue
        ref = track.summarize(recalc, lo, asof, 0.0)
        sessions = constants.CONTROL_SESSIONS_PER_YEAR * months // 12
        norm = _rolling_norm(hist, sessions, before=lo)
        win = {"months": months, "label": _MONTH_LABELS.get(months, f"{months} мес."),
               "from": cur["from"], "to": cur["to"], "days": cur["days"],
               "rule_pct": cur["rule_pct"], "cash_pct": cur["cash_pct"],
               "tr_pct": cur["tr_pct"],
               "vs_cash_pp": round(cur["rule_pct"] - cur["cash_pct"], 2),
               "vs_tr_pp": round(cur["rule_pct"] - cur["tr_pct"], 2),
               "rule_recalc_pct": ref["rule_pct"] if ref else None,
               "norm": dict(norm, sessions=sessions, since=hist[0][0]) if norm else None}
        prior = norm["cash"]["worst_prior_pp"] if norm else None
        if win["vs_cash_pp"] >= 0:
            status = "ok"
        elif prior is not None and win["vs_cash_pp"] < prior:
            status = "beyond"
        else:
            status = "lag"
        win["status"] = status
        win["text"] = _control_text(win["label"], win, norm, status)
        windows.append(win)
        if worst is None or STATUS_RANK[status] > STATUS_RANK[worst]:
            worst = status
    if not windows:
        return None
    return {"asof": asof, "status": worst, "since": since,
            "rule": "said" if said_pos else "recalc",
            "basis": (track.SAID_BASIS if said_pos
                      else "пересчёт нынешней версией правила") + "; " + track.BASIS
                     + "; норма — пересчёт нынешней версией на скользящих окнах той же длины",
            "windows": windows}


# ------------------------------------------------------------------ сила сигнала

def _zone_text(lo, hi):
    if lo is None:
        return f"ниже {num(hi, 1, True)}"
    if hi is None:
        return f"выше {num(lo, 1, True)}"
    return f"от {num(lo, 1, True)} до {num(hi, 1, True)}"


def _zone_of(value):
    for k, (lo, hi) in enumerate(constants.STRENGTH_ZONES):
        if (lo is None or value >= lo) and (hi is None or value < hi):
            return k
    return None


def _strength_label(value):
    a = abs(value)
    if a >= constants.STRENGTH_STRONG:
        return "сильный"
    if a >= constants.DECISION["comp_threshold"]:
        return "слабый"
    return "нейтральный"


def strength(dates, comp_live, is_decision_day, tr, cash, tr_last=None, since=None):
    """verdict.strength: лестница «оценка рынка в день решения → акции сверх вкладов за
    следующие 20 сессий» и зона, в которой оценка сейчас.

    Неделя — день решения с `since` с известной дневной оценкой (той же, что читает
    правило). Форвард — от закрытия СЛЕДУЮЩЕГО дня (день исполнения) на
    STRENGTH_FWD_SESSIONS сессий: дни t+2 … t+21. Последние 21 сессия без полного
    форварда в таблицу не входят; счёт идёт по последний день с собственной точкой
    индекса (tr_last).

    Две меры одного окна. mean_pct / median_pct — как в журнале (compute/track.py):
    простые накопленные проценты, акции минус вклады, п.п. mean_log_pct /
    median_log_pct — лог-мера (сумма дневных лог-разностей ×100): в ней посчитана
    таблица аудита 20.11.2026 (results/a6_dose_gate.txt), по ней и сверка. Доля недель в
    плюс (hit) от меры не зависит.
    """
    n = len(dates)
    if not n or not comp_live:
        return None
    since = since or constants.EVIDENCE_SINCE
    h = constants.STRENGTH_FWD_SESSIONS
    trf, cf = calc.ffill(tr or []), calc.ffill(cash or [])
    if len(trf) < n or len(cf) < n:
        return None
    last = n - 1
    if tr_last:
        while last > 0 and dates[last] > tr_last:
            last -= 1
    c_tr, c_cash, c_ok = [0.0] * (n + 1), [0.0] * (n + 1), [0] * (n + 1)
    for i in range(n):
        lt = lc = 0.0
        ok = 0
        if i >= 1:
            a, b, c = trf[i - 1], trf[i], cf[i - 1]
            if calc.is_num(a) and calc.is_num(b) and calc.is_num(c) and a > 0 and b > 0:
                days = (date.fromisoformat(dates[i][:10])
                        - date.fromisoformat(dates[i - 1][:10])).days
                lt = math.log(b / a)
                lc = (days / 365.0) * math.log1p(c / 100.0)
                ok = 1
        c_tr[i + 1], c_cash[i + 1], c_ok[i + 1] = c_tr[i] + lt, c_cash[i] + lc, c_ok[i] + ok
    acc = [[] for _ in constants.STRENGTH_ZONES]
    through = None
    for t in range(n):
        v = comp_live[t]
        if not is_decision_day[t] or not calc.is_num(v) or dates[t] < since:
            continue
        if t + 1 + h > last or c_ok[t + 2 + h] - c_ok[t + 2] < h:
            continue
        a = c_tr[t + 2 + h] - c_tr[t + 2]
        b = c_cash[t + 2 + h] - c_cash[t + 2]
        k = _zone_of(v)
        if k is None:
            continue
        acc[k].append((a - b, math.expm1(a) - math.expm1(b)))
        through = dates[t]
    weeks = sum(len(g) for g in acc)
    if not weeks:
        return None
    zones = []
    for k, (lo, hi) in enumerate(constants.STRENGTH_ZONES):
        g = acc[k]
        row = {"id": f"z{k + 1}", "lo": lo, "hi": hi, "range_text": _zone_text(lo, hi),
               "weeks": len(g), "mean_pct": None, "median_pct": None, "hit": None,
               "mean_log_pct": None, "median_log_pct": None}
        if g:
            logs, simple = [x[0] for x in g], [x[1] for x in g]
            row.update(mean_pct=round(sum(simple) / len(g) * 100, 2),
                       median_pct=round(calc.quantile(simple, 0.5) * 100, 2),
                       hit=round(sum(1 for x in logs if x > 0) / len(g), 2),
                       mean_log_pct=round(sum(logs) / len(g) * 100, 2),
                       median_log_pct=round(calc.quantile(logs, 0.5) * 100, 2))
        zones.append(row)
    out = {"asof": dates[-1], "since": since, "through": through, "weeks": weeks,
           "fwd_sessions": h, "strong_from": constants.STRENGTH_STRONG,
           "weak_from": constants.DECISION["comp_threshold"],
           "measure": "акции (индекс полной доходности MCFTR) минус вклады за "
                      f"{h} сессий от закрытия дня после дня решения; mean_pct и "
                      "median_pct — простые проценты, как в журнале; mean_log_pct и "
                      "median_log_pct — лог-мера",
           "zones": zones, "value": None, "zone": None, "label": None, "text": None}
    now = comp_live[-1]
    if calc.is_num(now):
        k = _zone_of(now)
        label = _strength_label(now)
        out.update(value=round(now, 4), zone=zones[k]["id"] if k is not None else None,
                   label=label)
        z = zones[k] if k is not None else None
        if z and z["weeks"]:
            out["text"] = (
                f"Оценка рынка {num(now, 2, True)}: сигнал {label}. При оценке "
                f"{z['range_text']} акции за следующие {h} сессий давали в среднем "
                f"{_pct(z['mean_pct'])} к вкладам, в плюс — {num(z['hit'] * 100, 0)}% недель "
                f"({z['weeks']} с {since[:4]} года)")
    return out


# ------------------------------------------------------------ голосование настроек

def _brent_usd_gap(cols):
    """Brent в долларах к своей средней за 504 дня (min 252), лог-отношение."""
    brent = cols.get("brent") or []
    window, min_obs = constants.VOTE_BRENT_MA
    ma = calc.rolling_mean(brent, window, min_periods=min_obs)
    return [math.log(x / m) if calc.is_num(x) and calc.is_num(m) and x > 0 and m > 0 else None
            for x, m in zip(brent, ma)]


def vote_positions(panel, mf, gate_open, is_decision_day):
    """Позиции всех настроек семейства. -> [((нефть, окно, порог), [0|1…])].

    Каждая настройка — то же правило с одной заменой: дневная оценка считается ТОЙ ЖЕ
    функцией decision.daily_composite (другая нефтяная нога и/или окно нормировки),
    знак — decision.comp_state_series с другим порогом, позиция — тем же автоматом
    decision.run_automaton на тех же воротах и днях решения. Первая настройка каждого
    измерения — боевая, и её позиция обязана совпасть с позицией панели побитово.
    """
    dates = panel.get("dates") or []
    cols = dict(panel.get("cols") or {})
    cols["brent_usd_gap"] = _brent_usd_gap(cols)
    raw = dict(mf.get("raw") or {})
    for oil, _label in constants.VOTE_OIL:
        if oil not in raw:
            raw[oil] = calc.resample_month_end(dates, cols.get(oil) or [None] * len(dates))[1]
    panel2 = {"dates": dates, "cols": cols}
    mf2 = {"dates": mf.get("dates") or [], "raw": raw}
    out = []
    for oil, _label in constants.VOTE_OIL:
        comps = [{"id": oil if c["id"] == constants.VOTE_OIL_LEG else c["id"],
                  "sign": c["sign"]} for c in constants.CORE_COMPONENTS]
        for window in constants.VOTE_WINDOWS:
            comp = decision_mod.daily_composite(panel2, mf2, components=comps, window=window)
            for thr in constants.VOTE_THRESHOLDS:
                cs = decision_mod.comp_state_series(comp, is_decision_day, thr)
                pos, _reasons = decision_mod.run_automaton(dates, gate_open, cs,
                                                           is_decision_day)
                out.append(((oil, window, thr), pos))
    return out


def _count(variants, t, axis, value):
    hit = [pos[t] for key, pos in variants if key[axis] == value]
    return {"n_long": sum(1 for x in hit if x), "n_total": len(hit)}


def vote(panel, mf, daily):
    """verdict.vote: сколько настроек семейства сейчас «за акции» — твёрдость решения.

    daily — рабочие ряды decision.compute_decision (gate_open, is_decision_day, pos).
    -> dict | None. Если боевая настройка семейства не совпала с позицией панели,
    бросает RuntimeError: голосование, которое считает не то правило, показывать нельзя.
    """
    dates = panel.get("dates") or []
    if not dates or not daily.get("pos"):
        return None
    variants = vote_positions(panel, mf, daily["gate_open"], daily["is_decision_day"])
    base_key = (constants.VOTE_OIL[0][0], constants.VOTE_WINDOWS[0],
                constants.VOTE_THRESHOLDS[0])
    base = dict(variants)[base_key]
    if base != daily["pos"]:
        raise RuntimeError("боевая настройка голосования разошлась с позицией панели")
    n, total = len(dates), len(variants)
    start = next((t for t, d in enumerate(dates) if d >= decision_mod.START), n)
    longs = [sum(1 for _k, pos in variants if pos[t]) for t in range(n)]

    def side(t):
        if 2 * longs[t] > total:
            return LONG
        return FLAT if 2 * longs[t] < total else None

    last = n - 1
    k = last
    while k - 1 >= start and side(k - 1) == side(last):
        k -= 1
    me = [i for i in calc.month_end_indices(dates) if i >= start]
    # Последний элемент month_end_indices — последний день панели (месяц не закрыт):
    # он идёт последней точкой ряда, перед ним — VOTE_MONTHS закрытых месяцев.
    monthly = [[dates[i], round(longs[i] / total, 3)]
               for i in me[-(constants.VOTE_MONTHS + 1):]]
    n_long = longs[last]
    maj = side(last)
    words = {LONG: "за акции", FLAT: "за деньги"}
    text = (f"За акции {n_long} из {total} настроек правила"
            + (f"; большинство {words[maj]} держится с {ru_day(dates[k])}" if maj else ""))
    return {
        "asof": dates[last], "n_long": n_long, "n_total": total,
        "share": round(n_long / total, 3), "majority": maj, "majority_since": dates[k],
        "rule_state": LONG if base[last] else FLAT,
        "agrees": (maj == (LONG if base[last] else FLAT)) if maj else None,
        "by_oil": [dict(id=oil, label=label, **_count(variants, last, 0, oil))
                   for oil, label in constants.VOTE_OIL],
        "by_window": [dict(months=w, **_count(variants, last, 1, w))
                      for w in sorted(constants.VOTE_WINDOWS)],
        "by_threshold": [dict(threshold=x, **_count(variants, last, 2, x))
                         for x in sorted(constants.VOTE_THRESHOLDS)],
        "monthly": monthly,
        "family": "то же правило с одной заменой: три измерения нефти × окно нормировки "
                  "48, 60 или 72 месяца × порог ±0,1, ±0,2 или ±0,3; ворота, дни решения "
                  "и исполнение — как у правила",
        "text": text,
    }


# ------------------------------------------------------- оценка по текущей нефти

def _month_mean(dates, values, key):
    xs = [v for d, v in zip(dates, values) if d[:7] == key and calc.is_num(v)]
    return (sum(xs) / len(xs), len(xs)) if xs else (None, 0)


def oil_now(store, panel, mf, min_age_days=None):
    """verdict.position.held_by.oil_now: оценка рынка по нефти ТЕКУЩЕГО месяца.

    Нефть входит в оценку разрывом рублёвой цены налоговой Urals с её средним за 24
    месяца; цена — средняя за прошлый месяц, обновляется около 5-го числа. Блок
    называет месяц, который сейчас стоит в расчёте (leg_month), и его возраст в днях
    от конца этого месяца (leg_age_days). Когда возраст больше min_age_days
    (constants.OIL_NOW_MIN_AGE_DAYS) и в текущем месяце не меньше
    constants.OIL_NOW_MIN_SESSIONS сессий с ценой нефти, заполняется estimate:

      цена текущего месяца = средняя Brent с начала месяца × средний курс с начала
      месяца × (налоговая цена ÷ (Brent × курс)) месяца, стоящего в расчёте;
      разрыв — та же формула, что в panel._urals_rub_gap (среднее за 24 месяца вместе
      с оценённым месяцем); вклад в оценку и оценка рынка — та же нормировка, что в
      decision.daily_composite (окно закрытых месяцев + значение дня, обрезка ±3).

    Оценка заполняется, только если пересчёт разрыва месяца, стоящего в расчёте, сошёлся
    с колонкой панели: иначе «текущая нефть» сравнивалась бы не с тем, что считает
    правило. На позицию не влияет. -> dict | None (нефти в расчёте нет).
    """
    dates = panel.get("dates") or []
    cols = panel.get("cols") or {}
    if not dates:
        return None
    if min_age_days is None:
        min_age_days = constants.OIL_NOW_MIN_AGE_DAYS
    legs = decision_mod._last_day_legs(panel, mf)
    leg = next((x for x in legs if x[0] == constants.VOTE_OIL_LEG), None)
    if leg is None:
        return None
    _cid, sgn, prev, cur, own = leg
    last = dates[-1]
    usd = cols.get("usd") or []
    brent = cols.get("brent") or []
    lag = panel_mod._lag_days("urals_tax")
    rows, seen = [], set()   # (конец месяца, цена Urals в $, рублёвая цена)
    for d, v in sorted(panel_mod._points(store, "urals_tax").items()):
        if not calc.is_num(v):
            continue
        end = panel_mod._month_end_iso(d)
        if end in seen:
            continue
        seen.add(end)
        fx, cnt = _month_mean(dates, usd, end[:7])
        if cnt and panel_mod._shift(end, lag) <= last:
            rows.append((end, float(v), float(v) * fx))
    if not rows:
        return None
    end, urals_usd, barrel = rows[-1]
    rub = [r[2] for r in rows]
    mean24 = calc.rolling_mean(rub, 24, min_periods=12)[-1]
    age = (date.fromisoformat(last[:10]) - date.fromisoformat(end)).days
    total = sum(x[4] for x in legs)
    out = {"leg_month": end[:7], "leg_age_days": age, "min_age_days": min_age_days,
           "leg_barrel_rub": round(barrel), "leg_contrib": round(own, 3),
           "comp_daily": round(total / len(legs), 4), "estimate": None, "text": None}
    leg_gap = math.log(barrel / mean24) if calc.is_num(mean24) and mean24 > 0 else None
    month = last[:7]
    if age <= min_age_days or month == end[:7] or leg_gap is None or abs(leg_gap - cur) > 1e-9:
        return out
    b_now, sessions = _month_mean(dates, brent, month)
    fx_now, _ = _month_mean(dates, usd, month)
    b_leg, _ = _month_mean(dates, brent, end[:7])
    if not (calc.is_num(b_now) and calc.is_num(fx_now) and calc.is_num(b_leg) and b_leg > 0):
        return out
    if sessions < constants.OIL_NOW_MIN_SESSIONS:
        return out      # один-три дня месяца — не оценка, а шум (constants)
    ratio = urals_usd / b_leg       # курс в числителе и знаменателе один и тот же
    est = b_now * fx_now * ratio
    tail = rub[-23:] + [est]
    if len(tail) < 12 or est <= 0:
        return out
    gap = math.log(est / (sum(tail) / len(tail)))
    z = decision_mod._z_with(prev, gap)
    if z is None:
        return out
    clip = constants.Z_CLIP
    contrib = sgn * max(-clip, min(clip, z))
    comp_if = (total - own + contrib) / len(legs)
    b_j = calc.last_valid([v if d[:7] == month else None for d, v in zip(dates, brent)])[0]
    own_brent = ((panel.get("coverage") or {}).get("brent") or {}).get("last")
    out["estimate"] = {
        "month": month, "sessions": sessions,
        "brent_usd": round(b_now, 2), "usd_rub": round(fx_now, 2), "ratio": round(ratio, 3),
        "barrel_rub": round(est), "gap": round(gap, 3), "contrib": round(contrib, 3),
        "comp_daily_if": round(comp_if, 4),
        "brent_asof": own_brent if own_brent and own_brent <= last
        else (dates[b_j] if b_j is not None else None),
    }
    out["text"] = (
        f"Нефть в расчёте — средняя за {ru_month(end)}: {num(barrel, 0)}{NBSP}₽ за баррель. "
        f"По ценам {ru_month(month + '-01', nominative=False)[:-5]} — около {num(est, 0)}{NBSP}₽: "
        f"с такой нефтью оценка рынка была бы {num(comp_if, 2, True)} вместо "
        f"{num(total / len(legs), 2, True)}")
    return out


# ------------------------------------------------------------- дата ставки вкладов

def cash_rate_period(store, dates):
    """Собственная дата ставки вкладов: конец декады, значение которой стоит в панели в
    последний день (с лагом публикации из реестра). cash_rate_asof называет последний
    день панели, а ставка декадная: 19.11.2026 показана ставка от 10.11. -> дата | None."""
    if not dates:
        return None
    lag = panel_mod._lag_days("deposit_decade")
    best = None
    for d, v in panel_mod._points(store, "deposit_decade").items():
        if not calc.is_num(v):
            continue
        try:
            if panel_mod._shift(str(d), lag) > dates[-1]:
                continue
        except (TypeError, ValueError):
            continue
        key = str(d)[:10]
        if best is None or key > best:
            best = key
    return best


# ------------------------------------------------------------------------ сборка

def attach(store, panel, mf, decision):
    """Дописать справку в блок решения. -> (extras, errors).

    extras — блоки верхнего уровня вердикта {"control", "strength", "vote"} (run.py
    кладёт их в payload["verdict"]); в сам блок решения дописываются
    position.cash_rate_period, position.said_history, position.held_by.oil_now,
    position.journal.windows[].said и поля said_* волн. Каждый блок считается в своём
    try: справка не имеет права ронять позицию, и отказ одного блока не прячет
    остальные. errors — [«блок: ошибка»] для журнала прогона.
    """
    extras, errors = {}, []
    pos_block = (decision or {}).get("position")
    daily = (decision or {}).get("daily") or {}
    dates = daily.get("dates") or []
    if not pos_block or not dates:
        return extras, errors
    cols = (panel or {}).get("cols") or {}
    tr, cash, pos = cols.get("mcftr"), cols.get("deposit"), daily["pos"]
    tr_last = daily.get("tr_last")
    box = {"said": None, "said_pos": None}

    def step(name, fn):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 — граница изоляции справки
            errors.append(f"{name}: {type(exc).__name__}: {exc}"[:200])

    def _said():
        said = track.said_load(store)
        box["said"] = said or None
        if not said:
            return
        pos_block["said_history"] = track.said_history(dates, said)
        box["said_pos"] = track.said_windows(pos_block.get("journal"), dates, pos, said,
                                             tr, cash, tr_last)
        if box["said_pos"] is None:
            box["said_pos"] = track.said_positions(dates, pos, said)

    def _waves():
        waves = (decision.get("skills") or {}).get("waves")
        if waves and box["said_pos"]:
            skills_mod.waves_said(waves, dates, tr, box["said_pos"], min(box["said"]))

    def _cash():
        if pos_block.get("cash_rate") is not None:
            pos_block["cash_rate_period"] = cash_rate_period(store, dates)

    def _oil():
        block = oil_now(store, panel, mf)
        if block is not None and isinstance(pos_block.get("held_by"), dict):
            pos_block["held_by"]["oil_now"] = block

    def _control():
        extras["control"] = control(dates, pos, tr, cash, box["said_pos"], tr_last)

    def _strength():
        extras["strength"] = strength(dates, daily["comp_live"], daily["is_decision_day"],
                                      tr, cash, tr_last)

    def _vote():
        extras["vote"] = vote(panel, mf, daily)

    for name, fn in (("живой след", _said), ("волны", _waves), ("дата ставки", _cash),
                     ("текущая нефть", _oil), ("контроль", _control),
                     ("сила сигнала", _strength), ("голосование", _vote)):
        step(name, fn)
    return {k: v for k, v in extras.items() if v}, errors
