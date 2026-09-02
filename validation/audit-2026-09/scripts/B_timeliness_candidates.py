# -*- coding: utf-8 -*-
"""Сводная таблица ключевых кандидатов (CAGR, Шарп сырой и избыточный над ДР, просадка, доля в рынке,
сделки/год, hit) + бутстреп разности Шарпов против правила панели и против «только знак».
Выход: results/B_timeliness_candidates.csv, results/B_timeliness_candidates_bootstrap.csv"""
import sys
sys.path.insert(0, "scripts")
import numpy as np, pandas as pd
import B_timeliness_lib as L
rng = np.random.default_rng(7)
df = L.load(); px = df["imoex"]; B = L.bits(df); sign = df["comp_sign"]; bh = df["r_long"]
G = L.build_gates(df, B, "core")
eps15 = pd.read_csv("results/B_timeliness_episodes.csv", parse_dates=["peak", "trough", "next_peak"])
CANDS = ["prod_toxic", "prod_two_of_three", "prod_any_bit", "single:bond_dd2", "single:trend_ma50", "single:trend_ma200",
         "single:vol_w21_p80", "single:bond_dd4", "two3 OR dd252_lt-15", "toxic OR dd252_lt-15", "two3 OR mom21_lt-5",
         "cell[trend_ma50|vol_w21_p70|bond_dd2]k2", "cell[trend_ma200_h2|vol_w21_p80|bond_mom42]k3",
         "cell[trend_ma200|vol_w21_p80|bond_dd3]k3", "cell[trend_ma200|vol_w21_p80|bond_dd2]k2",
         "cell[trend_ma100|vol_w21_p80|bond_dd4]k3", "asym[exit=mom21<-8|entry=px>MA200]", "toxic_confirm_on1_off10"]
WIN = {"FULL": L.WINDOWS["FULL_2004+"], "MAIN": L.WINDOWS["MAIN_2010+"], "A": L.WINDOWS["A_2004-2017"], "B": L.WINDOWS["B_2018-2026"],
       "E1": L.WINDOWS["2010-2021"], "E2": L.WINDOWS["2022-03..2024"], "E3": L.WINDOWS["2025-2026"]}
rows = []
for c in CANDS:
    g = G[c]["gate"]
    tl = L.summarize_timeliness(L.timeliness(g, px, eps15))
    for sn, pos in (("gs", L.positions_from_gate(g, sign)), ("go", L.positions_from_gate(g))):
        r = L.strat_returns(pos, df)
        row = dict(gate=c, strat=sn, caught=tl["n_caught"], exit_lag_med=tl["exit_lag_med"], fall_taken=tl["fall_taken_mean"],
                   entry_lag_med=tl["entry_lag_med"], rise_missed=tl["rise_missed_share_mean"], **{f"sw_yr": L.gate_stats(g, *WIN["FULL"])["switches_yr"]})
        for wn, (a, b) in WIN.items():
            m = L.metrics(r, df, pos, a, b, bh=bh)
            for k in ("cagr", "sharpe", "sharpe_ex", "maxdd", "in_mkt", "trades_yr", "hit_m", "beat_bh_yrs"):
                row[f"{wn}_{k}"] = m.get(k, np.nan)
        m = L.metrics(r, df, pos, *WIN["FULL"], excl=L.EXCL["ex2022"], bh=bh); row["ex2022_sharpe"] = m["sharpe"]; row["ex2022_maxdd"] = m["maxdd"]
        m = L.metrics(r, df, pos, *WIN["FULL"], excl=L.EXCL["ex2008crash"], bh=bh); row["ex2008_sharpe"] = m["sharpe"]; row["ex2008_maxdd"] = m["maxdd"]
        rows.append(row)
# базы
for nm, pos in (("b&h", pd.Series(1.0, index=df.index)), ("sign_only", (sign > 0).astype(float)), ("money_market", pd.Series(0.0, index=df.index))):
    r = L.strat_returns(pos, df); row = dict(gate=nm, strat="-")
    for wn, (a, b) in WIN.items():
        m = L.metrics(r, df, pos, a, b, bh=bh)
        for k in ("cagr", "sharpe", "sharpe_ex", "maxdd", "in_mkt", "trades_yr", "hit_m", "beat_bh_yrs"):
            row[f"{wn}_{k}"] = m.get(k, np.nan)
    rows.append(row)
T = pd.DataFrame(rows); T.to_csv("results/B_timeliness_candidates.csv", index=False)
pd.set_option("display.width", 250); pd.set_option("display.max_columns", 50); pd.set_option("display.max_colwidth", 46)
T["gate"] = T.gate.str.replace("trend_", "t.").str.replace("vol_", "v.").str.replace("bond_", "b.")
for sn in ("gs", "go"):
    print(f"\n=== {sn} ===")
    cols = ["gate", "caught", "exit_lag_med", "fall_taken", "entry_lag_med", "rise_missed", "sw_yr",
            "FULL_cagr", "FULL_sharpe", "FULL_sharpe_ex", "FULL_maxdd", "FULL_in_mkt", "FULL_trades_yr",
            "MAIN_cagr", "MAIN_sharpe", "MAIN_sharpe_ex", "MAIN_maxdd", "MAIN_in_mkt", "A_sharpe", "B_sharpe", "E1_sharpe", "E2_sharpe", "E3_sharpe", "E3_maxdd", "ex2022_sharpe", "ex2008_sharpe"]
    print(T[(T.strat == sn) | (T.strat == "-")][cols].round(2).to_string(index=False))

# бутстреп
def monthly(r, a, b): return r.loc[a:b].resample("ME").sum()
def sb_diff(x, y, n_boot=3000, mean_block=9):
    n = len(x); p = 1.0 / mean_block; xv, yv = x.values, y.values
    obs = x.mean() / x.std() * np.sqrt(12) - y.mean() / y.std() * np.sqrt(12)
    out = np.empty(n_boot)
    for bb in range(n_boot):
        idx = np.empty(n, dtype=int); i = rng.integers(0, n)
        for t in range(n):
            i = (i + 1) % n if (t > 0 and rng.random() >= p) else rng.integers(0, n)
            idx[t] = i
        xs, ys = xv[idx], yv[idx]
        out[bb] = xs.mean() / xs.std() * np.sqrt(12) - ys.mean() / ys.std() * np.sqrt(12)
    return obs, np.quantile(out, [0.05, 0.5, 0.95]), float((out <= 0).mean())
r_prod = L.strat_returns(L.positions_from_gate(B["prod_toxic"], sign), df)
r_sign = L.strat_returns((sign > 0).astype(float), df)
bt = []
for c in ["prod_any_bit", "single:bond_dd2", "two3 OR dd252_lt-15", "single:trend_ma50", "two3 OR mom21_lt-5", "cell[trend_ma200|vol_w21_p80|bond_dd2]k2"]:
    r = L.strat_returns(L.positions_from_gate(G[c]["gate"], sign), df)
    for wn in ("FULL", "MAIN", "A", "B"):
        a, b = WIN[wn]
        for vs, rr in (("prod_rule", r_prod), ("sign_only", r_sign)):
            obs, q, pneg = sb_diff(monthly(r, a, b), monthly(rr, a, b))
            bt.append(dict(candidate=c, vs=vs, window=wn, diff=obs, ci5=q[0], ci95=q[2], p_le0=pneg))
BT = pd.DataFrame(bt); BT.to_csv("results/B_timeliness_candidates_bootstrap.csv", index=False)
print("\n=== БУТСТРЕП (месячные, стационарный, блок 9, 3000) ===")
print(BT.round(3).to_string(index=False))
