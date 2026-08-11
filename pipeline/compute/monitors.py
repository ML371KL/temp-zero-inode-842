"""Слой 3: тайлы мониторинга (docs/CONTRACT.md §3, REGIME.md §6).

ПОЧЕМУ это отдельный слой без скоринга: валидация показала, что эти ряды либо не
имеют доказанной предиктивности (мало истории/событий — ОРФР, Polymarket, ротация
из фондов денежного рынка), либо ОПРОВЕРГНУТЫ как предикторы акций (недельный ИПЦ,
уровень RVI — VALIDATION.md §5). Их место — интерпретация и накопление истории,
поэтому каждый тайл несёт свой тир из constants.MONITOR_TIERS и явную пометку из
TIER_NOTES, а не вес в модели.

ПОЧЕМУ каждый тайл строится в своём try: отказ одного источника не имеет права
уронить прогон (CONTRACT §0). Тайл деградирует до status=missing/stale/error,
публикация идёт дальше — на панели это жёлтый/серый бейдж, а не пустая страница.
"""

import math
from datetime import date, datetime, timedelta, timezone

from pipeline.lib import calc, registry
from pipeline.lib.constants import (
    CB_MEETINGS_2026,
    MONITOR_TIERS,
    MSK_OFFSET_HOURS,
    SEP_NODE,
    SLA_MINUTES,
    TIER_NOTES,
)

TITLES = {
    "orfr": "Потоки ОРФР",
    "lqdt": "Фонды денежного рынка",
    "deposit_spread": "Вклады против дивидендов",
    "dividends": "Дивидендный календарь",
    "cb_meeting": "Заседание ЦБ",
    "cpi_weekly": "Недельная инфляция",
    "ofz_auctions": "Аукционы ОФЗ",
    "polymarket": "Вероятность перемирия",
    "futoi": "Позиции физлиц во фьючерсе",
    "rvi": "Индекс волатильности RVI",
    "rub_barrel": "Рублёвая бочка",
    "sep_node": "Бюджетный узел (сентябрь)",
    "breadth": "Ширина рынка",
    "mcxsm": "Малые каппы против индекса",
    "hy_spread": "Спред ВДО",
}

# Обязательная пометка для тира dead: тайл остаётся на панели как контекст, но
# читатель обязан видеть, что как предиктор акций он опровергнут (VALIDATION §5).
DEAD_MARK = "как предиктор акций опровергнуто"

# Бюджет-2026 сходится примерно при 5440 ₽ за баррель (Urals ~$59 × ~92 ₽/$).
# Это не рыночная величина, а точка безубыточности бюджета — от неё считаем гэп.
BUDGET_BARREL_RUB = 5440.0
# Дисконт Urals к Brent, если посчитать по факту не из чего (в 2026 ходил 10–15%).
FALLBACK_URALS_DISCOUNT = 0.88
# Доля дивидендов, возвращающаяся в рынок. ЦБ оценивал 40–60% — берём середину.
# Это ДОПУЩЕНИЕ, а не измерение, поэтому вынесено в константу и названо в тайле.
REINVEST_SHARE = 0.5
# СЧА фондов денежного рынка ниже пика на столько — считаем, что ротация пошла.
# Событие не наступало ни разу (VALIDATION §6), порог — гипотеза для первого раза.
ROTATION_DD_PCT = -10.0
# Сколько дней после заседания консенсус этого заседания ещё годится для вердикта
# о сюрпризе. Ставка приходит в ряд на 1–3 рабочих дня позже решения (16 из 17 смен
# с 2023 — ровно +3 дня), недели хватает с запасом; больше — и в «сюрприз» опять
# полезет позапрошлое заседание, ровно то, из-за чего убран фолбэк в _t_cb_meeting.
CB_DECISION_FRESH_DAYS = 7

MONTHS_RU = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
             "августа", "сентября", "октября", "ноября", "декабря"]
MONTHS_RU_NOM = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль",
                 "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]

_STATUS_RANK = {"ok": 0, "stale": 1, "error": 2, "missing": 3}


# ------------------------------------------------------------------ утилиты

def _load(store, sid):
    """Чтение ряда из стора. Битый файл соседнего модуля не имеет права уронить тайл."""
    try:
        obj = store.load_series(sid)
    except Exception:  # noqa: BLE001 — граница изоляции: любой сбой стора = «ряда нет»
        return None
    return obj if isinstance(obj, dict) else None


def _points(obj):
    pts = (obj or {}).get("points") or {}
    if not isinstance(pts, dict):
        return []
    out = [(d, float(v)) for d, v in pts.items()
           if isinstance(d, str) and isinstance(v, (int, float))]
    out.sort()  # ключи ISO — лексикографическая сортировка совпадает с хронологической
    return out


def _ser(store, sid):
    """(точки по возрастанию даты, meta) — пустые, если ряда нет."""
    obj = _load(store, sid)
    if obj is None:
        return [], {}
    return _points(obj), (obj.get("meta") or {})


def _sub(store, ids, keys=()):
    """Подряд: сначала отдельные series_id из списка кандидатов, потом словарь в точке.

    Имена подрядов в реестре и у фетчеров разошлись (registry описывает futoi_mx с
    subkeys, а iss.futoi пишет futoi_mx_fiz_pos), поэтому кандидаты перечисляются
    явно и в порядке предпочтения — то же решение, что в compute/panel.py.
    """
    for sid in ids:
        obj = _load(store, sid)
        pts = (obj or {}).get("points") or {}
        if not isinstance(pts, dict) or not pts:
            continue
        meta = obj.get("meta") or {}
        sample = next(iter(pts.values()), None)
        if isinstance(sample, dict):
            for key in keys:
                if key in sample:
                    return (sorted((d, float(v[key])) for d, v in pts.items()
                                   if isinstance(v, dict)
                                   and isinstance(v.get(key), (int, float))), meta)
            continue
        if not (registry.SERIES.get(sid) or {}).get("subkeys"):
            # Плоский ряд принимаем, только если реестр не обещал подключей: иначе
            # «zcyc» со скалярами раздал бы одну кривую всем срокам.
            return _points(obj), meta
    return [], {}


def _parse_ts(s):
    if not isinstance(s, str) or not s.strip():
        return None
    try:
        dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _age_min(ts, now):
    dt = _parse_ts(ts)
    if dt is None:
        return None
    return (now - dt).total_seconds() / 60.0


