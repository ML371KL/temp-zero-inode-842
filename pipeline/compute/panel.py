"""Сборка дневной панели сигналов из стора — порт validation/long_panel.py на чистый Python.

Три вещи, из-за которых этот файл выглядит занудно, и все три — пойманные грабли:

1. ЛАГ ДОСТУПНОСТИ. Значение месяца M видно не с M, а с M_end + pub_lag_days
   (registry). Аудит «первых появлений» в валидации показал 0 нарушений именно
   потому, что лаг применялся ДО выравнивания на календарь, а не после.
2. КАЛЕНДАРЬ. Панель живёт на торговом календаре IMOEX, но правило не механическое:
   * РАЗНОСТЬ двух рядов считается только на ОБЩЕМ календаре. MCFTR обязан быть
     выровнен на календарь IMOEX ffill'ом ДО сдвига на 252 дня: на «родных» датах
     MCFTR якоря сдвига разъезжаются с якорями индекса и ошибка дивдоходности
     доходит до 40 п.п. (баг, пойманный верификатором в первой итерации валидации);
   * ОКНО одного ряда считается на ЕГО СОБСТВЕННОМ календаре. rgbi_mom21 и просадка
     RGBI берутся по датам самого RGBI и лишь потом переносятся на календарь индекса,
     иначе «21 день» и «252-дневный максимум» отсчитываются по чужим торговым дням.
3. ГЭП РУБЛЁВОЙ БОЧКИ считается по МЕСЯЧНОМУ ряду (налоговая Urals × среднемесячный
   курс, скользящее среднее 24 мес), и только потом разворачивается на дни. Дневное
   504-дневное среднее по той же бочке — другой ряд и другой сигнал.

На выход — контракт docs/CONTRACT.md §4:
    build_panel(store) -> {"dates": [...], "cols": {name: [float|None, ...]}, ...}
Сети и файлов здесь нет: всё читается через переданный стор.
"""

import math
from datetime import date, timedelta

try:  # прогон как пакет: python -m pipeline.run
    from ..lib import calc, registry
except ImportError:  # прогон как python pipeline/run.py (корень sys.path = pipeline/)
    from lib import calc, registry

__all__ = ["build_panel", "PanelError"]


class PanelError(ValueError):
    """Панель собрать нечем (нет базового ряда). Всё остальное деградирует молча."""


# Курс до 2013 восстановлен из IMOEX/RTSI: RTSI — тот же индекс в долларах, поэтому
# отношение с точностью до множителя равно курсу. Коэффициент подобран на стыке 2013
# (корреляция с официальным 0,9985, REGIME.md §введение) и трогать его нельзя,
# иначе usd_mom63 до 2013 поедет уровнем.
USD_SPLICE_K = 31.4949

# Сколько торговых дней разрешено тянуть последнее значение. Числа взяты из
# long_panel.py/build_panel.py один в один (там это ffill(limit=N)); breadth — наш,
# в валидации ширина считалась на месте и протяжки не имела, но у нас это готовый
# агрегат фетчера, и один пропущенный опрос ISS не повод гасить тайл.
FFILL_LIMITS = {
    "mcftr": 5, "rgbi": 5, "zcyc": 5, "mcxsm": 3, "rvi": 3,
    "hy": 3, "brent": 7, "futoi": 3, "breadth": 3,
}


# --------------------------------------------------------------- доступ к стору
def _load(store, sid):
    """Стор может быть модулем pipeline.lib.store, объектом или просто словарём."""
    if store is None:
        return None
    getter = getattr(store, "load_series", None)
    if callable(getter):
        try:
            return getter(sid)
        except (KeyError, FileNotFoundError, OSError, ValueError):
            return None
    if isinstance(store, dict):
        return store.get(sid)
    return None


def _points(store, sid):
    ser = _load(store, sid)
    if not isinstance(ser, dict):
        return {}
    pts = ser.get("points")
    return pts if isinstance(pts, dict) else {}


