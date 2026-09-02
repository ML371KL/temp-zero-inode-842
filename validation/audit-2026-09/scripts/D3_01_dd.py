"""D3_01 — просадка dd252 как сигнал входа: IC по состояниям, пороги, скорость падения,
«второе дно», инкремент к правилу панели, своевременность."""
import numpy as np
import pandas as pd
from D3_lib import *

df = load_daily()
px = df["imoex"]
df["ret21"] = np.log(px / px.shift(21))
df["dd21"] = np.log(px / px.rolling(21).max())
M = monthly_frame(df)
for c in ("ret21", "dd21"):
    M[c] = df[c].reindex(M.index)
eps = episodes(df)
full = in_window(M.index, "full_2004-2026")

# ------------------------------------------------------------- 1. IC по срезам и состояниям
signals = {"-dd252": -M["dd252"], "-ret21": -M["ret21"], "-dd21": -M["dd21"]}
masks = std_masks(M)
state_masks = {
    "bond=0": M.st_bond == 0, "bond=1": M.st_bond == 1,
    "vol=0": M.st_vol == 0, "vol=1": M.st_vol == 1,
    "rate=-1(смягчение)": M.st_rate == -1, "rate=+1(ужесточение)": M.st_rate == 1,
    "toxic": M.cell == TOXIC, "not_toxic": (M.cell != TOXIC) & M.cell.notna(),
    "bond=0&2004-2017": (M.st_bond == 0) & masks["split_2004-2017"],
    "bond=0&2018-2026": (M.st_bond == 0) & masks["split_2018-2026"],
    "bond=0&ex2022": (M.st_bond == 0) & masks["full_ex2022"],
    "bond=1&ex2022": (M.st_bond == 1) & masks["full_ex2022"],
}
state_masks = {k: (v & full) for k, v in state_masks.items()}
ic1 = ic_table(M, signals, (1, 3, 6), masks, nboot=1000)
ic2 = ic_table(M, signals, (1, 3, 6), state_masks, nboot=1000)
ic = pd.concat([ic1, ic2])
ic["q_bh_family"] = fdr_bh(ic["p_nw"])
save(ic, "dd_ic")
print("IC (-dd252 -> fwd MCFTR), стандартные срезы:")
print(fmt(ic1[ic1.signal == "-dd252"], 3))
print("\nIC (-dd252) по состояниям:")
print(fmt(ic2[ic2.signal == "-dd252"], 3))

# ------------------------------------------------------------- 2. пороги просадки
rows = []
wins = {k: masks[k] for k in ("full_2004-2026", "split_2004-2017", "split_2018-2026", "full_ex2022")}
conds = {"all": pd.Series(True, index=M.index), "bond=0": M.st_bond == 0, "bond=1": M.st_bond == 1,
         "vol=1": M.st_vol == 1, "toxic": M.cell == TOXIC, "rate=-1": M.st_rate == -1}
for wn, wm in wins.items():
    for thr in (0.10, 0.15, 0.20, 0.25, 0.30):
        for cn, cm in conds.items():
            cond = (M.dd252 < np.log(1 - thr)) & cm.fillna(False)
            runs = int(((cond & wm).astype(int).diff() == 1).sum() + int(cond[wm].iloc[0]))
            for h in (1, 3, 6):
                r = cond_stats(cond[wm], M[f"fwd_ex_{h}"][wm], nboot=1000)
                rt = cond_stats(cond[wm], M[f"fwd_tr_{h}"][wm], nboot=0)
                rows.append(dict(window=wn, thr_pct=int(thr * 100), cond=cn, h=h, n_runs=runs,
                                 mean_ex=100 * r["mean_cond"], mean_rest_ex=100 * r["mean_rest"],
                                 diff_ex=100 * r["diff"], mean_tr=100 * rt["mean_cond"],
                                 hit=r["hit_cond"], n_cond=r["n_cond"], p_boot=r["p_boot"], t=r["t_simple"]))
thr_tab = pd.DataFrame(rows)
save(thr_tab, "dd_thresholds")
print("\nПороги просадки (полная история, избыток над ставкой, %):")
t = thr_tab[(thr_tab.window == "full_2004-2026") & (thr_tab.h.isin((1, 3)))]
print(fmt(t.pivot_table(index=["thr_pct", "cond"], columns="h", values=["mean_ex", "n_cond", "n_runs", "p_boot", "hit"]), 2))
print("\nПороги: split и ex-2022, h=3, cond=all / bond=0:")
print(fmt(thr_tab[(thr_tab.h == 3) & (thr_tab.cond.isin(("all", "bond=0", "bond=1")))]
          .pivot_table(index=["thr_pct", "cond"], columns="window", values=["mean_ex", "n_cond", "p_boot"]), 2))

