"""Фетчеры МосБиржи (ISS). Все эндпоинты проверены вживую 2026-08-11.

Общие грабли ISS, из-за которых код выглядит именно так:
  * history отдаёт страницами по 100 строк; курсор (history.cursor) есть не у всех
    блоков, поэтому листаем до пустой/короткой страницы, а курсор используем как
    подсказку;
  * неизвестные имена в history.columns/marketdata.columns ISS молча игнорирует —
    поэтому колонки ищем по имени в ответе, а не по позиции;
  * analyticalproducts/futoi обрезает ответ на 1000 строках и НЕ понимает start=
    (проверено: start=1000 вернул ту же первую строку) — только сужение окна дат;
  * zcyc на нерабочий день может вернуть ближайший более ранний срез — сверяем
    tradedate с запрошенной датой;
  * у индексов в marketdata LASTVALUE — это закрытие ПРЕДЫДУЩЕГО дня, текущее
    значение лежит в CURRENTVALUE.

Инкрементальность: если ряд уже есть в сторе, тянем с последней даты минус
RETRO_DAYS, а не всю историю.
"""

from urllib.parse import urlencode

from . import (FetchError, RETRO_DAYS, dates, empty_is_fatal, http,
               incremental_start, make_meta, store)

ISS = "https://iss.moex.com/iss"
PAGE = 100  # размер страницы history у ISS
FUTOI_ROW_CAP = 1000  # жёсткий предел ответа analyticalproducts/futoi

# id рядов, которые нельзя вывести из тикера (registry.SERIES знает их как cny_tom/gld_tom)
SELT_IDS = {"CNYRUB_TOM": "cny_tom", "GLDRUB_TOM": "gld_tom", "USD000UTSTOM": "usd_tom"}

# Сроки КБД, которые кладём в стор. Имена — из docs/CONTRACT.md §2 (zcyc_y1/y2/y10),
# полугодовой срок пишем как zcyc_y0_5: точка в id пошла бы в имя файла и в ключ R2.
ZCYC_SERIES = [(0.5, "zcyc_y0_5"), (1.0, "zcyc_y1"), (2.0, "zcyc_y2"),
               (5.0, "zcyc_y5"), (10.0, "zcyc_y10")]

# futoi: колонка ISS -> суффикс id. pos_long_num/pos_short_num — это ЧИСЛО ЛИЦ, а не
# контрактов (в валидации из них считался средний размер позиции физика).
FUTOI_FIELDS = {"pos": "pos", "pos_long": "long", "pos_short": "short",
                "pos_long_num": "holders_long", "pos_short_num": "holders_short"}
# Физлица — базовая группа ряда (futoi_mx_pos/long/short/holders_* по контракту §2):
# сигнал контр-позиционирования построен именно на них, юрлица идут как контекст
# с отдельным префиксом.
FUTOI_GROUPS = {"FIZ": "", "YUR": "yur"}

# ~45 ликвидных бумаг TQBR — список из validation/breadth_dl.py (им считалась
# ширина рынка в VALIDATION.md §B3; менять состав = ломать сопоставимость с историей).
BREADTH_TICKERS = ["SBER", "SBERP", "GAZP", "LKOH", "GMKN", "ROSN", "VTBR", "TATN",
                   "TATNP", "SNGS", "SNGSP", "NVTK", "MGNT", "MTSS", "ALRS", "CHMF",
                   "NLMK", "MAGN", "PLZL", "MOEX", "AFLT", "AFKS", "SIBN", "PHOR",
                   "RUAL", "HYDR", "IRAO", "FEES", "RTKM", "TRNFP", "UPRO", "LSRG",
                   "PIKK", "FLOT", "YNDX", "YDEX", "FIVE", "X5", "OZON", "TCSG",
                   "T", "VKCO", "POSI", "SMLT", "BSPB"]
BREADTH_MA = 200
BREADTH_MIN_OBS = 150   # min_periods как в validation/long_panel.py
BREADTH_MIN_TICKERS = 15


