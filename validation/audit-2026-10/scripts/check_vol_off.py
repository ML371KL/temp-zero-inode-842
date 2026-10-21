"""Смена редакции правила 21.10.2026: порог снятия флага волатильности p60 → p80.

Эпизод 2026 года: дно индекса 17.07, сырой флаг волы снят 18.09, а ворота с
гистерезисом p80/p60 открылись только 15.10 — через 64 торговых дня после дна. Скрипт
сравнивает на одном движке (decision.py, MCFTR / вклады, исполнение t+1, 0,2% за
смену) редакции «снятие ниже p60» (прежняя), p70 и p80 (действующая, гистерезиса волы
нет) и для справки — ворота по сырым флагам без всякого гистерезиса:
(1) годовые, Шарп над вкладами, просадка, смены, короткие отрезки ≤10 дней;
(2) чувствительность к цене смены (0,2% и 1%);
(3) смены 2026 года и счёт эпизода с выхода 22.05.

Числа — бэктест на той же истории, по которой правило выбирали: это выбор владельца
(чуткость входа ценой лишних смен), а не доказанное улучшение.
"""

from common import BASE, DATES, calc, constants, perf, positions, rle
from pipeline.compute import core as Cm, decision as Dm, states as Sm, track as Tm

HI = DATES[-1]


def variant(q_off):
    cols = dict(BASE["cols"])
    cols["vol_thresh60"] = calc.rolling_quantile(cols["realized_vol_21"], 756, q_off,
                                                 min_periods=252)
    return positions({"dates": DATES, "cols": cols})


def raw_gates():
    pan = BASE
    raw = Sm._bits(DATES, pan["cols"])
    gate = [None not in k and k != (0, 1, 1) for k in zip(raw["trend"], raw["vol"], raw["bond"])]
    comp = Dm.daily_composite(pan, Cm.monthly_frame(pan))
    dec = Dm.decision_days(DATES)
    cs = Dm.comp_state_series(comp, dec, constants.DECISION["comp_threshold"])
    return Dm.run_automaton(DATES, gate, cs, dec)[0]


def short_runs(pos, lo, max_len=10):
    runs = rle(DATES, pos)
    idx = {d: i for i, d in enumerate(DATES)}
    out = 0
    for (d0, _), (d1, _) in zip(runs, runs[1:]):
        if d0 >= lo and idx[d1] - idx[d0] <= max_len:
            out += 1
    return out


VARIANTS = [("снятие ниже p60 (до 21.10.2026)", variant(0.60)),
            ("снятие ниже p70", variant(0.70)),
            ("снятие ниже p80 (действующая)", variant(0.80)),
            ("сырые флаги, справочно", raw_gates())]

print(f"окно 2010-01-01 … {HI}")
for name, pos in VARIANTS:
    s = perf(pos, "2010-01-01", HI)
    s1 = perf(pos, "2010-01-01", HI, cost=0.01)
    print(f"  {name:34} {s}")
    print(f"  {'':34} при 1% за смену: {s1}; коротких отрезков ≤10 дней: "
          f"{short_runs(pos, '2010-01-01')}")

print("смены 2026 года")
for name, pos in VARIANTS:
    print(f"  {name:34}", [r for r in rle(DATES, pos) if r[0] >= "2026-01-01"])

# Эпизод — накопленные проценты тем же журналом, что на витрине (track.py): годовой
# темп за пять месяцев читался бы как обещание.
print(f"счёт эпизода с выхода 22.05.2026 по {HI}, накопленным итогом")
for name, pos in VARIANTS + [("удержание MCFTR", [1] * len(DATES))]:
    rows = Tm.daily_rows(DATES, pos, BASE["cols"]["mcftr"], BASE["cols"]["deposit"])
    s = Tm.summarize(rows, "2026-05-21", HI)
    print(f"  {name:34} правило {s['rule_pct']:+.1f}%, вклады {s['cash_pct']:+.1f}%, "
          f"просадка {s['mdd_pct']}%")
