# -*- coding: utf-8 -*-
"""Задача 5: «ворота оправданы хвостом 2008» — цена ворот без 2008 (и без 2022), по годам, даты просадок.
Выход: results/B_timeliness_ex2008.csv, results/B_timeliness_yearly.csv"""
import sys
sys.path.insert(0, "scripts")
import numpy as np
import pandas as pd
import B_timeliness_lib as L

df = L.load()
B = L.bits(df)
sign = df["comp_sign"]
bh = df["r_long"]
two3 = L.gate_from_bits(B["prod_trend"], B["prod_vol"], B["prod_bond"], 2)
STR = {
    "b&h MCFTR": pd.Series(1.0, index=df.index),
    "sign only (без ворот)": (sign > 0).astype(float),
    "RULE = toxic + sign": L.positions_from_gate(B["prod_toxic"], sign),
    "gate only (toxic)": L.positions_from_gate(B["prod_toxic"]),
    "two3 + sign": L.positions_from_gate(two3, sign),
    "trend(MA200) + sign": L.positions_from_gate(B["prod_trend"], sign),
    "trend(MA200) only": L.positions_from_gate(B["prod_trend"]),
}
RET = {k: L.strat_returns(p, df) for k, p in STR.items()}

rows = []
for nm, r in RET.items():
    pos = STR[nm]
    for wn, (a, b) in (("FULL_2004+", L.WINDOWS["FULL_2004+"]), ("MAIN_2010+", L.WINDOWS["MAIN_2010+"])):
        for ex in (None, "ex2008", "ex2008crash", "ex2022", "ex2008+ex2022"):
            if ex is None:
                m = L.metrics(r, df, pos, a, b, bh=bh)
            elif ex == "ex2008+ex2022":
                rr = r.loc[a:b]
                rr = rr[~(((rr.index >= "2008-01-01") & (rr.index <= "2008-12-31")) | ((rr.index >= "2022-01-01") & (rr.index <= "2022-12-31")))]
                m = L.metrics(rr, df, pos, bh=bh)
            else:
                m = L.metrics(r, df, pos, a, b, excl=L.EXCL[ex], bh=bh)
            m.update(strategy=nm, window=wn, excl=ex or "none"); rows.append(m)
E = pd.DataFrame(rows)
E.to_csv("results/B_timeliness_ex2008.csv", index=False)
pd.set_option("display.width", 250); pd.set_option("display.max_columns", 40)
cols = ["cagr", "vol", "sharpe", "sharpe_ex", "maxdd", "in_mkt", "trades_yr", "hit_m", "beat_bh_yrs"]
for wn in ("FULL_2004+", "MAIN_2010+"):
    for ex in ("none", "ex2008", "ex2008crash", "ex2022", "ex2008+ex2022"):
        print(f"\n=== {wn} / {ex} ===")
        print(E[(E.window == wn) & (E.excl == ex)].set_index("strategy")[cols].round(3).to_string())

# --- по годам: лог-доходность каждой стратегии
Y = pd.DataFrame({k: r.loc["2004-01-06":"2026-08-31"].groupby(r.loc["2004-01-06":"2026-08-31"].index.year).sum() for k, r in RET.items()})
Y["mm"] = df["r_flat"].loc["2004-01-06":"2026-08-31"].groupby(df.loc["2004-01-06":"2026-08-31"].index.year).sum()
Y = np.expm1(Y)
Y["gate_effect (RULE − sign only)"] = Y["RULE = toxic + sign"] - Y["sign only (без ворот)"]
Y.to_csv("results/B_timeliness_yearly.csv")
print("\n=== По годам (простые доходности) ===")
print(Y.round(3).to_string())
print("\nЭффект ворот по годам: положительный в", int((Y["gate_effect (RULE − sign only)"] > 0).sum()), "из", len(Y),
      "лет; сумма лог-эффекта без 2008:", round(float(np.log1p(Y["RULE = toxic + sign"]).sum() - np.log1p(Y["sign only (без ворот)"]).sum()
                                                    - (np.log1p(Y.loc[2008, "RULE = toxic + sign"]) - np.log1p(Y.loc[2008, "sign only (без ворот)"]))), 3))

# --- даты максимальных просадок
print("\n=== Максимальные просадки: даты ===")
for nm, r in RET.items():
    for wn, (a, b) in (("FULL_2004+", L.WINDOWS["FULL_2004+"]), ("MAIN_2010+", L.WINDOWS["MAIN_2010+"])):
        rr = r.loc[a:b]; cum = rr.cumsum(); dd = cum - cum.cummax()
        t = dd.idxmin(); pk = cum.loc[:t].idxmax()
        print(f"{nm:28s} {wn}: maxdd {np.expm1(dd.min())*100:6.1f}%  пик {pk.date()} -> дно {t.date()}")
