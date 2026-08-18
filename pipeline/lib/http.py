"""HTTP-клиент на стандартной библиотеке: ретраи, троттлинг по хосту, gzip.

Почему свой клиент, а не requests: пайплайн обязан подниматься на голой машине
без venv (docs/CONTRACT.md §0) — и на VPS, и в GitHub Actions, и на ноутбуке.

Грабли соседнего проекта (841, Bybit): лимиты источников считаются НА IP, а весь
пайплайн ходит с одного адреса VPS. Поэтому пауза между запросами держится
глобально на процесс и привязана к ХОСТУ, а не к вызывающему модулю: иначе три
фетчера в одном прогоне дают тройной поток к iss.moex.com и ловят 403/429 на
ровном месте.

Вторые грабли: 4xx (кроме 429) ретраить бессмысленно — неверный тикер не станет
верным с третьей попытки, а три попытки × 45 бумаг = минута впустую на каждом
прогоне. Ретраим только сетевые сбои, таймауты, 429 и 5xx.
"""

import gzip
import json
import os
import random
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from http.client import HTTPException
from urllib.parse import urlsplit

USER_AGENT = "moex-radar/1.0 (dashboard pipeline; python-urllib)"
DEFAULT_TIMEOUT = 30.0
DEFAULT_RETRIES = 3
BACKOFF_BASE = 1.0  # 1с, 2с, 4с — экспонента с джиттером

# Минимальный интервал между запросами к одному хосту, секунды.
# ISS терпит ~8 rps с одного адреса (проверено на истории 2900 дней zcyc),
# сайт ЦБ отдаёт HTML медленно и на частые запросы отвечает 429 — ему пауза больше.
HOST_MIN_INTERVAL = {
    "iss.moex.com": 0.12,
    "www.cbr.ru": 0.7,
    "cbr.ru": 0.7,
    "fred.stlouisfed.org": 0.5,
    # У T-Invest лимит 200 унарных запросов в минуту, а календарь опрашивает
    # три десятка бумаг подряд: дефолтная четверть секунды тут только тормозит.
    "invest-public-api.tinkoff.ru": 0.1,
}
DEFAULT_MIN_INTERVAL = 0.25

# Хосты, которым нужен ДОПОЛНИТЕЛЬНЫЙ корень доверия, и файл с ним (в lib/ca/).
#
# rosstat.gov.ru выдан УЦ Минцифры и не отдаёт промежуточный сертификат в
# рукопожатии: на машине без этого якоря запрос падает с CERTIFICATE_VERIFY_FAILED,
# и ряд ИПЦ молча уезжает на зеркало inflation-monitor.ru (у которого первая
# колонка — прогноз чужой модели, docs/SOURCES.md §2.5). Именно так и было на
# проде с самой установки — с ноутбука Росстат открывался, поэтому провал не
# видели.
#
# Доверие точечное и ДОПОЛНИТЕЛЬНОЕ: системные корни остаются, чужой УЦ действует
# только для перечисленных хостов и только внутри процесса конвейера. Класть его в
# системное хранилище машины нельзя — там он начнёт подтверждать любой домен для
# всех процессов, а машина общая (docs/LATENCY.md §5).
HOST_CA_BUNDLE = {
    "rosstat.gov.ru": "russian_trusted.pem",
    "www.rosstat.gov.ru": "russian_trusted.pem",
    # T-Invest выдан тем же УЦ Минцифры: цепочка *.tinkoff.ru -> Russian Trusted
    # Sub CA -> Russian Trusted Root CA. Без якоря curl отдаёт «код 000», и это
    # легко принять за блокировку по IP — рукопожатие при этом проходит целиком, а
    # падает ПРОВЕРКА (ssl_verify_result=19). С бандлом эндпоинт отвечает 401.
    "invest-public-api.tinkoff.ru": "russian_trusted.pem",
    "invest-public-api.tbank.ru": "russian_trusted.pem",
    "sandbox-invest-public-api.tinkoff.ru": "russian_trusted.pem",
}
CA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ca")

