# Контракт данных и модулей «MOEX Radar» (842)

Единственный источник правды о форматах. Любая правка формата — правка этого файла + инвариант в `tests/`.

## 0. Принципы реализации

- **Только стандартная библиотека Python 3.12** (`urllib`, `json`, `csv`, `datetime`, `statistics`, `re`, `gzip`). Ни pandas, ни numpy: пайплайн обязан запускаться на любой машине без venv. Тяжёлая статистика (реколибровка) живёт отдельно и не на критическом пути.
- **Один писатель в R2** — VPS. Фолбэк-писатель (GitHub Actions) активируется только при протухшем лизе.
- **Никаких «сегодня минус N» в тестах** — только замороженные фикстуры и фиксированные даты.
- **Все сигналы датируются днём ДОСТУПНОСТИ**, а не днём, к которому относятся (лаги в `SERIES` ниже).
- Провал одного источника **не** валит прогон: тайл получает `status: "stale"`, публикация идёт.

## 1. Нормализованный ряд (raw store)

Файл: локально `$STATE_DIR/raw/{series_id}.json`, зеркало в R2 `raw/{series_id}.json`.

```json
{
  "id": "imoex",
  "unit": "points",
  "cadence": "daily",
  "points": {"2026-08-10": 2293.32, "2026-08-11": 2301.0},
  "meta": {"source": "iss", "url": "https://iss.moex.com/...",
           "fetched_at": "2026-08-11T16:05:00Z", "asof": "2026-08-11",
           "status": "ok", "note": null}
}
```

Правила: ключи точек — `YYYY-MM-DD` (для месячных рядов — последний день месяца, к которому относится значение; лаг доступности применяется на слое расчёта); значения — число или `null`; `points` дополняется инкрементально, ретро-правки источника разрешены (перезапись значения).

Единый API стора (`pipeline/lib/store.py`):
```python
load_series(series_id) -> dict | None
save_series(series_id, series_dict) -> None       # пишет локально, помечает dirty для R2
upsert_points(series_id, {date: value}, meta_patch) -> dict
list_dirty() -> [series_id]                        # для выгрузки в R2
```

## 2. Реестр рядов (`pipeline/lib/registry.py`)

Каждая запись: `id`, `fetcher`, `cadence` (`intraday|daily|weekly|decade|monthly|event`), `pub_lag_days` (сколько добавить к дате периода, чтобы получить дату доступности), `poll_window` (для «до появления»: диапазон чисел месяца), `required` (bool — влияет ли на ядро), `tier`.

Обязательный минимум v1 (ядро + состояния): `imoex`, `imoex_close_val` (объём), `rgbi`, `rvi`, `mcftr`, `mcxsm`, `rtsi`, `cny_tom`, `gld_tom`, `usd_cbr`, `key_rate`, `zcyc_y1/y2/y10`, `rusfar3m`, `rucbhycp_yield`, `rucbcpns_yield`, `brent`, `deposit_decade`, `urals_tax`, `futoi_mx` (pos/long/short/holders), `stocks_breadth` (агрегат `%>MA200`).
Мониторы v1: `orfr_flows` (5 категорий), `lqdt_aum`, `cpi_weekly`, `cpi_monthly`, `ofz_auctions`, `polymarket_ceasefire`, `dividends_calendar`, `moex_retail`, `ngd_budget`, `fnb`, `m2`, `infom`.

## 3. `data.json` (то, что читает фронт)

Размер ≤ 250 КБ. Схема (`schema: 1`):

```json
{
  "schema": 1,
  "generated_at": "2026-08-11T16:05:12Z",
  "run_mode": "daily",
  "asof_trading_day": "2026-08-11",
  "stale_after_minutes": 150,
  "verdict": {
    "cell_code": "bear|stress|stress",
    "cell_label": "токсичная ячейка",
    "cell_stats": {"mean_fwd1m_pct": -2.94, "hit": 0.56, "n": 25},
    "rule": "…текст правила дня…",
    "core_value": 0.50,
    "core_label": "умеренный лонг"
  },
  "core": {
    "value": 0.50,
    "sign": 1,
    "sign_since": "2026-07-24",
    "month_end": {"date": "2026-07-31", "value": 0.50, "label": "умеренный лонг"},
    "degraded": false, "n_components": 3, "n_expected": 3,
    "full_legs_since": "2017-12-29",
    "legs_segments": [[1, "2004-01-30", "2016-11-30"], [2, "2016-12-30", "2017-11-30"],
                      [3, "2017-12-29", "2026-08-11"]],
    "components": [
      {"id": "usd_mom63", "label": "Девальвационный моментум", "z": 1.21,
       "raw": 0.0834, "raw_fmt": "+8.3% за 63д", "tier": "A",
       "weight": 0.3333333333333333,
       "mechanism": "Слабый рубль → рублёвая выручка 54% индекса",
       "spark": [[date, z], …]}
    ],
    "series": [["2004-01-31", 0.4], …],
    "health": {"ic_24m": 0.18, "n": 24, "status": "ok|warn|dead"}
  },
  "states": {
    "current": {"trend": 0, "vol": 1, "bond": 1, "rate_phase": -1,
                "since": {"trend": "2026-04-13", "vol": "2026-06-19", "bond": "2026-06-08"}},
    "distances": [{"id": "bond", "text": "RGBI −4.0% от максимума; флаг снимется при −3.9%",
                   "value": -4.0, "threshold": -3.9, "gap_pct": 0.1}],
    "active_signals": [{"id": "mom63", "label": "Трендследование", "value": -0.12,
                        "asof": "2026-08-11", "lag_days": 0,
                        "verdict": "против лонга", "why": "в стрессе IC +0.41"}],
    "cells": [{"code": "bull|calm|ok", "mean_fwd1m_pct": 0.93, "n": 110, "hit": 0.59}],
    "series": [["2004-01-31", "bear|calm|ok"], …]
  },
  "monitors": [
    {"id": "orfr", "title": "Потоки ОРФР", "tier": "monitor", "status": "ok",
     "asof": "2026-07", "fetched_at": "…", "headline": "ДУ −37.9 млрд (рекорд)",
     "payload": {…}, "note": "…"}
  ],
  "sources": {"iss": {"asof": "2026-08-11", "fetched_at": "…", "status": "ok", "lag_min": 12}},
  "events": [{"ts": "2026-08-11T16:05:00Z", "kind": "state_change|core_flip|cb|source",
              "severity": "info|warn", "text": "…"}]
}
```

