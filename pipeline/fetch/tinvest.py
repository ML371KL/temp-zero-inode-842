"""Клиент T-Invest API (Т-Банк): справочник бумаг и дивиденды.

Зачем он тут. Дивидендный календарь — единственный ряд, который до сих пор
заполнялся руками, и рукописный файл оказался не просто протухшим, а местами
неверным (SBER 35,0 ₽ против фактических 37,64; у LKOH запись с прошлогодней
суммой и придуманной датой). Биржевой датасет `securities/{sec}/dividends` мёртв —
у всех проверенных бумаг записи кончаются 2025 годом. T-Invest отдаёт по каждой
бумаге и прошлое, и будущее: дату закрытия реестра, последний день покупки, чистый
дивиденд, доходность и цену закрытия, от которой она посчитана.

ГЛАВНАЯ ГРАБЛЯ, стоившая одного неверного вывода. С прод-машины запрос отвечает
`curl: код 000`, и это ЛЕГКО принять за блокировку по IP — я так и сделал в первом
заходе. На деле TLS-рукопожатие проходит целиком (сервер отдаёт 5,8 КБ и свой
сертификат), а падает ПРОВЕРКА: `ssl_verify_result=19`, self-signed in chain.
Цепочка `*.tinkoff.ru → Russian Trusted Sub CA → Russian Trusted Root CA` — тот же
корень Минцифры, который уже лежит в `pipeline/lib/ca/` ради Росстата. С ним
эндпоинт отвечает 401, то есть достучались и не хватает только токена.
Мораль на будущее: `код=000` — это не диагноз; смотреть надо `ssl_verify_result`.

Токен — из личного кабинета Т-Инвестиций, живёт в окружении как `TINVEST_TOKEN`
(см. `http.HOST_AUTH_ENV`), заголовок ставит HTTP-слой. Без токена модуль молчит:
вызывающий обязан проверить `ready()` и уйти на резервный источник.
"""

import json

from . import FetchError, http

BASE = "https://invest-public-api.tinkoff.ru/rest/tinkoff.public.invest.api.contract.v1"
HOST = "invest-public-api.tinkoff.ru"
BOARD = "TQBR"                     # основной режим торгов акциями МосБиржи
_HEADERS = {"Content-Type": "application/json",
            # Имя приложения биржа просит указывать в запросах API — по нему она
            # разбирает нагрузку. Своё, а не чужое: подписываться клиентом-примером
            # значит мешать чужой статистике и своей же поддержке.
            "x-app-name": "moex-radar"}


def ready():
    """Есть ли токен. Проверяется на каждый вызов: вписанный в env-файл ключ
    начинает работать со следующего такта, без перезапуска сервиса."""
    return bool(http.auth_token(HOST))


def call(service, method, body=None, timeout=25, retries=2):
    """POST к REST-шлюзу. Возвращает разобранный JSON."""
    if not ready():
        raise FetchError("T-Invest: нет токена (env TINVEST_TOKEN)")
    # Точка, а не слэш: у gRPC-шлюза сервис — часть ПОЛНОГО ИМЕНИ пакета
    # (`…contract.v1.InstrumentsService`), а метод уже отделяется слэшем. Со слэшем
    # шлюз отдаёт 404, и это читается как «метода нет», хотя дело в адресе.
    url = "%s.%s/%s" % (BASE, service, method)
    payload = json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
    return http.get_json(url, data=payload, headers=_HEADERS, timeout=timeout,
                         retries=retries)


def quotation(value):
    """{'units': 37, 'nano': 640000000} -> 37.64. None/мусор -> None.

    Единицы и наноединицы приходят СТРОКАМИ у больших чисел (protobuf int64 в JSON),
    поэтому приводим явно. Отрицательные значения имеют знак в обоих полях.
    """
    if not isinstance(value, dict):
        return None
    try:
        units = int(value.get("units") or 0)
        nano = int(value.get("nano") or 0)
    except (TypeError, ValueError):
        return None
    return round(units + nano / 1e9, 9)