# Хосты с платным доступом: имя переменной окружения, откуда берётся токен.
#
# ALGOPACK (подписка МосБиржи) авторизуется статическим ключом из личного кабинета
# data.moex.com: `Authorization: Bearer <ключ>` к базе https://apim.moex.com/iss.
# Второй способ — логин/пароль через passport.moex.com — нам недоступен в принципе:
# этот хост не отвечает НИ с прод-машины, НИ с ноутбука (замер 12.08.2026, 20 с
# таймаута при живом DNS), тогда как apim.moex.com честно отдаёт 401. То есть выбора
# между способами нет, и хранить пароль от биржевого кабинета не нужно — только ключ.
#
# Пустая или отсутствующая переменная = подписки нет. Это ЗАКОННЫЙ режим: фетчеры
# обязаны работать по бесплатному ISS и лишь помечать, что данные с задержкой.
HOST_AUTH_ENV = {"apim.moex.com": "MOEX_ALGOPACK_TOKEN",
                 "invest-public-api.tinkoff.ru": "TINVEST_TOKEN",
                 "invest-public-api.tbank.ru": "TINVEST_TOKEN"}

_slot_lock = threading.Lock()
_next_slot = {}  # host -> момент (time.monotonic), раньше которого стучаться нельзя
_ctx_lock = threading.Lock()
_ctx_cache = {}  # имя файла -> SSLContext | None (None = бандл не читается)


class FetchError(RuntimeError):
    """Единственное исключение слоя загрузки: провал источника, не провал прогона.

    Вызывающий (run.py) ловит его, ставит тайлу status=stale/error и идёт дальше
    (docs/CONTRACT.md §0, §7).
    """

    def __init__(self, message, url=None, status=None, cause=None):
        super().__init__(message)
        self.url = url
        self.status = status
        self.cause = cause


def _log(message):
    """Хук логирования: агрегатор может подменить http.LOG своей функцией."""
    print(f"[http] {message}", file=sys.stderr)


LOG = _log


def set_min_interval(host, seconds):
    """Подкрутить троттлинг для хоста (например, когда источник начал огрызаться)."""
    HOST_MIN_INTERVAL[host] = float(seconds)


def _reserve(host):
    """Бронирует ближайший разрешённый момент запроса и возвращает, сколько ждать.

    Слот именно бронируется под локом (а не «посмотрели время — поспали»), иначе
    два потока просыпаются одновременно и оба считают, что интервал выдержан.
    """
    interval = HOST_MIN_INTERVAL.get(host, DEFAULT_MIN_INTERVAL)
    with _slot_lock:
        now = time.monotonic()
        slot = max(now, _next_slot.get(host, 0.0))
        _next_slot[host] = slot + interval
    return slot - time.monotonic()


def ssl_context(host):
    """SSLContext с дополнительным корнем для хоста или None (обычный контекст).

    Отказ чтения бандла НЕ валит запрос: без якоря соединение упадёт само и с
    внятной ошибкой TLS, а вот падение на отсутствующем файле выглядело бы как
    поломка всего HTTP-слоя.
    """
    name = HOST_CA_BUNDLE.get(host)
    if not name:
        return None
    with _ctx_lock:
        if name in _ctx_cache:
            return _ctx_cache[name]
        path = os.path.join(CA_DIR, name)
        ctx = None
        try:
            # create_default_context() уже подтягивает СИСТЕМНЫЕ корни, наш файл
            # добавляется к ним, а не заменяет их: если хост однажды переедет на
            # обычный УЦ, проверка продолжит работать.
            ctx = ssl.create_default_context()
            ctx.load_verify_locations(cafile=path)
        except (OSError, ssl.SSLError) as e:
            LOG(f"не прочитан CA-бандл {path}: {type(e).__name__}: {e}")
            ctx = None
        _ctx_cache[name] = ctx
        return ctx


def auth_token(host):
    """Токен подписки для хоста или None. Читается из окружения на КАЖДЫЙ запрос:
    ключ, вписанный в env-файл, начинает работать со следующего такта, без рестарта."""
    name = HOST_AUTH_ENV.get(host)
    if not name:
        return None
    return (os.environ.get(name) or "").strip() or None