# --------------------------------------------------------------- низкий уровень
def _url(path, params):
    query = dict(params or {})
    query.setdefault("iss.meta", "off")
    return f"{ISS}/{path}.json?{urlencode(query)}"


def _history(path, params, columns=None, max_pages=500):
    """Постраничный обход блока history. Возвращает (url первой страницы, колонки, строки)."""
    query = dict(params or {})
    if columns:
        query["history.columns"] = ",".join(columns)
        query["iss.only"] = "history,history.cursor"
    first_url, cols, rows, start = None, [], [], 0
    for _ in range(max_pages):
        query["start"] = start
        url = _url(path, query)
        first_url = first_url or url
        payload = http.get_json(url)
        block = payload.get("history") or {}
        cols = cols or list(block.get("columns") or [])
        data = block.get("data") or []
        rows.extend(data)
        if not data or len(data) < PAGE:
            break
        total = _cursor_total(payload)
        start += len(data)
        if total is not None and start >= total:
            break
    else:
        raise FetchError(f"ISS: страницы не кончаются на {path} (>{max_pages})", url=first_url)
    return first_url, cols, rows


def _cursor_total(payload):
    cur = payload.get("history.cursor") or {}
    try:
        idx = {c: i for i, c in enumerate(cur["columns"])}
        return int(cur["data"][0][idx["TOTAL"]])
    except (KeyError, IndexError, TypeError, ValueError):
        return None  # курсора нет — листаем по признаку короткой страницы


def _colmap(columns):
    return {name: i for i, name in enumerate(columns)}


def _num(row, idx, name):
    """Число из строки ответа по ИМЕНИ колонки: схема ISS плавает между эндпоинтами."""
    pos = idx.get(name)
    if pos is None or pos >= len(row):
        return None
    value = row[pos]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


# ------------------------------------------------------------------- индексы
def _index_series(sec, field, series_id, unit, default_start, drop_zero=False,
                  start=None, end=None, bootstrap=False):
    sid = series_id
    frm = start or incremental_start(sid, RETRO_DAYS, default_start, bootstrap)
    till = end or dates.fmt_date(dates.today_msk())
    url, cols, rows = _history(
        f"history/engines/stock/markets/index/securities/{sec}",
        {"from": frm, "till": till},
        columns=["TRADEDATE", "CLOSE", "VALUE", "YIELD"])
    idx = _colmap(cols)
    points = {}
    for row in rows:
        day = row[idx["TRADEDATE"]] if "TRADEDATE" in idx else None
        value = _num(row, idx, field)
        if not day or value is None:
            continue
        # У ценовых индексов YIELD приходит нулём-заглушкой: для облигационного
        # индекса доходность 0 невозможна, значит это «нет данных», а не значение.
        if drop_zero and value == 0.0:
            continue
        points[str(day)] = value
    if not points and empty_is_fatal(sid):
        raise FetchError(f"ISS: пусто по {sec}.{field} за {frm}..{till}", url=url)
    meta = make_meta("iss", url, points, unit=unit, rows=len(rows), secid=sec, field=field)
    return sid, points, meta


def index(sec="IMOEX", series_id=None, start=None, end=None, bootstrap=False):
    """Дневные закрытия индекса (imoex, rgbi, rvi, mcftr, mcxsm, rtsi, rusfar3m…)."""
    return _index_series(sec, "CLOSE", series_id or sec.lower(), "points",
                         "1997-01-01", start=start, end=end, bootstrap=bootstrap)


def index_value(sec="IMOEX", series_id=None, start=None, end=None, bootstrap=False):
    """Дневной оборот по индексу (registry: imoex_value)."""
    return _index_series(sec, "VALUE", series_id or f"{sec.lower()}_value", "rub",
                         "1997-01-01", start=start, end=end, bootstrap=bootstrap)


