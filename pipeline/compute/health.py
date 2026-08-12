"""Здоровье модели: работает ли ядро на свежей истории или уже нет.

Смысл блока — не «оценить качество», а вовремя признать поломку. Валидация прямо
предупреждает: состав ядра выбран с участием полной выборки (честная цена p=0,03–0,11),
эра 2025–26 в OOS — 19 месяцев, а инвестор реального времени этот состав в 2010 выбрать
не мог. Поэтому единственная защита — публично считать скользящий IC и краснеть.

Два правила, которые легко нарушить и получить самообман:
  * IC считается ТОЛЬКО по ЗАВЕРШЁННЫМ месяцам. Форвардная доходность месяца M
    известна лишь после закрытия M+1, а последний месяц панели почти всегда
    незавершён — его пара «сигнал → доходность» смотрит в будущее;
  * пары не перекрываются (месячный шаг, невырожденная выборка) — дневное окно
    раздувает эффективное n примерно в 60 раз (VALIDATION.md §1, ловушка 2).
"""

from datetime import date

try:
    from ..lib import calc, constants
except ImportError:
    from lib import calc, constants

__all__ = ["compute_health"]

# Левая граница витринных серий: окно валидации 2004–2026 (REGIME.md §3).
SERIES_START = "2004-01-01"


def _status(ic, n):
    """ok / warn / dead по constants.HEALTH_THRESHOLDS."""
    if ic is None:
        return "warn"
    if n < constants.HEALTH_IC_WINDOW_MONTHS // 2:
        return "warn"  # окно ещё не набралось — судить не о чем
    if ic >= constants.HEALTH_THRESHOLDS["ok"]:
        return "ok"
    if ic >= constants.HEALTH_THRESHOLDS["warn"]:
        return "warn"
    return "dead"


def _days_between(a, b):
    try:
        return (date.fromisoformat(b[:10]) - date.fromisoformat(a[:10])).days
    except (TypeError, ValueError, AttributeError):
        return None


def compute_health(panel, mf=None, sign_since=None):
    """-> {"ic_24m","n","status","series",…}. Чистая функция: сеть и файлы не трогает."""
    if mf is None:
        # отложенный импорт: core импортирует health на уровне модуля, и обратная
        # ссылка на верхнем уровне замкнула бы круг
        try:
            from . import core as core_mod
        except ImportError:
            import core as core_mod
        mf = core_mod.monthly_frame(panel)

    labels, comp, fwd = mf["dates"], mf["composite"], mf["fwd1m"]
    n_months = len(labels)

    # Последний месяц панели незавершён → его форвард опирается на цену незакрытого
    # месяца. Отбрасываем ДВЕ последние пары: i=n-1 (форварда нет вовсе) и i=n-2
    # (форвард считался бы по неполному месяцу).
    pairs = [(labels[i], comp[i], fwd[i]) for i in range(max(0, n_months - 2))
             if calc.is_num(comp[i]) and calc.is_num(fwd[i])]

    win = constants.HEALTH_IC_WINDOW_MONTHS
    ic, n = (None, 0)
    if pairs:
        tail = pairs[-win:]
        ic, n = calc.spearman_ic([p[1] for p in tail], [p[2] for p in tail])

    # Витринная серия IC обрезана слева тем же 2004 годом, что и серия ядра
    # (core.compute_core): валидация считалась на 2004–2026, а ранние точки вдобавок
    # опираются на месяцы, где композит был одной ногой из трёх. Показывать их рядом
    # с валидированным окном — обещать историю, которой у модели нет.
    # На сам ic_24m это не влияет: он берётся с хвоста пар, а не из этой серии.
    series = []
    for k in range(win, len(pairs) + 1):
        w = pairs[k - win:k]
        if w[-1][0] < SERIES_START:
            continue
        r, _ = calc.spearman_ic([p[1] for p in w], [p[2] for p in w])
        if r is not None:
            series.append([w[-1][0], round(r, 3)])

    # Доля месяцев с данными: если композит молчит половину окна, IC считается по
    # огрызку, и «ok» на нём — иллюзия.
    tail_months = labels[-(win + 2):-2] if n_months > win + 2 else labels[:max(0, n_months - 2)]
    tail_comp = comp[-(win + 2):-2] if n_months > win + 2 else comp[:max(0, n_months - 2)]
    with_data = sum(1 for v in tail_comp if calc.is_num(v))
    coverage = round(with_data / len(tail_months), 3) if tail_months else 0.0

    # Сколько ЗАКРЫТЫХ месяцев подряд скользящий IC держится ниже нуля. Это и есть
    # величина, на которую ссылается регламент пересмотра состава (§7: «health<0 два
    # квартала подряд»): до сих пор условие было записано словами, но не измерялось —
    # алерт срабатывал на первый день статуса dead и молчал дальше.
    streak, since = 0, None
    for month, value in reversed(series):
        if value >= 0:
            break
        streak += 1
        since = month

    asof = labels[-1] if labels else None
    out = {
        "ic_24m": round(ic, 3) if ic is not None else None,
        "n": n,
        "status": _status(ic, n),
        "window_months": win,
        "coverage": coverage,
        "months_total": len(pairs),
        "below_zero_months": streak,
        "below_since": since,
        "review_months": constants.HEALTH_REVIEW_MONTHS,
        # Достигнут ПОРОГ ЗДОРОВЬЯ, а не решение о пересмотре: регламент требует ещё и
        # механизм у кандидата, а это человеческая половина условия — измерить её
        # панель не может и притворяться не должна.
        "review_due": streak >= constants.HEALTH_REVIEW_MONTHS,
        "sign_since": sign_since,
        "sign_age_days": _days_between(sign_since, asof) if sign_since and asof else None,
        "asof_month": pairs[-1][0] if pairs else None,
        "series": series,
    }
    out["note"] = _note(out)
    return out


def _note(h):
    if h["ic_24m"] is None:
        return "IC не считается: мало завершённых месяцев с данными"
    # минус подставляем только в само число: в тексте есть «24-месячный» с дефисом
    txt = f"ранговый IC за {h['n']} мес: " + f"{h['ic_24m']:+.2f}".replace("-", "−")
    if h["status"] == "dead":
        txt += " — ядро не работает на свежей истории, доверять знаку нельзя"
    elif h["status"] == "warn":
        txt += " — слабо, держать в уме широкие доверительные интервалы"
    if h.get("below_zero_months"):
        txt += (f"; ниже нуля {h['below_zero_months']} мес подряд "
                f"(с {h['below_since']}), порог регламента — {h['review_months']}")
    if h.get("review_due"):
        txt += ". Порог здоровья для пересмотра состава достигнут — нужна реколибровка"
    if h["coverage"] < 0.8:
        txt += f"; данных только за {h['coverage'] * 100:.0f}% окна"
    return txt
