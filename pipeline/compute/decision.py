"""Слой решения: ПОЗИЦИЯ панели — «акции» или «деньги» (аудит 02.09.2026, P_package).

До этого слоя панель показывала оценку рынка (композит) и режим (ячейку), а вывод
«держать акции или сидеть в деньгах» читатель делал сам — и делал по-разному.
Аудит измерил ступени P0…P7 на 2004–2026 и оставил ступень P2:

  * ВОРОТА — три флага состояния С ГИСТЕРЕЗИСОМ (states._bits_hyst), читаются
    каждый день; закрыты только в токсичном сочетании «падающий рынок · нервная
    торговля · ОФЗ под давлением» и когда какого-то флага нет вовсе;
  * НАКЛОН — знак ДНЕВНОГО композита (daily_composite), читается раз в неделю на
    последнем торговом дне недели, с гистерезисом ±0,2: между порогами прежний знак;
  * АВТОМАТ — акции, когда ворота открыты И знак +1. Выход по воротам — любым днём
    (обвал не ждёт пятницы), выход по знаку — только в день решения, вход — любым
    днём, когда оба условия выполнены. Исполнение — на следующем закрытии.

Почему знак читается по ДНЕВНОМУ композиту, а не по закрытому месяцу (прежнее
правило P0): закрытый месяц опаздывал на 2–4 недели на каждом развороте, а дневное
число в дни решения с порогом 0,2 давало ту же частоту смен и лучший выход (P_package
§P1–P2). Почему раз в неделю, а не каждый день: ежедневное чтение дневного числа
дребезжало вокруг порога (P1), недельный такт убрал дребезг без потери скорости.

Эталон, который этот модуль обязан воспроизводить побитово, — движок аудита
`scripts/P_lib.py` (pandas); фикстура `tests/fixtures/decision_fixture.json`
хранит RLE позиций, флагов и знака с 1997 года, а tests/test_decision.py сверяет
всё это на боевом сторе. Здесь — чистая стандартная библиотека.

Автомат вынесен в `run_automaton`: тень (compute/shadow.py) гоняет тот же автомат
с дополнительным битом-выходом `es`, чтобы «а что если бы сигнал был в решении»
считалось тем же кодом, а не его пересказом.
"""

import math
from datetime import date, timedelta

try:
    from ..lib import calc, constants, dates as datelib
    from ..lib.wording import plural
except ImportError:
    from lib import calc, constants, dates as datelib
    from lib.wording import plural

try:
    from . import states as states_mod, track
except ImportError:
    import states as states_mod
    import track

__all__ = ["compute_decision", "daily_composite", "run_automaton", "decision_days",
           "comp_state_series", "next_decision_day", "last_day_decides",
           "reason_text", "REASON_TEXT", "START", "LONG", "FLAT"]

# С этого дня считается позиция (BT_START эталона): раньше копится только состояние
# знака и флагов — история до 2004 одноногая и в валидацию не входила.
START = "2004-01-06"
LONG, FLAT = "long", "flat"
EXECUTE_TEXT = "на следующем закрытии"
# Сколько дней назад искать последнюю смену для причины «репрайсинг снят» (эталон).
_ES_LOOKBACK = 400
# «Оценка рынка» — так композит зовётся снаружи (alerts.core_flip); внутренние слова
# «композит», «ячейка», «бит» наружу не выпускаются (tests/test_wording).
REASON_TEXT = {
    "gate_close": "ворота закрылись",
    "comp_neg": "оценка рынка ушла ниже −0,2 в день решения",
    "gate_open": "ворота открылись",
    "comp_pos": "оценка рынка поднялась выше +0,2 в день решения",
    "entry": "все условия выполнены: ворота открыты, оценка рынка в плюсе",
    # Тень: бит репрайсинга с 03.10.2026 меряет рост годовой доходности ОФЗ, а не
    # спред к ключу (shadow._sig_repricing) — и причина называет именно это.
    "es_exit": "доходность годовых ОФЗ резко выросла (больше +0,25 п.п. за 21 день)",
    "es_block": "ждём, пока доходность годовых ОФЗ успокоится",
    "es_clear": "доходность годовых ОФЗ успокоилась (день решения)",
}
# Что именно изменилось во флагах ворот в день смены позиции: (флаг, было, стало).
_FLAG_MOVES = {
    ("trend", 1, 0): "рынок ушёл под 200-дневную среднюю",
    ("trend", 0, 1): "рынок вернулся выше 200-дневной средней",
    ("vol", 0, 1): "торговля стала нервной (волатильность выше 80-го перцентиля)",
    ("vol", 1, 0): "волатильность успокоилась (ниже 60-го перцентиля)",
    ("bond", 0, 1): "ОФЗ под давлением (просадка RGBI глубже −4%)",
    ("bond", 1, 0): "ОФЗ вышли из стресса (просадка RGBI мельче −3%)",
}


