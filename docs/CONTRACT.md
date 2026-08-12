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

`SERIES` — словарь: ключ и есть `id`. Поля записи: `fetcher` (`модуль.функция`), `args` (что передать фетчеру), `cadence` (`daily|weekly|decade|monthly|event`), `pub_lag_days` (сколько добавить к дате периода, чтобы получить дату доступности), `sla` (профиль свежести из `lib/sla.py`; `None` — ряд без обещания), `required` (bool — падение ряда роняет прогон), `role` (`core|state|signal|monitor`), `label` (подпись для человека).

Ряды по ролям (34 записи, `role` — то, ЧТО ряд делает; `required` — ЧТО будет, если он пропадёт; это разные вещи, и совпадают они не всегда):

* **core (4)** — ноги композита и его вход: `imoex`, `urals_tax`, `usd_cbr`, `zcyc`;
* **state (3)** — машина состояний: `imoex_value`, `key_rate`, `rgbi`;
* **signal (6)** — второй слой: `brent`, `brent_moex`, `cny_tom`, `deposit_decade`, `mcftr`, `rtsi`;
* **monitor (21)** — витрина третьего слоя, в решение не входят: `breadth`, `budget_deficit`, `cb_consensus`, `cny_cbr`, `cpi_monthly`, `cpi_weekly`, `dividends`, `events_registry`, `futoi_mx`, `gld_tom`, `imoex2`, `lqdt_aum`, `mcxsm`, `moex_retail`, `ofz_auctions`, `orfr_flows`, `polymarket_ceasefire`, `rucbcpns_yield`, `rucbhycp_yield`, `rusfar3m`, `rvi`.

`required=true` стоит у шести: `imoex`, `key_rate`, `mcftr`, `rgbi`, `usd_cbr`, `zcyc`.

Режимы прогона (`registry.MODES`) — какие ряды тянет каждый такт: `intraday` (9 рядов), `daily` (23), `weekly` (4), `monthly` (6), `manual` (2). Ряд, не попавший ни в один режим, не обновляется никогда и при этом выглядит исправным — за этим следит `ops/check_schedule.py` в CI.

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
    "cell_stats": {"mean_fwd1m_pct": -2.94, "hit": 0.56, "n": 25,
                   "median_fwd1m_pct": 0.64, "worst_pct": -30.0, "best_pct": 18.0},
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
    "health": {"ic_24m": 0.18, "n": 24, "status": "ok|warn|dead",
               "window_months": 24, "coverage": 1.0, "months_total": 320,
               "below_zero_months": 0, "below_since": null,
               "review_months": 6, "review_due": false,
               "sign_since": "2026-06-30", "sign_age_days": 43,
               "asof_month": "2026-06-30",
               "series": [["2006-01-31", 0.31], …],
               "note": "ранговый IC за 24 мес: +0.04 — слабо…"}
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
  "quotes": {"imoex": {"value": 2301.43, "chg_pct": -0.96, "asof": "2026-08-12",
                       "updatetime": "19:00:11", "intraday": true, "delay_min": 0,
                       "age_min": 0.0, "instrument": "IMOEX", "label": "Индекс МосБиржи"}},
  "events": [{"ts": "2026-08-11T16:05:00Z", "kind": "state_change|core_flip|cb",
              "severity": "info|warn", "text": "…", "comment": "…|null"}]
}
```

Четыре правила чтения, которые фронт обязан соблюдать (каждое — след разобранного дефекта):

- **`core.value` — дневное число, `core.month_end` — решение.** Модель месячная (REGIME §6), внутримесячное значение дрожит каждый день. Показывать оба: крупно дневное, рядом якорь закрытого месяца.
- **`core.legs_segments` / `full_legs_since` — состав ядра во времени.** До 2017 композит держался на одной ноге (slope с 2015, бочка с 2016; REGIME §4, сольный период p=0,12) — 155 из 272 месяцев серии. Без пометки на графике одноногая эра неотличима от трёхногой, а именно в ней стоят все значения, упёртые в обрезку ±3.
- **`states.distances[bond]` — обычные проценты, бит — логарифмические.** `value`/`threshold` уже переведены через `exp(x)−1`; конвертация парная, поэтому момент переключения флага не сдвигается. Порог в log-мере (−4%) живёт только в `constants.STATE_RULES` — на нём стоят `CELL_STATS`, и менять его нельзя.
- **`quotes` — живая котировка, а НЕ вход модели.** Блок пишется интрадей-тактом отдельно от рядов и нужен шапке: `value`/`chg_pct` показывают, где рынок прямо сейчас. Ядро, состояния и сигналы считаются по `core`/`states` — то есть по закрытым дням. `intraday:false` означает, что источник отдал последнее закрытие вместо текущей цены, и подпись обязана это сказать.
- **`core.health.series` — витрина, `ic_24m` — число.** Серия обрезана слева 2004 годом (окно валидации), `ic_24m` считается по хвосту пар и от обрезки не зависит. `below_zero_months` считает подряд идущие ОТРИЦАТЕЛЬНЫЕ точки серии с конца; `review_due` — только достигнутый порог здоровья (`review_months`), а не решение о пересмотре: вторую половину условия (механизм у кандидата) панель измерить не может (ARCHITECTURE §7).
- **`states.active_signals[].asof` / `lag_days` — возраст показанного числа.** Часть рядов структурно отстаёт (позиция физлиц без подписки — ~14 дней, плюс до 3 торговых дней протяжки ffill; с ключом ALGOPACK этот ряд приходит текущим днём, и `lag_days` схлопывается сам). Вердикт по возрасту НЕ гасится (SLA из реестра меряет свежесть выкачки, а не возраст данных); фронт обязан показать дату, при большом `lag_days` — бейджем, как на тайлах мониторов.

Дополнительные объекты: `history/daily.json` (`{"imoex": [[d,v]…], "core": …, "rgbi": …, "states": …}`, прореженно до 2004), `history/monitors.json`.

## 4. Модули пайплайна

```
pipeline/
  run.py                 # CLI: --mode intraday|daily|weekly|monthly|event|bootstrap
  lib/{http,store,dates,calc,constants,registry,r2,lease,telegram,nexus,commentary}.py
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

