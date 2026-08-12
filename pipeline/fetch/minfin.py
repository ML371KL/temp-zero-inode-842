"""Минфин/Минэк: налоговая цена Urals, аукционы ОФЗ, НГД, бюджет, ФНБ.

Ряды: `urals_tax` (ядро — нога urals_rub_gap, pub_lag 5 дней, опрос 1–8 числа),
`ofz_auctions`, `ngd`, `budget_deficit`, `fnb` (мониторы).

Что здесь важно понимать про Urals. В ядро идёт НАЛОГОВАЯ (мониторинговая) цена
Минэка — среднее за календарный месяц, публикуемое в начале следующего. Это НЕ
котировка Brent и не спот Urals: именно её использует бюджетное правило и именно
она стоит в валидации (validation/VALIDATION.md §A2, гэп рублёвой бочки к
24-месячному тренду, IC −0,19). Подменять её спотом нельзя — сломается уровень.

Грабли:
1. economy.gov.ru из некоторых сетей не открывается вообще (в отладке 11.08.2026
   с машины пользователя — TLS handshake timeout, три попытки). Поэтому источник
   один, а путей к числу несколько: сначала первоисточник, потом зеркала СМИ.
   Зеркала иногда печатают ДРУГОЕ число за тот же месяц (январь-2026: 40,95 в
   пересказах против 45,0 у Минэка) — при конфликте выигрывает economy.gov.ru,
   расхождение уходит в meta["conflicts"], а не тихо перезаписывается.
2. Страница «Аукционы ОФЗ» на minfin.gov.ru рисуется скриптом: в HTML нет ни
   одной <table> (проверено 11.08.2026). Парсер это признаёт и возвращает
   status="error" с пустым словарём — прогон не валится (CONTRACT.md §0).
3. Пресс-центр Минфина отдаёт дату и текст прямо в HTML — ngd/budget/fnb берём
   оттуда; заголовок ищем по подстроке, а не по id, потому что id меняются.
4. С ПРОД-МАШИНЫ САЙТ МИНФИНА НЕДОСТУПЕН (замер 12.08.2026): minfin.gov.ru отдаёт
   503 на любые заголовки с VPS Hetzner и 200 с ноутбука пользователя — режет WAF по
   диапазонам датацентров, TLS при этом в порядке. Поэтому у пресс-центра появился
   запасной транспорт: телеграм-канал ведомства (`fetch/tg.py`). Он включается
   ТОЛЬКО когда сайт не дал ни одного кандидата, и что число пришло из зеркала,
   видно в `meta.source`/`meta.mirror`.
   Чего зеркало НЕ закрывает (проверено на живых релизах):
     * `fnb` — в телеграме печатают ОБЩИЙ объём фонда, а ряд хранит ЛИКВИДНУЮ часть
       (12 720,8 против 3 692,8 млрд руб.). Шаблоны требуют слова «ликвидн», поэтому
       сообщение не подойдёт и ряд честно останется пустым — подменять величину
       втрое большей нельзя;
     * `urals` — цены Юралс нет ни в канале Минфина, ни у Минэка (проверено на
       месяце сообщений трёх каналов), там остаются только зеркала СМИ.
"""

import re
from datetime import date, datetime, timedelta, timezone

try:                                       # прод: общий HTTP-слой (CONTRACT.md §4)
    from lib.http import get_text, FetchError
except ImportError:
    try:
        from pipeline.lib.http import get_text, FetchError
    except ImportError:                    # автономный запуск (отладка парсеров)
        class FetchError(Exception):
            pass

        def get_text(url, timeout=45, headers=None, **_kw):
            import gzip
            import urllib.request
            req = urllib.request.Request(url, headers=headers or _UA)
            try:
                resp = urllib.request.urlopen(req, timeout=timeout)
                raw = resp.read()
            except OSError as exc:
                raise FetchError("%s: %s" % (url, exc))
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return raw.decode("utf-8", "replace")

