"""Векторные утилиты на списках float|None — арифметика панели без pandas/numpy.

ПОЧЕМУ так, а не «как удобнее»: все пороги в constants.py (80-й перцентиль волы,
окно z=60 мес при min 24, −4% просадки RGBI) откалиброваны на pandas-версии расчёта
в validation/. Любое расхождение в семантике окна тихо сдвигает результат:
  * pandas rolling(w) по умолчанию min_periods=w — окно не отдаёт значение,
    пока не набралось w НЕпустых наблюдений (а не пока не прошло w строк);
  * .std() — выборочное (ddof=1), не популяционное;
  * .quantile(q) — линейная интерполяция (тип numpy по умолчанию), а не «ближайший»;
  * .rank(pct=True) — средние ранги на связях, деление на число непустых в окне.
Поэтому здесь всё повторено буквально, а не «примерно так же».

Все функции чистые, работают со списками float|None и корректно тянут None
(пропуск в середине ряда не должен превращаться в 0.0 — это было бы тихой ложью).
"""

import math
from bisect import bisect_left, insort
from collections import deque

__all__ = [
    "is_num", "log_return", "diff", "rolling_mean", "rolling_std", "rolling_max",
    "rolling_min", "rolling_quantile", "quantile", "zscore_rolling", "zscore_last",
    "percentile_rank_rolling", "drawdown_from_max", "realized_vol", "spearman_ic",
    "rank_average", "sign_changes", "hysteresis_sign", "ffill", "last_valid",
    "month_key", "month_end_indices", "resample_month_end",
]


def is_num(v):
    """Строгая проверка «это годное число».

    Отсеиваются четыре вида мусора, каждый из которых уже приезжал из парсеров:
    None и NaN (сравнение NaN > x молча даёт False и портит бит состояния),
    inf (одно деление на ноль — и весь ряд z уходит в NaN), строки ('н/д', '12,5')
    и bool (True прошёл бы как 1.0 и стал бы «ценой»).
    """
    if v is None or isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    return math.isfinite(v)


# --------------------------------------------------------------- преобразования
def log_return(xs, periods=1):
    """log(x[i] / x[i-periods]); None там, где база отсутствует или неположительна."""
    n = len(xs)
    out = [None] * n
    for i in range(periods, n):
        a, b = xs[i], xs[i - periods]
        if is_num(a) and is_num(b) and a > 0 and b > 0:
            out[i] = math.log(a / b)
    return out


def diff(xs, periods=1):
    """x[i] − x[i-periods]."""
    n = len(xs)
    out = [None] * n
    for i in range(periods, n):
        a, b = xs[i], xs[i - periods]
        if is_num(a) and is_num(b):
            out[i] = a - b
    return out


def ffill(xs, limit=None):
    """Протяжка последнего значения вперёд.

    limit считается в СТРОКАХ (как pandas .ffill(limit=N) после reindex на календарь):
    значение живёт свою строку плюс не более limit следующих. Ограничение нужно,
    чтобы мёртвый источник не изображал свежие данные месяцами.
    """
    out = []
    cur, age = None, 0
    for v in xs:
        if is_num(v):
            cur, age = float(v), 0
        elif cur is not None:
            age += 1
        if cur is not None and (limit is None or age <= limit):
            out.append(cur)
        else:
            out.append(None)
    return out


def last_valid(xs):
    """(индекс, значение) последнего непустого элемента; (None, None) если таких нет."""
    for i in range(len(xs) - 1, -1, -1):
        if is_num(xs[i]):
            return i, xs[i]
    return None, None


# ------------------------------------------------------------------ окна
def _mp(window, min_periods):
    return window if min_periods is None else min_periods


def rolling_mean(xs, window, min_periods=None):
    mp = _mp(window, min_periods)
    n = len(xs)
    out = [None] * n
    s, c = 0.0, 0
    for i in range(n):
        v = xs[i]
        if is_num(v):
            s += v
            c += 1
        j = i - window
        if j >= 0 and is_num(xs[j]):
            s -= xs[j]
            c -= 1
        if c >= mp and c > 0:
            out[i] = s / c
    return out


def rolling_std(xs, window, min_periods=None, ddof=1):
    """Выборочное СКО в окне (ddof=1, как pandas).

    Считаем в два прохода по окну, а не через сумму квадратов: на рядах вроде курса
    (уровень ~80, дисперсия крошечная) инкрементальная сумма квадратов теряет знаки
    после запятой на вычитании — z-скор потом «плавает» в третьем знаке.
    """
    mp = _mp(window, min_periods)
    n = len(xs)
    out = [None] * n
    for i in range(n):
        lo = max(0, i - window + 1)
        vals = [v for v in xs[lo:i + 1] if is_num(v)]
        c = len(vals)
        if c < mp or c <= ddof:
            continue
        m = sum(vals) / c
        out[i] = math.sqrt(sum((v - m) ** 2 for v in vals) / (c - ddof))
    return out