def _n(v, nd=2, plus=True):
    """Число с типографским минусом и русской запятой — как на всей панели."""
    s = f"{v:+.{nd}f}" if plus else f"{v:.{nd}f}"
    return s.replace("-", "−").replace(".", ",")


# ------------------------------------------------------------ дневной композит

def daily_composite(panel, mf):
    """Дневной ряд композита «как compute_core, если бы сегодня был любой день истории».

    Для дня t в месяце M у каждой ноги окно = сырые значения ЗАКРЫТЫХ месяцев до M
    (не больше 59 позиций назад, только не-None) плюс значение ноги в день t
    (протянутое внутри месяца); z = (v − mean)/sd при ddof=1, не меньше 24 точек,
    обрезка ±3; композит — среднее знак×z по доступным ногам. На последнем торговом
    дне закрытого месяца ряд ОБЯЗАН совпасть с mf["composite"] (тест сверяет ≤1e-9):
    это и есть доказательство, что дневное число — то же самое ядро, а не его
    родственник. Цена — O(дней×60) на ногу, при 7 тыс. дней это доли секунды.
    """
    dates = panel.get("dates") or []
    cols = panel.get("cols") or {}
    n = len(dates)
    labels = mf.get("dates") or []
    month_pos = {calc.month_key(lab): i for i, lab in enumerate(labels)}
    win, zmin, clip = (constants.Z_WINDOW_MONTHS, constants.Z_MIN_MONTHS,
                       constants.Z_CLIP)
    total, used = [0.0] * n, [0] * n
    for comp in constants.CORE_COMPONENTS:
        cid, sgn = comp["id"], comp["sign"]
        raw = (mf.get("raw") or {}).get(cid) or []
        vals = cols.get(cid) or [None] * n
        cur, cur_month, prev, s1 = None, None, None, 0.0
        for t, d in enumerate(dates):
            k = calc.month_key(d)
            if k != cur_month:
                cur_month, cur = k, None
                m = month_pos.get(k)
                prev = None
                if m is not None:
                    prev = [v for v in raw[max(0, m - (win - 1)):m] if calc.is_num(v)]
                    s1 = sum(prev)
            if calc.is_num(vals[t]):
                cur = vals[t]
            if cur is None or prev is None:
                continue
            nn = len(prev) + 1
            if nn < zmin:
                continue
            mean = (s1 + cur) / nn
            var = (sum((p - mean) ** 2 for p in prev) + (cur - mean) ** 2) / (nn - 1)
            if var <= 0:
                continue
            z = (cur - mean) / math.sqrt(var)
            total[t] += sgn * max(-clip, min(clip, z))
            used[t] += 1
    return [total[t] / used[t] if used[t] else None for t in range(n)]


def _closed_month_composite(dates, mf):
    """Композит последнего ЗАКРЫТОГО месяца, протянутый по дням (прежнее правило P0).

    Последний месяц панели закрытым не считается никогда — так же читает его и
    core.compute_core (month_end = последняя метка ДРУГОГО месяца). Месяц без
    композита предыдущее значение не сбрасывает (reindex+ffill эталона).
    """
    me = calc.month_end_indices(dates)
    comp = mf.get("composite") or []
    out = [None] * len(dates)
    cur, k, closed = None, 0, max(0, len(me) - 1)
    for t in range(len(dates)):
        while k < closed and me[k] <= t:
            if calc.is_num(comp[k]):
                cur = comp[k]
            k += 1
        out[t] = cur
    return out