try:                                       # запасной транспорт (грабля 4)
    from . import tg
except ImportError:                        # автономный запуск из каталога fetch
    try:
        import tg
    except ImportError:
        tg = None

# Грабля 11.08.2026: minfin.gov.ru отдаёт 503 на дефолтный UA пайплайна
# («moex-radar/1.0 … python-urllib») и спокойно отвечает браузерному. Поэтому
# заголовки передаём явно в каждый запрос — молча получать 503 хуже, чем врать
# про браузер.
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
       "Accept-Language": "ru,en;q=0.8"}

PRESS_URL = "https://minfin.gov.ru/ru/press-center/"
MINFIN_BASE = "https://minfin.gov.ru"
# Запасной транспорт: канал ведомства в телеграме (см. грабля 4 в шапке модуля).
TG_CHANNEL = "minfin"
TG_PAGES = 4          # ~80 сообщений ≈ месяц ленты: месячный релиз укладывается
AUCTION_URL = ("https://minfin.gov.ru/ru/perfomance/public_debt/internal/"
               "operations/ofz/auction/")

# Первоисточник цены Urals — Минэк; остальное зеркала (порядок = приоритет).
# Четвёртое поле — таймаут в секундах, пятое — число попыток: economy.gov.ru из
# части сетей просто ВИСИТ на TLS-рукопожатии, и дефолтные 3 попытки × 30 с
# съедают три минуты прогона на источнике, который сегодня недоступен.
# 12.08.2026 таймаут урезан с 12 с до 6: хост не отвечает НИ С ОДНОЙ из двух машин
# (с VPS не резолвится вовсе, с ноутбука виснет на рукопожатии), а замер прогона
# показал 26,5 с на ряд — почти всё это ожидание двух мёртвых адресов. Опрос идёт
# трижды в сутки, и полторы минуты в день уходили в никуда.
URALS_SOURCES = [
    ("economy.gov.ru", "https://www.economy.gov.ru/material/press/", 6, 1),
    ("economy.gov.ru", "https://www.economy.gov.ru/material/news/", 6, 1),
    ("1prime.ru", "https://1prime.ru/oil/", 30, 2),
    ("investfuture.ru", "https://investfuture.ru/news", 30, 2),
    ("mfd.ru", "https://mfd.ru/news/", 30, 2),
]

# Реперы 2026 из задания: только самопроверка, в данные не подставляются.
URALS_SELF_CHECK = {"2026-01": 45.0, "2026-04": 94.87, "2026-06": 63.52,
                    "2026-07": 59.02}

_MONTHS = {"январ": 1, "феврал": 2, "март": 3, "апрел": 4, "мая": 5, "мае": 5,
           "май": 5, "июн": 6, "июл": 7, "август": 8, "сентябр": 9, "октябр": 10,
           "ноябр": 11, "декабр": 12}


# ------------------------------------------------------------------ утилиты

def _plain(html):
    """HTML -> плоский текст. html.parser здесь избыточен: нужен только текст."""
    body = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S)
    body = re.sub(r"<[^>]+>", " ", body)
    body = (body.replace("&nbsp;", " ").replace("&#160;", " ")
                .replace("&laquo;", "«").replace("&raquo;", "»")
                .replace("&ndash;", "–").replace("&mdash;", "—")
                .replace("&amp;", "&").replace("&quot;", '"'))
    return re.sub(r"[\s ]+", " ", body).strip()


def _num(raw):
    """'136,17' / '4 897' -> float. Пробелы внутри числа — разделитель тысяч."""
    txt = re.sub(r"[\s  ]", "", raw).rstrip(".,").replace(",", ".")
    if txt.count(".") > 1:
        return None
    try:
        return float(txt)
    except ValueError:
        return None


def _month_end(year, month):
    nxt = (year + 1, 1) if month == 12 else (year, month + 1)
    return (date(nxt[0], nxt[1], 1) - timedelta(days=1)).isoformat()


