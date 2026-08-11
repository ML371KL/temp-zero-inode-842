"""Единственный писатель в R2 (docs/CONTRACT.md §5).

ПОЧЕМУ не блокировки: два раннера (VPS и фолбэк в GitHub Actions) пишут один и тот
же data.json, и настоящая распределённая блокировка тут дороже проблемы. Конфликт
разрешается приоритетом: VPS пишет всегда, GHA — только если VPS молчит дольше ttl.

ПОЧЕМУ так строго: в соседнем проекте (839) два писателя по cron перетирали друг
друга и на панели прыгали данные разной свежести — заметили это только через неделю.
"""

import os
import socket
from datetime import datetime, timezone

from pipeline.lib import r2

LEASE_KEY = "lease.json"
DEFAULT_TTL = 5400  # 90 минут: интрадей-прогон VPS ходит чаще, три пропуска подряд = отказ


def writer_role():
    """vps | gha. Явный RADAR_WRITER важнее эвристики: на VPS можно запустить и «как GHA»."""
    role = (os.environ.get("RADAR_WRITER") or "").strip().lower()
    if role in ("vps", "gha"):
        return role
    return "gha" if os.environ.get("GITHUB_ACTIONS") == "true" else "vps"


def writer_id():
    """Идентификатор держателя. Без pid: он меняется каждый прогон, а лиз должен
    оставаться «нашим» между запусками, иначе VPS вечно перехватывает сам себя."""
    return os.environ.get("RADAR_WRITER_ID") or f"{writer_role()}:{socket.gethostname()}"


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts):
    if not isinstance(ts, str) or not ts.strip():
        return None
    try:
        dt = datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def read_lease():
    """Текущий лиз или None (нет объекта либо бакет недоступен)."""
    try:
        obj = r2.get_json(LEASE_KEY)
    except r2.R2Error:
        return None
    return obj if isinstance(obj, dict) else None


def age_seconds(lease, now=None):
    dt = _parse((lease or {}).get("heartbeat"))
    if dt is None:
        return None
    return ((now or datetime.now(timezone.utc)) - dt).total_seconds()


def can_write(wid=None, role=None, now=None):
    """(можно ли писать, человекочитаемая причина) — причина идёт в журнал прогона."""
    wid = wid or writer_id()
    role = role or writer_role()
    now = now or datetime.now(timezone.utc)
    lease = read_lease()
    if lease is None:
        return True, "лиза нет — заявляем свой"
    holder = lease.get("holder_id")
    owner = lease.get("writer")
    if role == "vps":
        if holder == wid:
            return True, "лиз наш"
        return True, f"перехват лиза у {owner}/{holder}: VPS всегда в приоритете"
    if owner == "gha":
        return True, "лиз уже у GHA"
    age = age_seconds(lease, now)
    ttl = lease.get("ttl_seconds")
    ttl = int(ttl) if isinstance(ttl, (int, float)) and ttl > 0 else DEFAULT_TTL
    if age is None:
        return True, "в лизе нет heartbeat — считаем его протухшим"
    if age > ttl:
        return True, f"heartbeat VPS протух: {int(age // 60)} мин > {ttl // 60} мин"
    return False, f"пишет VPS, heartbeat {int(age // 60)} мин назад"


def claim_lease(wid=None, role=None, mode=None, ttl=DEFAULT_TTL, now=None):
    """Записать лиз на себя (он же refresh: heartbeat обновляется тем же PUT)."""
    lease = {
        "writer": role or writer_role(),
        "holder_id": wid or writer_id(),
        "heartbeat": _iso(now or datetime.now(timezone.utc)),
        "ttl_seconds": int(ttl),
        "mode": mode,
    }
    # verify=False: лиз крошечный и переписывается каждый прогон, обратная вычитка
    # здесь стоит дороже, чем цена одной потерянной записи heartbeat.
    r2.put_json(LEASE_KEY, lease, cache_control="no-store", verify=False)
    return lease


def refresh_heartbeat(wid=None, role=None, mode=None, ttl=DEFAULT_TTL, now=None):
    return claim_lease(wid=wid, role=role, mode=mode, ttl=ttl, now=now)


def status_line(now=None):
    """Однострочник для журнала прогона."""
    role, wid = writer_role(), writer_id()
    lease = read_lease()
    if lease is None:
        return f"роль={role} лиза нет"
    age = age_seconds(lease, now)
    age_txt = "н/д" if age is None else f"{int(age // 60)} мин"
    return (f"роль={role} держатель={lease.get('writer')}/{lease.get('holder_id')} "
            f"heartbeat={age_txt} назад" + ("" if lease.get("holder_id") == wid else " (не наш)"))