# ------------------------------------------------------------- дни решения

def _week_friday(d):
    """Пятница недели, к которой относится день (period W-FRI: суббота…пятница)."""
    return d + timedelta(days=(4 - d.weekday()) % 7)


def last_day_decides(day):
    """Последний день панели — день решения, если по календарю торговых дней в его
    неделе больше нет: пятница, либо четверг перед праздничной пятницей.

    В эталоне последний день панели всегда «последний в своей неделе» просто потому,
    что данных дальше нет. В проде так нельзя: вторник, объявленный днём решения,
    в среду им быть перестал бы — и знак наклона дрожал бы внутри недели, ради
    отсутствия чего недельный такт и введён. Эвристика календаря (lib/dates) ошибается
    лишь на внеплановых переносах — цена ошибки: решение недели уедет на неделю.
    """
    d = datelib.parse_date(day)
    cur, fri = d + timedelta(days=1), _week_friday(d)
    while cur <= fri:
        if datelib.is_trading_day(cur):
            return False
        cur += timedelta(days=1)
    return True


def decision_days(dates):
    """[bool] — последний торговый день каждой недели по календарю ПАНЕЛИ.

    Внутри истории «последний в неделе» виден по следующей дате (суббота биржи
    относится уже к следующей неделе W-FRI, как в эталоне); у последнего дня
    следующей даты нет — за него отвечает last_day_decides.
    """
    n = len(dates)
    out = [False] * n
    if not n:
        return out
    keys = [_week_friday(datelib.parse_date(d)) for d in dates]
    for t in range(n - 1):
        out[t] = keys[t] != keys[t + 1]
    out[-1] = last_day_decides(dates[-1])
    return out


def next_decision_day(day):
    """Ближайший день решения ПОСЛЕ day: последний торговый день (по календарю)
    текущей недели, а если она уже решена — следующей."""
    d = datelib.parse_date(day)
    fri = _week_friday(d)
    if fri <= d or last_day_decides(day):
        fri += timedelta(days=7)
    cur = fri
    while cur > d and not datelib.is_trading_day(cur):
        cur -= timedelta(days=1)
    if cur <= d:  # вся неделя нерабочая (новогодние каникулы) — берём саму пятницу
        cur = fri
    return cur.isoformat()


def comp_state_series(values, is_decision_day, threshold):
    """Знак композита с гистерезисом, обновляемый ТОЛЬКО в дни решения.

    0 — знак ещё не определялся (до первого пересечения порога в день решения).
    """
    s, out = 0, []
    for v, dec in zip(values, is_decision_day):
        if dec and calc.is_num(v):
            if v > threshold:
                s = 1
            elif v < -threshold:
                s = -1
        out.append(s)
    return out


# ----------------------------------------------------------------- автомат

def _last_reason_is_es_exit(reasons, t):
    for j in range(t - 1, max(-1, t - _ES_LOOKBACK), -1):
        if reasons[j]:
            return reasons[j] == "es_exit"
    return False


