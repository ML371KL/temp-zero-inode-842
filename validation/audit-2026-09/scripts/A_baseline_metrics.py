"""A_baseline: позиции всех вариантов, метрики по окнам/издержкам/исполнению, годовая таблица, бутстреп.
Выход: results/A_positions.csv, results/A_baseline_metrics.csv, results/A_baseline_yearly.csv,
       results/A_baseline_bootstrap.csv
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import pandas as pd
from A_baseline_lib import *

D, M, C = load()
P, Mc = build_positions(D, M, C)
pos_cols = [c for c in P.columns if c.startswith("pos_")]

# ------------------------------------------------------------- позиции наружу
out = P.loc[BT_START:, ["cell", "comp_closed", "sign_closed", "comp_daily", "sign_daily"] + pos_cols +
            ["ret_mcftr", "logret_mcftr", "ret_mm", "imoex", "mm_rate"]].copy()
out.index.name = "date"
out.to_csv(f"{RES}/A_positions.csv", float_format="%.10g")
print("positions saved:", out.shape, out.index[0].date(), "..", out.index[-1].date())
print("switches (2004+):", {c: int(P.loc[BT_START:, c].diff().abs().sum()) for c in pos_cols})
print("time in market (2010+):", {c: round(P.loc["2010":BT_END, c].mean(), 3) for c in pos_cols})
print("last day:", out.index[-1].date(), {c: int(out[c].iloc[-1]) for c in pos_cols},
      "comp_closed", round(out["comp_closed"].iloc[-1], 3), "comp_daily", round(out["comp_daily"].iloc[-1], 3))

# ------------------------------------------------------------- метрики
rows = []
bts = {}
for cost in (0.001, 0.002, 0.003):
    for lag in (0, 1):
        bh = backtest(P["pos_f"], P["ret_mcftr"], P["ret_mm"], cost, lag)
        mm = backtest(P["pos_g"], P["ret_mcftr"], P["ret_mm"], cost, lag)
        for v in VARIANTS:
            bt = backtest(P[f"pos_{v}"], P["ret_mcftr"], P["ret_mm"], cost, lag)
            bts[(v, cost, lag)] = bt
            for wname, (s, e) in WINDOWS.items():
                m = metrics(bt, bh, mm, s, e, ex2022=("ex-2022" in wname))
                rows.append(dict(variant=v, desc=VARIANTS[v], window=wname, cost=cost, exec=("t" if lag == 0 else "t+1"), **m))
R = pd.DataFrame(rows)
R.to_csv(f"{RES}/A_baseline_metrics.csv", index=False, float_format="%.5f")
print("metrics rows:", len(R))

def show(window, cost=0.002, ex="t+1"):
    sub = R[(R.window == window) & (R.cost == cost) & (R.exec == ex)]
    cols = ["variant", "cagr", "vol", "sharpe", "sharpe_ex_mm", "mdd", "mdd_date", "time_in_mkt", "trades_per_year",
            "years_beat_bh", "hit_month", "n_months"]
    print(f"\n=== {window} | cost {cost*100:.1f}% | exec {ex} ===")
    print(sub[cols].to_string(index=False, float_format=lambda x: f"{x:.3f}"))

for w in WINDOWS:
    show(w)
show("2010-2026 (основное)", ex="t")
show("2004-2026 (полное)", ex="t")
# чувствительность к издержкам для c
print("\n=== чувствительность (c), основное окно ===")
print(R[(R.variant == "c") & (R.window == "2010-2026 (основное)")][["cost", "exec", "cagr", "sharpe", "mdd", "trades_per_year"]]
      .to_string(index=False, float_format=lambda x: f"{x:.4f}"))

# ------------------------------------------------------------- годовая таблица (exec t+1, cost 0.2%)
lag, cost = 1, 0.002
yr = {}
for v in VARIANTS:
    nav = bts[(v, cost, lag)]["nav"].loc[BT_START:BT_END]
    base = bts[(v, cost, lag)]["nav"].loc[:BT_START].iloc[-2] if len(bts[(v, cost, lag)]["nav"].loc[:BT_START]) > 1 else nav.iloc[0]
    y = nav.groupby(nav.index.year).last()
    prev = y.shift(1); prev.iloc[0] = base
    yr[v] = (y / prev - 1)
Y = pd.DataFrame(yr)
Y["imoex_price"] = P["imoex"].loc[BT_START:BT_END].groupby(P["imoex"].loc[BT_START:BT_END].index.year).last().pct_change()
Y.loc[Y.index[0], "imoex_price"] = P["imoex"].loc[BT_START:BT_END].groupby(P["imoex"].loc[BT_START:BT_END].index.year).last().iloc[0] / P["imoex"].loc[:BT_START].iloc[-2] - 1
Y["tim_c"] = P["pos_c"].loc[BT_START:BT_END].groupby(P["pos_c"].loc[BT_START:BT_END].index.year).mean()
Y["c_minus_bh"] = Y["c"] - Y["f"]
Y["c_minus_mm"] = Y["c"] - Y["g"]
Y.index.name = "year"
Y.to_csv(f"{RES}/A_baseline_yearly.csv", float_format="%.4f")
print("\n=== годовые доходности (exec t+1, cost 0.2%) ===")
print((Y[["a", "b", "c", "d", "e", "h", "i", "f", "g", "imoex_price", "tim_c", "c_minus_bh"]] * 100).round(1).to_string())
print("c beats bh in", int((Y["c"] > Y["f"]).sum()), "of", len(Y), "years; beats mm in", int((Y["c"] > Y["g"]).sum()))

# ------------------------------------------------------------- бутстреп разности Шарпа (стационарный, средний блок 12 мес)
def stationary_bootstrap_idx(n, rng, mean_block=12):
    p = 1.0 / mean_block
    idx = np.empty(n, dtype=int)
    idx[0] = rng.integers(n)
    for i in range(1, n):
        idx[i] = rng.integers(n) if rng.random() < p else (idx[i - 1] + 1) % n
    return idx

def mdd_from_logret(r):
    nav = np.exp(np.cumsum(r)); peak = np.maximum.accumulate(nav)
    return (nav / peak - 1).min()

boot_rows = []
for wname in ["2010-2026 (основное)", "2004-2026 (полное)"]:
    s, e = WINDOWS[wname]
    for v in ["c", "a", "b", "d", "e", "h", "i"]:
        nav_s = slice_nav(bts[(v, cost, lag)]["nav"], s, e)
        nav_b = slice_nav(bts[("f", cost, lag)]["nav"], s, e)
        nav_m = slice_nav(bts[("g", cost, lag)]["nav"], s, e)
        rs = np.log(nav_s.resample("ME").last()).diff().dropna().values
        rb = np.log(nav_b.resample("ME").last()).diff().dropna().values
        rmm = np.log(nav_m.resample("ME").last()).diff().dropna().values
        n = len(rs)
        obs = sharpe_m(pd.Series(rs)) - sharpe_m(pd.Series(rb))
        obs_ex = sharpe_m(pd.Series(rs - rmm)) - sharpe_m(pd.Series(rb - rmm))
        rng = np.random.default_rng(42)
        d, d_ex, mdd_s, mdd_b, cagr_d = [], [], [], [], []
        for _ in range(2000):
            ix = stationary_bootstrap_idx(n, rng, 12)
            a, b, m_ = rs[ix], rb[ix], rmm[ix]
            d.append(sharpe_m(pd.Series(a)) - sharpe_m(pd.Series(b)))
            d_ex.append(sharpe_m(pd.Series(a - m_)) - sharpe_m(pd.Series(b - m_)))
            mdd_s.append(mdd_from_logret(a)); mdd_b.append(mdd_from_logret(b))
            cagr_d.append((a.mean() - b.mean()) * 12)
        d = np.array(d); d_ex = np.array(d_ex); mdd_s = np.array(mdd_s); mdd_b = np.array(mdd_b); cagr_d = np.array(cagr_d)
        boot_rows.append(dict(window=wname, variant=v, n_months=n, d_sharpe_obs=obs, d_sharpe_ci_lo=np.percentile(d, 2.5),
                              d_sharpe_ci_hi=np.percentile(d, 97.5), p_one_sided=float((d <= 0).mean()),
                              p_two_sided=float(min(1.0, 2 * min((d <= 0).mean(), (d >= 0).mean()))),
                              d_sharpe_ex_obs=obs_ex, p_ex_one_sided=float((d_ex <= 0).mean()),
                              d_cagr_ci_lo=np.percentile(cagr_d, 2.5), d_cagr_ci_hi=np.percentile(cagr_d, 97.5),
                              mdd_strat_p5=np.percentile(mdd_s, 5), mdd_strat_p25=np.percentile(mdd_s, 25),
                              mdd_strat_p50=np.percentile(mdd_s, 50), mdd_strat_p75=np.percentile(mdd_s, 75),
                              mdd_strat_p95=np.percentile(mdd_s, 95),
                              mdd_bh_p5=np.percentile(mdd_b, 5), mdd_bh_p50=np.percentile(mdd_b, 50), mdd_bh_p95=np.percentile(mdd_b, 95),
                              p_mdd_strat_worse=float((mdd_s < mdd_b).mean())))
B = pd.DataFrame(boot_rows)
B.to_csv(f"{RES}/A_baseline_bootstrap.csv", index=False, float_format="%.4f")
print("\n=== бутстреп разности Шарпа (стац. блок 12 мес, 2000 повторов, exec t+1, cost 0.2%) ===")
print(B[["window", "variant", "n_months", "d_sharpe_obs", "d_sharpe_ci_lo", "d_sharpe_ci_hi", "p_one_sided", "p_two_sided",
         "d_sharpe_ex_obs", "p_ex_one_sided", "mdd_strat_p5", "mdd_strat_p50", "mdd_strat_p95", "mdd_bh_p5", "mdd_bh_p50", "mdd_bh_p95",
         "p_mdd_strat_worse"]].to_string(index=False, float_format=lambda x: f"{x:.3f}"))

# ------------------------------------------------------------- дневное vs месячное наблюдение (c против e), exec t
print("\n=== c (дневное) vs e (месячное), exec t и t+1, cost 0.2% ===")
sub = R[(R.variant.isin(["c", "e", "e2"])) & (R.cost == 0.002)][["variant", "window", "exec", "cagr", "sharpe", "mdd", "trades_per_year", "time_in_mkt"]]
print(sub.sort_values(["window", "variant", "exec"]).to_string(index=False, float_format=lambda x: f"{x:.3f}"))