def index_yield(sec="RUCBHYCP", series_id=None, start=None, end=None, bootstrap=False):
    """Доходность облигационного индекса, % годовых (registry: *_yield)."""
    return _index_series(sec, "YIELD", series_id or f"{sec.lower()}_yield", "pct",
                         "2003-01-01", drop_zero=True, start=start, end=end,
                         bootstrap=bootstrap)


def selt(sec="CNYRUB_TOM", series_id=None, start=None, end=None, bootstrap=False):
    """Валюта/золото на бирже (CETS). CLOSE, при пустом — WAPRICE."""
    sid = series_id or SELT_IDS.get(sec.upper(), sec.lower())
    frm = start or incremental_start(sid, RETRO_DAYS, "2010-01-01", bootstrap)
    till = end or dates.fmt_date(dates.today_msk())
    url, cols, rows = _history(
        f"history/engines/currency/markets/selt/boards/CETS/securities/{sec}",
        {"from": frm, "till": till},
        columns=["TRADEDATE", "CLOSE", "WAPRICE"])
    idx = _colmap(cols)
    points = {}
    for row in rows:
        day = row[idx["TRADEDATE"]] if "TRADEDATE" in idx else None
        # В неполные дни CLOSE бывает пустым, а средневзвешенная цена есть.
        value = _num(row, idx, "CLOSE")
        if value is None:
            value = _num(row, idx, "WAPRICE")
        if day and value is not None:
            points[str(day)] = value
    if not points and empty_is_fatal(sid):
        raise FetchError(f"ISS: пусто по {sec} за {frm}..{till}", url=url)
    return sid, points, make_meta("iss", url, points, unit="rub", rows=len(rows), secid=sec)


# ------------------------------------------------------------------ КБД (zcyc)
def zcyc(start=None, end=None, max_days=None, bootstrap=False):
    """Кривая бескупонной доходности: 5 рядов (0.5/1/2/5/10 лет), % годовых.

    Эндпоинт даёт срез на ОДНУ дату, поэтому историю собираем по дню. Даты, где
    ISS подсунул более ранний tradedate, отбрасываем — иначе значение одного дня
    размажется по всем последующим выходным.
    """
    ids = [sid for _, sid in ZCYC_SERIES]
    known = [store.last_date(sid) for sid in ids]
    have = [d for d in known if d]
    # старт по САМОМУ отстающему сроку: иначе ряд, который однажды не долился,
    # так и останется дырявым.
    if start:
        frm = dates.parse_date(start)
    elif len(have) == len(ids) and not bootstrap:
        frm = dates.add_days(min(have), -RETRO_DAYS)
    else:
        frm = dates.parse_date("2015-01-05")  # глубина панели валидации
    till = dates.parse_date(end) if end else dates.today_msk()

    days = [d for d in dates.iter_trading_days(frm, till)]
    if max_days:
        days = days[-int(max_days):]
    per_tenor = {sid: {} for sid in ids}
    url = None
    failed = 0
    for n, day in enumerate(days, 1):
        key = dates.fmt_date(day)
        url = _url("engines/stock/zcyc", {"date": key})
        try:
            payload = http.get_json(url)
        except FetchError as e:
            failed += 1
            http.LOG(f"zcyc {key}: {e}")
            continue
        block = payload.get("yearyields") or {}
        cols, data = list(block.get("columns") or []), block.get("data") or []
        if not data:
            continue
        idx = _colmap(cols)
        for row in data:
            got_day = str(row[idx["tradedate"]]) if "tradedate" in idx else key
            if got_day != key:
                continue  # ISS вернул ближайший более ранний срез — не наш день
            period = _num(row, idx, "period")
            value = _num(row, idx, "value")
            if period is None or value is None:
                continue
            for tenor, sid in ZCYC_SERIES:
                if abs(period - tenor) < 1e-6:
                    per_tenor[sid][key] = value
        if n % 200 == 0:
            http.LOG(f"zcyc: {n}/{len(days)} дней")

    filled = sum(len(v) for v in per_tenor.values())
    if not filled and any(empty_is_fatal(sid) for sid in ids):
        raise FetchError(f"ISS: КБД пуста за {dates.fmt_date(frm)}..{dates.fmt_date(till)}",
                         url=url)
    note = f"дней опрошено {len(days)}, отказов {failed}" if failed else None
    out = []
    for tenor, sid in ZCYC_SERIES:
        points = per_tenor[sid]
        out.append((sid, points, make_meta("iss", url, points, unit="pct", note=note,
                                           tenor=tenor)))
    return out