def run_automaton(dates, gate_open, comp_state, is_decision_day, es=None, start=START):
    """Автомат позиции — ровно P_lib.run_engine (gate_entry=any, gate_exit=any,
    es_exit=any, es_reentry=dec) на готовых рядах.

    dates            — торговые дни панели;
    gate_open        — [bool|None] ворота открыты (None = закрыты);
    comp_state       — [int] знак наклона (+1/−1/0), уже с гистерезисом и тактом;
    is_decision_day  — [bool] дни решения по наклону;
    es               — необязательный бит-выход (тень): при es[t]==1 выход любым днём
                       («es_exit»), пока бит не снят — вход запрещён; возврат после
                       снятия — только в день решения («es_clear»);
    start            — с какого дня считается позиция (до него — только состояние).

    -> (pos: [0|1] позиция по закрытию дня, reasons: [""|причина] в день смены).

    Порядок причин при выходе — ворота раньше знака: если в день решения закрылись
    ворота И знак ушёл в минус, причиной названы ворота (они же и позволили бы выйти
    в любой другой день). Вход называет то, что изменилось последним: открылись ворота,
    вернулся знак, либо «все условия» (оба уже стояли — например, первый день расчёта).
    """
    n = len(dates)
    gate = [1 if (gate_open[t] and dates[t] >= start) else 0 for t in range(n)]
    if es is not None:
        es = [1 if x else 0 for x in es]
    pos, reasons = [0] * n, [""] * n
    cur, es_block = 0, False
    for t in range(n):
        dec = bool(is_decision_day[t])
        if es is not None:
            if es[t] == 1:
                es_block = True
            elif es_block and dec:
                es_block = False
        if dates[t] < start:
            continue
        cs = comp_state[t]
        comp_ok = cs == 1
        g_ok = gate[t] == 1
        want = g_ok and comp_ok and not es_block
        if cur == 1:
            if not want:
                c_es = es is not None and (es[t] == 1 or es_block)
                c_gate, c_comp = not g_ok, not comp_ok
                if dec or c_gate or c_es:
                    if c_es and es[t] == 1:
                        why = "es_exit"
                    elif c_gate:
                        why = "gate_close"
                    elif c_comp:
                        why = "comp_neg"
                    else:
                        why = "es_block"
                    cur, reasons[t] = 0, why
        elif want:
            if es is not None and t > 0 and _last_reason_is_es_exit(reasons, t):
                why = "es_clear"
            elif gate[t] == 1 and t > 0 and gate[t - 1] == 0:
                why = "gate_open"
            elif cs == 1 and t > 0 and comp_state[t - 1] != 1:
                why = "comp_pos"
            else:
                why = "entry"
            cur, reasons[t] = 1, why
        pos[t] = cur
    return pos, reasons


# ------------------------------------------------------------------ витрина

def _rle(dates, series, start=START):
    """[[дата, значение]] — только смены, начиная с первого дня расчёта."""
    out, prev = [], None
    for d, v in zip(dates, series):
        if d < start:
            continue
        if not out or v != prev:
            out.append([d, v])
            prev = v
    return out


def _flag_moves(hyst, t):
    """Какие флаги ворот изменились в день t — словами для причины смены позиции."""
    words = []
    if t <= 0:
        return words
    for name in ("trend", "vol", "bond"):
        a, b = hyst[name][t - 1], hyst[name][t]
        if a is None or b is None or a == b:
            continue
        text = _FLAG_MOVES.get((name, int(a), int(b)))
        if text:
            words.append(text)
    return words


def reason_text(reason, moves=None):
    """Причина смены позиции человеческим языком; moves — что изменилось во флагах."""
    base = REASON_TEXT.get(reason)
    if base is None:
        return "с начала расчёта"
    if reason in ("gate_close", "gate_open"):
        if moves:
            return base + ": " + "; ".join(moves)
        if reason == "gate_close":
            return base + ": по одному из флагов нет данных"
        return base + ": данные по флагам вернулись"
    return base


def _run_start(series, dates, j):
    v, k = series[j], j
    while k - 1 >= 0 and series[k - 1] == v:
        k -= 1
    return dates[k]


# Два сценария «спокойной торговли» для оценки, когда снимется флаг волы: индекс стоит
# на месте (самый быстрый путь) и ходит по ±1% в день (граница, которую обычно зовут
# спокойным днём). Ответ — коридор между ними, а не обещание.
CALM_STEP = 0.01
VOL_WINDOW = 21


def _ann_vol(rets):
    m = sum(rets) / len(rets)
    return math.sqrt(sum((r - m) ** 2 for r in rets) / (len(rets) - 1)) * math.sqrt(252)