## 6. События (`alerts.py`)

Только переходы, дедуп по ключу в состоянии: `core_flip`, `state_cell_change`, `bond_flag_on/off`, `buy_window_open` (vol=1 & bond=0), `cb_decision` (сюрприз/в линию), `cb_reminder` (за день), `orfr_published`, `auction_failed`, `deposit_uptick`, санитарные (`source_stale`, `lease_lost`, `health_dead`, `health_review_due`).

**Два рода событий, и они не смешиваются.** Рыночные (`core_flip`, `state_cell_change`, `bond_flag_*`, `buy_window_open`, `cb_*`, `orfr_published`, `auction_failed`, `deposit_uptick`) идут в ленту: журнал витрины, хаб NEXUS, телеграм-канал панели. Санитарные — `OPS_KINDS` в `alerts.py` (`source_stale`, `health_dead`, `health_review_due`, `lease_lost`, `payload_oversize`, `core_missing`) — идут ТОЛЬКО в общий ops-канал панелей (`ERROR_BOT_TOKEN`/`ERROR_CHAT_ID`, тот же бот, что у `dash-notify` на VPS) и в `events` витрины не попадают. Причина: журнал читают как ленту рынка, а «источник отдаёт 503» рынку ничего не сообщает — вперемешку они гасят друг друга.

**Комментарий (`comment`)** — разбор события бесплатной моделью через OpenRouter (`lib/commentary.py`). Проставляется до отправки и уезжает одинаковым во все три места; в телеграме и в хабе — отдельным абзацем после «💬», как у 837/838. Отсутствие комментария (нет ключа, лежит провайдер) — законный режим: событие уходит голым фактом.

Каналов доставки два, событие одно и то же:

- **телеграм** (`lib/telegram.py`) — дедуп маркером в `STATE_DIR/notify/*.json`;
- **лента хаба NEXUS** (`lib/nexus.py`) — POST `{source:"842", text, eventId, occurredAt}` на `NEXUS_EVENTS_URL` с `Authorization: Bearer NEXUS_INGEST_TOKEN`; дедуп на стороне хаба по паре (`source`, `eventId`), где `eventId` — тот же ключ события. Первая фраза текста становится заголовком ленты, остаток — подписью.

Событие считается доставленным, только когда прошли **оба** канала; недоставленное лежит в `pending` и повторяется до суток. Повтор безопасен: телеграм отвечает `DUP`, хаб гасит дубль по `eventId`. Незаполненные `NEXUS_*` — законный режим «зеркала нет»: канал возвращает `OFF` и доставку не блокирует.

## 7. Статусы источников

`ok` — свежее; `stale` — старше SLA (в `registry`); `missing` — нет ни одной точки; `error` — последняя попытка упала (данные из кэша). Фронт: `stale/error` — жёлтый бейдж на тайле, `missing` — серый.
