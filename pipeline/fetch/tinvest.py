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
