"""ИИ-комментарий к событиям ленты (docs/CONTRACT.md §6).

Событие само по себе — это факт: «ячейка сменилась», «флаг снят». Комментарий
отвечает на вопрос, которого в факте нет: что за этим стоит и к чему ведёт.
Тот же приём, что в 837/838, и намеренно в том же виде («💬 …» отдельным абзацем),
чтобы в общей ленте хаба разборы всех панелей выглядели одинаково.

МОДЕЛЬ — Claude Sonnet 5.5 через Messages API Anthropic (решение владельца 28.10.2026).
До этого здесь была цепочка бесплатных моделей OpenRouter, и к концу октября она
держалась на одной: запасная GPT-OSS исчезла из каталога (404 на каждом обращении),
Gemma отвечала 429 почти всегда, последний рубеж Poolside снимали 31.10, а в живой ленте
стояли выдуманные числа, прогнозы и ломаные слова (аудит 28.10.2026). Платная модель
стоит копейки при наших объёмах: замер 28.10 — 0,014–0,032 $ за разбор на `xhigh`.

ПОЧЕМУ ПРЯМОЙ HTTP, а не SDK: конвейер живёт на стандартной библиотеке без venv
(контракт §0, сторож в CI), и владелец 28.10.2026 выбрал сохранить это правило.
Запрос собран по документации Messages API: `x-api-key`, `anthropic-version`, тело с
`thinking`, `output_config.effort` и серверным резервом `fallbacks`. Повторы на 429/5xx/529
сделаны здесь же, с уважением `retry-after`.

ПОЧЕМУ ЕСТЬ ПОТОЛОК В ДОЛЛАРАХ: модель платная, а события рождаются кодом. Ошибка в
правиле или петля прогонов не должна молча жечь кредиты — суточный потолок
`LLM_DAILY_USD` останавливает разбор, а не доставку.

ПРОВЕРКА СМЫСЛА — pipeline/lib/llm_guard.py (с 29.10.2026, общий с соседней панелью):
числа своего события, прогнозы, внутренние слова, язык, предел 600 знаков и 4 фраз,
метки ===N===. Фраза с нарушением вырезается, разбор без двух годных фраз — отбрасывается;
вырезанное пишется в журнал. Итог обращения ложится в недельный учёт (llm_stats.json), а
ops_tick() каждого прогона шлёт строку недели и тревогу об отказе API по доступу.

ПОЧЕМУ НИКОГДА НЕ БРОСАЕТ: комментарий — украшение события, а не само событие. Нет
ключа, лежит провайдер, ответ не разобрался, потолок исчерпан — уходит голый факт, и
это нормальный режим, а не отказ.
"""

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from pipeline.lib import llm_guard, wording

ENDPOINT = "https://api.anthropic.com/v1/messages"
API_VERSION = "2026-10-21"
# Серверный резерв на случай отказа модели по политике: запрос повторяется другой
# моделью внутри того же вызова. Форма "default" идёт только с этим заголовком.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MODEL_DEFAULT = "claude-sonnet-5-5"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
EFFORT_DEFAULT = "xhigh"
# Размышления считаются в max_tokens вместе с ответом. Замер 28.10: на `xhigh` весь
# ответ на два события — около 3 тыс. токенов; 16 тыс. — запас без риска обрыва.
MAX_TOKENS = 16000
# Цены Claude Sonnet 5.5, $ за миллион токенов: вход, выход, чтение и запись кэша.
PRICE_PER_MTOK = {"input": 2.0, "output": 10.0, "cache_read": 0.2, "cache_write": 2.5}

