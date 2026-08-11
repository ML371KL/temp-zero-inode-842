"""Общая обвязка фетчеров.

Контракт (docs/CONTRACT.md §4): каждая функция возвращает
    (series_id, {date: value}, meta)
либо СПИСОК таких троек, если источник за один запрос отдаёт несколько рядов
(zcyc — пять сроков, futoi — две группы клиентов). В стор фетчеры не пишут:
запись — дело вызывающего (run.py), чтобы «скачал, но не сохранил» и «сохранил,
но не опубликовал» были разными состояниями, а не одним.

Читать стор фетчерам можно и нужно — на этом стоит инкрементальность.

Импорт-шим ниже: run.py могут запускать и как `python -m pipeline.run` из корня
репо, и как `python pipeline/run.py` (тогда в sys.path попадает сам pipeline/).
Относительный импорт пробуем первым — так в обоих случаях в процессе живёт ровно
одна копия lib.http с общим троттлингом (две копии = двойной поток к ISS).
"""

try:  # пакет pipeline.fetch
    from ..lib import dates, http, store
except ImportError:  # sys.path указывает внутрь pipeline/
    from lib import dates, http, store

FetchError = http.FetchError

# Насколько перетягивать хвост уже собранного ряда: источники правят
# опубликованное задним числом (ISS пересчитывает обороты и значения индексов
# после вечерней сессии, ЦБ уточняет декады).
RETRO_DAYS = 5

__all__ = ["dates", "http", "store", "FetchError", "RETRO_DAYS",
           "incremental_start", "make_meta", "to_float", "empty_is_fatal"]


def incremental_start(series_id, back_days=RETRO_DAYS, default_start="1997-01-01",
                      bootstrap=False):
    """С какой даты тянуть ряд: последняя точка в сторе минус запас на ретро-правки.

    bootstrap=True (run.py --mode bootstrap) — тянуть всё заново: ряд, который
    однажды долился с дырой в середине, инкрементальным окном не чинится никогда.
    """
    if bootstrap:
        return default_start
    last = store.last_date(series_id)
    if not last:
        return default_start
    return dates.fmt_date(dates.add_days(last, -abs(back_days)))


def make_meta(source, url, points=None, status="ok", note=None, asof=None, **extra):
    """meta по контракту §1 (+ произвольные поля источника: secid, rows, delay_min)."""
    meta = {
        "source": source,
        "url": url,
        "fetched_at": dates.iso_utc(),
        "asof": asof or dates.last_date_in_points(points or {}),
        "status": status,
        "note": note,
    }
    meta.update(extra)
    return meta


def to_float(value):
    """Число из ячейки источника или None. Запятая-разделитель — норма у ЦБ,
    неразрывный пробел в разрядах — тоже (HTML-таблицы ЦБ)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("\xa0", "").replace(" ", "").replace(",", ".")
    if not text or text in {"-", "—", ".", "n/a", "null"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def empty_is_fatal(series_id):
    """Пусто в окне — не всегда отказ источника.

    У futoi бесплатный ISS отдаёт с лагом ~14 дней, FRED публикует Brent с лагом
    3–7 дней: «в последние 5 дней ничего нового» — это норма, а не ошибка. А вот
    пусто при полностью пустом сторе означает, что мы стучимся не туда.
    """
    return store.last_date(series_id) is None
