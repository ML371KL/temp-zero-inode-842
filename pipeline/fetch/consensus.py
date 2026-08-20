"""Консенсус аналитиков по ключевой ставке: файл человека, затем зеркала опросов.

ЗАЧЕМ ЭТОТ РЯД. Панель сравнивает решение ЦБ с тем, чего ждали: без консенсуса
сообщение о решении честно говорит «сказать, совпало ли, нечем», а тайл пишет
«консенсус не внесён». До 20.08.2026 ряд заполнялся ТОЛЬКО руками — и не
заполнялся: в проде лежала одна точка-заглушка (16,0 на заседание 24.07, где ЦБ
дал 14,0), а на ближайшее заседание значения не было вовсе.

ПОРЯДОК ИСТОЧНИКОВ. Ручной ввод `inputs/consensus.yml` — всегда главный: человек,
прочитавший опрос, надёжнее любого разбора текста. Автоматика лишь заполняет
пустые заседания.

ПОЧЕМУ ИМЕННО ЗЕРКАЛА ТЕЛЕГРАМА. Проверено живыми запросами 20.08.2026:
  * макроопрос ЦБ (cbr.ru/statistics/ddkp/mo_br) — НЕ ТОТ показатель: там ставка
    «в среднем за год», прогноза на конкретное заседание нет по построению;
  * срочный рынок ставки мёртв: RRQ6/RRU6, MFQ6/MFU6 — ноль сделок и ноль
    открытого интереса за три месяца;
  * РБК отдаёт 401 с прод-машины, Ведомости 502, поиск Интерфакса закрыт robots;
  * а `t.me/s/<канал>?q=` открыт (robots.txt отдаёт 404), листается `&before=`
    и хранит историю годами — тем же приёмом панель уже читает Минфин (fetch/tg).
Восстановление всех пяти заседаний 2026 по зеркалам совпало с историей проекта
(docs/INDICATORS.md): 13.02 → 16,00, 20.03 → 15,00, 24.04 → 14,50, 19.06 → 14,00,
24.07 → 14,25.

ЧЕМ ЭТО ОПАСНО И КАК УДЕРЖАНО. Наивный разбор ловит чужие числа из того же текста:
«вклады до 25% годовых» из рекламы и «13,5%» из описания миноритарного сценария.
Поэтому четыре ограничителя разом — белый список каналов, обязательное слово
«консенсус/опрос», требование БУДУЩЕГО времени рядом со ставкой и окно −14…−1
день до заседания, — плюс главное правило: число попадает в ряд ТОЛЬКО при
согласии двух независимых каналов. Одиночная находка не пишется, а кладётся в
`meta.candidates` подсказкой человеку: робот не имеет права тихо подставить своё
число в сообщение о сюрпризе ЦБ.
"""

import re

try:
    from . import FetchError, make_meta, tg
    from . import manual as manual_mod
    from ..lib import constants, dates, http
except ImportError:                        # запуск из каталога fetch
    from __init__ import FetchError, make_meta                     # noqa: F401
    import manual as manual_mod
    import tg
    from lib import constants, dates, http

SERIES_ID = "cb_consensus"

# Белый список: каналы, которые публикуют СВОДКИ опросов, а не мнения. Чужой
# канал с теми же словами — не источник консенсуса, а пересказ.
CHANNELS = ("cbrstocks", "rbc_news", "if_market_news", "interfaxonline", "prime1")

# Запрос поиска по каналу. Одно слово намеренно: сводки опросов называют
# себя «консенсус-прогноз», а сужение запроса режет выдачу сильнее, чем
# помогает — фильтры ниже всё равно перепроверяют каждое сообщение.
QUERY = "консенсус"

# Окно поиска: опрос выходит за 2–5 дней до заседания. Две недели берём с
# запасом, «за день до» включительно; после заседания искать нечего — там уже
# обсуждают решение, а не ожидания.
LOOKBACK_DAYS = 14

# Слово, без которого сообщение не считается сводкой опроса.
_SURVEY = re.compile(r"консенсус|опрос(?:е|а|ы|ов)?\b|аналитик", re.I)
# Ставка и уровень: «сохранит ключевую ставку на уровне 16%», «снизит до 14,25%».
_RATE = re.compile(
    r"(?:ключев\w+\s+ставк\w+|ставк\w+)[^.]{0,160}?"
    r"(?:сохран\w+|снизит\w*|повыс\w+|оставит|опустит|поднимет|на уровне|до)"
    r"[^.]{0,40}?(\d{1,2}(?:[.,]\d{1,2})?)\s*%", re.I)
_RATE_ALT = re.compile(
    r"(?:сохран\w+|снизит\w*|повыс\w+|оставит|опустит|поднимет)[^.]{0,60}?"
    r"(?:ключев\w+\s+)?ставк\w+[^.]{0,60}?(\d{1,2}(?:[.,]\d{1,2})?)\s*%", re.I)
# Прошедшее время рядом со ставкой — это отчёт о состоявшемся решении.
_PAST = re.compile(r"\b(?:сохранил|снизил|повысил|оставил|принял реш\w+|"
                   r"по итогам заседани\w+)\b", re.I)
