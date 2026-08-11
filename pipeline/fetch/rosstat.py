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
"""

import io
import re
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone

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
MIRROR_WEEKLY = "https://inflation-monitor.ru/weekly_inflation/"

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

    Своя мини-читалка: нужны только значения ячеек и общие строки. Формулы,
    стили и типы дат игнорируем — в файлах Росстата всё лежит текстом и числами.
    """
    zf = zipfile.ZipFile(io.BytesIO(data))
    names = zf.namelist()
    shared = []
    if "xl/sharedStrings.xml" in names:
        root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
        shared = ["".join(t.text or "" for t in si.iter(_NS + "t")) for si in root]
    rels = {r.get("Id"): r.get("Target")
            for r in ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))}
    sheets = [(s.get("name"), rels.get(s.get(_NSR + "id")))
              for s in ET.fromstring(zf.read("xl/workbook.xml")).iter(_NS + "sheet")]
    target = None
    for name, path in sheets:
        if path and (wanted_name is None or name.strip() == wanted_name):
            target = path
            break
    if target is None:
        raise FetchError("в книге нет листа %r (есть: %s)"
                         % (wanted_name, [n for n, _ in sheets]))
    sheet = ET.fromstring(zf.read("xl/" + target.lstrip("/")))
    rows = []
    for row in sheet.iter(_NS + "row"):
        values = []
        for cell in row.findall(_NS + "c"):
            node = cell.find(_NS + "v")
            value = node.text if node is not None else None
            if cell.get("t") == "s" and value is not None:
                value = shared[int(value)]
            values.append(value)
        rows.append(values)
    return rows


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


def cpi_monthly():
    """-> ("cpi_monthly", {последний день месяца: прирост м/м, %}, meta)."""
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
