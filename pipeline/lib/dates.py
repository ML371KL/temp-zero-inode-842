"""Даты, время и торговый календарь.

Правила проекта: внутри всё в UTC, наружу (подписи, «данные на …») — МСК.
Время закрытия основной сессии берём из constants.TRADING_DAY_END_MSK, чтобы
не расползались две правды.

Про календарь честно: производственный календарь РФ задаётся постановлением
каждый год (переносы плавают), а МосБиржа с 2025 торгует и в выходные. Поэтому
is_trading_day() здесь — ЭВРИСТИКА, годная ровно для двух вещей: не долбить ISS
запросами по заведомо пустым дням (zcyc опрашивается по одному дню) и показать
«последний торговый день» до прихода данных. Единственный настоящий источник
правды о торговом дне — наличие точки в ряду; для этого есть
last_date_in_points().
"""

import re
from datetime import date, datetime, timedelta, timezone

try:  # пакет pipeline.lib
    from . import constants as _const
except ImportError:  # sys.path указывает внутрь pipeline/
    import constants as _const

MSK = timezone(timedelta(hours=_const.MSK_OFFSET_HOURS))
UTC = timezone.utc

_CLOSE_H, _CLOSE_M = (int(x) for x in _const.TRADING_DAY_END_MSK.split(":"))

_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_RU_RE = re.compile(r"^(\d{1,2})[./](\d{1,2})[./](\d{4})$")

# Праздники, добавленные руками (переносы конкретных лет, внеплановые нерабочие
# дни вроде 2020-03-30..04-30). Пополнять по факту, а не «на вырост».
EXTRA_HOLIDAYS = frozenset()


# ------------------------------------------------------------------ моменты
def utc_now():
    return datetime.now(UTC)


def msk_now():
    return datetime.now(MSK)


def today_msk():
    """Календарная дата в Москве — то, чем датируются интрадей-точки."""
    return msk_now().date()


def iso_utc(moment=None):
    """'2026-08-11T16:05:12Z' — формат fetched_at/generated_at из контракта §1."""
    m = moment or utc_now()
    if m.tzinfo is None:
        m = m.replace(tzinfo=UTC)
    return m.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(text):
    """Разбирает ISO-метку (с 'Z' или смещением) в aware-datetime UTC."""
    if not text:
        raise ValueError("пустая метка времени")
    s = text.strip().replace("Z", "+00:00")
    moment = datetime.fromisoformat(s)
    if moment.tzinfo is None:  # голое время трактуем как UTC — так пишем сами
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def age_minutes(ts_text, now=None):
    """Возраст метки в минутах — вход для SLA-статусов (constants.SLA_MINUTES)."""
    ref = now or utc_now()
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=UTC)
    return (ref - parse_ts(ts_text)).total_seconds() / 60.0


