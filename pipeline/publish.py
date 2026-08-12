"""Сборка payload и публикация в R2 (docs/CONTRACT.md §3, §5).

ПОЧЕМУ локальная копия пишется ВСЕГДА: (1) интрадей-прогон читает из неё прошлые
ядро и состояния, чтобы не пересчитывать композит внутри дня; (2) фолбэк-раннер и
отладка работают без бакета; (3) при отказе R2 остаётся ровно то, что мы собрали, —
разбирать инцидент по логам без артефакта невозможно.

ПОЧЕМУ жёсткий лимит 250 КБ: панель читают с телефона по мобильной сети, а data.json
грузится до первой отрисовки. Режем не «что попало», а по лестнице: сначала украшения
(спарклайны), потом глубину истории, и только в конце — события.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from pipeline.lib import constants, lease, r2

MAX_BYTES = 250 * 1024
DATA_KEY = "data.json"
HISTORY_DAILY_KEY = "history/daily.json"
HISTORY_MONITORS_KEY = "history/monitors.json"
# Дневная история хранится целиком за 2 последних года, дальше — по одной точке
# на месяц: до 2004 года ежедневных точек ~5500, и они не нужны для графика эпох.
HISTORY_DAILY_DAYS = 730
HISTORY_SERIES = ("imoex", "rgbi", "mcftr", "rvi")
MONITOR_HISTORY_LIMIT = 200


def state_dir():
    env = (os.environ.get("STATE_DIR") or "").strip()
    return Path(env) if env else Path(__file__).resolve().parents[1] / ".state"


def _iso(dt=None):
    return (dt or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def dumps(payload):
    """Ровно те байты, которые уйдут в бакет (и по которым меряется лимит)."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


# ------------------------------------------------------------------- вердикт

def core_label(value):
    if value is None:
        return "нет данных"
    for lo, hi, label in constants.CORE_LABELS:
        if lo <= value < hi:
            return label
    return "вне шкалы"


def _cell_code(cur):
    """«bear|stress|stress» из трёх бит состояния (CONTRACT §3)."""
    rules = constants.STATE_RULES
    parts = []
    for axis in ("trend", "vol", "bond"):
        bit = (cur or {}).get(axis)
        if bit is None:
            return None
        parts.append(rules[axis]["on"] if bit == 1 else rules[axis]["off"])
    return "|".join(parts)


def build_verdict(core, states):
    core = core or {}
    cur = (states or {}).get("current") or {}
    key = tuple(cur.get(a) for a in ("trend", "vol", "bond"))
    stats = constants.CELL_STATS.get(key)
    value = core.get("value")
    verdict = {
        "cell_code": _cell_code(cur),
        "cell_label": (stats or {}).get("label"),
        # Среднее по ячейке — хвостовая статистика, поэтому рядом с ним обязаны ехать
        # медиана и края: одно среднее читается как прогноз на месяц (constants §CELL_STATS).
        "cell_stats": ({k: stats[k] for k in
                        ("mean_fwd1m_pct", "hit", "n", "median_fwd1m_pct",
                         "worst_pct", "best_pct") if k in stats} if stats else None),
        "rule": constants.CELL_RULES.get(
            key, "Ячейка без исторической статистики: правила дня нет, смотреть на ядро."),
        "core_value": value,
        "core_label": core_label(value),
    }
    return verdict


def build_payload(core=None, states=None, monitors=None, sources=None, events=None,
                  mode="daily", asof=None, generated_at=None, quotes=None):
    payload = {
        "schema": constants.SCHEMA_VERSION,
        "generated_at": generated_at or _iso(),
        "run_mode": mode,
        "asof_trading_day": asof,
        "stale_after_minutes": constants.STALE_AFTER_MINUTES,
        "verdict": build_verdict(core, states),
        "core": core or {},
        "states": states or {},
        "monitors": monitors or [],
        "sources": sources or {},
        "events": events or [],
    }
    if quotes:
        # Живые котировки для интрадей-витрины: ядро и состояния внутри дня не
        # пересчитываются (они официально меняются по закрытию), а цена — меняется.
        payload["quotes"] = quotes
    return payload


# -------------------------------------------------------------------- обрезка