# ------------------------------------------------- открытые позиции физлиц (futoi)
def futoi(ticker="MX", series_prefix=None, start=None, end=None, chunk_days=3,
          bootstrap=False):
    """Открытые позиции по группам клиентов (FIZ/YUR) во фьючерсе на индекс.

    Берём ПОСЛЕДНЮЮ запись дня (внутри дня идут пятиминутные срезы) — это и есть
    позиция на конец сессии. Порядок — по seqnum, при его отсутствии по tradetime:
    схема блока плавала и раньше.
    """
    prefix = series_prefix or f"futoi_{ticker.lower()}"
    ids = {(grp, field): "_".join(p for p in (prefix, gsuf, fsuf) if p)
           for grp, gsuf in FUTOI_GROUPS.items()
           for field, fsuf in FUTOI_FIELDS.items()}
    anchor = f"{prefix}_pos"
    frm = dates.parse_date(start) if start else dates.parse_date(
        incremental_start(anchor, RETRO_DAYS, "2020-06-01", bootstrap))
    till = dates.parse_date(end) if end else dates.today_msk()

    best = {}   # (date, clgroup) -> (ключ сортировки, строка, idx)
    url = None
    day = frm
    while day <= till:
        chunk_end = min(dates.add_days(day, max(1, chunk_days) - 1), till)
        rows, cols, url = _futoi_window(ticker, day, chunk_end)
        if len(rows) >= FUTOI_ROW_CAP:
            # Ответ обрезан по 1000 строк — окно надо сузить до одного дня,
            # иначе тихо потеряем начало периода.
            rows, cols = [], []
            for one in dates.iter_days(day, chunk_end):
                r, c, url = _futoi_window(ticker, one, one)
                rows.extend(r)
                cols = cols or c
        _futoi_absorb(rows, cols, best)
        day = dates.add_days(chunk_end, 1)

    points = {sid: {} for sid in ids.values()}
    for (tradedate, grp), (_, row, idx) in best.items():
        for field in FUTOI_FIELDS:
            sid = ids.get((grp, field))
            value = _num(row, idx, field)
            if sid and value is not None:
                points[sid][tradedate] = value

    filled = sum(len(v) for v in points.values())
    if not filled and any(empty_is_fatal(sid) for sid in ids.values()):
        raise FetchError(f"ISS: futoi {ticker} пуст за {dates.fmt_date(frm)}..{dates.fmt_date(till)}",
                         url=url)
    # Бесплатный ISS отдаёт futoi с задержкой ~14 дней (см. registry) — «нет свежего»
    # это не отказ источника, а его нормальный режим.
    note = "бесплатный ISS публикует с задержкой ~14 дней"
    # holders_* — число ЛИЦ, остальное — контракты: разные единицы в одном наборе
    # рядов, и путать их нельзя (из них считается средний размер позиции).
    return [(sid, points[sid],
             make_meta("iss", url, points[sid], note=note, ticker=ticker,
                       unit="persons" if "holders" in sid else "contracts"))
            for sid in sorted(points)]


def _futoi_window(ticker, day_from, day_till):
    url = _url(f"analyticalproducts/futoi/securities/{ticker}",
               {"from": dates.fmt_date(day_from), "till": dates.fmt_date(day_till)})
    try:
        payload = http.get_json(url)
    except FetchError as e:
        http.LOG(f"futoi {ticker} {day_from}..{day_till}: {e}")
        return [], [], url
    block = payload.get("futoi") or {}
    return (block.get("data") or []), list(block.get("columns") or []), url