def _points_sub(store, ids, subkeys):
    """Ряд с подключами (zcyc, futoi): либо отдельный series_id, либо словарь в точке.

    Реестр описывает zcyc/futoi_mx как ОДИН ряд с subkeys, а §2 контракта перечисляет
    zcyc_y1/y2/y10 как отдельные id — фетчеры пишут кто как, поэтому пробуем оба вида,
    вместо того чтобы уронить всю панель из-за расхождения в имени.
    """
    for sid in ids:
        pts = _points(store, sid)
        if not pts:
            continue
        sample = next(iter(pts.values()), None)
        if isinstance(sample, dict):
            for key in subkeys:
                if key in sample:
                    return {d: v.get(key) for d, v in pts.items() if isinstance(v, dict)}
        elif not (registry.SERIES.get(sid) or {}).get("subkeys"):
            # плоский ряд принимаем только если реестр не обещал подключей — иначе
            # «zcyc» со скалярами раздал бы одну и ту же кривую всем трём срокам
            return pts
    return {}


def _lag_days(sid):
    spec = registry.SERIES.get(sid) or {}
    try:
        return int(spec.get("pub_lag_days") or 0)
    except (TypeError, ValueError):
        return 0


def _shift(day, lag):
    if not lag:
        return day
    try:
        return (date.fromisoformat(day[:10]) + timedelta(days=lag)).isoformat()
    except ValueError:
        return day


def _align(points, dates, lag=0, limit=None):
    """{дата периода: значение} → список по торговому календарю.

    Значение становится видимым с даты ДОСТУПНОСТИ (дата периода + lag) и тянется
    вперёд не более чем limit строк календаря. Наблюдения с датами вне календаря
    (выходные, месячные метки) не теряются — они «включаются» на первом же торговом дне.
    """
    items = []
    for d, v in points.items():
        if not calc.is_num(v):
            continue
        try:
            items.append((_shift(str(d), lag), float(v)))
        except (TypeError, ValueError):
            continue
    items.sort()
    out = [None] * len(dates)
    j, cur, age = 0, None, 0
    for i, t in enumerate(dates):
        fresh = False
        while j < len(items) and items[j][0] <= t:
            cur = items[j][1]
            j += 1
            fresh = True
        if fresh:
            age = 0
        elif cur is not None:
            age += 1
        if cur is not None and (limit is None or age <= limit):
            out[i] = cur
    return out


def _col(store, sid, dates, limit=None):
    return _align(_points(store, sid), dates, _lag_days(sid), limit)


def _col_cal(store, sid, dates, limit=None):
    """Рыночный ряд НА КАЛЕНДАРЕ БИРЖИ: только точные совпадения дат + протяжка.

    Повторяет pandas `.reindex(cal).ffill(limit=N)` из валидации. Разница с _align
    не косметическая: FRED печатает Brent в дни, когда МосБиржа закрыта (новогодние
    каникулы), и если такую печать «подобрать», ряд разъезжается с валидированным
    (проверено: rb_gap уходил на 0,17 в январе 2009). Для рядов с лагом публикации
    точное совпадение бессмысленно — там работает календарь доступности.
    """
    if _lag_days(sid):
        return _align(_points(store, sid), dates, _lag_days(sid), limit)
    return calc.ffill(_exact(_points(store, sid), dates), limit)


def _exact(points, dates):
    """Только точные совпадения дат, без протяжки (нужно для стыковки курса)."""
    out = []
    for d in dates:
        v = points.get(d)
        out.append(float(v) if calc.is_num(v) else None)
    return out


def _on_cal(points, dates, limit=None):
    """Готовый словарь точек → календарь биржи (точное совпадение + протяжка)."""
    return calc.ffill(_exact(points, dates), limit)


def _sub(a, b):
    return [None if not (calc.is_num(x) and calc.is_num(y)) else x - y for x, y in zip(a, b)]


def _mul(a, b):
    return [None if not (calc.is_num(x) and calc.is_num(y)) else x * y for x, y in zip(a, b)]


def _log_ratio(a, b):
    out = []
    for x, y in zip(a, b):
        ok = calc.is_num(x) and calc.is_num(y) and x > 0 and y > 0
        out.append(math.log(x / y) if ok else None)
    return out