def _thin_pairs(pairs, keep_last=None):
    """Прореживание [[дата, значение]…]: свежий хвост целиком, старое — помесячно."""
    if not pairs:
        return pairs
    keep_last = keep_last if keep_last is not None else 500
    head, tail = pairs[:-keep_last], pairs[-keep_last:]
    monthly, seen = [], set()
    for row in reversed(head):
        month = str(row[0])[:7]
        if month not in seen:
            seen.add(month)
            monthly.append(row)
    return list(reversed(monthly)) + tail


def _drop_sparks(payload):
    hit = False
    for comp in (payload.get("core") or {}).get("components") or []:
        if comp.pop("spark", None) is not None:
            hit = True
    return hit


def _drop_monitor_series(payload):
    hit = False
    for tile in payload.get("monitors") or []:
        pl = tile.get("payload") or {}
        for field in ("series", "stack", "recent", "prints"):
            if pl.pop(field, None) is not None:
                hit = True
    return hit


def _thin_core_series(payload):
    core = payload.get("core") or {}
    before = core.get("series") or []
    if len(before) <= 400:
        return False
    core["series"] = _thin_pairs(before, keep_last=240)
    return True


def _thin_states_series(payload):
    st = payload.get("states") or {}
    before = st.get("series") or []
    if len(before) <= 400:
        return False
    st["series"] = _thin_pairs(before, keep_last=240)
    return True


def _cut_events(payload):
    ev = payload.get("events") or []
    if len(ev) <= 20:
        return False
    payload["events"] = ev[-20:]
    return True


# Лестница обрезки: сверху — то, чего меньше всего жалко.
TRIM_STEPS = [("monitor_series", _drop_monitor_series), ("spark", _drop_sparks),
              ("core_series", _thin_core_series), ("states_series", _thin_states_series),
              ("events", _cut_events)]


def fit_size(payload, limit=MAX_BYTES):
    """(байты, что вырезали). Payload меняется на месте."""
    data = dumps(payload)
    cut = []
    for name, step in TRIM_STEPS:
        if len(data) <= limit:
            break
        if step(payload):
            cut.append(name)
            data = dumps(payload)
    if cut:
        payload["trimmed"] = cut
        data = dumps(payload)
    return data, cut


# ---------------------------------------------------------------- целостность

def check_payload(payload):
    """Пусто там, где обязано быть число. Пустой список = витрину можно публиковать.

    ПОЧЕМУ проверка нужна: неполный стор (восстановление из обрезанной копии,
    оборванная запись, ручная чистка raw/) даёт панель со словами «нет данных»
    вместо вердикта, и при этом ВСЁ зелёное — фетч прошёл, источники ok, health
    смотрит только на 'dead', сторож видит свежий Last-Modified. Узнавалось это
    глазами на самой панели.
    """
    payload = payload or {}
    problems = []
    if (payload.get("core") or {}).get("value") is None:
        problems.append("ядро пустое (core.value = null)")
    if not (payload.get("verdict") or {}).get("cell_code"):
        problems.append("вердикт пуст (verdict.cell_code = null)")
    return problems


# -------------------------------------------------------------------- история

def _series_pairs(store, sid):
    try:
        obj = store.load_series(sid) if store else None
    except Exception:  # noqa: BLE001 — история не критична, пропускаем ряд
        return []
    pts = (obj or {}).get("points") or {}
    if not isinstance(pts, dict):
        return []
    return sorted(([d, v] for d, v in pts.items() if v is not None), key=lambda r: r[0])


def build_history(store, payload):
    daily = {"generated_at": payload.get("generated_at"),
             "asof": payload.get("asof_trading_day")}
    for sid in HISTORY_SERIES:
        pairs = _series_pairs(store, sid)
        if pairs:
            daily[sid] = _thin_pairs(pairs, keep_last=HISTORY_DAILY_DAYS)
    core_series = (payload.get("core") or {}).get("series") or []
    if core_series:
        daily["core"] = core_series
    st_series = (payload.get("states") or {}).get("series") or []
    if st_series:
        daily["states"] = st_series
    return daily


def build_monitors_history(monitors, previous=None):
    """Лента заголовков по тайлам: одна запись на изменение asof/заголовка."""
    hist = dict(previous or {})
    ts = _iso()
    for tile in monitors or []:
        tid = tile.get("id")
        if not tid or tile.get("status") == "missing":
            continue
        rec = {"ts": ts, "asof": tile.get("asof"), "headline": tile.get("headline"),
               "status": tile.get("status")}
        rows = list(hist.get(tid) or [])
        if rows and rows[-1].get("asof") == rec["asof"] and \
                rows[-1].get("headline") == rec["headline"]:
            continue
        rows.append(rec)
        hist[tid] = rows[-MONITOR_HISTORY_LIMIT:]
    return hist


