"""Журнал позиции: что действующее правило дало в деньгах.

ЗАЧЕМ (аудит 03.10.2026, §2.5–2.6). Здоровье ядра меряет ранговую связь композита со
следующим месяцем, а не деньги позиции, и на 24 месяцах она почти всегда неотличима
от нуля. Отчёт реколибровки мерил прежнее месячное правило по цене индекса и с нулём
во флэте. Ни то ни другое не отвечает на вопрос «работает ли правило», а через год
его зададут. Здесь одна мера для витрины (verdict.position.journal) и для отчёта
реколибровки:

  * лонг — индекс полной доходности MCFTR: позиция «акции» держит именно его, с
    дивидендами;
  * флэт — вклады топ-10 (cols["deposit"]), та же ставка, что стоит рядом с
    позицией на витрине;
  * исполнение на следующем закрытии (DECISION["execution"]): позиция, решённая по
    закрытию дня t, исполняется на закрытии t+1 и зарабатывает доходность дня t+2;
  * комиссия SWITCH_COST за каждую смену позиции (как во всех бэктестах аудитов).

Налоги, проскальзывание в дни обвалов и комиссия фонда денежного рынка не учтены —
как и в остальных числах панели.
"""

import math
from datetime import date, timedelta

try:
    from ..lib import calc
except ImportError:
    from lib import calc

__all__ = ["FREEZE", "SWITCH_COST", "BASIS", "daily_rows", "summarize", "monthly_hits",
           "wilson", "months_back", "journal"]

# С этого дня правило P2 ведёт позицию в проде (аудит 02.09.2026). Всё, что раньше, —
# бэктест на той же истории, по которой правило выбирали; честный счёт идёт с этой даты.
FREEZE = "2026-09-02"
SWITCH_COST = 0.002
BASIS = ("акции — индекс полной доходности MCFTR, деньги — вклады топ-10; исполнение "
         "на следующем закрытии, 0,2% за смену; налоги не учтены")
# Шарп над деньгами по окну короче квартала — шум, его не печатаем.
MIN_DAYS_SHARPE = 60


def daily_rows(dates, pos, tr, cash, lag=1, cost=SWITCH_COST):
    """-> [(день, держим 0|1, r_rule, r_tr, r_cash)] — доходности дня по закрытиям.

    pos[t] — позиция, решённая по закрытию t; в день i держится pos[i − 1 − lag].
    Индекс и ставка протягиваются вперёд: пропуск индекса — нулевой день и догон на
    следующем (так и ведёт себя счёт), пропуск ставки — последняя известная ставка.
    День без индекса или ставки до первого наблюдения в журнал не попадает.
    """
    trf, cf = calc.ffill(tr or []), calc.ffill(cash or [])
    rows, prev = [], None
    for i in range(lag + 1, len(dates)):
        if i >= len(trf) or i >= len(cf):
            break
        a, b, c = trf[i - 1], trf[i], cf[i - 1]
        if not (calc.is_num(a) and calc.is_num(b) and calc.is_num(c)) or a <= 0:
            continue
        held = 1 if pos[i - 1 - lag] else 0
        days = (date.fromisoformat(dates[i][:10]) - date.fromisoformat(dates[i - 1][:10])).days
        r_cash = (1.0 + c / 100.0) ** (days / 365.0) - 1.0
        r_tr = b / a - 1.0
        r_rule = r_tr if held else r_cash
        if prev is not None and held != prev:
            r_rule -= cost
        prev = held
        rows.append((dates[i], held, r_rule, r_tr, r_cash))
    return rows


def _pct(x, nd=2):
    return round(x * 100.0, nd)


