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

from pipeline.lib import wording

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

# Метка блока. Якоря начала и конца строки СНЯТЫ намеренно: боевой ответ пришёл
# склеенным — «…построенную на экспортно-импортных равновесиях.===1===\nСтавка…».
# Строгая регулярка такую метку не видела вовсе: разбор возвращал ОДИН блок,
# комментарий второго события утекал хвостом в первое, а само второе оставалось
# без разбора. Формат меток наш собственный, и встретить «===1===» в живом русском
# тексте неоткуда, поэтому терпимость здесь ничего не ломает (в 837 разбор ответа
# терпим по той же причине и с той же оговоркой).
_BLOCK = re.compile(r"={2,}\s*(\d+)\s*={2,}")

# Направленных прогнозов комментатору не разрешено с 03.10.2026: в ленте уже стояли
# «это не разворот тренда, а распродажа» о флажке волатильности, который снялся через
# два дня, — уверенность, которой панель сама себе не позволяет (аудит 03.10.2026, §4).
# Дата и «только из входа» — с 28.10.2026: модель без даты опиралась на свои устаревшие
# знания о «текущих» событиях, а в ленте появлялись участники и пороги, которых не было
# ни во входе, ни на рынке («Управляющая компания перешла на акции», «RVI выше 40»).
SYSTEM = """Ты — независимый аналитик российского рынка. К тебе приходят свежие события с рынка акций и облигаций, и ты объясняешь читателю, что они означают. Читатель умный, но не финансист: он знает, что такое ключевая ставка и дивиденды, но не обязан помнить устройство кривой ОФЗ.

ЯЗЫК ОТВЕТА — ТОЛЬКО РУССКИЙ. Весь текст ответа обязан быть на русском. Общепринятые сокращения (ОФЗ, ЦБ, ВВП, IMOEX) — можно. НИКОГДА не смешивай латиницу и кириллицу внутри одного слова. Десятичный разделитель — запятая.

ДАТА. Сегодняшняя дата по Москве — в поле «сегодня». Даты данных — в самих событиях и в market_now. Не называй событие свежим или давним, не сверившись с этими датами. Твои знания о текущих событиях могли устареть: о решениях, заявлениях и новостях говори только то, что есть во входных данных.

ТВОЯ ЗАДАЧА — осмыслить событие, а не пересказать его:
· что за ним стоит по существу и почему оно могло произойти;
· как это вяжется с соседними величинами из market_now — другой картины текущей обстановки у тебя нет;
· какие сценарии из этого следуют и по каким признакам их можно будет различить.

Ты СВОБОДЕН рассуждать и связывать факты. Рядовое движение назови рядовым одной фразой и не раздувай. Важное — объясни, почему оно важное. Если текст события расходится с числами market_now, опирайся на числа.

«Позиция» в событиях — это правило самой панели (держать акции или деньги), а не сделка какого-то участника рынка. Объясняй, что сдвинуло правило, а не приписывай решение фондам или управляющим.

ЧЕГО ДЕЛАТЬ НЕЛЬЗЯ:
· Утверждать, куда пойдёт рынок, и выдавать догадку о намерениях участников за факт («это не разворот, а распродажа», «участники начали агрессивно закрывать позиции»). Проверка на двадцати годах истории показала: направление акций по таким событиям не угадывается. Говори «может означать», «если …, то …» и называй, что подтвердит или опровергнет версию.
· Пересказывать текст события своими словами — он у читателя уже перед глазами.
· Упоминать панель, дашборд, композит, ядро, ячейку, ворота, слои, мониторы, баллы, оценку панели и прочее устройство — читатель о нём не знает. Говори о рынке: тренде, волатильности, ставке, облигациях.
· Выдумывать числа, пороги, уровни, время, консенсусы и прогнозы, которых тебе не давали. Про ожидания рынка можно говорить только качественно.
· Упоминать решения, заявления и события, которых нет во входных данных.
· Канцелярит, дисклеймеры, «важно отметить», «стоит подчеркнуть», «в заключение».
· Приказы «покупайте / продавайте» — объясняй механику и последствия.

ФОРМАТ: 2–4 предложения живого русского на событие. Термин — с короткой расшифровкой прямо в тексте при первом употреблении.

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
    """Состояние РЫНКА своими именами: цены, ставка, режим словами.

    Ни вердикта, ни баллов, ни имён компонентов: они протекли бы в текст, а читатель
    ленты про устройство панели ничего не знает.
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
    for tile in payload.get("monitors") or []:
        if isinstance(tile, dict) and tile.get("id") == "cb_meeting":
            rate = ((tile.get("payload") or {}).get("key_rate"))
            if isinstance(rate, (int, float)):
                out["ключевая_ставка_%"] = _num(rate)
            break
    return out