def shares(board=BOARD):
    """{тикер: {uid, name, lot, pays_dividends}} по бумагам одного режима.

    Один запрос вместо резолва по тикеру на каждую бумагу: справочник отдаёт ~1900
    инструментов за пару секунд, и из него же берётся флаг `divYieldFlag` — по нему
    отсеиваются бумаги, которые дивидендов не платят вовсе, и к ним не идёт лишний
    запрос.
    """
    data = call("InstrumentsService", "Shares",
                {"instrumentStatus": "INSTRUMENT_STATUS_BASE"}, timeout=60)
    out = {}
    for item in data.get("instruments") or []:
        if board and item.get("classCode") != board:
            continue
        ticker = str(item.get("ticker") or "").upper()
        uid = item.get("uid")
        if ticker and uid:
            out[ticker] = {"uid": uid, "name": item.get("name"),
                           "lot": item.get("lot"),
                           "pays_dividends": bool(item.get("divYieldFlag"))}
    if not out:
        raise FetchError("T-Invest: справочник акций пуст", url=BASE + "/InstrumentsService/Shares")
    return out


# Инструменты витрины котировок: id ряда в сторе -> uid в T-Invest.
#
# Uid'ы разрешены и проверены 12.08.2026 (`InstrumentsService/Indicatives` для
# индексов, `Currencies` для валютной секции) и захардкожены осознанно: это
# постоянные идентификаторы, а разрешать их на каждом пятиминутном такте — два
# лишних запроса ради того, что не меняется. Если T-Invest вернёт по uid пусто,
# витрина уходит на бесплатный ISS целиком (см. run.fetch_live_quotes).
LIVE_UIDS = {
    "live_imoex": "4821c9aa-36e8-4743-b37c-861e58581b25",
    "live_rgbi": "fceffb27-3c51-4101-834c-d28c98ada458",
    "live_rvi": "c83d74aa-4539-4f27-85f0-295511d50d63",
    "live_cny_tom": "4587ab1d-a9c9-4910-a0d6-86c7b9c42510",
    "live_gld_tom": "258e2b93-54e8-4f2d-ba3d-a507c47e3ae2",
}
MSK_OFFSET_HOURS = 3


def last_prices(uids):
    """{uid: (цена, время в UTC)} одним запросом на все инструменты сразу.

    Одним, а не пятью: у бесплатного ISS на каждую бумагу свой вызов, здесь весь
    набор берётся за 0,8 с. И главное — здесь нет пятнадцатиминутной задержки,
    которой биржа накрывает ход торгов инструментами без подписки (замер
    12.08.2026 в 11:10 МСК: у ISS по юаню UPDATETIME=10:55 при SYSTIME=11:10,
    у T-Invest та же бумага — 11:10:46).
    """
    if not uids:
        return {}
    data = call("MarketDataService", "GetLastPrices", {"instrumentId": list(uids)})
    out = {}
    for row in data.get("lastPrices") or []:
        uid = row.get("instrumentUid")
        price = quotation(row.get("price"))
        # Нулевая цена приходит по инструменту, по которому сегодня сделок не было:
        # это НЕ котировка, и подставлять ею живую цену нельзя.
        if uid and price:
            out[uid] = (price, str(row.get("time") or ""))
    return out


def _msk_time(iso):
    """'2026-08-12T08:26:14.123Z' -> '11:26:14' по Москве. Пусто -> None."""
    from datetime import datetime, timedelta, timezone
    try:
        raw = str(iso).replace("Z", "+00:00")
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (stamp.astimezone(timezone.utc) +
            timedelta(hours=MSK_OFFSET_HOURS)).strftime("%H:%M:%S")


def futures_uid(ticker):
    """uid фьючерса по тикеру МосБиржи ('BRU6') или None.

    Через полный справочник фьючерсов (477 инструментов, ~1,5 с), а не через
    `FuturesBy`: тот отвечает 404 на тикер с любым classCode, включая правильный
    SPBFUT (проверено 12.08.2026). Запрос тяжёлый, поэтому вызывается ТОЛЬКО при
    смене переднего контракта — то есть раз в месяц (см. front_futures).
    """
    data = call("InstrumentsService", "Futures",
                {"instrumentStatus": "INSTRUMENT_STATUS_BASE"}, timeout=60)
    want = str(ticker or "").upper()
    for item in data.get("instruments") or []:
        if str(item.get("ticker") or "").upper() == want:
            return item.get("uid")
    return None