def _month_from_text(text):
    """'в июле 2026 года' -> (2026, 7). Берём ПЕРВОЕ совпадение месяц+год."""
    for m in re.finditer(r"([а-яё]{3,9})\s+(20\d\d)", text.lower()):
        for stem, num in _MONTHS.items():
            if m.group(1).startswith(stem):
                return int(m.group(2)), num
    return None


def _meta(source, url, status, note=None, extra=None):
    meta = {"source": source, "url": url, "status": status, "note": note,
            "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    if extra:
        meta.update(extra)
    return meta


# ------------------------------------------------------------- пресс-центр

def press_items():
    """[(url, заголовок)] со страницы пресс-центра Минфина. Пусто — если не открылась."""
    try:
        html = get_text(PRESS_URL, headers=_UA)
    except FetchError:
        return []
    out, seen = [], set()
    for href, inner in re.findall(
            r'<a[^>]+href="(/ru/press-center/\?id_4=[^"]+)"[^>]*>(.{0,300}?)</a>',
            html, re.S):
        title = re.sub(r"<[^>]+>|\s+", " ", inner).strip()
        if not title or href in seen:
            continue
        seen.add(href)
        out.append((MINFIN_BASE + href.replace("&amp;", "&"), title))
    return out


def _press_article(url):
    """Текст новости + её дата публикации (по шапке 'DD месяца YYYY HH:MM')."""
    text = _plain(get_text(url, headers=_UA))
    pub = None
    m = re.search(r"(\d{1,2})\s+([а-яё]{3,9})\s+(20\d\d)", text.lower())
    if m:
        for stem, num in _MONTHS.items():
            if m.group(2).startswith(stem):
                pub = date(int(m.group(3)), num, int(m.group(1))).isoformat()
                break
    return text, pub


def _candidates(keywords):
    """(кандидаты, заметки): новости, чей ЗАГОЛОВОК содержит все ключевые слова.

    Порядок жёсткий: сначала пресс-центр, и только если он не дал НИ ОДНОГО
    кандидата — телеграм-зеркало. Правило проекта «первоисточник не перетирается
    зеркалом» (см. urals) здесь означает: пока сайт открывается, зеркало не
    смотрим вовсе, даже если оно свежее.

    text=None означает «текст ещё не скачан» — статью пресс-центра открываем
    только когда до неё дошла очередь: у Минфина по десятку похожих заголовков.
    """
    items = press_items()
    out = [{"src": "minfin", "url": url, "title": title, "text": None, "pub": None}
           for url, title in items if all(kw in title.lower() for kw in keywords)]
    if out:
        return out, []
    notes = ["пресс-центр не открылся" if not items
             else "в пресс-центре нет новости по ключам %s" % (keywords,)]
    if tg is None:
        return out, notes
    try:
        for msg in tg.find(TG_CHANNEL, keywords, pages=TG_PAGES):
            # Текст сообщения разложен по строкам, а шаблоны писались под сплошной
            # текст статьи: «[^.]{0,120}» не должен спотыкаться о перевод строки.
            out.append({"src": "minfin_tg", "url": msg["url"], "title": msg["head"],
                        "text": msg["text"].replace("\n", " "), "pub": msg["published"]})
    except FetchError as exc:
        notes.append("telegram-зеркало: %s" % exc)
    return out, notes


def _press_number(keywords, series_id, patterns, sign_words=None, month_hint=None,
                  derive=None):
    """Общий каркас ngd/budget/fnb: найти новость по заголовку и число в тексте.

    patterns — список регулярок с группой 1 = число (в млрд руб). Необязательная
    именованная группа `unit` («млрд» или «млн») говорит, в чём это число написано:
    Минфин печатает бюджет в млрд, а ФНБ — в млн, и без нормировки в ряд уехало бы
    число в тысячу раз больше. month_hint(текст до числа) -> (год, месяц) — для
    релизов, где месяц значения не совпадает с месяцем публикации.
    derive(текст) -> (значение, позиция, пояснение) — последняя попытка, когда сам
    показатель в тексте не назван, но однозначно считается из соседних (бюджет).
    """
    items, misses = _candidates(keywords)
    if not items:
        return series_id, {}, _meta("minfin", PRESS_URL, "error", "; ".join(misses))
    for item in items:
        text, pub = item["text"], item["pub"]
        if text is None:
            try:
                text, pub = _press_article(item["url"])
            except FetchError as exc:
                misses.append("%s: %s" % (item["title"], exc))
                continue
        hit = _match_number(text, patterns, sign_words)
        if hit is None and derive is not None:
            hit = derive(text)
        if hit is None:
            # заголовок подошёл, а числа нет — идём к следующей новости: у Минфина
            # много похожих заголовков («…о результатах размещения средств ФНБ»)
            misses.append("число не распозналось: %s" % item["title"])
            continue
        value, at, how = hit
        when = (month_hint(text[:at]) if month_hint else None) or \
            _month_from_text(text[:at] or text) or (
                (int(pub[:4]), int(pub[5:7])) if pub else None)
        if when is None:
            misses.append("не определился месяц: %s" % item["title"])
            continue
        key = _month_end(when[0], when[1])
        mirror = item["src"] != "minfin"
        note = item["title"] if not how else "%s (%s)" % (item["title"], how)
        if mirror:
            note = "ЗЕРКАЛО t.me/%s: %s" % (TG_CHANNEL, note)
        return series_id, {key: value}, _meta(
            item["src"], item["url"], "ok", note,
            {"published": pub, "asof": key, "mirror": mirror,
             "quote": text[max(0, at - 120):at + 120],
             "site_failed": misses or None})
    return series_id, {}, _meta("minfin", PRESS_URL, "error",
                                "; ".join(misses) or
                                "в пресс-центре нет новости по ключам %s" % (keywords,))


def _match_number(text, patterns, sign_words=None):
    """(значение, позиция в тексте, пояснение|None) по первому сработавшему шаблону."""
    for pattern in patterns:
        m = re.search(pattern, text, re.I)
        if not m:
            continue
        value = _num(m.group(1))
        if value is None:
            continue
        if (m.groupdict().get("unit") or "").lower().startswith("млн"):
            value = round(value / 1000.0, 4)   # ряды храним в млрд руб.
        if sign_words:
            window = text[max(0, m.start() - 160):m.end() + 40].lower()
            # знак определяет слово (покупку/продажу, дефицит/профицит),
            # а не тире перед числом — оно у Минфина разделитель
            if any(w in window for w in sign_words):
                value = -value
        return value, m.start(), None
    return None


# ------------------------------------------------------------------- Urals

_PRICE_RE = re.compile(r"(\d{1,3}(?:[.,]\d{1,2})?)\s*(?:долл|дол\.|usd|\$)")
_MONTH_YEAR_RE = re.compile(r"([а-яё]{3,9})\s+(20\d\d)")


def parse_urals_text(text):
    """Плоский текст -> {последний день месяца: цена, $/барр}.

    С каждого упоминания Юралс берём РОВНО ОДНУ пару «месяц → цена» и требуем,
    чтобы между ними не было другого месяца. Обе оговорки не теоретические:
    (1) на ленте новостей сообщения стоят вплотную, и широкое окно утаскивает
    число из чужого месяца; (2) в самом релизе всегда есть сравнение с прошлым
    годом («в июле 2026 … 59,02 долл., в июле 2025 – 65,32 долл.»), и без
    требования «месяц и цена рядом» в историю уезжает пара из хвоста фразы.
    """
    low = text.lower()
    mentions = [m.start() for m in re.finditer(r"юралс|urals", low)]
    out = {}
    for idx, at in enumerate(mentions):
        stop = mentions[idx + 1] if idx + 1 < len(mentions) else len(low)
        window = low[max(0, at - 120):min(stop, at + 340)]
        pair = _month_price_pair(low[at:min(stop, at + 340)]) or _month_price_pair(window)
        if pair:
            out.setdefault(_month_end(pair[0], pair[1]), pair[2])
    return out


def _month_price_pair(window):
    """(год, месяц, цена) из фрагмента, если месяц и цена стоят рядом."""
    for m in _MONTH_YEAR_RE.finditer(window):
        month = None
        for stem, num in _MONTHS.items():
            if m.group(1).startswith(stem):
                month = num
                break
        if month is None:
            continue
        tail = window[m.end():]
        price_match = _PRICE_RE.search(tail)
        if not price_match:
            continue
        other = _MONTH_YEAR_RE.search(tail[:price_match.start()])
        if other:                       # между месяцем и ценой влез другой месяц
            continue
        price = _num(price_match.group(1))
        if price is not None and 5.0 < price < 250.0:
            return int(m.group(2)), month, price
    return None


def urals():
    """-> ("urals_tax", {последний день месяца: цена}, meta).

    Сначала economy.gov.ru, затем зеркала. Значение с более приоритетного
    источника НЕ перезаписывается зеркалом; расхождения складываются в
    meta["conflicts"] — их надо смотреть глазами, а не усреднять.
    """
    points, conflicts, tried, ok_sources = {}, [], [], []
    for name, url, timeout, retries in URALS_SOURCES:
        try:
            html = get_text(url, headers=_UA, timeout=timeout, retries=retries)
        except FetchError as exc:
            tried.append("%s: %s" % (name, exc))
            continue
        found = parse_urals_text(_plain(html))
        if not found:
            tried.append("%s: страница открылась, цены не найдено" % name)
            continue
        ok_sources.append(name)
        for day, price in found.items():
            if day not in points:
                points[day] = price
            elif abs(points[day] - price) > 0.01:
                conflicts.append("%s: %s против %.2f у %s"
                                 % (day, price, points[day], ok_sources[0]))
    if not points:
        return "urals_tax", {}, _meta("minfin_urals", URALS_SOURCES[0][1], "error",
                                      "; ".join(tried) or "источники не ответили")
    return "urals_tax", points, _meta(
        "minfin_urals", URALS_SOURCES[0][1], "ok",
        "источники: %s" % ", ".join(ok_sources),
        {"conflicts": conflicts, "failed": tried, "selfcheck": _urals_selfcheck(points)})


def _urals_selfcheck(points):
    """Сверка с реперами задания: расхождение — сигнал, что изменилась вёрстка."""
    bad = []
    for period, ref in URALS_SELF_CHECK.items():
        key = _month_end(int(period[:4]), int(period[5:7]))
        if key in points and abs(points[key] - ref) > 0.05:
            bad.append("%s: %.2f против репера %.2f" % (period, points[key], ref))
    return "; ".join(bad) if bad else "ok"


# ------------------------------------------------------------ аукционы ОФЗ

_AUCTION_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_AUCTION_CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S)