# Сколько знаков одного комментария вообще имеет смысл разбирать. Просим три-четыре
# фразы; всё, что длиннее, — уже не комментарий. Ограничение нужно не ради красоты:
# поиск зацикливания ниже квадратичен по длине (замер: 4 тыс. знаков — 0,1 с,
# 8 тыс. — 0,41 с, то есть 100 КБ ответа заняли бы около минуты). Интрадей-такт идёт
# каждые 5 минут и живёт под RuntimeMaxSec — минута в регулярном выражении там лишняя.
_SCAN_LIMIT = 4000

# Письменности, которых в русском разборе быть не может ни при каких терминах
# (арабская, иврит, CJK, кана, хангыль) — перенесено из 837 (`textDefect`).
_FOREIGN_SCRIPT = re.compile("[\u0600-\u06ff\u0590-\u05ff\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")
# Транслит — длинные СТРОЧНЫЕ латинские слова. Аббревиатуры (ОФЗ, RGBI, IMOEX) заглавные,
# имена собственные начинаются с заглавной; одиночный устоявшийся англицизм терпим.
_TRANSLIT = re.compile(r"(?<![A-Za-z])[a-z]{5,}(?![A-Za-z])")
_TRANSLIT_OK = {"spread", "carry", "hedge", "futures", "options", "basis", "funding"}


def _defect(text):
    """Явный брак ответа: пустота, обрубок, зацикливание, разметка вместо текста."""
    t = (text or "").strip()
    if len(t) < 40:
        return "слишком короткий ответ"
    # Все проверки ниже смотрят на голову текста: признаки брака (зацикливание,
    # чужой язык, транслит) равномерны по ответу, а стоимость — нет.
    head = t[:_SCAN_LIMIT]
    if re.search(r"(.{12,}?)\1{2,}", head):
        return "текст зациклился"
    if t.count("```") or t.lstrip().startswith("{"):
        return "вместо текста разметка или JSON"
    if _FOREIGN_SCRIPT.search(head):
        return "чужая письменность"
    cyr = len(re.findall(r"[а-яёА-ЯЁ]", head))
    lat = len(re.findall(r"[a-zA-Z]", head))
    # Промпт требует русского, но требование можно и проигнорировать: англоязычный
    # разбор до читателя доходить не должен.
    if cyr + lat >= 40 and cyr / (cyr + lat) < 0.5:
        return f"ответ не на русском (кириллицы {round(100 * cyr / (cyr + lat))}%)"
    # Латиница и кириллица ВНУТРИ одного слова — «atractтивными», «фondовый». Промпт
    # это запрещает прямым текстом, но первый же боевой ответ такое слово принёс.
    # Порог в два слова намеренный: одна описка в семистах знаках — это опечатка, и
    # выбрасывать из-за неё разумный разбор дороже, чем показать.
    mixed = [w for w in re.findall(r"[^\W\d_]+", head, re.UNICODE)
             if len(re.findall(r"[а-яёА-ЯЁ]", w)) >= 2 and len(re.findall(r"[a-zA-Z]", w)) >= 2]
    if len(mixed) >= 2:
        return "латиница вперемешку с кириллицей в словах: " + ", ".join(mixed[:3])
    translit = [w for w in _TRANSLIT.findall(head) if w not in _TRANSLIT_OK]
    if len(translit) >= 2:
        return "похоже на транслитерацию: " + ", ".join(translit[:3])
    return None


def parse(text, count):
    """Разбор ответа по меткам ===N===. -> список длиной count (None где пусто)."""
    marks = list(_BLOCK.finditer(text or ""))
    if not marks:
        return None
    out = [None] * count
    for i, m in enumerate(marks):
        idx = int(m.group(1))
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        if 0 <= idx < count:
            out[idx] = text[m.end():end].strip() or None
    return out if any(out) else None


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