def _st(sid, pts, meta, now):
    """Статус ряда по CONTRACT §7: missing → error → stale → ok.

    pts нужен только как признак «данные есть» — сгодится любая непустая
    последовательность (для рядов, собранных из нескольких подрядов).
    """
    if not pts:
        return "missing"
    declared = (meta or {}).get("status")
    if declared == "error":
        return "error"
    spec = registry.SERIES.get(sid)
    if spec is None:
        # Подряд вида futoi_mx_fiz_pos в реестре не описан — SLA берём у базового ряда.
        base = max((k for k in registry.SERIES if sid.startswith(k)), key=len, default=None)
        spec = registry.SERIES.get(base) or {}
    sla_key = spec.get("sla")
    sla = SLA_MINUTES.get(sla_key) if sla_key else None
    age = _age_min((meta or {}).get("fetched_at"), now)
    if sla and age is not None and age > sla:
        return "stale"
    if declared in _STATUS_RANK:
        return declared
    return "ok"


series_status = _st          # публичное имя для run.py (сводка по источникам)
series_points = _ser         # публичное имя: (точки, meta) без падений на битом сторе


def _worst(*statuses):
    known = [s for s in statuses if s in _STATUS_RANK]
    if not known:
        return "missing"
    return max(known, key=lambda s: _STATUS_RANK[s])


def _note(tid, extra=None):
    tier = MONITOR_TIERS.get(tid, "monitor")
    note = " ".join(p for p in (extra, TIER_NOTES.get(tier)) if p).strip()
    if tier == "dead" and DEAD_MARK not in note.lower():
        note = (note + " — " + DEAD_MARK).strip(" —")
    return note


def _tile(tid, status, asof, headline, payload, extra_note=None, fetched_at=None):
    return {
        "id": tid,
        "title": TITLES.get(tid, tid),
        "tier": MONITOR_TIERS.get(tid, "monitor"),
        "status": status,
        "asof": asof,
        "fetched_at": fetched_at,
        "headline": headline,
        "payload": payload or {},
        "note": _note(tid, extra_note),
    }


def _empty(tid, extra_note=None, headline="нет данных"):
    return _tile(tid, "missing", None, headline, {}, extra_note)


def _last(pts):
    return pts[-1] if pts else (None, None)


def _val_back(pts, n):
    """Значение на n точек назад (не «n дней»: у рядов свои торговые календари)."""
    return pts[-1 - n][1] if len(pts) > n else None


def _chg(pts, n):
    v0, v1 = _val_back(pts, n), (pts[-1][1] if pts else None)
    if v0 is None or v1 is None:
        return None
    return v1 - v0


def _chg_pct(pts, n):
    v0, v1 = _val_back(pts, n), (pts[-1][1] if pts else None)
    if v0 in (None, 0) or v1 is None:
        return None
    return (v1 / v0 - 1.0) * 100.0


def _pct_last(vals, window):
    """Перцентиль последнего значения в окне (средний ранг для связей).

    Средний ранг, а не «доля значений ≤ x»: на неподвижном ряде нестрогое сравнение
    даёт ложные 0/100 перцентили — эти грабли уже ловили в соседнем проекте (841).
    """
    xs = [v for v in vals[-window:] if isinstance(v, (int, float))]
    if len(xs) < 20:
        return None
    x = xs[-1]
    less = sum(1 for u in xs if u < x)
    eq = sum(1 for u in xs if u == x)
    return (less + (eq + 1) / 2.0) / len(xs) * 100.0


def _r(v, d=1):
    return None if v is None else round(float(v), d)


def _n(v, d=1, plus=False):
    """Число для заголовка: неразрывные пробелы между разрядами, «н/д» вместо None."""
    if v is None:
        return "н/д"
    s = f"{v:{'+' if plus else ''},.{d}f}".replace(",", " ")
    return s


def _msk_now(now):
    return now + timedelta(hours=MSK_OFFSET_HOURS)


def _d(dstr):
    """Дата из ключа ряда: «2026-07» (месячные) и «2026-07-31» оба валидны."""
    if not isinstance(dstr, str):
        return None
    s = dstr[:10]
    try:
        if len(s) == 7:
            return date.fromisoformat(s + "-01")
        return date.fromisoformat(s)
    except ValueError:
        return None


def _ddmm(dstr):
    d = _d(dstr)
    return d.strftime("%d.%m") if d else "н/д"


def _month_ru(dstr):
    d = _d(dstr)
    return f"{MONTHS_RU_NOM[d.month - 1]} {d.year}" if d else "н/д"


# ------------------------------------------------------------------- тайлы

def _t_orfr(store, now):
    """Стек потоков по категориям + метрика исчерпания продавца.

    Исчерпание продавца = 3-месячная сумма нетто-продаж НФО в доверительном
    управлении и её изменение к предыдущему кварталу. Как СИГНАЛ не доказано
    (5 событий, мощность околонулевая — VALIDATION §5.8), поэтому только цифры.
    """
    cats = [("fiz", "Физлица"), ("nfo_du", "НФО (ДУ)"), ("nfo_own", "НФО (свои)"),
            ("szko", "СЗКО"), ("other_banks", "Прочие банки"), ("nonres", "Нерезиденты")]
    stack, metas, months = {}, [], set()
    for key, _label in cats:
        pts, meta = _sub(store, (f"orfr_flows_{key}", f"orfr_{key}", "orfr_flows"), (key,))
        if pts:
            stack[key] = dict(pts)
            months.update(d for d, _ in pts)
            metas.append(meta)
    if not stack:
        return _empty("orfr", "Источник: обзор рисков финрынков ЦБ, лаг публикации ~15 дней.")

    order = sorted(months)[-18:]
    meta = metas[0]
    status = _st("orfr_flows", order, meta, now)
    # asof берём у САМОЙ СВЕЖЕЙ точки, а не из meta: фетчер ОРФР кладёт в meta.asof
    # период разобранного PDF (последний на сайте ЦБ — февральский), и подпись
    # «данные: фев 2026» вставала под июльскими цифрами в этом же тайле — тайл спорил
    # сам с собой на полгода. Данные — единственный честный источник своей даты.
    # Форма — полная дата конца месяца, как у остальных тайлов: один ярлык «данные:»
    # не должен в одной сетке означать то день, то месяц.
    asof = order[-1]

    du = [stack.get("nfo_du", {}).get(d) for d in sorted(months)]
    du = [v for v in du if v is not None]
    sum3 = sum(du[-3:]) if len(du) >= 3 else None
    prev3 = sum(du[-6:-3]) if len(du) >= 6 else None
    delta = (sum3 - prev3) if (sum3 is not None and prev3 is not None) else None
    if sum3 is None:
        exhaust = "истории ДУ меньше квартала"
    elif delta is None:
        exhaust = f"продажи ДУ за 3 мес {_n(sum3, 1, True)} млрд"
    elif sum3 < 0 and delta > 0:
        exhaust = (f"продажи ДУ за 3 мес {_n(sum3, 1, True)} млрд против "
                   f"{_n(prev3, 1, True)} кварталом ранее — давление слабеет")
    elif sum3 < 0:
        exhaust = (f"продажи ДУ за 3 мес {_n(sum3, 1, True)} млрд против "
                   f"{_n(prev3, 1, True)} кварталом ранее — давление растёт")
    else:
        exhaust = f"ДУ за 3 мес в плюсе {_n(sum3, 1, True)} млрд — продавец ушёл"

    last_by_cat = {k: stack.get(k, {}).get(order[-1]) for k, _ in cats if k in stack}
    payload = {
        "unit": meta.get("unit") or "млрд ₽",
        "months": order,
        "stack": {k: [stack[k].get(d) for d in order] for k in stack},
        "labels": {k: lbl for k, lbl in cats if k in stack},
        "last": {k: _r(v, 1) for k, v in last_by_cat.items()},
        "seller_exhaustion": {"sum_3m_nfo_du": _r(sum3, 1), "prev_3m": _r(prev3, 1),
                              "delta": _r(delta, 1), "text": exhaust},
    }
    du_last = last_by_cat.get("nfo_du")
    fiz_last = last_by_cat.get("fiz")
    headline = (f"{_month_ru(order[-1])}: ДУ {_n(du_last, 1, True)} млрд, "
                f"физлица {_n(fiz_last, 1, True)} млрд")
    return _tile("orfr", status, asof, headline, payload,
                 "Данные ОРФР качественные, но история короткая: «исчерпание продавца» — "
                 "наблюдение, а не сигнал (мощность теста околонулевая).",
                 meta.get("fetched_at"))