# -------------------------------------------------------------------- даты
def parse_date(value):
    """'YYYY-MM-DD', 'YYYY-MM-DDTHH:MM:SS', 'DD.MM.YYYY', 'DD/MM/YYYY' -> date."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    m = _ISO_RE.match(s)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = _RU_RE.match(s)
    if m:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    raise ValueError(f"не распознал дату: {value!r}")


def fmt_date(d):
    """Ключ точки ряда по контракту §1."""
    return parse_date(d).isoformat()


def fmt_ru(d, sep="."):
    """dd.mm.yyyy (формы ЦБ) или dd/mm/yyyy (XML_dynamic — там sep='/')."""
    x = parse_date(d)
    return f"{x.day:02d}{sep}{x.month:02d}{sep}{x.year}"


def add_days(d, n):
    return parse_date(d) + timedelta(days=n)


def days_between(a, b):
    return (parse_date(b) - parse_date(a)).days


def iter_days(start, end):
    """Календарные дни включительно."""
    cur, last = parse_date(start), parse_date(end)
    while cur <= last:
        yield cur
        cur += timedelta(days=1)


# -------------------------------------------------------- торговый календарь
def is_weekend(d):
    return parse_date(d).weekday() >= 5


def ru_holidays(year):
    """Нерабочие праздничные дни РФ по ТК + перенос с выходных на понедельник.

    Точные переносы каждого года не воспроизводим (их назначают постановлением):
    ошибка стоит одного пустого запроса к ISS, а не неверных данных.
    """
    fixed = [(1, d) for d in range(1, 9)]
    fixed += [(2, 23), (3, 8), (5, 1), (5, 9), (6, 12), (11, 4)]
    out = set()
    for month, day in fixed:
        d = date(year, month, day)
        out.add(d)
        if d.weekday() >= 5:  # праздник в выходной — ближайший рабочий день тоже выходной
            shift = 2 if d.weekday() == 5 else 1
            out.add(d + timedelta(days=shift))
    return frozenset(out)


def is_trading_day(d):
    d = parse_date(d)
    if is_weekend(d) or d in EXTRA_HOLIDAYS:
        return False
    return d not in ru_holidays(d.year)


def prev_trading_day(d):
    cur = parse_date(d) - timedelta(days=1)
    for _ in range(30):  # длиннее новогодних каникул подряд не бывает
        if is_trading_day(cur):
            return cur
        cur -= timedelta(days=1)
    return cur


def next_trading_day(d):
    cur = parse_date(d) + timedelta(days=1)
    for _ in range(30):
        if is_trading_day(cur):
            return cur
        cur += timedelta(days=1)
    return cur


def shift_trading_days(d, n):
    cur = parse_date(d)
    step = prev_trading_day if n < 0 else next_trading_day
    for _ in range(abs(n)):
        cur = step(cur)
    return cur


def iter_trading_days(start, end):
    for d in iter_days(start, end):
        if is_trading_day(d):
            yield d


def last_trading_day(now=None, require_close=True):
    """Последний торговый день.

    require_close=True (для дневного прогона): пока сессия не закрылась, «последним»
    считается вчерашний день — иначе дневной прогон, запущенный в обед, запишет
    промежуточное значение как закрытие.
    """
    moment = now or msk_now()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=MSK)
    moment = moment.astimezone(MSK)
    d = moment.date()
    closed = (moment.hour, moment.minute) >= (_CLOSE_H, _CLOSE_M)
    if is_trading_day(d) and (closed or not require_close):
        return d
    return prev_trading_day(d)


def last_date_in_points(points):
    """Настоящий «последний торговый день» ряда: максимальный ключ с не-None."""
    real = [k for k, v in (points or {}).items() if v is not None]
    return max(real) if real else None


# ------------------------------------------------------------------ месяцы
def month_start(d):
    x = parse_date(d)
    return date(x.year, x.month, 1)


def month_end(d):
    x = parse_date(d)
    if x.month == 12:
        return date(x.year, 12, 31)
    return date(x.year, x.month + 1, 1) - timedelta(days=1)


def add_months(d, n):
    """Сдвиг на n месяцев с прижатием к концу месяца (31.01 + 1 = 28/29.02)."""
    x = parse_date(d)
    total = (x.year * 12 + (x.month - 1)) + n
    year, month = divmod(total, 12)
    month += 1
    last = month_end(date(year, month, 1)).day
    return date(year, month, min(x.day, last))


def iter_months(start, end):
    """Концы месяцев в диапазоне — ключи месячных рядов по контракту §1.

    Отдаём только ЗАВЕРШЁННЫЕ месяцы (конец месяца <= end): иначе итератор до
    сегодняшнего дня выдаёт будущую дату, и месячный ряд получает точку вперёд.
    """
    cur = month_end(start)
    last = parse_date(end)
    while cur <= last:
        yield cur
        cur = month_end(add_months(cur, 1))


def apply_pub_lag(period_date, pub_lag_days):
    """Дата периода + лаг публикации -> дата ДОСТУПНОСТИ (registry.pub_lag_days).

    Ровно та операция, которой лагировались сигналы в валидации: значение за месяц
    нельзя было увидеть в день конца месяца, поэтому в панель оно попадает
    только с этой даты (validation/VALIDATION.md §1).
    """
    return parse_date(period_date) + timedelta(days=int(pub_lag_days or 0))
