#!/usr/bin/env python3
"""Точка входа пайплайна MOEX Radar (docs/CONTRACT.md §4).

ПОЧЕМУ отказ источника не валит прогон: панель обязана обновляться и с половиной
источников — протухший тайл честно светится жёлтым, это лучше вчерашней страницы
целиком. Ненулевой код возврата остаётся ровно на один случай: опубликовать не смогли.

ПОЧЕМУ интрадей не пересчитывает ядро и состояния: и композит, и три бита состояния
определены на ЗАКРЫТИЕ дня (REGIME.md §2). Пересчёт внутри дня даёт мигающие флаги
на дневных шипах — сигнал, которого на истории не существовало, а значит и в
валидации его нет. Внутри дня двигаются только котировки и возраст данных.
"""

import argparse
import importlib
import inspect
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Журнал прогона на русском, а консоль Windows по умолчанию cp1252 — там print
# кириллицы кидает UnicodeEncodeError и валит прогон на диагностическом сообщении.
# На VPS (Linux/UTF-8) этого не бывает, но пайплайн запускают и с рабочей машины.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    # Чтобы `python pipeline/run.py` работал так же, как `python -m pipeline.run`:
    # на VPS в systemd-юните и в GHA стартовые каталоги разные.
    sys.path.insert(0, str(_ROOT))

from pipeline import alerts, publish as publish_mod             # noqa: E402
from pipeline.lib import constants, lease, r2, registry, telegram  # noqa: E402
from pipeline.lib.http import FetchError                        # noqa: E402

MISSING_MODULES = {}


def _optional(modname):
    """Модули соседних агентов: отсутствие ловим и докладываем, а не падаем трейсбеком."""
    try:
        return importlib.import_module(modname)
    except ImportError as exc:
        MISSING_MODULES[modname] = str(exc)
        return None


store = _optional("pipeline.lib.store")
panel_mod = _optional("pipeline.compute.panel")
core_mod = _optional("pipeline.compute.core")
states_mod = _optional("pipeline.compute.states")
health_mod = _optional("pipeline.compute.health")
monitors_mod = _optional("pipeline.compute.monitors")

MODES = ("intraday", "daily", "weekly", "monthly", "manual", "bootstrap", "selftest")
QUOTE_SERIES = [("imoex", "Индекс МосБиржи"), ("rgbi", "RGBI"), ("rvi", "RVI"),
                ("cny_tom", "CNY/RUB"), ("brent_moex", "Brent"), ("gld_tom", "Золото")]
# Интрадей-котировки: бумага ISS → ОТДЕЛЬНЫЙ ряд стора live_*.
#
# ПОЧЕМУ отдельный ряд, а не точка в imoex: дневное закрытие — это то, на чём
# посчитаны ядро и состояния (REGIME.md §2). Подложив в imoex цену середины дня,
# мы бы подменили закрытие внутридневным значением, и фолбэк «интрадей без прошлого
# data.json — считаем полностью» посчитал бы композит на неполном дне. live_* живут
# сбоку, их читает только витрина котировок, а панель и валидация их не видят.
LIVE_QUOTE_IDS = {"IMOEX": "live_imoex", "RGBI": "live_rgbi", "RVI": "live_rvi",
                  "CNYRUB_TOM": "live_cny_tom", "GLDRUB_TOM": "live_gld_tom"}
LIVE_PREFIX = "live_"
SELFTEST_ASOF = "2026-08-11"  # фиксированная дата: «сегодня минус N» в проверках запрещено


class Journal:
    """Компактный журнал прогона: одна строка на событие, без многострочных трейсбеков."""

    def __init__(self, verbose=False):
        self.t0 = time.time()
        self.verbose = verbose
        self.warns = []

    def line(self, tag, msg):
        print(f"[{tag}] {msg}", flush=True)

    def warn(self, tag, msg):
        self.warns.append(f"{tag}: {msg}")
        print(f"[{tag}] ВНИМАНИЕ {msg}", flush=True)

    def debug(self, tag, msg):
        if self.verbose:
            self.line(tag, msg)

    def elapsed(self):
        return time.time() - self.t0