def get_bytes(url, timeout=DEFAULT_TIMEOUT, retries=DEFAULT_RETRIES,
              headers=None, accept_gzip=True, data=None):
    """Скачать тело ответа. Кидает FetchError, если не вышло за `retries` попыток.

    `data` (bytes) превращает запрос в POST. Нужен ровно для одного случая: у ЦБ
    ключевая ставка есть в SOAP-сервисе DailyInfo, который отвечает только на POST,
    и его ответ в полсотни раз меньше HTML-страницы с той же таблицей. Ретраить
    такой POST безопасно — это запрос на чтение, а не изменение.
    """
    host = urlsplit(url).netloc
    context = ssl_context(host)
    hdrs = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if accept_gzip:
        hdrs["Accept-Encoding"] = "gzip"
    token = auth_token(host)
    if token:
        # Ставим ДО заголовков вызывающего: фетчер может переопределить нарочно,
        # но случайный `headers=_UA` не должен сбивать авторизацию.
        hdrs["Authorization"] = "Bearer " + token
    if headers:
        hdrs.update(headers)

    last_err = None
    for attempt in range(1, max(1, retries) + 1):
        wait = _reserve(host)
        if wait > 0:
            time.sleep(wait)
        try:
            req = urllib.request.Request(url, headers=hdrs, data=data)
            with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
                raw = resp.read()
                enc = (resp.headers.get("Content-Encoding") or "").lower()
            if "gzip" in enc:
                raw = gzip.decompress(raw)
            return raw
        except urllib.error.HTTPError as e:
            last_err = e
            if 400 <= e.code < 500 and e.code != 429:
                raise FetchError(f"HTTP {e.code} на {url}", url=url, status=e.code,
                                 cause=e) from e
            LOG(f"HTTP {e.code}, попытка {attempt}/{retries}: {url}")
        except (urllib.error.URLError, TimeoutError, ConnectionError, ssl.SSLError,
                HTTPException, gzip.BadGzipFile, OSError) as e:
            last_err = e
            LOG(f"{type(e).__name__}: {e}; попытка {attempt}/{retries}: {url}")
        if attempt < max(1, retries):
            time.sleep(BACKOFF_BASE * (2 ** (attempt - 1)) + random.uniform(0, 0.3))

    status = getattr(last_err, "code", None)
    raise FetchError(f"не удалось скачать {url}: {type(last_err).__name__}: {last_err}",
                     url=url, status=status, cause=last_err)


def get_text(url, encoding="utf-8", **kw):
    """Текст ответа. encoding задаётся явно: XML ЦБ приходит в windows-1251."""
    raw = get_bytes(url, **kw)
    try:
        return raw.decode(encoding, "replace")
    except LookupError as e:  # неизвестная кодировка — это ошибка кода, а не сети
        raise FetchError(f"неизвестная кодировка {encoding!r} для {url}",
                         url=url, cause=e) from e


def _reject_nonfinite(name):
    """NaN/Infinity в ответе источника — отказ, а не число.

    json.loads по умолчанию принимает их (расширение Python), а дальше они
    неотличимы от обычного float: пролезают в стор, в композит и в data.json,
    где json.dumps сериализует NaN литералом — и JSON.parse браузера падает на
    ПЕРВОМ символе, убивая всю витрину разом (publish.dumps закрывает выход тем
    же правилом: allow_nan=False). Ловим на входе, где ещё известно, какой
    источник виноват.
    """
    raise ValueError(f"источник прислал {name} вместо числа")


def get_json(url, encoding="utf-8", **kw):
    """JSON ответа. Битый JSON — тоже отказ источника, а не крах прогона."""
    text = get_text(url, encoding=encoding, **kw)
    try:
        return json.loads(text, parse_constant=_reject_nonfinite)
    except ValueError as e:
        raise FetchError(f"не JSON в ответе {url}: {e}", url=url, cause=e) from e
