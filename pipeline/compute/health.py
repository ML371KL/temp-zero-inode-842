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

import math
from datetime import date

try:
    from ..lib import calc, constants
except ImportError:
    from lib import calc, constants

__all__ = ["compute_health"]

# Левая граница витринных серий: окно валидации 2004–2026 (REGIME.md §3).
SERIES_START = "2004-01-01"


def _status(ic, n, streak=0):
    """ok / warn / review — по constants.HEALTH_THRESHOLDS и HEALTH_REVIEW_MONTHS.

    Статуса «dead» больше нет (аудит 02.09.2026): при окне 24 месяца IC ниже нуля
    неотличим от нуля (se≈0,21), и слово «мертва» на карточке было приговором без
    улик. «review» выдаётся ТОЛЬКО когда ряд IC держится ниже нуля 12 месяцев подряд
    (критерий ×12: на истории рабочей модели таких серий не было), и значит он одно:
    плановая ревалидация состава — реколибровка, а не сокращение позиции.
    """
    if ic is None:
        return "warn"
    if n < constants.HEALTH_IC_WINDOW_MONTHS // 2:
        return "warn"  # окно ещё не набралось — судить не о чем
    if ic >= constants.HEALTH_THRESHOLDS["ok"]:
        return "ok"
    if streak >= constants.HEALTH_REVIEW_MONTHS:
        return "review"
    return "warn"


def status_text(status, ic=None, n=0, covers_zero=False):
    """Одна фраза под статусом — то, что читатель видит вместо слова ok/warn/review."""
    if status == "ok":
        return "связь видна"
    if status == "review":
        return "двенадцать месяцев подряд ниже нуля — плановая ревалидация состава"
    if ic is None or n < constants.HEALTH_IC_WINDOW_MONTHS // 2:
        return "данных для оценки ещё мало"
    if ic >= constants.HEALTH_THRESHOLDS["warn"]:
        return "связь слабая; интервал широкий"
    return ("связи на этом окне не видно"
            + ("; интервал накрывает ноль" if covers_zero else ""))


def ic_ci95(ic, n):
    """Грубый 95% интервал рангового IC: (низ, верх) или (None, None).

    Формула та же, что в ops/recalibrate.py, и это принципиально: отчёт
    реколибровки уже год печатает интервал рядом с IC, а живая карточка панели —
    нет. Из-за этого одно и то же число читалось на панели как приговор, а в
    отчёте — как «неотличимо от монетки».

    ПОЧЕМУ ИНТЕРВАЛ ОБЯЗАН БЫТЬ РЯДОМ. При окне 24 месяца стандартная ошибка
    рангового IC около 0,21, то есть интервал шириной ±0,41. Значение −0,08
    накрывает ноль с огромным запасом; более того, при таком n НИКАКОЕ значение
    между −0,4 и +0,4 не отличимо от нуля вовсе. Поэтому статус dead — это
    «информации нет», а не «модель сломалась», и текст обязан это говорить.
    """
    if ic is None or not isinstance(n, int) or n < 4:
        return None, None
    se = 1.0 / math.sqrt(n - 1)
    return round(ic - 1.96 * se, 3), round(ic + 1.96 * se, 3)


def _days_between(a, b):
    try:
        return (date.fromisoformat(b[:10]) - date.fromisoformat(a[:10])).days
    except (TypeError, ValueError, AttributeError):
        return None


def below_zero_streak(series):
    """Сколько ЗАКРЫТЫХ месяцев ПОДРЯД с конца скользящий IC ниже нуля -> (сколько, с какого).

    Это и есть величина, на которую ссылается регламент пересмотра состава ядра
    (docs/ARCHITECTURE.md §7: «health<0 два квартала подряд»). До 12.08.2026 условие
    было записано словами и не измерялось ничем: алерт срабатывал на ПЕРВЫЙ месяц
    ниже нуля и молчал дальше, а порог наступал через полгода — в тишине.

    Отдельной функцией, потому что иначе её невозможно проверить: ряд IC собирается
    из панели внутри compute_health, и тест поневоле переписывал бы этот цикл у себя
    и проверял собственную копию (так и было — аудит 13.08.2026).

    Ноль НЕ считается «ниже нуля»: порог статуса dead в HEALTH_THRESHOLDS строгий.
    """
    streak, since = 0, None
    for month, value in reversed(list(series or [])):
        if value is None or value >= 0:
            break
        streak += 1
        since = month
    return streak, since


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

    streak, since = below_zero_streak(series)

    asof = labels[-1] if labels else None
    # Интервал считаем от ТОГО ЖЕ числа, которое печатаем: иначе центр и границы
    # на карточке расходятся в третьем знаке, и читатель, проверивший арифметику
    # руками, находит расхождение там, где его нет. Точности это не стоит ничего —
    # интервал всё равно шириной ±0,41.
    ic_shown = round(ic, 3) if ic is not None else None
    ci_lo, ci_hi = ic_ci95(ic_shown, n)
    status = _status(ic, n, streak)
    covers_zero = bool(ci_lo is not None and ci_lo < 0 < ci_hi)
    out = {
        "ic_24m": ic_shown,
        "ic_ci95": [ci_lo, ci_hi] if ci_lo is not None else None,
        "n": n,
        "status": status,
        "status_text": status_text(status, ic, n, covers_zero),
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
    ci = h.get("ic_ci95")
    if ci and ci[0] is not None:
        span = (f"{ci[0]:+.2f}".replace("-", "−") + "; " + f"{ci[1]:+.2f}".replace("-", "−"))
        txt += f" (95% интервал [{span}])"
    covers_zero = bool(ci and ci[0] is not None and ci[0] < 0 < ci[1])
    if h["status"] == "review":
        txt += (" — двенадцать месяцев подряд ниже нуля: плановая ревалидация "
                "состава (реколибровка, не сокращение позиции)")
    elif h["status"] == "warn" and h["ic_24m"] < constants.HEALTH_THRESHOLDS["warn"]:
        # «Не работает» — утверждение, которого данные не выдерживают: при n=24
        # интервал шире самого числа втрое. Говорим то, что есть: связи на этом
        # окне НЕ ВИДНО, и это не то же самое, что «связи нет».
        txt += (" — связи на этом окне не видно"
                + ("; интервал накрывает ноль, то есть отличить модель от монетки "
                   "нечем" if covers_zero else ""))
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