def front_futures(store_mod, daily_sid="brent_moex", live_sid="live_brent_moex",
                  today=None):
    """(uid, secid, дата смены контракта) для переднего фьючерса или (None, …).

    Контракт НЕ разрешается заново: его уже нашёл суточный прогон
    (`iss.futures_br` кладёт secid в meta дневного ряда), и в установившемся
    режиме здесь ноль запросов — uid лежит в meta живого ряда с прошлого раза.
    Один запрос случается только на перекате.

    Третье значение — дата, с которой действует НОВЫЙ контракт. Она нужна не для
    красоты: на перекате живая цена нового контракта против вчерашнего закрытия
    старого даёт ложное движение в 1–2% контанго, и «изменение за день» в этот
    день считать нельзя.
    """
    daily = (store_mod.load_series(daily_sid) or {}).get("meta") or {}
    secid = daily.get("secid")
    if not secid:
        return None, None, None
    cached = (store_mod.load_series(live_sid) or {}).get("meta") or {}
    if cached.get("secid") == secid and cached.get("uid"):
        return cached["uid"], secid, cached.get("secid_since")
    uid = futures_uid(secid)
    if not uid:
        http.LOG("T-Invest: фьючерс %s не найден в справочнике" % secid)
        return None, secid, None
    # Дату смены ставим, только если РАНЬШЕ был ДРУГОЙ контракт. При первом
    # включении мы просто впервые узнали текущий — это не перекат, и гасить
    # изменение за день не за что (иначе панель один день молчала бы о движении
    # нефти без всякой причины).
    return uid, secid, (today if cached.get("secid") else None)


def live_quotes(mapping=None, extra_meta=None):
    """[(series_id, {дата: цена}, meta)] — тот же контракт, что у iss.intraday_quote.

    Дата точки — МОСКОВСКИЙ день сделки: витрина живёт по биржевому календарю, а
    после полуночи UTC «сегодня» у нас и у биржи разные.
    """
    from datetime import datetime, timedelta, timezone
    ids = dict(mapping or LIVE_UIDS)
    prices = last_prices(ids.values())
    if not prices:
        raise FetchError("T-Invest: живые котировки не получены", url=BASE)
    out = []
    for sid, uid in sorted(ids.items()):
        got = prices.get(uid)
        if not got:
            continue
        price, when = got
        msk = datetime.now(timezone.utc) + timedelta(hours=MSK_OFFSET_HOURS)
        day = msk.date().isoformat()
        meta = {"source": "tinvest", "url": BASE, "asof": day, "status": "ok",
                "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "intraday": True, "delay_min": 0, "uid": uid,
                "updatetime": _msk_time(when),
                "note": "T-Invest: цена без задержки"}
        meta.update((extra_meta or {}).get(sid) or {})
        out.append((sid, {day: price}, meta))
    if not out:
        raise FetchError("T-Invest: ни одной цены по известным инструментам", url=BASE)
    return out


def dividends(uid, frm, till):
    """[{record_date, last_buy_date, amount_rub, yield_pct, price, payment_date}].

    Даты приходят в RFC3339 — обрезаем до дня: время в них всегда полночь UTC, а
    хранить «2026-07-20T00:00:00Z» в ряду с дневными ключами значит однажды получить
    два разных ключа на один день.
    """
    data = call("InstrumentsService", "GetDividends",
                {"instrumentId": uid, "from": "%sT00:00:00Z" % frm,
                 "to": "%sT00:00:00Z" % till})
    out = []
    for row in data.get("dividends") or []:
        record = str(row.get("recordDate") or "")[:10]
        if not record:
            continue
        out.append({"record_date": record,
                    "last_buy_date": str(row.get("lastBuyDate") or "")[:10] or None,
                    "payment_date": str(row.get("paymentDate") or "")[:10] or None,
                    "declared_date": str(row.get("declaredDate") or "")[:10] or None,
                    "amount_rub": quotation(row.get("dividendNet")),
                    "yield_pct": quotation(row.get("yieldValue")),
                    "price": quotation(row.get("closePrice")),
                    "dividend_type": row.get("dividendType") or None})
    return out