def _futoi_absorb(rows, cols, best):
    if not rows or not cols:
        return
    idx = _colmap(cols)
    for row in rows:
        pos_date, pos_grp = idx.get("tradedate"), idx.get("clgroup")
        if pos_date is None or pos_grp is None:
            continue
        tradedate, grp = str(row[pos_date]), str(row[pos_grp])
        if grp not in FUTOI_GROUPS:
            continue
        order = _num(row, idx, "seqnum")
        if order is None:
            pos_time = idx.get("tradetime", idx.get("systime"))
            order = str(row[pos_time]) if pos_time is not None else ""
        key = (tradedate, grp)
        prev = best.get(key)
        if prev is None or _cmp_key(order) > _cmp_key(prev[0]):
            best[key] = (order, row, idx)


def _cmp_key(value):
    """Числа и времена в одном сравнении: seqnum есть не всегда."""
    return (0, float(value), "") if isinstance(value, (int, float)) else (1, 0.0, str(value))


# --------------------------------------------------------------- ширина рынка
def breadth(tickers=None, start=None, end=None, bootstrap=False):
    """Доля ликвидных бумаг выше своей 200-дневной, 0..1 + сырые цены отдельными рядами.

    Возвращает [("breadth", …)] + по ряду px_<тикер> на бумагу. Сырьё держим в
    сторе, чтобы не тянуть 45 историй по 12 лет на каждом прогоне: для MA200
    нужна история, а из агрегата её не восстановить.

    Считаем как в validation/long_panel.py: MA200 по СОБСТВЕННЫМ наблюдениям
    бумаги (min 150), агрегат только по дням, где посчиталось минимум по 15 бумагам.
    """
    names = list(tickers or BREADTH_TICKERS)
    till = end or dates.fmt_date(dates.today_msk())
    merged, fresh, urls, missing = {}, {}, {}, []
    for ticker in names:
        sid = f"px_{ticker.lower()}"
        frm = start or incremental_start(sid, RETRO_DAYS, "2014-01-01", bootstrap)
        url = None
        try:
            url, cols, rows = _history(
                f"history/engines/stock/markets/shares/boards/TQBR/securities/{ticker}",
                {"from": frm, "till": till},
                columns=["TRADEDATE", "CLOSE", "LEGALCLOSEPRICE"])
        except FetchError as e:
            http.LOG(f"breadth {ticker}: {e}")
            cols, rows = [], []
        urls[ticker] = url
        idx = _colmap(cols)
        new = {}
        for row in rows:
            day = row[idx["TRADEDATE"]] if "TRADEDATE" in idx else None
            # CLOSE пуст в дни без сделок в основном режиме — тогда цена закрытия
            # берётся из LEGALCLOSEPRICE (так же делал загрузчик валидации).
            value = _num(row, idx, "CLOSE")
            if value is None:
                value = _num(row, idx, "LEGALCLOSEPRICE")
            if day and value is not None:
                new[str(day)] = value
        stored = (store.load_series(sid) or {}).get("points") or {}
        history = {k: v for k, v in stored.items() if v is not None}
        history.update(new)
        if history:
            merged[ticker] = history
        else:
            # Пустая история без отказа HTTP — это делистинг/переименование
            # (YNDX→YDEX, FIVE→X5, TCSG→T). Молча выпасть из состава ширина рынка
            # не должна: иначе однажды окажется, что «45 бумаг» — это 12.
            missing.append(ticker)
        if new:
            fresh[ticker] = new

    if len(merged) < BREADTH_MIN_TICKERS:
        raise FetchError(f"ISS: ширина рынка — данных всего по {len(merged)} бумагам",
                         url=urls.get(names[0]))

    agg = _pct_above_ma(merged)
    note = f"нет данных по {len(missing)}: {','.join(missing)}" if missing else None
    # В meta агрегата кладём шаблон запроса, а не URL последней бумаги: иначе
    # источник ряда читается как «ширина рынка = BSPB».
    agg_url = f"{ISS}/history/engines/stock/markets/shares/boards/TQBR/securities/{{ticker}}.json"
    out = [("breadth", agg, make_meta("iss", agg_url, agg, unit="share", note=note,
                                      tickers=len(merged)))]
    for ticker, points in fresh.items():
        sid = f"px_{ticker.lower()}"
        out.append((sid, points, make_meta("iss", urls.get(ticker), points, unit="rub",
                                           secid=ticker, note="сырьё для breadth")))
    return out