# ------------------------------------------------------------------ утилиты

def _msk_today(now):
    return (now + timedelta(hours=constants.MSK_OFFSET_HOURS)).date()


def _pairs(sid):
    """Отсортированные точки ряда из стора; пусто при любой проблеме."""
    try:
        obj = store.load_series(sid) if store else None
    except Exception:  # noqa: BLE001 — битый файл ряда не повод падать
        return []
    pts = (obj or {}).get("points") or {}
    if not isinstance(pts, dict):
        return []
    return sorted(((d, v) for d, v in pts.items() if v is not None), key=lambda r: r[0])


def _meta(sid):
    try:
        obj = store.load_series(sid) if store else None
    except Exception:  # noqa: BLE001
        return {}
    return (obj or {}).get("meta") or {}


def _age_min(ts, now):
    if not isinstance(ts, str) or not ts.strip():
        return None
    try:
        dt = datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return round((now - dt).total_seconds() / 60.0, 1)


# --------------------------------------------------------------------- фетч

def _resolve(fetcher):
    """'iss.index' → pipeline.fetch.iss.index."""
    mod_name, _, fn_name = str(fetcher).partition(".")
    if not mod_name or not fn_name:
        raise FetchError(f"кривая ссылка на фетчер: {fetcher!r}")
    try:
        mod = importlib.import_module(f"pipeline.fetch.{mod_name}")
    except ImportError as exc:
        raise FetchError(f"нет модуля pipeline.fetch.{mod_name} ({exc})") from exc
    fn = getattr(mod, fn_name, None)
    if not callable(fn):
        raise FetchError(f"в pipeline.fetch.{mod_name} нет функции {fn_name}")
    return fn


def _normalize(res):
    """Фетчер отдаёт (series_id, points, meta) либо список таких троек (subkeys)."""
    if res is None:
        return []
    if isinstance(res, tuple) and len(res) == 3 and isinstance(res[0], str):
        return [res]
    if isinstance(res, (list, tuple)):
        return [item for item in res
                if isinstance(item, tuple) and len(item) == 3 and isinstance(item[0], str)]
    return []


def plan(mode, now, only=None):
    """[(series_id, spec, причина пропуска|None)] — печатается в журнал как есть."""
    if mode == "bootstrap":
        ids = list(registry.SERIES)
    else:
        ids = [sid for sid, _ in registry.series_for_mode(mode)]
    if only:
        wanted = {s.strip() for s in only.split(",") if s.strip()}
        ids = [sid for sid in ids if sid in wanted]
    day = _msk_today(now).day
    out = []
    for sid in ids:
        spec = registry.SERIES[sid]
        skip = None
        if not only and mode != "bootstrap" and not registry.poll_due(spec, day):
            win = spec.get("poll_window")
            skip = f"окно опроса {win[0]}–{win[1]} числа, сегодня {day}"
        out.append((sid, spec, skip))
    return out


