"""D3_07 — устойчивость и плата за перебор: (a) tr36 vs px36 на одной выборке; (b) сетка tr36
(окно × порог) и walk-forward выбора параметров; (c) окна z-скора dy; (d) месяцы dy_z<-1;
(e) нога ядра: нефть в $ против рублёвой бочки, ядро с заменённой/добавленной ногой;
(f) комбинированные правила; (g) текущие показания всех кандидатов."""
import numpy as np
import pandas as pd
from D3_lib import *

df = load_daily()
px = df["imoex"]
df["ret21"] = np.log(px / px.shift(21))
df["dy_mm"] = df.dy_trail - df.mm_rate
M = monthly_frame(df)
for c in ("ret21", "dy_mm"):
    M[c] = df[c].reindex(M.index)
eps = episodes(df)
full = in_window(M.index, "full_2004-2026")
ltr, lpx = np.log(M.mcftr_ffill), np.log(M.imoex)
M["tr36"], M["px36"] = ltr - ltr.shift(36), lpx - lpx.shift(36)
M["div36"] = M.tr36 - M.px36
pos_panel_m = ((M.cell != TOXIC) & M.cell.notna() & (M.hyst == 1)).astype(float)
gate_m = (M.cell != TOXIC) & M.cell.notna()

# ---------------------------------------------------------------- (a) tr36 vs px36, одна выборка
print("=== (a) tr36 vs px36 на одной выборке (2006-02+) ===")
sel = M.tr36.notna() & full
rows = []
for nm, s in (("-tr36", -M.tr36), ("-px36", -M.px36), ("-div36 (3-летние дивиденды)", -M.div36), ("+div36", M.div36)):
    for h in (3, 6, 12):
        r = ic_stats(s[sel], M[f"fwd_tr_{h}"][sel], h=h, nboot=1000)
        rows.append(dict(signal=nm, h=h, **r))
a = pd.DataFrame(rows)
print(fmt(a, 3))
save(a, "robust_tr36_vs_px36")

# ---------------------------------------------------------------- (b) сетка tr36 и walk-forward
print("\n=== (b) сетка: панель ИЛИ trL дёшево(pct>q) ===")
grid = {}
for L in (24, 36, 48, 60):
    trL = ltr - ltr.shift(L)
    for q in (0.80, 0.85, 0.90, 0.95):
        p = expanding_pct(-trL, 36)
        grid[(L, q)] = monthly_to_daily(((p > q) | (pos_panel_m == 1)).astype(float), df)
rows = []
base = {w: metrics(backtest(df, monthly_to_daily(pos_panel_m, df), w)) for w in ("main_2010-2026", "full_2004-2026", "split_2004-2017", "split_2018-2026")}
for (L, q), pos in grid.items():
    row = dict(L=L, q=q)
    for w in base:
        m = metrics(backtest(df, pos, w))
        row[f"dSh_{w}"] = m["sharpe"] - base[w]["sharpe"]
        row[f"dCAGR_{w}"] = m["cagr_pct"] - base[w]["cagr_pct"]
    bt = backtest(df, pos, "full_2004-2026")
    btp = backtest(df, monthly_to_daily(pos_panel_m, df), "full_2004-2026")
    row["dSh_ex2022"] = metrics(bt[bt.index.year != 2022])["sharpe"] - metrics(btp[btp.index.year != 2022])["sharpe"]
    rows.append(row)
g = pd.DataFrame(rows)
print(fmt(g, 3))
print("Доля ячеек сетки с ΔШарп>0: main %.2f, full %.2f, 2004-17 %.2f, 2018-26 %.2f, ex2022 %.2f" % tuple(
    (g[c] > 0).mean() for c in ("dSh_main_2010-2026", "dSh_full_2004-2026", "dSh_split_2004-2017", "dSh_split_2018-2026", "dSh_ex2022")))
save(g, "robust_tr36_grid")

# walk-forward: каждый год с 2010 выбираем (L,q) по Шарпу варианта на всех месяцах ДО этого года
mo = {k: monthly_returns(backtest(df, pos, "full_2004-2026")) for k, pos in grid.items()}
mo_panel = monthly_returns(backtest(df, monthly_to_daily(pos_panel_m, df), "full_2004-2026"))
oos, chosen = [], []
for Y in range(2010, 2027):
    best, best_sh = None, -9
    for k, r in mo.items():
        past = r[r.index.year < Y]
        past = past[past.index.year >= 2004]
        sh = past.mean() / past.std() * np.sqrt(12) if past.std() > 0 else -9
        if sh > best_sh:
            best, best_sh = k, sh
    chosen.append((Y, best, round(best_sh, 2)))
    oos.append(mo[best][mo[best].index.year == Y])
