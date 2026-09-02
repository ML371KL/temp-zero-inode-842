"""G_decision, шаг 4: диагностика кандидатов — погодовая таблица, журнал переключений с причинами,
текущее состояние и «что должно случиться, чтобы сигнал изменился».
Выход: results/G_yearly.csv, G_switches.csv, G_current_state.txt
"""
import os
import sys
import math
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from G_lib import *  # noqa
from G_rules import rule_pos  # noqa

CANDIDATES = [
    ("R0_h0", "R0", {"min_hold": 0}),
    ("R0_h21", "R0", {"min_hold": 21}),
    ("R1_in0.4_out0.2", "R1", {"th": 0.3, "h": 0.1, "min_hold": 0, "t_in": 0.4, "t_out": 0.2}),
    ("R1_in0.3_out0.0", "R1", {"th": 0.15, "h": 0.15, "min_hold": 0, "t_in": 0.3, "t_out": 0.0}),
    ("R1_in0.45_out0.15", "R1", {"th": 0.3, "h": 0.15, "min_hold": 0, "t_in": 0.45, "t_out": 0.15}),
]


def fmt(v, nd=2, plus=True):
    s = f"{v:+.{nd}f}" if plus else f"{v:.{nd}f}"
    return s.replace("-", "−").replace(".", ",")


def switch_reason(F, i, name, p, to_long):
    row = F.iloc[i]
    comp = row.comp_closed
    if name == "R0":
        if to_long:
            return f"ворота открыты ({row.cell}), знак закрытого месяца +, композит {fmt(comp)}"
        if not row.gate:
            return f"ворота закрыты: ячейка {row.cell}"
        return f"знак закрытого месяца − (композит {fmt(comp)})"
    if name == "R1":
        t_in, t_out = p["t_in"], p["t_out"]
        if to_long:
            return f"ворота открыты ({row.cell}) и композит {fmt(comp)} > {fmt(t_in)}"
        if not row.gate:
            return f"ворота закрыты: ячейка {row.cell} (композит {fmt(comp)})"
        return f"композит {fmt(comp)} < {fmt(t_out)}"
    return ""


def switch_log(F, pos, name, p, label):
    rows = []
    prev = 0.0
    for i in range(len(pos)):
        if pos[i] != prev:
            rows.append({"rule": label, "signal_date": F.index[i].date().isoformat(),
                         "position": "акции" if pos[i] > 0.5 else "деньги",
                         "reason": switch_reason(F, i, name, p, pos[i] > 0.5)})
            prev = pos[i]
    return rows


def current_state(F, pos, name, p, label):
    i = len(F) - 1
    row = F.iloc[i]
    j = i
    while j > 0 and pos[j - 1] == pos[i]:
        j -= 1
    since = F.index[j].date().isoformat()
    long = pos[i] > 0.5
    lines = [f"[{label}] ПОЗИЦИЯ: {'акции' if long else 'деньги'}, с {since} (сигнал на закрытии {F.index[i].date()})"]
    lines.append("  причина: " + switch_reason(F, j, name, p, long))
    # что должно случиться
    t_in = p.get("t_in", 0.0) if name == "R1" else 0.0
    t_out = p.get("t_out", 0.0) if name == "R1" else 0.0
    dd = row.bond_gap - 0.04  # log dd
    need_bond = (math.exp(-0.04) / math.exp(dd) - 1) * 100
    need_trend = (1 / (1 + row.trend_gap) - 1) * 100
    need_vol = row.vol_gap * 100
    if not long:
        lines.append("  чтобы стать «акции», нужно ОДНОВРЕМЕННО:")
        if row.toxic:
            lines.append(f"    1) выйти из токсичной ячейки — достаточно одного из: RGBI +{need_bond:.1f}% (просадка {fmt((math.exp(dd)-1)*100,1)}% → выше −3,9%), "
                         f"IMOEX +{need_trend:.1f}% (выше MA200), реализованная вола ниже порога (сейчас на {fmt(need_vol,1)} п.п. выше)")
        else:
            lines.append("    1) ворота открыты — уже выполнено")
        if name == "R1":
            ok = row.comp_closed > t_in
            lines.append(f"    2) композит закрытого месяца > {fmt(t_in)} — {'уже выполнено' if ok else 'НЕ выполнено'} (сейчас {fmt(row.comp_closed)}, дневной {fmt(row.comp_daily)})")
        else:
            ok = row.sign_closed > 0
            lines.append(f"    2) знак закрытого месяца + (гистерезис ±0,1) — {'уже выполнено' if ok else 'НЕ выполнено'} (композит {fmt(row.comp_closed)})")
    else:
        lines.append("  чтобы стать «деньги», достаточно ОДНОГО из:")
        lines.append(f"    1) ячейка станет токсичной: сейчас {row.cell}; до этого — RGBI просадка глубже −3,9% (сейчас {fmt((math.exp(dd)-1)*100,1)}%), "
                     f"IMOEX ниже MA200 (сейчас {fmt(row.trend_gap*100,1)}%), вола выше порога (сейчас {fmt(row.vol_gap*100,1)} п.п.)")
        if name == "R1":
            lines.append(f"    2) композит закрытого месяца < {fmt(t_out)} (сейчас {fmt(row.comp_closed)}, дневной {fmt(row.comp_daily)})")
        else:
            lines.append(f"    2) композит закрытого месяца < −0,1 (сейчас {fmt(row.comp_closed)}, дневной {fmt(row.comp_daily)})")
    return "\n".join(lines)


if __name__ == "__main__":
    d, c, m = load_raw()
    F = build_features(d, c, m).loc["2003-06-01":]
    bh, cash = bh_frame(F)
    yrows, srows, states = [], [], []
    yr_b = (1 + bh["ret"].fillna(0)).groupby(bh.index.year).prod() - 1
    yr_c = (1 + cash["ret"].fillna(0)).groupby(cash.index.year).prod() - 1
    Y = pd.DataFrame({"bh_mcftr": yr_b, "cash": yr_c})
    for label, name, p in CANDIDATES:
        pos = rule_pos(F, name, p)
        bt = backtest(pos, F, cost=0.002, lag=1)
        Y[label] = (1 + bt["ret"].fillna(0)).groupby(bt.index.year).prod() - 1
        Y[label + "_tim"] = bt["held"].groupby(bt.index.year).mean()
        srows += switch_log(F, pos, name, p, label)
        states.append(current_state(F, pos, name, p, label))
    Y = Y.loc[2004:]
    Y.to_csv(os.path.join(RES, "G_yearly.csv"), float_format="%.4f")
    print((Y * 100).round(1).to_string())
    S = pd.DataFrame(srows)
    S.to_csv(os.path.join(RES, "G_switches.csv"), index=False)
    for label, _, _ in CANDIDATES:
        sub = S[S.rule == label]
        print(f"\n--- переключения {label}: всего {len(sub)}, с 2022: {len(sub[sub.signal_date >= '2022'])} ---")
        print(sub[sub.signal_date >= "2020"].drop(columns=["rule"]).to_string(index=False))
    txt = "\n\n".join(states)
    print("\n" + txt)
    open(os.path.join(RES, "G_current_state.txt"), "w", encoding="utf-8").write(txt + "\n")