def _t_lqdt(store, now):
    pts, meta = _ser(store, "lqdt_aum")
    if not pts:
        return _empty("lqdt", "Ротация из фондов денежного рынка в акции не случалась ни разу.")
    status = _st("lqdt_aum", pts, meta, now)
    asof, aum = pts[-1]
    peak = max(v for _, v in pts[-500:])
    dd = (aum / peak - 1.0) * 100.0 if peak else None
    rotation = bool(dd is not None and dd <= ROTATION_DD_PCT)
    payload = {
        "unit": meta.get("unit") or "млрд ₽",
        "aum": _r(aum, 1),
        "flow_1d": _r(_chg(pts, 1), 1),
        "flow_5d": _r(_chg(pts, 5), 1),
        "flow_21d": _r(_chg(pts, 21), 1),
        "peak": _r(peak, 1),
        "dd_from_peak_pct": _r(dd, 1),
        "rotation_started": rotation,
        "rotation_threshold_pct": ROTATION_DD_PCT,
        "series": [[d, _r(v, 1)] for d, v in pts[-120:]],
    }
    tail = ("СЧА ниже пика на "
            f"{_n(abs(dd or 0), 1)}% — похоже на первую ротацию" if rotation
            else "большой ротации ещё не случалось")
    # Дневной поток есть не всегда (у затравки одна точка): «за день н/д млрд» —
    # это мусор в заголовке, лучше честно опустить кусок фразы.
    day_chg = _chg(pts, 1)
    chg_txt = f", за день {_n(day_chg, 1, True)} млрд" if day_chg is not None else ""
    headline = f"СЧА {_n(aum, 0)} млрд{chg_txt}; {tail}"
    return _tile("lqdt", status, asof, headline, payload,
                 "Индикатор ждёт первого срабатывания: перетока денег из фондов ликвидности "
                 "в акции не было ни разу, проверить его на истории невозможно.",
                 meta.get("fetched_at"))


def _dy_trail(store):
    """Трейлинг-дивдоходность = 252-дневная разница лог-доходностей MCFTR и IMOEX.

    Реконструкция из VALIDATION §A3: уровни сверены с фактом (медиана 5,5% в 2015–21).
    Считаем по общим датам двух рядов — иначе разъехавшийся торговый календарь даёт
    скачок доходности на пустом месте.
    """
    m = dict(_ser(store, "mcftr")[0])
    i = dict(_ser(store, "imoex")[0])
    common = sorted(set(m) & set(i))
    if len(common) < 253:
        return None, None
    d1, d0 = common[-1], common[-253]
    if min(m[d1], m[d0], i[d1], i[d0]) <= 0:
        return None, None
    return (math.log(m[d1] / m[d0]) - math.log(i[d1] / i[d0])) * 100.0, d1


def _t_deposit_spread(store, now):
    dep_pts, dep_meta = _ser(store, "deposit_decade")
    dy, dy_asof = _dy_trail(store)
    if not dep_pts and dy is None:
        return _empty("deposit_spread")
    dep_asof, dep = _last(dep_pts)
    spread = (dy - dep) if (dy is not None and dep is not None) else None
    status = _st("deposit_decade", dep_pts, dep_meta, now)
    if dy is None:
        status = _worst(status, "stale")
    payload = {
        "deposit_pct": _r(dep, 2),
        "deposit_asof": dep_asof,
        "dy_trail_pct": _r(dy, 2),
        "dy_asof": dy_asof,
        "spread_pp": _r(spread, 2),
        "deposit_chg_pp": _r(_chg(dep_pts, 1), 2),
        "ever_positive": bool(spread is not None and spread > 0),
        "series": [[d, _r(v, 2)] for d, v in dep_pts[-72:]],
    }
    if spread is None:
        headline = f"Вклады {_n(dep, 1)}%, дивдоходность не посчитана"
    elif spread > 0:
        headline = (f"Дивдоходность {_n(dy, 1)}% выше вкладов {_n(dep, 1)}% "
                    f"на {_n(spread, 1)} п.п. — событие впервые в истории")
    else:
        headline = (f"Вклады {_n(dep, 1)}% против дивидендов {_n(dy, 1)}%: "
                    f"спред {_n(spread, 1)} п.п. не в пользу акций")
    return _tile("deposit_spread", status, dep_asof or dy_asof, headline, payload,
                 "Как сигнал спред работает только в фазе смягчения ставки (IC +0,26); "
                 "устойчивого «спред > 0» на истории не наступало ни разу.",
                 dep_meta.get("fetched_at"))