def parse_auctions(html):
    """HTML таблицы -> {дата: {placed_bln, demand_bln, failed, issue}}.

    Столбцы Минфина плавают, поэтому опираемся на дату в первой ячейке и на два
    самых больших числа строки (спрос ≥ размещения). Признак несостоявшегося —
    слово «признан несостоявшимся» или отсутствие объёма размещения.
    """
    out = {}
    for row_html in _AUCTION_ROW.finditer(html):
        cells = [_plain(c) for c in _AUCTION_CELL.findall(row_html.group(1))]
        if len(cells) < 3:
            continue
        m = re.search(r"(\d{2})\.(\d{2})\.(20\d\d)", " ".join(cells[:2]))
        if not m:
            continue
        day = "%s-%s-%s" % (m.group(3), m.group(2), m.group(1))
        joined = " ".join(cells).lower()
        failed = "несостоя" in joined
        numbers = []
        for cell in cells[1:]:
            value = _num(cell)
            if value is not None and value > 0:
                numbers.append(value)
        issue = ""
        m_issue = re.search(r"(su\d{5}\w*|офз[-\s]?[а-я]{2,3}\s*№?\s*\d{5}\w*)", joined)
        if m_issue:
            issue = m_issue.group(1).upper()
        numbers.sort(reverse=True)
        out[day] = {"demand_bln": numbers[0] if numbers else None,
                    "placed_bln": numbers[1] if len(numbers) > 1 else None,
                    "failed": failed, "issue": issue}
    return out