def _pct_above_ma(px_by_ticker, window=BREADTH_MA, min_obs=BREADTH_MIN_OBS,
                  min_tickers=BREADTH_MIN_TICKERS):
    tally = {}  # дата -> [сколько выше MA, сколько посчиталось]
    for series in px_by_ticker.values():
        days = sorted(series)
        values = [series[d] for d in days]
        running = 0.0
        for i, day in enumerate(days):
            running += values[i]
            if i >= window:
                running -= values[i - window]
            count = min(i + 1, window)
            if count < min_obs:
                continue
            slot = tally.setdefault(day, [0, 0])
            slot[1] += 1
            if values[i] > running / count:
                slot[0] += 1
    return {day: above / total for day, (above, total) in tally.items()
            if total >= min_tickers}


# ------------------------------------------------------------------- интрадей
def intraday_quote(secs=("IMOEX",), ids=None):
    """Текущие значения для интрадей-прогона: точка на СЕГОДНЯ в тот же ряд.

    Бесплатный ISS отдаёт котировки с задержкой ~15 минут — это норма, но она
    отражена в meta (delay_min), чтобы фронт не выдавал задержку за свежесть.
    Дневной прогон потом перезапишет эту точку официальным закрытием.
    """
    mapping = ids or {}
    out, errors, url = [], [], None
    for sec in secs:
        sid = mapping.get(sec) or SELT_IDS.get(sec.upper()) or sec.lower()
        is_fx = _is_selt(sec)
        path = (f"engines/currency/markets/selt/boards/CETS/securities/{sec}" if is_fx
                else f"engines/stock/markets/index/securities/{sec}")
        url = _url(path, {"iss.only": "marketdata"})
        try:
            payload = http.get_json(url)
        except FetchError as e:
            errors.append(f"{sec}: {e}")
            continue
        block = payload.get("marketdata") or {}
        idx = _colmap(list(block.get("columns") or []))
        row = next((r for r in (block.get("data") or []) if _quote_value(r, idx, is_fx)), None)
        value = _quote_value(row, idx, is_fx) if row else None
        if value is None:
            errors.append(f"{sec}: пустой marketdata")
            continue
        day = _quote_date(row, idx)
        meta = make_meta("iss", url, {day: value}, asof=day, delay_min=15, intraday=True,
                         unit="rub" if is_fx else "points",
                         secid=sec, note="ISS отдаёт бесплатно с задержкой ~15 минут",
                         updatetime=_cell(row, idx, "UPDATETIME") or _cell(row, idx, "SYSTIME"))
        out.append((sid, {day: value}, meta))
    if not out:
        raise FetchError("ISS: интрадей-котировки не получены: " + "; ".join(errors), url=url)
    return out


def _is_selt(sec):
    """CNYRUB_TOM/GLDRUB_TOM живут на валютном рынке, индексы — на фондовом."""
    name = sec.upper()
    return name.endswith(("_TOM", "_TOD", "_TMS")) or "RUB" in name


