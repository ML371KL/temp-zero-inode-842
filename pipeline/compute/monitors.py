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
from bisect import bisect_right
from datetime import date, datetime, timedelta, timezone

from pipeline.lib import calc, registry, schedule
from pipeline.lib.constants import (
    CB_MEETINGS_2026,
    MONITOR_TIERS,
    MSK_OFFSET_HOURS,
    SEP_NODE,
    SLA_MINUTES,
    TIER_NOTES,
)

try:
    # Порог репрайсинга живёт у тени: там он ТЕСТИРУЕТСЯ как бит (P3m аудита
    # 02.09.2026), здесь только подписывается. Две копии числа разошлись бы.
    from pipeline.compute.shadow import REPRICING_PP
except ImportError:  # тень пишется параллельно — тайл не должен от неё зависеть
    REPRICING_PP = 0.25

TITLES = {
    "orfr": "Потоки ОРФР",
    "lqdt": "Фонды денежного рынка",
    "deposit_spread": "Вклады против дивидендов",
    "dividends": "Дивидендный календарь",
    "cb_meeting": "Заседание ЦБ",
    "expectations": "Цена ожиданий по ставке",
    # cpi_weekly с витрины снят (аудит 02.09.2026): недельный ИПЦ как предиктор
    # акций опровергнут, а как вход ожиданий ставки он теперь строка в cb_meeting.
    # Заголовок и функция _t_cpi_weekly оставлены — вернуть тайл стоит одну строку
    # в BUILDERS.
    "cpi_weekly": "Недельная инфляция",
    "ofz_auctions": "Аукционы ОФЗ",
    # Именно «соглашения»: рынки с формулировкой «ceasefire» разрешались YES по
    # трёхдневному перемирию, и ряд берёт серию со словом agreement (см. шапку
    # fetch/polymarket.py). Название плитки обязано совпадать с тем, что куплено.
    "polymarket": "Вероятность соглашения о перемирии",
    "futoi": "Позиции физлиц во фьючерсе",
    "rvi": "Индекс волатильности RVI",
    "rub_barrel": "Рублёвая бочка",
    "sep_node": "Бюджетный узел (сентябрь)",
    "breadth": "Ширина рынка",
    "mcxsm": "Малые каппы против индекса",
    "hy_spread": "Спред ВДО",
    "retail": "Частные инвесторы",
}

# Обязательная пометка для тира dead: тайл остаётся на панели как контекст, но
# читатель обязан видеть, что как предиктор акций он опровергнут (VALIDATION §5).
DEAD_MARK = "как предиктор акций опровергнуто"

# БАЗА БЮДЖЕТНОГО ПРАВИЛА по годам: цена отсечения × курс, на котором построен
# бюджет. Столько рублей с барреля бюджет получает по правилу; всё, что выше,
# Минфин отправляет в ФНБ покупкой валюты, недостачу ФНБ покрывает продажей. То
# есть знак разрыва налоговой бочки с базой — это знак операций Минфина, а не
# «сходится ли бюджет»: прежняя подпись «бюджет сходится при 5 440 ₽» обещала
# то, чего правило не делает (дефицит 2026 года заложен и при базе).
#
# Прежняя константа 5 440 ₽ — это 59 $ × 92,2 ₽ 2026 года. 24.09.2026 Минфин внёс
# проект бюджета на 2027–2029 годы: отсечка 50 $ на все три года, курс бюджета
# 87,4 / 92 / 96 ₽. До 2027 года отсечка и прогноз цены Urals совпадали (59 $),
# в проекте разошлись (50 против 53) — база считается по ОТСЕЧКЕ: бюджет тратит
# её, разницу копит в ФНБ. Та же таблица живёт в панели 843
# (`pipeline/lib/policy.py`): меняя здесь, поменяй и там. Пересматривать при
# каждом бюджете — в сентябре (проект) и в ноябре (закон), а не молча.
BUDGET_BASE = {
    2026: {"cutoff_usd": 59.0, "fx": 92.2, "status": "закон"},
    2027: {"cutoff_usd": 50.0, "fx": 87.4, "status": "проект"},
    2028: {"cutoff_usd": 50.0, "fx": 92.0, "status": "проект, плановый период"},
    2029: {"cutoff_usd": 50.0, "fx": 96.0, "status": "проект, плановый период"},
}


def _budget_base(year):
    """База правила года, ₽/барр., округлённая до 10 ₽ (5 439,8 -> 5 440), или None."""
    p = BUDGET_BASE.get(int(year)) if str(year).isdigit() else None
    if not p:
        return None
    return dict(p, year=int(year), barrel_rub=round(p["cutoff_usd"] * p["fx"], -1))
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

# «manual_needed» — тоже НЕ «ok»: так фетчер говорит «живой источник молчит, взял
# ручной резерв». Слова не было в наборе, и _st превращал его в зелёный: при мёртвых
# T-Invest и smart-lab витрина показывала свежую точку над датами из
# inputs/dividends.yml, а блок «Источники» — зелёную семью (аудит 20.08.2026).
_STATUS_RANK = {"ok": 0, "stale": 1, "manual_needed": 1, "error": 2, "missing": 3}


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
    subkeys, а iss.futoi пишет futoi_mx_pos и родственные), поэтому кандидаты перечисляются
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


# Сколько времени даём прогону доехать от срабатывания таймера до записи ряда.
# Суточный прогон занимает секунды, но может ждать замок стора (до 240 с) и
# повторяться; полчаса закрывают это с запасом и не прячут настоящий пропуск.
RUN_GRACE_MIN = 30


def _poll_missed(sid, meta, now):
    """Был ли ПЛАНОВЫЙ опрос этого ряда, о котором в сторе нет следа.

    ЗАЧЕМ ЭТО ВООБЩЕ. Раньше свежесть считалась одним вопросом: «сколько часов
    прошло с последнего удачного опроса». Вопрос неверный там, где конвейер молчит
    ПО РАСПИСАНИЮ. Замер 14.08.2026 на боевом сторе: в ближайшие выходные так
    протухли бы 23 ряда из 26 — пять сообщений в ops-канал о том, что биржа закрыта.
    А ряд аукционов ОФЗ краснел КАЖДУЮ НЕДЕЛЮ: его норма 26 часов, а опрашивают его
    недельным режимом, между тактами которого до 97 часов. Именно это сообщение и
    пришло владельцу в пятницу 08:25 — при исправном источнике и верных числах.

    Теперь спрашиваем иначе: прошёл ли с момента последней записи ТАКТ, который
    обязан был этот ряд опросить. Если следующий такт ещё не наступил, возраст ряда
    не значит ничего — так же, как тишина панели ночью не значит поломки
    (`lib/schedule.next_publish_at`, тот же урок).

    Ответ «не знаю» (расписание не прочиталось) трактуется как «пропуск был»: тогда
    решает прежняя проверка по SLA, и поведение остаётся старым.
    """
    modes = [m for m, ids in registry.MODES.items() if sid in ids]
    if not modes:
        base = max((k for k in registry.SERIES if sid.startswith(k)), key=len, default=None)
        modes = [m for m, ids in registry.MODES.items() if base in ids] if base else []
    if not modes:
        return True
    deadline = now - timedelta(minutes=RUN_GRACE_MIN)
    due = []
    for mode in modes:
        moment = schedule.last_run_at(mode, deadline)
        if moment is not None:
            due.append(moment)
    if not due:
        return True
    fetched = _parse_ts((meta or {}).get("fetched_at"))
    return fetched is None or fetched < max(due)