def vol_calm_days(ret1, q60, step=CALM_STEP, window=VOL_WINDOW, horizon=VOL_WINDOW):
    """Сколько спокойных дней подряд нужно, чтобы 21-дневная вола ушла ниже p60.

    Спокойный день — ход ровно ±step со сменой знака каждый день; из двух порядков
    знаков берётся худший (step=0 — индекс стоит на месте). Порог p60 считается
    неизменным: его окно — 756 дней, за месяц он почти не двигается. Флаг держат прежде
    всего большие дни внутри окна, поэтому ответ — это вопрос о том, когда они из него
    выпадут (аудит 03.10.2026, рекомендация 11).

    -> (дней | None, статус): "ok"; "beyond" — за horizon дней не снимется (нужны дни
    ещё тише, и честнее сказать это, чем назвать срок); "no_data" — считать не по чему.
    """
    tail = list(ret1[-window:]) if ret1 else []
    if len(tail) < window or not all(calc.is_num(r) for r in tail) or not calc.is_num(q60):
        return None, "no_data"
    worst = 0
    for first in (step, -step):
        buf, found = tail, None
        for k in range(1, horizon + 1):
            buf = buf[1:] + [first if k % 2 else -first]
            if _ann_vol(buf) < q60:
                found = k
                break
        if found is None:
            return None, "beyond"
        worst = max(worst, found)
    return worst, "ok"


def _z_with(prev, x):
    """z значения x в окне prev + [x] — ровно как daily_composite (ddof=1)."""
    nn = len(prev) + 1
    mean = (sum(prev) + x) / nn
    var = (sum((p - mean) ** 2 for p in prev) + (x - mean) ** 2) / (nn - 1)
    return (x - mean) / math.sqrt(var) if var > 0 else None


def comp_leg_moves(panel, mf, target):
    """Сдвиг ОДНОЙ ноги ядра, при котором дневной композит последнего дня равен target.

    Остальные ноги стоят на месте, окна закрытых месяцев те же, что у daily_composite.
    -> [{"id", "delta"}]: delta — в единицах ноги (лог-ход курса за 63 дня, п.п. наклона,
    лог-гэп бочки); None — одной этой ногой не достать (нужен z за обрезкой ±3).
    Это «при прочих равных», а не прогноз: ноги двигаются вместе.
    """
    dates = panel.get("dates") or []
    cols = panel.get("cols") or {}
    if not dates or not calc.is_num(target):
        return []
    t = len(dates) - 1
    month = calc.month_key(dates[t])
    m = {calc.month_key(lab): i for i, lab in enumerate(mf.get("dates") or [])}.get(month)
    if m is None:
        return []
    win, zmin, clip = constants.Z_WINDOW_MONTHS, constants.Z_MIN_MONTHS, constants.Z_CLIP
    legs = []
    for comp in constants.CORE_COMPONENTS:
        cid, sgn = comp["id"], comp["sign"]
        raw = (mf.get("raw") or {}).get(cid) or []
        prev = [v for v in raw[max(0, m - (win - 1)):m] if calc.is_num(v)]
        vals = cols.get(cid) or []
        cur = None
        for k in range(min(t, len(vals) - 1), -1, -1):
            if calc.month_key(dates[k]) != month:
                break
            if calc.is_num(vals[k]):
                cur = vals[k]
                break
        z = _z_with(prev, cur) if cur is not None and len(prev) + 1 >= zmin else None
        if z is not None:
            legs.append((cid, sgn, prev, cur, sgn * max(-clip, min(clip, z))))
    out = []
    total = sum(leg[4] for leg in legs)
    for cid, sgn, prev, cur, own in legs:
        z_need = (target * len(legs) - (total - own)) / sgn
        delta = None
        if abs(z_need) < clip:
            sd = math.sqrt(sum((p - sum(prev) / len(prev)) ** 2 for p in prev) / (len(prev) - 1))
            lo, hi = cur - 50 * sd, cur + 50 * sd
            if _z_with(prev, lo) < z_need < _z_with(prev, hi):
                for _ in range(200):
                    mid = (lo + hi) / 2
                    if _z_with(prev, mid) < z_need:
                        lo = mid
                    else:
                        hi = mid
                delta = (lo + hi) / 2 - cur
        out.append({"id": cid, "delta": delta})
    return out


def _leg_text(cid, delta):
    """Сдвиг ноги словами читателя (без слов «нога», «z», «композит»)."""
    if delta is None:
        return None
    if cid == "usd_mom63":
        f = (math.exp(delta) - 1) * 100
        return f"доллар {'дороже' if f > 0 else 'дешевле'} сегодняшнего на {_n(abs(f), 0, False)}%"
    if cid == "slope_10_2":
        return (f"кривая ОФЗ {'круче' if delta > 0 else 'положе'} на "
                f"{_n(abs(delta), 1, False)} п.п. (10 лет минус 2 года)")
    if cid == "urals_rub_gap":
        f = (math.exp(delta) - 1) * 100
        return f"рублёвая бочка {'выше' if f > 0 else 'ниже'} своего тренда ещё на {_n(abs(f), 0, False)}%"
    return None


