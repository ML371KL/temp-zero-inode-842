"""D3_06 — композит value (rank-среднее -dd252, dy−ставка, -rb_gap; перцентили только по прошлому)
как сигнал ВХОДА в токсичной ячейке: «покупать value» против «покупать подтверждение» (снятие ворот)
по доходности следующих 3/6 мес, просадке после входа и своевременности относительно дна."""
import numpy as np
import pandas as pd
from D3_lib import *

df = load_daily()
df["dy_mm"] = df.dy_trail - df.mm_rate
M = monthly_frame(df)
M["dy_mm"] = df.dy_mm.reindex(M.index)
eps = episodes(df)
full = in_window(M.index, "full_2004-2026")

comps_m = {"-dd252": -M.dd252, "dy_mm": M.dy_mm, "-rb_gap": -M.rb_gap}
comps_d = {"-dd252": -df.dd252, "dy_mm": df.dy_mm, "-rb_gap": -df.rb_gap}
pcts = pd.DataFrame({k: expanding_pct(v, 36) for k, v in comps_m.items()})
M["val"] = pcts.mean(axis=1).where(pcts.notna().sum(axis=1) >= 2)
M["val_pct"] = expanding_pct(M.val, 36)
M["val2"] = pcts[["-dd252", "dy_mm"]].mean(axis=1).where(pcts[["-dd252", "dy_mm"]].notna().sum(axis=1) == 2)


def daily_pct(daily, monthly_hist, min_n=36):
    """Перцентиль дневного значения среди МЕСЯЧНОЙ истории до начала текущего месяца."""
    out = pd.Series(np.nan, index=daily.index)
    mh = monthly_hist.dropna()
    for ym, g in daily.groupby(daily.index.to_period("M")):
        hist = np.sort(mh[mh.index < ym.start_time].values)
        if len(hist) < min_n:
            continue
        v = g.values
        ok = np.isfinite(v)
        p = (np.searchsorted(hist, v[ok], "left") + np.searchsorted(hist, v[ok], "right")) / 2.0 / len(hist)
        out.loc[g.index[ok]] = p
    return out


dp = pd.DataFrame({k: daily_pct(comps_d[k], comps_m[k]) for k in comps_d})
df["val_d"] = dp.mean(axis=1).where(dp.notna().sum(axis=1) >= 2)
df["val_d_pct"] = daily_pct(df.val_d, M.val)

# ------------------------------------------------------------- 1. IC композита
signals = {"val (среднее перцентилей)": M.val, "val_pct": M.val_pct, "val2 (dd+dy)": M.val2,
           "pct(-dd252)": pcts["-dd252"], "pct(dy_mm)": pcts["dy_mm"], "pct(-rb_gap)": pcts["-rb_gap"]}
masks = std_masks(M)
state_masks = {"toxic": M.cell == TOXIC, "not_toxic": (M.cell != TOXIC) & M.cell.notna(), "bond=0": M.st_bond == 0, "bond=1": M.st_bond == 1,
               "vol=1": M.st_vol == 1, "trend=0": M.st_trend == 0, "toxic&ex2022": (M.cell == TOXIC) & masks["full_ex2022"]}
state_masks = {k: v & full for k, v in state_masks.items()}
ic1 = ic_table(M, signals, (1, 3, 6), masks, nboot=1000)
ic2 = ic_table(M, signals, (1, 3, 6), state_masks, nboot=1000)
ic = pd.concat([ic1, ic2])
ic["q_bh_family"] = fdr_bh(ic.p_nw)
save(ic, "composite_ic")
print("IC композита value, стандартные срезы:")
print(fmt(ic1.pivot_table(index=["signal", "h"], columns="sample", values="ic"), 3))
print(fmt(ic1.pivot_table(index=["signal", "h"], columns="sample", values="p_boot"), 3))
print("\nIC по состояниям:")
print(fmt(ic2.pivot_table(index=["signal", "h"], columns="sample", values="ic"), 3))
print(fmt(ic2.pivot_table(index=["signal", "h"], columns="sample", values="p_boot"), 3))