# ------------------------------------------------------------------ запись

def _write_local(name, data):
    path = state_dir() / "out" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else dumps(data))
    return path


def read_local_payload():
    """Прошлый data.json с диска — источник ядра и состояний для интрадей-прогона."""
    path = state_dir() / "out" / DATA_KEY
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _mirror_raw(store, errors, limit=100):
    """Зеркалирование грязных рядов в raw/. Без обратной вычитки: их десятки, а
    критичен только data.json — цена лишней пары запросов выше пользы."""
    if store is None or not hasattr(store, "list_dirty"):
        return []
    try:
        dirty = list(store.list_dirty() or [])
    except Exception as exc:  # noqa: BLE001 — стор не обязан быть идеальным
        errors.append(f"list_dirty: {exc}")
        return []
    done = []
    for sid in dirty[:limit]:
        try:
            obj = store.load_series(sid)
            if obj:
                r2.put_json(f"raw/{sid}.json", obj, cache_control="public, max-age=300",
                            verify=False)
                done.append(sid)
        except (r2.R2Error, OSError, ValueError) as exc:
            errors.append(f"raw/{sid}: {exc}")
    clear = getattr(store, "clear_dirty", None) or getattr(store, "mark_clean", None)
    if clear and done:
        try:
            clear(done)
        except TypeError:
            for sid in done:
                try:
                    clear(sid)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"clear_dirty {sid}: {exc}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"clear_dirty: {exc}")
    return done


