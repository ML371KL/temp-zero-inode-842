# -*- coding: utf-8 -*-
"""Задача 3: плата за перебор. Split-sample A=2004–2017 / B=2018–2026-08 (и наоборот) по семействам ворот,
плацебо со случайными порогами того же класса, поправка «лучший из K», бутстреп разности Шарпов.
Выход: results/B_timeliness_split.csv, results/B_timeliness_placebo.csv, results/B_timeliness_bootstrap.csv"""
import sys
sys.path.insert(0, "scripts")
import numpy as np
import pandas as pd
import B_timeliness_lib as L

rng = np.random.default_rng(20260902)
df = L.load()
px = df["imoex"]
B = L.bits(df)
bh = df["r_long"]
sign = df["comp_sign"]
A, Bw = L.WINDOWS["A_2004-2017"], L.WINDOWS["B_2018-2026"]
R = pd.read_csv("results/B_timeliness_alternatives.csv")
R["family2"] = R.family.replace({"prod": "grid_k3", "prod_k2": "grid_k2"})

# ------------------------------------------------------------ 1. split по семействам
rows = []
for crit in ("gs", "go"):
    for fam in ("grid_k3", "grid_k2", "single", "addon_toxic", "addon_two3", "asym", "confirm", "ALL"):
        sub = R if fam == "ALL" else R[R.family2 == fam]
        sub = sub[sub.family != "prod_k1"]
        if len(sub) < 2:
            continue
        base = R[R.gate == "prod_toxic"].iloc[0]
        for ins, oos in (("A", "B"), ("B", "A")):
            best = sub.sort_values(f"{crit}_{ins}_sharpe", ascending=False).iloc[0]
            rank_base_ins = int((sub[f"{crit}_{ins}_sharpe"] > base[f"{crit}_{ins}_sharpe"]).sum()) + 1
            rank_base_oos = int((sub[f"{crit}_{oos}_sharpe"] > base[f"{crit}_{oos}_sharpe"]).sum()) + 1
            rho = sub[[f"{crit}_A_sharpe", f"{crit}_B_sharpe"]].corr(method="spearman").iloc[0, 1]
            rows.append(dict(criterion=crit, family=fam, n_variants=len(sub), insample=ins, oos=oos,
                             best_gate=best.gate, best_ins_sharpe=best[f"{crit}_{ins}_sharpe"],
                             best_oos_sharpe=best[f"{crit}_{oos}_sharpe"], best_oos_maxdd=best[f"{crit}_{oos}_maxdd"],
                             prod_ins_sharpe=base[f"{crit}_{ins}_sharpe"], prod_oos_sharpe=base[f"{crit}_{oos}_sharpe"],
                             prod_oos_maxdd=base[f"{crit}_{oos}_maxdd"],
                             oos_gain_vs_prod=best[f"{crit}_{oos}_sharpe"] - base[f"{crit}_{oos}_sharpe"],
                             prod_rank_ins=rank_base_ins, prod_rank_oos=rank_base_oos,
                             spearman_A_B=rho, median_oos_sharpe_family=float(sub[f"{crit}_{oos}_sharpe"].median()),
                             share_family_beats_prod_oos=float((sub[f"{crit}_{oos}_sharpe"] > base[f"{crit}_{oos}_sharpe"]).mean())))
S = pd.DataFrame(rows)
S.to_csv("results/B_timeliness_split.csv", index=False)
pd.set_option("display.width", 250); pd.set_option("display.max_columns", 40)
print("=== SPLIT: лучший на in-sample -> out-of-sample ===")
print(S.round(3).to_string(index=False))

# ------------------------------------------------------------ 2. плацебо: случайные пороги того же класса
def rand_cell(k):
    n_ma = int(rng.integers(20, 401))
    w = int(rng.integers(5, 64)); q = float(rng.uniform(0.5, 0.95))
    dd = float(rng.uniform(0.005, 0.10))
    t = px < px.rolling(n_ma).mean()
    v = L.rv(px, w); v = v > L.roll_q(v, q)
    rg = df["rgbi"]; b = np.log(rg / rg.rolling(252, min_periods=120).max()) < -dd
    return L.gate_from_bits(t, v, b, k), dict(ma=n_ma, w=w, q=q, dd=dd, k=k)

N_PL = 400
pl = []
for i in range(N_PL):
    k = 3 if i % 2 == 0 else 2
    g, p = rand_cell(k)
    row = dict(p)
    for sn, pos in (("gs", L.positions_from_gate(g, sign)), ("go", L.positions_from_gate(g))):
        r = L.strat_returns(pos, df)
        for wn, (a, b) in (("A", A), ("B", Bw), ("FULL", L.WINDOWS["FULL_2004+"]), ("MAIN", L.WINDOWS["MAIN_2010+"])):
            m = L.metrics(r, df, pos, a, b)
            row[f"{sn}_{wn}_sharpe"] = m.get("sharpe", np.nan); row[f"{sn}_{wn}_maxdd"] = m.get("maxdd", np.nan)
    gs = L.gate_stats(g, *L.WINDOWS["FULL_2004+"]); row["sw_FULL"] = gs["switches_yr"]; row["off_FULL"] = gs["off_share"]
    pl.append(row)
    if (i + 1) % 100 == 0:
        print(f"  placebo {i+1}/{N_PL}", flush=True)
