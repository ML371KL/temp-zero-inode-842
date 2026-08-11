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

# Грабля 11.08.2026: minfin.gov.ru отдаёт 503 на дефолтный UA пайплайна
# («moex-radar/1.0 … python-urllib») и спокойно отвечает браузерному. Поэтому
# заголовки передаём явно в каждый запрос — молча получать 503 хуже, чем врать
# про браузер.
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
       "Accept-Language": "ru,en;q=0.8"}

PRESS_URL = "https://minfin.gov.ru/ru/press-center/"
MINFIN_BASE = "https://minfin.gov.ru"
AUCTION_URL = ("https://minfin.gov.ru/ru/perfomance/public_debt/internal/"
               "operations/ofz/auction/")

# Первоисточник цены Urals — Минэк; остальное зеркала (порядок = приоритет).
# Четвёртое поле — таймаут в секундах, пятое — число попыток: economy.gov.ru из
# части сетей просто ВИСИТ на TLS-рукопожатии, и дефолтные 3 попытки × 30 с
# съедают три минуты прогона на источнике, который сегодня недоступен.
URALS_SOURCES = [
    ("economy.gov.ru", "https://www.economy.gov.ru/material/press/", 12, 1),
    ("economy.gov.ru", "https://www.economy.gov.ru/material/news/", 12, 1),
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


def _press_number(keywords, series_id, patterns, sign_words=None):
    """Общий каркас ngd/budget/fnb: найти новость по заголовку и число в тексте.

    patterns — список регулярок с группой 1 = число (в млрд руб). Возвращает
    контракт fetch-модуля.
    """
    items = press_items()
    if not items:
        return series_id, {}, _meta("minfin", PRESS_URL, "error",
                                    "пресс-центр не открылся")
    misses = []
    for url, title in items:
        low = title.lower()
        if not all(kw in low for kw in keywords):
            continue
        try:
            text, pub = _press_article(url)
        except FetchError as exc:
            misses.append("%s: %s" % (title, exc))
            continue
        for pattern in patterns:
            m = re.search(pattern, text, re.I)
            if not m:
                continue
            value = _num(m.group(1))
            if value is None:
                continue
            if sign_words:
                window = text[max(0, m.start() - 160):m.end() + 40].lower()
                # знак определяет слово (покупку/продажу, дефицит/профицит),
                # а не тире перед числом — оно у Минфина разделитель
                if any(w in window for w in sign_words):
                    value = -value
            when = _month_from_text(text[:m.start()] or text) or (
                (int(pub[:4]), int(pub[5:7])) if pub else None)
            if when is None:
                continue
            key = _month_end(when[0], when[1])
            return series_id, {key: value}, _meta(
                "minfin", url, "ok", title,
                {"published": pub, "asof": key,
                 "quote": text[max(0, m.start() - 120):m.end() + 60]})
        # заголовок подошёл, а числа нет — идём к следующей новости: у Минфина
        # много похожих заголовков («…о результатах размещения средств ФНБ»)
        misses.append("число не распозналось: %s" % title)
    return series_id, {}, _meta("minfin", PRESS_URL, "error",
                                "; ".join(misses) or
                                "в пресс-центре нет новости по ключам %s" % (keywords,))


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


def budget():
    """Исполнение федерального бюджета: дефицит (−) / профицит (+), млрд руб."""
    return _press_number(
        ("предварительн", "исполнени"), "budget_deficit",
        [r"дефицит[^.]{0,160}?составил[^.]{0,60}?([\d\s.,]+)\s*млрд",
         r"профицит[^.]{0,160}?составил[^.]{0,60}?([\d\s.,]+)\s*млрд"],
        sign_words=("дефицит",))


def fnb():
    """Ликвидная часть ФНБ, млрд руб (то, чем реально можно закрывать дефицит)."""
    return _press_number(
        ("фонде национального благосостояния",), "fnb",
        [r"ликвидн[^.]{0,200}?составил[^.]{0,80}?([\d\s.,]+)\s*млрд",
         r"объем ликвидных активов[^.]{0,200}?([\d\s.,]+)\s*млрд"])