def _t_dividends(store, now):
    pts, meta = _ser(store, "dividends")
    items = meta.get("items")
    today = _msk_now(now).date().isoformat()
    rows = []
    if isinstance(items, list):
        for it in items:
            if not isinstance(it, dict):
                continue
            ex = str(it.get("ex_date") or it.get("date") or "")[:10]
            if not ex:
                continue
            rows.append({"ticker": it.get("ticker") or it.get("name") or "?",
                         "ex_date": ex,
                         "yield_pct": _r(it.get("yield_pct"), 2),
                         "amount_bn": _r(it.get("amount_bn"), 1)})
    else:
        # Фолбэк: без meta.items ряд несёт только суммы/доходности по датам отсечек.
        for d, v in pts:
            rows.append({"ticker": "?", "ex_date": d[:10], "yield_pct": None, "amount_bn": _r(v, 1)})
    if not rows:
        return _empty("dividends", "Источник — inputs/dividends.yml (ручной ввод).")

    upcoming = sorted((r for r in rows if r["ex_date"] >= today), key=lambda r: r["ex_date"])
    horizon = (_msk_now(now).date() + timedelta(days=90)).isoformat()
    window = [r for r in upcoming if r["ex_date"] <= horizon]
    amounts = [r["amount_bn"] for r in window if r["amount_bn"] is not None]
    # Сумма выплат остаётся None, если в календаре нет ни одной суммы: «0 млрд»
    # на панели читается как «дивидендов не будет», а это неправда.
    total = sum(amounts) if amounts else None
    share = meta.get("reinvest_share")
    share = float(share) if isinstance(share, (int, float)) else REINVEST_SHARE
    payload = {
        "upcoming": upcoming[:8],
        "sum_90d_bn": _r(total, 1),
        "reinvest_est_bn": _r(total * share, 1) if total is not None else None,
        "reinvest_share": share,
        "horizon_to": horizon,
    }
    # Календарь приходит из inputs/ вручную, SLA у него нет: протухшим считаем
    # ровно тот случай, когда впереди не осталось ни одной отсечки.
    status = "error" if meta.get("status") == "error" else "ok"
    if not upcoming:
        status = "stale"
        headline = "Ближайших отсечек в календаре нет — календарь пора обновить"
    else:
        nxt = upcoming[0]
        y = f" ({_n(nxt['yield_pct'], 1)}%)" if nxt["yield_pct"] is not None else ""
        money = (f"; за 90 дней {_n(total, 0)} млрд, реинвест ≈{_n(total * share, 0)} млрд"
                 if total is not None else "; сумм выплат в календаре нет")
        headline = f"Ближайшая отсечка: {nxt['ticker']} {_ddmm(nxt['ex_date'])}{y}{money}"
    return _tile("dividends", status, meta.get("asof") or (upcoming[0]["ex_date"] if upcoming else None),
                 headline, payload,
                 f"Оценка реинвеста — допущение: возвращается {int(share * 100)}% выплат "
                 "(диапазон оценок ЦБ 40–60%), это не измеренная величина.",
                 meta.get("fetched_at"))


def _t_cb_meeting(store, now):
    today = _msk_now(now).date()
    future = [m for m in CB_MEETINGS_2026 if _d(m) and _d(m) >= today]
    nxt = future[0] if future else None
    key_pts, key_meta = _ser(store, "key_rate")
    cons_pts, cons_meta = _ser(store, "cb_consensus")
    rus_pts, rus_meta = _ser(store, "rusfar3m")
    key_asof, key_rate = _last(key_pts)
    cons_map = dict(cons_pts)
    # Консенсус — ТОЛЬКО по точному совпадению с датой заседания. Фолбэк на последнюю
    # точку ряда брал число уже ПРОШЕДШЕГО заседания и печатал его как прогноз на
    # ближайшее (июль-2026: 16,00% выдавалось за сентябрьский консенсус при пустой
    # строке в inputs/consensus.yml). Сам файл предписывает обратное: «пустая строка
    # честнее вымышленной», а строки прошедших заседаний из него не удаляют — то есть
    # фолбэку всегда есть за что зацепиться, и он никогда не молчит.
    cons = cons_map.get(nxt) if nxt else None
    # Отдельная величина для алерта о решении: новая ставка попадает в ряд key_rate не
    # в день заседания, а на 1–3 рабочих дня позже (16 из 17 смен с 2023 — ровно +3 дня:
    # 24.07 → 27.07, 19.06 → 22.06). К этому моменту nxt — уже СЛЕДУЮЩЕЕ заседание,
    # поэтому сюрприз надо считать от консенсуса только что прошедшего, и только пока
    # оно свежее недели: иначе старое заседание опять начнёт подставляться.
    past = [m for m in CB_MEETINGS_2026 if _d(m) and _d(m) <= today]
    last_meeting = past[-1] if past else None
    last_fresh = bool(last_meeting and (today - _d(last_meeting)).days <= CB_DECISION_FRESH_DAYS)
    last_cons = cons_map.get(last_meeting) if last_fresh else None
    rus_asof, rusfar = _last(rus_pts)
    spread = (rusfar - key_rate) if (rusfar is not None and key_rate is not None) else None
    days = (_d(nxt) - today).days if nxt else None
    delta_bp = round((cons - key_rate) * 100) if (cons is not None and key_rate is not None) else None

    if spread is None:
        priced = "RUSFAR не получен — что в цене, сказать нельзя"
    elif spread < -0.15:
        priced = f"RUSFAR 3M ниже ключа на {_n(abs(spread), 2)} п.п. — рынок закладывает снижение"
    elif spread > 0.15:
        priced = f"RUSFAR 3M выше ключа на {_n(spread, 2)} п.п. — рынок закладывает ужесточение"
    else:
        priced = "RUSFAR 3M у ключа — снижение НЕ в цене"

    payload = {
        "next_meeting": nxt,
        "days_left": days,
        "key_rate": _r(key_rate, 2),
        "key_rate_asof": key_asof,
        "consensus": _r(cons, 2),
        "consensus_delta_bp": delta_bp,
        "consensus_source": cons_meta.get("source") or cons_meta.get("note"),
        "last_meeting": last_meeting if last_fresh else None,
        "last_consensus": _r(last_cons, 2),
        "rusfar3m": _r(rusfar, 2),
        "rusfar_asof": rus_asof,
        "spread_pp": _r(spread, 2),
        "priced_text": priced,
        "calendar": CB_MEETINGS_2026,
    }
    status = _st("key_rate", key_pts, key_meta, now)
    if nxt is None:
        status = _worst(status, "stale")
    if rus_pts:
        status = _worst(status, _st("rusfar3m", rus_pts, rus_meta, now))
    # «консенсус н/д%» — мусор в заголовке: единица приклеена к отсутствующему числу.
    # Фронт в этом случае давно печатает «консенсус не внесён» — говорим то же самое.
    cons_txt = (f"консенсус {_n(cons, 2)}%" if cons is not None else "консенсус не внесён")
    if nxt is None:
        headline = f"Ключ {_n(key_rate, 2)}% — календарь заседаний закончился, нужен новый"
    elif days == 0:
        bp = f" ({_n(delta_bp, 0, True)} б.п.)" if delta_bp is not None else ""
        headline = f"Заседание сегодня. Ключ {_n(key_rate, 2)}%, {cons_txt}{bp}"
    else:
        headline = f"До заседания {_ddmm(nxt)} — {days} дн. Ключ {_n(key_rate, 2)}%, {cons_txt}"
    # asof тайла — дата, на которую известны ЕГО ЧИСЛА (ключ и RUSFAR), а не дата
    # заседания. Раньше в общий ярлык «данные:» уезжала дата из БУДУЩЕГО (11.09.2026):
    # у соседних тайлов тот же ярлык означает дату факта, а вдобавок asof из будущего
    # не может протухнуть по определению — индикатор свежести у тайла был мёртв.
    # Дата заседания живёт в payload.next_meeting и печатается отдельной строкой.
    data_asof = max([d for d in (key_asof, rus_asof) if d], default=None)
    return _tile("cb_meeting", status, data_asof, headline, payload,
                 "Сюрприз ЦБ против консенсуса — качественная правда о двух великих "
                 "разворотах (n=2), статистики за этим нет.",
                 key_meta.get("fetched_at") or rus_meta.get("fetched_at"))