def auctions():
    """-> ("ofz_auctions", {дата: {…}}, meta). Значение точки — СЛОВАРЬ."""
    try:
        html = get_text(AUCTION_URL, headers=_UA)
    except FetchError as exc:
        return "ofz_auctions", {}, _meta("minfin", AUCTION_URL, "error", str(exc))
    points = parse_auctions(html)
    if not points:
        return "ofz_auctions", {}, _meta(
            "minfin", AUCTION_URL, "error",
            "страница аукционов не разобралась: таблиц в HTML нет (данные "
            "подгружаются скриптом). Структура спроса по категориям при этом "
            "есть в ОРФР ЦБ — см. docs/SOURCES.md",
            {"value_keys": ["demand_bln", "placed_bln", "failed", "issue"]})
    # В стор кладём ЧИСЛО (размещённый объём) — контракт §1 не знает словарей;
    # подробности последнего аукциона едут в meta["last"], откуда их и берёт тайл
    # (monitors._t_ofz_auctions). Так же поступают КБД и открытые позиции.
    last_day = max(points)
    last = points[last_day]
    numeric = {d: (row.get("placed_bln") if isinstance(row, dict) else row)
               for d, row in points.items()}
    numeric = {d: v for d, v in numeric.items() if v is not None}
    return "ofz_auctions", numeric, _meta(
        "minfin", AUCTION_URL, "ok", "аукционных дней: %d" % len(points),
        {"last": {"date": last_day, "placed_bn": last.get("placed_bln"),
                  "demand_bn": last.get("demand_bln"), "failed": last.get("failed"),
                  "issue": last.get("issue")}})