# ОБЩИЙ БЮДЖЕТ ВРЕМЕНИ на разбор, включая повторы. Комментатор зовётся ДО публикации
# (alerts.run), поэтому медленный ответ не имеет права съесть дедлайн прогона: витрина
# не должна стоять из-за украшения к событию. Обвязка выставляет LLM_DEADLINE_AT —
# момент, к которому разбор обязан закончиться; бюджет — меньшее из двух.
# Бюджет читается ЛЕНИВО и не бросает: на уровне модуля `float("абв")` — это ValueError
# в момент import, то есть опечатка в окружении роняла бы весь прогон конвейера.
BUDGET_DEFAULT_S = 120.0
# Меньше этого запрос начинать нет смысла: ответ на `xhigh` приходит за 10–25 с.
MIN_ATTEMPT_S = 15.0
ATTEMPTS = 3                     # первая попытка и до двух повторов
RETRY_STATUSES = {408, 409, 429, 500, 502, 503, 504, 529}
DAILY_USD_DEFAULT = 1.0
SPEND_FILE = "llm_spend.json"
# Недельный учёт разборов и отказов API — llm_guard.record / llm_guard.ops_tick.
STATS_FILE = "llm_stats.json"
MSK = timezone(timedelta(hours=3))


def _float_env(name, default):
    try:
        value = float((os.environ.get(name) or "").strip() or default)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def budget_s():
    """Сколько секунд суммарно тратим на разбор."""
    return _float_env("LLM_BUDGET_S", BUDGET_DEFAULT_S)


def daily_usd():
    """Суточный потолок расходов на разбор, $."""
    return _float_env("LLM_DAILY_USD", DAILY_USD_DEFAULT)


def _now():
    """Часы модуля одной точкой: тесты подменяют их, а не читают текущее время."""
    return datetime.now(timezone.utc)


def _epoch():
    return time.time()


def deadline_left():
    """Сколько секунд осталось до LLM_DEADLINE_AT; None — обвязка срока не ставила."""
    raw = (os.environ.get("LLM_DEADLINE_AT") or "").strip()
    if not raw:
        return None
    try:
        return float(raw) - _epoch()
    except ValueError:
        return None


def model():
    return (os.environ.get("MOEX_LLM_MODEL") or "").strip() or MODEL_DEFAULT


def effort():
    value = (os.environ.get("LLM_EFFORT") or "").strip().lower()
    return value if value in EFFORTS else EFFORT_DEFAULT


# Тот же урок, что в lib/nexus.py: дефолтный «Python-urllib/3.x» ловит 403 на
# защите от ботов, а причина в коде ниоткуда не видна.
USER_AGENT = "moex-radar/1.0 (dashboard pipeline; +https://github.com/ML371KL/temp-zero-inode-842)"

# Имя панели в ops-сообщениях учёта разборов и адрес, куда смотреть при отказе API.
PANEL = "Рынок РФ"
OPS_WHERE = "ключ — ANTHROPIC_API_KEY в /usr/local/etc/moex-radar/env."
# Прогнозы в ответе ловит словарь llm_guard. Здесь — вне «если …, то …»: сценарий с
# условием и признаком и есть то, о чём просит промпт. У 843 запрет жёсткий.
FORECAST_STRICT = False