def _t_cpi_weekly(store, now):
    pts, meta = _ser(store, "cpi_weekly")
    if not pts:
        return _empty("cpi_weekly")
    status = _st("cpi_weekly", pts, meta, now)
    asof, last = pts[-1]

    def _saar(weekly_pct_list):
        # Недельный прирост в годовые проценты: (1+w)^(365/7)−1. Грубо (без сезонности),
        # поэтому в тайле подписано «оценка».
        vals = [v for v in weekly_pct_list if v is not None]
        if not vals:
            return None
        w = sum(vals) / len(vals) / 100.0
        if 1.0 + w <= 0:
            return None
        return ((1.0 + w) ** (365.0 / 7.0) - 1.0) * 100.0

    last4 = [v for _, v in pts[-4:]]
    payload = {
        "prints": [[d, _r(v, 2)] for d, v in pts[-12:]],
        "last_pct": _r(last, 2),
        "saar_last_pct": _r(_saar([last]), 1),
        "saar_4w_pct": _r(_saar(last4), 1),
    }
    headline = (f"Неделя {_ddmm(asof)}: {_n(last, 2, True)}% "
                f"(SAAR-оценка по 4 неделям {_n(_saar(last4), 1)}%)")
    return _tile("cpi_weekly", status, asof, headline, payload,
                 "Пост-публикационный эффект для акций — ноль (знаменитый пре-дрейф оказался "
                 "артефактом остановки торгов в марте 2022); держим как вход в ожидания ставки.",
                 meta.get("fetched_at"))


def _t_ofz_auctions(store, now):
    pts, meta = _ser(store, "ofz_auctions")
    last_meta = meta.get("last") if isinstance(meta.get("last"), dict) else {}
    if not pts and not last_meta:
        return _empty("ofz_auctions")
    asof, placed = _last(pts)
    asof = last_meta.get("date") or asof
    placed = last_meta.get("placed_bn", placed)
    demand = last_meta.get("demand_bn")
    btc = last_meta.get("bid_to_cover")
    if btc is None and demand and placed:
        btc = demand / placed if placed else None
    failed = last_meta.get("failed")
    if failed is None:
        # Признаком провала считаем нулевое размещение или спрос ниже размещения:
        # Минфин отменяет аукцион, когда рынок требует премию, — это и есть сигнал.
        # ВАЖНО: отсутствие числа — не признак провала. В затравке у части дней
        # объём размещения пуст (в новостях его не назвали), и наивное «нет числа
        # → считаем нулём → аукцион провален» рисовало ложную тревогу на 05.08.2026.
        failed = bool((placed is not None and placed <= 0)
                      or (btc is not None and placed is not None and btc < 1.0))
    status = _st("ofz_auctions", pts, meta, now)
    payload = {
        "date": asof,
        "issue": last_meta.get("issue"),
        "placed_bn": _r(placed, 1),
        "demand_bn": _r(demand, 1),
        "bid_to_cover": _r(btc, 2),
        "premium_bp": last_meta.get("premium_bp"),
        "failed": bool(failed),
        "recent": [[d, _r(v, 1)] for d, v in pts[-12:]],
    }
    if failed and (placed is None or placed <= 0):
        # «размещено 0,0 млрд» крупным кеглем неотличимо от аукциона, где ноль реально
        # разместили, а при пустом объёме получалось «размещено н/д млрд» — единица,
        # приклеенная к отсутствующему числу. Провал по нулю называем словами.
        headline = f"Аукцион {_ddmm(asof)} не состоялся: размещения не было"
    elif failed:
        # Провал по спросу (bid-to-cover < 1) — здесь числа как раз содержательны.
        headline = (f"Аукцион {_ddmm(asof)} провален: размещено {_n(placed, 1)} млрд "
                    f"при спросе {_n(demand, 1)} млрд, bid-to-cover {_n(btc, 2)}")
    else:
        btc_txt = f", bid-to-cover {_n(btc, 2)}" if btc is not None else ""
        headline = (f"Аукцион {_ddmm(asof)}: размещено {_n(placed, 1)} млрд при спросе "
                    f"{_n(demand, 1)} млрд{btc_txt}")
    return _tile("ofz_auctions", status, asof, headline, payload,
                 "Провал аукциона читаем как индикатор фискальной премии в длинном конце, "
                 "а не как торговый сигнал для акций.",
                 meta.get("fetched_at"))


def _t_polymarket(store, now):
    pts, meta = _ser(store, "polymarket_ceasefire")
    if not pts:
        return _empty("polymarket")
    status = _st("polymarket_ceasefire", pts, meta, now)
    asof, p = pts[-1]
    # Ряд может приходить долей (0..1) или процентами — нормализуем к процентам.
    scale = 100.0 if max(v for _, v in pts[-60:]) <= 1.0 else 1.0
    prob = p * scale
    chg7 = (_chg(pts, 7) or 0.0) * scale if len(pts) > 7 else None
    chg30 = (_chg(pts, 30) or 0.0) * scale if len(pts) > 30 else None
    payload = {
        "prob_pct": _r(prob, 1),
        "chg_7d_pp": _r(chg7, 1),
        "chg_30d_pp": _r(chg30, 1),
        "question": meta.get("question") or meta.get("note"),
        "series": [[d, _r(v * scale, 1)] for d, v in pts[-120:]],
    }
    tail = f" ({_n(chg7, 1, True)} п.п. за неделю)" if chg7 is not None else ""
    headline = f"Перемирие: {_n(prob, 0)}%{tail}"
    return _tile("polymarket", status, asof, headline, payload,
                 "История с 2022 года и мало разрешившихся событий — проверить предиктивность "
                 "нечем; тайл нужен для чтения новостного фона.",
                 meta.get("fetched_at"))