def _asof_date(value):
    """meta.asof -> date. «2026-07» читается как КОНЕЦ июля, а не как 1-е.

    Месячные ряды подписывают период, а не день: разница в 30 суток, и на ней
    moex_retail выглядел бы протухшим вдвое старше, чем есть.
    """
    s = str(value or "")[:10]
    if len(s) == 7 and s[4] == "-":
        try:
            y, m = int(s[:4]), int(s[5:7])
        except ValueError:
            return None
        nxt = date(y + (m == 12), 1 if m == 12 else m + 1, 1)
        return nxt - timedelta(days=1)
    return _d(s)


def data_age_days(sid, meta, now):
    """Сколько суток данным ряда. None — сказать нечего (нет asof или он в будущем).

    Будущее — не дефект: у дивидендного календаря точки это ОТСЕЧКИ, они впереди
    по построению (оплачено 12.08.2026).
    """
    asof = _asof_date((meta or {}).get("asof"))
    if asof is None:
        return None
    today = (now or datetime.now(timezone.utc)).date()
    age = (today - asof).days
    return age if age >= 0 else None


def _st(sid, pts, meta, now):
    """Статус ряда по CONTRACT §7: missing → error → stale → ok.

    pts нужен только как признак «данные есть» — сгодится любая непустая
    последовательность (для рядов, собранных из нескольких подрядов).

    ДВЕ РАЗНЫЕ ПРОТУХШЕСТИ, и до 26.08.2026 проверялась только первая:
      1. источник не ОПРАШИВАЛСЯ (fetched_at старше SLA) — он молчит;
      2. источник отвечает исправно, но отдаёт СТАРОЕ (asof старше своей нормы).
    Вторая не ловилась вовсе: для FRED, который каждый день бодро возвращает Brent
    недельной давности, опрос всегда свежий. Возраст данных замечала одна лишь
    протяжка в панели — и молча, значение просто исчезало по истечении лимита.
    Именно так неделю выглядели исправными и сломанный расчёт доходности ВДО, и
    отвалившийся по сертификату ALGOPACK.
    """
    if not pts:
        return "missing"
    declared = (meta or {}).get("status")
    if declared == "error":
        return "error"
    spec = registry.SERIES.get(sid)
    if spec is None:
        # Подряды futoi_mx_pos/…_long/…_short в реестре не описаны — SLA у базового ряда.
        base = max((k for k in registry.SERIES if sid.startswith(k)), key=len, default=None)
        spec = registry.SERIES.get(base) or {}
    sla_key = spec.get("sla")
    sla = SLA_MINUTES.get(sla_key) if sla_key else None
    age = _age_min((meta or {}).get("fetched_at"), now)
    if sla and age is not None and age > sla and _poll_missed(sid, meta, now):
        return "stale"
    norm = registry.data_age_norm(sid)
    data_age = data_age_days(sid, meta, now)
    if norm is not None and data_age is not None and data_age > norm:
        return "stale"
    if declared in _STATUS_RANK:
        return declared
    return "ok"


def stale_reason(sid, pts, meta, now):
    """Почему ряд протух: "poll" (не опрашивался) | "data" (отдаёт старое) | None.

    Причины разные и чинятся по-разному: первая — конвейер или расписание, вторая —
    сам источник. Одно слово «устарел» на обе посылало владельца искать не там.
    """
    if _st(sid, pts, meta, now) != "stale":
        return None
    spec = registry.SERIES.get(sid)
    if spec is None:
        base = max((k for k in registry.SERIES if sid.startswith(k)), key=len, default=None)
        spec = registry.SERIES.get(base) or {}
    sla = SLA_MINUTES.get(spec.get("sla")) if spec.get("sla") else None
    age = _age_min((meta or {}).get("fetched_at"), now)
    if sla and age is not None and age > sla and _poll_missed(sid, meta, now):
        return "poll"
    return "data"


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


def _dmy(dstr):
    d = _d(dstr)
    return d.strftime("%d.%m.%Y") if d else "н/д"


def _month_ru(dstr):
    d = _d(dstr)
    return f"{MONTHS_RU_NOM[d.month - 1]} {d.year}" if d else "н/д"


def _step(pts):
    """Функция «значение на дату»: последняя точка ряда не позже дня (ступенька).

    Ключевая ставка живёт событиями, кривая ОФЗ — торговыми днями; разность двух
    рядов имеет смысл только на ставке, ДЕЙСТВОВАВШЕЙ в день среза кривой.
    """
    keys = [d for d, _ in pts]
    vals = [v for _, v in pts]

    def at(day):
        k = bisect_right(keys, day) - 1
        return vals[k] if k >= 0 else None
    return at


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
        # chg_*, а не flow_*: измерено ИЗМЕНЕНИЕ СЧА, приток отдельно не выделен.
        "chg_1d": _r(_chg(pts, 1), 1),
        "chg_5d": _r(_chg(pts, 5), 1),
        "chg_21d": _r(_chg(pts, 21), 1),
        "peak": _r(peak, 1) if len(pts) >= 60 else None,
        "dd_from_peak_pct": _r(dd, 1) if len(pts) >= 60 else None,
        "history_points": len(pts),
        "market_total_bln": meta.get("moex_total_bln"),
        "coverage_note": meta.get("coverage_note"),
        "rotation_started": rotation,
        "rotation_threshold_pct": ROTATION_DD_PCT,
        "series": [[d, _r(v, 1)] for d, v in pts[-120:]],
    }
    # Ряд — это ОДИН фонд (LQDT), а не весь рынок денежных фондов: так его собирает
    # fetch/investfunds и сам предупреждает об этом в meta.coverage_note. Заголовок
    # «СЧА 755 млрд» под названием «Фонды денежного рынка» читался как объём всего
    # рынка — ошибка более чем вдвое (весь рынок больше 1,8 трлн).
    whose = meta.get("funds_label") or "LQDT"
    # ПРИРОСТ СЧА — НЕ ПРИТОК. СЧА растёт сама на доходность пая: при 755 млрд и
    # ключе 14% это ~0,29 млрд в день само по себе. Пока приток не выделен
    # (investfunds.estimate_flow написан, но в реестре нет ряда цены пая), пишем
    # честное «изменение», а не «поток».
    dd_known = dd is not None and len(pts) >= 60
    # КУДА УШЛИ ДЕНЬГИ, говорит не СЧА фонда, а поток физлиц в акции (ОРФР).
    # Минфин предлагает с 2027 года облагать доход внутри ПИФ 15% (бюджетный пакет
    # 24.09.2026): тогда отток из фондов ликвидности может уйти во вклады, и
    # «СЧА ниже пика» перестанет значить «ротация в акции» даже приблизительно.
    fiz_pts, _ = _ser(store, "orfr_flows_fiz")
    fiz = fiz_pts[-1] if fiz_pts else None
    payload["retail_equity_flow_bln"] = _r(fiz[1], 1) if fiz else None
    payload["retail_equity_flow_month"] = fiz[0] if fiz else None
    if rotation:
        tail = f"СЧА ниже пика на {_n(abs(dd or 0), 1)}% — отток из фонда"
        if fiz and calc.is_num(fiz[1]):
            tail += (f"; физлица за {_month_ru(fiz[0])} купили акций на "
                     f"{_n(fiz[1], 1, True)} млрд — похоже на ротацию" if fiz[1] > 0 else
                     f"; но физлица за {_month_ru(fiz[0])} акции продавали "
                     f"({_n(fiz[1], 1, True)} млрд) — деньги ушли не в акции")
        else:
            tail += "; ушли ли деньги в акции, покажет поток физлиц (ОРФР)"
    elif dd_known:
        tail = "большой ротации ещё не случалось"
    else:
        # На коротком ряде «пик» — это просто максимум последних дней, и вывод об
        # истории из него делать нельзя.
        tail = f"истории мало ({len(pts)} набл.) — про ротацию судить рано"
    day_chg = _chg(pts, 1)
    chg_txt = f", изменение за день {_n(day_chg, 1, True)} млрд" if day_chg is not None else ""
    headline = f"СЧА {whose} {_n(aum, 0)} млрд{chg_txt}; {tail}"
    note = ("Индикатор ждёт первого срабатывания: перетока денег из фондов ликвидности "
            f"в акции не было ни разу, проверить его на истории невозможно. Ряд — СЧА "
            f"фонда {whose}, а не всего рынка денежных фондов; изменение СЧА включает "
            "доходность пая и потому не равно притоку денег. Просадка СЧА сама по себе "
            "ротацией в акции не считается: рядом стоит поток физлиц в акции по ОРФР. "
            "С 2027 года Минфин предлагает облагать доход внутри ПИФ 15% — отток из "
            "фондов ликвидности тогда может уйти во вклады, а не в акции.")
    return _tile("lqdt", status, asof, headline, payload, note, meta.get("fetched_at"))


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