# Направленных прогнозов комментатору не разрешено с 03.10.2026: в ленте уже стояли
# «это не разворот тренда, а распродажа» о флажке волатильности, который снялся через
# два дня, — уверенность, которой панель сама себе не позволяет (аудит 03.10.2026, §4).
# Дата и «только из входа» — с 28.10.2026: модель без даты опиралась на свои устаревшие
# знания о «текущих» событиях, а в ленте появлялись участники и пороги, которых не было
# ни во входе, ни на рынке («Управляющая компания перешла на акции», «RVI выше 40»).
SYSTEM = """Ты — аналитик российского рынка акций и облигаций. К тебе приходят события с рынка. Строку факта события (заголовок, «было → стало», подробность и «что это значит») читатель уже видит, твой абзац стоит под ней. Читатель умный, но не финансист: знает, что такое ключевая ставка и дивиденды, но не обязан помнить устройство кривой ОФЗ.

ЗАДАЧА — чтобы читатель, увидевший только это сообщение, узнал то, чего в строке факта нет, и понял, что из этого следует. Простыми словами, в таком порядке:
· Что это значит — одной фразой, без жаргона. Термин при первом упоминании — с короткой расшифровкой (RGBI — индекс гособлигаций, RVI — индекс ожидаемой волатильности акций).
· Почему это важно — механика и контекст из входа: насколько движение необычно на фоне соседних величин market_now (насколько далеко до порогов, ставка, вклады против дивидендов, ставка денежного рынка, недельная инфляция), что в market_now картину подтверждает, а что ей противоречит. Рядовое движение назови рядовым и не раздувай.
· На что смотреть дальше — условно, «если …, то …»: какие признаки из тех, что видны во входе, подтвердят картину, а какие опровергнут.

ЧЕГО НЕ НАДО:
· Пересказывать строку факта или определение показателя, повторять её числа без вывода.
· Общих слов: «важно для инвесторов», «рынок будет следить», «стоит отметить», «важно подчеркнуть»; канцелярита и дисклеймеров.
· Утверждать, куда пойдёт рынок, обещать движение цены, выдавать догадку о намерениях участников за факт. Проверка на двадцати годах истории показала: направление акций по таким событиям не угадывается. Сценарий — только с условием и признаком: «если …, то …».
· Приказов «покупайте / продавайте» — объясняй механику и последствия.
· «Позиция» в событиях — правило самой панели (держать акции или деньги), а не сделка участника рынка. Объясняй, что сдвинуло правило, а не приписывай решение фондам или управляющим.

ОПОРА — ТОЛЬКО ВХОД:
· Сегодняшняя дата по Москве — в поле «сегодня», даты данных — в событиях и в market_now. Не называй событие свежим или давним, не сверившись с датами. Твои знания о текущих событиях могли устареть: о решениях, заявлениях и новостях говори только то, что есть во входе.
· Число бери только из своего события и из market_now: округлять можно, разность или отношение двух данных чисел — тоже («на …», «в … раза»). Год — только из дат входа. Не выдумывай пороги, уровни, время, консенсусы и прогнозы.
· Если текст события расходится с числами market_now, опирайся на числа.

ЯЗЫК — только русский, десятичный разделитель — запятая. Общепринятые сокращения (ОФЗ, ЦБ, ВВП, IMOEX, RGBI, RVI, RUSFAR) — можно. Не смешивай латиницу и кириллицу в одном слове. Не упоминай панель, дашборд, композит, ядро, ячейку, ворота, якоря, веер, детекторы, слои, мониторы, баллы, оценку панели и имена полей входа — читатель о них не знает. Говори о рынке: тренде, волатильности, ставке, облигациях, деньгах.

ФОРМАТ: 2–4 предложения на событие, вместе не длиннее 600 знаков. Первое — что это значит, дальше — почему важно и на что смотреть.

ПРОВЕРКА. Ответ проверяется машинно: предложение с числом не из входа, прогнозом вне «если …» или внутренним словом вырезается, всё сверх 4 предложений и 600 знаков отрезается с конца. Самое важное ставь первым.

ОТВЕТ строго такой структурой, без вступлений и заголовков:
===0===
текст про событие с номером 0
===1===
текст про событие с номером 1"""


def _sayer(log):
    """Логгер, который не может уронить вызывающего.

    ОПЛАЧЕНО ПРОБНЫМ ПРОГОНОМ: print с кириллицей в консоль cp1252 бросает
    UnicodeEncodeError — а это подкласс ValueError, и он приходил ровно в тот
    обработчик, который сам зовёт say(). Второе исключение уже никем не ловилось и
    уносило с собой весь разбор. Модуль обещает не бросать никогда: обещание не
    должно зависеть от того, куда пишет чужой логгер.
    """
    def say(message):
        if log is None:
            return
        try:
            log(message)
        except Exception:  # noqa: BLE001 — диагностика не имеет права быть фатальной
            pass
    return say


def api_key():
    return (os.environ.get("ANTHROPIC_API_KEY") or "").strip() or None


def _num(value, digits=2):
    return None if not isinstance(value, (int, float)) else round(float(value), digits)