def fetch_all(items, journal, bootstrap=False):
    """Загрузка с изоляцией отказов. Возвращает сводку по каждому ряду."""
    report = {}
    for sid, spec, skip in items:
        if skip:
            journal.line("fetch", f"{sid} пропуск ({skip})")
            report[sid] = {"status": "skip", "note": skip}
            continue
        t0 = time.time()
        try:
            fn = _resolve(spec["fetcher"])
            args = dict(spec.get("args") or {})
            if bootstrap and "bootstrap" in inspect.signature(fn).parameters:
                args["bootstrap"] = True
            results = _normalize(fn(**args))
            if not results:
                raise FetchError("фетчер вернул пусто")
            added = 0
            for out_id, points, meta in results:
                points = points or {}
                store.upsert_points(out_id, points, meta or {})
                added += len(points)
            ms = int((time.time() - t0) * 1000)
            asof = (results[0][2] or {}).get("asof")
            # Половина фетчеров (minfin, rosstat, orfr, moex_press, polymarket,
            # investfunds) по контракту §0 наружу не кидает, а отдаёт отказ полем
            # meta.status. Считать провалом только исключение — значит писать в
            # журнал «ok» о ряде, который не собрался ни разу: три таких ряда
            # молча простояли пустыми, и сводка отказов их не показывала.
            bad = sorted({str((m or {}).get("status")) for _, _, m in results
                          if (m or {}).get("status") in ("error", "manual_needed")})
            if bad:
                note = "; ".join(str((m or {}).get("note") or "")[:120] for _, _, m in results
                                 if (m or {}).get("status") in ("error", "manual_needed"))
                journal.warn("fetch", f"{sid} источник вернул {','.join(bad)} "
                                      f"({ms}мс, точек={added}): {note}")
                report[sid] = {"status": "error", "points": added, "ms": ms, "asof": asof,
                               "note": note}
                continue
            journal.line("fetch", f"{sid} ok {ms}мс точек={added} asof={asof}")
            report[sid] = {"status": "ok", "points": added, "ms": ms, "asof": asof}
        except FetchError as exc:
            journal.warn("fetch", f"{sid} отказ: {exc} (работаем на кэше)")
            report[sid] = {"status": "error", "note": str(exc)}
            _mark_error(sid, str(exc))
        except Exception as exc:  # noqa: BLE001 — граница изоляции: баг фетчера ≠ падение прогона
            journal.warn("fetch", f"{sid} сбой: {type(exc).__name__}: {exc}")
            report[sid] = {"status": "error", "note": f"{type(exc).__name__}: {exc}"}
            _mark_error(sid, f"{type(exc).__name__}: {exc}")
    return report


def fetch_live_quotes(journal, now=None):
    """marketdata ISS для интрадей-такта: цена, которая действительно движется днём.

    Раньше режим intraday опрашивал history тех же бумаг, а history внутри дня ещё
    не содержит текущего дня — панель весь торговый день переиздавала вчерашнее
    закрытие с новым generated_at и писала «обновлено только что». iss.intraday_quote
    существовал, но не вызывался ниоткуда.
    """
    if store is None:
        return {}
    now = now or datetime.now(timezone.utc)
    results, origin = [], "iss"
    # T-Invest первым, когда есть токен: бесплатный ISS накрывает ход торгов
    # ИНСТРУМЕНТАМИ (юань, золото) задержкой ровно в 15 минут — замерено
    # 12.08.2026 в 11:10 МСК: UPDATETIME=10:55 при SYSTIME=11:10. Индексы биржа
    # отдаёт без задержки и там выигрыша нет, но один запрос T-Invest покрывает
    # весь набор сразу, а пять запросов к ISS — по одному на бумагу.
    try:
        tinvest = importlib.import_module("pipeline.fetch.tinvest")
        if tinvest.ready():
            mapping, extra = dict(tinvest.LIVE_UIDS), {}
            # Фьючерс Brent — единственный инструмент витрины с ПЛАВАЮЩИМ
            # идентификатором: контракт катится помесячно. Передний контракт уже
            # разрешил суточный прогон, uid лежит в meta с прошлого раза, поэтому
            # обычный такт не делает ни одного лишнего запроса.
            uid, secid, since = tinvest.front_futures(store, today=_msk_today(now).isoformat())
            if uid:
                mapping["live_brent_moex"] = uid
                extra["live_brent_moex"] = {"secid": secid, "secid_since": since}
            results = _normalize(tinvest.live_quotes(mapping, extra))
            origin = "tinvest"
    except Exception as exc:  # noqa: BLE001 — граница изоляции
        journal.warn("fetch", f"живые котировки T-Invest: {type(exc).__name__}: {exc} "
                              f"(беру бесплатный ISS)")
        results = []
    if not results:
        try:
            iss = importlib.import_module("pipeline.fetch.iss")
            results = _normalize(iss.intraday_quote(secs=tuple(LIVE_QUOTE_IDS),
                                                    ids=LIVE_QUOTE_IDS))
        except Exception as exc:  # noqa: BLE001 — граница изоляции, как в fetch_all
            journal.warn("fetch", f"живые котировки: {type(exc).__name__}: {exc} "
                                  f"(витрина покажет последнее закрытие)")
            return {}
    out = {}
    for sid, points, meta in results:
        try:
            store.upsert_points(sid, points or {}, meta or {})
        except Exception as exc:  # noqa: BLE001
            journal.warn("fetch", f"{sid}: {type(exc).__name__}: {exc}")
            continue
        out[sid] = (meta or {}).get("asof")
    journal.line("fetch", f"живые котировки ({origin}): " +
                 (", ".join(f"{k}={v}" for k, v in sorted(out.items())) or "нет"))
    return out