def _month_end_iso(day):
    """Любую метку месяца ('2015-01', '2015-01-01', '2015-01-31') — в конец месяца.

    Контракт требует хранить месячные точки датой конца месяца, но фетчеры Минфина
    исторически отдавали '2015-01', и молчаливый сдвиг лага на 30 дней здесь дороже
    десяти строк нормализации.
    """
    s = str(day)[:10]
    try:
        if len(s) == 7:
            y, m = int(s[:4]), int(s[5:7])
        else:
            d = date.fromisoformat(s)
            y, m = d.year, d.month
    except ValueError:
        return s
    nxt = date(y + (m == 12), 1 if m == 12 else m + 1, 1)
    return (nxt - timedelta(days=1)).isoformat()


# --------------------------------------------------------------------- панель
def build_panel(store):
    px_pts = _points(store, "imoex")
    dates = sorted(d for d, v in px_pts.items() if calc.is_num(v))
    if not dates:
        raise PanelError("нет ряда imoex — торговый календарь строить не из чего")
    px = [float(px_pts[d]) for d in dates]

    cols = {"imoex": px}
    cols["ret1"] = calc.log_return(px, 1)

    # ---- курс: официальный ЦБ, до 2013 — из отношения IMOEX/RTSI --------------
    rtsi_exact = _exact(_points(store, "rtsi"), dates)
    usd_exact = _exact(_points(store, "usd_cbr"), dates)
    usd_official = calc.ffill(usd_exact)  # чистый официальный курс, без склейки
    usd_raw = []
    for i in range(len(dates)):
        v = usd_exact[i]
        if v is None and calc.is_num(rtsi_exact[i]) and rtsi_exact[i] > 0:
            v = px[i] / rtsi_exact[i] * USD_SPLICE_K
        usd_raw.append(v)
    usd = calc.ffill(usd_raw)
    cols["usd"] = usd
    cols["usd_mom63"] = calc.log_return(usd, 63)

    # ---- цена индекса: тренд, просадка, моментум ------------------------------
    cols["ma200"] = calc.rolling_mean(px, 200)
    cols["dd252"] = calc.drawdown_from_max(px, 252)
    cols["mom63"] = calc.log_return(px, 63)
    cols["realized_vol_21"] = calc.realized_vol(cols["ret1"], 21)
    # Порог стресса волы: 80-й перцентиль за 756 дней (min 252) — окно ВКЛЮЧАЕТ
    # текущий день, ровно как rv.rolling(756, min_periods=252).quantile(0.8).
    cols["vol_thresh80"] = calc.rolling_quantile(cols["realized_vol_21"], 756, 0.80,
                                                 min_periods=252)

    # ---- облигации: окна считаем на СОБСТВЕННОМ календаре RGBI ----------------
    # Валидация считала log(rgbi/rgbi.shift(21)) и просадку по индексу RGBI, и лишь
    # потом переносила на календарь IMOEX. Если сначала протянуть RGBI на дни, когда
    # облигации не торговались (2022 — рынки открывались вразнобой), то и «21 день»,
    # и «252-дневный максимум» отсчитываются по чужим дням.
    rgbi_pts = _points(store, "rgbi")
    rg_dates = sorted(d for d, v in rgbi_pts.items() if calc.is_num(v))
    rg_vals = [float(rgbi_pts[d]) for d in rg_dates]
    cols["rgbi"] = _col_cal(store, "rgbi", dates, FFILL_LIMITS["rgbi"])
    cols["rgbi_mom21"] = _exact(dict(zip(rg_dates, calc.log_return(rg_vals, 21))), dates)
    cols["rgbi_dd"] = calc.ffill(
        _exact(dict(zip(rg_dates, calc.drawdown_from_max(rg_vals, 252))), dates),
        FFILL_LIMITS["rgbi"])

    # ---- дивдоходность: MCFTR ОБЯЗАН быть на календаре IMOEX ДО сдвига --------
    mcftr = _col_cal(store, "mcftr", dates, FFILL_LIMITS["mcftr"])
    mcftr_252 = calc.log_return(mcftr, 252)
    px_252 = calc.log_return(px, 252)
    cols["dy_trail"] = [None if not (calc.is_num(a) and calc.is_num(b)) else (a - b) * 100.0
                        for a, b in zip(mcftr_252, px_252)]

    # ---- ставка по вкладам и спред переключения -------------------------------
    deposit = _col(store, "deposit_decade", dates)  # декада + pub_lag 4 дня, тянем без лимита
    cols["deposit"] = deposit
    cols["switch_spread"] = _sub(cols["dy_trail"], deposit)

    # ---- нефть: дневная рублёвая бочка (Brent) --------------------------------
    brent = _col_cal(store, "brent", dates, FFILL_LIMITS["brent"])
    if not any(calc.is_num(v) for v in brent):  # FRED мог не ответить — берём фьючерс BR
        brent = _col_cal(store, "brent_moex", dates, FFILL_LIMITS["brent"])
    cols["brent"] = brent
    rb = _mul(brent, usd)
    cols["rb_gap"] = _log_ratio(rb, calc.rolling_mean(rb, 504))
    cols["brent_mom63"] = calc.log_return(brent, 63)

    # ---- гэп налоговой бочки: считается ПО МЕСЯЦАМ, потом разворачивается -----
    # Курс здесь ОФИЦИАЛЬНЫЙ, без склейки с RTSI: Минфин считает налоговую бочку по
    # курсу ЦБ, и подмешивание рыночного имплицита в дни без публикации ЦБ (5–9 января)
    # сдвигает месячное среднее на 2% — гэп уезжает на 0,01 в лог-масштабе.
    cols["urals_rub_gap"] = _urals_rub_gap(store, dates, usd_official)

    # ---- оборот -----------------------------------------------------------
    val = _col_cal(store, "imoex_value", dates)
    lv = [math.log(v) if calc.is_num(v) and v > 0 else None for v in val]
    cols["vol_z"] = calc.zscore_rolling(lv, 60)

    # ---- кривая ОФЗ -----------------------------------------------------------
    zk = FFILL_LIMITS["zcyc"]
    y1 = _on_cal(_points_sub(store, ("zcyc_y1", "zcyc"), ("y1.0", "y1", "1.0")), dates, zk)
    y2 = _on_cal(_points_sub(store, ("zcyc_y2", "zcyc"), ("y2.0", "y2", "2.0")), dates, zk)
    y10 = _on_cal(_points_sub(store, ("zcyc_y10", "zcyc"), ("y10.0", "y10", "10.0")), dates, zk)
    cols["y1"], cols["y2"], cols["y10"] = y1, y2, y10
    cols["slope_10_2"] = _sub(y10, y2)

    hy = _col_cal(store, "rucbhycp_yield", dates, FFILL_LIMITS["hy"])
    cols["hy_spread"] = [None if not (calc.is_num(h) and calc.is_num(a) and calc.is_num(b))
                         else h - (a + b) / 2.0 for h, a, b in zip(hy, y1, y2)]

    # ---- позиция физлиц: z-120, а НЕ перцентиль-252 ---------------------------
    cols["futoi_z120"] = _futoi_z120(store, dates)

    # ---- ширина рынка и малые компании ---------------------------------------
    cols["breadth"] = _col_cal(store, "breadth", dates, FFILL_LIMITS["breadth"])
    mcx = _col_cal(store, "mcxsm", dates, FFILL_LIMITS["mcxsm"])
    mcx63 = calc.log_return(mcx, 63)
    cols["mcxsm_rel63"] = _sub(mcx63, cols["mom63"])

    cols["rvi"] = _col_cal(store, "rvi", dates, FFILL_LIMITS["rvi"])
    cols["key_rate"] = _col(store, "key_rate", dates)

    return {"dates": dates, "cols": cols, "coverage": _coverage(store)}