# условия по порогам композита
rows = []
conds = {"val_pct>0.8": M.val_pct > 0.8, "val_pct>0.9": M.val_pct > 0.9, "val_pct<0.2": M.val_pct < 0.2, "val_pct<0.1": M.val_pct < 0.1,
         "val_pct>0.8 & toxic": (M.val_pct > 0.8) & (M.cell == TOXIC), "val_pct>0.9 & toxic": (M.val_pct > 0.9) & (M.cell == TOXIC),
         "val_pct<0.5 & toxic": (M.val_pct < 0.5) & (M.cell == TOXIC), "toxic": M.cell == TOXIC,
         "val_pct>0.8 & bond=0": (M.val_pct > 0.8) & (M.st_bond == 0), "val>0.7": M.val > 0.7, "val<0.3": M.val < 0.3}
for wn in ("full_2004-2026", "split_2004-2017", "split_2018-2026", "full_ex2022"):
    wm = masks[wn]
    for cn, cond in conds.items():
        for h in (1, 3, 6):
            r = cond_stats(cond.fillna(False)[wm], M[f"fwd_ex_{h}"][wm], nboot=1000)
            rows.append(dict(window=wn, cond=cn, h=h, mean_ex=100 * r["mean_cond"], diff_ex=100 * r["diff"], hit=r["hit_cond"],
                             n_cond=r["n_cond"], p_boot=r["p_boot"]))
ct = pd.DataFrame(rows)
save(ct, "composite_conditions")
print("\nУсловия по композиту (избыток над ставкой, %), полная история:")
print(fmt(ct[ct.window == "full_2004-2026"].pivot_table(index="cond", columns="h", values=["mean_ex", "n_cond", "p_boot", "hit"]), 2))
print("\nh=3 по окнам:")
print(fmt(ct[ct.h == 3].pivot_table(index="cond", columns="window", values=["mean_ex", "n_cond"]), 2))

# ------------------------------------------------------------- 2. токсичные эпизоды: value-вход vs подтверждение
tox = df.toxic.fillna(0).astype(int)
n = len(df)
runs = []
i = 0
while i < n:
    if tox.iloc[i] == 1:
        j = i
        while j < n and tox.iloc[j] == 1:
            j += 1
        runs.append([i, j - 1])
        i = j
    else:
        i += 1
merged = []
for r in runs:
    if merged and r[0] - merged[-1][1] <= 10:
        merged[-1][1] = r[1]
    else:
        merged.append(r)
merged = [r for r in merged if r[1] - r[0] + 1 >= 10 and df.index[r[0]] >= pd.Timestamp("2004-01-01")]
ltr = np.log(df.mcftr_ffill)
me_set = set(month_end_dates(df))
is_me = pd.Series([d in me_set for d in df.index], index=df.index)
hyst_d = M.hyst.reindex(df.index).ffill()


def entry_eval(i_entry, i_trough, name, ep_id, start_date, end_date):
    if i_entry is None or i_entry + 126 >= n:
        return dict(episode=ep_id, start=start_date, end=end_date, entry=name, entry_date=None)
    path = ltr.iloc[i_entry:i_entry + 127].values - ltr.iloc[i_entry]
    rf63 = df.rf.iloc[i_entry + 1:i_entry + 64].sum()
    rf126 = df.rf.iloc[i_entry + 1:i_entry + 127].sum()
    return dict(episode=ep_id, start=start_date, end=end_date, entry=name, entry_date=df.index[i_entry].date(),
                lag_vs_trough=i_entry - i_trough, ex63=100 * (path[63] - rf63), ex126=100 * (path[126] - rf126),
                r63=100 * path[63], r126=100 * path[126], mdd126=100 * (path - np.maximum.accumulate(path)).min(),
                val_pct_at_entry=df.val_d_pct.iloc[i_entry], dd252_at_entry=100 * df.dd252.iloc[i_entry])