def _mark_error(sid, note):
    """Пометить ряд как упавший, не трогая точки: панель обязана показать, что
    данные из кэша, а не притворяться свежей."""
    if store is None or not hasattr(store, "upsert_points"):
        return
    try:
        store.upsert_points(sid, {}, {"status": "error", "note": note[:200]})
    except Exception:  # noqa: BLE001 — пометка не важнее прогона
        pass


# ------------------------------------------------------------------- расчёт

def _sources(now, journal):
    """Сводка источников для data.json §3. Предпочитаем compute/health.py."""
    if health_mod is not None:
        for name in ("build_sources", "compute_sources", "sources", "build_health"):
            fn = getattr(health_mod, name, None)
            if not callable(fn):
                continue
            try:
                res = fn(store)
            except TypeError:
                try:
                    res = fn()
                except Exception as exc:  # noqa: BLE001
                    journal.warn("sources", f"health.{name}: {exc}")
                    continue
            except Exception as exc:  # noqa: BLE001
                journal.warn("sources", f"health.{name}: {exc}")
                continue
            if isinstance(res, dict) and res:
                return res
        journal.debug("sources", "в compute/health.py не нашлось сборщика — считаем сами")
    return _sources_fallback(now)


# Порядок «худшести» статуса источника. Неизвестный статус приравнивается к
# missing, а не к error: выдумывать дефект по незнакомому слову хуже, чем
# промолчать (CONTRACT §7 перечисляет ровно эти четыре).
STATUS_RANK = {"ok": 0, "stale": 1, "missing": 2, "error": 3}


def _worse(status, than):
    return STATUS_RANK.get(status, 2) > STATUS_RANK.get(than, 2)