def _urals_rub_gap(store, dates, usd):
    """log(рублёвая бочка / её 24-месячное среднее), лаг публикации из реестра.

    Бочка = налоговая цена Urals (месяц) × СРЕДНЕМЕСЯЧНЫЙ курс USD по торговым дням.
    Скользящее среднее — 24 месяца при min 12 (validation/build_panel.py), а не 504
    дневных наблюдения: месячный ряд короче и по-другому сглаживается.
    """
    urals = _points(store, "urals_tax")
    if not urals:
        return [None] * len(dates)

    usd_sum, usd_cnt = {}, {}
    for d, v in zip(dates, usd):
        if calc.is_num(v):
            k = calc.month_key(d)
            usd_sum[k] = usd_sum.get(k, 0.0) + v
            usd_cnt[k] = usd_cnt.get(k, 0) + 1

    rows = []  # (дата конца месяца, рублёвая бочка)
    seen = set()
    for d, v in sorted(urals.items()):
        if not calc.is_num(v):
            continue
        end = _month_end_iso(d)
        if end in seen:
            continue
        seen.add(end)
        k = end[:7]
        if not usd_cnt.get(k):
            continue
        rows.append((end, float(v) * usd_sum[k] / usd_cnt[k]))
    if not rows:
        return [None] * len(dates)

    rb = [r[1] for r in rows]
    mean24 = calc.rolling_mean(rb, 24, min_periods=12)
    gap = {}
    for (end, v), m in zip(rows, mean24):
        if calc.is_num(m) and m > 0 and v > 0:
            gap[end] = math.log(v / m)
    return _align(gap, dates, _lag_days("urals_tax"))


