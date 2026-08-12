"""Справочная таблица «Данные, применяемые для расчёта НДПИ в отношении нефти».

Отсюда берётся `urals_tax` — НАЛОГОВАЯ цена Юралс, нога ядра. Это не выбор из
удобства: ровно из этой таблицы (КонсультантПлюс, `cons_doc_LAW_50642`) исследование
собрало весь ряд, на котором посчитан IC −0,19, и её же цитируют письма ФНС и релизы
Минэка. Формулировка колонки — «Средний уровень цен нефти сорта „Юралс“», основание в
последней колонке — «Информация Минэкономразвития России».

ПОЧЕМУ НЕ ПРЕСС-РЕЛИЗЫ АГЕНТСТВ. Налоговая серия местами расходится с «рыночной»
ценой из пересказов СМИ, и расхождение не теоретическое: январь-2026 официально
40,95, а в лентах ходило ~45. Подставить рыночную цену в ряд — значит незаметно
поменять величину, на которой калибровалось ядро. Зеркала СМИ остались в minfin.py,
но только как СВЕРКА: их число уходит в meta.conflicts и в ряд не попадает.

ПОЧЕМУ НЕ МИНЭК НАПРЯМУЮ. `economy.gov.ru` не резолвится с прод-машины, виснет на
рукопожатии с ноутбука и не отвечает по IP (замеры 12.08.2026, docs/LATENCY.md §3.3).

Как устроен обход:
  1. карточка документа (адрес постоянный) → список разделов со ССЫЛКАМИ И
     ЗАГОЛОВКАМИ. Хэш раздела меняется при обновлении документа, поэтому раздел
     ищется по заголовку, а не по запомненному адресу;
  2. подходящих разделов ровно два — «1.3.» (нефть) и «2.2.» (нефть и газовый
     конденсат). Оба печатают одну и ту же цену, и это бесплатная перекрёстная
     проверка внутри одного документа;
  3. в каждой строке рядом с ценой стоят курс и Кц, а между ними жёсткое
     тождество Кц = (Ц − 15) × Р / 261. Строка, где оно не сходится, — это
     съехавшие колонки, и такую строку брать нельзя. У самого свежего месяца курса
     и Кц ещё нет (Минэк уже опубликовал цену, ФНС ещё не досчитала) — это норма,
     проверять там нечего.
"""

import re
from datetime import date, timedelta

try:                                       # прод: общий HTTP-слой (CONTRACT.md §4)
    from lib.http import get_text, FetchError
except ImportError:
    try:
        from pipeline.lib.http import get_text, FetchError
    except ImportError:                    # автономный запуск (отладка парсеров)
        class FetchError(Exception):
            pass

        def get_text(url, timeout=40, headers=None, **_kw):
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

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
       "Accept-Language": "ru,en;q=0.8"}

BASE = "https://www.consultant.ru"
CARD = BASE + "/document/cons_doc_LAW_50642/"
# Разделы с ЖИВОЙ таблицей: «1.3.» и «2.2.». Требуем двухуровневый номер — у «1.» и
# «2.» тот же заголовок, но это оглавления, а таблицы с месяцами в них нет.
SECTION_RE = re.compile(
    r'<a[^>]+href="(/document/cons_doc_LAW_50642/[a-f0-9]{8,}/)"[^>]*>(.{0,200}?)</a>', re.S)
SECTION_TITLE = re.compile(
    r"^\d+\.\d+\.\s*данные,\s*применяемые\s*для\s*расч[её]та\s*налога\s*на\s*добычу"
    r"\s*полезных\s*ископаемых\s*в\s*отношении\s*нефти")