def market_now(payload):
    """Состояние РЫНКА своими именами: цены, ставка, режим словами и контекст к ним.

    Ни вердикта, ни баллов, ни имён компонентов: они протекли бы в текст, а читатель
    ленты про устройство панели ничего не знает. С 29.10.2026 — и контекст, без
    которого «почему это важно» модели строить не из чего (единый стандарт сообщений,
    §6): насколько далеко признаки рынка от своих порогов, вклады против дивидендов,
    ставка денежного рынка, недельная инфляция, дни до заседания ЦБ. Всё — числа,
    которые панель уже показывает; новых расчётов здесь нет.
    """
    out = {}
    for sid, row in (payload.get("quotes") or {}).items():
        if not isinstance(row, dict) or row.get("value") is None:
            continue
        out[row.get("label") or sid] = {
            "значение": _num(row.get("value"), 4),
            "за_день_%": _num(row.get("chg_pct")),
            "на_дату": row.get("asof"),
        }
    words = {"trend": {0: "нисходящий тренд", 1: "восходящий тренд"},
             "vol": {0: "волатильность спокойная", 1: "волатильность повышенная"},
             "bond": {0: "рынок ОФЗ спокоен", 1: "рынок ОФЗ в стрессе"}}
    current = (payload.get("states") or {}).get("current") or {}
    regime = [words[k].get(current.get(k)) for k in ("trend", "vol", "bond")]
    regime = [w for w in regime if w]
    if regime:
        out["режим_рынка"] = ", ".join(regime)
    # Где признаки стоят относительно своих порогов: «насколько необычно» без этого
    # модель бралась угадывать и выдумывала пороги («RVI выше 40», аудит 28.10.2026).
    names = {"trend": (("value", "индекс_к_200_дневной_средней_%"),),
             "vol": (("value", "волатильность_индекса_%"),
                     ("threshold", "порог_нервной_торговли_%")),
             "bond": (("value", "RGBI_от_максимума_за_год_%"),
                      ("threshold", "порог_стресса_ОФЗ_%"))}
    for row in (payload.get("states") or {}).get("distances") or []:
        for field, name in names.get((row or {}).get("id"), ()):
            if isinstance(row.get(field), (int, float)):
                out[name] = _num(row[field])
    for tile in payload.get("monitors") or []:
        if not isinstance(tile, dict):
            continue
        pl = tile.get("payload") or {}
        if tile.get("id") == "cb_meeting":
            if isinstance(pl.get("key_rate"), (int, float)):
                out["ключевая_ставка_%"] = _num(pl["key_rate"])
            if isinstance(pl.get("rusfar3m"), (int, float)):
                out["ставка_денежного_рынка_RUSFAR_3M_%"] = _num(pl["rusfar3m"])
            if pl.get("next_meeting") and isinstance(pl.get("days_left"), int):
                out["следующее_заседание_ЦБ"] = pl["next_meeting"]
                out["дней_до_заседания_ЦБ"] = pl["days_left"]
            prints = [p for p in pl.get("cpi_weekly_prints") or []
                      if isinstance(p, (list, tuple)) and len(p) == 2
                      and isinstance(p[1], (int, float))]
            if prints:
                out["недельная_инфляция_%"] = [[d, _num(v)] for d, v in prints[-4:]]
        elif tile.get("id") == "deposit_spread":
            if isinstance(pl.get("deposit_pct"), (int, float)):
                out["ставка_вкладов_%"] = _num(pl["deposit_pct"])
            if isinstance(pl.get("dy_trail_pct"), (int, float)):
                out["дивидендная_доходность_акций_%"] = _num(pl["dy_trail_pct"])
    return out


# Проверка ответа — общий модуль llm_guard (тот же код в 842 и 843, тот же алгоритм в
# 837/838): числа своего события, прогнозы, внутренние слова, язык, длина, метки блоков.
# Брак текста (зацикливание, разметка, чужой язык) ищется в голове ответа — первых
# llm_guard.SCAN_LIMIT знаках: поиск повтора квадратичен по длине, а интрадей-такт
# живёт под RuntimeMaxSec.
def parse(text, count):
    """Разбор ответа по меткам ===N===. -> список длиной count (None где пусто) | None."""
    return llm_guard.parse_blocks(text, count)[0]