def _dy_at(store, days):
    """{дата: трейлинг-дивдоходность} для запрошенных дат — тем же правилом.

    Нужна, чтобы утверждение «спред положителен ВПЕРВЫЕ» проверялось по истории,
    а не предполагалось: до 20.08.2026 заголовок обещал «событие впервые в
    истории», посчитав ровно сегодняшний день.
    """
    m = dict(_ser(store, "mcftr")[0])
    i = dict(_ser(store, "imoex")[0])
    common = sorted(set(m) & set(i))
    if len(common) < 253:
        return {}
    pos = {d: k for k, d in enumerate(common)}
    out = {}
    for day in days:
        k = pos.get(day)
        if k is None:
            # У вкладов декадные даты, у индексов — торговые дни: берём ближайший
            # предыдущий торговый день, а не пропускаем точку.
            earlier = [d for d in common if d <= day]
            if not earlier:
                continue
            k = pos[earlier[-1]]
        if k < 252:
            continue
        d1, d0 = common[k], common[k - 252]
        if min(m[d1], m[d0], i[d1], i[d0]) <= 0:
            continue
        out[day] = (math.log(m[d1] / m[d0]) - math.log(i[d1] / i[d0])) * 100.0
    return out


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
    # Сколько дней в прошлом спред уже был положительным: считаем по ряду, а не
    # по сегодняшнему значению.
    dy_hist = _dy_at(store, [d for d, _ in dep_pts[:-1]])
    before_positive = sum(1 for d, v in dep_pts[:-1]
                          if d in dy_hist and dy_hist[d] - v > 0)
    payload = {
        "deposit_pct": _r(dep, 2),
        "deposit_asof": dep_asof,
        "dy_trail_pct": _r(dy, 2),
        "dy_asof": dy_asof,
        "spread_pp": _r(spread, 2),
        "deposit_chg_pp": _r(_chg(dep_pts, 1), 2),
        # ever_positive обещает «когда-либо было положительным», а считало ТОЛЬКО
        # сегодняшний день — при том что 72 точки истории лежат тут же, в series.
        # Отсюда и заголовок «событие впервые в истории», который никто не проверял.
        "positive_now": bool(spread is not None and spread > 0),
        "positive_days_before": before_positive,
        "series": [[d, _r(v, 2)] for d, v in dep_pts[-72:]],
    }
    if spread is None:
        headline = f"Вклады {_n(dep, 1)}%, дивдоходность не посчитана"
    elif spread > 0:
        # «Впервые» — утверждение ОБ ИСТОРИИ, и оно проверяется по ряду, а не
        # предполагается. История спреда у нас с 2024 года, поэтому и говорим
        # ровно про неё, а не «в истории» вообще.
        first = " — впервые за всю доступную историю ряда" if not before_positive else (
            f" — такое уже бывало ({before_positive} набл. в ряду)")
        headline = (f"Дивдоходность {_n(dy, 1)}% выше вкладов {_n(dep, 1)}% "
                    f"на {_n(spread, 1)} п.п.{first}")
    else:
        # Без «в пользу/не в пользу акций»: спред — не вердикт, а число; как
        # сигнал он работает только в одной фазе, и это сказано тут же.
        headline = (f"Вклады {_n(dep, 1)}% против дивидендов {_n(dy, 1)}%: "
                    f"спред {_n(spread, 1)} п.п.; как сигнал работает только в фазе "
                    f"смягчения (карточка второго ряда)")
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
                         "amount_bn": _r(it.get("amount_bn"), 1),
                         # Просадка ИНДЕКСА от этой отсечки = вес бумаги × её
                         # дивдоходность. Это и есть та величина, ради которой
                         # календарь заводился: механический гэп нельзя путать с
                         # ухудшением рынка, а сам по себе процент доходности бумаги
                         # ничего не говорит о том, насколько просядет IMOEX.
                         "index_drag_pct": _r(it.get("index_drag_pct"), 3),
                         "weight_pct": _r(it.get("weight_pct"), 2)})
    else:
        # Фолбэк: без meta.items ряд несёт по датам отсечек ДИВДОХОДНОСТИ в
        # процентах (контракт обоих источников: fetch/dividends.py unit='pct',
        # fetch/manual.py). Класть их в amount_bn значило печатать проценты как
        # миллиарды («выплат 9 млрд» из точки 9,13%) — ошибка масштаба на два
        # порядка в денежном поле; сумм в этом ряде нет вовсе.
        for d, v in pts:
            rows.append({"ticker": "?", "ex_date": d[:10],
                         "yield_pct": _r(v, 2), "amount_bn": None})
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
    # Суммарная просадка индекса от всех отсечек горизонта. Считается ЗДЕСЬ по тем
    # же строкам, что показаны, а не берётся из meta: meta.index_drag_ahead_pct
    # покрывает весь календарь источника, а тайл говорит про окно в 90 дней.
    drags = [r["index_drag_pct"] for r in window if r.get("index_drag_pct") is not None]
    drag_total = round(sum(drags), 3) if drags else None
    payload = {
        "upcoming": upcoming[:8],
        "sum_90d_bn": _r(total, 1),
        "reinvest_est_bn": _r(total * share, 1) if total is not None else None,
        "reinvest_share": share,
        "index_drag_90d_pct": drag_total,
        "source": meta.get("origin") or meta.get("source"),
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
        # Гэп индекса — первое, что нужно читателю: он говорит, на сколько просядет
        # IMOEX механически. Доходность отдельной бумаги без её веса этого не даёт.
        gap = (f"; гэп индекса за 90 дней ≈{_n(drag_total, 2)}%"
               if drag_total is not None else "")
        money = (f", выплат {_n(total, 0)} млрд, реинвест ≈{_n(total * share, 0)} млрд"
                 if total is not None else "")
        headline = f"Ближайшая отсечка: {nxt['ticker']} {_ddmm(nxt['ex_date'])}{y}{gap}{money}"
    # Запасная дата — день последнего успешного чтения, а НЕ ближайшая отсечка.
    # Отсечка лежит в будущем, и подпись «данные: 21.09.2026» означала бы, что
    # витрина знает больше, чем произошло. Ручной резерв (fetch/manual.py) asof не
    # ставит вовсе — без этой строки тайл снова уехал бы в будущее.
    asof = meta.get("asof") or (str(meta.get("fetched_at") or "")[:10] or None)
    return _tile("dividends", status, asof,
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
    # Ставка НА ДЕНЬ заседания и возраст решения — для события о СОХРАНЕНИИ ставки
    # (alerts._cb). Без них «сохранил» отличить от «ещё не дошло до данных» нечем:
    # решение попадает в ряд key_rate только на следующий рабочий день.
    rate_at_meeting = None
    if last_fresh and last_meeting:
        upto = [v for d, v in key_pts if d <= last_meeting and v is not None]
        rate_at_meeting = upto[-1] if upto else None
    days_since = (today - _d(last_meeting)).days if (last_fresh and last_meeting) else None
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
        "rate_at_last_meeting": _r(rate_at_meeting, 2),
        "days_since_last_meeting": days_since,
        "rusfar3m": _r(rusfar, 2),
        "rusfar_asof": rus_asof,
        "spread_pp": _r(spread, 2),
        "priced_text": priced,
        "calendar": CB_MEETINGS_2026,
    }
    # Недельный ИПЦ — вход в ожидания ставки, а не предиктор акций (тайл снят
    # аудитом 02.09.2026): одной строкой здесь, где он и читается — перед заседанием.
    cpi_pts, _cpi_meta = _ser(store, "cpi_weekly")
    if cpi_pts:
        last4 = cpi_pts[-4:]
        total = sum(v for _, v in last4)
        payload["cpi_weekly_prints"] = [[d, _r(v, 2)] for d, v in last4]
        payload["cpi_weekly_4w"] = (
            f"недельная инфляция за {len(last4)} нед.: "
            + ", ".join(f"{_n(v, 2, True)}%" for _, v in last4)
            + f" (сумма {_n(total, 2, True)}%, последняя неделя {_ddmm(last4[-1][0])})")
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


def _t_expectations(store, now):
    """Цена ожиданий по ставке: сколько смягчения или ужесточения рынок уже заложил.

    Четыре числа — один и тот же спред с разных концов: год ОФЗ минус ключ
    (уровень), его изменение за 21 торговый день, RUSFAR 3M минус ключ и полгода
    ОФЗ минус ключ. Как УРОВЕНЬ спред направление акций не предсказывает (аудит
    02.09.2026), поэтому тайл вердикта не выносит — только называет, что в цене.
    Единственный живой остаток — РОСТ спреда больше +0,25 п.п. за 21 день как
    детектор турбулентности; он живёт тенью (compute/shadow.py::repricing), а тут
    лишь подписывается, чтобы читатель знал, откуда взялось число.

    Ставка берётся ДЕЙСТВОВАВШАЯ в день среза кривой (_step), а не последняя:
    иначе в неделю после заседания спред за 21 день целиком состоял бы из шага
    ЦБ, а не из движения кривой.
    """
    y1_pts, y1_meta = _sub(store, ("zcyc_y1", "zcyc"), ("y1.0", "y1", "1.0"))
    y05_pts, _y05_meta = _sub(store, ("zcyc_y0_5", "zcyc"), ("y0.5", "y0_5", "0.5"))
    key_pts, key_meta = _ser(store, "key_rate")
    rus_pts, rus_meta = _ser(store, "rusfar3m")
    src_note = "Источники: КБД МосБиржи (1Y, 0.5Y), ключевая ставка ЦБ, RUSFAR 3M."
    if not y1_pts or not key_pts:
        return _empty("expectations", src_note)
    key_at = _step(key_pts)
    spread = [(d, y - key_at(d)) for d, y in y1_pts if key_at(d) is not None]
    if not spread:
        return _empty("expectations", src_note)
    asof, s_last = spread[-1]
    chg21 = (spread[-1][1] - spread[-22][1]) if len(spread) > 21 else None
    key_now = key_at(asof)
    y1_now = dict(y1_pts)[asof]
    y05_now = _step(y05_pts)(asof) if y05_pts else None
    rus_asof, rus = _last(rus_pts)
    rus_key = key_at(rus_asof) if rus_asof else None
    rus_spread = (rus - rus_key) if (rus is not None and rus_key is not None) else None

    status = _worst(_st("zcyc_y1", y1_pts, y1_meta, now), _st("key_rate", key_pts, key_meta, now))
    if rus_pts:
        status = _worst(status, _st("rusfar3m", rus_pts, rus_meta, now))
    repricing = None if chg21 is None else bool(chg21 > REPRICING_PP)
    payload = {
        "y1_pct": _r(y1_now, 2),
        "y05_pct": _r(y05_now, 2),
        "key_rate_pct": _r(key_now, 2),
        "key_rate_asof": key_pts[-1][0],
        "rusfar3m_pct": _r(rus, 2),
        "rusfar_asof": rus_asof,
        "spread_y1_key_pp": _r(s_last, 2),
        "spread_y05_key_pp": _r((y05_now - key_now) if y05_now is not None else None, 2),
        "spread_rusfar_key_pp": _r(rus_spread, 2),
        "chg_21d_pp": _r(chg21, 2),
        "repricing_threshold_pp": REPRICING_PP,
        "repricing": repricing,
        "series": [[d, _r(v, 2)] for d, v in spread[-120:]],
    }
    if s_last < -0.05:
        priced = f"в цене {_n(abs(s_last), 2)} п.п. смягчения"
    elif s_last > 0.05:
        priced = f"в цене {_n(s_last, 2)} п.п. ужесточения"
    else:
        priced = "у ключа, смягчение не в цене"
    chg_txt = (f"за 21 день спред {_n(chg21, 2, True)} п.п." if chg21 is not None
               else "истории меньше 21 дня")
    headline = f"Год ОФЗ {_n(y1_now, 2)}% против ключа {_n(key_now, 2)}%: {priced}; {chg_txt}"
    if repricing:
        headline += " — репрайсинг ожиданий (тень)"
    return _tile("expectations", status, asof, headline, payload,
                 "Как уровень направление не предсказывает; рост спреда "
                 f"больше +{_n(REPRICING_PP, 2)} п.п. за 21 день — детектор турбулентности "
                 "(тень, на позицию не влияет).",
                 y1_meta.get("fetched_at") or key_meta.get("fetched_at"))


def _t_cpi_weekly(store, now):
    """Оставлен как функция, из BUILDERS убран (аудит 02.09.2026): для акций
    недельный ИПЦ опровергнут, а как вход ожиданий он — строка в cb_meeting.

    id тайла — в переменной, а не литералом: поиск по исходнику `_tile("cpi_weekly"`
    не должен находить снятый тайл как живой (так однажды считал test_guide).
    """
    tid = "cpi_weekly"
    pts, meta = _ser(store, tid)
    if not pts:
        return _empty(tid)
    status = _st(tid, pts, meta, now)
    asof, last = pts[-1]

    def _saar(weekly_pct_list):
        # Недельный прирост в годовые проценты: (1+w)^(365/7)−1. СЕЗОННОСТИ ЗДЕСЬ
        # НЕТ — а буквы SA в аббревиатуре SAAR обещают именно её (seasonally
        # adjusted). Летняя плодоовощная дефляция раскручивалась в «−0,8% годовых»
        # и читалась как очищенная от сезона оценка инфляции. Поэтому наружу это
        # называется «в годовом выражении», без сезонного обещания (аудит 20.08.2026).
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
        # annualized_*, а не saar_*: сезонной корректировки в расчёте нет.
        "annualized_last_pct": _r(_saar([last]), 1),
        "annualized_4w_pct": _r(_saar(last4), 1),
        "annualized_note": "без сезонной корректировки",
    }
    headline = (f"Неделя {_ddmm(asof)}: {_n(last, 2, True)}% "
                f"(в годовом выражении по 4 неделям {_n(_saar(last4), 1)}%, "
                f"без сезонной корректировки)")
    return _tile(tid, status, asof, headline, payload,
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
    weeks = meta.get("weeks_since")
    ahead = meta.get("next_auction")
    payload = {
        "date": asof,
        "issue": last_meta.get("issue"),
        "placed_bn": _r(placed, 1),
        "demand_bn": _r(demand, 1),
        "bid_to_cover": _r(btc, 2),
        "premium_bp": last_meta.get("premium_bp"),
        "failed": bool(failed),
        "weeks_since": weeks,
        "next_auction": ahead,
        "method": meta.get("method"),
        "recent": [[d, _r(v, 1)] for d, v in pts[-12:]],
    }
    # Спрос теперь бывает пустым штатно: биржа его не раскрывает, а сайт Минфина с
    # прод-машины недоступен. Молчание про спрос — это «нет данных», и печатать
    # «при спросе н/д млрд» нельзя: единица, приклеенная к отсутствующему числу.
    demand_txt = f" при спросе {_n(demand, 1)} млрд" if demand is not None else ""
    btc_txt = f", bid-to-cover {_n(btc, 2)}" if btc is not None else ""
    if failed and (placed is None or placed <= 0):
        # «размещено 0,0 млрд» крупным кеглем неотличимо от аукциона, где ноль реально
        # разместили. Провал по нулю называем словами.
        headline = f"Аукцион {_ddmm(asof)} не состоялся: размещения не было"
    elif failed:
        headline = (f"Аукцион {_ddmm(asof)} провален: размещено {_n(placed, 1)} млрд"
                    f"{demand_txt}{btc_txt}")
    else:
        headline = (f"Аукцион {_ddmm(asof)}: размещено {_n(placed, 1)} млрд"
                    f"{demand_txt}{btc_txt}")
    # Пауза в размещениях — самостоятельное состояние рынка, а не «старые данные».
    # С 20.07.2026 аукционы приостановлены, и заголовок без этой оговорки читался бы
    # как свежая новость про аукцион месячной давности.
    if isinstance(weeks, int) and weeks >= 2:
        headline = (f"Аукционов нет {weeks} нед. Последний — {headline[8:]}"
                    if headline.startswith("Аукцион ") else headline)
    if ahead:
        headline += f". Назначен следующий: {_ddmm(ahead)}"
    return _tile("ofz_auctions", status, asof, headline, payload,
                 "Провал аукциона читаем как индикатор фискальной премии в длинном конце, "
                 "а не как торговый сигнал для акций. Объём — биржевой (доска PACT), без "
                 "доразмещения после аукциона; спрос биржа не раскрывает, а сайт Минфина "
                 "с прод-машины недоступен.",
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
    # Горизонт — часть самого числа, а не подробность: вероятность «до даты»
    # зависит от того, сколько до неё осталось. Без срока «перемирие 2%» и
    # «перемирие 24%» выглядят противоречием, хотя это один рынок в один день.
    # Дата из САМОГО вопроса, а не endDate: у «by December 31, 2026» биржа ставит
    # расчёт на 2027-01-01T04:59Z, и заголовок «до 01.01.2027» спорил с вопросом,
    # напечатанным строкой ниже. Горизонт при этом считается по endDate — это
    # настоящий момент расчёта.
    end_date = str(meta.get("resolves_on") or meta.get("end_date") or "")[:10]
    horizon = meta.get("horizon_days")
    if not isinstance(horizon, int):
        a, b = _d(str(meta.get("end_date") or end_date)[:10]), _d(asof)
        horizon = (a - b).days if (a and b) else None
    payload = {
        "prob_pct": _r(prob, 1),
        "chg_7d_pp": _r(chg7, 1),
        "chg_30d_pp": _r(chg30, 1),
        "question": meta.get("question") or meta.get("note"),
        "end_date": end_date or None,
        "horizon_days": horizon if isinstance(horizon, int) else None,
        "market_volume_usd": _r(meta.get("volume"), 0),
        "series": [[d, _r(v * scale, 1)] for d, v in pts[-120:]],
    }
    tail = f" ({_n(chg7, 1, True)} п.п. за неделю)" if chg7 is not None else ""
    # Дата, а не месяц прописью: рынок разрешается КОНКРЕТНЫМ днём, а «к декабрю»
    # ещё и требует дательного падежа, которого в MONTHS_RU_NOM нет — «к декабрь
    # 2026» уехало в прод на один такт.
    when = f" до {_dmy(end_date)}" if end_date else ""
    headline = f"Соглашение{when}: {_n(prob, 0)}%{tail}"
    note = ("История с 2022 года и мало разрешившихся событий — проверить предиктивность "
            "нечем; тайл нужен для чтения новостного фона.")
    if isinstance(horizon, int):
        note += (f" Показан самый торгуемый контракт серии, до его даты {horizon} дн.: "
                 f"у контрактов на исходе срока уровень определяется календарём, "
                 f"а не переговорами.")
    return _tile("polymarket", status, asof, headline, payload, note,
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
    # ПОЧЕМУ сформулировано относительно нормы, а не «перегружены лонгом/шортом»:
    # z-120 мерит ОТКЛОНЕНИЕ доли от 120-дневной нормы, а не сам уровень. Уровень с 2025
    # структурно положительный, поэтому z=−2,93 стоял рядом с нетто-ЛОНГОМ +18 493
    # контракта в том же payload — заголовок «физики перегружены шортом» опровергался
    # числами тайла. Уровень назван прямо, отклонение — числом.
    # ПОЧЕМУ без «контрариан за/против роста» (аудит 02.09.2026): знак сигнала
    # сменился в 2024–2026, и вердикт по нему был бы монетой. Заголовок — факты.
    if z is None:
        verdict = "z(120д) не посчитан — истории мало"
    else:
        if z >= 1.0:
            rel = "выше своей 120-дневной нормы"
        elif z <= -1.0:
            rel = "ниже своей 120-дневной нормы"
        else:
            rel = "у своей 120-дневной нормы"
        verdict = f"z(120д) {_n(z, 2, True)}: доля {rel}"
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
                 "В решение не входит: знак сигнала сменился в 2024–2026 (аудит "
                 "02.09.2026). Нормировка перцентилем-252 сломана структурным сдвигом — "
                 "используем z-120.",
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
    base_now = _budget_base(_msk_now(now).year)
    if not u_pts or not usd_pts:
        return _empty("rub_barrel", ("База бюджетного правила " + str(base_now["year"]) +
                                     " года — " + _n(base_now["barrel_rub"], 0) +
                                     " ₽ за баррель." if base_now else
                                     "Параметров бюджета на текущий год нет."))
    u_asof, urals = u_pts[-1]
    # База — ТОГО ГОДА, которому принадлежит месяц цены: в январе налоговая цена
    # ещё декабрьская, и сравнивать её надо с базой уходящего года.
    base = _budget_base(str(u_asof)[:4])
    # Курс СРЕДНЕМЕСЯЧНЫЙ, а не последний день месяца: так определена налоговая бочка
    # в валидации (VALIDATION §A2) и ровно так её считает панельный сигнал
    # panel._urals_rub_gap. Словарь {месяц: курс} молча оставлял ПОСЛЕДНЮЮ точку месяца,
    # и тайл расходился с собственным сигналом до 5,7% (июнь-2026: 4 939 против 4 665 ₽,
    # гэп к бюджету ошибался на 5 п.п.). Нулевые курсы отбрасываем: старая запись через
    # `or` подменяла нулевой курс последней точкой, среднее бы его размазало.
    month_rates = [v for d, v in usd_pts if d[:7] == u_asof[:7] and v > 0]
    usd_for_month = sum(month_rates) / len(month_rates) if month_rates else usd_pts[-1][1]
    barrel = urals * usd_for_month
    budget_rub = base["barrel_rub"] if base else None
    gap = (barrel / budget_rub - 1.0) * 100.0 if budget_rub else None

    br_pts = _ser(store, "brent_moex")[0] or _ser(store, "brent")[0]
    measured_k = _urals_discount(store)
    k = measured_k or FALLBACK_URALS_DISCOUNT
    usd_last = usd_pts[-1][1]
    proxy = proxy_asof = None
    if br_pts:
        proxy_asof, brent = br_pts[-1]
        proxy = brent * usd_last * k
    status = _worst(_st("urals_tax", u_pts, u_meta, now), _st("usd_cbr", usd_pts, usd_meta, now))
    # Интрадей-оценка — про СЕГОДНЯ, поэтому и база у неё текущего года: 1 января
    # она переходит на новую отсечку раньше, чем месячная цена.
    proxy_base = base_now["barrel_rub"] if base_now else None
    payload = {
        "tax_barrel_rub": _r(barrel, 0),
        "tax_month": u_asof,
        "urals_usd": _r(urals, 2),
        "usd": _r(usd_for_month, 2),
        "usd_basis": ("среднемесячный" if month_rates else "последняя точка курса"),
        "budget_barrel_rub": budget_rub,
        "budget_year": base["year"] if base else None,
        "budget_cutoff_usd": base["cutoff_usd"] if base else None,
        "budget_fx": base["fx"] if base else None,
        "budget_status": base["status"] if base else None,
        "gap_pct": _r(gap, 1),
        "proxy_rub": _r(proxy, 0),
        "proxy_asof": proxy_asof,
        "proxy_budget_barrel_rub": proxy_base,
        "proxy_gap_pct": (_r((proxy / proxy_base - 1.0) * 100.0, 1)
                          if proxy and proxy_base else None),
        "discount_k": _r(k, 3),
        "discount_is_fallback": measured_k is None,
    }
    # Прокси помечается ОЦЕНКОЙ, а когда дисконт Urals не измерен — говорится и
    # это: подставленный коэффициент 0,88 против измеренного 0,75 даёт +18% к
    # печатаемой рублёвой цене, и молчать об этом нельзя.
    if proxy:
        tail = (f"; интрадей-оценка {_n(proxy, 0)} ₽"
                + (" (дисконт Urals не измерен, взят типовой)"
                   if measured_k is None else ""))
    else:
        tail = ""
    # «База правила», а не «бюджетная цена»: выше неё Минфин покупает валюту в ФНБ,
    # ниже — продаёт, а бюджет получает саму базу. Прежнее «ориентир бюджета
    # 5 440 ₽» читалось как точка, где бюджет сходится, — правило такого не обещает.
    if gap is None:
        headline = (f"Налоговая бочка {_n(barrel, 0)} ₽; параметров бюджета на "
                    f"{str(u_asof)[:4]} год нет{tail}")
    else:
        side = "ниже" if gap < 0 else "выше"
        headline = (f"Налоговая бочка {_n(barrel, 0)} ₽ — на {_n(abs(gap), 0)}% {side} "
                    f"базы бюджетного правила {_n(budget_rub, 0)} ₽{tail}")
    # ПОЧЕМУ заметка разделяет два разных гэпа: крупное число на тайле — гэп к базе
    # правила, его в валидации никто не тестировал. Валидированный сигнал — другой гэп,
    # к собственному 24-месячному тренду, и REGIME §4 прямо пишет, что эта нога плату
    # за множественность не переживает. Без явного разделения бейдж тира читается как
    # доказанность бюджетного разрыва, чего в исследовании нет.
    def _base_words(b):
        return (f"{b['cutoff_usd']:.0f} $ × {str(b['fx']).replace('.', ',')} ₽ = "
                f"{_n(b['barrel_rub'], 0)} ₽ в {b['year']} году ({b['status']})")
    base_txt = _base_words(base) if base else "параметров бюджета на этот год нет"
    nxt = _budget_base(base["year"] + 1) if base else None
    # Следующий год — отдельной фразой из той же таблицы: 1 января база сменится,
    # и читатель должен знать заранее, на что (в 2027 — отсечка 50 $ вместо 59 $:
    # при той же нефти в ФНБ уходит больше).
    next_txt = f" Следующая база: {_base_words(nxt)}." if nxt else ""
    return _tile("rub_barrel", status, u_asof, headline, payload,
                 "Крупное число — разрыв налоговой бочки с БАЗОЙ бюджетного правила: "
                 f"цена отсечения × курс бюджета, {base_txt}. Выше базы Минфин "
                 "докупает валюту в ФНБ, ниже — продаёт; бюджету достаётся сама база, "
                 "поэтому знак разрыва — это знак операций Минфина, а не «сходится ли "
                 f"бюджет».{next_txt} В валидации "
                 "этот разрыв не проверялся. Сигнал ядра — другой разрыв, к своему "
                 "24-месячному тренду, знак КОНТРАРИАН (IC −0,19), и эта нога поправку "
                 "на множественность не переживает (соло p=0,15, REGIME §4). Бочка "
                 "считается по среднемесячному курсу USD, интрадей-оценка — по последнему "
                 "и через дисконт Urals к Brent.",
                 u_meta.get("fetched_at"))


def _september_returns(store, today):
    """Лог-доходность IMOEX за календарный сентябрь по годам, %, с 2022 года.

    Ровно та величина, на которой стоит приор VALIDATION.md (строка про сентябрь):
    ln(закрытие сентября / закрытие августа). Сверено по ISS 24.09.2026: 2022 —
    −20,39, 2023 — −2,98, 2024 — +7,53 (в среднем −5,28 ≈ «−5,3% за 2022–24»),
    2025 — −7,70 («−7,7%»). Валидация велит держать приор «с ежегодной
    перепроверкой» — пока числа были зашиты текстом, перепроверку не делал никто.

    Возвращает (закрытые [(год, %)], текущий или None). Сентябрь считается
    закрытым с 1 октября; до того его число меняется каждый день и в среднее не
    идёт — иначе среднее дрейфовало бы вместе с рынком.
    """
    pts, _ = _ser(store, "imoex")
    last = {}
    for d, v in pts:
        if calc.is_num(v) and v > 0:
            last[str(d)[:7]] = (str(d)[:10], float(v))
    closed, current = [], None
    for y in range(SEP_NODE["since_year"], today.year + 1):
        aug, sep = last.get(f"{y}-08"), last.get(f"{y}-09")
        if not aug or not sep:
            continue
        ret = math.log(sep[1] / aug[1]) * 100.0
        if today >= date(y, 10, 1):
            closed.append((y, ret))
        else:
            current = {"year": y, "ret_pct": _r(ret, 2), "asof": sep[0], "closed": False}
    return closed, current


def _t_sep_node(store, now):
    """Календарный бейдж бюджетного узла; окно целиком берётся из constants.SEP_NODE.

    Дат в тексте НЕ дублируем: копия окна в докстроке уже разошлась с проектными
    документами (docs/INDICATORS.md и ARCHITECTURE.md называют 15.09–05.10, константа —
    10.09), и вторая копия только помогает расхождению дожить. Источник окна —
    календарь, поэтому статус всегда ok. Приор (сентябрьская доходность индекса)
    считается по стору: без истории индекса тайл остаётся календарным бейджем.
    """
    today = _msk_now(now).date()
    closed, current = _september_returns(store, today)
    sm, sd = SEP_NODE["start_md"]
    em, ed = SEP_NODE["end_md"]
    start = date(today.year, sm, sd)
    end = date(today.year, em, ed)
    if today > end:
        start = date(today.year + 1, sm, sd)
        end = date(today.year + 1, em, ed)
    active = start <= today <= end
    days_to = (start - today).days
    mean = (sum(r for _, r in closed) / len(closed)) if closed else None
    payload = {
        "active": active,
        "window": f"{start.strftime('%d.%m')}–{end.strftime('%d.%m')}",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "days_to_start": max(days_to, 0),
        "days_left": (end - today).days if active else None,
        "prior": SEP_NODE["note"],
        # приор, посчитанный по стору: лог-доходность IMOEX за сентябрь, %
        "septembers": [[y, _r(r, 2)] for y, r in closed],
        "sept_mean_pct": _r(mean, 2),
        "sept_n": len(closed),
        "sept_current": current,
    }
    headline = (f"Окно узла активно до {end.strftime('%d.%m')}" if active
                else f"До окна бюджетного узла {days_to} дн.")
    # Текущий сентябрь печатается рядом со средним прошлых, а в среднее не идёт:
    # 24.09.2026 он шёл +5,8% против приора −5,9% — и это ровно та перепроверка,
    # которой приор требует каждый год.
    if current and mean is not None:
        headline += (f"; сентябрь {current['year']} пока {_n(current['ret_pct'], 1, True)}% "
                     f"против {_n(mean, 1, True)}% в среднем за {closed[0][0]}–{closed[-1][0]}")
    elif mean is not None and active:
        headline += f"; сентябри {closed[0][0]}–{closed[-1][0]}: {_n(mean, 1, True)}% в среднем"
    n_txt = (f"n={len(closed)} (сентябри {closed[0][0]}–{closed[-1][0]}: "
             + ", ".join(f"{y} {_n(r, 1, True)}%" for y, r in closed) + ")"
             if closed else "истории индекса в сторе нет")
    return _tile("sep_node", "ok", today.isoformat(), headline, payload,
                 f"Сезонный приор, {n_txt}; доходность — логарифм закрытия сентября к "
                 "закрытию августа. Событийная альфа «налогового узла» опровергнута полным "
                 "списком инициатив 2016–2026, поэтому это напоминание о календаре, а не сигнал.")


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
                 "Знак сигнала зависит от эры: подтверждение до 2022, контрариан в "
                 "2025–2026 (выборка молодая — 19 месяцев); в решение не входит. "
                 "Ранний бит ворот «<40 % бумаг» живёт тенью.",
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
    # ТРИ ЧИСЛА ТАЙЛА — ИЗ ОДНОГО ДНЯ. Спред считался по последней ОБЩЕЙ дате, а
    # доходности брались хвостами своих рядов: на витрине 29,25 − 14,62 = 14,63 не
    # сходилось с напечатанным спредом 14,72, потому что база была из другого дня
    # (аудит 20.08.2026). Разошедшиеся календари — штатное дело: КБД считается не
    # каждый день, а доходность индекса могла отстать из-за поломки источника.
    hy_map = dict(hy_pts)
    hy_yield = hy_map.get(asof, hy_pts[-1][1])
    base_yield = base.get(asof, base_pts[-1][1])
    ig_spread = None
    if ig_pts:
        ig = dict(ig_pts)
        # Спред IG считаем только когда обе ноги есть в ОДИН день; иначе честный
        # None — «нет данных» лучше разности чисел из разных недель.
        if asof in ig and asof in base:
            ig_spread = ig[asof] - base[asof]
    status = _worst(_st("rucbhycp_yield", hy_pts, hy_meta, now),
                    _st("rucbcpns_yield", ig_pts, ig_meta, now) if ig_pts else "ok")
    vals = [v for _, v in pairs]
    payload = {
        "hy_yield": _r(hy_yield, 2),
        "base_label": base_label,
        "base_yield": _r(base_yield, 2),
        "spread_pp": _r(spread, 2),
        "ig_spread_pp": _r(ig_spread, 2),
        "pct_1y": _r(_pct_last(vals, 252), 0),
        "chg_21d_pp": _r((pairs[-1][1] - pairs[-22][1]) if len(pairs) > 21 else None, 2),
        "series": [[d, _r(v, 2)] for d, v in pairs[-120:]],
    }
    headline = (f"ВДО {_n(hy_yield, 1)}% — спред к {base_label} {_n(spread, 1)} п.п. "
                f"({_n(_pct_last(vals, 252), 0)}-й перцентиль за год)")
    # Панель не выдаёт свои оценки за чужие числа. Когда доходность посчитана из
    # состава индекса (биржа сломала собственный расчёт — fetch/iss.index_yield),
    # это обязано стоять в подписи: расхождение метода с биржевым до 0,4 п.п., и
    # читатель, сверяющий тайл с сайтом MOEX, должен понимать, почему числа разные.
    note = ("Широкий спред читается контрариан (премия за риск уже уплачена), "
            "сила эффекта умеренная.")
    if hy_meta.get("method") == "constituents":
        payload["method"] = "constituents"
        payload["estimate_cover_pct"] = hy_meta.get("estimate_cover_pct")
        dropped = hy_meta.get("estimate_dropped") or {}
        payload["estimate_dropped_n"] = dropped.get("count")
        payload["estimate_dropped_weight_pct"] = dropped.get("weight_pct")
        note += (" ⚠️ Доходность ВДО биржа сейчас считает неверно (в её поле приходят "
                 "сотни и тысячи процентов), поэтому значение посчитано ПАНЕЛЬЮ из "
                 f"состава индекса: покрытие {_n(hy_meta.get('estimate_cover_pct'), 1)}% "
                 "веса, сверка с биржей на здоровых днях расходится не больше чем "
                 "на 0,4 п.п. Это оценка, а не число биржи.")
        # «Покрытие 92,8%» читается как «столько нашлось», хотя на деле столько
        # ОСТАЛОСЬ после отсева битых бумаг. Разница важна для направления ошибки:
        # режутся самые доходные строки, значит оценка смещена ВНИЗ, и молчать об
        # этом нельзя — 26.08.2026 без отсева она показывала 45,9% вместо 29,7%.
        if dropped.get("count"):
            note += (f" Из корзины отброшено {dropped['count']} бумаг с битой "
                     f"доходностью (вес {_n(dropped.get('weight_pct'), 1)}%): режутся "
                     f"самые доходные строки, поэтому оценка смещена скорее вниз.")
    return _tile("hy_spread", status, asof, headline, payload, note,
                 hy_meta.get("fetched_at"))


def _t_retail(store, now):
    """Кто такие «физики» на этом рынке: доля в обороте, участие, портфель.

    Ряд `moex_retail` собирался месяцами, но показать его было негде — и когда он
    сломался, это заметили только по баннеру. Тайл делает три вещи, которых на
    панели не было:

    1. **доля физлиц в обороте акций.** Две трети оборота — это и есть причина,
       по которой потоки розницы вообще что-то значат для индекса;
    2. **участие против счетов.** Открытых счетов 41,9 млн, сделки в месяц
       заключают 3,0 млн. Разница — это разница между маркетинговым счётчиком и
       живым рынком, и путать их нельзя;
    3. **концентрация народного портфеля.** Сбербанк с префами — под 40% всего
       розничного портфеля: «розница купила рынок» на деле обычно означает
       «розница купила Сбербанк».

    Предиктивной претензии нет и быть не может: валидация прямо показывает, что
    физлица — не константа (в 2024 крупнейший нетто-ПРОДАВЕЦ года, в 2026
    выкупали падение). Тир monitor.
    """
    pts, meta = _ser(store, "moex_retail")
    payload_src = meta.get("payload") if isinstance(meta.get("payload"), dict) else {}
    if not pts and not payload_src:
        return _empty("retail", "Релиз выходит раз в месяц, 5–14 числа.")
    asof, active = _last(pts)
    total = payload_src.get("clients_total_mln")
    share = payload_src.get("share_equity_pct")
    equity = payload_src.get("inflow_equity_bln")
    # Доля ДЕЙСТВУЮЩИХ: счёт открыт у 41,9 млн, торгует 3,0 млн. Считаем здесь, а
    # не берём из релиза, — биржа этой доли не публикует, а без неё «41,9 млн
    # инвесторов» читается как 41,9 млн участников рынка.
    active_share = (active / total * 100.0) if (active and total) else None
    portfolio = _by_issuer(payload_src.get("portfolio"))
    top_name, top_share = portfolio[0] if portfolio else (None, None)
    status = _st("moex_retail", pts, meta, now)
    payload = {
        "period": meta.get("asof"),
        "share_equity_pct": _r(share, 1),
        "active_mln": _r(active, 2),
        "clients_total_mln": _r(total, 1),
        "active_share_pct": _r(active_share, 1),
        "clients_added_k": _r(payload_src.get("clients_added_k"), 0),
        "inflow_equity_bln": _r(equity, 1),
        "inflow_bonds_bln": _r(payload_src.get("inflow_bonds_bln"), 1),
        "inflow_funds_bln": _r(payload_src.get("inflow_funds_bln"), 1),
        "top_name": top_name,
        "top_share_pct": _r(top_share, 1),
        "portfolio": [{"name": name, "share_pct": _r(pct, 1)} for name, pct in portfolio[:6]],
        "news_url": meta.get("url"),
    }
    parts = []
    if share is not None:
        parts.append(f"{_n(share, 0)}% оборота акций")
    if active and total:
        parts.append(f"активны {_n(active, 1)} из {_n(total, 1)} млн счетов "
                     f"({_n(active_share, 0)}%)")
    if top_share:
        parts.append(f"доля {top_name} {_n(top_share, 0)}%")
    headline = "Физлица: " + "; ".join(parts) if parts else "релиз разобран не полностью"
    return _tile("retail", status, asof, headline, payload,
                 "Описание состава рынка, а не сигнал: валидация показывает, что физлица "
                 "не константа — в 2024 розница была крупнейшим нетто-продавцом года, "
                 "а в 2026 выкупала падение.",
                 meta.get("fetched_at"))


def _by_issuer(rows):
    """[(эмитент, доля %)] по убыванию доли, обыкновенные и привилегированные вместе.

    Биржа печатает «Сбербанка 31,8%» и «Сбербанка (прив.) 7,3%» отдельными строками
    и подряд, а не по величине. Обе строки — одна и та же ставка розницы на одного
    эмитента: без сложения концентрация занижена почти на треть (31,8 вместо 39,1),
    а без сортировки «первая строка» случайно совпадает с крупнейшей только пока
    Сбербанк стоит первым.
    """
    total = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").replace(" (прив.)", "").strip()
        pct = row.get("share_pct")
        if not name or not isinstance(pct, (int, float)) or isinstance(pct, bool):
            continue
        total[name] = total.get(name, 0.0) + float(pct)
    return sorted(total.items(), key=lambda kv: -kv[1])


# Порядок — порядок на витрине (аудит 02.09.2026): первыми то, что читается перед
# решением по ставке (цена ожиданий и заседание), дальше потоки и цены, описательные
# тайлы (малые каппы, розница) — последними. cpi_weekly снят — см. TITLES.
BUILDERS = [
    ("expectations", _t_expectations), ("cb_meeting", _t_cb_meeting), ("orfr", _t_orfr),
    ("futoi", _t_futoi), ("hy_spread", _t_hy_spread), ("rub_barrel", _t_rub_barrel),
    ("deposit_spread", _t_deposit_spread), ("dividends", _t_dividends),
    ("ofz_auctions", _t_ofz_auctions), ("rvi", _t_rvi), ("breadth", _t_breadth),
    ("polymarket", _t_polymarket), ("sep_node", _t_sep_node), ("lqdt", _t_lqdt),
    ("mcxsm", _t_mcxsm), ("retail", _t_retail),
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
