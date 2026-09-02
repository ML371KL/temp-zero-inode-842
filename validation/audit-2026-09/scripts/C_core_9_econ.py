"""C_core задача 9 — гипотезы из рецензии экономиста и коллег:
(а) «плохое» крутизнение (Δslope63>0 & Δy10_63>0) vs «хорошее» (Δslope63>0 & Δy10_63≤0) и модификации ноги наклона;
(б) режимный фильтр ноги рубля: вклад 0 при vol=1 и первые 3 мес после выхода из токсичной ячейки;
(в) гистерезис 0,4–0,6: месячный шаг на split (из 3b) против недельного шага (сетка порогов на неделях);
(г) CUSUM «умения тайминга» = сумма (pos − p̄)(r_tr − r_mm): не зависит от направления рынка."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from C_core_lib import *
import statsmodels.api as sm

D, M, C = load()
me = month_end_idx(D)
mk = monthly_market(D, C, me)
Mm = M.reindex(me)
fwd = mk["fwd_imoex"]
gate = (Mm["cell"] != TOXIC).astype(float)
Z = pd.DataFrame({k: sgn * Mm["z_" + k] for k, sgn in LEGS})
splits = {"A' 2016-02..2020": ("2016-02-01", "2020-12-31"), "B' 2021+": ("2021-01-01", "2026-08-31"),
          "A ..2017": ("2004-01-01", "2017-12-31"), "B 2018+": ("2018-01-01", "2026-08-31"), "MAIN": MAIN, "2025-26": ("2025-01-01", "2026-08-31")}
ALL_ROWS = []


def evalc(comp, name, hyst=0.1, use_gate=True, tag=""):
    hs = hysteresis_sign(comp, hyst)
    pos = (hs > 0).astype(float) * (gate if use_gate else 1.0)
    out = []
    for sname, (a, b) in splits.items():
        m = (comp.index >= a) & (comp.index <= b)
        r = ic_stats(comp[m], fwd[m], n_boot=300)
        mt = metrics(backtest(pos, mk, start=a, end=b))
        out.append(dict(block=tag, variant=name, split=sname, n=r["n"], ic=r["ic"], p_boot=r["p_boot"], sharpe=mt.get("sharpe"), cagr=mt.get("cagr"), mdd=mt.get("maxdd"), trades_yr=mt.get("trades_yr")))
    ALL_ROWS.extend(out)
    print(f"{name:52s}" + " | ".join(f"{o['split']}: IC {o['ic']:+.3f} Sh {o['sharpe']:.2f}" for o in out))
    return out


# ---------------------------------------------------------------- (а) наклон
print("=== (а) крутизнение по определению экономиста (2015+, месячные срезы) ===")
dslope = (D["slope_10_2"] - D["slope_10_2"].shift(63)).reindex(me)
dy10 = (D["y10"] - D["y10"].shift(63)).reindex(me)
dy2 = (D["y2"] - D["y2"].shift(63)).reindex(me)
zs = Z["slope_10_2"]
m = zs.notna() & (me <= pd.Timestamp(MAIN[1]))
bad = ((dslope > 0) & (dy10 > 0)).fillna(False)
good = ((dslope > 0) & (dy10 <= 0)).fillna(False)
flat = (dslope <= 0).fillna(False)
rows = []
for cname, cm in (("плохое: dslope>0 & dy10>0", bad), ("хорошее: dslope>0 & dy10<=0", good), ("уплощение: dslope<=0", flat),
                  ("плохое & dy2>=0 (только длинный конец)", bad & (dy2 >= 0).fillna(False)), ("плохое & dy2<0 (оба конца)", bad & (dy2 < 0).fillna(False))):
    mm_ = m & cm
    r = ic_stats(zs[mm_], fwd[mm_], n_boot=300)
    rc = ic_stats(Mm["composite"][mm_], fwd[mm_], n_boot=300)
    print(f"{cname:42s} n={r['n']:3d} fwd ср. {fwd[mm_].mean()*100:+.2f}% hit {(fwd[mm_]>0).mean():.2f} | IC z_slope {r['ic']:+.3f} (p={r['p_boot']:.2f}) | IC композита {rc['ic']:+.3f}")
    rows.append(dict(cond=cname, n=r["n"], fwd_mean=fwd[mm_].mean(), hit=(fwd[mm_] > 0).mean(), ic_slope=r["ic"], p=r["p_boot"], ic_comp=rc["ic"]))
pd.DataFrame(rows).to_csv(f"{RES}/C_core_9a_steepening.csv", index=False, float_format="%.4f")
d3 = pd.DataFrame({"f": fwd[m], "z": zs[m], "bad": bad[m].astype(float)}).dropna()
d3["z_bad"] = d3["z"] * d3["bad"]
mod = sm.OLS(d3["f"], sm.add_constant(d3[["z", "z_bad", "bad"]])).fit(cov_type="HAC", cov_kwds={"maxlags": 3})
print("fwd ~ z_slope + z_slope*I(bad) + I(bad), NW(3): " + ", ".join(f"{k}={mod.params[k]*100:+.2f}% (t={mod.tvalues[k]:+.2f})" for k in ["z", "z_bad", "bad"]) + f"; n={len(d3)}")
print("модификации ноги наклона в композите (остальные ноги прод):")
base = Z.mean(axis=1)
evalc(base, "прод", tag="a")
z_mod0 = Z.copy(); z_mod0["slope_10_2"] = zs.where(~bad, 0.0)
evalc(z_mod0.mean(axis=1), "наклон = 0 при плохом крутизнении", tag="a")
z_mod1 = Z.copy(); z_mod1["slope_10_2"] = zs.where(~bad, np.nan)
evalc(z_mod1.mean(axis=1), "наклон выпадает (n_used-1) при плохом крутизнении", tag="a")
z_mod2 = Z.copy(); z_mod2["slope_10_2"] = zs.where(~bad, -zs)
evalc(z_mod2.mean(axis=1), "знак наклона перевёрнут при плохом крутизнении", tag="a")
z_mod3 = Z.copy(); z_mod3["slope_10_2"] = zs.where(~(dy10 > 0).fillna(False), 0.0)
evalc(z_mod3.mean(axis=1), "наклон = 0 при любом росте y10 за 63д", tag="a")

# ---------------------------------------------------------------- (б) рубль
print("\n=== (б) режимный фильтр ноги рубля ===")
tox = (Mm["cell"] == TOXIC)
post_tox = pd.Series(False, index=me)
for i in range(1, 4):
    post_tox |= tox.shift(i).fillna(False).astype(bool)
post_tox &= ~tox
vol1 = (Mm["st_vol"] == 1).fillna(False)
zu = Z["usd_mom63"]
for cname, cm in (("vol=1", vol1), ("первые 3 мес после выхода из токсичной", post_tox), ("vol=1 или пост-токсичные 3 мес", vol1 | post_tox), ("остальные", ~(vol1 | post_tox))):
    mm_ = cm & (me >= pd.Timestamp(MAIN[0])) & (me <= pd.Timestamp(MAIN[1]))
    r = ic_stats(zu[mm_], fwd[mm_], n_boot=300)
    rc = ic_stats(Mm["composite"][mm_], fwd[mm_], n_boot=300)
    print(f"  IC usd при [{cname}]: {r['ic']:+.3f} (n={r['n']}, p={r['p_boot']:.2f}); IC композита там же {rc['ic']:+.3f}; fwd ср. {fwd[mm_].mean()*100:+.2f}%")
evalc(base, "прод", tag="b")
zf1 = Z.copy(); zf1["usd_mom63"] = zu.where(~vol1, np.nan)
evalc(zf1.mean(axis=1), "F1: usd выпадает при vol=1", tag="b")
zf2 = Z.copy(); zf2["usd_mom63"] = zu.where(~post_tox, np.nan)
evalc(zf2.mean(axis=1), "F2: usd выпадает 3 мес после токсичной", tag="b")
zf3 = Z.copy(); zf3["usd_mom63"] = zu.where(~(vol1 | post_tox), np.nan)
evalc(zf3.mean(axis=1), "F3: F1+F2", tag="b")
zf4 = Z.copy(); zf4["usd_mom63"] = zu.where(~vol1, 0.0)
evalc(zf4.mean(axis=1), "F4: usd = 0 (не выпадает) при vol=1", tag="b")
print("без ворот (ядро соло), чтобы увидеть 2008:")
evalc(base, "прод, без ворот", use_gate=False, tag="b")
evalc(zf1.mean(axis=1), "F1, без ворот", use_gate=False, tag="b")
sub = pd.DataFrame({"comp_prod": base, "comp_F1": zf1.mean(axis=1), "vol": Mm["st_vol"], "fwd": fwd}).loc["2008-06-30":"2009-03-31"]
print(sub.round(3).to_string())

# ---------------------------------------------------------------- (в) гистерезис недельный vs месячный
print("\n=== (в) гистерезис: недельный шаг (дневной композит как в проде, ворота недельные), сетка порогов ===")
Zd = pd.DataFrame({k: sgn * daily_z_prod(D, me, k) for k, sgn in LEGS})
comp_d = Zd.mean(axis=1)
wk = D.index.to_period("W")
we = pd.DatetimeIndex(pd.Series(D.index, index=D.index).groupby(wk).last().values)
comp_w = comp_d.reindex(we)
gate_w = (D["cell"].reindex(we) != TOXIC).astype(float)
tr = C["mcftr_ffill"].reindex(D.index).ffill()
mmr = C["mm_rate"].reindex(D.index).ffill() / 100 / 252
cum_mm = (1 + mmr).cumprod()
tr_w = tr.reindex(we)
mk_w = pd.DataFrame({"fwd_tr": tr_w.shift(-1) / tr_w - 1, "fwd_mm": cum_mm.reindex(we).shift(-1) / cum_mm.reindex(we) - 1})


def bt_w(pos, a, b, cost=0.002):
    df = mk_w.copy()
    df["pos"] = pos.reindex(df.index).fillna(0)
    df = df[(df.index >= a) & (df.index <= b)].dropna()
    df["trade"] = (df["pos"] != df["pos"].shift(1).fillna(0)).astype(int)
    df["ret"] = df["pos"] * df["fwd_tr"] + (1 - df["pos"]) * df["fwd_mm"] - cost * df["trade"]
    r = df["ret"]; yrs = len(r) / 52; cum = (1 + r).cumprod()
    return dict(sharpe=r.mean() / r.std() * np.sqrt(52), cagr=cum.iloc[-1] ** (1 / yrs) - 1, mdd=(cum / cum.cummax() - 1).min(), trades_yr=df["trade"].sum() / yrs, time_in=df["pos"].mean())


rows = []
for sname, (a, b) in {"2010-17": ("2010-01-01", "2017-12-31"), "2018+": ("2018-01-01", "2026-08-31"), "MAIN": MAIN}.items():
    line = f"{sname:8s}"
    for h in (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8):
        pos = (hysteresis_sign(comp_w, h) > 0).astype(float) * gate_w
        mt = bt_w(pos, a, b)
        rows.append(dict(step="weekly", split=sname, hyst=h, **mt))
        line += f" | {h:.1f}: Sh {mt['sharpe']:.2f} MDD {mt['mdd']*100:.0f}% tr/y {mt['trades_yr']:.1f}"
    print(line)
print("месячный шаг (из 3b), те же пороги:")
G = pd.read_csv(f"{RES}/C_core_3b_hyst_grid.csv")
for sname in ["A ..2017", "B 2018+", "MAIN"]:
    g = G[G.split == sname].set_index("hyst")
    print(f"{sname:8s}" + " | ".join(f"{h:.1f}: Sh {g.loc[h,'sharpe']:.2f} MDD {g.loc[h,'maxdd']*100:.0f}% tr/y {g.loc[h,'trades_yr']:.1f}" for h in (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8)))
cross = 0; tot = 0
hs_m = hysteresis_sign(Mm["composite"], 0.4)
for i in range(1, len(me)):
    seg = comp_d.loc[me[i - 1]:me[i]].iloc[1:]
    if seg.notna().sum() == 0 or not np.isfinite(hs_m.iloc[i - 1]):
        continue
    tot += 1
    s = hs_m.iloc[i - 1]
    if ((s > 0) and (seg < -0.4).any() and not (Mm["composite"].iloc[i] < -0.4)) or ((s < 0) and (seg > 0.4).any() and not (Mm["composite"].iloc[i] > 0.4)):
        cross += 1
print(f"месяцев, где ВНУТРИ месяца дневной композит пробивал +-0,4 в новую сторону, а срез конца месяца — нет: {cross} из {tot} ({cross/tot:.0%}) — недельный шаг ловит эти ложные пробои")
pd.DataFrame(rows).to_csv(f"{RES}/C_core_9c_weekly_hyst.csv", index=False, float_format="%.4f")

# ---------------------------------------------------------------- (г) CUSUM умения тайминга
print("\n=== (г) CUSUM умения тайминга: c_t = (pos_t - p_hist)*(r_tr - r_mm); ожидание 0 без умения при любом рынке ===")
pos = panel_positions(Mm)
bt = backtest(pos, mk, start=MAIN[0], end=MAIN[1])
ex_mkt = bt["fwd_tr"] - bt["fwd_mm"]
pbar = bt["pos"].loc[:"2024-12-31"].mean()
c = (bt["pos"] - pbar) * ex_mkt
ref = c.loc[:"2024-12-31"]
mu, sd = ref.mean(), ref.std()
k = len(c.loc["2025-01-01":])
cus = ((c.loc["2025-01-01":] - mu) / sd).cumsum()
rng = np.random.default_rng(9)
mins = np.array([((ref.values[stationary_bootstrap_idx(len(ref), 6, rng)][:k] - mu) / sd).cumsum().min() for _ in range(4000)])
print(f"p_hist(2010-24)={pbar:.2f}; умение в 2010-24: {mu*100:+.2f}%/мес (t={mu/sd*np.sqrt(len(ref)):+.2f}); 2025-26: {c.loc['2025-01-01':].mean()*100:+.2f}%/мес (k={k})")
print(f"CUSUM с 2025-01: конечное {cus.iloc[-1]:+.2f}s, минимум {cus.min():+.2f}s ({cus.idxmin().date()}); квантили минимума при [умение как раньше]: 5% {np.percentile(mins,5):+.2f}, 10% {np.percentile(mins,10):+.2f}, 25% {np.percentile(mins,25):+.2f}, 50% {np.percentile(mins,50):+.2f}")
pos2 = panel_positions(Mm, gate=False)
bt2 = backtest(pos2, mk, start=MAIN[0], end=MAIN[1])
c2 = (bt2["pos"] - bt2["pos"].loc[:"2024-12-31"].mean()) * (bt2["fwd_tr"] - bt2["fwd_mm"])
ref2 = c2.loc[:"2024-12-31"]; mu2, sd2 = ref2.mean(), ref2.std()
cus2 = ((c2.loc["2025-01-01":] - mu2) / sd2).cumsum()
mins2 = np.array([((ref2.values[stationary_bootstrap_idx(len(ref2), 6, rng)][:k] - mu2) / sd2).cumsum().min() for _ in range(4000)])
print(f"ядро без ворот: умение 2010-24 {mu2*100:+.2f}%/мес, 2025-26 {c2.loc['2025-01-01':].mean()*100:+.2f}%/мес; CUSUM мин {cus2.min():+.2f}s; квантили 5% {np.percentile(mins2,5):+.2f}, 10% {np.percentile(mins2,10):+.2f}")
pd.DataFrame({"cusum_panel": cus, "cusum_core": cus2}).to_csv(f"{RES}/C_core_9d_cusum_skill.csv", float_format="%.4f")
alarms = 0; trials = 0
q5 = np.percentile(mins, 5)
cz = ((c - mu) / sd)
for start in range(0, len(cz) - k, 3):
    seg = cz.iloc[start:start + k].cumsum()
    trials += 1
    if seg.min() < q5:
        alarms += 1
print(f"плацебо по истории: доля окон длины {k} (шаг 3 мес, 2010-2026), где CUSUM < граница 5% ({q5:+.2f}s): {alarms}/{trials} = {alarms/trials:.2f}")
pd.DataFrame(ALL_ROWS).to_csv(f"{RES}/C_core_9ab_modifications.csv", index=False, float_format="%.4f")