def switch_distance(panel, mf, state, comp_state, flags):
    """Сколько не хватает до смены позиции — то, что может сдвинуть её ближайшим.

    vol_calm_days_min / _max — через сколько торговых дней снимется флаг волы, если
    индекс будет стоять на месте / ходить по ±1% в день (только когда флаг стоит и
    позиция «деньги»; vol_calm_status — ok | slow_beyond | beyond | no_data);
    comp_target и legs — порог оценки рынка, который сейчас решает (вход выше +0,2 у
    «денег» без знака «за акции», выход ниже −0,2 у «акций»), и сдвиг каждой ноги,
    которого хватило бы одного. Аудит 03.10.2026, рекомендация 11.
    """
    cols = panel.get("cols") or {}
    thr = constants.DECISION["comp_threshold"]
    out = {"calm_step_pct": CALM_STEP * 100, "vol_calm_days_min": None,
           "vol_calm_days_max": None, "vol_calm_status": None,
           "comp_target": None, "legs": []}
    if state == FLAT and flags.get("vol") == 1:
        q60 = calc.last_valid(cols.get("vol_thresh60") or [])[1]
        ret1 = cols.get("ret1") or []
        fast, st_fast = vol_calm_days(ret1, q60, step=0.0)
        slow, st_slow = vol_calm_days(ret1, q60, step=CALM_STEP)
        out["vol_calm_days_min"], out["vol_calm_days_max"] = fast, slow
        out["vol_calm_status"] = (st_fast if st_fast != "ok"
                                  else ("ok" if st_slow == "ok" else "slow_beyond"))
    target = None
    if state == LONG:
        target = -thr
    elif comp_state != 1:
        target = thr
    if target is not None:
        out["comp_target"] = target
        labels = {c["id"]: c.get("label") for c in constants.CORE_COMPONENTS}
        for leg in comp_leg_moves(panel, mf, target):
            leg["label"] = labels.get(leg["id"])
            leg["text"] = _leg_text(leg["id"], leg["delta"])
            if leg["delta"] is not None:
                leg["delta"] = round(leg["delta"], 4)
            out["legs"].append(leg)
    return out


def _conditions(state, hyst, gate_open_now, comp_state, comp_now, cols, dates, dist=None):
    """Что должно случиться, чтобы позиция сменилась — по одной строке на условие."""
    out = []
    thr = constants.DECISION["comp_threshold"]
    band = constants.DECISION["trend_band"] * 100.0
    now = (f"сейчас {_n(comp_now)}" if calc.is_num(comp_now) else "сейчас нет данных")
    last = lambda name: calc.last_valid(cols.get(name) or [])[1]  # noqa: E731
    ratio, rv, q60, dd = (last("px_ma_ratio"), last("realized_vol_21"),
                          last("vol_thresh60"), last("rgbi_dd"))
    flags = {k: hyst[k][-1] for k in ("trend", "vol", "bond")}

    def flag_off_lines():
        rows = []
        if flags["trend"] == 0:
            cur = f" (сейчас {_n(ratio * 100, 1)}%)" if calc.is_num(ratio) else ""
            rows.append(f"рынок: индекс выше 200-дневной средней на {_n(band, 0, False)}%{cur}")
        if flags["vol"] == 1:
            cur = (f"сейчас {_n(rv * 100, 1, False)}% против {_n(q60 * 100, 1, False)}%"
                   if calc.is_num(rv) and calc.is_num(q60) else "")
            eta = _calm_text(dist)
            inner = "; ".join(x for x in (cur, eta) if x)
            rows.append(f"волатильность: ниже 60-го перцентиля за 3 года"
                        + (f" ({inner})" if inner else ""))
        if flags["bond"] == 1:
            cur = (f" (сейчас {_n((math.exp(dd) - 1) * 100, 1)}%)" if calc.is_num(dd) else "")
            rows.append(f"ОФЗ: просадка RGBI мельче −3%{cur}")
        return rows

    if state == FLAT:
        if not gate_open_now:
            rows = flag_off_lines()
            if rows:
                out.append("ворота откроются, когда снимется ЛЮБОЙ из флагов: "
                           + "; ".join(rows))
            else:
                out.append("ворота откроются, когда по всем трём флагам появятся данные")
        if comp_state != 1:
            out.append(f"оценка рынка выше +{_n(thr, 1, False)} в день решения ({now})")
            legs = _legs_line(dist, "до входа по оценке рынка")
            if legs:
                out.append(legs)
        if gate_open_now and comp_state == 1:
            out.append("условия входа выполнены — позиция сменится на следующем расчёте")
    else:
        missing = []
        if flags["trend"] != 0:
            missing.append(f"рынок под 200-дневной средней на {_n(band, 0, False)}%")
        if flags["vol"] != 1:
            missing.append("волатильность выше 80-го перцентиля")
        if flags["bond"] != 1:
            missing.append("просадка RGBI глубже −4%")
        gate_txt = ("ворота закроются — нужны все три флага сразу"
                    + (f"; не хватает: {'; '.join(missing)}" if missing else ""))
        out.append("любой из: " + gate_txt
                   + f"; оценка рынка ниже −{_n(thr, 1, False)} в день решения ({now})")
        legs = _legs_line(dist, "до выхода по оценке рынка")
        if legs:
            out.append(legs)
    return out