def _sources_fallback(now):
    """Минимальная сводка из meta рядов, если health-модуль назвал функцию иначе.

    Семье присваивается статус ХУДШЕГО её ряда, и порядок «худшести» тут не
    косметика. `error` — «спросили и получили отказ», это дефект. `missing` —
    «ещё не собирали», а у месячных рядов это штатное состояние одиннадцать
    месяцев из двенадцати. Пока missing стоял хуже error, вся семья Минфина
    светилась «missing» из-за одного `ngd` вне окна опроса — при том что
    `budget_deficit` в ней собирался, а `fnb` с `ofz_auctions` честно падали.
    Читалось это как «у Минфина не собрано ничего», то есть ровно наоборот.

    Второй слой той же ошибки — ниже: ряд, которого в сторе нет ВООБЩЕ, не
    должен определять статус семьи, пока в ней есть собранные ряды. Оговорка
    «он не повод топить семью» уже стояла для рядов стора, но для записей
    реестра терялась.
    """
    stored = set(store.list_series()) if hasattr(store, "list_series") else set()
    known, empty = {}, {}
    for sid, spec in registry.SERIES.items():
        family = str(spec.get("fetcher", "")).split(".")[0]
        # Ряд реестра может разворачиваться в НЕСКОЛЬКО рядов стора: zcyc -> zcyc_y1…,
        # futoi_mx -> futoi_mx_pos…, orfr_flows -> orfr_flows_fiz… Искать по точному
        # имени бессмысленно: живой источник объявлялся «missing» и красил всю семью
        # (поймано на первом же прогоне — ISS отработал 16 рядов, а витрина писала,
        # что его нет).
        ids = [sid] + sorted(x for x in stored if x.startswith(sid + "_"))
        best = None
        for cand in ids:
            pts, meta = (monitors_mod.series_points(store, cand) if monitors_mod
                         else (_pairs(cand), _meta(cand)))
            if not pts and not meta.get("fetched_at"):
                continue  # ряд ни разу не тянули — он не повод топить семью
            status = (monitors_mod.series_status(cand, pts, meta, now) if monitors_mod
                      else ("missing" if not pts else (meta.get("status") or "ok")))
            row = {"asof": meta.get("asof") or (pts[-1][0] if pts else None),
                   "fetched_at": meta.get("fetched_at"),
                   "status": status,
                   "lag_min": _age_min(meta.get("fetched_at"), now),
                   "series": cand}
            if best is None or _worse(status, best["status"]):
                best = row
        if best is None:
            # Ряда нет в сторе вовсе. Это «ещё не собирали» — он запоминается
            # отдельно и станет статусом семьи, только если в ней вообще ничего
            # не собрано.
            empty.setdefault(family, sid)
            continue
        cur = known.get(family)
        if cur is None or _worse(best["status"], cur["status"]):
            known[family] = best
    out = dict(known)
    for family, sid in empty.items():
        # Фронт красит missing серым, не жёлтым: «не собирали» — не поломка.
        out.setdefault(family, {"asof": None, "fetched_at": None, "status": "missing",
                                "lag_min": None, "series": sid})
    return out


def _asof_trading_day(now):
    pts = _pairs("imoex")
    return pts[-1][0] if pts else _msk_today(now).isoformat()


def _quotes(now):
    """Блок котировок: живая цена, если она свежее закрытия, иначе закрытие.

    Изменение к вчерашнему считаем от ПОСЛЕДНЕГО ЗАКРЫТИЯ строго ДО даты живой
    точки: сравнивать живую цену с самой собой (закрытие того же дня уже могло
    приехать в history вечером) — значит рисовать 0% в момент, когда рынок ходит.
    """
    out = {}
    for sid, label in QUOTE_SERIES:
        pts = _pairs(sid)
        live = _pairs(LIVE_PREFIX + sid)
        row = None
        if live and (not pts or live[-1][0] >= pts[-1][0]):
            d, v = live[-1]
            base = [p for p in pts if p[0] < d]
            prev = base[-1][1] if base else None
            meta = _meta(LIVE_PREFIX + sid)
            # Перекат фьючерса: живая цена НОВОГО контракта против закрытия
            # СТАРОГО — это контанго в 1–2%, а не движение нефти. Раз в месяц
            # панель рисовала бы выдуманный скачок, поэтому в день смены
            # контракта изменение за день не считаем вовсе.
            since = meta.get("secid_since")
            if since and base and base[-1][0] < since:
                prev = None
            row = {"intraday": True, "delay_min": meta.get("delay_min"),
                   "updatetime": meta.get("updatetime"),
                   "contract": meta.get("secid")}
        elif pts:
            d, v = pts[-1]
            prev = pts[-2][1] if len(pts) > 1 else None
            meta = _meta(sid)
            row = {"intraday": False}
        if row is None:
            continue
        row.update({"label": label, "value": round(float(v), 4), "asof": d,
                    "chg_pct": (round((v / prev - 1.0) * 100.0, 2) if prev else None),
                    "age_min": _age_min(meta.get("fetched_at"), now)})
        out[sid] = row
    return out