def ask(events, payload, key, seconds, log=None):
    """Одна попытка. -> (комментарии | None, стоит ли повторять, пауза до повтора)."""
    say = _sayer(log)
    started = time.monotonic()
    name = model()
    try:
        data = _call_with_deadline(request_body(events, payload), key, seconds)
    except ApiError as exc:
        retry = exc.status in RETRY_STATUSES
        say(f"{name}: отказ API {exc.status} {exc.kind} — {exc}"[:300])
        return None, retry, exc.retry_after
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        say(f"{name}: запрос не удался — {type(exc).__name__}: {str(exc)[:160]}")
        return None, True, None
    except Exception as exc:  # noqa: BLE001 — контракт модуля: разбор не роняет прогон
        say(f"{name}: непредвиденный отказ — {type(exc).__name__}: {str(exc)[:160]}")
        return None, False, None
    try:
        if not isinstance(data, dict) or data.get("type") == "error":
            err = (data or {}).get("error") if isinstance(data, dict) else None
            say(f"{name}: тело-ошибка при 200 — {str(err)[:200]}")
            return None, True, None
        usage = data.get("usage") or {}
        usd = cost_usd(usage)
        _add_spend(today_msk(), usd)
        thinking = (usage.get("output_tokens_details") or {}).get("thinking_tokens")
        stop = data.get("stop_reason")
        say(f"{data.get('model') or name}: вход {usage.get('input_tokens')}, выход {usage.get('output_tokens')}"
            + (f" (размышления {thinking})" if thinking is not None else "")
            + f", ${usd:.4f}, {time.monotonic() - started:.1f} с, {stop}")
        if stop == "refusal":
            category = (data.get("stop_details") or {}).get("category")
            say(f"{name}: модель отказалась отвечать (категория {category}) — разбора не будет")
            return None, False, None
        if stop == "max_tokens":
            say(f"{name}: не хватило max_tokens={MAX_TOKENS} — ответ оборван, разбора не будет")
            return None, False, None
        text = _text_of(data)
        parsed = parse(text, len(events))
        if parsed is None:
            say(f"{name}: ответ не разобран ({len(text)} симв.): {text[:200]}")
            return None, False, None
        for i, item in enumerate(parsed):
            if item is None:
                continue
            defect = _defect(item)
            if defect:
                say(f"{name}: комментарий к событию {i} отброшен — {defect}")
                parsed[i] = None
        if not any(parsed):
            return None, False, None
        return parsed, False, None
    except Exception as exc:  # noqa: BLE001 — контракт модуля: разбор не роняет прогон
        say(f"{name}: ответ не разобран — {type(exc).__name__}: {str(exc)[:160]}")
        return None, False, None


def comments(events, payload, log=None):
    """Комментарии к событиям в том же порядке. None — разбора нет (это норма)."""
    say = _sayer(log)
    key = api_key()
    if not key or not events:
        return None
    day = today_msk()
    spent, cap = _spend_today(day), daily_usd()
    if spent >= cap:
        say(f"суточный потолок расходов на разбор исчерпан (${spent:.2f} из ${cap:.2f}) — события уйдут без комментария")
        return None
    budget = budget_s()
    left = deadline_left()
    if left is not None:
        budget = min(budget, left)
    started = time.monotonic()
    for attempt in range(ATTEMPTS):
        remaining = budget - (time.monotonic() - started)
        if remaining < MIN_ATTEMPT_S:
            say(f"на разбор осталось {max(remaining, 0):.0f} с — не начинаю"
                + (" (дедлайн прогона)" if left is not None and left <= budget_s() else ""))
            break
        got, retry, pause = ask(events, payload, key, remaining, log=say)
        if got:
            return got
        if not retry or attempt + 1 >= ATTEMPTS:
            break
        wait = min(pause if pause is not None else 2.0 * (attempt + 1), 20.0)
        if budget - (time.monotonic() - started) - wait < MIN_ATTEMPT_S:
            break
        say(f"повтор через {wait:.0f} с")
        time.sleep(wait)
    say("модель не дала разбора — события уйдут без комментария")
    return None


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
