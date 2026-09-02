"""G_decision, шаг 5: (а) перцентили реальных правил в плацебо и премия за перебор; (б) экономика порога —
форвардный месяц MCFTR минус кэш по корзинам композита при открытых воротах; (в) свип лага исполнения;
(г) бутстреп ΔSharpe кандидатов против R0_h0. Выход: results/G_buckets.csv, G_lagsweep.csv, G_bootstrap_vs_R0h0.csv
"""
import os
import sys
import math
import numpy as np
import pandas as pd
from scipy import stats

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from G_lib import *  # noqa
from G_rules import rule_pos  # noqa
from G_search import FastEval  # noqa

d, c, m = load_raw()
F = build_features(d, c, m).loc["2003-06-01":]

# ---------------------------------------------------------------- (а) плацебо: перцентили
PL = pd.read_csv(os.path.join(RES, "G_placebo.csv"))
S = pd.read_csv(os.path.join(RES, "G_search.csv"))
R1 = S[S["class"] == "R1"]
best = R1.loc[R1["sharpe_train"].idxmax()]
r0 = S[(S["class"] == "R0") & (S.params == "min_hold=0")].iloc[0]
print("=== (а) плацебо ===")
print(f"реальный R1 best-by-train: train {best.sharpe_train:.3f} test {best.sharpe_test:.3f} main {best.sharpe_main:.3f}")
print(f"реальный R0_h0: train {r0.sharpe_train:.3f} test {r0.sharpe_test:.3f} main {r0.sharpe_main:.3f}")
for kind in ("shift_comp", "shift_all"):
    P = PL[PL.placebo == kind]
    pct = lambda col, v: float((P[col] < v).mean())
    bonus_pl = (P["max_main_sharpe"] - P["R0_main"])
    print(f"[{kind}] n={len(P)}: перцентиль реального max_train {pct('max_train_sharpe', best.sharpe_train):.2f}, "
          f"test_of_best {pct('test_of_train_best', best.sharpe_test):.2f}, max_main {pct('max_main_sharpe', best.sharpe_main):.2f}; "
          f"R0_test реальный {r0.sharpe_test:.2f} → перцентиль {pct('R0_test', r0.sharpe_test):.2f}, R0_main → {pct('R0_main', r0.sharpe_main):.2f}; "
          f"премия за перебор (max_main − R0_main): плацебо mean {bonus_pl.mean():.3f} p95 {bonus_pl.quantile(.95):.3f}, "
          f"реальная {best.sharpe_main - r0.sharpe_main:.3f} → перцентиль {float((bonus_pl < best.sharpe_main - r0.sharpe_main).mean()):.2f}")
# b&h в окнах
ev = FastEval(F)
bh = ev.eval(np.ones(len(F)))
print("b&h MCFTR Sharpe: train %.3f test %.3f main %.3f" % (bh["sharpe_train"], bh["sharpe_test"], bh["sharpe_main"]))

# ---------------------------------------------------------------- (б) корзины композита
print("\n=== (б) форвардный месяц MCFTR − кэш по корзинам композита закрытого месяца, ворота открыты на конце месяца ===")
me = month_end_mask(F)
ME = F[me].copy()
mc = F["mcftr"]
ME["fwd_tr"] = np.log(mc.shift(-1).reindex(ME.index).values / ME["mcftr"].values)  # placeholder, replaced below
# честная форвардная доходность: от конца месяца до следующего конца месяца
mc_me = ME["mcftr"]
ME["fwd_tr"] = np.log(mc_me.shift(-1) / mc_me)
ME["fwd_cash"] = (ME["mm_rate"] / 100.0 / 12.0)
ME["excess"] = ME["fwd_tr"] - ME["fwd_cash"]
ME = ME.loc["2004-01-01":"2026-07-31"].dropna(subset=["fwd_tr", "comp_closed"])
edges = [-9, -0.3, 0.0, 0.15, 0.3, 0.45, 0.6, 9]
labels = ["<−0.3", "−0.3…0", "0…0.15", "0.15…0.3", "0.3…0.45", "0.45…0.6", ">0.6"]
ME["bucket"] = pd.cut(ME["comp_closed"], edges, labels=labels)
rows = []
for era, (a, b) in {"all_2004_2026": ("2004-01-01", "2026-07-31"), "2004_2017": ("2004-01-01", "2017-12-31"),
                    "2018_2026": ("2018-01-01", "2026-07-31"), "2010_2026": ("2010-01-01", "2026-07-31")}.items():
    sub = ME.loc[a:b]
    for gate_state in ("open", "toxic"):
        s2 = sub[sub.gate] if gate_state == "open" else sub[sub.toxic]
        for bk, g in s2.groupby("bucket", observed=True):
            if len(g) < 3:
                continue
            t = g["excess"].mean() / g["excess"].std() * math.sqrt(len(g)) if g["excess"].std() > 0 else np.nan
            rows.append({"era": era, "gate": gate_state, "bucket": bk, "n": len(g),
                         "mean_excess_pct": g["excess"].mean() * 100, "t": t, "hit_excess": (g["excess"] > 0).mean(),
                         "mean_tr_pct": g["fwd_tr"].mean() * 100, "cash_pct": g["fwd_cash"].mean() * 100})
B = pd.DataFrame(rows)
B.to_csv(os.path.join(RES, "G_buckets.csv"), index=False, float_format="%.4f")
for era in ("all_2004_2026", "2004_2017", "2018_2026"):
    print(f"\n-- {era}, ворота открыты --")
    print(B[(B.era == era) & (B.gate == "open")].drop(columns=["era", "gate"]).to_string(index=False, float_format=lambda x: f"{x:.2f}"))