rows = []
for k, (a, b) in enumerate(merged):
    horizon_end = min(b + 126, n - 1)
    i_tr = a + int(np.argmin(ltr.iloc[a:horizon_end + 1].values))
    seg = df.iloc[a:horizon_end + 1]

    def first(mask):
        idx = np.where(mask.values)[0]
        return a + int(idx[0]) if len(idx) else None

    entries = {
        "A: value pct>=0.8 (дневн.)": first(seg.val_d_pct >= 0.8),
        "A: value pct>=0.9 (дневн.)": first(seg.val_d_pct >= 0.9),
        "A: dd252<-20% (дневн.)": first(seg.dd252 < np.log(0.8)),
        "A: dd252<-25% (дневн.)": first(seg.dd252 < np.log(0.75)),
        "B: снятие ворот (дневн., 1 день)": first((seg.toxic == 0) & (np.arange(len(seg)) > 0)),
        "B: снятие ворот на срезе месяца": first((seg.toxic == 0) & is_me.iloc[a:horizon_end + 1]),
        "B: правило панели (ворота+ядро, срез месяца)": first((seg.toxic == 0) & is_me.iloc[a:horizon_end + 1] & (hyst_d.iloc[a:horizon_end + 1] == 1)),
        "C: старт токсичного эпизода (контроль)": a,
    }
    for name, ie in entries.items():
        rows.append(entry_eval(ie, i_tr, name, k, df.index[a].date(), df.index[b].date()))
E = pd.DataFrame(rows)
save(E, "composite_toxic_entries")
print(f"\nТоксичных эпизодов (>=10 дн, слияние разрывов <=10 дн) с 2004: {len(merged)}")
print(pd.DataFrame([dict(start=df.index[a].date(), end=df.index[b].date(), days=b - a + 1) for a, b in merged]).to_string())
agg = E.dropna(subset=["entry_date"]).groupby("entry").agg(n=("ex63", "size"), lag_vs_trough_mean=("lag_vs_trough", "mean"), lag_med=("lag_vs_trough", "median"),
                                                         before_trough=("lag_vs_trough", lambda x: (x < 0).mean()),
                                                         ex63=("ex63", "mean"), ex63_med=("ex63", "median"), hit63=("ex63", lambda x: (x > 0).mean()),
                                                         ex126=("ex126", "mean"), mdd126=("mdd126", "mean"), mdd126_worst=("mdd126", "min"))
print("\nВход в токсичной ячейке: value-порог vs подтверждение (лаг в торговых днях от дна MCFTR; ex = избыток над ставкой, %):")
print(fmt(agg, 1))
save(agg.reset_index(), "composite_toxic_summary")
print("\nПо эпизодам (ex63 / лаг):")
print(fmt(E.pivot_table(index="episode", columns="entry", values="ex63"), 1))
print(fmt(E.pivot_table(index="episode", columns="entry", values="lag_vs_trough"), 0))

# то же для 10 больших эпизодов просадок (>15%) — вход относительно дна
rows = []
for k, e in eps.iterrows():
    ipk, itr = df.index.get_loc(e.peak), df.index.get_loc(e.trough)
    horizon_end = min(itr + 252, n - 1)
    seg = df.iloc[ipk:horizon_end + 1]

    def first(mask):
        idx = np.where(mask.values)[0]
        return ipk + int(idx[0]) if len(idx) else None

    entries = {
        "A: value pct>=0.8 (дневн.)": first(seg.val_d_pct >= 0.8), "A: value pct>=0.9 (дневн.)": first(seg.val_d_pct >= 0.9),
        "A: dd252<-20% (дневн.)": first(seg.dd252 < np.log(0.8)),
        "A: dd252<-15% & ret21>0 (дневн.)": first((seg.dd252 < np.log(0.85)) & (np.log(seg.imoex / seg.imoex.shift(21)) > 0)),
        "B: правило панели (срез месяца)": first((seg.toxic == 0) & is_me.iloc[ipk:horizon_end + 1] & (hyst_d.iloc[ipk:horizon_end + 1] == 1) & (np.arange(len(seg)) > 0)),
        "B: панель с дневными воротами": first((seg.toxic == 0) & (hyst_d.iloc[ipk:horizon_end + 1] == 1) & (np.arange(len(seg)) > 0)),
    }
    # для B ищем первый вход ПОСЛЕ первого выхода (или после пика, если на пике флэт)
    for name, ie in entries.items():
        rows.append(entry_eval(ie, itr, name, f"{e.peak.date()}→{e.trough.date()}", e.peak.date(), e.trough.date()))
