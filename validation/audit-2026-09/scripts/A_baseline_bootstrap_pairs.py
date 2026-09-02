"""A_baseline: парный бутстреп разностей Шарпа (стационарный бутстреп, средний блок 12 мес, 2000 повторов)
для пар c−f (правило против b&h), c−a (приращение ворот к композиту), c−b, c−e (дневное против месячного),
d−c (дневной композит против закрытого месяца), a−f; при исполнении t и t+1; также по избыточной над ММ доходности.
Выход: results/A_baseline_bootstrap_pairs.csv
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import pandas as pd
from A_baseline_lib import *

P = pd.read_csv(f"{RES}/A_positions.csv", parse_dates=["date"]).set_index("date")


def stationary_bootstrap_idx(n, rng, mean_block=12):
    p = 1.0 / mean_block; idx = np.empty(n, dtype=int); idx[0] = rng.integers(n)
    for i in range(1, n):
        idx[i] = rng.integers(n) if rng.random() < p else (idx[i - 1] + 1) % n
    return idx


def mret(v, lag, s, e):
    bt = backtest(P[f"pos_{v}"], P["ret_mcftr"], P["ret_mm"], 0.002, lag)
    nav = slice_nav(bt["nav"], s, e)
    return np.log(nav.resample("ME").last()).diff().dropna().values


rows = []
for wname in ["2010-2026 (основное)", "2004-2026 (полное)"]:
    s, e = WINDOWS[wname]
    for lag in (0, 1):
        rm = {v: mret(v, lag, s, e) for v in ["c", "a", "b", "e", "d", "f", "g"]}
        n = len(rm["c"])
        for x, y in [("c", "f"), ("c", "a"), ("c", "b"), ("c", "e"), ("d", "c"), ("a", "f")]:
            rng = np.random.default_rng(7)
            obs = sharpe_m(pd.Series(rm[x])) - sharpe_m(pd.Series(rm[y]))
            obs_ex = sharpe_m(pd.Series(rm[x] - rm["g"])) - sharpe_m(pd.Series(rm[y] - rm["g"]))
            d = np.empty(2000); dex = np.empty(2000)
            for b in range(2000):
                ix = stationary_bootstrap_idx(n, rng, 12)
                d[b] = sharpe_m(pd.Series(rm[x][ix])) - sharpe_m(pd.Series(rm[y][ix]))
                dex[b] = sharpe_m(pd.Series(rm[x][ix] - rm["g"][ix])) - sharpe_m(pd.Series(rm[y][ix] - rm["g"][ix]))
            rows.append(dict(window=wname, exec=("t" if lag == 0 else "t+1"), pair=f"{x}-{y}", n=n, d_sharpe=obs,
                             ci_lo=np.percentile(d, 2.5), ci_hi=np.percentile(d, 97.5), p_one=(d <= 0).mean(),
                             d_sharpe_ex_mm=obs_ex, p_one_ex=(dex <= 0).mean()))
B2 = pd.DataFrame(rows)
B2.to_csv(f"{RES}/A_baseline_bootstrap_pairs.csv", index=False, float_format="%.4f")
pd.set_option("display.width", 250)
print(B2.to_string(index=False, float_format=lambda x: f"{x:.3f}"))
