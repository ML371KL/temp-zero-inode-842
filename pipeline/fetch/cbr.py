"""Фетчеры Банка России: официальный курс, ключевая ставка, депозитные декады.

Три разных источника с тремя разными характерами:
  * XML_dynamic — windows-1251, десятичная ЗАПЯТАЯ, даты dd/mm/yyyy в запросе;
  * hd_base/keyrate и statistics/avgprocstav — обычный HTML (utf-8) с таблицей
    <table class="data">, даты в запросе dd.mm.yyyy;
  * у avgprocstav форма дат критична: короткая форма mm.yyyy молча отдаёт лишь
    последние пару лет (проверено: с 1.2009 вернулось 67 декад с 09.2024),
    а dd.mm.yyyy — всю историю с 2009 года (614 декад). Формат тут не косметика.
"""

import re
from html.parser import HTMLParser
from urllib.parse import urlencode

from . import (FetchError, dates, empty_is_fatal, http, incremental_start,
               make_meta, to_float)

FX_URL = "https://www.cbr.ru/scripts/XML_dynamic.asp"
KEYRATE_URL = "https://www.cbr.ru/hd_base/keyrate/"
DEPOSIT_URL = "https://www.cbr.ru/statistics/avgprocstav/"
# Ключевая ставка есть и в SOAP-сервисе ЦБ. Он предпочтителен по двум причинам:
# ответ на то же окно — ~1,8 КБ против ~92 КБ у HTML-страницы (ряд опрашивается
# каждым интрадей-тактом, чтобы решение по ставке было видно за минуты, а не через
# пять часов), и это структурированный XML, а не таблица, которую держит вёрстка.
# Отвечает только на POST: GET-вариант сервиса выключен (проверено 12.08.2026 —
# «Формат запроса не распознан»).
KEYRATE_SOAP = "https://www.cbr.ru/DailyInfoWebServ/DailyInfo.asmx"
_KEYRATE_ENVELOPE = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
    "<soap:Body><KeyRate xmlns=\"http://web.cbr.ru/\">"
    "<fromDate>%s</fromDate><ToDate>%s</ToDate>"
    "</KeyRate></soap:Body></soap:Envelope>")

# Коды валют ЦБ -> id рядов реестра.
FX_IDS = {"R01235": "usd_cbr", "R01375": "cny_cbr", "R01239": "eur_cbr"}

# Ключевая ставка существует с 13.09.2013; курс тянем с 2003, чтобы у панели с
# 2004 года был разгон для 63-дневного моментума (usd_mom63 — нога ядра).
FX_DEFAULT_START = "2003-01-01"
KEYRATE_DEFAULT_START = "2013-09-13"
DEPOSIT_DEFAULT_START = "2009-01-01"
DEPOSIT_BACK_DAYS = 40  # декада правится задним числом; 40 дней = 4 декады запаса

_RECORD_RE = re.compile(r'<Record\s+Date="([\d.]+)"[^>]*>(.*?)</Record>', re.S)
_VUNIT_RE = re.compile(r"<VunitRate>([^<]+)</VunitRate>")
_VALUE_RE = re.compile(r"<Value>([^<]+)</Value>")
_NOMINAL_RE = re.compile(r"<Nominal>([^<]+)</Nominal>")
_DECADE_RE = re.compile(r"^(I{1,3})\.(\d{1,2})\.(\d{4})$")


# --------------------------------------------------------------- курс валюты
def _drop_future(points, till, sid, slack_days=7):
    """Выбросить точки дальше запрошенного края + недельный запас.

    Инвариант проекта «точка вне запрошенного окна в ряд не попадает» жил только в
    ISS-фетчерах; ЦБ отдавал даты как есть. Цена: один битый Record Date (класс
    сбоя, уже ловленный у ISS — 2099-12-31) отравляет ряд НАВСЕГДА: upsert сливает
    точки, incremental_start видит «последняя точка в будущем» и каждый прогон
    заново тянет историю с 2003-го, а вычистить фантом можно только рукой в сторе.
    У usd_cbr это нога ядра. Запас в неделю пропускает законное «курс на завтра
    опубликован сегодня» и длинные праздники; прошлое не режем — история в окне
    правдива по построению.
    """
    edge = dates.fmt_date(dates.add_days(till, slack_days))
    bad = sorted(d for d in points if d > edge)
    for d in bad:
        points.pop(d)
    if bad:
        http.LOG(f"{sid}: {len(bad)} точек дальше {edge} отброшено "
                 f"(первая {bad[0]}) — дата из будущего не попадает в ряд")
    return points