def _futoi_z120(store, dates):
    """Нетто/брутто позиция физлиц во фьючерсе на индекс, z по окну 120 дней (min 60).

    Перцентиль-252 здесь сломан: доля физиков структурно уехала с −0,4 (2020) на
    +0,57 (2025–26), и перцентиль прижимается к 1,0 — сигнал превращается в константу
    (REGIME.md §5, VALIDATION.md B2). Поэтому нормировка — короткий z.
    """
    pos = _points_sub(store, ("futoi_mx_pos", "futoi_mx"), ("pos", "fiz_pos"))
    lng = _points_sub(store, ("futoi_mx_long", "futoi_mx"), ("pos_long", "fiz_pos_long"))
    sht = _points_sub(store, ("futoi_mx_short", "futoi_mx"), ("pos_short", "fiz_pos_short"))
    ratio = {}
    for d, p in pos.items():
        a, b = lng.get(d), sht.get(d)
        if not (calc.is_num(p) and calc.is_num(a) and calc.is_num(b)):
            continue
        # Брутто-позиция группы = длинная сторона + короткая. ISS отдаёт pos_short
        # отрицательным (pos = long + short), и в валидации стояло long − short.
        # Пишем через модули: на текущем соглашении это ТО ЖЕ ЧИСЛО, но если ISS
        # однажды отдаст шорт положительным, разность тихо превратится в нетто/нетто.
        gross = abs(float(a)) + abs(float(b))
        if gross:
            ratio[d] = float(p) / gross
    if not ratio:
        return [None] * len(dates)
    # z считается уже ПОСЛЕ переноса на календарь биржи (так было в валидации):
    # окно 120 — это 120 торговых дней индекса, а не 120 срезов ОИ.
    fizsh = _on_cal(ratio, dates, FFILL_LIMITS["futoi"])
    return calc.zscore_rolling(fizsh, 120, min_periods=60)


def _coverage(store):
    """Кто из рядов чем закрыт — для бейджей источников и диагностики прогона."""
    out = {}
    for sid in registry.SERIES:
        ser = _load(store, sid)
        if not isinstance(ser, dict):
            continue
        pts = ser.get("points") or {}
        days = sorted(d for d, v in pts.items() if calc.is_num(v) or isinstance(v, dict))
        meta = ser.get("meta") or {}
        out[sid] = {
            "n": len(days),
            "first": days[0] if days else None,
            "last": days[-1] if days else None,
            "status": meta.get("status") or ("missing" if not days else "ok"),
            "fetched_at": meta.get("fetched_at"),
            "asof": meta.get("asof"),
        }
    return out