def _monitors(now, journal):
    if monitors_mod is None:
        return []
    try:
        tiles = monitors_mod.build_monitors(store, now)
    except Exception as exc:  # noqa: BLE001 — build_monitors и так изолирует тайлы
        journal.warn("monitors", f"сборка тайлов упала: {type(exc).__name__}: {exc}")
        return []
    by_status = {}
    for t in tiles:
        by_status[t["status"]] = by_status.get(t["status"], 0) + 1
    journal.line("monitors", f"тайлов {len(tiles)}: " +
                 ", ".join(f"{k}={v}" for k, v in sorted(by_status.items())))
    return tiles


def build_full(now, journal):
    """Полный пересчёт: панель → ядро → состояния → мониторы."""
    panel = panel_mod.build_panel(store)
    core = core_mod.compute_core(panel)
    states = states_mod.compute_states(panel)
    dates = (panel or {}).get("dates") or []
    journal.line("compute", f"панель {len(dates)}×{len((panel or {}).get('cols') or {})} "
                            f"ядро={core.get('value')} знак={core.get('sign')} "
                            f"здоровье={(core.get('health') or {}).get('status')}")
    return core, states


def build_payload_for_mode(mode, now, journal):
    monitors = _monitors(now, journal)
    sources = _sources(now, journal)
    asof = _asof_trading_day(now)
    quotes = _quotes(now)

    if mode == "intraday":
        prev = publish_mod.read_local_payload()
        if prev and (prev.get("core") or prev.get("states")):
            journal.line("compute", "интрадей: ядро и состояния взяты из прошлого прогона "
                                    f"(asof {prev.get('asof_trading_day')})")
            # asof_trading_day остаётся ДНЁМ ВЕРДИКТА, а не днём последней котировки:
            # иначе панель подписала бы вчерашнее ядро сегодняшней датой. Свежесть
            # цен живёт отдельно, в quotes.
            return publish_mod.build_payload(
                core=prev.get("core"), states=prev.get("states"), monitors=monitors,
                sources=sources, mode=mode,
                asof=prev.get("asof_trading_day") or asof, quotes=quotes)
        journal.warn("compute", "интрадей без прошлого data.json — считаем полностью")

    if not all((panel_mod, core_mod, states_mod, store)):
        raise RuntimeError("нет модулей расчёта: " + ", ".join(sorted(MISSING_MODULES)))
    core, states = build_full(now, journal)
    return publish_mod.build_payload(core=core, states=states, monitors=monitors,
                                     sources=sources, mode=mode, asof=asof, quotes=quotes)


def _seed_payload(journal):
    """Опубликованная витрина для восстановления состояния алертов на чистой машине.

    Нужна только фолбэк-раннеру GHA (STATE_DIR в runner.temp, каждый прогон с нуля)
    и первой установке: без снимка прошлого detect молчит по определению, и ровно
    в аварии VPS телеграм-канал был глухим. На VPS состояние есть — лишнего GET к
    бакету не делаем.
    """
    try:
        if not alerts.needs_seed():
            return None
    except Exception as exc:  # noqa: BLE001 — состояние не читается: не наша беда
        journal.debug("alerts", f"состояние не прочиталось: {exc}")
        return None
    prev = publish_mod.read_local_payload()
    if prev:
        return prev
    if not r2.configured():
        return None
    try:
        prev = r2.get_json(publish_mod.DATA_KEY)
    except Exception as exc:  # noqa: BLE001 — бакет недоступен: работаем как раньше
        journal.warn("alerts", f"опубликованный data.json не прочитан: {exc}")
        return None
    if prev:
        journal.line("alerts", "состояние восстановлено из опубликованного data.json "
                               f"(asof {prev.get('asof_trading_day')})")
    return prev


# ------------------------------------------------------------------ selftest

class _EmptyStore:
    """Стор без единого ряда: проверяем, что тайлы деградируют, а не падают."""

    def load_series(self, sid):
        return None

    def list_dirty(self):
        return []