def summarize(rows, lo=None, hi=None):
    """Итог окна (lo, hi] -> dict | None (в окне нет ни одного дня).

    total_* — накопленные проценты за окно; ann_* — годовые (только для окна от
    года: годовой темп за месяц читается как обещание); excess_pp — правило минус
    вклады за окно, п.п.; ex_sharpe — Шарп дневного избытка над вкладами.
    """
    idx = [i for i, r in enumerate(rows)
           if (lo is None or r[0] > lo) and (hi is None or r[0] <= hi)]
    if not idx:
        return None
    sel = rows[idx[0]:idx[-1] + 1]
    # Начало окна — lo, но не раньше последнего дня ДО первой строки: если история
    # начинается позже lo, годовой темп «с 2010» на трёх годах данных занижался бы
    # вшестеро.
    prev = rows[idx[0] - 1][0] if idx[0] > 0 else sel[0][0]
    start = max(lo, prev) if lo else prev
    eq = {"rule": 1.0, "tr": 1.0, "cash": 1.0}
    peak, mdd, inm, switches, prev = 1.0, 0.0, 0, 0, None
    ex = []
    for _d, held, rr, rt, rc in sel:
        eq["rule"] *= 1.0 + rr
        eq["tr"] *= 1.0 + rt
        eq["cash"] *= 1.0 + rc
        peak = max(peak, eq["rule"])
        mdd = min(mdd, eq["rule"] / peak - 1.0)
        inm += held
        if prev is not None and held != prev:
            switches += 1
        prev = held
        ex.append(rr - rc)
    n = len(sel)
    years = (date.fromisoformat(sel[-1][0][:10]) - date.fromisoformat(start[:10])).days / 365.25
    out = {"from": start, "to": sel[-1][0], "days": n,
           "rule_pct": _pct(eq["rule"] - 1.0), "tr_pct": _pct(eq["tr"] - 1.0),
           "cash_pct": _pct(eq["cash"] - 1.0),
           "excess_pp": _pct(eq["rule"] - eq["cash"]),
           "mdd_pct": _pct(mdd, 1), "in_market_pct": round(100.0 * inm / n),
           "switches": switches}
    if years >= 0.99:
        for k in ("rule", "tr", "cash"):
            out[f"{k}_ann_pct"] = _pct(eq[k] ** (1.0 / years) - 1.0, 1)
    if n >= MIN_DAYS_SHARPE:
        m = sum(ex) / n
        sd = math.sqrt(sum((x - m) ** 2 for x in ex) / n)
        out["ex_sharpe"] = round(m / sd * math.sqrt(252), 2) if sd > 0 else None
    return out


def wilson(k, n, z=1.96):
    """95% интервал Уилсона для доли k/n: у малых n он не вылезает за [0; 1]."""
    if not n:
        return None, None
    p = k / n
    den = 1.0 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return round(mid - half, 3), round(mid + half, 3)


def monthly_hits(rows, lo_month, hi_month):
    """Доля месяцев, когда выбор правила был верным, с интервалом Уилсона.

    Месяц «верный», если позиция правила дала не меньше, чем зеркальная (деньги
    вместо акций и наоборот), — без комиссий с обеих сторон: вопрос о выборе, а не о
    счёте. Месяцы — календарные, от lo_month до hi_month включительно ('YYYY-MM');
    незакрытый месяц передавать не надо.
    """
    acc = {}
    for d, held, _rr, rt, rc in rows:
        m = d[:7]
        if not (lo_month <= m <= hi_month):
            continue
        a = acc.setdefault(m, [1.0, 1.0])
        a[0] *= 1.0 + (rt if held else rc)
        a[1] *= 1.0 + (rc if held else rt)
    n = len(acc)
    k = sum(1 for own, mirror in acc.values() if own >= mirror)
    lo, hi = wilson(k, n)
    return {"months": n, "right": k, "share": round(k / n, 3) if n else None,
            "ci95": [lo, hi]}


def months_back(day, months):
    d = date.fromisoformat(day[:10])
    y, m = divmod(d.month - 1 - months, 12)
    y += d.year
    m += 1
    last = (date(y + (m // 12), m % 12 + 1, 1) - timedelta(days=1)).day
    return date(y, m, min(d.day, last)).isoformat()


def journal(dates, pos, reasons, tr, cash, freeze=FREEZE, lag=1, cost=SWITCH_COST):
    """Блок verdict.position.journal: правило против удержания индекса и вкладов.

    Три окна — с заморозки правила и за 12/24 месяца — и сделки с заморозки.
    -> None, если считать не по чему (нет индекса полной доходности или ставки).
    """
    if not dates:
        return None
    rows = daily_rows(dates, pos, tr, cash, lag, cost)
    if not rows:
        return None
    asof = rows[-1][0]
    windows = []
    for wid, label, lo in (("since_freeze", "с заморозки правила", freeze),
                           ("m12", "12 месяцев", months_back(asof, 12)),
                           ("m24", "24 месяца", months_back(asof, 24))):
        s = summarize(rows, lo, asof)
        if s is not None:
            s.update({"id": wid, "label": label})
            windows.append(s)
    nxt = {dates[i]: (dates[i + 1] if i + 1 < len(dates) else None)
           for i in range(len(dates))}
    trades = [{"decided": d, "executed": nxt.get(d),
               "to": "long" if pos[i] else "flat", "reason": reasons[i]}
              for i, d in enumerate(dates) if reasons[i] and d >= freeze]
    return {"freeze": freeze, "asof": asof, "basis": BASIS, "windows": windows,
            "trades": trades}