# бины просадки — монотонность
bins = [-10, -0.7, -0.45, -0.3, -0.2, -0.15, -0.1, -0.05, 0.001]
lab = ["<-50%", "-50..-36", "-36..-26", "-26..-18", "-18..-14", "-14..-10", "-10..-5", "-5..0"]
b = pd.cut(M.dd252[full], bins, labels=lab)
bt_ = pd.DataFrame({"bin": b, "ex1": M.fwd_ex_1[full] * 100, "ex3": M.fwd_ex_3[full] * 100, "ex6": M.fwd_ex_6[full] * 100,
                    "bond": M.st_bond[full]})
bins_tab = bt_.groupby("bin", observed=True).agg(n=("ex1", "size"), ex1=("ex1", "mean"), ex3=("ex3", "mean"), ex6=("ex6", "mean"),
                                                 hit3=("ex3", lambda x: (x > 0).mean()))
bins_bond = bt_.groupby(["bin", "bond"], observed=True).agg(n=("ex1", "size"), ex3=("ex3", "mean"), hit3=("ex3", lambda x: (x > 0).mean()))
print("\nБины просадки (2004+):")
print(fmt(bins_tab, 2))
print("\nБины × bond:")
print(fmt(bins_bond.unstack("bond"), 2))
save(bins_tab.reset_index(), "dd_bins")

# ------------------------------------------------------------- 3. скорость падения
rows = []
speed = {
    "ret21<-10%": M.ret21 < np.log(0.90), "ret21<-15%": M.ret21 < np.log(0.85),
    "dd<-20% & ret21<-10% (нож)": (M.dd252 < np.log(0.8)) & (M.ret21 < np.log(0.9)),
    "dd<-20% & ret21>0 (стабилизация)": (M.dd252 < np.log(0.8)) & (M.ret21 > 0),
    "dd<-20% & ret21>+5%": (M.dd252 < np.log(0.8)) & (M.ret21 > np.log(1.05)),
    "dd<-15% & ret21>0": (M.dd252 < np.log(0.85)) & (M.ret21 > 0),
    "dd<-15% & ret21<-10%": (M.dd252 < np.log(0.85)) & (M.ret21 < np.log(0.9)),
    "dd<-15% & ret21>0 & bond=0": (M.dd252 < np.log(0.85)) & (M.ret21 > 0) & (M.st_bond == 0),
    "dd<-15% & ret21>0 & bond=1": (M.dd252 < np.log(0.85)) & (M.ret21 > 0) & (M.st_bond == 1),
}
for wn, wm in wins.items():
    for cn, cond in speed.items():
        for h in (1, 3, 6):
            r = cond_stats(cond[wm], M[f"fwd_ex_{h}"][wm], nboot=1000)
            rows.append(dict(window=wn, cond=cn, h=h, mean_ex=100 * r["mean_cond"], diff_ex=100 * r["diff"],
                             hit=r["hit_cond"], n_cond=r["n_cond"], p_boot=r["p_boot"]))
sp = pd.DataFrame(rows)
save(sp, "dd_speed")
print("\nСкорость падения (полная история):")
print(fmt(sp[sp.window == "full_2004-2026"].pivot_table(index="cond", columns="h", values=["mean_ex", "n_cond", "p_boot", "hit"]), 2))
print("\nСкорость: split/ex2022, h=3:")
print(fmt(sp[sp.h == 3].pivot_table(index="cond", columns="window", values=["mean_ex", "n_cond"]), 2))

# ------------------------------------------------------------- 4. «второе дно» (дневные события)
lpx = np.log(px)
ltr = np.log(df.mcftr_ffill)
n = len(df)
vals = px.values
dd = df.dd252.values
first_touch, retest = np.zeros(n, bool), np.zeros(n, bool)
min252 = px.rolling(252).min().values
for i in range(300, n):
    if not np.isfinite(dd[i]) or dd[i] > np.log(0.85):
        continue
    w = vals[i - 62:i + 1]
    j = int(np.argmin(w))
    i_low = i - 62 + j
    days_since = i - i_low
    if vals[i] <= min252[i] * 1.0001 and dd[i - 1] > np.log(0.85):
        first_touch[i] = True
    if days_since >= 15:
        bounce = vals[i_low:i + 1].max() / vals[i_low] - 1
        if bounce >= 0.05 and vals[i] <= vals[i_low] * 1.03:
            retest[i] = True


def dedup(mask, gap=42):
    idx = np.where(mask)[0]
    keep, last = [], -10 ** 9
    for k in idx:
        if k - last >= gap:
            keep.append(k)
            last = k
    return np.array(keep, int)