def _stats_path():
    return os.path.join(os.environ.get("STATE_DIR") or ".state", STATS_FILE)


def _record(**kw):
    """Итог обращения к модели — в недельный учёт. Сбой записи разбор не трогает."""
    path = _stats_path()
    llm_guard.save_stats(path, llm_guard.record(llm_guard.load_stats(path), _now(), **kw))


def ops_tick(send, log=None):
    """Раз в прогон: тревога об отказе API по доступу и строка за прошлую неделю.

    send(text) -> bool — отправка в ops-канал панели. Никогда не бросает.
    """
    say = _sayer(log)
    try:
        path = _stats_path()
        stats = llm_guard.load_stats(path)
        sent = llm_guard.ops_tick(stats, _now(), PANEL, send, OPS_WHERE)
        llm_guard.save_stats(path, stats)
        for item in sent:
            say(f"ops: {item}")
    except Exception as exc:  # noqa: BLE001 — учёт не роняет прогон
        say(f"ops: учёт разборов не сработал — {type(exc).__name__}: {str(exc)[:160]}")


def today_msk(now=None):
    return (now or _now()).astimezone(MSK).strftime("%d.%m.%Y")


def request_body(events, payload):
    """Тело запроса к Messages API. Без temperature: на этой версии API это 400."""
    return {
        "model": model(),
        "max_tokens": MAX_TOKENS,
        "system": SYSTEM,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": effort()},
        "fallbacks": "default",
        "messages": [{"role": "user", "content": json.dumps({
            "сегодня": today_msk(),
            "market_now": market_now(payload),
            # Событие уходит модели РАЗОБРАННЫМ на части — так же, как у 837:
            # отдельно вид, отдельно заголовок, отдельно движение и отдельно
            # смысл. Прежде уезжала одна склеенная строка, и модель тратила
            # первую фразу на её пересказ, хотя пересказ прямо запрещён
            # промптом: ей просто не с чем было работать, кроме этой строки.
            # `kind` даётся человеческим ярлыком, а не служебным ключом.
            "events": [{
                "i": i,
                "тип": (wording.KIND.get(e.get("kind")) or {}).get("label") or e.get("kind"),
                "событие": e.get("title") or e.get("text"),
                "было": e.get("before"),
                "стало": e.get("after"),
                "подробность": e.get("detail"),
                "что_это_значит": e.get("meaning"),
            } for i, e in enumerate(events)],
        }, ensure_ascii=False)}],
    }


class ApiError(Exception):
    """Отказ Messages API: статус, тип ошибки, пауза из retry-after."""

    def __init__(self, status, kind, message, retry_after=None):
        super().__init__(f"{status} {kind}: {message}")
        self.status, self.kind, self.retry_after = status, kind, retry_after
        self.message = message


