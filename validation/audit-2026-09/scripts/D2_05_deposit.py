"""D2_05: депозитная альтернатива. switch_spread (дивдоходность − вклад) уровень/изменение × фаза ставки;
лид-лаг «падение ставок по вкладам → нетто-покупки физлиц (ОРФР)»; поток физлиц и доходность.
Месячные данные: вклады с 2009-07, ОРФР физлиц 2021-04…2026-07 (n=60) — честно про мощность."""
import numpy as np
import pandas as pd
from D2_lib import *

d = derive_rates(load_daily())
m = monthly_frame(d)
o = pd.read_csv(DATA / "orfr_flows.csv"); o["m"] = pd.PeriodIndex(o["month"], freq="M")
mm = m.copy(); mm["m"] = mm.index.to_period("M")
mm = mm.merge(o[["m", "fiz", "nfo_du", "nonres", "szko"]], on="m", how="left").set_index(m.index)
mm["dep_d1"] = mm["deposit"].diff(); mm["dep_d3"] = mm["deposit"].diff(3)
mm["ret1m"] = np.log(mm["mcftr_ffill"]).diff()
mm["ret3m"] = np.log(mm["mcftr_ffill"]).diff(3)

# --- 1. switch_spread по фазам ставки: fwd1m по терцилям
print("=== switch_spread: fwd1m (MCFTR, %) по терцилям × фаза ставки, 2009-07+ ===")
sub = mm.dropna(subset=["switch_spread", "fwd1m_tr"]).copy()
sub["terc"] = pd.qcut(sub["switch_spread"], 3, labels=["низкий", "средний", "высокий"])
sub["phase"] = sub["phase_dec"].map({-1: "смягчение", 1: "ужесточение"}).fillna("нет")
tab = sub.groupby(["phase", "terc"], observed=True)["fwd1m_tr"].agg(["mean", "median", "count"])
tab["mean"] = (tab["mean"] * 100).round(2); tab["median"] = (tab["median"] * 100).round(2)
print(tab.to_string())
tab.to_csv(RES / "D2_05_switch_by_phase.csv")
rows = []
for ph in ["смягчение", "ужесточение"]:
    s = sub[sub.phase == ph]
    for sig in ["switch_spread", "switch_d63", "deposit_d63", "dep_d3"]:
        r = ic_stats(s[sig], s["fwd1m_tr"], n_boot=1000); r.update(phase=ph, signal=sig); rows.append(r)
        print(f"  {ph:12s} {sig:14s} n={r['n']} IC={r['ic']:+.3f} p={r['p_boot']:.3f}")
pd.DataFrame(rows).to_csv(RES / "D2_05_switch_ic_by_phase.csv", index=False)

# --- 2. лид-лаг: Δвклад(t) → поток физлиц(t+k); switch(t) → fiz(t+k); fiz(t) → ret(t+k)
def xcorr(x, y, lags, n_boot=2000, block=6, seed=2):
    out = []
    rng = np.random.default_rng(seed)
    for k in lags:
        df = pd.DataFrame({"x": x, "y": y.shift(-k)}).dropna()
        if len(df) < 20: out.append(dict(lag=k, n=len(df), rho=np.nan, ci_lo=np.nan, ci_hi=np.nan)); continue
        xv, yv = df["x"].values, df["y"].values
        rho = stats.spearmanr(xv, yv)[0]; bs = []
        for i in range(n_boot):
            ix = stationary_bootstrap_idx(len(xv), block, rng); bs.append(stats.spearmanr(xv[ix], yv[ix])[0])
        bs = np.array(bs); bs = bs[np.isfinite(bs)]
        out.append(dict(lag=k, n=len(df), rho=round(rho, 3), ci_lo=round(np.quantile(bs, 0.05), 3), ci_hi=round(np.quantile(bs, 0.95), 3)))
    return pd.DataFrame(out)

lags = [-3, -2, -1, 0, 1, 2, 3]
res = []
for xn, yn in [("dep_d1", "fiz"), ("dep_d3", "fiz"), ("switch_spread", "fiz"), ("switch_d63", "fiz"), ("ret1m", "fiz"), ("fiz", "ret1m"), ("fiz", "fwd1m_tr"),
               ("dep_d1", "nfo_du"), ("ret1m", "nfo_du"), ("nfo_du", "fwd1m_tr"), ("nonres", "fwd1m_tr")]:
    t = xcorr(mm[xn], mm[yn], lags); t.insert(0, "y", yn); t.insert(0, "x", xn); res.append(t)
    print(f"\nСпирмен x={xn}(t) vs y={yn}(t+k)  [k>0: x опережает y]")
    print(t.to_string(index=False))
pd.concat(res).to_csv(RES / "D2_05_leadlag.csv", index=False)

# --- 3. регрессия потока физлиц на прошлое: Δвклад(t−1), ret(t−1), ret(t) — контрарианность и ставка
import statsmodels.api as sm
df = mm[["fiz", "dep_d1", "ret1m"]].copy(); df["dep_d1_l1"] = df["dep_d1"].shift(1); df["ret_l1"] = df["ret1m"].shift(1)
df = df.dropna()
X = sm.add_constant(df[["dep_d1", "dep_d1_l1", "ret1m", "ret_l1"]]); y = df["fiz"]
fit = sm.OLS(y, X).fit(cov_type="HAC", cov_kwds={"maxlags": 3})
print("\nOLS fiz(t) ~ Δвклад(t), Δвклад(t−1), ret(t), ret(t−1)  [HAC], n=%d" % len(df))
print(fit.summary().tables[1])
