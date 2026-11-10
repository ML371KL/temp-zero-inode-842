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
    from ..lib import calc, constants
except ImportError:
    from lib import calc, constants

__all__ = ["FREEZE", "RULE_SINCE", "SWITCH_COST", "LAG", "BASIS", "daily_rows", "summarize",
           "monthly_hits", "wilson", "months_back", "last_switch", "journal",
           "curve_positions", "CURVE_LABEL", "SAID_POS", "SAID_RULE", "said_load",
           "said_record", "version_on", "live_journal"]

# С этого дня слой решения (правило P2) ведёт позицию в проде (аудит 02.09.2026); до
# него позицию вело прежнее правило — так и подписана его карточка на витрине.
# Даты редакций живут в одном месте — constants.RULE_EDITIONS (аудит 10.11.2026).
FREEZE = constants.RULE_FREEZE
# С этого дня действует ТЕКУЩАЯ версия правила (constants.RULE_VERSION). История
# позиции в пересчёте построена ею целиком, поэтому всё раньше этой даты в пересчёте —
# расчёт на истории, а не то, что правило делало вживую. Вживую — said_* ниже.
RULE_SINCE = constants.RULE_SINCE
# Исполнение на следующем закрытии (DECISION["execution"]) и издержки смены: входят в
# отпечаток замороженного правила (tests/test_rule_freeze.py).
LAG = 1
SWITCH_COST = 0.002
BASIS = ("акции — индекс полной доходности MCFTR, деньги — вклады топ-10; исполнение "
         "на следующем закрытии, 0,2% за смену; налоги не учтены")
# Шарп над деньгами по окну короче квартала — шум, его не печатаем.
MIN_DAYS_SHARPE = 60
# Годовой темп окна «с начала года» — только с квартала: в январе две недели,
# возведённые в годовые, дают трёхзначные проценты, которые читаются как обещание.
# Вклады котируются в годовых, поэтому с квартала сравнение «в годовых» честнее
# накопленного итога, где ставка вкладов выглядит меньше, чем она есть.
YTD_ANN_MIN_YEARS = 0.25


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