def _post(body, key, timeout):
    """Один HTTP-запрос. -> разобранный JSON ответа; ApiError на ответ с ошибкой."""
    req = urllib.request.Request(ENDPOINT, data=json.dumps(body).encode("utf-8"), method="POST")
    req.add_header("content-type", "application/json")
    req.add_header("x-api-key", key)
    req.add_header("anthropic-version", API_VERSION)
    req.add_header("anthropic-beta", FALLBACK_BETA)
    req.add_header("user-agent", USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        try:
            err = (json.loads(exc.read().decode("utf-8", "replace")) or {}).get("error") or {}
        except (ValueError, OSError):
            err = {}
        retry_after = None
        try:
            retry_after = float(exc.headers.get("retry-after")) if exc.headers else None
        except (TypeError, ValueError):
            retry_after = None
        raise ApiError(exc.code, err.get("type") or "http_error",
                       str(err.get("message") or exc.reason)[:200], retry_after) from None


def _call_with_deadline(body, key, seconds):
    """Запрос с ЖЁСТКИМ пределом по часам.

    `urlopen(timeout=)` ограничивает простой сокета, а не длительность: так на этой
    панели уже съедались минуты дедлайна. Поэтому запрос идёт в фоновом потоке, а
    ждём его ровно `seconds`; не уложился — бросаем ожидание (поток умрёт с процессом).
    """
    box = {}

    def work():
        try:
            box["data"] = _post(body, key, seconds)
        except BaseException as exc:  # noqa: BLE001 — исключение передаётся вызывающему
            box["error"] = exc

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        raise TimeoutError(f"нет ответа за {seconds:.0f} с")
    if "error" in box:
        raise box["error"]
    return box["data"]


def cost_usd(usage):
    usage = usage or {}
    return (PRICE_PER_MTOK["input"] * (usage.get("input_tokens") or 0)
            + PRICE_PER_MTOK["output"] * (usage.get("output_tokens") or 0)
            + PRICE_PER_MTOK["cache_read"] * (usage.get("cache_read_input_tokens") or 0)
            + PRICE_PER_MTOK["cache_write"] * (usage.get("cache_creation_input_tokens") or 0)) / 1e6


def _spend_path():
    return os.path.join(os.environ.get("STATE_DIR") or ".state", SPEND_FILE)


def _spend_today(day):
    try:
        with open(_spend_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return float(data.get("usd") or 0.0) if data.get("day") == day else 0.0
    except (OSError, ValueError, TypeError, AttributeError):
        return 0.0


def _add_spend(day, usd):
    """Сложить расход дня. Отказ записи не роняет разбор: потолок — страховка, а не учёт."""
    try:
        path = _spend_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        total = _spend_today(day) + usd
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"day": day, "usd": round(total, 6)}, fh)
        os.replace(tmp, path)
    except OSError:
        pass


def _text_of(data):
    return "".join(str(b.get("text") or "") for b in data.get("content") or []
                   if isinstance(b, dict) and b.get("type") == "text").strip()


def ask(events, payload, key, seconds, log=None, trace=None):
    """Одна попытка. -> (комментарии | None, стоит ли повторять, пауза до повтора).

    trace — словарь итога для недельного учёта: цена, вид отказа, отказ по доступу,
    сводка проверки ответа.
    """
    say = _sayer(log)
    trace = trace if trace is not None else {}
    started = time.monotonic()
    name = model()
    body = request_body(events, payload)
    try:
        data = _call_with_deadline(body, key, seconds)
    except ApiError as exc:
        retry = exc.status in RETRY_STATUSES
        say(f"{name}: отказ API {exc.status} {exc.kind} — {exc}"[:300])
        trace["failed"] = f"API {exc.status}"
        if llm_guard.access_problem(exc.status, exc.message):
            trace["access"] = {"status": exc.status, "kind": exc.kind, "message": exc.message}
        return None, retry, exc.retry_after
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        say(f"{name}: запрос не удался — {type(exc).__name__}: {str(exc)[:160]}")
        trace["failed"] = "таймаут" if isinstance(exc, TimeoutError) else "сеть"
        return None, True, None
    except Exception as exc:  # noqa: BLE001 — контракт модуля: разбор не роняет прогон
        say(f"{name}: непредвиденный отказ — {type(exc).__name__}: {str(exc)[:160]}")
        trace["failed"] = "сбой запроса"
        return None, False, None
    try:
        if not isinstance(data, dict) or data.get("type") == "error":
            err = (data or {}).get("error") if isinstance(data, dict) else None
            say(f"{name}: тело-ошибка при 200 — {str(err)[:200]}")
            trace["failed"] = "ошибка API"
            return None, True, None
        usage = data.get("usage") or {}
        usd = cost_usd(usage)
        _add_spend(today_msk(), usd)
        trace["usd"] = trace.get("usd", 0.0) + usd
        trace["ok"] = True
        thinking = (usage.get("output_tokens_details") or {}).get("thinking_tokens")
        stop = data.get("stop_reason")
        say(f"{data.get('model') or name}: вход {usage.get('input_tokens')}, выход {usage.get('output_tokens')}"
            + (f" (размышления {thinking})" if thinking is not None else "")
            + f", ${usd:.4f}, {time.monotonic() - started:.1f} с, {stop}")
        if stop == "refusal":
            category = (data.get("stop_details") or {}).get("category")
            say(f"{name}: модель отказалась отвечать (категория {category}) — разбора не будет")
            trace["failed"] = "отказ модели"
            return None, False, None
        if stop == "max_tokens":
            say(f"{name}: не хватило max_tokens={MAX_TOKENS} — ответ оборван, разбора не будет")
            trace["failed"] = "обрыв ответа"
            return None, False, None
        # Проверка смысла: числа своего события, прогнозы, внутренние слова, язык,
        # длина, метки. Вырезанное и отброшенное — в журнал с причиной и текстом.
        got, summary = llm_guard.review(_text_of(data), body["messages"][0]["content"], len(events),
                                        strict=FORECAST_STRICT, say=lambda m: say(f"{name}: {m}"),
                                        extra=SYSTEM.split("ФОРМАТ:")[0])
        trace["summary"] = summary
        return (got if any(got) else None), False, None
    except Exception as exc:  # noqa: BLE001 — контракт модуля: разбор не роняет прогон
        say(f"{name}: ответ не разобран — {type(exc).__name__}: {str(exc)[:160]}")
        trace["failed"] = "сбой разбора"
        return None, False, None