def fx(code="R01235", series_id=None, start=None, end=None, bootstrap=False):
    """Официальный курс ЦБ по дате ПРИМЕНЕНИЯ.

    Record Date в XML_dynamic — это дата, НА которую курс действует (опубликован
    он накануне). Ничего сдвигать не надо: именно так ряд лагирован в валидации,
    и именно поэтому заглядывания в будущее тут нет.
    """
    sid = series_id or FX_IDS.get(code.upper(), f"fx_{code.lower()}")
    frm = dates.parse_date(start or incremental_start(sid, 5, FX_DEFAULT_START,
                                                      bootstrap))
    till = dates.parse_date(end or dates.today_msk())
    url = f"{FX_URL}?" + urlencode({"date_req1": dates.fmt_ru(frm, "/"),
                                    "date_req2": dates.fmt_ru(till, "/"),
                                    "VAL_NM_RQ": code})
    text = http.get_text(url, encoding="windows-1251")
    points = {}
    for day, body in _RECORD_RE.findall(text):
        value = _fx_value(body)
        if value is None:
            continue
        points[dates.fmt_date(day)] = value
    _drop_future(points, till, sid)
    if not points and empty_is_fatal(sid):
        raise FetchError(f"ЦБ: пустой ряд курса {code} за {frm}..{till}", url=url)
    return sid, points, make_meta("cbr", url, points, unit="rub", code=code,
                                  note="дата = дата применения курса")


def _fx_value(body):
    """VunitRate = курс за ЕДИНИЦУ валюты. Его и берём: у юаня номинал менялся,
    и Value без деления на Nominal даёт скачок ряда на порядок."""
    m = _VUNIT_RE.search(body)
    if m:
        return to_float(m.group(1))
    value = to_float(_VALUE_RE.search(body).group(1)) if _VALUE_RE.search(body) else None
    nominal = to_float(_NOMINAL_RE.search(body).group(1)) if _NOMINAL_RE.search(body) else 1.0
    if value is None or not nominal:
        return None
    return value / nominal


# ------------------------------------------------------------- ключевая ставка
_KR_ROW = re.compile(r"<KR\b[^>]*>\s*<DT>([^<]+)</DT>\s*<Rate>([^<]+)</Rate>", re.S)


def _keyrate_soap(frm, till):
    """{дата: ставка} из SOAP-сервиса ЦБ. Пусто — значит сервис не дал данных."""
    body = (_KEYRATE_ENVELOPE % (dates.fmt_date(frm), dates.fmt_date(till))).encode("utf-8")
    text = http.get_text(KEYRATE_SOAP, data=body, retries=2,
                         headers={"Content-Type": "text/xml; charset=utf-8",
                                  "SOAPAction": '"http://web.cbr.ru/KeyRate"'})
    points = {}
    for raw_day, raw_rate in _KR_ROW.findall(text):
        try:
            day = dates.fmt_date(str(raw_day)[:10])
        except ValueError:
            continue
        value = to_float(raw_rate)
        if value is not None:
            points[day] = value
    return points


def keyrate(series_id="key_rate", start=None, end=None, bootstrap=False):
    """Ключевая ставка, % годовых. ЦБ отдаёт значение на каждый рабочий день —
    так и храним (реестр зовёт ряд событийным, но событие видно как изменение).

    Сначала SOAP (лёгкий XML), при отказе — та же таблица со страницы hd_base.
    Резерв не декоративный: страница переживёт смену SOAP-контракта, а SOAP —
    смену вёрстки, и ряд, по которому строится «сюрприз против консенсуса», не
    должен зависеть от одного из двух.
    """
    frm = dates.parse_date(start or incremental_start(series_id, 5,
                                                      KEYRATE_DEFAULT_START, bootstrap))
    till = dates.parse_date(end or dates.today_msk())
    url = KEYRATE_SOAP
    note = None
    try:
        points = _keyrate_soap(frm, till)
    except FetchError as exc:
        points, note = {}, f"SOAP не ответил ({exc}); взята страница hd_base"
    if not points:
        note = note or "SOAP вернул пусто; взята страница hd_base"
        url = f"{KEYRATE_URL}?" + urlencode({"UniDbQuery.Posted": "True",
                                             "UniDbQuery.From": dates.fmt_ru(frm),
                                             "UniDbQuery.To": dates.fmt_ru(till)})
        rows = _data_rows(http.get_text(url), url, want="Дата")
        points = {}
        for row in rows:
            if len(row) < 2:
                continue
            try:
                day = dates.fmt_date(row[0])
            except ValueError:
                continue  # строка заголовка/итога
            value = to_float(row[1])
            if value is not None:
                points[day] = value
    _drop_future(points, till, series_id)
    if not points and empty_is_fatal(series_id):
        raise FetchError(f"ЦБ: ключевая ставка пуста за {frm}..{till}", url=url)
    return series_id, points, make_meta("cbr", url, points, unit="pct", note=note)