# Разумный коридор ключевой ставки: 4,25% (минимум истории) … 25%. Вне него —
# это не ставка, а доходность вклада или процент из соседнего сюжета.
RATE_SANE = (4.0, 25.0)


def _rate_from(text):
    """Ожидаемая ставка из сообщения или None. Отбирает только сводки опросов."""
    if not text or not _SURVEY.search(text):
        return None
    if _PAST.search(text):
        return None                        # рассказ о состоявшемся решении
    for pattern in (_RATE, _RATE_ALT):
        m = pattern.search(text)
        if not m:
            continue
        try:
            value = float(m.group(1).replace(",", "."))
        except ValueError:
            continue
        if RATE_SANE[0] <= value <= RATE_SANE[1]:
            return round(value, 2)
    return None


def survey(meeting, channels=CHANNELS, pages=3, lookback=LOOKBACK_DAYS):
    """Кандидаты в консенсус на заседание -> [{value, channel, at, url}].

    Каждый канал опрашивается отдельно: согласие считается ПО КАНАЛАМ, а два
    сообщения одного канала — это один голос.
    """
    try:
        end = dates.parse_date(meeting)
    except (TypeError, ValueError):
        return []
    start = dates.add_days(end, -abs(lookback))
    found = []
    for channel in channels:
        try:
            # ПОИСК, а не лента: опрос выходит за 2–5 дней до заседания, а лента
            # канала с десятком постов в день уносит его за горизонт за неделю.
            msgs = tg.messages(channel, pages=pages, query=QUERY)
        except FetchError as exc:
            # Канал может быть переименован или закрыт: это не отказ ряда, а минус
            # один голос. Согласие всё равно требует двух — молча кривым значение
            # не станет.
            http.LOG(f"консенсус: канал {channel} не открылся — {exc}")
            continue
        for msg in msgs:
            # tg.parse_page отдаёт `at` объектом datetime (уже в МСК), а не строкой.
            raw_at = msg.get("at")
            at = dates.fmt_date(raw_at.date()) if hasattr(raw_at, "date") else str(raw_at or "")[:10]
            if not at or not (dates.fmt_date(start) <= at < dates.fmt_date(end)):
                continue
            if tg.is_reprint(msg.get("text") or ""):
                continue
            value = _rate_from(msg.get("text") or "")
            if value is None:
                continue
            found.append({"value": value, "channel": channel, "at": at,
                          "url": msg.get("url")})
    return found


def agreed(candidates, min_channels=2):
    """Значение, о котором договорились ≥N РАЗНЫХ каналов, иначе None.

    При нескольких согласованных уровнях берётся тот, у кого больше каналов; при
    равенстве — более свежий. Разнобой без большинства — это не консенсус.
    """
    by_value = {}
    for c in candidates:
        by_value.setdefault(c["value"], set()).add(c["channel"])
    best = None
    for value, chans in by_value.items():
        if len(chans) < min_channels:
            continue
        newest = max(c["at"] for c in candidates if c["value"] == value)
        key = (len(chans), newest)
        if best is None or key > best[0]:
            best = (key, value, sorted(chans))
    return None if best is None else (best[1], best[2])


def _next_meetings(now=None):
    """Заседания, до которых осталось не больше окна поиска (ближайшее первым)."""
    today = dates.fmt_date(dates.today_msk() if now is None else now)
    out = []
    for day in getattr(constants, "CB_MEETINGS_2026", ()):
        if not day or day <= today:
            continue
        if dates.fmt_date(dates.add_days(dates.parse_date(day), -LOOKBACK_DAYS)) <= today:
            out.append(day)
    return sorted(out)


def rate(now=None):
    """-> ("cb_consensus", {дата заседания: ожидаемая ставка}, meta).

    Сначала ручной файл (он же формирует историю), затем — только для заседаний,
    которых в файле нет, — сводки опросов из зеркал.
    """
    sid, points, meta = manual_mod.consensus()
    points = dict(points or {})
    meta = dict(meta or {})
    picked, candidates = [], []
    for meeting in _next_meetings(now):
        if points.get(meeting) is not None:
            continue                       # человек уже вписал — не трогаем
        found = survey(meeting)
        candidates += found
        deal = agreed(found)
        if deal is None:
            continue
        value, chans = deal
        points[meeting] = value
        picked.append({"date": meeting, "value": value, "channels": chans})

    if picked:
        note = "; ".join(f"{p['date']}: {p['value']}% по опросам "
                         f"({', '.join(p['channels'])})" for p in picked)
        meta.update(status="ok", auto=picked,
                    note=(f"{meta.get('note')}; " if meta.get("note") else "")
                         + "консенсус по зеркалам опросов — " + note)
        meta["asof"] = max(points) if points else meta.get("asof")
    elif candidates:
        # Нашли, но согласия нет: это подсказка человеку, а не значение. Молча
        # взять одиночную находку значило бы подставить своё число в сообщение о
        # сюрпризе ЦБ — самое громкое, что панель вообще говорит.
        meta["candidates"] = candidates[:12]
        meta["note"] = ((f"{meta.get('note')}; " if meta.get("note") else "")
                        + f"кандидаты без согласия двух каналов ({len(candidates)}) — "
                          f"вписать вручную в inputs/consensus.yml")
    meta.setdefault("source", "manual+telegram")
    return sid, points, meta
