"""Что панель умеет и ориентиры рядом с позицией (аудит 10.11.2026, этап 2).

Три блока, и ни один не входит в решение — правило заморожено (constants.RULE_EDITIONS):

* ВОЛНЫ РЫНКА (`waves`). Главная задача панели по замыслу — заметить разворот, после
  которого акции начнут обгонять деньги. Здесь разметка всех волн индекса полной
  доходности MCFTR с августа 2009 года (разворот от 20 %) и что из каждой взяло правило
  нынешней версии на пересчитанной истории: по падениям — какую долю падения оно
  пересидело в акциях, по волнам роста — через сколько торговых дней после дна вошло и
  какую долю волны взяло. Это НА ИСТОРИИ, на которой правило выбирали: витрина так и
  подписывает.
* ДИВИДЕНДЫ МИНУС ВКЛАД (`div_gap`). Разница дивидендной доходности индекса и ставки
  вкладов (switch_spread) делит историю с 2009 года на три зоны с разной доходностью
  акций сверх вкладов за следующий год. Границы зон и их статистика заморожены
  (constants.DIV_GAP_ZONES, как CELL_STATS): пересчитываются только руками, с датой.
  Ориентир, а не правило.
* РЫНОК РАСТЁТ ВОПРЕКИ ОЦЕНКЕ (`against`). Позиция «деньги» исполнена, а индекс полной
  доходности за 20 сессий вырос больше чем на 10 %. На истории после таких эпизодов
  рынок чаще продолжал расти. Это повод владельцу посмотреть, а не сигнал.

Чистая стандартная библиотека; на вход — дневная панель и позиции автомата.
"""

import math
from datetime import date

try:
    from ..lib import calc, constants
except ImportError:
    from lib import calc, constants

__all__ = ["WAVE_THRESHOLD", "WAVES_SINCE", "zigzag", "capture", "waves",
           "div_gap_table", "div_gap", "against_episodes", "against"]

WAVE_THRESHOLD = 0.20          # разворот волны — 20 % от экстремума
WAVES_SINCE = "2009-08-01"     # с этого дня есть и индекс полной доходности, и вклады
LAST_RISES = 4                 # сколько последних волн роста показывать отдельно

AGAINST_SINCE = "2010-01-01"
AGAINST_WINDOW = 20            # сессий
AGAINST_RISE = 0.10            # рост индекса полной доходности за окно
AGAINST_GAP_DAYS = 30          # сигналы ближе этого (календарных дней) — один эпизод
AGAINST_FWD = 40               # сессий вперёд для статистики эпизодов


def _held(pos, n):
    """Позиция, которая держится в день i: решено по закрытию i−2, исполнено на i−1."""
    return [0, 0][:n] + [1 if pos[i - 2] else 0 for i in range(2, n)]


def zigzag(tr, i0, th=WAVE_THRESHOLD):
    """[[«up»|«down», индекс начала, индекс конца]] — волны от разворота на th.

    Последняя волна не закрыта: её конец — последний день ряда.
    """
    n = len(tr)
    if i0 is None or i0 >= n:
        return []
    legs, lo, hi, li, hi_i, up = [], tr[i0], tr[i0], i0, i0, None
    for i in range(i0, n):
        v = tr[i]
        if up is None:
            if v > lo * (1 + th):
                up = True
                legs.append(["up", li, None])
                hi, hi_i = v, i
            elif v < hi * (1 - th):
                up = False
                legs.append(["down", hi_i, None])
                lo, li = v, i
            if v < lo:
                lo, li = v, i
            if v > hi:
                hi, hi_i = v, i
        elif up:
            if v > hi:
                hi, hi_i = v, i
            elif v < hi * (1 - th):
                legs[-1][2] = hi_i
                legs.append(["down", hi_i, None])
                up = False
                lo, li = v, i
        else:
            if v < lo:
                lo, li = v, i
            elif v > lo * (1 + th):
                legs[-1][2] = li
                legs.append(["up", li, None])
                up = True
                hi, hi_i = v, i
    if legs:
        legs[-1][2] = n - 1
    return legs


def capture(tr, held, a, b):
    """(доля лог-хода волны, взятая в акциях; дней от начала до входа | None)."""
    tot = math.log(tr[b] / tr[a])
    got = sum(math.log(tr[i] / tr[i - 1]) for i in range(a + 1, b + 1) if held[i])
    first = next((i for i in range(a + 1, b + 1) if held[i]), None)
    share = got / tot if tot else 0.0
    return (0.0 if abs(share) < 1e-12 else share), (None if first is None else first - a)


