"""ИИ-комментарий к событиям ленты (docs/CONTRACT.md §6).

Событие само по себе — это факт: «ячейка сменилась», «флаг снят». Комментарий
отвечает на вопрос, которого в факте нет: что за этим стоит и к чему ведёт.
Тот же приём, что в 837/838, и намеренно в том же виде («💬 …» отдельным абзацем),
чтобы в общей ленте хаба разборы всех панелей выглядели одинаково.

ПОЧЕМУ ЦЕПОЧКА МОДЕЛЕЙ, а не одна: бесплатные модели живут на общих мощностях и
регулярно отвечают «ResourceExhausted» — в 837 это оставляло боевое уведомление без
разбора. Провайдеры в цепочке РАЗНЫЕ: перегрузка NVIDIA не должна выключать разбор
целиком.

ПОЧЕМУ ТОЛЬКО БЕСПЛАТНЫЕ: суффикс `:free` проверяется у ВСЕЙ цепочки, и платная
модель без явного `LLM_ALLOW_PAID=1` в сеть не уходит. Прогонов до сотни в сутки —
одна опечатка в переменной жгла бы деньги молча.

ПОЧЕМУ НИКОГДА НЕ БРОСАЕТ: комментарий — украшение события, а не само событие. Нет
ключа, лежит провайдер, ответ не разобрался — уходит голый факт, и это нормальный
режим, а не отказ.
"""

import json
import os
import re
import time
import urllib.error
import urllib.request

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
TIMEOUT = 90

# ОБЩИЙ БЮДЖЕТ на всю цепочку. `urlopen(timeout=)` ограничивает ПРОСТОЙ сокета, а не
# длительность запроса: замер с прод-ключа дал у основной модели 152 с при таймауте 90.
# Четыре такие попытки подряд — это больше десяти минут, то есть RuntimeMaxSec юнита,
# и прогон умер бы ПО ДОРОГЕ К ПУБЛИКАЦИИ: витрина не обновилась бы из-за украшения
# к событию. Ровно на этих граблях проект уже стоял, когда алерты роняли прогон.
BUDGET = float(os.environ.get("LLM_BUDGET_S") or 240)

FREE_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"
# Порядок — по качеству разбора; провайдеры разные намеренно. Каталог живой: прежний
# «inclusionai/ling-3.0-flash:free» из 837 к августу 2026 отдаёт 404, а пришедший ему
# на смену «ling-3.0-tiny» разметку ответа игнорирует — обе проверены запросом.
FREE_CHAIN = (
    FREE_MODEL,                          # NVIDIA, самая крупная
    "google/gemma-4-31b-it:free",        # Google
    "openai/gpt-oss-20b:free",           # OpenAI OSS
    "poolside/laguna-s-2.1:free",        # Poolside, последний рубеж
)

# Тот же урок, что в lib/nexus.py: дефолтный «Python-urllib/3.x» ловит 403 на
# защите от ботов, а причина в коде ниоткуда не видна.
USER_AGENT = "moex-radar/1.0 (dashboard pipeline; +https://github.com/ML371KL/temp-zero-inode-842)"

_BLOCK = re.compile(r"^[^\S\n]*=+\s*(\d+)\s*=+[^\S\n]*$", re.M)