def _t_futoi(store, now):
    pos, meta = _sub(store, ("futoi_mx_fiz_pos", "futoi_mx_pos", "futoi_mx"),
                     ("pos", "fiz_pos"))
    if not pos:
        return _empty("futoi")
    longs = dict(_sub(store, ("futoi_mx_fiz_long", "futoi_mx_long", "futoi_mx"),
                      ("pos_long", "fiz_pos_long"))[0])
    shorts = dict(_sub(store, ("futoi_mx_fiz_short", "futoi_mx_short", "futoi_mx"),
                       ("pos_short", "fiz_pos_short"))[0])
    hl = dict(_sub(store, ("futoi_mx_fiz_long_num", "futoi_mx_holders_long", "futoi_mx"),
                   ("pos_long_num",))[0])
    hs = dict(_sub(store, ("futoi_mx_fiz_short_num", "futoi_mx_holders_short", "futoi_mx"),
                   ("pos_short_num",))[0])
    asof, net = pos[-1]

    # Нормируем ровно как панель (compute/panel.py::_futoi_z120): нетто к брутто,
    # где брутто = long − short (short приходит отрицательным). Иначе тайл и сигнал
    # второго ряда показывали бы разные числа под одним названием.
    ratio = []
    for d, p in pos:
        a, b = longs.get(d), shorts.get(d)
        if a is not None and b is not None and (a - b):
            ratio.append((d, p / (a - b)))
    used = ratio if ratio else pos
    # z-120, а НЕ перцентиль-252: уровень нетто-доли физиков структурно уехал
    # с −0,4 (2020) на +0,57 (2025–26), перцентиль прижат к 1,0 и сигнала не несёт
    # (VALIDATION §B2, REGIME §5).
    z = calc.zscore_last([v for _, v in used], 120, min_periods=40)
    status = _st("futoi_mx_fiz_pos", pos, meta, now)
    payload = {
        "net": _r(net, 4),
        "net_share": _r(used[-1][1], 4) if ratio else None,
        "z120": _r(z, 2),
        "long": _r(longs.get(asof), 0),
        "short": _r(shorts.get(asof), 0),
        "holders_long": _r(hl.get(asof), 0),
        "holders_short": _r(hs.get(asof), 0),
        "series": [[d, _r(v, 4)] for d, v in used[-120:]],
        "norm": "z-120 по нетто/брутто" if ratio else "z-120 по нетто-позиции",
    }
    # ПОЧЕМУ вердикт сформулирован относительно нормы, а не «перегружены лонгом/шортом»:
    # z-120 мерит ОТКЛОНЕНИЕ доли от 120-дневной нормы, а не сам уровень. Уровень с 2025
    # структурно положительный, поэтому z=−2,93 стоял рядом с нетто-ЛОНГОМ +18 493
    # контракта в том же payload — заголовок «физики перегружены шортом» опровергался
    # числами тайла. Уровень теперь назван прямо, вердикт остаётся на z.
    if z is None:
        verdict = "z(120д) не посчитан — истории мало"
    else:
        if z >= 1.0:
            rel = "выше своей 120-дневной нормы — контрариан против роста"
        elif z <= -1.0:
            rel = "ниже своей 120-дневной нормы — контрариан за рост"
        else:
            rel = "у своей 120-дневной нормы"
        verdict = f"z(120д) {_n(z, 2, True)}: позиция {rel}"
    holders = ""
    if hl.get(asof) or hs.get(asof):
        holders = f"; держателей лонга {_n(hl.get(asof), 0)}, шорта {_n(hs.get(asof), 0)}"
    if net > 0:
        level = f"Физлица в нетто-лонге {_n(net, 0)}"
    elif net < 0:
        level = f"Физлица в нетто-шорте {_n(abs(net), 0)}"
    else:
        level = "Нетто-позиция физлиц нулевая"
    headline = f"{level}, {verdict}{holders}"
    return _tile("futoi", status, asof, headline, payload,
                 "Работает только в спокойном быке (IC −0,24); в медведе знак неустойчив. "
                 "Нормировка перцентилем-252 сломана структурным сдвигом — используем z-120.",
                 meta.get("fetched_at"))


def _t_rvi(store, now):
    pts, meta = _ser(store, "rvi")
    if not pts:
        return _empty("rvi")
    status = _st("rvi", pts, meta, now)
    asof, v = pts[-1]
    vals = [x for _, x in pts]
    pk = max(vals[-6:-1]) if len(vals) >= 6 else None
    # «Разворот с пика >50» — единственная устойчивая к параметрам конструкция (§B4),
    # но это post-hoc подвыборка: показываем как гипотезу под OOS, не как правило.
    reversal = bool(pk is not None and pk > 50 and v < pk * 0.9)
    payload = {
        "rvi": _r(v, 2),
        "chg_5d": _r(_chg(pts, 5), 2),
        "pct_3y": _r(_pct_last(vals, 756), 0),
        "peak_5d": _r(pk, 2),
        "peak_reversal": reversal,
        "series": [[d, _r(x, 2)] for d, x in pts[-120:]],
    }
    tail = " — разворот с пика >50 (гипотеза под OOS)" if reversal else ""
    headline = f"RVI {_n(v, 1)} ({_n(_pct_last(vals, 756), 0)}-й перцентиль за 3 года){tail}"
    # ПОЧЕМУ заметка стала точнее: опровергнут именно УРОВЕНЬ и «зоны» (VALIDATION §B4,
    # §5 «прочие нули»), а не тайл целиком. Разворот с пика >50 исследование относит
    # к уровню B и прямо просит держать в дашборде как гипотезу под OOS, а REGIME §5
    # оставляет RVI слабым сигналом второго ряда в медведе — прежняя формулировка
    # «опровергнуто» была категоричнее собственной валидации.
    return _tile("rvi", status, asof, headline, payload,
                 "Опровергнут УРОВЕНЬ и «зоны»: пересечение 50 — артефакт 2022 года. "
                 "Живой остаток один — разворот с пика >50 (VALIDATION §B4, уровень B, "
                 "гипотеза под OOS-проверку); в медвежьей фазе RVI даёт слабые +0,20 "
                 "(n=51, REGIME §5).",
                 meta.get("fetched_at"))