print("\n-- all, токсичная ячейка --")
print(B[(B.era == "all_2004_2026") & (B.gate == "toxic")].drop(columns=["era", "gate"]).to_string(index=False, float_format=lambda x: f"{x:.2f}"))
# Спирмен композит → избыточная доходность при открытых воротах
for era, (a, b) in {"2004_2026": ("2004-01-01", "2026-07-31"), "2010_2026": ("2010-01-01", "2026-07-31")}.items():
    sub = ME.loc[a:b]
    sub = sub[sub.gate]
    rho, p = stats.spearmanr(sub["comp_closed"], sub["excess"])
    rho2, p2 = stats.spearmanr(sub["comp_closed"], sub["fwd_tr"])
    print(f"Спирмен {era}, ворота открыты, n={len(sub)}: композит→excess {rho:+.3f} (p={p:.3f}); композит→TR {rho2:+.3f} (p={p2:.3f})")

# ---------------------------------------------------------------- (в) свип лага исполнения
print("\n=== (в) свип лага исполнения (Sharpe main 2010+ / test) ===")
lrows = []
specs = [("R0_h0", "R0", {"min_hold": 0}), ("R0_h21", "R0", {"min_hold": 21}),
         ("R1_in0.45_out0", "R1", {"th": 0.225, "h": 0.225, "min_hold": 0}),
         ("R1_in0.4_out0.2", "R1", {"th": 0.3, "h": 0.1, "min_hold": 0})]
for lag in range(0, 8):
    evL = FastEval(F, cost=0.002, lag=lag)
    for label, name, p in specs:
        r = evL.eval(rule_pos(F, name, p))
        lrows.append({"rule": label, "lag": lag, "sharpe_main": r["sharpe_main"], "sharpe_test": r["sharpe_test"],
                      "sharpe_train": r["sharpe_train"], "maxdd_main": r["maxdd_main"], "cagr_main": r["cagr_main"]})
L = pd.DataFrame(lrows)
L.to_csv(os.path.join(RES, "G_lagsweep.csv"), index=False, float_format="%.4f")
print(L.pivot(index="lag", columns="rule", values="sharpe_main").round(3).to_string())
# разложение: доходность стратегии в день после сигнала о смене
r_eq = F["mcftr"].pct_change()
for label, name, p in specs[:1]:
    pos = rule_pos(F, name, p)
    ch = np.diff(pos, prepend=0)
    idx_buy = np.where(ch > 0)[0]
    idx_sell = np.where(ch < 0)[0]
    for k in (1, 2, 3):
        rb = np.nanmean([r_eq.iloc[i + k] for i in idx_buy if i + k < len(F)]) * 100
        rs = np.nanmean([r_eq.iloc[i + k] for i in idx_sell if i + k < len(F)]) * 100
        print(f"{label}: средняя дневная доходность MCFTR через {k} дн после сигнала BUY {rb:+.2f}% (n={len(idx_buy)}), после SELL {rs:+.2f}% (n={len(idx_sell)})")

# ---------------------------------------------------------------- (г) бутстреп vs R0_h0
print("\n=== (г) бутстреп ΔSharpe против R0_h0 ===")
bhf, cash = bh_frame(F)
cands = [("R0_h21", "R0", {"min_hold": 21}), ("R0_monthly", "R0", {"monthly": True}),
         ("R1_in0.3_out0.1", "R1", {"th": 0.2, "h": 0.1, "min_hold": 0}),
         ("R1_in0.4_out0.2", "R1", {"th": 0.3, "h": 0.1, "min_hold": 0}),
         ("R1_in0.45_out0", "R1", {"th": 0.225, "h": 0.225, "min_hold": 0}),
         ("R1_in0.45_out0_h21_de", "R1", {"th": 0.225, "h": 0.225, "min_hold": 21, "daily_exit": True}),
         ("R1_in0.6_out0.15", "R1", {"th": 0.375, "h": 0.225, "min_hold": 0}),
         ("R2veto_k2_exit", "R2veto", {"k": 2, "exit_on_veto": True, "min_hold": 0}),
         ("R5_frac", "R5", {"t_full": 0.3, "t_half": -0.3, "min_hold": 0})]
ref_bt = backtest(rule_pos(F, "R0", {"min_hold": 0}), F)
brows = []
for wname in ("main_2010_2026", "full_2004_2026", "test_2018_2026", "train_2004_2017"):
    a, b = WINDOWS_BT[wname]
    ref = monthly_returns(ref_bt.loc[a:b])
    for label, name, p in cands:
        bt = backtest(rule_pos(F, name, p), F)
        mt = metrics(bt, bhf, cash, a, b)
        r = sharpe_diff_bootstrap(monthly_returns(bt.loc[a:b]), ref)
        brows.append({"rule": label, "window": wname, "sharpe": mt["sharpe"], "sharpe_ex_cash": mt["sharpe_ex_cash"],
                      "maxdd": mt["maxdd"], "cagr": mt["cagr"], "time_in_mkt": mt["time_in_mkt"],
                      "switches_per_year": mt["switches_per_year"], **r})
BB = pd.DataFrame(brows)
BB.to_csv(os.path.join(RES, "G_bootstrap_vs_R0h0.csv"), index=False, float_format="%.4f")
print(BB.to_string(index=False, float_format=lambda x: f"{x:.3f}"))