def _quote_value(row, idx, is_fx):
    if not row:
        return None
    if is_fx:
        for name in ("LAST", "MARKETPRICE", "WAPRICE", "LCURRENTPRICE"):
            value = _num(row, idx, name)
            if value:
                return value
        return None
    # У индексов LASTVALUE = закрытие предыдущего дня, живое значение — CURRENTVALUE.
    for name in ("CURRENTVALUE", "LASTVALUE"):
        value = _num(row, idx, name)
        if value:
            return value
    return None


def _quote_date(row, idx):
    """Какой датой класть живую котировку. Дату берём у биржи, а не у себя:
    после полуночи МСК и в вечернюю сессию «сегодня» у нас и у ISS разные."""
    for name in ("TRADEDATE", "TRADE_SESSION_DATE", "SYSTIME"):
        raw = _cell(row, idx, name)
        if not raw:
            continue
        try:
            return dates.fmt_date(str(raw)[:10])
        except ValueError:
            continue
    return dates.fmt_date(dates.today_msk())


def _cell(row, idx, name):
    pos = idx.get(name)
    return row[pos] if pos is not None and pos < len(row) else None


# ---------------------------------------------------------------- фьючерс Brent
def futures_br(series_id="brent_moex", assetcode="BR", start=None, end=None,
               bootstrap=False):
    """Дневные закрытия БЛИЖАЙШЕГО фьючерса BR (интрадей-прокси для Brent).

    Ряд склеен из разных контрактов, поэтому тянем только окно, в котором текущий
    контракт и есть ближайший (месяц до его экспирации): иначе на перекате история
    прошлого контракта переписалась бы ценами следующего (контанго ~1–2%).
    Настоящая история Brent — external.brent_fred; здесь свежесть, а не история.
    """
    secid, expiry = _front_contract(assetcode)
    window_start = dates.add_days(expiry, -31)
    frm = dates.parse_date(start) if start else dates.parse_date(
        incremental_start(series_id, RETRO_DAYS,
                          dates.fmt_date(dates.add_days(dates.today_msk(), -120)),
                          bootstrap))
    if frm < window_start:
        frm = window_start
    till = dates.parse_date(end) if end else dates.today_msk()

    url, cols, rows = _history(
        f"history/engines/futures/markets/forts/securities/{secid}",
        {"from": dates.fmt_date(frm), "till": dates.fmt_date(till)},
        columns=["TRADEDATE", "CLOSE", "SETTLEPRICE"])
    idx = _colmap(cols)
    points = {}
    for row in rows:
        day = row[idx["TRADEDATE"]] if "TRADEDATE" in idx else None
        value = _num(row, idx, "CLOSE")
        if value is None:
            value = _num(row, idx, "SETTLEPRICE")
        if day and value is not None:
            points[str(day)] = value
    if not points and empty_is_fatal(series_id):
        raise FetchError(f"ISS: пусто по фьючерсу {secid}", url=url)
    meta = make_meta("iss", url, points, unit="usd", secid=secid, expiry=dates.fmt_date(expiry),
                     note="склейка ближайших контрактов: на перекате возможен разрыв уровня")
    return series_id, points, meta


def _front_contract(assetcode="BR"):
    """SECID ближайшего непогашенного контракта и его последний торговый день."""
    url = _url("engines/futures/markets/forts/securities",
               {"iss.only": "securities",
                "securities.columns": "SECID,ASSETCODE,LASTTRADEDATE"})
    payload = http.get_json(url)
    block = payload.get("securities") or {}
    idx = _colmap(list(block.get("columns") or []))
    today = dates.fmt_date(dates.today_msk())
    alive = []
    for row in block.get("data") or []:
        if str(_cell(row, idx, "ASSETCODE") or "").upper() != assetcode.upper():
            continue
        last_day = str(_cell(row, idx, "LASTTRADEDATE") or "")
        secid = _cell(row, idx, "SECID")
        if secid and last_day >= today:
            alive.append((last_day, str(secid)))
    if not alive:
        raise FetchError(f"ISS: не нашёл живых контрактов {assetcode}", url=url)
    last_day, secid = min(alive)
    return secid, dates.parse_date(last_day)