def waves(dates, tr_raw, pos):
    """verdict.waves: волны MCFTR от 20 % с WAVES_SINCE и что из них взяло правило."""
    n = len(dates)
    tr = calc.ffill(tr_raw or [])
    i0 = next((i for i, d in enumerate(dates) if d >= WAVES_SINCE and i < len(tr)
               and calc.is_num(tr[i])), None)
    if i0 is None or len(tr) < n:
        return None
    held = _held(pos, n)
    rows = []
    for kind, a, b in zigzag(tr, i0):
        share, wait = capture(tr, held, a, b)
        row = {"kind": kind, "from": dates[a], "to": dates[b],
               "move_pct": round((tr[b] / tr[a] - 1) * 100, 1),
               "taken_pct": round(share * 100), "open": b == n - 1}
        if kind == "up":
            row["entry_days"] = wait
            row["missed_pct"] = (round((tr[a + wait - 1] / tr[a] - 1) * 100, 1)
                                 if wait is not None else None)
        rows.append(row)
    ups = [r for r in rows if r["kind"] == "up"]
    downs = [r for r in rows if r["kind"] == "down"]
    if not ups or not downs:
        return None
    entered = [r for r in ups if r["entry_days"] is not None]
    waits = sorted(r["entry_days"] for r in entered)
    return {
        "since": dates[i0], "threshold_pct": round(WAVE_THRESHOLD * 100),
        "basis": "индекс полной доходности MCFTR, волна — разворот от 20 %; правило "
                 "нынешней версии на пересчитанной истории, исполнение на следующем закрытии",
        "falls": {"n": len(downs),
                  "lived_pct": round(sum(r["taken_pct"] for r in downs) / len(downs))},
        "rises": {"n": len(ups), "entered": len(entered),
                  "entry_days_avg": round(sum(waits) / len(waits)) if waits else None,
                  "entry_days_median": waits[len(waits) // 2] if waits else None,
                  "missed_pct_avg": (round(sum(r["missed_pct"] for r in entered) / len(entered))
                                     if entered else None),
                  "taken_pct_avg": round(sum(r["taken_pct"] for r in ups) / len(ups))},
        "last_rises": ups[-LAST_RISES:],
        "rows": rows,
    }


# ------------------------------------------------------- дивиденды минус вклад

def div_gap_table(dates, cols):
    """Пересчёт трёх зон «дивиденды минус вклад» по панели (для заморозки и теста).

    Точка — конец месяца: спред (последнее значение за неделю до метки) и доходность
    индекса полной доходности за 12 месяцев вперёд минус ставка вкладов на метке.
    Границы — терции распределения спреда. -> dict | None.
    """
    sw = cols.get("switch_spread") or []
    tr, cash = calc.ffill(cols.get("mcftr") or []), calc.ffill(cols.get("deposit") or [])
    me = calc.month_end_indices(dates)
    pts = []
    for k, i in enumerate(me):
        v = calc.last_valid(sw[max(0, i - 5):i + 1])[1]
        if not calc.is_num(v) or k + 12 >= len(me) or i >= len(tr) or i >= len(cash):
            continue
        j = me[k + 12]
        if j >= len(tr) or not (calc.is_num(tr[i]) and calc.is_num(tr[j]) and calc.is_num(cash[i])):
            continue
        pts.append((dates[i], v, (tr[j] / tr[i] - 1) * 100 - cash[i]))
    if len(pts) < 30:
        return None
    vals = sorted(p[1] for p in pts)
    t1, t2 = vals[len(vals) // 3], vals[2 * len(vals) // 3]
    zones = []
    for zid, f in (("low", lambda v: v < t1), ("mid", lambda v: t1 <= v < t2),
                   ("high", lambda v: v >= t2)):
        g = [p for p in pts if f(p[1])]
        zones.append({"id": zid, "months": len(g), "years": len({p[0][:4] for p in g}),
                      "excess_pct": round(sum(p[2] for p in g) / len(g), 1),
                      "hit": round(sum(1 for p in g if p[2] > 0) / len(g), 2)})
    rho, npairs = calc.spearman_ic([p[1] for p in pts], [p[2] for p in pts])
    return {"since": pts[0][0][:7], "until": pts[-1][0][:7], "months": len(pts),
            "cuts": [round(t1, 2), round(t2, 2)], "zones": zones,
            "ic": round(rho, 2) if rho is not None else None}


def div_gap(dates, cols):
    """verdict.div_gap: сегодняшний спред, его зона и замороженная таблица зон."""
    j, now = calc.last_valid(cols.get("switch_spread") or [])
    frozen = constants.DIV_GAP_ZONES
    if now is None:
        return None
    lo, hi = frozen["cuts"]
    zone = "low" if now < lo else ("mid" if now < hi else "high")
    return {"value_pp": round(now, 2), "asof": dates[j], "zone": zone,
            "cuts": frozen["cuts"], "zones": frozen["zones"], "frozen_on": frozen["asof"],
            "since": frozen["since"], "months": frozen["months"], "ic": frozen["ic"],
            "note": "ориентир, не правило: зоны выбраны по той же истории, независимых лет около 16"}


# ------------------------------------------------- рынок растёт вопреки оценке

def _signal(dates, tr, pos, t):
    """День t: исполненная к закрытию t позиция — деньги, MCFTR за 20 сессий > +10 %."""
    if t < AGAINST_WINDOW or t < 1 or dates[t] < AGAINST_SINCE:
        return False
    a, b = tr[t - AGAINST_WINDOW], tr[t]
    if not (calc.is_num(a) and calc.is_num(b)) or a <= 0:
        return False
    return (not pos[t - 1]) and b / a - 1 > AGAINST_RISE


def against_episodes(dates, tr_raw, pos):
    """[(индекс начала эпизода, индекс последнего сигнала)] и статистика «через 40 сессий»."""
    n = len(dates)
    tr = calc.ffill(tr_raw or [])
    if len(tr) < n:
        return [], None
    eps, last = [], None
    flat_fwd = []
    for t in range(n):
        if dates[t] < AGAINST_SINCE or t < 1:
            continue
        if not pos[t - 1] and t + AGAINST_FWD < n and calc.is_num(tr[t]) and tr[t] > 0:
            flat_fwd.append(tr[t + AGAINST_FWD] / tr[t] - 1)
        if not _signal(dates, tr, pos, t):
            continue
        gap = (None if last is None else
               (date.fromisoformat(dates[t]) - date.fromisoformat(dates[last])).days)
        if gap is None or gap > AGAINST_GAP_DAYS:
            eps.append([t, t])
        else:
            eps[-1][1] = t
        last = t
    fwd = [tr[a + AGAINST_FWD] / tr[a] - 1 for a, _ in eps if a + AGAINST_FWD < n]
    stats = None
    if fwd and flat_fwd:
        stats = {"episodes": len(eps), "with_fwd": len(fwd),
                 "higher": sum(1 for x in fwd if x > 0),
                 "fwd_avg_pct": round(sum(fwd) / len(fwd) * 100, 1),
                 "flat_fwd_avg_pct": round(sum(flat_fwd) / len(flat_fwd) * 100, 1)}
    return eps, stats


def against(dates, tr_raw, pos):
    """verdict.against: сработало ли условие в последний день, начало эпизода, история."""
    n = len(dates)
    if not n:
        return None
    tr = calc.ffill(tr_raw or [])
    eps, stats = against_episodes(dates, tr_raw, pos)
    t = n - 1
    rise = None
    if len(tr) == n and t >= AGAINST_WINDOW and calc.is_num(tr[t]) and calc.is_num(tr[t - AGAINST_WINDOW]):
        rise = round((tr[t] / tr[t - AGAINST_WINDOW] - 1) * 100, 1)
    active = bool(len(tr) == n and _signal(dates, tr, pos, t))
    start = dates[eps[-1][0]] if active and eps and eps[-1][1] == t else None
    return {"active": active, "episode_start": start, "rise_20_pct": rise,
            "flat_now": not pos[t - 1] if t >= 1 else None,
            "window": AGAINST_WINDOW, "threshold_pct": round(AGAINST_RISE * 100),
            "fwd_sessions": AGAINST_FWD, "since": AGAINST_SINCE, "stats": stats,
            "episodes": [{"from": dates[a], "last": dates[b]} for a, b in eps][-15:]}