def _days(n):
    return f"{n} {plural(n, 'торговый день', 'торговых дня', 'торговых дней')}"


def _calm_text(dist):
    d = dist or {}
    status, lo, hi = d.get("vol_calm_status"), d.get("vol_calm_days_min"), d.get("vol_calm_days_max")
    if status == "beyond":
        return "даже если индекс замрёт, за месяц не снимется"
    if status == "slow_beyond" and lo:
        return (f"снимется через {_days(lo)}, если индекс будет стоять на месте; при ходах "
                f"по 1% в день — не раньше чем через месяц")
    if status != "ok" or not lo or not hi:
        return ""
    if lo == hi:
        return f"при ходах индекса до 1% в день снимется через {_days(lo)}"
    word = plural(hi, "торговый день", "торговых дня", "торговых дней")
    return (f"снимется через {lo}–{hi} {word}: {lo} — если индекс будет стоять на месте, "
            f"{hi} — если ходить по 1% в день")


def _legs_line(dist, head):
    texts = [leg.get("text") for leg in (dist or {}).get("legs") or [] if leg.get("text")]
    if not texts:
        return None
    return (f"{head} при прочих равных хватило бы одного из: " + "; ".join(texts)
            + " — это ориентир, а не прогноз: показатели двигаются вместе")


def _position_block(dates, cols, states, hyst, gate_open, comp_live, comp_state,
                    is_dec, pos, reasons, dist=None):
    n = len(dates)
    last = n - 1
    state = LONG if pos[last] else FLAT
    change = None
    for t in range(last, -1, -1):
        if reasons[t]:
            change = t
            break
    reason = reasons[change] if change is not None else None
    since = dates[change] if change is not None else next((d for d in dates if d >= START),
                                                          dates[0])
    moves = _flag_moves(hyst, change) if change is not None else []
    dep_j, dep = calc.last_valid(cols.get("deposit") or [])
    regime = ((states or {}).get("regime") or {}).get("id")
    if regime is None:
        reg = states_mod.regime_of(hyst["trend"][last], hyst["vol"][last], hyst["bond"][last])
        regime = reg["id"] if reg else None
    five_years_ago = (datelib.parse_date(dates[last]) - timedelta(days=round(5 * 365.25))
                      ).isoformat()
    switches = sum(1 for t in range(n) if reasons[t] and dates[t] > five_years_ago)
    comp_now = comp_live[last]
    return {
        "state": state,
        "since": since,
        "reason": reason,
        "reason_text": reason_text(reason, moves),
        "execute": EXECUTE_TEXT,
        "decision_day": dates[last],
        "is_decision_day": bool(is_dec[last]),
        "next_decision": next_decision_day(dates[last]),
        "comp_daily": round(comp_now, 4) if calc.is_num(comp_now) else None,
        "comp_state": comp_state[last],
        "comp_state_since": _run_start(comp_state, dates, last),
        "comp_threshold": constants.DECISION["comp_threshold"],
        "gate_open": bool(gate_open[last]),
        "gate_since": _run_start(gate_open, dates, last),
        "flags": {k: hyst[k][last] for k in ("trend", "vol", "bond")},
        "regime": regime,
        "cash_rate": round(dep, 2) if calc.is_num(dep) else None,
        "cash_rate_asof": dates[dep_j] if dep_j is not None else None,
        "conditions": _conditions(state, hyst, gate_open[last], comp_state[last],
                                  comp_now, cols, dates, dist),
        "switch_distance": dist,
        "history": _rle(dates, pos),
        "switches_per_year": round(switches / 5.0, 1),
        "start": START,
        # Что правило дало в деньгах с заморозки и за 12/24 месяца (compute/track.py).
        "journal": track.journal(dates, pos, reasons, cols.get("mcftr"), cols.get("deposit")),
    }