Четыре правила чтения, которые фронт обязан соблюдать (каждое — след разобранного дефекта):

- **`core.value` — дневное число, `core.month_end` — решение.** Модель месячная (REGIME §6), внутримесячное значение дрожит каждый день. Показывать оба: крупно дневное, рядом якорь закрытого месяца.
- **`core.legs_segments` / `full_legs_since` — состав ядра во времени.** До 2017 композит держался на одной ноге (slope с 2015, бочка с 2016; REGIME §4, сольный период p=0,12) — 155 из 272 месяцев серии. Без пометки на графике одноногая эра неотличима от трёхногой, а именно в ней стоят все значения, упёртые в обрезку ±3.
- **`states.distances[bond]` — обычные проценты, бит — логарифмические.** `value`/`threshold` уже переведены через `exp(x)−1`; конвертация парная, поэтому момент переключения флага не сдвигается. Порог в log-мере (−4%) живёт только в `constants.STATE_RULES` — на нём стоят `CELL_STATS`, и менять его нельзя.
- **`states.active_signals[].asof` / `lag_days` — возраст показанного числа.** Часть рядов структурно отстаёт (позиция физлиц с бесплатного ISS — ~14 дней, плюс до 3 торговых дней протяжки ffill). Вердикт по возрасту НЕ гасится (SLA из реестра меряет свежесть выкачки, а не возраст данных); фронт обязан показать дату, при большом `lag_days` — бейджем, как на тайлах мониторов.

Дополнительные объекты: `history/daily.json` (`{"imoex": [[d,v]…], "core": …, "rgbi": …, "states": …}`, прореженно до 2004), `history/monitors.json`.

## 4. Модули пайплайна

```
pipeline/
  run.py                 # CLI: --mode intraday|daily|weekly|monthly|event|bootstrap
  lib/{http,store,dates,calc,constants,registry,r2,lease,telegram}.py
  fetch/{iss,cbr,minfin,rosstat,investfunds,polymarket,orfr,moex_press}.py
  compute/{panel,core,states,monitors,health}.py
  publish.py  alerts.py
```

Контракты:
```python
# fetch/*: каждая функция -> (series_id, {date: value}, meta) либо кидает FetchError
# compute/panel.py
build_panel(store) -> {"dates": [YYYY-MM-DD…], "cols": {"usd_mom63": [float|None…], …}}
# compute/core.py
compute_core(panel) -> {"value","sign","sign_since","components":[…],"series":[…],"health":{…}}
# compute/states.py
compute_states(panel) -> {"current":{…},"since":{…},"distances":[…],"active_signals":[…],"series":[…]}
# compute/monitors.py
build_monitors(store) -> [ {…tile…} ]
# publish.py
publish(payload, mode) -> None    # лиз → PUT data.json → PUT history/* → heartbeat
```

## 5. Лиз (single-writer)

Объект `lease.json`: `{"writer":"vps|gha","holder_id":"…","heartbeat":"ISO","ttl_seconds":5400}`.
Правила: VPS пишет всегда и обновляет heartbeat. GHA-фолбэк публикует, только если `now - heartbeat > ttl` ИЛИ `writer == "gha"`; при возвращении VPS перехватывает лиз (пишет `writer:"vps"`). Никаких блокировок: разрешение конфликта по приоритету VPS.

## 6. Телеграм-события (`alerts.py`)

Только переходы, дедуп по ключу в состоянии: `core_flip`, `state_cell_change`, `bond_flag_on/off`, `buy_window_open` (vol=1 & bond=0), `cb_decision` (сюрприз/в линию), `cb_reminder` (за день), `orfr_published`, `auction_failed`, `deposit_uptick`, санитарные (`source_stale`, `lease_lost`, `health_dead`).

## 7. Статусы источников

`ok` — свежее; `stale` — старше SLA (в `registry`); `missing` — нет ни одной точки; `error` — последняя попытка упала (данные из кэша). Фронт: `stale/error` — жёлтый бейдж на тайле, `missing` — серый.
