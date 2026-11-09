"""Росстат: недельный и месячный ИПЦ.

Ряды: `cpi_weekly` (pub_lag 2 дня), `cpi_monthly` (pub_lag 13 дней, опрос 9–18).
Значения — в ПРОЦЕНТАХ прироста (0.62 = +0,62%), а не в индексах: Росстат
печатает 100,62%, мы храним 0.62.

Зачем ряд, если валидация его похоронила. Для акций недельные принты ИПЦ —
подтверждённый ноль (VALIDATION.md §5.3: пост-публикационные CAR неотличимы от
нуля, знаменитый «пре-дрейф» оказался артефактом четырёх мартовских публикаций
2022 года, отображённых на один день реопена). Ряд ведём как вход в ОЖИДАНИЯ
СТАВКИ (тайл-контекст перед заседанием), tier «dead» в constants.MONITOR_TIERS
это прямо фиксирует. Не превращать в сигнал.

Грабли:
1. Зеркало inflation-monitor.ru печатает В ПЕРВОЙ КОЛОНКЕ прогноз «Нейронной
   экономики», а не Росстат. Брать надо колонку `col-prod-week-rosstat` —
   иначе в ряд попадёт чужая модель, похожая на данные.
2. Файл Росстата nedel_Ipc.xlsx — ПОТОВАРНЫЕ индексы, сводного ИПЦ там нет:
   заголовочное число живёт только в тексте релиза «Об оценке ИПЦ».
3. Месячный файл называется ipc_mes_MM-YYYY.xlsx — имя меняется каждый месяц,
   поэтому ссылку каждый раз ищем на странице раздела, а не хардкодим.
4. xlsx читаем сами (zipfile+ElementTree): это стандартная библиотека, а openpyxl
   — внешняя зависимость, запрещённая CONTRACT.md §0.
5. С НОЯБРЯ 2026 xlsx РОСТАТА НЕ КАЧАЮТСЯ ВОВСЕ (замер 09.11.2026): все файлы
   `/storage/mediabank/*.xlsx` отдают 404 — и с VPS, и с ноутбука, и не только
   ИПЦ, — а PDF и картинки из того же каталога отдаются. Страница «Цены» застыла
   на 13.03.2026 и ссылается на `ipc_mes_02-2026.xlsx`. Свежие релизы Росстат
   теперь вешает в ленту новостей ФАЙЛОМ PDF («Об индексе потребительских цен в
   октябре 2026 года»), страница `/central-news/document/N` лишь перенаправляет
   на него. Поэтому месячный ИПЦ берётся из релиза, а xlsx остаётся запасным
   путём на случай, если Росстат его вернёт.
"""

import io
import re
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from html import unescape as _unescape

try:                                       # текст PDF: тот же извлекатель, что у ОРФР
    from .orfr import pdf_text as _pdf_text
except ImportError:
    try:
        from orfr import pdf_text as _pdf_text
    except ImportError:                    # автономная отладка без пакета
        _pdf_text = None

try:                                       # чтение xlsx по адресам ячеек (lib/xlsx)
    from lib import xlsx as _xlsx
except ImportError:
    try:
        from pipeline.lib import xlsx as _xlsx
    except ImportError:                    # автономный запуск без пакета
        _xlsx = None

try:                                       # прод: общий HTTP-слой (CONTRACT.md §4)
    from lib.http import get_text, get_bytes, FetchError
except ImportError:
    try:
        from pipeline.lib.http import get_text, get_bytes, FetchError
    except ImportError:                    # автономный запуск (отладка парсеров)
        class FetchError(Exception):
            pass

        def get_bytes(url, timeout=60, headers=None, **_kw):
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
            return raw

        def get_text(url, encoding="utf-8", **kw):
            return get_bytes(url, **kw).decode(encoding, "replace")

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
       "Accept-Language": "ru,en;q=0.8"}

ROSSTAT_MAIN = "https://rosstat.gov.ru/"
ROSSTAT_PRICE = "https://rosstat.gov.ru/statistics/price"
ROSSTAT_NEWS = "https://rosstat.gov.ru/central-news"
MIRROR_WEEKLY = "https://inflation-monitor.ru/weekly_inflation/"
# Где искать месячный релиз (порядок = приоритет): лента новостей держит ссылки
# на PDF недели две, главная — только последние публикации.
RELEASE_PAGES = (ROSSTAT_NEWS, ROSSTAT_MAIN)
# Месячный ИПЦ Росстат публикует 10–16 числа (16.01, 13.02, 13.03 в 2026). Если к
# 17-му релиза за прошлый месяц в ленте нет, это уже не «ещё не вышел», а поломка
# (сменился заголовок, ссылка, формат) — и молчать до нормы возраста нельзя.
RELEASE_DUE_DAY = 17
MSK = timezone(timedelta(hours=3))