def _prev_rule_block(dates, pos0, reasons0):
    last = len(dates) - 1
    change = next((t for t in range(last, -1, -1) if reasons0[t]), None)
    return {
        "state": LONG if pos0[last] else FLAT,
        "since": dates[change] if change is not None else None,
        "reason": reasons0[change] if change is not None else None,
        "rule": "сырые флаги без гистерезиса, знак закрытого месяца с порогом ±0,1, "
                "читается ежедневно",
    }


def compute_decision(panel, mf, states=None):
    """-> {"position": {...}, "position_prev_rule": {...}, "daily": {...}}.

    `position` и `position_prev_rule` уезжают в data.json (verdict), `daily` — рабочие
    дневные ряды для тени и тестов (в payload не попадают): dates, comp_live,
    comp_state, gate_open, is_decision_day, pos, reasons, bits (с гистерезисом),
    pos_prev_rule, reasons_prev_rule.
    """
    dates = panel.get("dates") or []
    cols = panel.get("cols") or {}
    if not dates:
        return {"position": None, "position_prev_rule": None, "daily": {}}
    raw = states_mod._bits(dates, cols)
    hyst = states_mod._bits_hyst(dates, cols, raw)
    gate_open = states_mod.gate_open_series(raw, hyst)
    comp_live = daily_composite(panel, mf)
    is_dec = decision_days(dates)
    comp_state = comp_state_series(comp_live, is_dec, constants.DECISION["comp_threshold"])
    pos, reasons = run_automaton(dates, gate_open, comp_state, is_dec)

    # Прежнее правило (P0) — для сравнения на витрине в первые месяцы и для теста
    # побитового совпадения с эталоном: сырые биты, закрытый месяц, порог 0,1, ежедневно.
    n = len(dates)
    gate_raw = []
    for t in range(n):
        key = (raw["trend"][t], raw["vol"][t], raw["bond"][t])
        gate_raw.append(None not in key and key != states_mod.TOXIC)
    always = [True] * n
    comp_state0 = comp_state_series(_closed_month_composite(dates, mf), always,
                                    constants.CORE_FLIP_HYSTERESIS)
    pos0, reasons0 = run_automaton(dates, gate_raw, comp_state0, always)

    last = n - 1
    dist = switch_distance(panel, mf, LONG if pos[last] else FLAT, comp_state[last],
                           {k: hyst[k][last] for k in ("trend", "vol", "bond")})
    return {
        "position": _position_block(dates, cols, states, hyst, gate_open, comp_live,
                                    comp_state, is_dec, pos, reasons, dist),
        "position_prev_rule": _prev_rule_block(dates, pos0, reasons0),
        "daily": {
            "dates": dates, "comp_live": comp_live, "comp_state": comp_state,
            "gate_open": gate_open, "is_decision_day": is_dec, "pos": pos,
            "reasons": reasons, "bits": hyst, "raw_bits": raw,
            "pos_prev_rule": pos0, "reasons_prev_rule": reasons0,
        },
    }
