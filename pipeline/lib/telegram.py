"""Отправка событий в телеграм-канал + дедуп по ключу (docs/CONTRACT.md §6).

ПОЧЕМУ функции никогда не бросают исключений: алерт — это уведомление о данных, а
не сама работа. Упавший телеграм не имеет права уронить публикацию: в соседнем
проекте (837) отвалившийся токен однажды остановил весь прогон, и панель встала.

ПОЧЕМУ маркер двигается ТОЛЬКО после успешной доставки: иначе после сетевого сбоя
событие считается отправленным и пропадает навсегда. Недоставленное alerts.py
кладёт в pending и повторяет на следующем прогоне.
"""

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

API = "https://api.telegram.org"
TIMEOUT = 15
_SAFE_KEY = re.compile(r"[^0-9A-Za-z._=-]+")

# ДВА КАНАЛА, а не один. «Рынок развернулся» и «источник отдаёт 503» — новости для
# разных читателей и с разной судьбой: первую читают как ленту и хранят, вторую
# чинят и забывают. Смешанные в одном чате, они гасят друг друга: за неделю
# санитарных сообщений владелец перестаёт открывать канал, и вместе с ними мимо
# проходит смена ячейки.
#
# ops-канал — ОБЩИЙ для всех панелей (тот же бот, что зовёт /usr/local/sbin/dash-notify
# на VPS), поэтому отказы 842 приходят туда же, где отказы 837/838/839.
CHANNELS = {
    "alerts": ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"),
    "ops": ("ERROR_BOT_TOKEN", "ERROR_CHAT_ID"),
}

# Четыре исхода доставки. ПОЧЕМУ не bool: «уже отправляли» и «не доставили» — разные
# вещи, а notify() возвращал на них одинаковый False. Из-за этого alerts.run считал
# доставленное событие потерянным и клал его в очередь повторов: за день накануне
# заседания ЦБ 56 интрадей-тактов забивали очередь одним и тем же cb_reminder, и
# настоящие события (снятие облигационного флага, окно входа) в неё уже не влезали.
#
# OFF — «канала нет вовсе»: переменные не заданы, слать некуда и не станет куда до
# правки окружения. Это НЕ провал доставки, и повторять такое событие бессмысленно:
# без OFF прогон на машине без токена копил вечную очередь повторов — ровно та же
# ошибка, которую уже исправили в зеркале хаба (nexus.OFF, alerts.dispatch).
SENT, DUP, FAIL, OFF = "sent", "dup", "fail", "off"


def state_dir():
    """STATE_DIR задаётся на VPS и в GHA; локально — .state в корне репозитория.

    Дублируется в publish.py/alerts.py/run.py намеренно: низкоуровневый модуль не
    должен тянуть за собой соседей ради трёх строк.
    """
    env = (os.environ.get("STATE_DIR") or "").strip()
    return Path(env) if env else Path(__file__).resolve().parents[2] / ".state"


def config(channel="alerts"):
    token_env, chat_env = CHANNELS.get(channel) or CHANNELS["alerts"]
    token = (os.environ.get(token_env) or "").strip()
    chat = (os.environ.get(chat_env) or "").strip()
    return {"token": token, "chat": chat} if token and chat else None


def configured(channel="alerts"):
    return config(channel) is not None


def _marker_path(key, channel="alerts"):
    safe = _SAFE_KEY.sub("_", str(key))[:120] or "empty"
    # Маркеры разведены по каналам: один и тот же ключ, отправленный в другой чат,
    # обязан уйти заново. Каталог основного канала оставлен прежним — иначе смена
    # раскладки заставила бы его переслать всё, о чём уже сообщали.
    base = state_dir() / "notify"
    return (base if channel == "alerts" else base / channel) / f"{safe}.json"


def already_sent(key, cooldown_hours=None, channel="alerts"):
    """Был ли ключ доставлен. cooldown_hours позволяет повторить старое событие."""
    path = _marker_path(key, channel)
    if not path.exists():
        return False
    if cooldown_hours is None:
        return True
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
        sent = datetime.fromisoformat(str(rec.get("sent_at")).replace("Z", "+00:00"))
    except (OSError, ValueError, TypeError):
        return True  # маркер есть, но битый — считаем отправленным, лучше промолчать
    if sent.tzinfo is None:
        sent = sent.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - sent < timedelta(hours=cooldown_hours)