_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S)
_MONTHS = {"январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
           "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12}
_PERIOD = re.compile(r"^([а-яё]{3,9})\s+(20\d\d)\s*(?:г\.?)?$", re.I)

# Кц = (Ц − 15) × Р / 261 (п. 3 ст. 342 НК). Допуск — на округление таблицы до
# четвёртого знака: у Кц порядка 20 это ±0,0006, берём вдесятеро больше.
KC_BASE, KC_DIV, KC_TOL = 15.0, 261.0, 0.006
PRICE_MIN, PRICE_MAX = 5.0, 250.0        # санитарный коридор $/барр


def _plain(fragment):
    text = re.sub(r"<[^>]+>", " ", fragment)
    text = (text.replace("&nbsp;", " ").replace("&#160;", " ")
                .replace("&quot;", '"').replace("&amp;", "&"))
    return re.sub(r"[\s\xa0 ]+", " ", text).strip()


def _num(raw):
    """'73,5447' / '1 234,5' -> float. Пустая ячейка -> None."""
    txt = re.sub(r"[\s\xa0 ]", "", str(raw)).replace(",", ".")
    if not txt or txt.count(".") > 1:
        return None
    try:
        return float(txt)
    except ValueError:
        return None


def month_end(period):
    """'Июль 2026' -> '2026-07-31'. Не месяц (квартал, диапазон) -> None."""
    m = _PERIOD.match(_plain(period))
    if not m:
        return None
    word = m.group(1).lower()
    num = next((v for stem, v in _MONTHS.items() if word.startswith(stem)), None)
    if num is None:
        return None
    year = int(m.group(2))
    nxt = (year + 1, 1) if num == 12 else (year, num + 1)
    return (date(nxt[0], nxt[1], 1) - timedelta(days=1)).isoformat()


def sections(card_html):
    """[(url, заголовок)] разделов с живой таблицей, в порядке появления."""
    out, seen = [], set()
    for href, inner in SECTION_RE.findall(card_html):
        title = _plain(inner)
        if href in seen or not SECTION_TITLE.match(title.lower()):
            continue
        seen.add(href)
        out.append((BASE + href, title))
    return out


def parse_table(html, check_kc=True):
    """HTML раздела -> ({дата: цена}, [проблемы]).

    Строка принимается, только если период разобрался в месяц, цена лежит в
    санитарном коридоре и (когда курс с Кц уже напечатаны) сходится тождество Кц.

    `check_kc=False` — для раздела про нефть И ГАЗОВЫЙ КОНДЕНСАТ: колонки там те же
    и цена та же (проверено: 63,52 и курс 73,5447 совпадают до знака), но Кц свой,
    считается по другому знаменателю (16,2032 против 13,6720 за тот же июнь).
    Гонять по нему нефтяное тождество — значит забраковать исправную таблицу.
    """
    points, problems = {}, []
    for row_html in _ROW.findall(html):
        cells = [_plain(c) for c in _CELL.findall(row_html)]
        if len(cells) < 2:
            continue
        key = month_end(cells[0])
        if not key:
            continue                       # шапка таблицы или архивный диапазон
        price = _num(cells[1])
        if price is None or not (PRICE_MIN < price < PRICE_MAX):
            problems.append("%s: цена «%s» вне коридора" % (cells[0], cells[1][:20]))
            continue
        fx = _num(cells[2]) if len(cells) > 2 else None
        kc = _num(cells[3]) if len(cells) > 3 else None
        if check_kc and fx and kc:
            want = (price - KC_BASE) * fx / KC_DIV
            if abs(want - kc) > KC_TOL:
                # Съехали колонки — а это ровно тот отказ, который тихо кладёт в
                # ногу ядра ставку НДПИ или курс вместо цены барреля.
                problems.append("%s: Кц не сходится (%.4f против %.4f при Ц=%s, Р=%s)"
                                % (cells[0], want, kc, price, fx))
                continue
        points[key] = price
    return points, problems


def ndpi_prices(timeout=40):
    """-> ({дата: цена, $/барр}, meta-словарь).

    Разделы читаются оба: первый даёт значения, второй подтверждает. Расхождение
    между ними не усредняется — оно уходит в conflicts и требует человека.
    """
    card = get_text(CARD, headers=_UA, timeout=timeout, retries=2)
    found = sections(card)
    if not found:
        raise FetchError("КонсультантПлюс: в карточке нет раздела с таблицей НДПИ "
                         "(изменилась разметка или заголовок)", url=CARD)
    points, conflicts, problems, used = {}, [], [], []
    for url, title in found:
        # Тождество Кц проверяем только в НЕФТЯНОМ разделе («1.x»): у конденсата
        # («2.x») своя формула коэффициента при той же колонке цены.
        oil = title.lstrip().startswith("1.")
        try:
            table, bad = parse_table(get_text(url, headers=_UA, timeout=timeout, retries=2),
                                     check_kc=oil)
        except FetchError as exc:
            problems.append("%s: %s" % (title[:40], exc))
            continue
        problems.extend(bad)
        if not table:
            problems.append("%s: таблица не разобралась" % title[:40])
            continue
        used.append(title)
        for day, value in table.items():
            if day not in points:
                points[day] = value
            elif abs(points[day] - value) > 0.005:
                conflicts.append("%s: %s против %s (%s)"
                                 % (day, value, points[day], title[:30]))
    if not points:
        raise FetchError("КонсультантПлюс: ни одной строки с ценой (%s)"
                         % ("; ".join(problems[:3]) or "разделы пусты"), url=CARD)
    return points, {"url": found[0][0], "sections": used, "conflicts": conflicts,
                    "problems": problems, "rows": len(points)}