_MONTHS = {"январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
           "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11,
           "декабр": 12}
_MONTH_ORDER = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль",
                "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]


def _plain(html):
    body = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S)
    body = re.sub(r"<[^>]+>", " ", body)
    body = (body.replace("&nbsp;", " ").replace("&#160;", " ")
                .replace("&ndash;", "–").replace("&mdash;", "—").replace("&amp;", "&"))
    return re.sub(r"[\s ]+", " ", body).strip()


def _num(raw):
    txt = re.sub(r"[\s  ]", "", str(raw)).rstrip(".,").replace(",", ".")
    try:
        return float(txt)
    except ValueError:
        return None


def _stored_last(series_id):
    """Последняя дата уже собранного ряда или None (стор фетчерам читать можно).

    Импорт ленивый и по обеим схемам путей — как и у get_text выше: модуль зовут
    и как pipeline.fetch.rosstat, и напрямую при отладке парсеров.
    """
    try:
        from lib.store import last_date
    except ImportError:
        try:
            from pipeline.lib.store import last_date
        except ImportError:
            return None
    try:
        return last_date(series_id)
    except (OSError, ValueError):
        return None


def _meta(source, url, status, note=None, extra=None):
    meta = {"source": source, "url": url, "status": status, "note": note,
            "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    if extra:
        meta.update(extra)
    return meta


def _month_num(word):
    word = word.lower()
    for stem, num in _MONTHS.items():
        if word.startswith(stem):
            return num
    return None


def _month_end(year, month):
    nxt = (year + 1, 1) if month == 12 else (year, month + 1)
    return (date(nxt[0], nxt[1], 1) - timedelta(days=1)).isoformat()


# ------------------------------------------------------- недельный ИПЦ

_WEEK_TITLE = re.compile(
    r'href="([^"]*/document/\d+)"[^>]*>(?:\s|<[^>]+>)*(?:<b>[^<]*</b>)?\s*'
    r'([^<]{0,140}?оценке индекса потребительских цен[^<]{0,80})', re.I)


def _week_end_from_title(title):
    """'…с 28 июля по 3 августа 2026 года' -> '2026-08-03'.

    Год стоит один раз, в конце: если период переходит через новый год
    («с 30 декабря по 5 января 2026»), год начала — предыдущий, но нам нужен
    только КОНЕЦ недели, поэтому берём указанный год как есть.
    """
    m = re.search(r"по\s+(\d{1,2})\s+([а-яё]+)\s+(20\d\d)", title.lower())
    if not m:
        return None
    month = _month_num(m.group(2))
    if not month:
        return None
    try:
        return date(int(m.group(3)), month, int(m.group(1))).isoformat()
    except ValueError:
        return None


def _parse_release(text):
    """Текст релиза -> прирост за неделю, %. «составил 99,98%» -> -0.02."""
    m = re.search(r"индекс потребительских цен[^.]{0,120}?составил\D{0,20}?"
                  r"(\d{2,3}[.,]\d{1,2})\s*%", text, re.I)
    if not m:
        m = re.search(r"составил\D{0,20}?(\d{2,3}[.,]\d{1,2})\s*%", text)
    if not m:
        return None
    value = _num(m.group(1))
    return None if value is None else round(value - 100.0, 4)


# Публичное имя разбора недельного релиза. Тест недельного ИПЦ искал parse_weekly /
# cpi_weekly_from_html / parse и МОЛЧА пропускался — единственный ряд с фолбэком на
# зеркало оставался без единой проверки. Имя _parse_release не трогаем: оно в вызовах.
parse_weekly = _parse_release


def cpi_weekly(limit=4):
    """-> ("cpi_weekly", {дата конца недели: прирост, %}, meta).

    Первичный источник — релизы Росстата (ссылки на главной), фолбэк — зеркало.
    limit ограничивает число дозагружаемых релизов: для дневного прогона хватает
    последнего, историю добирает bootstrap.
    """
    points, notes = {}, []
    try:
        html = get_text(ROSSTAT_MAIN, headers=_UA)
    except FetchError as exc:
        notes.append("главная Росстата: %s" % exc)
        html = ""
    seen = set()
    for href, title in _WEEK_TITLE.findall(html):
        # /announcements/ — АНОНС будущей публикации («12 августа: Об оценке ИПЦ
        # с 4 по 10 августа»), чисел в нём ещё нет; открывать его — потерять слот
        if "/announcements/" in href:
            continue
        if href in seen or len(seen) >= limit:
            continue
        seen.add(href)
        day = _week_end_from_title(title)
        if not day:
            continue
        url = href if href.startswith("http") else "https://rosstat.gov.ru" + href
        try:
            value = _parse_release(_plain(get_text(url, headers=_UA)))
        except FetchError as exc:
            notes.append("%s: %s" % (url, exc))
            continue
        if value is not None:
            points[day] = value
    if points and len(points) >= limit:
        return "cpi_weekly", points, _meta(
            "rosstat", ROSSTAT_MAIN, "ok", "релизов разобрано: %d" % len(points),
            {"failed": notes})
    mirror_points, mirror_note = _mirror_weekly(limit)
    if points:
        # На главной Росстата висит только последний релиз, поэтому историю
        # добираем зеркалом — но ТОЛЬКО в пустые даты: значение первоисточника
        # зеркалом не перетирается никогда.
        added = [d for d in mirror_points if d not in points]
        for day in added:
            points[day] = mirror_points[day]
        return "cpi_weekly", points, _meta(
            "rosstat", ROSSTAT_MAIN, "ok",
            "релизов Росстата: %d, добрано зеркалом: %d" % (len(points) - len(added),
                                                            len(added)),
            {"failed": notes, "mirror_filled": sorted(added),
             "note_mirror": mirror_note})
    if mirror_points:
        return "cpi_weekly", mirror_points, _meta(
            "inflation-monitor.ru", MIRROR_WEEKLY, "ok",
            "первоисточник не дался (%s), взято зеркало" % ("; ".join(notes) or "нет релизов"),
            {"mirror": True, "note_mirror": mirror_note})
    return "cpi_weekly", {}, _meta("rosstat", ROSSTAT_MAIN, "error",
                                   "; ".join(notes + [mirror_note]) or "нет данных")


def _mirror_weekly(limit=4):
    """Зеркало: берём колонку Росстата (третья), а не прогноз в первой."""
    try:
        html = get_text(MIRROR_WEEKLY, headers=_UA)
    except FetchError as exc:
        return {}, "зеркало: %s" % exc
    options = re.findall(r'<option value="([\d-]+)"[^>]*>\s*([\d.]+)\s*-\s*([\d.]+)',
                         html)
    points = {}
    # +1 к запасу: свежая неделя на зеркале какое-то время стоит «Ожидается»
    # (Росстат ещё не опубликовал), и без запаса вернётся пустой результат
    for slug, _, end in options[:max(1, limit) + 1]:
        page = html if slug == (options[0][0] if options else None) else None
        if page is None:
            try:
                page = get_text(MIRROR_WEEKLY.rstrip("/") + "/" + slug, headers=_UA)
            except FetchError:
                continue
        value = _mirror_value(page)
        day = _mirror_day(end)
        if value is not None and day:
            points[day] = value
    return points, "разобрано недель: %d" % len(points)


def _mirror_value(html):
    m = re.search(r'<table id="ipc-table".*?</table>', html, re.S)
    if not m:
        return None
    cells = re.findall(r'<td[^>]*class="([^"]*)"[^>]*>(.*?)</td>', m.group(0), re.S)
    for cls, raw in cells:
        if "week-rosstat" in cls:            # ключевая грабля: не первая колонка
            value = _num(_plain(raw))
            return None if value is None else round(value - 100.0, 4)
    return None


def _mirror_day(end):
    """'03.08.2026' -> '2026-08-03'."""
    m = re.match(r"(\d{2})\.(\d{2})\.(20\d\d)", end)
    return "%s-%s-%s" % (m.group(3), m.group(2), m.group(1)) if m else None


# -------------------------------------------------------- месячный ИПЦ (xlsx)

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_NSR = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def xlsx_sheet(data, wanted_name=None):
    """xlsx (байты) -> [[значения строки]] нужного листа (первого, если не задан).

    Читает pipeline/lib/xlsx: там ячейки раскладываются ПО АДРЕСАМ (r="B2"), а не
    подряд. Разница не косметическая — прежняя своя читалка складывала ячейки в
    порядке появления, а ПУСТЫЕ ЯЧЕЙКИ В XML ПРОСТО ОТСУТСТВУЮТ: перегенерённый
    без пустых ячеек файл (обычная нормализация writer'а) молча сдвигал весь
    месячный ИПЦ на соседний ГОД — значения 100,x проходят стражу диапазона, и по
    графику сдвиг неотличим от правды (аудит 18.08.2026).
    """
    if _xlsx is None:
        raise FetchError("pipeline/lib/xlsx недоступен — книгу разобрать нечем")
    try:
        book = _xlsx.open_bytes(data)
    except _xlsx.XlsxError as exc:
        raise FetchError(str(exc)) from exc
    name = wanted_name
    if name is not None and name.strip() not in book.sheets:
        raise FetchError("в книге нет листа %r (есть: %s)" % (name, book.sheets))
    if name is None:
        if not book.sheets:
            raise FetchError("в книге нет ни одного листа")
        name = book.sheets[0]
    try:
        rows = book.rows(name.strip())
    except _xlsx.XlsxError as exc:
        raise FetchError(str(exc)) from exc
    # Прежний контракт этой функции: значения-СТРОКИ (числа разбирают вызывающие),
    # None вместо пустых. lib/xlsx отдаёт float — возвращаем в текст без хвоста
    # «.0» у целых, чтобы не трогать разборщики ниже.
    out = []
    for row in rows:
        vals = []
        for v in row:
            if isinstance(v, float):
                vals.append(("%d" % v) if v == int(v) else repr(v))
            else:
                vals.append(v)
        out.append(vals)
    return out


def parse_cpi_monthly(rows):
    """Лист «01» файла ipc_mes -> {последний день месяца: прирост, %}.

    Раскладка: строка с годами (B..), затем блок «к концу предыдущего месяца» и
    12 строк с названиями месяцев. Дальше в листе идут другие базы сравнения
    (к декабрю, к соответствующему месяцу) — их брать НЕЛЬЗЯ, поэтому читаем
    ровно один блок после нужного заголовка.
    """
    years, start = None, None
    for idx, row in enumerate(rows):
        cells = [c for c in row if c]
        if years is None and len(cells) >= 5 and all(
                re.match(r"^(19|20)\d\d$", str(c).strip()) for c in cells[:5]):
            years = [int(str(c).strip()) if re.match(r"^(19|20)\d\d$", str(c).strip())
                     else None for c in row]
        if row and row[0] and "к концу предыдущего месяца" in str(row[0]).lower():
            start = idx + 1
            if years is not None:
                break
    if years is None or start is None:
        return {}
    out = {}
    for row in rows[start:start + 12]:
        if not row or not row[0]:
            continue
        name = str(row[0]).strip().lower()
        if name not in _MONTH_ORDER:
            continue
        month = _MONTH_ORDER.index(name) + 1
        for col, raw in enumerate(row):
            if col >= len(years) or years[col] is None or raw in (None, ""):
                continue
            value = _num(raw)
            if value is None or not 20.0 < value < 500.0:
                continue
            out[_month_end(years[col], month)] = round(value - 100.0, 4)
    return out


def _cpi_monthly_xlsx():
    """Прежний путь: файл ipc_mes со страницы «Цены» (с 11.2026 отдаёт 404, грабля 5)."""
    try:
        page = get_text(ROSSTAT_PRICE, headers=_UA)
    except FetchError as exc:
        return "cpi_monthly", {}, _meta("rosstat", ROSSTAT_PRICE, "error", str(exc))
    files = re.findall(r'href="([^"]*ipc_mes_(\d{2})-(\d{4})\.xlsx)"', page)
    if not files:
        return "cpi_monthly", {}, _meta(
            "rosstat", ROSSTAT_PRICE, "error",
            "на странице цен нет файла ipc_mes_MM-YYYY.xlsx (сменилось имя?)")
    href, month, year = max(files, key=lambda f: (f[2], f[1]))
    url = href if href.startswith("http") else "https://rosstat.gov.ru" + href
    try:
        rows = xlsx_sheet(get_bytes(url, headers=_UA), "01")
    except (FetchError, zipfile.BadZipFile, ET.ParseError, KeyError) as exc:
        return "cpi_monthly", {}, _meta("rosstat", url, "error",
                                        "xlsx не разобрался: %s" % exc)
    points = parse_cpi_monthly(rows)
    if not points:
        return "cpi_monthly", {}, _meta("rosstat", url, "error",
                                        "лист «01» разобран, блок м/м не найден")
    # asof — по РЯДУ, а не по своей порции. xlsx Росстата отстаёт от исследовательской
    # затравки (июльский ipc_mes выходит только 9–18 августа), и `max(points)` откатывал
    # meta.asof с июля на июнь: ряд выглядел протухшим при живых данных — ровно то,
    # от чего защищается store.py:134-136 («asof пересчитываем каждый раз»).
    asof = max(points)
    known = _stored_last("cpi_monthly")
    if known and known > asof:
        asof = known
    return "cpi_monthly", points, _meta(
        "rosstat", url, "ok", "месяцев в ряду: %d (файл за %s.%s)"
        % (len(points), month, year), {"asof": asof})


# ------------------------------------------------- месячный ИПЦ (релиз, PDF/HTML)

_LINK = re.compile(r'<a\b[^>]*?\bhref="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)
_MONTHLY_TITLE = re.compile(
    r"^об\s+индексе\s+потребительских\s+цен\s+в\s+([а-яё]+)\s+(20\d\d)\s+год", re.I)
_MONTH_STEM = "(январ|феврал|март|апрел|ма|июн|июл|август|сентябр|октябр|ноябр|декабр)[а-я]*"
# Лид релиза: «В январе 2026 г. по сравнению с декабрем 2025 г. индекс потребительских
# цен составил 101,62%». Ищем в тексте БЕЗ ПРОБЕЛОВ: извлечённый из PDF текст рвёт
# слова и числа переводами строк («10\n0,08%», «ок\nтября»). Месяцы названы
# основами, а не «[а-я]+»: иначе «в» из соседнего слова съедает начало совпадения.
_RELEASE_LEAD = re.compile(
    r"в" + _MONTH_STEM + r"(20\d\d)(?:года|г\.?)?посравнениюс" + _MONTH_STEM
    + r"(20\d\d)(?:года|г\.?)?индекспотребительскихцен[^%]{0,40}?составил"
    r"(\d{2,3},\d{1,2})%")


def _today_msk():
    return datetime.now(MSK).date()


def _abs_url(href):
    href = _unescape(href.strip())
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("http"):
        return href
    return "https://rosstat.gov.ru" + ("" if href.startswith("/") else "/") + href


def monthly_releases(html):
    """[(url, год, месяц)] релизов «Об индексе потребительских цен в <месяце> <года>».

    Недельные «Об ОЦЕНКЕ индекса…» сюда не попадают по построению: у них другое
    начало заголовка.
    """
    out, seen = [], set()
    for href, inner in _LINK.findall(html or ""):
        m = _MONTHLY_TITLE.match(_plain(inner))
        if not m:
            continue
        month = _month_num(m.group(1))
        if not month:
            continue
        url = _abs_url(href)
        if url in seen:
            continue
        seen.add(url)
        out.append((url, int(m.group(2)), month))
    return out


def parse_cpi_release(text):
    """Текст месячного релиза -> (последний день месяца, прирост м/м, %) или None.

    Берётся ТОЛЬКО сравнение с ПРЕДЫДУЩИМ месяцем: в том же лиде стоят «по сравнению
    с декабрем» (с начала года) и справочно прошлогодний месяц, и любое из них,
    взятое по ошибке, проходит стражу диапазона, а на графике неотличимо от правды.
    """
    squeezed = re.sub(r"\s+", "", (text or "").lower()).replace("ё", "е")
    for m in _RELEASE_LEAD.finditer(squeezed):
        month, prev = _month_num(m.group(1)), _month_num(m.group(3))
        year, prev_year = int(m.group(2)), int(m.group(4))
        if not month or not prev:
            continue
        want = (year - 1, 12) if month == 1 else (year, month - 1)
        if (prev_year, prev) != want:
            continue                      # база — не прошлый месяц
        value = _num(m.group(5))
        if value is None or not 80.0 < value < 130.0:
            continue
        return _month_end(year, month), round(value - 100.0, 4)
    return None


def _is_pdf(data):
    return b"%PDF-" in (data or b"")[:1024]


def _release_text(url):
    """Текст релиза по ссылке из ленты: PDF или HTML (с переходом на PDF)."""
    data = get_bytes(url, headers=_UA)
    if not _is_pdf(data):
        page = data.decode("utf-8", "replace")
        # страница документа, которая не перенаправила сама, а лишь назвала файл
        hop = re.search(r'href="([^"]+\.pdf)"', page, re.I)
        if "потребительских цен" not in _plain(page).lower() and hop:
            data = get_bytes(_abs_url(hop.group(1)), headers=_UA)
        else:
            return _plain(page)
    if not _is_pdf(data):
        return _plain(data.decode("utf-8", "replace"))
    if _pdf_text is None:
        raise FetchError("релиз в PDF, а извлекателя текста нет (fetch/orfr.pdf_text)")
    try:
        return _pdf_text(data)
    except Exception as exc:              # noqa: BLE001 — битый PDF не роняет прогон
        raise FetchError("PDF релиза не читается: %s" % exc) from exc


def cpi_monthly():
    """-> ("cpi_monthly", {последний день месяца: прирост м/м, %}, meta).

    Первоисточник — месячный релиз Росстата в ленте новостей (грабля 5), запас —
    прежний xlsx. Статус различает три случая, которые раньше сливались в error и
    будили владельца зря:
      * релиз за прошлый месяц ЕЩЁ НЕ ВЫШЕЛ (до RELEASE_DUE_DAY) или уже в ряду —
        ok без новых точек, asof = последняя точка ряда; протухание, если оно
        случится, поймает норма возраста данных;
      * релиз найден, но число не разобралось, или к RELEASE_DUE_DAY его нет —
        error: это поломка разбора или публикации;
      * лента Росстата не открылась — error.
    """
    today = _today_msk()
    known = _stored_last("cpi_monthly")
    exp_year, exp_month = ((today.year - 1, 12) if today.month == 1
                           else (today.year, today.month - 1))
    expected = _month_end(exp_year, exp_month)
    notes, releases, listed = [], [], False
    for page_url in RELEASE_PAGES:
        try:
            html = get_text(page_url, headers=_UA)
        except FetchError as exc:
            notes.append("%s: %s" % (page_url, exc))
            continue
        listed = True
        releases = monthly_releases(html)
        if releases:
            break
    broken = None
    if releases:
        url, year, month = max(releases, key=lambda r: (r[1], r[2]))
        label = "%02d.%d" % (month, year)
        try:
            got = parse_cpi_release(_release_text(url))
        except FetchError as exc:
            got, broken = None, "релиз за %s не скачался: %s" % (label, exc)
        if got is not None and got[0] == _month_end(year, month):
            day, value = got
            late = (day < expected and not (known and known >= expected)
                    and today.day >= RELEASE_DUE_DAY)
            if not late:
                return "cpi_monthly", {day: value}, _meta(
                    "rosstat", url, "ok", "релиз за %s: %+.2f%% м/м" % (label, value),
                    {"asof": max(day, known or day), "release": label})
            # в ленте только прошлый релиз, а срок нового прошёл — это не «ок»
            notes.append("в ленте только релиз за %s" % label)
        elif broken is None:
            broken = ("релиз за %s открылся, но строки «по сравнению с <прошлым месяцем> "
                      "… составил N%%» в нём нет — сменился формат?" % label)
    # Нового числа из релиза нет — запасной путь, вдруг Росстат вернул xlsx.
    sid, xpoints, xmeta = _cpi_monthly_xlsx()
    if xmeta.get("status") == "ok":
        xmeta["note"] = "%s; релиз: %s" % (xmeta.get("note"), broken or "нового нет")
        return sid, xpoints, xmeta
    notes.append("xlsx: %s" % xmeta.get("note"))
    if broken:
        return "cpi_monthly", {}, _meta("rosstat", url, "error",
                                        "; ".join([broken] + notes))
    if not listed:
        return "cpi_monthly", {}, _meta("rosstat", ROSSTAT_NEWS, "error",
                                        "лента Росстата не открылась: " + "; ".join(notes))
    have = bool(known and known >= expected)
    if have or today.day < RELEASE_DUE_DAY:
        why = ("за %02d.%d уже в ряду" % (exp_month, exp_year) if have else
               "релиз за %02d.%d ещё не вышел (Росстат публикует 10–16 числа)"
               % (exp_month, exp_year))
        return "cpi_monthly", {}, _meta("rosstat", ROSSTAT_NEWS, "ok",
                                        "новых данных нет: " + why,
                                        {"asof": known, "failed": notes})
    return "cpi_monthly", {}, _meta(
        "rosstat", ROSSTAT_NEWS, "error",
        "релиза за %02d.%d нет в ленте Росстата к %d числу; %s"
        % (exp_month, exp_year, today.day, "; ".join(notes)))