oos = pd.concat(oos)
pan = mo_panel[mo_panel.index >= "2010-01"]


def sh(x):
    return x.mean() / x.std() * np.sqrt(12)


print("walk-forward выбор (год, (L,q), Шарп in-sample):", chosen)
print("OOS 2010–2026: Шарп WF-варианта %.3f против панели %.3f; CAGR %.2f%% против %.2f%%" % (
    sh(oos), sh(pan), 100 * (np.exp(oos.mean() * 12) - 1), 100 * (np.exp(pan.mean() * 12) - 1)))
sb = sharpe_diff_boot(oos, pan, nboot=2000)
print("бутстреп ΔШарп OOS:", {k: round(float(v), 3) for k, v in sb.items()})
wf = pd.DataFrame(chosen, columns=["year", "params", "sharpe_is"])
wf["oos_sharpe_total"] = sh(oos)
wf["panel_sharpe"] = sh(pan)
save(wf, "robust_tr36_walkforward")

# ---------------------------------------------------------------- (c) окна z-скора dy
print("\n=== (c) окна z-скора дивдоходности ===")
rows = []
masks = std_masks(M)
for w in (36, 48, 60, 84, 120):
    z = rolling_z(M.dy_trail, w, max(24, w // 2))
    for wn in ("full_2004-2026", "split_2004-2017", "split_2018-2026", "full_ex2022"):
        for h in (1, 3, 6):
            r = ic_stats(z[masks[wn]], M[f"fwd_tr_{h}"][masks[wn]], h=h, nboot=500)
            rows.append(dict(window_m=w, sample=wn, h=h, ic=r["ic"], p_boot=r["p_boot"], n=r["n"]))
    # стратегии
    v1 = monthly_to_daily((gate_m & ((M.hyst == 1) | (z > 1))).astype(float), df)
    v2 = monthly_to_daily(((pos_panel_m == 1) & ~(z < -1)).astype(float), df)
    for nm, pos in (("ворота И (ядро>0 ИЛИ z>1)", v1), ("панель И НЕ z<-1", v2)):
        for wn in ("main_2010-2026", "full_2004-2026", "split_2004-2017", "split_2018-2026"):
            rows.append(dict(window_m=w, sample=wn, h=0, ic=np.nan, p_boot=np.nan, n=0, strat=nm,
                             d_sharpe=metrics(backtest(df, pos, wn))["sharpe"] - base[wn]["sharpe"]))
c = pd.DataFrame(rows)
print(fmt(c[c.h > 0].pivot_table(index=["window_m", "h"], columns="sample", values=["ic", "p_boot"]), 3))
print(fmt(c[c.h == 0].pivot_table(index=["strat", "window_m"], columns="sample", values="d_sharpe"), 3))
save(c, "robust_dy_z_windows")

# ---------------------------------------------------------------- (d) месяцы dy_z60 < -1
z60 = rolling_z(M.dy_trail, 60, 24)
d = M[(z60 < -1) & full][["imoex", "dy_trail", "cell", "hyst", "fwd_ex_1", "fwd_ex_3"]].copy()
d["dy_z60"] = z60[(z60 < -1) & full]
print("\n=== (d) месяцы dy_z60 < -1 ===")
print(fmt(d, 3))
save(d.reset_index(), "robust_dy_z_below_m1")

# ---------------------------------------------------------------- (e) нога ядра: нефть в $ vs рублёвая бочка
print("\n=== (e) нефть в $ против рублёвой бочки; ядро с заменённой ногой ===")
ur = load_raw(["urals_tax"])["urals_tax"]
ur.index = ur.index.to_period("M")
ur_l = np.log(ur)
gap_usd = ur_l - ur_l.rolling(24, min_periods=24).mean()          # гэп Urals $ к 24-мес среднему (месяц t)
Mp = M.index.to_period("M")
M["urals_usd_gap"] = gap_usd.reindex(Mp - 1).values                # доступно с лагом: на срезе t известен месяц t-1
M["urals_usd_gap_nolag"] = gap_usd.reindex(Mp).values
rows = []
common = M.urals_rub_gap.notna() & M.urals_usd_gap.notna() & full
sub = {"2016-2026": common, "2016-2021": common & (M.index <= "2021-12-31"), "2022_03-2026": common & (M.index >= "2022-03-01"),
       "ex2022": common & ex2022(M.index), "vol=1": common & (M.st_vol == 1), "trend=0": common & (M.st_trend == 0)}
for nm, s in (("-urals_rub_gap (ядро)", -M.urals_rub_gap), ("-urals_usd_gap (лаг 1 мес)", -M.urals_usd_gap),
              ("-urals_usd_gap (без лага, справочно)", -M.urals_usd_gap_nolag), ("-brent_gap", -M.brent_gap if "brent_gap" in M else -(np.log(df.brent / df.brent.rolling(504).mean())).reindex(M.index)),
              ("+usd_mom63", M.usd_mom63), ("-usd_gap504", -(np.log(df.usd / df.usd.rolling(504).mean())).reindex(M.index))):
    for sn, mask in sub.items():
        for h in (1, 3, 6):
            r = ic_stats(s[mask], M[f"fwd_tr_{h}"][mask], h=h, nboot=1000)
            rows.append(dict(signal=nm, sample=sn, h=h, ic=r["ic"], p_boot=r["p_boot"], n=r["n"]))
e = pd.DataFrame(rows)
print(fmt(e.pivot_table(index=["signal", "h"], columns="sample", values="ic"), 3))
print(fmt(e.pivot_table(index=["signal", "h"], columns="sample", values="p_boot"), 3))
save(e, "robust_oil_leg_ic")

# корреляция ноги с usd_mom63 — «гасит ли бочка девальвационную ногу»
cc = M.loc[common, ["urals_rub_gap", "urals_usd_gap", "usd_mom63"]].corr(method="spearman")
print("Спирмен между ногами (2016+):")
print(fmt(cc, 3))

# реплика ядра и варианты
def core(legs):
    zs = pd.DataFrame({k: rolling_z(v, 60, 24) for k, v in legs.items()})
    n_used = zs.notna().sum(axis=1)
    return zs.mean(axis=1).where(n_used >= 1), n_used


legs_base = {"usd": M.usd_mom63, "slope": M.slope_10_2, "oil": -M.urals_rub_gap}
comp_rep, n_rep = core(legs_base)
chk = pd.concat([comp_rep, M.composite], axis=1).dropna()
print("реплика ядра vs panel_prod_monthly: corr=%.4f, max|Δ|=%.4f (n=%d)" % (chk.corr().iloc[0, 1], (chk.iloc[:, 0] - chk.iloc[:, 1]).abs().max(), len(chk)))
variants_core = {
    "ядро как в проде (реплика)": comp_rep,
    "ядро: бочка$ вместо бочки₽": core({"usd": M.usd_mom63, "slope": M.slope_10_2, "oil": -M.urals_usd_gap})[0],
    "ядро + 4-я нога dy_z60": core({**legs_base, "dy": M.dy_trail})[0],
    "ядро + 4-я нога -tr36": core({**legs_base, "tr36": -M.tr36})[0],
    "ядро: бочка$ + dy": core({"usd": M.usd_mom63, "slope": M.slope_10_2, "oil": -M.urals_usd_gap, "dy": M.dy_trail})[0],
    "ядро: бочка$ + dy + -tr36": core({"usd": M.usd_mom63, "slope": M.slope_10_2, "oil": -M.urals_usd_gap, "dy": M.dy_trail, "tr36": -M.tr36})[0],
}
rows = []
strat_core = {}
for nm, comp in variants_core.items():
    hy = hysteresis_sign(comp, 0.10)
    pos_m = (gate_m & (hy == 1)).astype(float)
    strat_core[nm] = monthly_to_daily(pos_m, df)
    for wn in ("main_2010-2026", "full_2004-2026", "split_2004-2017", "split_2018-2026", "era_2010-2021", "era_2022_03-2024", "era_2025-2026"):
        mk = masks[wn] if wn in masks else in_window(M.index, wn)
        r1 = ic_stats(comp[mk], M.fwd_tr_1[mk], h=1, nboot=500)
        rows.append(dict(core=nm, sample=wn, ic_fwd1=r1["ic"], p_boot=r1["p_boot"], n=r1["n"],
                         corr_with_prod=pd.concat([comp, M.composite], axis=1)[mk].corr().iloc[0, 1]))
cr = pd.DataFrame(rows)
print(fmt(cr.pivot_table(index="core", columns="sample", values=["ic_fwd1", "p_boot"]), 3))
save(cr, "robust_core_variants_ic")
R_core, T_core = evaluate_variants(df, M, {"панель": monthly_to_daily(pos_panel_m, df), **strat_core}, "core_variants", eps)
show_strats(R_core)

# ---------------------------------------------------------------- (f) комбинированные правила
print("\n=== (f) комбинированные правила ===")
p_tr36 = expanding_pct(-M.tr36, 36)
p_val = pd.read_csv(RES / "D3_composite_monthly_series.csv", parse_dates=["date"]).set_index("date")["val_pct"].reindex(M.index)
stab = (M.dd252 < np.log(0.85)) & (M.ret21 > 0)
rich_oil = M.rb_gap > 0.3
rich_val = p_val < 0.2
entry_tr = p_tr36 > 0.9
entry_dyz = z60 > 1
pos_pd = panel_positions(df, M, "daily_gate")
variants = {
    "панель": monthly_to_daily(pos_panel_m, df),
    "C1: панель ИЛИ tr36 pct>0.9": monthly_to_daily((entry_tr | (pos_panel_m == 1)).astype(float), df),
    "C2: панель И НЕ rb_gap>+0.3": monthly_to_daily(((pos_panel_m == 1) & ~rich_oil).astype(float), df),
    "C3: панель И НЕ val_pct<0.2": monthly_to_daily(((pos_panel_m == 1) & ~rich_val).astype(float), df),
    "C4: C1 И НЕ rb_gap>+0.3": monthly_to_daily(((entry_tr | (pos_panel_m == 1)) & ~rich_oil).astype(float), df),
    "C5: C1 И НЕ val_pct<0.2": monthly_to_daily(((entry_tr | (pos_panel_m == 1)) & ~rich_val).astype(float), df),
    "C6: C4 ИЛИ стабилизация(dd<-15%&ret21>0)": monthly_to_daily(((entry_tr | stab | (pos_panel_m == 1)) & ~rich_oil).astype(float), df),
    "C7: ворота И (ядро>0 ИЛИ dy_z60>1 ИЛИ tr36 pct>0.9)": monthly_to_daily((gate_m & ((M.hyst == 1) | entry_dyz | entry_tr)).astype(float), df),
    "C8: C7 И НЕ rb_gap>+0.3": monthly_to_daily((gate_m & ((M.hyst == 1) | entry_dyz | entry_tr) & ~rich_oil).astype(float), df),
    "C9: панель(дн.ворота) + C1 + C2": ((((pos_pd == 1) | (monthly_to_daily(entry_tr.astype(float), df) == 1)) & ~(monthly_to_daily(rich_oil.astype(float), df) == 1))).astype(float),
}
R, T = evaluate_variants(df, M, variants, "combo", eps)
show_strats(R, windows=("main_2010-2026", "full_2004-2026", "full_ex2022", "split_2004-2017", "split_2018-2026", "era_2010-2021", "era_2022_03-2024", "era_2025-2026"))
print("\nСвоевременность комбинированных правил:")
show_timeliness(T, ["панель", "C4: C1 И НЕ rb_gap>+0.3", "C6: C4 ИЛИ стабилизация(dd<-15%&ret21>0)", "C9: панель(дн.ворота) + C1 + C2"])
print("\nЧувствительность к издержкам (main): ")
print(fmt(R[(R.window == "main_2010-2026")][["name", "cost", "sharpe", "cagr_pct", "d_sharpe_vs_panel"]], 2))

# ---------------------------------------------------------------- (g) текущие показания
print("\n=== (g) текущие показания (последние 3 среза + 01.09.2026) ===")
cur = pd.DataFrame({
    "imoex": M.imoex, "cell": M.cell, "hyst": M.hyst, "composite": M.composite,
    "dd252_%": 100 * (np.exp(M.dd252) - 1), "ret21_%": 100 * (np.exp(M.ret21) - 1),
    "tr36_%": 100 * (np.exp(M.tr36) - 1), "tr36_pct(cheap)": p_tr36,
    "dy": M.dy_trail, "dy_z60": z60, "dy_mm": M.dy_mm,
    "rb_gap": M.rb_gap, "rb_gap_pct": expanding_pct(M.rb_gap, 36), "urals_rub_gap": M.urals_rub_gap, "urals_usd_gap(лаг)": M.urals_usd_gap,
    "val_pct": p_val, "stab": stab, "entry_tr36": entry_tr, "exit_rich_oil": rich_oil, "exit_rich_val": rich_val,
})
print(fmt(cur.tail(6).T, 3))
save(cur.tail(14).reset_index(), "robust_current_readings")
last = df.iloc[-1]
print("\nДневное 01.09.2026: dd252=%.1f%%, ret21=%.1f%%, dy=%.2f, rb_gap=%.3f, cell=%s" % (
    100 * (np.exp(last.dd252) - 1), 100 * (np.exp(last.ret21) - 1), df.dy_trail.dropna().iloc[-1], last.rb_gap, last.cell))