def comments(events, payload, log=None):
    """Комментарии к событиям в том же порядке. None — разбора нет (это норма)."""
    say = _sayer(log)
    if not events:
        return None
    key = api_key()
    if not key:
        _record(asked=len(events), failed="нет ключа")
        return None
    day = today_msk()
    spent, cap = _spend_today(day), daily_usd()
    if spent >= cap:
        say(f"суточный потолок расходов на разбор исчерпан (${spent:.2f} из ${cap:.2f}) — события уйдут без комментария")
        _record(asked=len(events), failed="потолок $")
        return None
    budget = budget_s()
    left = deadline_left()
    if left is not None:
        budget = min(budget, left)
    started = time.monotonic()
    trace = {}
    got = None
    for attempt in range(ATTEMPTS):
        remaining = budget - (time.monotonic() - started)
        if remaining < MIN_ATTEMPT_S:
            say(f"на разбор осталось {max(remaining, 0):.0f} с — не начинаю"
                + (" (дедлайн прогона)" if left is not None and left <= budget_s() else ""))
            trace.setdefault("failed", "нет времени")
            break
        got, retry, pause = ask(events, payload, key, remaining, log=say, trace=trace)
        if got or not retry or attempt + 1 >= ATTEMPTS:
            break
        wait = min(pause if pause is not None else 2.0 * (attempt + 1), 20.0)
        if budget - (time.monotonic() - started) - wait < MIN_ATTEMPT_S:
            break
        say(f"повтор через {wait:.0f} с")
        time.sleep(wait)
    summary = trace.get("summary")
    _record(asked=len(events), summary=summary,
            failed=None if summary else (trace.get("failed") or "нет ответа"),
            usd=trace.get("usd", 0.0), access=trace.get("access"), ok=trace.get("ok", False))
    if not got:
        say("модель не дала разбора — события уйдут без комментария")
    return got


def annotate(events, payload, log=None):
    """Проставить `comment` рыночным событиям без комментария. Возвращает их число."""
    from pipeline import alerts  # локально: lib не должен зависеть от верхнего слоя

    need = [e for e in events or []
            if not alerts.is_ops(e) and not (e.get("comment") or "").strip()]
    if not need:
        return 0
    got = comments(need, payload, log=log)
    if not got:
        return 0
    filled = 0
    for event, text in zip(need, got):
        if text:
            event["comment"] = text
            filled += 1
    return filled