def selftest(journal):
    problems = []
    for name, mod in (("pipeline.lib.store", store), ("pipeline.compute.panel", panel_mod),
                      ("pipeline.compute.core", core_mod), ("pipeline.compute.states", states_mod),
                      ("pipeline.compute.health", health_mod),
                      ("pipeline.compute.monitors", monitors_mod)):
        state = "есть" if mod is not None else f"НЕТ ({MISSING_MODULES.get(name)})"
        journal.line("selftest", f"модуль {name}: {state}")
        if mod is None and name != "pipeline.compute.health":
            problems.append(f"нет модуля {name}")

    if len(constants.CELL_STATS) != 8:
        problems.append(f"CELL_STATS: {len(constants.CELL_STATS)} ячеек вместо 8")
    for key in constants.CELL_STATS:
        if key not in constants.CELL_RULES:
            problems.append(f"нет правила дня для ячейки {key}")

    if monitors_mod is not None:
        problems += monitors_mod.check_coverage()
        tiles = monitors_mod.build_monitors(_EmptyStore())
        journal.line("selftest", f"тайлов на пустом сторе: {len(tiles)}")
        for t in tiles:
            if t["status"] not in ("ok", "stale", "error", "missing"):
                problems.append(f"{t['id']}: неизвестный статус {t['status']}")
            if t["tier"] == "dead" and monitors_mod.DEAD_MARK not in (t["note"] or "").lower():
                problems.append(f"{t['id']}: тир dead без обязательной пометки")
            if t["status"] == "error":
                problems.append(f"{t['id']}: упал на пустом сторе — {t['payload'].get('error')}")
        payload = publish_mod.build_payload(
            core={"value": 0.5, "sign": 1, "health": {"status": "ok"}},
            states={"current": {"trend": 0, "vol": 1, "bond": 1}},
            monitors=tiles, sources={}, mode="selftest", asof=SELFTEST_ASOF)
        data, cut = publish_mod.fit_size(payload)
        journal.line("selftest", f"payload {len(data)} Б, обрезано: {cut or 'ничего'}; "
                                 f"вердикт {payload['verdict']['cell_code']} / "
                                 f"{payload['verdict']['cell_label']}")
        if payload["verdict"]["cell_code"] != "bear|stress|stress":
            problems.append("вердикт собрался неверно на контрольной ячейке")

    journal.line("selftest", f"R2: {'настроен' if r2.configured() else 'НЕ настроен'}; "
                             f"телеграм: {'настроен' if telegram.configured() else 'НЕ настроен'}; "
                             f"писатель: {lease.writer_role()}/{lease.writer_id()}")
    journal.line("selftest", f"каталог состояния: {publish_mod.state_dir()}")
    for p in problems:
        journal.warn("selftest", p)
    journal.line("selftest", "ПРОБЛЕМ НЕТ" if not problems else f"проблем: {len(problems)}")
    return 0 if not problems else 1


