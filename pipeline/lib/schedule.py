"""Когда конвейер публикует — по самим юнитам `ops/*.timer`, а не по копии расписания.

ЗАЧЕМ ЭТО КОНВЕЙЕРУ. Витрина обязана отличать «данные протухли» от «сейчас ночь и
такта не было по расписанию». Без этого баннер «Данные устарели» загорался КАЖДУЮ
ночь: последняя публикация дня — 21:00 UTC (00:00 МСК), первая следующая — 07:00 UTC
(10:00 МСК), а норма стояла плоская, 150 минут круглые сутки. Читатель в 05:50 МСК
видел «Числа на экране — последние успешные, а не сегодняшние», хотя числа были
именно сегодняшние: биржа закрылась в 23:50 и с тех пор не произошло ничего.
Тревога, которая горит семь часов из каждых суток, перестаёт означать что-либо —
ровно та же болезнь, от которой в alerts.py лечатся «переходами вместо состояний».

ПОЧЕМУ ЧИТАЕМ ЮНИТЫ, А НЕ КОНСТАНТУ. Расписание уже живёт в `ops/*.timer`, и это
единственное, что реально запускает прогоны. Любая копия в коде разъезжается с ним
молча — так уже было с шагом такта (15 минут в документации против 5 в юните).

Разбирается ровно то подмножество календаря systemd, которым пользуется проект:
`Mon..Fri *-*-* 07..20:00,05,10 UTC`. Незнакомая форма не угадывается, а честно
пропускается: лучше отказаться от подсказки, чем построить неверное ожидание.
"""

import glob
import os
import re
from datetime import datetime, timedelta, timezone

__all__ = ["starts_of_day", "max_starts_in_window", "next_publish_at", "expand_field"]

ON_CALENDAR = re.compile(r"^OnCalendar=(.+?)\s*$", re.M)
DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
MINUTES_IN_DAY = 24 * 60


def expand_field(field, lo, hi):
    """Поле календаря ('*', '7..20', '0,5,10', '*/5') -> множество значений."""
    out = set()
    for part in str(field).split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            part, raw = part.split("/", 1)
            try:
                step = max(1, int(raw))
            except ValueError:
                return set()
        try:
            if part in ("*", ""):
                first, last = lo, hi
            elif ".." in part:
                first, last = (int(x) for x in part.split("..", 1))
            else:
                first = last = int(part)
        except ValueError:
            return set()   # форма незнакомая — молча не угадываем
        out |= set(range(first, last + 1, step))
    return {v for v in out if lo <= v <= hi}


def _weekdays(token):
    """'Mon..Fri' -> {0,1,2,3,4}; 'Sat,Sun' -> {5,6}; не день недели -> все дни."""
    low = token.lower().rstrip(",")
    if not any(name in low for name in DAYS):
        return set(range(7))
    days = set()
    for part in low.split(","):
        part = part.strip()
        if ".." in part:
            a, b = part.split("..", 1)
            if a in DAYS and b in DAYS:
                i, j = DAYS[a], DAYS[b]
                days |= {d % 7 for d in range(i, j + 1 if j >= i else j + 8)}
        elif part in DAYS:
            days.add(DAYS[part])
    return days or set(range(7))


def parse_calendar(line):
    """Строка OnCalendar -> (дни недели, минуты суток). Непонятная -> (set(), set())."""
    tokens = line.replace(" UTC", "").split()
    if not tokens:
        return set(), set()
    clock = next((t for t in tokens if ":" in t), None)
    if clock is None:
        return set(), set()
    parts = clock.split(":")
    hours = expand_field(parts[0], 0, 23)
    mins = expand_field(parts[1], 0, 59) if len(parts) > 1 else {0}
    if not hours or not mins:
        return set(), set()
    return _weekdays(tokens[0]), {h * 60 + m for h in hours for m in mins}


def starts_of_day(calendar_lines, weekday=None):
    """Минуты суток, в которые таймер сработает. weekday=None — самый плотный день."""
    minutes = set()
    for line in calendar_lines:
        days, mins = parse_calendar(line)
        if weekday is None or weekday in days:
            minutes |= mins
    return sorted(minutes)


def max_starts_in_window(starts, window_min):
    """Сколько срабатываний влезает в самое плотное окно такой длины."""
    if not starts:
        return 0
    doubled = list(starts) + [m + MINUTES_IN_DAY for m in starts]
    best = 1
    for i, first in enumerate(starts):
        best = max(best, sum(1 for t in doubled[i:] if t - first < window_min))
    return best


def _timer_files(ops_dir):
    return sorted(glob.glob(os.path.join(ops_dir, "*.timer")))


def publishing_calendars(ops_dir, modes=None):
    """Строки OnCalendar тех юнитов, что действительно ПУБЛИКУЮТ витрину.

    Реколибровка сюда не входит: она ничего не пишет в `data.json`, и ждать от неё
    обновления витрины — значит построить ожидание, которое никогда не сбудется.
    """
    skip = set(modes or ("recalibrate",))
    lines = []
    for path in _timer_files(ops_dir):
        base = os.path.basename(path)[: -len(".timer")]
        if base.rsplit("-", 1)[-1] in skip:
            continue
        with open(path, encoding="utf-8") as fh:
            lines += ON_CALENDAR.findall(fh.read())
    return lines


def next_publish_at(now=None, ops_dir=None, horizon_days=8):
    """Ближайший момент СТРОГО после `now`, когда витрину ждёт очередная публикация.

    -> datetime в UTC или None, если расписание прочитать не удалось (тогда витрина
    остаётся на плоской норме — подсказки не будет, но и вранья тоже).
    """
    now = now or datetime.now(timezone.utc)
    if ops_dir is None:
        ops_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "..", "..", "ops")
    ops_dir = os.path.normpath(ops_dir)
    try:
        lines = publishing_calendars(ops_dir)
    except OSError:
        return None
    if not lines:
        return None

    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    now_min = (now - midnight).total_seconds() / 60.0
    for day in range(horizon_days):
        weekday = (midnight + timedelta(days=day)).weekday()
        for minute in starts_of_day(lines, weekday=weekday):
            if day == 0 and minute <= now_min:
                continue
            return midnight + timedelta(days=day, minutes=minute)
    return None