# ------------------------------------------------- месячные показатели Минфина

def ngd():
    """Нефтегазовые доходы: объём операций по бюджетному правилу, млрд руб.

    Знак: «+» — покупка валюты/золота (ЦБ зеркалит на рынке продажей валюты),
    «−» — продажа из ФНБ. Ключ точки — месяц ПУБЛИКАЦИИ (операции идут с ~7-го
    числа этого месяца по ~4-е следующего), как и в registry (pub_lag_days=5).
    """
    return _press_number(
        ("нефтегазов",), "ngd",
        [r"направляемых на покупку[^.]{0,120}?составит\s*([\d\s.,]+)\s*млрд",
         r"направляемых на продажу[^.]{0,120}?составит\s*([\d\s.,]+)\s*млрд",
         r"совокупный объем средств[^.]{0,160}?составит\s*([\d\s.,]+)\s*млрд"],
        sign_words=("на продажу", "продаже иностранной валюты"))


# Итог «доходы минус расходы» — только по СВОДНЫМ строкам релиза. В том же тексте
# рядом стоят «ненефтегазовые доходы 17 518» и «нефтегазовые доходы 4 595 млрд», и
# широкий шаблон вида «доходы … млрд» взял бы слагаемое вместо суммы.
_BUDGET_INCOME = r"объ[её]м доходов[^.]{0,160}?составил\D{0,25}?([\d\s.,]+)\s*млрд"
_BUDGET_SPEND = r"объ[её]м расходов[^.]{0,160}?составил\D{0,25}?([\d\s.,]+)\s*млрд"