# ---------------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(prog="run.py", description="Прогон пайплайна MOEX Radar")
    ap.add_argument("--mode", required=True, choices=MODES)
    ap.add_argument("--dry-run", action="store_true", help="ничего не писать в R2")
    ap.add_argument("--no-alerts", action="store_true", help="не слать события в телеграм")
    ap.add_argument("--only", default=None, help="список series_id через запятую (окна опроса игнорируются)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    # Журнал на русском, а консоль Windows по умолчанию cp1251/cp1252: без этого
    # прогон падает на первой же строке лога с UnicodeEncodeError (проверено).
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass

    journal = Journal(args.verbose)
    now = datetime.now(timezone.utc)
    journal.line("start", f"режим={args.mode} utc={now.strftime('%Y-%m-%dT%H:%M:%SZ')} "
                          f"мск={_msk_today(now)} dry_run={args.dry_run}")
    if args.mode == "selftest":
        return selftest(journal)

    if store is None:
        journal.warn("start", "нет pipeline/lib/store.py — публиковать нечего")
        return 3

    items = plan(args.mode, now, args.only)
    journal.line("plan", f"рядов к опросу {sum(1 for _, _, s in items if not s)} "
                         f"из {len(items)} (пропуск по окнам: {sum(1 for _, _, s in items if s)})")
    fetch_report = fetch_all(items, journal, bootstrap=(args.mode == "bootstrap"))
    if args.mode == "intraday":
        fetch_live_quotes(journal, now)
    failed = [sid for sid, r in fetch_report.items() if r["status"] == "error"]
    if failed:
        journal.line("fetch", f"отказов {len(failed)}: {', '.join(sorted(failed)[:8])}")

    try:
        payload = build_payload_for_mode(args.mode, now, journal)
    except Exception as exc:  # noqa: BLE001 — расчёт упал целиком: пробуем прошлый payload
        journal.warn("compute", f"расчёт упал: {type(exc).__name__}: {exc}")
        prev = publish_mod.read_local_payload()
        if not prev:
            journal.warn("compute", "прошлого data.json нет — публиковать нечего")
            return 2
        payload = publish_mod.build_payload(
            core=prev.get("core"), states=prev.get("states"),
            monitors=_monitors(now, journal), sources=_sources(now, journal),
            mode=args.mode, asof=prev.get("asof_trading_day"), quotes=_quotes(now))

    # Алерты изолированы целиком: это единственный этап, который сам ничего не
    # публикует. Раньше исключение в правиле или мусор в alerts_state.json
    # превращали «нет уведомления» в «панель не обновилась» — прогон падал ДО
    # publish, и systemd после трёх таких падений глушил юнит.
    events = []
    if not args.no_alerts:
        try:
            events = alerts.run(payload, dry_run=args.dry_run, enabled=True, now=now,
                                seed_payload=_seed_payload(journal))
        except Exception as exc:  # noqa: BLE001 — граница изоляции
            journal.warn("alerts", f"правила упали: {type(exc).__name__}: {exc} "
                                   f"(публикацию это не останавливает)")
    try:
        payload["events"] = alerts.payload_events(events)
    except Exception as exc:  # noqa: BLE001
        journal.warn("alerts", f"лента событий не собралась: {type(exc).__name__}: {exc}")
        payload["events"] = []
    delivered = sum(1 for e in events if e.get("delivered"))
    journal.line("alerts", f"событий {len(events)}, доставлено {delivered}" +
                 ("" if not events else ": " + "; ".join(e["kind"] for e in events)))

    res = publish_mod.publish(payload, args.mode, store=store, dry_run=args.dry_run)
    size_kb = res["bytes"] / 1024.0
    journal.line("publish", f"data.json {size_kb:.1f} КБ; опубликовано={res['published']}; "
                            f"{res.get('reason')}; объектов {len(res['objects'])}; "
                            f"raw {len(res.get('raw_mirrored') or [])}"
                            + (f"; обрезано: {', '.join(res['trimmed'])}" if res["trimmed"] else ""))
    for err in res["errors"]:
        journal.warn("publish", err)
    if res.get("integrity"):
        journal.warn("publish", ("целостность payload: " if res["published"] else
                                 "витрина НЕ опубликована, целостность: ")
                     + "; ".join(res["integrity"]))
    if not args.no_alerts:
        try:
            alerts.after_publish(res, dry_run=args.dry_run, enabled=True, now=now)
        except Exception as exc:  # noqa: BLE001 — граница изоляции
            journal.warn("alerts", f"санитарные события упали: {type(exc).__name__}: {exc}")
    try:
        telegram.prune_markers()
    except Exception as exc:  # noqa: BLE001
        journal.warn("alerts", f"чистка маркеров: {type(exc).__name__}: {exc}")

    code = 0 if res["ok"] else 2
    journal.line("done", f"режим={args.mode} за {journal.elapsed():.1f}с "
                         f"предупреждений={len(journal.warns)} код={code}")
    return code


if __name__ == "__main__":
    sys.exit(main())