def _urals_discount(store):
    """Дисконт налоговой Urals к Brent по последним общим месяцам (медиана)."""
    u = _ser(store, "urals_tax")[0]
    b = _ser(store, "brent")[0]
    if not u or not b:
        return None
    by_month = {}
    for d, v in b:
        by_month.setdefault(d[:7], []).append(v)
    ratios = []
    for d, v in u[-6:]:
        vals = by_month.get(d[:7])
        if vals and sum(vals) > 0:
            ratios.append(v / (sum(vals) / len(vals)))
    if not ratios:
        return None
    ratios.sort()
    return ratios[len(ratios) // 2]


def _t_rub_barrel(store, now):
    u_pts, u_meta = _ser(store, "urals_tax")
    usd_pts, usd_meta = _ser(store, "usd_cbr")
    if not u_pts or not usd_pts:
        return _empty("rub_barrel", f"Бюджет-2026 сходится примерно при "
                                    f"{int(BUDGET_BARREL_RUB)} ₽ за баррель.")
    u_asof, urals = u_pts[-1]
    # Курс СРЕДНЕМЕСЯЧНЫЙ, а не последний день месяца: так определена налоговая бочка
    # в валидации (VALIDATION §A2) и ровно так её считает панельный сигнал
    # panel._urals_rub_gap. Словарь {месяц: курс} молча оставлял ПОСЛЕДНЮЮ точку месяца,
    # и тайл расходился с собственным сигналом до 5,7% (июнь-2026: 4 939 против 4 665 ₽,
    # гэп к бюджету ошибался на 5 п.п.). Нулевые курсы отбрасываем: старая запись через
    # `or` подменяла нулевой курс последней точкой, среднее бы его размазало.
    month_rates = [v for d, v in usd_pts if d[:7] == u_asof[:7] and v > 0]
    usd_for_month = sum(month_rates) / len(month_rates) if month_rates else usd_pts[-1][1]
    barrel = urals * usd_for_month
    gap = (barrel / BUDGET_BARREL_RUB - 1.0) * 100.0

    br_pts = _ser(store, "brent_moex")[0] or _ser(store, "brent")[0]
    measured_k = _urals_discount(store)
    k = measured_k or FALLBACK_URALS_DISCOUNT
    usd_last = usd_pts[-1][1]
    proxy = proxy_asof = None
    if br_pts:
        proxy_asof, brent = br_pts[-1]
        proxy = brent * usd_last * k
    status = _worst(_st("urals_tax", u_pts, u_meta, now), _st("usd_cbr", usd_pts, usd_meta, now))
    payload = {
        "tax_barrel_rub": _r(barrel, 0),
        "tax_month": u_asof,
        "urals_usd": _r(urals, 2),
        "usd": _r(usd_for_month, 2),
        "usd_basis": "среднемесячный",
        "budget_barrel_rub": BUDGET_BARREL_RUB,
        "gap_pct": _r(gap, 1),
        "proxy_rub": _r(proxy, 0),
        "proxy_asof": proxy_asof,
        "proxy_gap_pct": _r((proxy / BUDGET_BARREL_RUB - 1.0) * 100.0, 1) if proxy else None,
        "discount_k": _r(k, 3),
        "discount_is_fallback": measured_k is None,
    }
    side = "ниже" if gap < 0 else "выше"
    tail = f"; интрадей-прокси {_n(proxy, 0)} ₽" if proxy else ""
    headline = (f"Налоговая бочка {_n(barrel, 0)} ₽ — на {_n(abs(gap), 0)}% {side} "
                f"бюджетных {_n(BUDGET_BARREL_RUB, 0)} ₽{tail}")
    # ПОЧЕМУ заметка разделяет два разных гэпа: крупное число на тайле — гэп к БЮДЖЕТНОЙ
    # цене, его в валидации никто не тестировал. Валидированный сигнал — другой гэп,
    # к собственному 24-месячному тренду, и REGIME §4 прямо пишет, что эта нога плату
    # за множественность не переживает. Без явного разделения бейдж тира читается как
    # доказанность бюджетного разрыва, чего в исследовании нет.
    return _tile("rub_barrel", status, u_asof, headline, payload,
                 "Крупное число — разрыв с БЮДЖЕТНОЙ ценой: наблюдение, в валидации не "
                 "проверялось. Сигнал ядра — другой разрыв, к своему 24-месячному тренду, "
                 "знак КОНТРАРИАН (IC −0,19), и эта нога поправку на множественность не "
                 "переживает (соло p=0,15, REGIME §4). Бочка считается по среднемесячному "
                 "курсу USD, интрадей-прокси — по последнему и через дисконт Urals к Brent.",
                 u_meta.get("fetched_at"))


def _t_sep_node(store, now):
    """Календарный бейдж бюджетного узла; окно целиком берётся из constants.SEP_NODE.

    Дат в тексте НЕ дублируем: копия окна в докстроке уже разошлась с проектными
    документами (docs/INDICATORS.md и ARCHITECTURE.md называют 15.09–05.10, константа —
    10.09), и вторая копия только помогает расхождению дожить. Источник — календарь,
    а не данные, поэтому статус всегда ok: протухать тут нечему.
    """
    today = _msk_now(now).date()
    sm, sd = SEP_NODE["start_md"]
    em, ed = SEP_NODE["end_md"]
    start = date(today.year, sm, sd)
    end = date(today.year, em, ed)
    if today > end:
        start = date(today.year + 1, sm, sd)
        end = date(today.year + 1, em, ed)
    active = start <= today <= end
    days_to = (start - today).days
    payload = {
        "active": active,
        "window": f"{start.strftime('%d.%m')}–{end.strftime('%d.%m')}",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "days_to_start": max(days_to, 0),
        "days_left": (end - today).days if active else None,
        "prior": SEP_NODE["note"],
    }
    headline = (f"Окно узла активно до {end.strftime('%d.%m')}" if active
                else f"До окна бюджетного узла {days_to} дн.")
    return _tile("sep_node", "ok", today.isoformat(), headline, payload,
                 "Сезонный приор на n=4 (сентябри 2022–2025); событийная альфа «налогового узла» "
                 "опровергнута полным списком инициатив 2016–2026.")


def _t_breadth(store, now):
    pts, meta = _ser(store, "breadth")
    if not pts:
        return _empty("breadth")
    status = _st("breadth", pts, meta, now)
    asof, v = pts[-1]
    vals = [x for _, x in pts]
    scale = 100.0 if max(vals[-60:]) <= 1.0 else 1.0
    payload = {
        "pct_above_ma200": _r(v * scale, 1),
        "chg_21d_pp": _r((_chg(pts, 21) or 0.0) * scale, 1) if len(pts) > 21 else None,
        "pct_1y": _r(_pct_last(vals, 252), 0),
        "series": [[d, _r(x * scale, 1)] for d, x in pts[-120:]],
    }
    chg = payload["chg_21d_pp"]
    tail = f" ({_n(chg, 0, True)} п.п. за месяц)" if chg is not None else ""
    headline = f"Выше 200-дневной {_n(v * scale, 0)}% бумаг{tail}"
    return _tile("breadth", status, asof, headline, payload,
                 "В режиме 2025–26 ширина работает КОНТРАРИАН (узкая = перепроданность), "
                 "но выборка молодая — 19 месяцев.",
                 meta.get("fetched_at"))


def _t_mcxsm(store, now):
    s_pts, s_meta = _ser(store, "mcxsm")
    i_pts, i_meta = _ser(store, "imoex")
    s, i = dict(s_pts), dict(i_pts)
    common = sorted(set(s) & set(i))
    if len(common) < 2:
        return _empty("mcxsm")
    ratio = [(d, s[d] / i[d]) for d in common if i[d]]
    asof, r_last = ratio[-1]

    def _rs(n):
        if len(ratio) <= n or not ratio[-1 - n][1]:
            return None
        return (r_last / ratio[-1 - n][1] - 1.0) * 100.0

    status = _worst(_st("mcxsm", s_pts, s_meta, now), _st("imoex", i_pts, i_meta, now))
    payload = {
        "ratio": _r(r_last, 4),
        "rs_21d_pct": _r(_rs(21), 1),
        "rs_63d_pct": _r(_rs(63), 1),
        "rs_252d_pct": _r(_rs(252), 1),
        "series": [[d, _r(v, 4)] for d, v in ratio[-120:]],
    }
    rs63 = _rs(63)
    if rs63 is None:
        # `(rs63 or 0) >= 0` при None давало «лучше индекса на н/д%»: заголовок называл
        # направление, которого в данных нет, да ещё и приклеивал «%» к пустому числу.
        headline = "Общей истории с индексом меньше 63 дней — относительную силу не посчитать"
    else:
        side = "лучше" if rs63 >= 0 else "хуже"
        headline = f"Малые каппы {side} индекса на {_n(abs(rs63), 1)}% за 63 дня"
    return _tile("mcxsm", status, asof, headline, payload,
                 "Термометр фазы ставки: в ужесточение малые отстают на 8–9,5 п.п./год, "
                 "в 2026 впервые опережают. Интерпретация, не сигнал.",
                 s_meta.get("fetched_at"))


def _t_hy_spread(store, now):
    hy_pts, hy_meta = _ser(store, "rucbhycp_yield")
    ig_pts, ig_meta = _ser(store, "rucbcpns_yield")
    base_pts, base_label = _sub(store, ("zcyc_y2", "zcyc"), ("y2.0", "y2"))[0], "ОФЗ 2Y"
    if not base_pts:
        base_pts, base_label = _sub(store, ("zcyc_y1", "zcyc"), ("y1.0", "y1"))[0], "ОФЗ 1Y"
    if not base_pts:
        base_pts, base_label = _ser(store, "key_rate")[0], "ключевой ставке"
    if not hy_pts or not base_pts:
        return _empty("hy_spread")
    base = dict(base_pts)
    pairs = [(d, v - base[d]) for d, v in hy_pts if d in base]
    if not pairs:
        # Календари разъехались (КБД считается не каждый день) — берём последнюю базу.
        pairs = [(hy_pts[-1][0], hy_pts[-1][1] - base_pts[-1][1])]
    asof, spread = pairs[-1]
    ig_spread = None
    if ig_pts:
        ig = dict(ig_pts)
        if asof in ig and asof in base:
            ig_spread = ig[asof] - base[asof]
        else:
            ig_spread = ig_pts[-1][1] - base_pts[-1][1]
    status = _worst(_st("rucbhycp_yield", hy_pts, hy_meta, now),
                    _st("rucbcpns_yield", ig_pts, ig_meta, now) if ig_pts else "ok")
    vals = [v for _, v in pairs]
    payload = {
        "hy_yield": _r(hy_pts[-1][1], 2),
        "base_label": base_label,
        "base_yield": _r(base_pts[-1][1], 2),
        "spread_pp": _r(spread, 2),
        "ig_spread_pp": _r(ig_spread, 2),
        "pct_1y": _r(_pct_last(vals, 252), 0),
        "chg_21d_pp": _r((pairs[-1][1] - pairs[-22][1]) if len(pairs) > 21 else None, 2),
        "series": [[d, _r(v, 2)] for d, v in pairs[-120:]],
    }
    headline = (f"ВДО {_n(hy_pts[-1][1], 1)}% — спред к {base_label} {_n(spread, 1)} п.п. "
                f"({_n(_pct_last(vals, 252), 0)}-й перцентиль за год)")
    return _tile("hy_spread", status, asof, headline, payload,
                 "Широкий спред читается контрариан (премия за риск уже уплачена), "
                 "сила эффекта умеренная.",
                 hy_meta.get("fetched_at"))


BUILDERS = [
    ("orfr", _t_orfr), ("lqdt", _t_lqdt), ("deposit_spread", _t_deposit_spread),
    ("dividends", _t_dividends), ("cb_meeting", _t_cb_meeting), ("cpi_weekly", _t_cpi_weekly),
    ("ofz_auctions", _t_ofz_auctions), ("polymarket", _t_polymarket), ("futoi", _t_futoi),
    ("rvi", _t_rvi), ("rub_barrel", _t_rub_barrel), ("sep_node", _t_sep_node),
    ("breadth", _t_breadth), ("mcxsm", _t_mcxsm), ("hy_spread", _t_hy_spread),
]


def build_monitors(store, now=None):
    """Тайлы слоя 3 в порядке BUILDERS. Ни один сбой не выходит наружу."""
    now = now or datetime.now(timezone.utc)
    tiles = []
    for tid, fn in BUILDERS:
        try:
            tile = fn(store, now)
        except Exception as exc:  # noqa: BLE001 — граница изоляции тайла (CONTRACT §0)
            tile = _tile(tid, "error", None, "тайл не собрался",
                         {"error": f"{type(exc).__name__}: {exc}"[:300]})
        tiles.append(tile)
    return tiles


def check_coverage():
    """Сверка тайлов с реестром тиров — вызывается из run.py --mode selftest."""
    problems = []
    ids = [tid for tid, _ in BUILDERS]
    if len(set(ids)) != len(ids):
        problems.append("дубликаты id в BUILDERS")
    for tid in ids:
        if tid not in MONITOR_TIERS:
            problems.append(f"{tid}: нет тира в MONITOR_TIERS")
        if tid not in TITLES:
            problems.append(f"{tid}: нет заголовка")
    for tid in MONITOR_TIERS:
        if tid not in ids:
            problems.append(f"{tid}: тир есть, тайла нет")
    for tier in set(MONITOR_TIERS.values()):
        if tier not in TIER_NOTES:
            problems.append(f"тир {tier}: нет пояснения в TIER_NOTES")
    return problems
