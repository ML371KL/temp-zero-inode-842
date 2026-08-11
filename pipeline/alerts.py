"""События в телеграм (docs/CONTRACT.md §6): ТОЛЬКО переходы, не состояния.

ПОЧЕМУ только переходы: панель и так показывает состояние; уведомление ценно ровно
в момент смены. Ежедневное «сегодня по-прежнему токсичная ячейка» читатель выключает
через неделю, и вместе с ним выключает настоящий алерт.

ПОЧЕМУ состояние прошлого прогона лежит в файле, а не выводится из data.json: часть
переходов (доставлен ли алерт, о каком знаке ядра уже сообщали) в публикуемый payload
не попадает — иначе гистерезис ядра пришлось бы восстанавливать по истории каждый раз.

ПОЧЕМУ pending: телеграм лежит регулярно. Недоставленное событие остаётся в очереди
и повторяется на следующем прогоне (сутки), маркер дедупа двигает только успех.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pipeline.lib import constants, telegram

STATE_NAME = "alerts_state.json"
FEED_LIMIT = 20          # столько последних событий уезжает в data.json
PENDING_MAX_HOURS = 24   # старше — не повторяем: новость протухла
DEPOSIT_UPTICK_PP = 0.05  # декадная ставка шумит в сотых, порог отсекает дрожь


def state_dir():
    env = (os.environ.get("STATE_DIR") or "").strip()
    return Path(env) if env else Path(__file__).resolve().parents[1] / ".state"


def _state_path():
    return state_dir() / STATE_NAME


def load_state():
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state):
    path = _state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        return True
    except OSError:
        return False


def _iso(dt=None):
    return (dt or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _num(v, d=1, plus=False):
    if v is None:
        return "н/д"
    return f"{v:{'+' if plus else ''},.{d}f}".replace(",", " ")


def _mons(payload):
    return {t.get("id"): t for t in (payload.get("monitors") or []) if isinstance(t, dict)}


def _mp(mons, tid, key, default=None):
    return ((mons.get(tid) or {}).get("payload") or {}).get(key, default)


def _ev(key, kind, text, severity="info", now=None):
    return {"key": key, "kind": kind, "severity": severity, "text": text.strip(),
            "ts": _iso(now)}


def snapshot(payload, now=None):
    """Что запоминаем до следующего прогона. Только то, по чему ищем переходы."""
    core = payload.get("core") or {}
    cur = (payload.get("states") or {}).get("current") or {}
    mons = _mons(payload)
    return {
        "asof": payload.get("asof_trading_day"),
        "core_value": core.get("value"),
        "core_sign": core.get("sign"),
        "cell": (payload.get("verdict") or {}).get("cell_code"),
        "trend": cur.get("trend"), "vol": cur.get("vol"), "bond": cur.get("bond"),
        "health": (core.get("health") or {}).get("status"),
        "key_rate": _mp(mons, "cb_meeting", "key_rate"),
        "deposit": _mp(mons, "deposit_spread", "deposit_pct"),
        "orfr_asof": (mons.get("orfr") or {}).get("asof"),
        "auction_date": _mp(mons, "ofz_auctions", "date"),
        "sources": {k: (v or {}).get("status") for k, v in (payload.get("sources") or {}).items()
                    if isinstance(v, dict)},
        "updated_at": _iso(now),
    }


# -------------------------------------------------------------------- правила

def _core_flip(payload, prev, state, now):
    """Разворот ядра с гистерезисом: сообщаем, только когда |value| ушёл за порог.

    Без гистерезиса композит, болтающийся около нуля, шлёт по алерту в день —
    ровно это и заставило зафиксировать CORE_FLIP_HYSTERESIS в валидации.
    """
    core = payload.get("core") or {}
    val = core.get("value")
    if not isinstance(val, (int, float)):
        return []
    hyst = constants.CORE_FLIP_HYSTERESIS
    sign = 1 if val > hyst else (-1 if val < -hyst else 0)
    if sign == 0:
        return []
    prev_sign = state.get("core_sign_alerted")
    state["core_sign_alerted"] = sign
    if prev_sign is None or prev_sign == sign:
        # Первый прогон запоминает знак молча: иначе установка пайплайна начинается
        # с алерта «разворот», которого не было.
        return []
    from_txt = _num(prev.get("core_value"), 2, True)
    since = core.get("sign_since")
    tail = f" Новый знак с {since}." if since else ""
    label = (payload.get("verdict") or {}).get("core_label") or ""
    return [_ev(f"core_flip:{sign}:{payload.get('asof_trading_day')}", "core_flip",
                f"Ядро развернулось: {_num(val, 2, True)} ({label}), было {from_txt}.{tail}",
                now=now)]


def _cell_change(payload, prev, now):
    verdict = payload.get("verdict") or {}
    cell = verdict.get("cell_code")
    old = prev.get("cell")
    if not cell or not old or cell == old:
        return []
    stats = verdict.get("cell_stats") or {}
    label = verdict.get("cell_label") or "без статистики"
    nums = ""
    if stats:
        nums = (f", исторически {_num(stats.get('mean_fwd1m_pct'), 1, True)}%/мес "
                f"(n={stats.get('n')}, hit {stats.get('hit')})")
    sev = "warn" if (stats.get("mean_fwd1m_pct") or 0) < 0 else "info"
    return [_ev(f"cell:{payload.get('asof_trading_day')}:{cell}", "state_cell_change",
                f"Смена ячейки: {old} → {cell} ({label}){nums}.", sev, now)]


def _bond_flag(payload, prev, now):
    cur = (payload.get("states") or {}).get("current") or {}
    new, old = cur.get("bond"), prev.get("bond")
    if new is None or old is None or new == old:
        return []
    dist = ""
    for row in (payload.get("states") or {}).get("distances") or []:
        if row.get("id") == "bond" and row.get("text"):
            dist = f" {row['text']}."
            break
    asof = payload.get("asof_trading_day")
    if new == 1:
        return [_ev(f"bond_on:{asof}", "bond_flag_on",
                    f"Облигационный флаг ВКЛЮЧЁН.{dist} Покупка просадок отключается: "
                    f"при долговом стрессе dd<−10% давала −0,55%/мес.", "warn", now)]
    return [_ev(f"bond_off:{asof}", "bond_flag_off",
                f"Облигационный флаг снят.{dist} Покупка просадок снова в силе: "
                f"+1,4%/мес (hit 0,64) при спокойном RGBI.", "info", now)]


def _buy_window(payload, prev, now):
    cur = (payload.get("states") or {}).get("current") or {}
    vol, bond = cur.get("vol"), cur.get("bond")
    if vol != 1 or bond != 0:
        return []
    if prev.get("vol") == 1 and prev.get("bond") == 0:
        return []
    trend = cur.get("trend")
    stats = constants.CELL_STATS.get((trend, 1, 0)) or {}
    nums = (f" Ячейка исторически {_num(stats.get('mean_fwd1m_pct'), 1, True)}%/мес "
            f"(n={stats.get('n')}).") if stats else ""
    return [_ev(f"buy_window:{payload.get('asof_trading_day')}", "buy_window_open",
                f"Окно входа: шип волатильности при спокойных ОФЗ.{nums}", "info", now)]


def _cb(payload, prev, now):
    mons = _mons(payload)
    tile = mons.get("cb_meeting") or {}
    pl = tile.get("payload") or {}
    out = []
    days, nxt = pl.get("days_left"), pl.get("next_meeting")
    key_rate, cons = pl.get("key_rate"), pl.get("consensus")
    if nxt and days == 1:
        out.append(_ev(f"cb_reminder:{nxt}", "cb_reminder",
                       f"Завтра заседание ЦБ ({nxt}). Ключ {_num(key_rate, 2)}%, консенсус "
                       f"{_num(cons, 2)}%. {pl.get('priced_text') or ''}", "info", now))
    old_rate = prev.get("key_rate")
    if isinstance(key_rate, (int, float)) and isinstance(old_rate, (int, float)) \
            and abs(key_rate - old_rate) > 1e-9:
        delta_bp = round((key_rate - old_rate) * 100)
        if isinstance(cons, (int, float)):
            surprise_bp = round((key_rate - cons) * 100)
            verdict = ("в линию с консенсусом" if surprise_bp == 0
                       else f"СЮРПРИЗ {_num(surprise_bp, 0, True)} б.п. к консенсусу {_num(cons, 2)}%")
        else:
            verdict = "консенсуса в данных нет"
        out.append(_ev(f"cb_decision:{payload.get('asof_trading_day')}:{key_rate}", "cb_decision",
                       f"ЦБ: ключевая {_num(key_rate, 2)}% ({_num(delta_bp, 0, True)} б.п.) — "
                       f"{verdict}.", "warn" if "СЮРПРИЗ" in verdict else "info", now))
    return out


def _orfr(payload, prev, now):
    tile = _mons(payload).get("orfr") or {}
    asof = tile.get("asof")
    if not asof or not prev.get("orfr_asof") or asof == prev.get("orfr_asof"):
        return []
    pl = tile.get("payload") or {}
    exhaust = (pl.get("seller_exhaustion") or {}).get("text") or ""
    return [_ev(f"orfr:{asof}", "orfr_published",
                f"Потоки ОРФР за {asof}: {tile.get('headline')}. {exhaust}.", "info", now)]


def _auction(payload, prev, now):
    tile = _mons(payload).get("ofz_auctions") or {}
    pl = tile.get("payload") or {}
    date_ = pl.get("date")
    if not pl.get("failed") or not date_ or date_ == prev.get("auction_date"):
        return []
    return [_ev(f"auction_failed:{date_}", "auction_failed",
                f"Аукцион ОФЗ {date_} провален: размещено {_num(pl.get('placed_bn'), 1)} млрд "
                f"при спросе {_num(pl.get('demand_bn'), 1)} млрд. Минфин не даёт премию — "
                f"давление уходит в длинный конец.", "warn", now)]


def _deposit(payload, prev, now):
    tile = _mons(payload).get("deposit_spread") or {}
    pl = tile.get("payload") or {}
    new, old = pl.get("deposit_pct"), prev.get("deposit")
    if not isinstance(new, (int, float)) or not isinstance(old, (int, float)):
        return []
    if new - old < DEPOSIT_UPTICK_PP:
        return []
    return [_ev(f"deposit_uptick:{pl.get('deposit_asof')}", "deposit_uptick",
                f"Максимальная ставка по вкладам {_num(new, 2)}% "
                f"({_num(new - old, 2, True)} п.п.). Спред к дивдоходности "
                f"{_num(pl.get('spread_pp'), 1)} п.п. — ротация в акции отдаляется.", "info", now)]


def _sources(payload, prev, now):
    out = []
    old = prev.get("sources") or {}
    for name, meta in sorted((payload.get("sources") or {}).items()):
        if not isinstance(meta, dict):
            continue
        st = meta.get("status")
        if st not in ("stale", "error"):
            continue
        if old.get(name) in ("stale", "error"):
            continue  # уже сообщали, повторять каждый прогон нельзя
        age = meta.get("lag_min")
        age_txt = f", возраст {int(age // 60)} ч" if isinstance(age, (int, float)) and age >= 60 \
            else (f", возраст {int(age)} мин" if isinstance(age, (int, float)) else "")
        out.append(_ev(f"source_stale:{name}:{payload.get('asof_trading_day')}", "source_stale",
                       f"Источник {name}: {st}{age_txt}, данные от {meta.get('asof')}. "
                       f"Панель считает по кэшу.", "warn", now))
    return out


def _health(payload, prev, now):
    health = ((payload.get("core") or {}).get("health") or {})
    st = health.get("status")
    if st != "dead" or prev.get("health") == "dead":
        return []
    return [_ev(f"health_dead:{payload.get('asof_trading_day')}", "health_dead",
                f"Здоровье ядра: IC за {health.get('n')} мес {_num(health.get('ic_24m'), 2, True)} — "
                f"статус dead. Композит не использовать до разбора.", "warn", now)]


def detect(payload, state, now=None):
    """Все переходы этого прогона. state мутируется (гистерезис знака ядра)."""
    now = now or datetime.now(timezone.utc)
    prev = state.get("last") or {}
    events = []
    events += _core_flip(payload, prev, state, now)
    if not prev:
        # Первый прогон на чистой машине запоминает мир молча. Иначе установка
        # пайплайна начинается с пачки «событий» о том, что случилось до него:
        # проваленный на прошлой неделе аукцион и протухший с вечера источник.
        return events
    events += _cell_change(payload, prev, now)
    events += _bond_flag(payload, prev, now)
    events += _buy_window(payload, prev, now)
    events += _cb(payload, prev, now)
    events += _orfr(payload, prev, now)
    events += _auction(payload, prev, now)
    events += _deposit(payload, prev, now)
    events += _sources(payload, prev, now)
    events += _health(payload, prev, now)
    return events


# ------------------------------------------------------------------ доставка

def render(event):
    prefix = "Внимание. " if event.get("severity") == "warn" else ""
    return f"{prefix}{event.get('text', '')}"


def dispatch(events, dry_run=False, enabled=True):
    for ev in events:
        if not enabled:
            ev["delivered"], ev["skip"] = False, "алерты выключены"
        elif dry_run:
            ev["delivered"], ev["skip"] = False, "dry-run"
        else:
            ev["delivered"] = telegram.notify(ev["key"], render(ev))
    return events


def _fresh(ev, now):
    try:
        ts = datetime.fromisoformat(str(ev.get("ts")).replace("Z", "+00:00"))
    except ValueError:
        return False
    return now - ts < timedelta(hours=PENDING_MAX_HOURS)


def payload_events(new_events=None, state=None):
    """Лента для data.json: сохранённый хвост + события этого прогона (CONTRACT §3)."""
    state = state if state is not None else load_state()
    feed = list(state.get("feed") or [])
    seen = {e.get("key") for e in feed}
    for ev in new_events or []:
        if ev.get("key") not in seen:
            feed.append({k: ev[k] for k in ("key", "ts", "kind", "severity", "text") if k in ev})
            seen.add(ev.get("key"))
    feed = feed[-FEED_LIMIT:]
    return [{"ts": e.get("ts"), "kind": e.get("kind"), "severity": e.get("severity"),
             "text": e.get("text")} for e in feed]


def run(payload, dry_run=False, enabled=True, now=None):
    """Полный цикл: определить переходы, повторить недоставленное, отправить, сохранить."""
    now = now or datetime.now(timezone.utc)
    state = load_state()
    events = detect(payload, state, now)
    pending = [e for e in (state.get("pending") or []) if _fresh(e, now)]
    batch = pending + events
    dispatch(batch, dry_run=dry_run, enabled=enabled)

    feed = list(state.get("feed") or [])
    seen = {e.get("key") for e in feed}
    for ev in events:
        if ev.get("key") not in seen:
            feed.append({k: ev[k] for k in ("key", "ts", "kind", "severity", "text")})
    state["feed"] = feed[-FEED_LIMIT:]
    state["last"] = snapshot(payload, now)
    state["pending"] = [{k: e[k] for k in ("key", "ts", "kind", "severity", "text")}
                        for e in batch if not e.get("delivered")][:40]
    if not dry_run:
        # В dry-run состояние НЕ трогаем: иначе прогон «на посмотреть» съедает
        # переход, и настоящий прогон о нём уже не сообщит.
        save_state(state)
    return batch


def after_publish(publish_result, dry_run=False, enabled=True, now=None):
    """Санитарные события, которые видны только после попытки записи."""
    now = now or datetime.now(timezone.utc)
    state = load_state()
    had = bool(state.get("lease_ok", True))
    have = bool((publish_result or {}).get("lease_ok", True))
    events = []
    if had and not have:
        events.append(_ev(f"lease_lost:{now.strftime('%Y-%m-%d')}", "lease_lost",
                          f"Лиз писателя потерян: {(publish_result or {}).get('lease_reason')}. "
                          f"Этот раннер публикацию пропустил.", "warn", now))
    dispatch(events, dry_run=dry_run, enabled=enabled)
    if not dry_run:
        state["lease_ok"] = have
        save_state(state)
    return events