E2 = pd.DataFrame(rows)
save(E2, "composite_bigdd_entries")
agg2 = E2.dropna(subset=["entry_date"]).groupby("entry").agg(n=("ex63", "size"), lag_mean=("lag_vs_trough", "mean"), lag_med=("lag_vs_trough", "median"),
                                                           before_trough=("lag_vs_trough", lambda x: (x < 0).mean()), ex63=("ex63", "mean"),
                                                           hit63=("ex63", lambda x: (x > 0).mean()), ex126=("ex126", "mean"), mdd126=("mdd126", "mean"))
print("\nБольшие просадки (>15%): первый вход после пика по разным правилам (лаг от дна, дни):")
print(fmt(agg2, 1))
print(fmt(E2.pivot_table(index="episode", columns="entry", values="lag_vs_trough"), 0))

# ------------------------------------------------------------- 3. стратегии
pos_panel_m = ((M.cell != TOXIC) & M.cell.notna() & (M.hyst == 1)).astype(float)
gate_m = (M.cell != TOXIC) & M.cell.notna()
pos_pd = panel_positions(df, M, "daily_gate")
variants = {
    "панель": monthly_to_daily(pos_panel_m, df),
    "панель (дн. ворота)": pos_pd,
    "панель ИЛИ val_pct>0.8": monthly_to_daily(((M.val_pct > 0.8) | (pos_panel_m == 1)).astype(float), df),
    "панель ИЛИ val_pct>0.9": monthly_to_daily(((M.val_pct > 0.9) | (pos_panel_m == 1)).astype(float), df),
    "панель ИЛИ (toxic & val_pct>0.8)": monthly_to_daily((((M.val_pct > 0.8) & (M.cell == TOXIC)) | (pos_panel_m == 1)).astype(float), df),
    "панель И НЕ val_pct<0.1": monthly_to_daily(((pos_panel_m == 1) & ~(M.val_pct < 0.1)).astype(float), df),
    "панель И НЕ val_pct<0.2": monthly_to_daily(((pos_panel_m == 1) & ~(M.val_pct < 0.2)).astype(float), df),
    "панель ИЛИ val>0.8, И НЕ val<0.1": monthly_to_daily((((M.val_pct > 0.8) | (pos_panel_m == 1)) & ~(M.val_pct < 0.1)).astype(float), df),
    "ворота И (ядро>0 ИЛИ val_pct>0.8)": monthly_to_daily((gate_m & ((M.hyst == 1) | (M.val_pct > 0.8))).astype(float), df),
    "панель(дн.ворота) ИЛИ val_d_pct>0.9 дневн.": ((pos_pd == 1) | (df.val_d_pct > 0.9)).astype(float),
    "панель(дн.ворота) ИЛИ val_d_pct>0.8 дневн.": ((pos_pd == 1) | (df.val_d_pct > 0.8)).astype(float),
}
R, T = evaluate_variants(df, M, variants, "composite", eps)
print("\nСтратегии:")
show_strats(R)
show_timeliness(T, ["панель", "панель (дн. ворота)", "панель ИЛИ val_pct>0.8", "панель(дн.ворота) ИЛИ val_d_pct>0.8 дневн."])
save(M[["imoex", "cell", "hyst", "dd252", "dy_mm", "rb_gap", "val", "val_pct", "fwd_ex_1", "fwd_ex_3", "fwd_ex_6"]].reset_index(), "composite_monthly_series")