def publish(payload, mode="daily", store=None, dry_run=False, history=None):
    """Публикация. Возвращает отчёт; исключения наружу не выпускает.

    ok=False означает ровно одно: data.json опубликовать не удалось (для run.py это
    ненулевой код возврата). Отказ истории или зеркала raw/ — не повод падать.
    """
    result = {"ok": False, "published": False, "reason": None, "bytes": 0,
              "objects": [], "errors": [], "trimmed": [], "lease_ok": True,
              "lease_reason": None, "mode": mode, "limit": MAX_BYTES,
              "oversize": False, "integrity": []}

    # Прошлую витрину читаем ДО того, как перезапишем локальную копию: она же —
    # эталон для проверки целостности (не подсовываем ли мы пустоту поверх числа).
    previous = read_local_payload()
    problems = check_payload(payload)
    result["integrity"] = problems

    # История собирается ДО обрезки: fit_size меняет payload на месте, и прореженные
    # ядро с состояниями уезжали в history/daily.json — объект, который панель
    # называет «полной историей». Заметно только за 250 КБ, поэтому и не всплывало.
    hist = history if history is not None else build_history(store, payload)

    data, cut = fit_size(payload)
    result["bytes"], result["trimmed"] = len(data), cut
    if len(data) > MAX_BYTES:
        # Публикуем всё равно: тяжёлая панель лучше вчерашней. Но это состояние
        # обязано быть громким — лестницу обрезки пора чинить (событие шлёт
        # alerts.after_publish по этому флагу).
        result["oversize"] = True
        result["errors"].append(f"payload {len(data)} Б больше лимита {MAX_BYTES} Б после обрезки")

    if problems:
        result["errors"] += problems
        result["reason"] = "целостность: " + "; ".join(problems)
        if previous is None and not dry_run and r2.configured():
            # Локальной копии нет только у раннера с пустым STATE_DIR (фолбэк GHA).
            # Прежде чем положить «нет данных» поверх живой витрины, спрашиваем бакет:
            # иначе подмена писателя затирает хорошую панель ровно в аварии.
            try:
                previous = r2.get_json(DATA_KEY)
            except (r2.R2Error, OSError, ValueError) as exc:
                result["errors"].append(f"чтение {DATA_KEY}: {exc}")
        # Регрессия: вчера число было, сегодня null. Публиковать НЕЛЬЗЯ — иначе
        # авария конвейера доедет до читателя как «нет данных» и станет неотличима
        # от рыночного состояния. Локальную копию тоже не трогаем: из неё интрадей
        # берёт ядро и состояния, а следующий прогон — эталон целостности.
        known = set(check_payload(previous)) if isinstance(previous, dict) else None
        if known is not None and [p for p in problems if p not in known]:
            try:
                _write_local(DATA_KEY + ".rejected", data)
            except OSError as exc:
                result["errors"].append(f"локальная копия: {exc}")
            return result

    try:
        # Локальная раскладка повторяет бакет: фолбэк-раннер и отладка читают
        # те же пути, что фронт, — иначе расхождение вылезает в самый неудобный момент.
        _write_local(DATA_KEY, data)
        _write_local(HISTORY_DAILY_KEY, hist)
    except OSError as exc:
        result["errors"].append(f"локальная копия: {exc}")

    if dry_run:
        # ok=False при пустом ядре/вердикте и в сухом прогоне: проверка «на посмотреть»
        # обязана краснеть на том же, на чём покраснеет боевая.
        result.update(ok=not problems, reason=result["reason"] or "dry-run: в R2 не пишем")
        return result
    if not r2.configured():
        result["reason"] = "R2 не сконфигурирован"
        result["errors"].append(result["reason"])
        return result

    try:
        allowed, why = lease.can_write()
    except Exception as exc:  # noqa: BLE001 — недоступный бакет не должен ронять прогон
        allowed, why = False, f"лиз не читается: {exc}"
    result["lease_reason"] = why
    if not allowed:
        # Не наша очередь писать — это НЕ ошибка прогона (CONTRACT §5).
        result.update(ok=not problems, lease_ok=False,
                      reason=result["reason"] or f"лиз: {why}")
        return result

    try:
        r2.put(DATA_KEY, data, "application/json; charset=utf-8",
               cache_control="public, max-age=60")
        result["objects"].append(DATA_KEY)
        result.update(ok=not problems, published=True, reason=result["reason"] or why)
    except (r2.R2Error, OSError, ValueError) as exc:
        result["reason"] = f"data.json: {exc}"
        result["errors"].append(result["reason"])
        return result

    try:
        r2.put_json(HISTORY_DAILY_KEY, hist, cache_control="public, max-age=600")
        result["objects"].append(HISTORY_DAILY_KEY)
    except (r2.R2Error, OSError, ValueError) as exc:
        result["errors"].append(f"{HISTORY_DAILY_KEY}: {exc}")

    # «Объекта нет» и «прочитать не удалось» — РАЗНЫЕ вещи. get_json отдаёт None
    # только на 404; на 503 и на битом JSON он кидает. Раньше исключение приводило
    # к prev=None, и лента заголовков (до 200 записей × 14 тайлов) перезаписывалась
    # одной сегодняшней записью — необратимо, потому что локальная копия пишется
    # уже урезанной. Не прочитали — шаг ПРОПУСКАЕМ, ошибка остаётся в errors.
    prev, prev_ok = None, True
    try:
        prev = r2.get_json(HISTORY_MONITORS_KEY)
    except (r2.R2Error, OSError, ValueError) as exc:
        prev_ok = False
        result["errors"].append(f"чтение {HISTORY_MONITORS_KEY}: {exc} — историю мониторов "
                                f"в этот раз не трогаем")
    if prev_ok:
        mon_hist = build_monitors_history(payload.get("monitors"),
                                          prev if isinstance(prev, dict) else None)
        # Между интрадей-тактами объект байт-в-байт тот же (≈57 раз в сутки): лишний
        # PUT — это лишняя экспозиция к сбою записи, а пользы от него нет.
        if mon_hist != prev:
            try:
                r2.put_json(HISTORY_MONITORS_KEY, mon_hist, cache_control="public, max-age=600")
                result["objects"].append(HISTORY_MONITORS_KEY)
                _write_local(HISTORY_MONITORS_KEY, mon_hist)
            except (r2.R2Error, OSError, ValueError) as exc:
                result["errors"].append(f"{HISTORY_MONITORS_KEY}: {exc}")

    result["raw_mirrored"] = _mirror_raw(store, result["errors"])

    try:
        lease.refresh_heartbeat(mode=mode)
    except (r2.R2Error, OSError, ValueError) as exc:
        # Heartbeat не обновился — при следующем прогоне GHA может решить, что VPS
        # умер, и перехватить запись. Это шумно, но не ломает уже опубликованные данные.
        result["errors"].append(f"heartbeat: {exc}")
    return result
