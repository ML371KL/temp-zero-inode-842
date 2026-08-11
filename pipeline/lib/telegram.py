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


def state_dir():
    """STATE_DIR задаётся на VPS и в GHA; локально — .state в корне репозитория.

    Дублируется в publish.py/alerts.py/run.py намеренно: низкоуровневый модуль не
    должен тянуть за собой соседей ради трёх строк.
    """
    env = (os.environ.get("STATE_DIR") or "").strip()
    return Path(env) if env else Path(__file__).resolve().parents[2] / ".state"


def config():
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    return {"token": token, "chat": chat} if token and chat else None


def configured():
    return config() is not None


def _marker_path(key):
    safe = _SAFE_KEY.sub("_", str(key))[:120] or "empty"
    return state_dir() / "notify" / f"{safe}.json"


def already_sent(key, cooldown_hours=None):
    """Был ли ключ доставлен. cooldown_hours позволяет повторить старое событие."""
    path = _marker_path(key)
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


def _mark(key, text):
    path = _marker_path(key)
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


def send(text, silent=False, retries=2):
    """(ok, ошибка). Никогда не бросает."""
    cfg = config()
    if cfg is None:
        return False, "нет TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID"
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


def notify(key, text, silent=False, cooldown_hours=None):
    """Отправить с дедупом по ключу. True — доставлено именно сейчас."""
    try:
        if already_sent(key, cooldown_hours):
            return False
        ok, _err = send(text, silent=silent)
        if ok:
            _mark(key, text)
        return ok
    except Exception:  # noqa: BLE001 — контракт модуля: уведомление не роняет прогон
        return False


def prune_markers(days=45):
    """Чистка каталога маркеров: за год их накапливаются тысячи."""
    cutoff = time.time() - days * 86400
    removed = 0
    try:
        for path in (state_dir() / "notify").glob("*.json"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
    except OSError:
        return 0
    return removed
