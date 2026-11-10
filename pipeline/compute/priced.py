"""Что в цене: разложение дневного хода индекса на ставки, нефть в рублях, разрядку и
остаток (аудит 10.11.2026, этап 3; метод — разбор роста июля–октября 2026,
moex-rally-2026-10/REPORT.md, «геобета»).

После 2022 года рынок двигают новости о переговорах и санкциях, а три показателя оценки
рынка их не видят и видеть не могут. Предсказать такие новости нельзя, но измерить,
сколько разрядки уже в цене, можно. Здесь — только измерение; в решение не входит.

Устройство (всё — только на прошлом, ни одного заглядывания вперёд):

* ход индекса — дневная лог-доходность IMOEX;
* «ставки» — дневное изменение доходности 10-летних ОФЗ (КБД), п.п.;
* «нефть в рублях» — лог-изменение Brent × юань/рубль (рубль меряется биржевым юанем:
  доллара на бирже нет с 2024 года, а курс доллара к юаню за день почти не ходит).
  Brent — FRED (спот, с лагом публикации 3–7 дней); хвост, который FRED ещё не
  опубликовал, продолжен ходом ближайшего фьючерса МосБиржи;
* «разрядка» — доходность корзины «чувствительные к новостям минус нечувствительные»:
  верхняя пятая часть бумаг ширины рынка (px_*) по чувствительности минус нижняя.
  Чувствительность бумаги — средний перевес её доходности над индексом в дни событий
  реестра (events_registry: санкции и переговоры), со знаком ожидаемого влияния
  события, и только по событиям СТРОГО РАНЬШЕ дня корзины;
* коэффициенты — скользящие регрессии по данным ДО ВЧЕРАШНЕГО дня: индекс на ставки и
  нефть (окно 252 дня), затем остаток этой регрессии на корзину (окно 252 дня); вклад
  дня = вчерашний коэффициент × сегодняшний ход показателя; остаток — всё, что не
  объяснено, вместе со свободным членом. Вклады и остаток в сумме дают ход индекса
  ТОЧНО.

РАЗРЯДКА ВЫКЛЮЧЕНА (DETENTE = False). Приёмка задания 10.11.2026 требовала: за октябрь
2026 главный вклад — разрядка, а на случайных датах событий корзина ничего не объясняет.
На копии боевого стора 09.11.2026 оба условия не выполнены: в реестре событий четыре
геополитических события (16.07–07.08.2026), корзина есть с 29.07, коэффициент — с 26.08;
за 28.09–26.10 (индекс +10,4 лп) ставки дали +1,6, нефть +0,1, разрядка +1,2, остальное
+7,4; на 200 наборах случайных дат событий корзина в 49 случаях объяснила октябрь не хуже
настоящей. Натягивать не стали: блок выходит со ставками, нефтью и остатком, а расчёт
корзины оставлен для проверки, когда реестр событий станет длиннее (включение — новым
решением владельца и той же приёмкой: work-exec/eval_priced.py в папке аудита).

Чистая стандартная библиотека.
"""

import math
import random

try:
    from ..lib import calc
except ImportError:
    from lib import calc

__all__ = ["WINDOW", "MIN_OBS", "DET_MIN_OBS", "MIN_EVENTS", "GEO_TYPES", "HORIZONS",
           "log_returns", "event_days", "sensitivities", "basket", "rolling_ols",
           "decompose", "summary", "placebo_events"]

WINDOW = 252          # окно скользящей регрессии, торговых дней
MIN_OBS = 120         # меньше наблюдений в окне — коэффициента нет, вклад уходит в остаток
DET_MIN_OBS = 20      # корзина короче — разрядки нет (реестр событий ведётся с июля 2026)
MIN_EVENTS = 3        # событий в прошлом меньше — чувствительность не меряется
QUANTILE = 5          # пятые части по чувствительности, как в разборе роста
GEO_TYPES = ("sanction_us", "sanction_eu", "peace_pos", "peace_neg")
HORIZONS = (5, 21, 63)
DETENTE = False       # см. докстринг: приёмка (в) и (г) 10.11.2026 не пройдена


def log_returns(xs):
    out = [None] * len(xs)
    for t in range(1, len(xs)):
        a, b = xs[t - 1], xs[t]
        if calc.is_num(a) and calc.is_num(b) and a > 0 and b > 0:
            out[t] = math.log(b / a)
    return out


