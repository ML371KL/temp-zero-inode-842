"""Внешние (не российские) источники. Пока один: Brent из FRED.

FRED публикует DCOILBRENTEU с лагом 3–7 дней (проверено 11.08.2026: последняя
точка 03.08). Это не поломка источника, а его расписание: за свежесть отвечает
фьючерс BR на МосБирже (fetch/iss.futures_br), за историю — этот ряд.
"""

import csv
from urllib.parse import urlencode

from . import (FetchError, dates, empty_is_fatal, http, incremental_start,
               make_meta, to_float)

FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"
BRENT_DEFAULT_START = "1997-01-01"  # глубже панели дашборда всё равно не нужно


def brent_fred(series_id="brent", fred_id="DCOILBRENTEU", start=None, end=None,
               bootstrap=False):
    """Дневная цена Brent, $/барр. Пропуски в CSV помечены точкой — это выходные
    и праздники США, а не нули: такие строки просто не кладём в ряд."""
    frm = start or incremental_start(series_id, 5, BRENT_DEFAULT_START, bootstrap)
    params = {"id": fred_id, "cosd": dates.fmt_date(frm)}
    if end:
        params["coed"] = dates.fmt_date(end)
    url = f"{FRED_CSV}?{urlencode(params)}"
    text = http.get_text(url)

    reader = csv.reader(text.splitlines())
    header = next(reader, None)
    if not header or len(header) < 2:
        raise FetchError("FRED: не CSV в ответе", url=url)
    points = {}
    for row in reader:
        if len(row) < 2:
            continue
        value = to_float(row[1])
        if value is None:
            continue
        try:
            points[dates.fmt_date(row[0])] = value
        except ValueError:
            continue  # мусорная строка в хвосте файла
    if not points and empty_is_fatal(series_id):
        raise FetchError(f"FRED: пустой ряд {fred_id} с {frm}", url=url)
    return series_id, points, make_meta("fred", url, points, unit="usd", fred_id=fred_id,
                                        note="FRED публикует с лагом 3–7 дней")