def rolling_max(xs, window, min_periods=None):
    return _rolling_extreme(xs, window, min_periods, is_max=True)


def rolling_min(xs, window, min_periods=None):
    return _rolling_extreme(xs, window, min_periods, is_max=False)


def _rolling_extreme(xs, window, min_periods, is_max):
    """Монотонная очередь: 252-дневный максимум по всей истории иначе стоит секунды."""
    mp = _mp(window, min_periods)
    n = len(xs)
    out = [None] * n
    dq = deque()  # индексы, значения по убыванию (для max) / по возрастанию (для min)
    cnt = 0
    for i in range(n):
        v = xs[i]
        if is_num(v):
            cnt += 1
            while dq and ((xs[dq[-1]] <= v) if is_max else (xs[dq[-1]] >= v)):
                dq.pop()
            dq.append(i)
        j = i - window
        if j >= 0 and is_num(xs[j]):
            cnt -= 1
        while dq and dq[0] <= j:
            dq.popleft()
        if cnt >= mp and dq:
            out[i] = xs[dq[0]]
    return out


def quantile(values, q):
    """Квантиль типа «linear» (numpy/pandas по умолчанию): позиция (n−1)*q с интерполяцией."""
    vals = sorted(v for v in values if is_num(v))
    n = len(vals)
    if n == 0:
        return None
    if n == 1:
        return vals[0]
    pos = (n - 1) * q
    lo = int(math.floor(pos))
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return vals[lo] * (1.0 - frac) + vals[hi] * frac


def rolling_quantile(xs, window, q, min_periods=None):
    """Скользящий квантиль. Окно держим отсортированным (bisect), иначе 756-дневный
    порог волы по 22 годам истории пересортировывает список на каждом шаге."""
    mp = _mp(window, min_periods)
    n = len(xs)
    out = [None] * n
    buf = []
    for i in range(n):
        v = xs[i]
        if is_num(v):
            insort(buf, float(v))
        j = i - window
        if j >= 0 and is_num(xs[j]):
            k = bisect_left(buf, float(xs[j]))
            if k < len(buf) and buf[k] == xs[j]:
                del buf[k]
        c = len(buf)
        if c >= mp and c > 0:
            if c == 1:
                out[i] = buf[0]
            else:
                pos = (c - 1) * q
                lo = int(math.floor(pos))
                hi = min(lo + 1, c - 1)
                frac = pos - lo
                out[i] = buf[lo] * (1.0 - frac) + buf[hi] * frac
    return out


def zscore_rolling(xs, window, min_periods=None, clip=None):
    """(x − скользящее среднее) / скользящее СКО, опционально обрезанный по ±clip.

    Окно ВКЛЮЧАЕТ текущую точку — именно так считались z в walkforward.py; сдвиг на
    один шаг назад даёт другой ряд и другой знак композита на разворотах.
    """
    m = rolling_mean(xs, window, min_periods)
    s = rolling_std(xs, window, min_periods)
    out = [None] * len(xs)
    for i, v in enumerate(xs):
        if is_num(v) and is_num(m[i]) and is_num(s[i]) and s[i] > 0:
            z = (v - m[i]) / s[i]
            if clip is not None:
                z = max(-clip, min(clip, z))
            out[i] = z
    return out


def zscore_last(xs, window, min_periods=None, clip=None):
    """z только для последней точки ряда — когда история z не нужна (дешевле в разы)."""
    if not xs or not is_num(xs[-1]):
        return None
    mp = _mp(window, min_periods)
    vals = [v for v in xs[-window:] if is_num(v)]
    if len(vals) < mp or len(vals) < 2:
        return None
    m = sum(vals) / len(vals)
    sd = math.sqrt(sum((v - m) ** 2 for v in vals) / (len(vals) - 1))
    if sd <= 0:
        return None
    z = (xs[-1] - m) / sd
    if clip is not None:
        z = max(-clip, min(clip, z))
    return z