def summarize(rows, lo=None, hi=None, ann_min_years=0.99):
    """Итог окна (lo, hi] -> dict | None (в окне нет ни одного дня).

    total_* — накопленные проценты за окно; ann_* — годовые (только для окна от
    ann_min_years, по умолчанию от года: годовой темп за месяц читается как
    обещание); excess_pp — правило минус вклады за окно, п.п.; ex_sharpe — Шарп
    дневного избытка над вкладами.
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
    if years >= ann_min_years:
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


def last_switch(dates, pos, reasons, asof):
    """Последняя смена позиции, которая уже ИСПОЛНЕНА к asof. -> (решено, исполнено, к) | None.

    Окно «с последней смены» начинается с закрытия дня исполнения: с него держится
    новая позиция, а комиссия смены падает на первый день окна. Смена, решённая в
    последний день (исполнение ещё впереди), окна не открывает — в нём пока прежняя
    позиция, и считать надо от предыдущей смены.
    """
    for i in range(len(dates) - 2, -1, -1):
        if reasons[i] and dates[i + 1] < asof:
            return dates[i], dates[i + 1], ("long" if pos[i] else "flat")
    return None


def _curve_cols(curve_rows, win):
    """Столбец простой кривой для окна правила: те же границы и тот же порог годовых."""
    if not curve_rows or win is None:
        return {}
    c = summarize(curve_rows, win["from"], win["to"], 0.0 if "rule_ann_pct" in win else 99.0)
    if c is None:
        return {}
    out = {"curve_pct": c["rule_pct"],
           "vs_curve_pp": round(win["rule_pct"] - c["rule_pct"], 2)}
    if "rule_ann_pct" in win and "rule_ann_pct" in c:
        out["curve_ann_pct"] = c["rule_ann_pct"]
    return out


def journal(dates, pos, reasons, tr, cash, freeze=FREEZE, rule_since=RULE_SINCE,
            lag=LAG, cost=SWITCH_COST, curve=None):
    """Блок verdict.position.journal: правило против удержания индекса и вкладов.

    Окна: с последней смены позиции — накопленные проценты; с начала года — ещё и
    годовые, как только прошёл квартал (YTD_ANN_MIN_YEARS); за 12 и 24 месяца — ещё и
    годовые. Сделки — с даты действующей редакции правила. Это ПЕРЕСЧЁТ истории
    нынешней версией; что панель говорила вживую — live_journal ниже.
    curve — позиции простой кривой (curve_positions): столбец сравнения в каждом окне.
    -> None, если считать не по чему (нет индекса полной доходности или ставки).
    """
    if not dates:
        return None
    rows = daily_rows(dates, pos, tr, cash, lag, cost)
    if not rows:
        return None
    curve_rows = daily_rows(dates, curve, tr, cash, lag, cost) if curve else None
    asof = rows[-1][0]
    windows = []
    sw = last_switch(dates, pos, reasons, asof)
    year_start = next((d for d in reversed(dates) if d < asof[:4] + "-01-01"), None)
    spans = []
    if sw:
        spans.append(("since_change", "с последней смены позиции", sw[1],
                      {"since_decided": sw[0], "since_executed": sw[1], "since_state": sw[2]}, 0.99))
    if year_start:
        spans.append(("ytd", "с начала года", year_start, {"year": asof[:4]},
                      YTD_ANN_MIN_YEARS))
    spans += [("m12", "12 месяцев", months_back(asof, 12), {}, 0.99),
              ("m24", "24 месяца", months_back(asof, 24), {}, 0.99)]
    for wid, label, lo, extra, ann_min in spans:
        s = summarize(rows, lo, asof, ann_min)
        if s is not None:
            s.update({"id": wid, "label": label}, **extra)
            s.update(_curve_cols(curve_rows, s))
            windows.append(s)
    nxt = {dates[i]: (dates[i + 1] if i + 1 < len(dates) else None)
           for i in range(len(dates))}
    trades = [{"decided": d, "executed": nxt.get(d),
               "to": "long" if pos[i] else "flat", "reason": reasons[i]}
              for i, d in enumerate(dates) if reasons[i] and d >= rule_since]
    return {"freeze": freeze, "rule_since": rule_since,
            "rule_version": constants.RULE_VERSION, "asof": asof, "basis": BASIS,
            "windows": windows, "trades": trades}


# ------------------------------------------------------------- простая кривая
# Ориентир без подбора (аудит 10.11.2026, §3.2): «акции», когда 10-летние ОФЗ доходнее
# 2-летних. Читается в те же дни решения, что и оценка рынка, исполняется так же и с
# теми же издержками — иначе сравнение мерило бы такт, а не правило. Ни одного
# подобранного числа: порог — ноль, окна нет.
CURVE_LABEL = "простая кривая: акции, когда 10-летние ОФЗ доходнее 2-летних"


def curve_positions(dates, slope, is_decision_day, start="2004-01-06"):
    """[0|1] — позиция простой кривой по закрытию дня (такт как у decision.run_automaton).

    В день решения при известном наклоне: 1, если slope > 0, иначе 0; между днями
    решения и при пропуске — прежняя. До start позиция 0, как у правила.
    """
    out, cur = [], 0
    slope = slope or []
    for t, d in enumerate(dates):
        v = slope[t] if t < len(slope) else None
        if is_decision_day[t] and calc.is_num(v):
            cur = 1 if v > 0 else 0
        out.append(cur if d >= start else 0)
    return out


# --------------------------------------------- живой журнал: что панель говорила
# Пересчёт (journal выше) переписывает прошлое при каждой правке правила: 21.10.2026
# одна настройка превратила «на 8,6 п.п. хуже вкладов за год» в «на 3,8 лучше» (аудит
# 10.11.2026, §3.4). Живой журнал — неизменяемый ряд в сторе: день → позиция, решённая
# по его закрытию, и версия правила, которая её решила. Прогон только ДОПИСЫВАЕТ новый
# последний день и никогда не трогает записанные; источника у ряда нет.
SAID_POS = "said_position"      # 1 — акции, 0 — деньги (решено по закрытию дня)
SAID_RULE = "said_rule"         # версия правила, решившая этот день
_SAID_META = {"source": "said", "cadence": "derived", "status": "ok"}


def _said_points(store, sid):
    getter = getattr(store, "load_series", None)
    if not callable(getter):
        return {}
    try:
        obj = getter(sid)
    except Exception:  # noqa: BLE001 — не прочитался: писать поверх нельзя
        return None
    if obj is None:
        # load_series отдаёт None и на отсутствие, и на нечитаемый файл; второе стор
        # различает сам (upsert_points откажется затирать), здесь — «журнала нет».
        return {}
    pts = obj.get("points") if isinstance(obj, dict) else None
    return {k: v for k, v in (pts or {}).items() if calc.is_num(v)}


def said_load(store):
    """{день: (0|1, версия)} — записанный журнал; {} — его ещё нет; None — не прочитался."""
    pos, ver = _said_points(store, SAID_POS), _said_points(store, SAID_RULE)
    if pos is None or ver is None:
        return None
    return {d: (int(round(v)), int(round(ver[d])) if d in ver else None)
            for d, v in sorted(pos.items())}


def version_on(day):
    """Версия правила, действовавшая в этот день (constants.RULE_EDITIONS)."""
    cur = None
    for ed in constants.RULE_EDITIONS:
        if ed["since"] <= day:
            cur = ed["version"]
    return cur


def _seed_said(dates, pos):
    """Затравка: что панель публиковала до первого прогона с журналом.

    До SAID_SEED_TO — смены из constants.SAID_SEED_CHANGES (настоящая история, сверена
    с событиями position_change); после — расчёт нынешней версии: она же и
    публиковалась в эти дни. Дни после затравки, но раньше RULE_SINCE, расчётом не
    заполняются: их решала другая версия (так бывает, только если стор потерян уже
    после следующей правки; тогда эти дни читаются протяжкой).
    """
    changes = sorted(constants.SAID_SEED_CHANGES)
    out = {}
    for t, d in enumerate(dates):
        if d < constants.SAID_SEED_FROM:
            continue
        if d <= constants.SAID_SEED_TO:
            state = None
            for day, st in changes:
                if day <= d:
                    state = st
            if state is not None:
                out[d] = (1 if state == "long" else 0, version_on(d))
        elif d >= RULE_SINCE:
            out[d] = (1 if pos[t] else 0, constants.RULE_VERSION)
    return out


def said_record(store, dates, pos):
    """Дописать в журнал последний день панели. -> (журнал {день: (0|1, версия)}, отчёт).

    Пустой журнал — затравка (_seed_said). Записанный день не переписывается никогда:
    если сегодняшний расчёт того же дня другой (данные пересмотрели задним числом), в
    журнале остаётся сказанное. Пропущенные прогоном дни не досчитываются: в те дни
    панель стояла на прежней витрине, и журнал читается прежней позицией
    (live_journal протягивает её вперёд).
    """
    report = {"seeded": False, "appended": [], "error": None}
    if not dates:
        return {}, report
    said = said_load(store)
    if said is None:
        report["error"] = "журнал не прочитался — не пишу поверх"
        return {}, report
    if not said:
        new = _seed_said(dates, pos)
        report["seeded"] = bool(new)
    else:
        last = dates[-1]
        new = {}
        if last > max(said) and last >= constants.PANEL_LAUNCH:
            new[last] = (1 if pos[-1] else 0, constants.RULE_VERSION)
    if new:
        fn = getattr(store, "upsert_points", None)
        if not callable(fn):
            report["error"] = "стор только для чтения"
            return said, report
        note = "живой журнал позиции ({}); пишет compute/track.py, записанное не переписывается"
        try:
            fn(SAID_POS, {d: float(v[0]) for d, v in new.items()},
               dict(_SAID_META, unit="0/1", note=note.format("1 — акции, 0 — деньги")))
            fn(SAID_RULE, {d: float(v[1]) for d, v in new.items()},
               dict(_SAID_META, unit="версия", note=note.format("версия правила")))
        except Exception as exc:  # noqa: BLE001 — журнал не важнее публикации
            report["error"] = f"{type(exc).__name__}: {exc}"[:200]
            return said, report
        said = dict(said)
        said.update(new)
        report["appended"] = sorted(new)
    return dict(sorted(said.items())), report


def _said_series(dates, said):
    """Журнал на календаре панели: протяжка сказанного на дни без записи."""
    out, cur, gaps = [], None, 0
    first = min(said) if said else None
    for d in dates:
        if d in said:
            cur = said[d][0]
        elif cur is not None and first and d > first:
            gaps += 1
        out.append(cur)
    return out, gaps


def live_journal(dates, said, tr, cash, curve=None, pos_now=None, lag=LAG, cost=SWITCH_COST):
    """verdict.position.journal.live: что дало то, что панель ГОВОРИЛА вживую.

    Окна «с запуска панели» и «при нынешней версии правила» (до последнего дня с
    индексом); в каждом — правило по журналу, простая кривая, акции (MCFTR) и вклады.
    spells — отрезки сказанной позиции: акции и вклады от исполнения смены до
    исполнения следующей (или до последнего дня). differs_days — дни нынешней версии,
    где сегодняшний пересчёт расходится со сказанным (данные пересмотрены задним
    числом). -> None, если журнала нет.
    """
    if not dates or not said:
        return None
    ser, gaps = _said_series(dates, said)
    held = [1 if v else 0 for v in ser]
    rows = daily_rows(dates, held, tr, cash, lag, cost)
    if not rows:
        return None
    curve_rows = daily_rows(dates, curve, tr, cash, lag, cost) if curve else None
    asof = rows[-1][0]
    windows = []
    for wid, label, lo in (("launch", "с запуска панели", constants.PANEL_LAUNCH),
                           ("version", "при нынешней версии правила", RULE_SINCE)):
        if lo >= asof:
            continue
        s = summarize(rows, lo, asof, 99.0)
        if s is None:
            continue
        s.update({"id": wid, "label": label})
        s.update(_curve_cols(curve_rows, s))
        windows.append(s)
    idx = {d: i for i, d in enumerate(dates)}
    trf, cf = calc.ffill(tr or []), calc.ffill(cash or [])

    def leg(a, b):
        """Акции и вклады от закрытия a до закрытия b (обе даты — дни панели)."""
        ia, ib = idx.get(a), idx.get(b)
        if ia is None or ib is None or ib <= ia or ib >= len(trf):
            return None, None
        x, y = trf[ia], trf[ib]
        r_tr = (y / x - 1.0) if calc.is_num(x) and calc.is_num(y) and x > 0 else None
        e = 1.0
        for i in range(ia + 1, ib + 1):
            c = cf[i - 1] if i - 1 < len(cf) else None
            if not calc.is_num(c):
                return r_tr, None
            days = (date.fromisoformat(dates[i][:10]) - date.fromisoformat(dates[i - 1][:10])).days
            e *= (1.0 + c / 100.0) ** (days / 365.0)
        return r_tr, e - 1.0

    spells, prev = [], None
    for t, d in enumerate(dates):
        if d not in said:
            continue
        v = said[d][0]
        if v == prev:
            continue
        execd = dates[t + 1] if t + 1 < len(dates) else None
        spells.append({"decided": d, "executed": execd, "to": "long" if v else "flat",
                       "version": said[d][1], "start": prev is None})
        prev = v
    for k, sp in enumerate(spells):
        a = sp["decided"] if sp["start"] else sp["executed"]
        nxt = spells[k + 1]["executed"] if k + 1 < len(spells) else None
        closed = bool(nxt and nxt <= asof)
        b = nxt if closed else asof
        sp["open"] = not closed
        sp["until"] = b
        if a and a < b:
            r_tr, r_c = leg(a, b)
            sp["tr_pct"] = _pct(r_tr) if r_tr is not None else None
            sp["cash_pct"] = _pct(r_c) if r_c is not None else None
    differs = None
    if pos_now is not None:
        differs = sum(1 for t, d in enumerate(dates)
                      if d in said and d >= RULE_SINCE and said[d][0] != (1 if pos_now[t] else 0))
    return {"launch": constants.PANEL_LAUNCH, "rule_version": constants.RULE_VERSION,
            "rule_since": RULE_SINCE, "asof": asof, "said_last": max(said),
            "said_days": sum(1 for d in said if d <= asof), "gap_days": gaps,
            "differs_days": differs, "windows": windows, "spells": spells,
            "curve": CURVE_LABEL, "basis": BASIS}