def ev_stats(idx_arr, name, sample_mask=None):
    rows = []
    for i in idx_arr:
        if sample_mask is not None and not sample_mask[i]:
            continue
        if i + 126 >= n:
            continue
        r63 = ltr.iloc[i + 63] - ltr.iloc[i]
        r126 = ltr.iloc[i + 126] - ltr.iloc[i]
        path = ltr.iloc[i:i + 127].values - ltr.iloc[i]
        mdd = (path - np.maximum.accumulate(path)).min()
        rf63 = df.rf.iloc[i + 1:i + 64].sum()
        rows.append(dict(event=name, date=df.index[i].date(), dd252=dd[i], r63=r63, r126=r126, ex63=r63 - rf63, mdd126=mdd,
                         bond=df.st_bond.iloc[i], cell=df.cell.iloc[i]))
    return pd.DataFrame(rows)


sm = np.asarray(df.index >= "2004-01-01")
ft = ev_stats(dedup(first_touch), "первое касание -15% (новый 252д минимум)", sm)
rt = ev_stats(dedup(retest), "повторный тест дна (отскок>=5%, >=15д, в 3% от минимума)", sm)
anyd = ev_stats(dedup((dd <= np.log(0.85)) & sm), "любой день dd<=-15% (дедуп 42д)", sm)
ev = pd.concat([ft, rt, anyd])
save(ev, "dd_second_bottom_events")
agg = ev.groupby("event").agg(n=("r63", "size"), r63=("r63", "mean"), r63_med=("r63", "median"), hit63=("r63", lambda x: (x > 0).mean()),
                              ex63=("ex63", "mean"), r126=("r126", "mean"), mdd126=("mdd126", "mean"))
print("\nСобытия «первое касание» vs «второе дно» (2004+, MCFTR, лог-доходности):")
print(fmt(agg, 3))
for name, g in ev.groupby("event"):
    g2 = g[g.date.map(lambda d: d.year != 2022)]
    print(f"  ex-2022: {name}: n={len(g2)} r63={g2.r63.mean():+.3f} hit={ (g2.r63>0).mean():.2f}  | bond=0: n={(g.bond==0).sum()} r63={g[g.bond==0].r63.mean():+.3f} | bond=1: n={(g.bond==1).sum()} r63={g[g.bond==1].r63.mean():+.3f}")
save(agg.reset_index(), "dd_second_bottom")

# ------------------------------------------------------------- 5. инкремент к правилу панели
pos_panel_m = ((M.cell != TOXIC) & M.cell.notna() & (M.hyst == 1)).astype(float)
gate_m = ((M.cell != TOXIC) & M.cell.notna())
variants = {"панель": monthly_to_daily(pos_panel_m, df)}
for thr in (0.15, 0.20, 0.25, 0.30):
    v = ((M.dd252 < np.log(1 - thr)) | (pos_panel_m == 1)).astype(float)
    variants[f"панель ИЛИ dd<-{int(thr * 100)}%"] = monthly_to_daily(v, df)
variants["панель ИЛИ (dd<-10% & bond=0)"] = monthly_to_daily(((M.dd252 < np.log(0.9)) & (M.st_bond == 0) | (pos_panel_m == 1)).astype(float), df)
variants["панель ИЛИ (dd<-15% & bond=0)"] = monthly_to_daily(((M.dd252 < np.log(0.85)) & (M.st_bond == 0) | (pos_panel_m == 1)).astype(float), df)
variants["панель ИЛИ (dd<-20% & ret21>0)"] = monthly_to_daily(((M.dd252 < np.log(0.8)) & (M.ret21 > 0) | (pos_panel_m == 1)).astype(float), df)
variants["панель ИЛИ (dd<-15% & ret21>0)"] = monthly_to_daily(((M.dd252 < np.log(0.85)) & (M.ret21 > 0) | (pos_panel_m == 1)).astype(float), df)
variants["ворота ИЛИ (dd<-15% & ret21>0)"] = monthly_to_daily(((M.dd252 < np.log(0.85)) & (M.ret21 > 0) | gate_m).astype(float), df)
# дневная версия лучшего кандидата «стабилизация»: ворота дневные, вход по dd&ret21 ежедневно
pos_pd = panel_positions(df, M, "daily_gate")
stab_d = ((df.dd252 < np.log(0.85)) & (df.ret21 > 0)).astype(float)
variants["панель(дн.ворота) ИЛИ (dd<-15% & ret21>0) дневн."] = ((pos_pd == 1) | (stab_d == 1)).astype(float)
R, T = evaluate_variants(df, M, variants, "dd", eps)
print("\nСтратегии (издержки 0,2%):")
show_strats(R)
print("\nСвоевременность (панель vs лучший override):")
show_timeliness(T, ["панель", "панель ИЛИ dd<-20%", "панель ИЛИ (dd<-15% & ret21>0)"])