SYSTEM = """Ты — независимый аналитик российского рынка. К тебе приходят свежие события с рынка акций и облигаций, и ты объясняешь читателю, что они означают. Читатель умный, но не финансист: он знает, что такое ключевая ставка и дивиденды, но не обязан помнить устройство кривой ОФЗ.

ЯЗЫК ОТВЕТА — ТОЛЬКО РУССКИЙ. Думать можешь как угодно, но весь текст ответа обязан быть на русском. Общепринятые сокращения (ОФЗ, ЦБ, ВВП, IMOEX) — можно. НИКОГДА не смешивай латиницу и кириллицу внутри одного слова.

ТВОЯ ЗАДАЧА — осмыслить событие, а не пересказать его:
· что за ним стоит по существу и почему оно могло произойти;
· как это вяжется с тем, что сейчас происходит на рынке — соседние величины переданы в market_now;
· к чему это ведёт дальше и за чем имеет смысл следить.

Ты СВОБОДЕН рассуждать, связывать факты и делать выводы. Рядовое движение назови рядовым одной фразой и не раздувай. Важное — объясни, почему оно важное, и доведи мысль до вывода.

ЧЕГО ДЕЛАТЬ НЕЛЬЗЯ:
· Пересказывать текст события своими словами — он у читателя уже перед глазами.
· Упоминать панель, дашборд, композит, ядро, ячейку, слои, мониторы, баллы и прочее устройство — читатель о нём не знает. Говори о рынке: тренде, волатильности, ставке, облигациях.
· Выдумывать числа, консенсусы и прогнозы, которых тебе не давали. Про ожидания рынка можно говорить только качественно.
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
    return (os.environ.get("OPENROUTER_KEY") or "").strip() or None


def paid_allowed():
    return os.environ.get("LLM_ALLOW_PAID") == "1"


def chain():
    """Явно заданная модель уважается и цепочкой не подменяется."""
    explicit = (os.environ.get("MOEX_LLM_MODEL") or "").strip()
    return [explicit] if explicit else list(FREE_CHAIN)


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


def _defect(text):
    """Явный брак ответа: пустота, обрубок, зацикливание, разметка вместо текста."""
    t = (text or "").strip()
    if len(t) < 40:
        return "слишком короткий ответ"
    if re.search(r"(.{12,}?)\1{2,}", t):
        return "текст зациклился"
    if t.count("```") or t.lstrip().startswith("{"):
        return "вместо текста разметка или JSON"
    cyr = len(re.findall(r"[а-яёА-ЯЁ]", t))
    lat = len(re.findall(r"[a-zA-Z]", t))
    # Промпт требует русского, но требование можно и проигнорировать: англоязычный
    # разбор до читателя доходить не должен.
    if cyr + lat >= 40 and cyr / (cyr + lat) < 0.5:
        return f"ответ не на русском (кириллицы {round(100 * cyr / (cyr + lat))}%)"
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


def _post(model, events, payload, key, with_reasoning=True):
    body = {
        "model": model,
        # Рассуждающая модель тратит на размышления больше, чем на ответ, а лимит
        # обязан покрывать и то, и другое: тесного бюджета хватает ровно на черновик.
        "max_tokens": min(16000, 1500 * len(events) + 4000),
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": json.dumps({
                "market_now": market_now(payload),
                "events": [{"i": i, "kind": e.get("kind"), "text": e.get("text")}
                           for i, e in enumerate(events)],
            }, ensure_ascii=False)},
        ],
    }
    if with_reasoning:
        # Размышления нужны модели, но не нам: просим не возвращать их, чтобы ответ
        # гарантированно лежал в content и его нельзя было спутать с черновиком.
        body["reasoning"] = {"exclude": True}
    req = urllib.request.Request(ENDPOINT, data=json.dumps(body).encode("utf-8"), method="POST")
    req.add_header("content-type", "application/json")
    req.add_header("authorization", f"Bearer {key}")
    req.add_header("user-agent", USER_AGENT)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def ask(model, events, payload, key, log=None):
    """Одна попытка у одной модели -> (комментарии | None, пробовать_следующую)."""
    say = _sayer(log)
    try:
        try:
            data = _post(model, events, payload, key)
        except urllib.error.HTTPError as exc:
            # Подавление размышлений понимает не каждый провайдер, и отказ приходит
            # то HTTP-ошибкой, то телом с error при коде 200. Один повтор без
            # параметра дешевле потери разбора.
            say(f"{model}: запрос с подавлением размышлений отклонён ({exc.code}), повтор без него")
            data = _post(model, events, payload, key, with_reasoning=False)
        if data.get("error"):
            # OpenRouter умеет вернуть 200 с телом-ошибкой и пустым choices — именно
            # так выглядит перегрузка бесплатного провайдера.
            say(f"{model}: провайдер вернул ошибку — {str(data['error'])[:180]}")
            return None, True
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = str(message.get("content") or "").strip()
        thinking = str(message.get("reasoning") or "").strip()
        # ЧЕРНОВИК — НЕ ОТВЕТ. Поток мыслей однажды уехал читателю целиком и
        # по-английски; берём его, только если он СОДЕРЖИТ нашу разметку.
        text = content or (thinking if _BLOCK.search(thinking) else "")
        if not text:
            why = ("не хватило бюджета токенов" if choice.get("finish_reason") == "length"
                   else "вернулись только черновые размышления" if thinking else "ответ пуст")
            say(f"{model}: не дал текста — {why} (finish_reason: {choice.get('finish_reason')})")
            return None, True
        parsed = parse(text, len(events))
        if parsed is None:
            say(f"{model}: ответ не разобран ({len(text)} симв.): {text[:200]}")
            return None, True
        for i, item in enumerate(parsed):
            if item is None:
                continue
            defect = _defect(item)
            if defect:
                say(f"{model}: комментарий к событию {i} отброшен — {defect}")
                parsed[i] = None
        if not any(parsed):
            return None, True
        return parsed, False
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError) as exc:
        say(f"{model}: запрос не удался — {type(exc).__name__}: {str(exc)[:160]}")
        return None, True
    except Exception as exc:  # noqa: BLE001 — контракт модуля: разбор не роняет прогон
        say(f"{model}: непредвиденный отказ — {type(exc).__name__}: {str(exc)[:160]}")
        return None, True


def comments(events, payload, log=None):
    """Комментарии к событиям в том же порядке. None — разбора нет (это норма)."""
    say = _sayer(log)
    key = api_key()
    if not key or not events:
        return None
    models = chain()
    paid = [m for m in models if not m.endswith(":free")]
    if paid and not paid_allowed():
        say(f"модель «{paid[0]}» платная, а LLM_ALLOW_PAID не выставлен — комментариев не будет")
        return None
    started = time.monotonic()
    for i, model in enumerate(models):
        if i and time.monotonic() - started > BUDGET:
            say(f"бюджет разбора {BUDGET:.0f}с исчерпан на {i} моделях — дальше не пробую")
            break
        got, try_next = ask(model, events, payload, key, log=say)
        if got:
            if i:
                say(f"комментарий получен запасной моделью {model} (основная не ответила)")
            return got
        if not try_next:
            break
        if i + 1 < len(models):
            say(f"перехожу к следующей модели: {models[i + 1]}")
    say("ни одна бесплатная модель не дала разбора — события уйдут без комментария")
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