P = pd.DataFrame(pl)
P.to_csv("results/B_timeliness_placebo.csv", index=False)
base = R[R.gate == "prod_toxic"].iloc[0]
print("\n=== ПЛАЦЕБО (случайные пороги MA 20–400, окно волы 5–63, квантиль 0.5–0.95, dd RGBI 0.5–10%) ===")
for crit in ("gs", "go"):
    for k in (3, 2):
        sub = P[P.k == k]
        print(f"\n{crit}, k={k}, n={len(sub)}: прод A={base[f'{crit}_A_sharpe']:.3f} B={base[f'{crit}_B_sharpe']:.3f} "
              f"MAIN={base[f'{crit}_MAIN_sharpe']:.3f} FULL={base[f'{crit}_FULL_sharpe']:.3f}")
        for wn in ("A", "B", "MAIN", "FULL"):
            s = sub[f"{crit}_{wn}_sharpe"]
            print(f"  {wn}: плацебо медиана {s.median():.3f}, p10 {s.quantile(.1):.3f}, p90 {s.quantile(.9):.3f}, "
                  f"доля плацебо >= прод: {(s >= base[f'{crit}_{wn}_sharpe']).mean():.2f}")
        # поправка «лучший из K»: выбираем лучшего в A из K случайных, смотрим в B
        for K in (50, 200, 800):
            oos = []
            for _ in range(500):
                idx = rng.integers(0, len(sub), size=K)
                j = idx[np.argmax(sub[f"{crit}_A_sharpe"].values[idx])]
                oos.append(sub[f"{crit}_B_sharpe"].values[j])
            oos = np.array(oos)
            print(f"  лучший-из-{K} по A -> B: медиана {np.median(oos):.3f}, p10 {np.quantile(oos,.1):.3f}, p90 {np.quantile(oos,.9):.3f}")

# ------------------------------------------------------------ 3. бутстреп разности Шарпов (стационарный, месячные доходности)
def monthly(r, a, b):
    return r.loc[a:b].resample("ME").sum()


def stationary_bootstrap_diff(x, y, n_boot=3000, mean_block=9):
    """Разность Шарпов (x − y) по месячным лог-доходностям, стационарный бутстреп (Politis–Romano)."""
    n = len(x); p = 1.0 / mean_block
    obs = x.mean() / x.std() * np.sqrt(12) - y.mean() / y.std() * np.sqrt(12)
    out = np.empty(n_boot)
    xv, yv = x.values, y.values
    for b in range(n_boot):
        idx = np.empty(n, dtype=int)
        i = rng.integers(0, n)
        for t in range(n):
            if t > 0 and rng.random() >= p:
                i = (i + 1) % n
            else:
                i = rng.integers(0, n)
            idx[t] = i
        xs, ys = xv[idx], yv[idx]
        out[b] = xs.mean() / xs.std() * np.sqrt(12) - ys.mean() / ys.std() * np.sqrt(12)
    return obs, np.quantile(out, [0.05, 0.5, 0.95]), float((out <= 0).mean())


G = L.build_gates(df, B, "core")
cands = ["prod_two_of_three"]
# лучшие по OOS-устойчивости: для каждого семейства — победитель в A и победитель в B
for _, s in S[S.criterion == "gs"].iterrows():
    if s.best_gate not in cands and s.best_gate in G:
        cands.append(s.best_gate)
# плюс интерпретируемые кандидаты
for c in ["cell[trend_ma100|vol_w21_p80|bond_dd4]k3", "cell[trend_ma200|vol_w21_p80|bond_dd3]k3",
          "cell[trend_ma200|vol_w21_p80|bond_dd4]k2", "asym[exit=two3|entry=!toxic&px>MA200]",
          "asym[exit=toxic|entry=px>MA200]", "toxic OR mom21_lt-10", "toxic_confirm5", "single:trend_ma200"]:
    if c not in cands and c in G:
        cands.append(c)
pos_prod = L.positions_from_gate(B["prod_toxic"], sign)
r_prod = L.strat_returns(pos_prod, df)
pos_sign = (sign > 0).astype(float)
r_sign = L.strat_returns(pos_sign, df)
bt = []
for c in cands:
    pos = L.positions_from_gate(G[c]["gate"], sign)
    r = L.strat_returns(pos, df)
    for wn, (a, b) in (("B_2018-2026", Bw), ("MAIN_2010+", L.WINDOWS["MAIN_2010+"]), ("FULL_2004+", L.WINDOWS["FULL_2004+"])):
        obs, q, pneg = stationary_bootstrap_diff(monthly(r, a, b), monthly(r_prod, a, b))
        bt.append(dict(candidate=c, vs="prod_rule", window=wn, sharpe_diff_monthly=obs, ci5=q[0], ci50=q[1], ci95=q[2], p_le0=pneg))
# и сами ворота против «без ворот» (только знак)
for wn, (a, b) in (("B_2018-2026", Bw), ("MAIN_2010+", L.WINDOWS["MAIN_2010+"]), ("FULL_2004+", L.WINDOWS["FULL_2004+"])):
    obs, q, pneg = stationary_bootstrap_diff(monthly(r_prod, a, b), monthly(r_sign, a, b))
    bt.append(dict(candidate="prod_rule", vs="sign_only(no gate)", window=wn, sharpe_diff_monthly=obs, ci5=q[0], ci50=q[1], ci95=q[2], p_le0=pneg))
    obs, q, pneg = stationary_bootstrap_diff(monthly(r_prod, a, b), monthly(bh, a, b))
    bt.append(dict(candidate="prod_rule", vs="b&h", window=wn, sharpe_diff_monthly=obs, ci5=q[0], ci50=q[1], ci95=q[2], p_le0=pneg))
BT = pd.DataFrame(bt)
BT.to_csv("results/B_timeliness_bootstrap.csv", index=False)
print("\n=== БУТСТРЕП разности Шарпов (месячные, стационарный, блок ~9 мес, 3000 реп.) ===")
print(BT.round(3).to_string(index=False))
