"""Зеркало событий в приватную ленту хаба NEXUS (docs/CONTRACT.md §6).

ПОЧЕМУ отдельный модуль, а не ветка в telegram.py: у каналов разные механизмы
дедупа. Телеграм помнит отправленное маркером на диске (STATE_DIR/notify/*.json),
а хаб дедупит на своей стороне по паре (source, eventId) — повтор из очереди
pending для него безвреден, и локальный маркер здесь просто не нужен.

ПОЧЕМУ никогда не бросает исключений: тот же контракт, что у telegram.py. Хаб —
витрина, а не работа; его 500 не имеет права остановить публикацию панели.

ПОЧЕМУ пустая конфигурация — это OFF, а не FAIL: локальный прогон и dry-run про
хаб ничего не знают, и «не настроено» обязано отличаться от «не доставили».
Иначе alerts.dispatch считал бы каждое событие недоставленным и держал бы вечную
очередь повторов на машине разработчика.
"""

import json
import os
import re
import urllib.error
import urllib.request

TIMEOUT = 15
SOURCE = "842"

# Три исхода, как в telegram.py: доставлено / канал выключен / не смогли.
SENT, OFF, FAIL = "sent", "off", "fail"

# Граница предложения = точка (или ! ?) и пробел за ней. Числа вида «+1.4%/мес»
# и «−0.55%» под правило не попадают: после десятичной точки пробела нет.
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def config():
    url = (os.environ.get("NEXUS_EVENTS_URL") or "").strip()
    token = (os.environ.get("NEXUS_INGEST_TOKEN") or "").strip()
    return {"url": url, "token": token} if url and token else None


def configured():
    return config() is not None


def compose(event):
    """Текст для хаба: первая фраза — заголовок ленты, остаток — подпись под ним.

    Хаб режет присланный текст по строкам (app/api/events/route.ts): строка 1
    становится заголовком события, строки 2–3 — подписью. Без этого разреза
    подпись падает обратно на заголовок, и свёрнутая лента дублирует сама себя.

    Префикс «Внимание.» из alerts.render здесь намеренно не используется: он стал
    бы всем заголовком целиком, а сама новость уехала бы в подпись.
    """
    text = (event.get("text") or "").strip()
    if not text:
        return ""
    parts = _SENTENCE.split(text, maxsplit=1)
    return parts[0] if len(parts) == 1 else f"{parts[0]}\n{parts[1].strip()}"


def deliver(event):
    """Отправить событие в ленту: SENT | OFF (канал не настроен) | FAIL."""
    cfg = config()
    if cfg is None:
        return OFF
    text = compose(event)
    if not text:
        return OFF
    body = json.dumps({
        "source": SOURCE,
        "text": text,
        # Ключ события стабилен и переживает повтор из очереди pending: хаб
        # дедупит по (source, eventId) и второй строки в ленте не заведёт.
        # Без него дедуп шёл бы по тексту, а текст у одного и того же события
        # меняется вместе с числами (расстояние до порога считается каждый прогон).
        "eventId": str(event.get("key") or "")[:1000],
        "occurredAt": event.get("ts"),
    }, ensure_ascii=False)
    req = urllib.request.Request(cfg["url"], data=body.encode("utf-8"), method="POST")
    req.add_header("content-type", "application/json; charset=utf-8")
    req.add_header("authorization", f"Bearer {cfg['token']}")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return SENT if 200 <= getattr(resp, "status", 0) < 300 else FAIL
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return FAIL
    except Exception:  # noqa: BLE001 — контракт модуля: зеркало не роняет прогон
        return FAIL
