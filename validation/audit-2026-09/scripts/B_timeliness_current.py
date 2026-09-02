# -*- coding: utf-8 -*-
"""Задача 1: запаздывание ТЕКУЩИХ ворот (trend, vol, bond, токсичная ячейка, правило панели)
на эпизодах >15% и >25% с 2004. Выход: results/B_timeliness_current.csv (+ _summary.csv, _episodes.csv)."""
import sys
sys.path.insert(0, "scripts")
import numpy as np
import pandas as pd
import B_timeliness_lib as L

df = L.load()
px = df["imoex"]
B = L.bits(df)

eps15 = L.episodes(px, 0.15)
eps25 = L.episodes(px, 0.25)


def merge_2008(eps):
    """Каскад 2008 (5 подэпизодов) склеиваем в один: пик 2008-05-19 -> дно 2008-10-24 -> пик 2009-06-01."""
    m = eps[(eps.peak >= "2008-05-01") & (eps.peak <= "2009-01-31")]
    if len(m) <= 1:
        return eps
    merged = m.iloc[0].copy()
    j = px.loc["2008-05-19":"2009-03-01"].idxmin()
    merged["trough"] = j
    merged["trough_px"] = px.loc[j]
    merged["dd"] = px.loc[j] / merged["peak_px"] - 1
    nxt = eps[eps.peak > "2009-03-01"].iloc[0]
    merged["next_peak"] = nxt["peak"]; merged["next_px"] = nxt["peak_px"]
    merged["rise"] = nxt["peak_px"] / px.loc[j] - 1
    merged["days_fall"] = int(px.index.get_loc(j) - px.index.get_loc(merged["peak"]))
    merged["days_rise"] = int(px.index.get_loc(nxt["peak"]) - px.index.get_loc(j))
    out = pd.concat([eps[eps.peak < "2008-05-01"], merged.to_frame().T, eps[eps.peak > "2009-03-01"]])
    for c in ("dd", "rise", "peak_px", "trough_px", "next_px"):
        out[c] = out[c].astype(float)
    return out.reset_index(drop=True)


major = merge_2008(eps15)
major.to_csv("results/B_timeliness_episodes.csv", index=False)
print("Эпизоды >15% (2008 склеен):", len(major), " | >25%:", len(eps25))

# правило панели: лонг = не токсичная И знак композита > 0 -> риск-офф = НЕ лонг
rule_off = ~(L.positions_from_gate(B["prod_toxic"], df["comp_sign"]) > 0)
sign_off = ~(df["comp_sign"] > 0)

GATES = {
    "bit_trend (IMOEX<MA200)": B["prod_trend"],
    "bit_vol (rv21>p80)": B["prod_vol"],
    "bit_bond (RGBI dd<-4%)": B["prod_bond"],
    "cell_toxic (0,1,1)": B["prod_toxic"],
    "sign_composite<=0": sign_off,
    "RULE = toxic OR sign<=0": rule_off,
    "any_bit (1 of 3)": L.gate_from_bits(B["prod_trend"], B["prod_vol"], B["prod_bond"], 1),
    "two_of_three": L.gate_from_bits(B["prod_trend"], B["prod_vol"], B["prod_bond"], 2),
}

rows, summ = [], []
for label, eps in (("dd>15%", major), ("dd>25%", eps25)):
    for name, g in GATES.items():
        tl = L.timeliness(g, px, eps)
        tl.insert(0, "gate", name); tl.insert(1, "episode_set", label)
        rows.append(tl)
        s = L.summarize_timeliness(tl); s.update(gate=name, episode_set=label)
        summ.append(s)
out = pd.concat(rows, ignore_index=True)
out.to_csv("results/B_timeliness_current.csv", index=False)
S = pd.DataFrame(summ).set_index(["episode_set", "gate"])
S.to_csv("results/B_timeliness_current_summary.csv")
pd.set_option("display.width", 250); pd.set_option("display.max_columns", 30)
print(S.round(2).to_string())

print("\n=== dd>15% (2008 склеен): подробно по эпизодам для токсичной ячейки и правила ===")
for name in ("bit_trend (IMOEX<MA200)", "bit_vol (rv21>p80)", "bit_bond (RGBI dd<-4%)", "cell_toxic (0,1,1)", "RULE = toxic OR sign<=0"):
    t = out[(out.episode_set == "dd>15%") & (out.gate == name)]
    print("\n--", name)
    print(t[["peak", "trough", "dd", "exit_lag", "exit_date", "fall_before_exit", "fall_taken_share",
             "entry_lag", "entry_date", "rise_missed", "rise_missed_share"]].round(2).to_string(index=False))

# --- стратегии текущего правила: дневная и месячная перекладка, чувствительность к издержкам и лагу
bh = df["r_long"]
pos_rule = L.positions_from_gate(B["prod_toxic"], df["comp_sign"])
pos_gate = L.positions_from_gate(B["prod_toxic"])
pos_sign = (df["comp_sign"] > 0).astype(float)
# месячная перекладка: позиция обновляется только на последний торговый день месяца
me = df.index.to_series().groupby([df.index.year, df.index.month]).transform("max") == df.index.to_series()
pos_rule_m = pos_rule.where(me).ffill().fillna(0)
res = []
for nm, pos in (("b&h MCFTR", pd.Series(1.0, index=df.index)), ("100% money market", pd.Series(0.0, index=df.index)),
                ("RULE daily", pos_rule), ("RULE monthly", pos_rule_m), ("gate only daily", pos_gate),
                ("sign only", pos_sign)):
    for cost in (0.001, 0.002, 0.003):
        r = L.strat_returns(pos, df, cost=cost)
        for w, (a, b) in L.WINDOWS.items():
            m = L.metrics(r, df, pos, a, b, bh=bh); m.update(strategy=nm, cost=cost, window=w); res.append(m)
        for ex, (a, b) in L.EXCL.items():
            m = L.metrics(r, df, pos, "2004-01-06", "2026-08-31", excl=(a, b), bh=bh)
            m.update(strategy=nm, cost=cost, window="FULL_" + ex); res.append(m)
    # лаг t+1 (позиция с закрытия t+1)
    r = L.strat_returns(pos, df, cost=0.002, lag=2)
    for w, (a, b) in L.WINDOWS.items():
        m = L.metrics(r, df, pos, a, b, bh=bh); m.update(strategy=nm + " [lag t+1]", cost=0.002, window=w); res.append(m)
R = pd.DataFrame(res)
R.to_csv("results/B_timeliness_current_strategies.csv", index=False)
cols = ["cagr", "vol", "sharpe", "sharpe_ex", "maxdd", "in_mkt", "trades_yr", "hit_m", "beat_bh_yrs"]
print("\n=== Стратегии текущего правила (издержки 0.2%) ===")
sub = R[(R.cost == 0.002) & (~R.strategy.str.contains("lag"))]
for w in ("FULL_2004+", "MAIN_2010+", "2010-2021", "2022-03..2024", "2025-2026", "FULL_ex2022", "FULL_ex2008", "FULL_ex2008crash"):
    print("\n--", w)
    print(sub[sub.window == w].set_index("strategy")[cols].round(3).to_string())
print("\n=== чувствительность: RULE daily по издержкам и лагу, MAIN_2010+ ===")
print(R[(R.strategy.str.startswith("RULE daily")) & (R.window == "MAIN_2010+")].set_index(["strategy", "cost"])[cols].round(3).to_string())