def _budget_from_parts(text):
    """(дефицит, позиция, пояснение) из доходов и расходов, если итог не назван.

    Нужно из-за телеграм-версии релиза: на сайте Минфин пишет «бюджет сложился с
    дефицитом в размере 6 455 млрд рублей», а в канале печатает только доходы
    (22 112) и расходы (28 567) — сам дефицит не называет. Это тождество, а не
    оценка: 22 112 − 28 567 = −6 455, ровно то число, что стоит в релизе на сайте.
    Тем не менее способ получения уходит в `meta.note`: подставлять посчитанное
    молча — значит однажды не заметить, что Минфин сменил разбивку.
    """
    inc = re.search(_BUDGET_INCOME, text, re.I)
    exp = re.search(_BUDGET_SPEND, text, re.I)
    if not inc or not exp:
        return None
    income, spend = _num(inc.group(1)), _num(exp.group(1))
    if income is None or spend is None or income <= 0 or spend <= 0:
        return None
    return (round(income - spend, 4), inc.start(),
            "посчитано как доходы %.0f − расходы %.0f" % (income, spend))


def budget():
    """Исполнение федерального бюджета: дефицит (−) / профицит (+), млрд руб.

    Минфин пишет итог двумя способами, и «составил» — не самый частый: в релизах
    за июнь и июль 2026 стоит «федеральный бюджет сложился с дефицитом в размере
    6 455 млрд рублей», из-за чего ряд не собирался НИ РАЗУ. Старые шаблоны
    оставлены первыми — вдруг формулировка вернётся.
    """
    return _press_number(
        ("предварительн", "исполнени"), "budget_deficit",
        [r"дефицит[^.]{0,160}?составил[^.]{0,60}?([\d\s.,]+)\s*млрд",
         r"профицит[^.]{0,160}?составил[^.]{0,60}?([\d\s.,]+)\s*млрд",
         r"сложился с (?:дефицитом|профицитом)[^.]{0,60}?([\d\s.,]+)\s*млрд",
         r"(?:дефицит|профицит)[^.]{0,60}?в размере\s*([\d\s.,]+)\s*млрд"],
        sign_words=("дефицит",), derive=_budget_from_parts)


def _fnb_month(text_before):
    """«по состоянию на 1 августа 2026 г.» -> (2026, 7).

    Минфин публикует остаток НА ПЕРВОЕ число месяца — это остаток на конец
    предыдущего. Без поправки августовский релиз лёг бы точкой 31 августа, то есть
    датой из будущего, а ряд бы выглядел на месяц свежее, чем он есть.
    """
    last = None
    for last in re.finditer(r"по состоянию на\s+(\d{1,2})\s+([а-яё]{3,9})\s+(20\d\d)",
                            text_before.lower()):
        pass
    if last is None:
        return None
    month = next((num for stem, num in _MONTHS.items() if last.group(2).startswith(stem)), None)
    if month is None:
        return None
    year, day = int(last.group(3)), int(last.group(1))
    if day <= 3:
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    return year, month


def fnb():
    """Ликвидная часть ФНБ, млрд руб (то, чем реально можно закрывать дефицит).

    Три грабли живого релиза, из-за которых ряд не собрался ни разу:
    1. заголовок — «о результатах размещения средств ФондА национального
       благосостояния», а ключ искал «фондЕ»; берём корень без окончания;
    2. число напечатано в МИЛЛИОНАХ («составил эквивалент 3 692 785,4 млн рублей»),
       а шаблоны требовали «млрд» — теперь единица читается группой `unit`;
    3. рядом в той же фразе стоит сумма в ДОЛЛАРАХ («46 242,3 млн долл. США»),
       поэтому требуем «руб» после единицы: иначе при другом порядке слов в ряд
       тихо ляжет 46 вместо 3693.
    """
    return _press_number(
        ("национального благосостояния",), "fnb",
        [r"объем ликвидных активов[^.]{0,200}?([\d\s.,]+)\s*(?P<unit>млрд|млн)\s*руб",
         r"ликвидн[^.]{0,200}?составил[^.]{0,80}?([\d\s.,]+)\s*(?P<unit>млрд|млн)\s*руб"],
        month_hint=_fnb_month)