def _mark(key, text, channel="alerts"):
    path = _marker_path(key, channel)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {"key": str(key),
             "sent_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "sha1": hashlib.sha1(text.encode("utf-8"),
                                  usedforsecurity=False).hexdigest()[:12]},
            ensure_ascii=False), encoding="utf-8")
    except OSError:
        # Не смогли записать маркер — событие уйдёт повторно на следующем прогоне.
        # Это шумно, но безопаснее молчаливой потери алерта.
        pass


def send(text, silent=False, retries=2, channel="alerts"):
    """(ok, ошибка). Никогда не бросает."""
    cfg = config(channel)
    if cfg is None:
        return False, "не заданы " + "/".join(CHANNELS.get(channel) or CHANNELS["alerts"])
    body = json.dumps({"chat_id": cfg["chat"], "text": text[:4000],
                       "disable_web_page_preview": True,
                       "disable_notification": bool(silent)}, ensure_ascii=False)
    req = urllib.request.Request(f"{API}/bot{cfg['token']}/sendMessage",
                                 data=body.encode("utf-8"), method="POST")
    req.add_header("content-type", "application/json; charset=utf-8")
    last = "неизвестно"
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
                if payload.get("ok"):
                    return True, None
                last = str(payload.get("description"))[:200]
        except urllib.error.HTTPError as exc:
            try:
                last = exc.read()[:200].decode("utf-8", "replace")
            except OSError:
                last = f"HTTP {exc.code}"
            if exc.code in (400, 401, 403, 404):
                return False, last  # неверный токен/чат — ретрай не поможет
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            last = f"{type(exc).__name__}: {exc}"[:200]
        if attempt < retries:
            time.sleep(1.5)
    return False, last


def deliver(key, text, silent=False, cooldown_hours=None, channel="alerts"):
    """SENT (ушло сейчас) | DUP (уже отправляли) | OFF (канал не настроен) | FAIL.

    Вызывающему важно отличать DUP и OFF от FAIL: на первые двух событие считается
    закрытым и в очередь повторов НЕ кладётся, на FAIL — кладётся и повторяется.
    """
    try:
        if config(channel) is None:
            return OFF
        if already_sent(key, cooldown_hours, channel):
            return DUP
        ok, _err = send(text, silent=silent, channel=channel)
        if ok:
            _mark(key, text, channel)
            return SENT
        return FAIL
    except Exception:  # noqa: BLE001 — контракт модуля: уведомление не роняет прогон
        return FAIL


def notify(key, text, silent=False, cooldown_hours=None, channel="alerts"):
    """Совместимость: True — сообщение ушло ИМЕННО СЕЙЧАС (DUP и FAIL дают False)."""
    return deliver(key, text, silent=silent, cooldown_hours=cooldown_hours,
                   channel=channel) == SENT


# Ключи, которые обязаны пережить обычную чистку. Маркер — единственная память о
# том, что сообщение уже уходило, и живёт он ровно до `prune_markers`. У месячного
# отчёта реколибровки это ломало собственное обещание «пока состав проблем тот же —
# тишина»: маркер от 5 сентября исчезал к 20 октября, и 5 ноября та же находка
# уходила заново. Находка живёт месяцами по построению (константы не пересчитаны —
# расхождение верно и через полгода), поэтому её маркеру нужен свой срок.
LONG_LIVED = ("recalibrate:",)
LONG_LIVED_DAYS = 400


def _is_long_lived(path):
    """Долгоживущий ли маркер. Смотрим на КЛЮЧ внутри файла, а не на имя: имя —
    это хеш, из него исходный ключ не достать."""
    try:
        key = json.loads(path.read_text(encoding="utf-8")).get("key") or ""
    except (OSError, ValueError, TypeError):
        return False
    return any(str(key).startswith(prefix) for prefix in LONG_LIVED)


def prune_markers(days=45):
    """Чистка каталога маркеров: за год их накапливаются тысячи."""
    cutoff = time.time() - days * 86400
    long_cutoff = time.time() - LONG_LIVED_DAYS * 86400
    removed = 0
    try:
        # rglob, а не glob: маркеры ops-канала лежат подкаталогом, и обычный glob
        # чистил бы только основной — второй рос бы вечно.
        for path in (state_dir() / "notify").rglob("*.json"):
            try:
                limit = long_cutoff if _is_long_lived(path) else cutoff
                if path.stat().st_mtime < limit:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
    except OSError:
        return 0
    return removed