# --------------------------------------------------------- депозитные декады
def deposit(series_id="deposit_decade", start=None, end=None, bootstrap=False):
    """Максимальная ставка по вкладам топ-10 банков, декадами.

    Ключ точки — КОНЕЦ декады (I→10-е, II→20-е, III→конец месяца), как дата
    периода; лаг доступности (+4 дня, registry.pub_lag_days) накидывает слой
    расчёта, а не стор (контракт §1).
    """
    frm = dates.parse_date(start or incremental_start(series_id, DEPOSIT_BACK_DAYS,
                                                      DEPOSIT_DEFAULT_START, bootstrap))
    till = dates.parse_date(end or dates.today_msk())
    url = f"{DEPOSIT_URL}?" + urlencode({"UniDbQuery.Posted": "True",
                                         "UniDbQuery.From": dates.fmt_ru(frm),
                                         "UniDbQuery.To": dates.fmt_ru(till)})
    rows = _data_rows(http.get_text(url), url, want="Декада")
    points = {}
    for row in rows:
        if len(row) < 2:
            continue
        day = decade_end(row[0])
        value = to_float(row[1])
        if day and value is not None:
            points[dates.fmt_date(day)] = value
    # Запас шире дневных рядов: ключ точки — КОНЕЦ декады, и у третьей декады он
    # впереди даты запроса почти на месяц.
    _drop_future(points, till, series_id, slack_days=35)
    if not points and empty_is_fatal(series_id):
        raise FetchError(f"ЦБ: таблица депозитных декад пуста за {frm}..{till}", url=url)
    return series_id, points, make_meta("cbr", url, points, unit="pct",
                                        note="дата = конец декады")


def decade_end(label):
    """'III.07.2026' -> 2026-07-31. I -> 10-е, II -> 20-е, III -> конец месяца."""
    m = _DECADE_RE.match(str(label).strip())
    if not m:
        return None
    roman, month, year = m.group(1), int(m.group(2)), int(m.group(3))
    if roman == "I":
        return dates.parse_date(f"{year:04d}-{month:02d}-10")
    if roman == "II":
        return dates.parse_date(f"{year:04d}-{month:02d}-20")
    return dates.month_end(f"{year:04d}-{month:02d}-01")


# ------------------------------------------------------------- разбор HTML ЦБ
class _DataTables(HTMLParser):
    """Строки таблиц <table class="data"> — единственный формат, в котором ЦБ
    отдаёт эти два ряда. Вложенность считаем честно: страница со временем обрастает
    вёрсткой, и «первая таблица на странице» перестаёт быть нашей."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self._depth = 0
        self._start_depth = None
        self._rows = None
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._depth += 1
            classes = (dict(attrs).get("class") or "").split()
            if self._rows is None and "data" in classes:
                self._rows, self._start_depth = [], self._depth
        elif self._rows is not None:
            if tag == "tr":
                self._row = []
            elif tag in ("td", "th"):
                self._cell = []

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        if tag == "table":
            if self._rows is not None and self._depth == self._start_depth:
                self.tables.append(self._rows)
                self._rows, self._start_depth = None, None
            self._depth = max(0, self._depth - 1)
        elif self._rows is not None:
            if tag in ("td", "th") and self._cell is not None:
                text = "".join(self._cell).replace("\xa0", " ").strip()
                if self._row is None:
                    self._row = []
                self._row.append(text)
                self._cell = None
            elif tag == "tr" and self._row is not None:
                self._rows.append(self._row)
                self._row = None


def _data_rows(html, url, want=None):
    """Строки нужной таблицы без заголовка. `want` — слово из шапки."""
    parser = _DataTables()
    parser.feed(html)
    tables = [t for t in parser.tables if t]
    if not tables:
        # Так выглядит заглушка/капча/редирект — источник отказал, прогон живёт.
        raise FetchError("ЦБ: на странице нет таблицы class=\"data\"", url=url)
    chosen = None
    if want:
        chosen = next((t for t in tables if any(want in c for c in t[0])), None)
    table = chosen or max(tables, key=len)
    return table[1:]