def percentile_rank_rolling(xs, window, min_periods=None):
    """Перцентиль текущего значения внутри окна (pandas .rolling().rank(pct=True)).

    Связи усредняются. ВНИМАНИЕ: для позиции физлиц (futoi) эта нормировка СЛОМАНА
    структурным сдвигом уровня (REGIME.md §5) — там используется z-120, а не перцентиль.
    Функция оставлена для мониторов, не для сигналов.
    """
    mp = _mp(window, min_periods)
    n = len(xs)
    out = [None] * n
    for i in range(n):
        v = xs[i]
        if not is_num(v):
            continue
        lo = max(0, i - window + 1)
        vals = [u for u in xs[lo:i + 1] if is_num(u)]
        c = len(vals)
        if c < mp or c == 0:
            continue
        less = sum(1 for u in vals if u < v)
        eq = sum(1 for u in vals if u == v)
        out[i] = (less + (eq + 1) / 2.0) / c
    return out


def drawdown_from_max(xs, window, min_periods=None):
    """log(x / скользящий максимум) — лог-просадка, всегда ≤ 0."""
    mx = rolling_max(xs, window, min_periods)
    out = [None] * len(xs)
    for i, v in enumerate(xs):
        if is_num(v) and is_num(mx[i]) and v > 0 and mx[i] > 0:
            out[i] = math.log(v / mx[i])
    return out


def realized_vol(returns, window=21, annualized=True, periods_per_year=252):
    """Реализованная вола: СКО дневных лог-доходностей × sqrt(252)."""
    sd = rolling_std(returns, window)
    k = math.sqrt(periods_per_year) if annualized else 1.0
    return [None if not is_num(v) else v * k for v in sd]


# ------------------------------------------------------------------ статистика
def rank_average(vals):
    """Ранги 1..n со средним рангом на связях (как scipy.stats.rankdata)."""
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    ranks = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman_ic(xs, ys):
    """Ранговая корреляция Спирмена по парам, где обе величины есть.

    Возвращает (rho|None, n). Связи обрабатываются средними рангами — на месячных
    выборках с повторяющимися нулями (например, ставка не менялась) иначе получаются
    завышенные |rho|.
    """
    pairs = [(x, y) for x, y in zip(xs, ys) if is_num(x) and is_num(y)]
    n = len(pairs)
    if n < 3:
        return None, n
    rx = rank_average([p[0] for p in pairs])
    ry = rank_average([p[1] for p in pairs])
    mx = sum(rx) / n
    my = sum(ry) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sxx = sum((a - mx) ** 2 for a in rx)
    syy = sum((b - my) ** 2 for b in ry)
    if sxx <= 0 or syy <= 0:
        return None, n
    return sxy / math.sqrt(sxx * syy), n


def sign_changes(xs):
    """Индексы, где знак сменился относительно предыдущего ненулевого значения."""
    out = []
    prev = 0
    for i, v in enumerate(xs):
        if not is_num(v) or v == 0:
            continue
        s = 1 if v > 0 else -1
        if prev and s != prev:
            out.append(i)
        prev = s
    return out


def hysteresis_sign(xs, threshold=0.0):
    """Знак с гистерезисом: переключается только при |x| > threshold.

    Без гистерезиса композит около нуля дребезжит и рассылает «развороты ядра»
    каждый второй день — ровно тот шум, который walk-forward назвал провалом M2.
    Возвращает список знаков (+1/−1) и None до первого уверенного пересечения.
    """
    out = []
    s = 0
    for v in xs:
        if is_num(v):
            if v > threshold:
                s = 1
            elif v < -threshold:
                s = -1
        out.append(s if s else None)
    return out


# ------------------------------------------------------------------ календарь
def month_key(date_str):
    """'2026-08-11' -> '2026-08'."""
    return date_str[:7]


def month_end_indices(dates):
    """Индексы ПОСЛЕДНИХ торговых дней каждого календарного месяца."""
    out = []
    for i, d in enumerate(dates):
        if i + 1 == len(dates) or month_key(dates[i + 1]) != month_key(d):
            out.append(i)
    return out


def resample_month_end(dates, values):
    """Месячная выборка: (метки, значения).

    Метка — последний ТОРГОВЫЙ день месяца (валидация использовала календарный конец
    месяца как ярлык, но брала то же самое значение). Значение — последнее непустое
    в месяце: pandas .resample('ME').last() пропускает NaN, и если ряд не обновился
    в последний день месяца, берётся предыдущее наблюдение того же месяца.
    """
    labels, vals = [], []
    cur_key, cur_val = None, None
    for i, d in enumerate(dates):
        k = month_key(d)
        if k != cur_key:
            cur_key, cur_val = k, None
        if is_num(values[i]):
            cur_val = values[i]
        if i + 1 == len(dates) or month_key(dates[i + 1]) != k:
            labels.append(d)
            vals.append(cur_val)
    return labels, vals
