"""События в телеграм и в ленту хаба NEXUS (docs/CONTRACT.md §6): ТОЛЬКО переходы,
не состояния.

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

from pipeline.lib import constants, nexus, telegram

STATE_NAME = "alerts_state.json"
FEED_LIMIT = 20          # столько последних событий уезжает в data.json
PENDING_MAX_HOURS = 24   # старше — не повторяем: новость протухла
PENDING_MAX = 40         # длина очереди повторов; режем СТАРОЕ, а не свежее
DEPOSIT_UPTICK_PP = 0.05  # декадная ставка шумит в сотых, порог отсекает дрожь
EVENT_FIELDS = ("key", "ts", "kind", "severity", "text")


def state_dir():
    env = (os.environ.get("STATE_DIR") or "").strip()
    return Path(env) if env else Path(__file__).resolve().parents[1] / ".state"


def _state_path():
    return state_dir() / STATE_NAME


def load_state():
    """Состояние прошлого прогона. Значения неверных типов выбрасываем.

    ПОЧЕМУ проверяем типы: файл переживает падения прогона, ручные правки и откаты
    версий. Один раз попавший в pending мусор (строка вместо списка событий) валил
    весь прогон в alerts.run ДО публикации — панель не обновлялась из-за алерта.
    """
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    for key in ("pending", "feed"):
        data[key] = [e for e in data.get(key) or [] if isinstance(e, dict) and e.get("key")] \
            if isinstance(data.get(key), list) else []
    if not isinstance(data.get("last"), dict):
        data.pop("last", None)
    return data


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
    # «Было X» берём из ЗАФИКСИРОВАННОГО знака, а не из вчерашнего снимка: путь
    # 0.66 → 0.02 → −0.02 → −0.66 давал текст «развернулось: −0.66, было −0.02» —
    # оба числа одного знака, сообщение выглядит ошибочным ровно там, ради чего
    # гистерезис и введён.
    prev_value = state.get("core_value_alerted")
    state["core_sign_alerted"] = sign
    state["core_value_alerted"] = val
    if prev_sign is None or prev_sign == sign:
        # Первый прогон запоминает знак молча: иначе установка пайплайна начинается
        # с алерта «разворот», которого не было.
        return []
    if not isinstance(prev_value, (int, float)):
        prev_value = prev.get("core_value")  # состояние от старой версии без core_value_alerted
    from_txt = _num(prev_value, 2, True)
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
        # Разделитель в жёстких строках — ТОЧКА, как в _num и monitors._n: иначе в
        # одном прогоне рядом висят «+1.4%/мес (hit 0.6)» и «+1,4%/мес (hit 0,64)»,
        # и читатель принимает разные величины за одну и ту же с опечаткой.
        return [_ev(f"bond_on:{asof}", "bond_flag_on",
                    f"Облигационный флаг ВКЛЮЧЁН.{dist} Покупка просадок отключается: "
                    f"при долговом стрессе dd<−10% давала −0.55%/мес.", "warn", now)]
    return [_ev(f"bond_off:{asof}", "bond_flag_off",
                f"Облигационный флаг снят.{dist} Покупка просадок снова в силе: "
                f"+1.4%/мес (hit 0.64) при спокойном RGBI.", "info", now)]


def _buy_window(payload, prev, now):
    cur = (payload.get("states") or {}).get("current") or {}
    vol, bond = cur.get("vol"), cur.get("bond")
    if vol != 1 or bond != 0:
        return []
    # Та же защита, что в _bond_flag: неизвестное прошлое — не переход. Состояния
    # приходят с None после дня, когда панель не посчиталась (нет RGBI), и без
    # этой строки следующий прогон зовёт «Окно входа» о ячейке, которая не менялась.
    if prev.get("vol") is None or prev.get("bond") is None:
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


# Тревога об аукционе — про СЕГОДНЯШНИЙ провал. Аукционы идут по средам, четырёх
# суток хватает и на вечернюю публикацию, и на пропущенный такт.
AUCTION_FRESH_DAYS = 4


def _age_days(day, now):
    """Сколько суток назад была дата тайла. None — если дата не разобралась."""
    try:
        when = datetime.strptime(str(day)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return (now - when).days


def _auction(payload, prev, now):
    tile = _mons(payload).get("ofz_auctions") or {}
    pl = tile.get("payload") or {}
    date_ = pl.get("date")
    if not pl.get("failed") or not date_ or date_ == prev.get("auction_date"):
        return []
    # Дата тайла меняется НЕ ТОЛЬКО когда прошёл новый аукцион: переезд на другой
    # источник, починка ряда или восстановление стора сдвигают её задним числом.
    # 12.08.2026 так и вышло — ряд переехал на биржевую доску, «последним» стал
    # реальный аукцион 15.07 вместо нуля затравки от 05.08, и правило разослало
    # «аукцион провален» про день четырёхнедельной давности. Смена записи в истории
    # не событие рынка; событие — свежий провал.
    age = _age_days(date_, now)
    if age is None or age > AUCTION_FRESH_DAYS:
        return []
    # Спрос бывает пустым штатно: биржа его не раскрывает. «при спросе н/д млрд» —
    # единица, приклеенная к отсутствующему числу (та же ошибка, что уже правили на
    # тайле), поэтому про молчание говорим словами.
    demand = pl.get("demand_bn")
    tail = (f" при спросе {_num(demand, 1)} млрд" if isinstance(demand, (int, float))
            else "; спрос не раскрыт")
    return [_ev(f"auction_failed:{date_}", "auction_failed",
                f"Аукцион ОФЗ {date_} провален: размещено {_num(pl.get('placed_bn'), 1)} млрд"
                f"{tail}. Минфин не даёт премию — давление уходит в длинный конец.",
                "warn", now)]


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


def _core_missing(payload, prev, now):
    """Вчера был вердикт, сегодня «нет данных» — это авария, а не состояние рынка.

    Ловит частичную потерю стора (восстановление из неполной копии, оборванная
    запись, ручная чистка raw/): фетч при этом проходит зелёным, источники ok,
    health смотрит только на 'dead', — молчали ВСЕ правила, а панель писала
    «нет данных» поверх рабочего вердикта.
    """
    core = payload.get("core") or {}
    cell = (payload.get("verdict") or {}).get("cell_code")
    lost_core = core.get("value") is None and isinstance(prev.get("core_value"), (int, float))
    lost_cell = not cell and bool(prev.get("cell"))
    if not lost_core and not lost_cell:
        return []
    what = " и ".join(p for p in ("ядро" if lost_core else "", "вердикт" if lost_cell else "") if p)
    was = _num(prev.get("core_value"), 2, True) if lost_core else prev.get("cell")
    return [_ev(f"core_missing:{payload.get('asof_trading_day')}", "core_missing",
                f"Витрина потеряла {what}: было {was}, стало «нет данных». "
                f"Похоже на неполный стор — публиковать такую панель нельзя.", "warn", now)]


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
    events += _core_missing(payload, prev, now)
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
            outcome = telegram.deliver(ev["key"], render(ev))
            # DUP («такой ключ уже уходил») — это ДОСТАВЛЕНО. Считать его провалом
            # значит вечно держать событие в очереди повторов: cb_reminder рождается
            # на каждом из 56 интрадей-тактов и один вытеснял из очереди всё живое.
            ev["outcome"] = outcome
            # Копия события уходит в ленту хаба NEXUS. Доставленным событие
            # считается, только когда прошли ОБА канала: недоставленное ложится в
            # pending и повторяется, а телеграм на повторе отвечает DUP и второй
            # раз в канал не пишет. Отдельная очередь для зеркала не нужна.
            # OFF (канал не настроен) блокировать доставку не имеет права — иначе
            # прогон без NEXUS_* копил бы вечную очередь повторов.
            ev["nexus"] = nexus.deliver(ev)
            ev["delivered"] = (outcome in (telegram.SENT, telegram.DUP)
                               and ev["nexus"] in (nexus.SENT, nexus.OFF))
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


def _dedup(events):
    """По одному событию на ключ; при повторе остаётся ПОСЛЕДНЯЯ (свежая) копия."""
    out = {}
    for ev in events:
        if isinstance(ev, dict) and ev.get("key"):
            out[ev["key"]] = ev
    return list(out.values())


def _requeue(batch):
    """Очередь повторов: только недоставленное, без дублей, режем СТАРОЕ.

    Срез с головы ([:40]) выбрасывал самое свежее событие — а именно свежее и есть
    новость. Плюс дедуп: без него одна и та же напоминалка занимала всю очередь.
    """
    return [{k: e[k] for k in EVENT_FIELDS if k in e}
            for e in _dedup(batch) if not e.get("delivered")][-PENDING_MAX:]


def _feed_add(state, events):
    feed = list(state.get("feed") or [])
    seen = {e.get("key") for e in feed}
    for ev in events:
        if ev.get("key") not in seen:
            feed.append({k: ev[k] for k in EVENT_FIELDS if k in ev})
            seen.add(ev.get("key"))
    state["feed"] = feed[-FEED_LIMIT:]
    return state


def seed_from_payload(state, payload, now=None):
    """Восстановить снимок «прошлого прогона» из ОПУБЛИКОВАННОГО data.json.

    ПОЧЕМУ нужно: на чистой машине state пуст, а detect на пустом prev молчит по
    определению (иначе установка началась бы с пачки «событий» о прошлой неделе).
    Фолбэк-раннер GHA поднимается с пустым STATE_DIR КАЖДЫЙ раз — то есть ровно в
    аварии VPS, когда владелец ждёт уведомлений, канал был гарантированно глухим.
    Витрины хватает на все правила, кроме гистерезиса знака ядра.
    """
    if state.get("last") or not isinstance(payload, dict):
        return False
    snap = snapshot(payload, now)
    if snap.get("core_value") is None and not snap.get("cell"):
        return False  # витрина сама пустая — восстанавливать нечего
    state["last"] = snap
    val = snap.get("core_value")
    if isinstance(val, (int, float)) and abs(val) > constants.CORE_FLIP_HYSTERESIS:
        # Знак из опубликованного ядра, иначе первый прогон на фолбэке объявит
        # «разворот» просто потому, что раньше о знаке никто не сообщал.
        state["core_sign_alerted"] = 1 if val > 0 else -1
        state["core_value_alerted"] = val
    return True


def run(payload, dry_run=False, enabled=True, now=None, seed_payload=None):
    """Полный цикл: определить переходы, повторить недоставленное, отправить, сохранить."""
    now = now or datetime.now(timezone.utc)
    state = load_state()
    if seed_payload is not None:
        seed_from_payload(state, seed_payload, now)
    events = detect(payload, state, now)
    pending = [e for e in (state.get("pending") or []) if _fresh(e, now)]
    batch = _dedup(pending + events)
    dispatch(batch, dry_run=dry_run, enabled=enabled)

    _feed_add(state, events)
    state["last"] = snapshot(payload, now)
    state["pending"] = _requeue(batch)
    if not dry_run:
        # В dry-run состояние НЕ трогаем: иначе прогон «на посмотреть» съедает
        # переход, и настоящий прогон о нём уже не сообщит.
        save_state(state)
    return batch


def needs_seed():
    """Нужен ли внешний снимок: состояния прошлого прогона на машине нет."""
    return not load_state().get("last")


def after_publish(publish_result, dry_run=False, enabled=True, now=None):
    """Санитарные события, которые видны только после попытки записи."""
    now = now or datetime.now(timezone.utc)
    res = publish_result or {}
    state = load_state()
    had = bool(state.get("lease_ok", True))
    have = bool(res.get("lease_ok", True))
    events, lease_events = [], []
    if had and not have:
        lease_events.append(_ev(f"lease_lost:{now.strftime('%Y-%m-%d')}", "lease_lost",
                                f"Лиз писателя потерян: {res.get('lease_reason')}. "
                                f"Этот раннер публикацию пропустил.", "warn", now))
    events += lease_events
    if res.get("oversize"):
        # Лестница обрезки прошла целиком, а payload всё равно за лимитом: это
        # дефект лестницы (раздулся блок, которого в ней нет), и молчать о нём
        # нельзя — иначе о нарушении контракта §3 узнаёт только тот, кто читает
        # journald руками.
        events.append(_ev(f"payload_oversize:{now.strftime('%Y-%m-%d')}", "payload_oversize",
                          f"data.json {_num(res.get('bytes'), 0)} Б больше лимита "
                          f"{_num(res.get('limit'), 0)} Б после всей обрезки "
                          f"(вырезано: {', '.join(res.get('trimmed') or []) or 'нечего'}). "
                          f"Первая отрисовка на мобильной сети замедлится — чинить лестницу.",
                          "warn", now))
    dispatch(events, dry_run=dry_run, enabled=enabled)
    if not dry_run:
        _feed_add(state, [e for e in events if e.get("delivered")])
        # Недоставленное уезжает в ту же очередь повторов, что и обычные события:
        # раньше lease_lost умирал молча при первом же сетевом чихе, а теряется
        # лиз как раз в инфраструктурной аварии, когда телеграм тоже нестабилен.
        state["pending"] = _requeue(list(state.get("pending") or []) + events)
        # Флаг двигаем только после доставки: иначе следующий прогон уже не увидит
        # перехода had→have и повторять будет нечего.
        if all(e.get("delivered") for e in lease_events):
            state["lease_ok"] = have
        save_state(state)
    return events