def diffs(xs):
    out = [None] * len(xs)
    for t in range(1, len(xs)):
        a, b = xs[t - 1], xs[t]
        if calc.is_num(a) and calc.is_num(b):
            out[t] = b - a
    return out


def event_days(dates, events):
    """[(индекс дня реакции, знак)] — первый торговый день не раньше даты события."""
    out = []
    for day, sign in sorted(events):
        if sign not in (1, -1):
            continue
        t = next((i for i, d in enumerate(dates) if d >= day), None)
        if t is not None:
            out.append((t, sign))
    return out


def sensitivities(stock_rets, idx_ret, ev, before):
    """{бумага: средний перевес над индексом со знаком события} по событиям с днём < before."""
    past = [(t, s) for t, s in ev if t < before]
    if len(past) < MIN_EVENTS:
        return {}
    out = {}
    for name, r in stock_rets.items():
        xs = [s * (r[t] - idx_ret[t]) for t, s in past
              if calc.is_num(r[t]) and calc.is_num(idx_ret[t])]
        if len(xs) >= MIN_EVENTS:
            out[name] = sum(xs) / len(xs)
    return out


def basket(dates, stock_rets, idx_ret, ev):
    """Дневная доходность корзины «верхняя пятая минус нижняя» (None — корзины нет).

    Состав пересобирается только в день реакции на новое событие (между событиями
    чувствительности не меняются), поэтому цена — O(событий × бумаг), а не O(дней).
    """
    n = len(dates)
    out = [None] * n
    ev_days = sorted({t for t, _ in ev})
    top, bot, cur_key = [], [], None
    for t in range(n):
        key = sum(1 for d in ev_days if d < t)
        if key != cur_key:
            cur_key = key
            sens = sensitivities(stock_rets, idx_ret, ev, t)
            if len(sens) >= 2 * QUANTILE:
                ranked = sorted(sens, key=lambda k: (sens[k], k))
                k = max(1, len(ranked) // QUANTILE)
                bot, top = ranked[:k], ranked[-k:]
            else:
                bot, top = [], []
        if not top:
            continue
        up = [stock_rets[x][t] for x in top if calc.is_num(stock_rets[x][t])]
        dn = [stock_rets[x][t] for x in bot if calc.is_num(stock_rets[x][t])]
        if len(up) >= len(top) // 2 + 1 and len(dn) >= len(bot) // 2 + 1:
            out[t] = sum(up) / len(up) - sum(dn) / len(dn)
    return out


def _solve(a, b):
    """Решение малой системы a·x = b (Гаусс с выбором ведущего); None — вырождена."""
    m = len(b)
    a = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(m):
        p = max(range(c, m), key=lambda r: abs(a[r][c]))
        if abs(a[p][c]) < 1e-12:
            return None
        a[c], a[p] = a[p], a[c]
        for r in range(m):
            if r != c:
                f = a[r][c] / a[c][c]
                for k in range(c, m + 1):
                    a[r][k] -= f * a[c][k]
    return [a[i][m] / a[i][i] for i in range(m)]


def rolling_ols(y, xs, window=WINDOW, min_obs=MIN_OBS):
    """Коэффициенты на день t по дням [t−window, t−1] — СТРОГО до t.

    y — [float|None], xs — список регрессоров той же длины; свободный член добавляется.
    -> [None | [α, β1, …]]. Суммы окна ведутся накопительно: O(дней × k²).
    """
    n, k = len(y), len(xs) + 1
    sxx = [[0.0] * k for _ in range(k)]
    sxy = [0.0] * k
    cnt = 0
    rows = [None] * n
    for t in range(n):
        if calc.is_num(y[t]) and all(calc.is_num(x[t]) for x in xs):
            rows[t] = [1.0] + [x[t] for x in xs]
    out = [None] * n
    for t in range(n):
        # окно для t — дни t−window … t−1: добавить t−1, убрать t−1−window
        add, drop = t - 1, t - 1 - window
        for j, sign in ((add, 1.0), (drop, -1.0)):
            if j >= 0 and rows[j] is not None:
                v = rows[j]
                for a_ in range(k):
                    sxy[a_] += sign * v[a_] * y[j]
                    for b_ in range(k):
                        sxx[a_][b_] += sign * v[a_] * v[b_]
                cnt += 1 if sign > 0 else -1
        if cnt >= min_obs:
            out[t] = _solve([r[:] for r in sxx], sxy[:])
    return out


def decompose(dates, idx_px, y10, oil_rub, stock_px, events, window=WINDOW,
              with_detente=None):
    """Дневное разложение. -> dict рядов той же длины, что dates (значения — доли, не %).

    r — ход индекса; c_rates, c_oil, c_det — вклады; other — остаток (с α);
    basket — доходность корзины разрядки; tickers_top/bottom — состав на последний день.
    """
    n = len(dates)
    if with_detente is None:
        with_detente = DETENTE
    r = log_returns(idx_px)
    f_rates, f_oil = diffs(y10), log_returns(oil_rub)
    ev, b = [], [None] * n
    if with_detente:
        stock_rets = {k: log_returns(v) for k, v in stock_px.items()}
        ev = event_days(dates, events)
        b = basket(dates, stock_rets, r, ev)
    macro = rolling_ols(r, [f_rates, f_oil], window, MIN_OBS)
    c_rates, c_oil = [0.0] * n, [0.0] * n
    resid1 = [None] * n
    for t in range(n):
        if not calc.is_num(r[t]):
            continue
        beta = macro[t]
        if beta is not None:
            if calc.is_num(f_rates[t]):
                c_rates[t] = beta[1] * f_rates[t]
            if calc.is_num(f_oil[t]):
                c_oil[t] = beta[2] * f_oil[t]
            if calc.is_num(f_rates[t]) and calc.is_num(f_oil[t]):
                resid1[t] = r[t] - beta[0] - c_rates[t] - c_oil[t]
    det = rolling_ols(resid1, [b], window, DET_MIN_OBS) if with_detente else [None] * n
    c_det, other = [0.0] * n, [None] * n
    for t in range(n):
        if not calc.is_num(r[t]):
            continue
        if det[t] is not None and calc.is_num(b[t]):
            c_det[t] = det[t][1] * b[t]
        other[t] = r[t] - c_rates[t] - c_oil[t] - c_det[t]
    return {"r": r, "c_rates": c_rates, "c_oil": c_oil, "c_det": c_det, "other": other,
            "basket": b, "beta_macro": macro, "beta_det": det, "events": ev}


def _window_sum(res, lo, hi, key):
    return sum(res[key][t] for t in range(lo, hi + 1) if calc.is_num(res[key][t]))


def _corr2(pairs):
    """Квадрат корреляции дневного хода индекса и суммы вкладов показателей."""
    if len(pairs) < 3:
        return None
    xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxy = sum((x - mx) * (y - my) for x, y in pairs)
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy * sxy / (sxx * syy)


def summary(dates, res, horizons=HORIZONS, end=None):
    """Накопленные вклады за последние N сессий, лог-пункты (×100), и доля объяснённого.

    Доля объяснённого — квадрат корреляции дневного хода индекса с суммой вкладов
    показателей по дням окна: какая часть дневных колебаний индекса совпала с ходом
    ставок и нефти (вне выборки — коэффициенты вчерашние). От 0 до 1; накопленный
    итог окна она не описывает — для него есть сами вклады.
    """
    n = len(dates)
    hi = n - 1 if end is None else end
    out = []
    for h in horizons:
        lo = hi - h + 1
        if lo < 1:
            continue
        idx = _window_sum(res, lo, hi, "r")
        parts = {k: _window_sum(res, lo, hi, "c_" + k) for k in ("rates", "oil", "det")}
        other = _window_sum(res, lo, hi, "other")
        pairs = [(res["r"][t], res["r"][t] - res["other"][t]) for t in range(lo, hi + 1)
                 if calc.is_num(res["r"][t]) and calc.is_num(res["other"][t])]
        expl = _corr2(pairs)
        out.append({"sessions": h, "from": dates[lo - 1], "to": dates[hi],
                    "index_lp": round(idx * 100, 2),
                    "index_pct": round((math.exp(idx) - 1) * 100, 1),
                    "rates_lp": round(parts["rates"] * 100, 2),
                    "oil_lp": round(parts["oil"] * 100, 2),
                    "detente_lp": round(parts["det"] * 100, 2),
                    "other_lp": round(other * 100, 2),
                    "explained": round(expl, 2) if expl is not None else None})
    return out


def _points(store, sid):
    getter = getattr(store, "load_series", None)
    try:
        obj = getter(sid) if callable(getter) else (store or {}).get(sid)
    except Exception:  # noqa: BLE001 — битый ряд: «ряда нет»
        return {}, {}
    if not isinstance(obj, dict):
        return {}, {}
    return obj.get("points") or {}, obj.get("meta") or {}


def _exact(points, dates):
    return [points.get(d) if calc.is_num(points.get(d)) else None for d in dates]


def oil_rub_series(dates, brent_pts, futures_pts, cny):
    """Brent (FRED) × юань/рубль на календаре индекса; хвост после последней точки FRED
    продолжен ходом фьючерса МосБиржи (цепью от последнего значения FRED)."""
    br = _exact(brent_pts, dates)
    fut = _exact(futures_pts, dates)
    last = max((t for t, v in enumerate(br) if calc.is_num(v)), default=None)
    if last is not None:
        lvl = br[last]
        for t in range(last + 1, len(dates)):
            a, b = fut[t - 1], fut[t]
            if lvl is not None and calc.is_num(a) and calc.is_num(b) and a > 0:
                lvl = lvl * b / a
                br[t] = lvl
            else:
                lvl = None
    return [b * c if calc.is_num(b) and calc.is_num(c) else None for b, c in zip(br, cny)]


def inputs(store, panel):
    """Ряды для decompose из стора и панели: индекс, 10 лет ОФЗ, нефть в рублях, бумаги
    ширины рынка (с поправкой на дробления), события реестра (санкции и переговоры)."""
    try:
        from ..fetch import iss
    except ImportError:  # pragma: no cover
        from fetch import iss
    dates = panel.get("dates") or []
    cols = panel.get("cols") or {}
    brent, _ = _points(store, "brent")
    fut, _ = _points(store, "brent_moex")
    cny_pts, _ = _points(store, "cny_tom")
    oil = oil_rub_series(dates, brent, fut, _exact(cny_pts, dates))
    stocks = {}
    for ticker in iss.BREADTH_TICKERS:
        pts, _ = _points(store, "px_" + ticker.lower())
        if pts:
            pts = iss._apply_corporate_actions(ticker, {k: v for k, v in pts.items()
                                                        if calc.is_num(v)})
            stocks[ticker] = _exact(pts, dates)
    _, meta = _points(store, "events_registry")
    events = [(str(rec.get("date"))[:10], int(rec.get("expected_sign") or 0))
              for rec in meta.get("records") or []
              if rec.get("type") in GEO_TYPES and rec.get("date")]
    return {"dates": dates, "idx": cols.get("imoex") or [], "y10": cols.get("y10") or [],
            "oil": oil, "stocks": stocks, "events": events}


def block(store, panel):
    """verdict.priced: разложение последних 5, 21 и 63 сессий. None — считать не по чему."""
    inp = inputs(store, panel)
    dates = inp["dates"]
    if len(dates) < MIN_OBS + max(HORIZONS) + 2:
        return None
    res = decompose(dates, inp["idx"], inp["y10"], inp["oil"], inp["stocks"], inp["events"])
    wins = summary(dates, res)
    if not wins:
        return None
    if not DETENTE:
        for w in wins:
            w.pop("detente_lp", None)
    beta = next((b for b in reversed(res["beta_macro"]) if b is not None), None)
    oil_last = max((t for t, v in enumerate(inp["oil"]) if calc.is_num(v)), default=None)
    return {"asof": dates[-1], "windows": wins, "detente": DETENTE,
            "rates_pct_per_pp": round(beta[1] * 100, 2) if beta else None,
            "oil_pct_per_pct": round(beta[2], 3) if beta else None,
            "oil_through": dates[oil_last] if oil_last is not None else None,
            "window": WINDOW,
            "basis": "дневные лог-доходности IMOEX; ставки — изменение доходности 10-летних ОФЗ, "
                     "нефть — Brent × юань/рубль; коэффициенты — скользящая регрессия за 252 дня "
                     "до вчерашнего дня; вклады в лог-пунктах"}


def placebo_events(dates, events, rng, lo=None, hi=None):
    """Те же события со случайными днями (в том же промежутке) и случайными знаками."""
    pool = [d for d in dates if (lo is None or d >= lo) and (hi is None or d <= hi)]
    return [(rng.choice(pool), rng.choice((1, -1))) for _ in events]
