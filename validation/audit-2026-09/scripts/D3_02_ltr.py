"""D3_02 — долгосрочная реверсия: доходность 12/36/60 мес (MCFTR, IMOEX, номинал и реальная через
cpi_monthly), цена/MA500 и MA1000 -> следующие 1/3/6/12 мес. Инкремент к правилу панели."""
import numpy as np
import pandas as pd
from D3_lib import *

df = load_daily()
M = monthly_frame(df)
eps = episodes(df)
full = in_window(M.index, "full_2004-2026")

# CPI: месячные %, доступность — через месяц после отчётного (используем индекс на конец ПРЕДЫДУЩЕГО месяца)
cpi = load_raw(["cpi_monthly"])["cpi_monthly"]
lcpi = np.log(1 + cpi / 100.0).cumsum()
lcpi.index = lcpi.index.to_period("M")
M["lcpi"] = lcpi.reindex(M.index.to_period("M") - 1).values
ltr, lpx = np.log(M.mcftr_ffill), np.log(M.imoex)
for k in (12, 36, 60):
    M[f"tr{k}"] = ltr - ltr.shift(k)
    M[f"px{k}"] = lpx - lpx.shift(k)
    infl = M.lcpi - M.lcpi.shift(k)
    M[f"tr{k}_real"] = M[f"tr{k}"] - infl
    M[f"px{k}_real"] = M[f"px{k}"] - infl
for w in (500, 1000):
    df[f"pma{w}"] = np.log(df.imoex / df.imoex.rolling(w).mean())
    M[f"pma{w}"] = df[f"pma{w}"].reindex(M.index)
# реальный уровень индекса к своему 5-летнему среднему (value-якорь)
M["lpx_real"] = lpx - M.lcpi
M["real_vs_ma60"] = M.lpx_real - M.lpx_real.rolling(60, min_periods=36).mean()

signals = {"-tr36": -M.tr36, "-tr36_real": -M.tr36_real, "-px36": -M.px36, "-px36_real": -M.px36_real,
           "-tr12": -M.tr12, "-tr12_real": -M.tr12_real, "-px12": -M.px12, "-tr60": -M.tr60, "-tr60_real": -M.tr60_real,
           "-pma500": -M.pma500, "-pma1000": -M.pma1000, "-real_vs_ma60": -M.real_vs_ma60}
masks = std_masks(M)
ic = ic_table(M, signals, (1, 3, 6, 12), masks, nboot=1000)
ic["q_bh_family"] = fdr_bh(ic.p_nw)
save(ic, "ltr_ic")
print("IC долгосрочной реверсии (знак -> лонг), полная история 2004+ и split:")
show = ic[ic["sample"].isin(("full_2004-2026", "split_2004-2017", "split_2018-2026", "full_ex2022"))]
print(fmt(show.pivot_table(index=["signal", "h"], columns="sample", values=["ic", "p_boot", "n"]), 3))

# невырожденная годовая подвыборка для h=12: 12 смещений
rows = []
for s in ("-tr36", "-tr36_real", "-px36", "-pma1000", "-tr12"):
    x = signals[s]
    ics = []
    for off in range(12):
        sel = full & (np.arange(len(M)) % 12 == off)
        r = ic_stats(x[sel], M.fwd_tr_12[sel], h=1, min_n=10)
        ics.append(r["ic"])
    ics = np.array(ics)
    rows.append(dict(signal=s, ic_mean_12offsets=np.nanmean(ics), share_pos=np.nanmean(ics > 0), ic_min=np.nanmin(ics), ic_max=np.nanmax(ics)))
nonov = pd.DataFrame(rows)
print("\nh=12: невырожденные годовые подвыборки (12 смещений):")
print(fmt(nonov, 3))
save(nonov, "ltr_nonoverlap12")

# квинтили (расширяющийся перцентиль, только прошлое)
rows = []
for s in ("-tr36", "-tr36_real", "-px36_real", "-pma1000", "-pma500", "-tr12"):
    pct = expanding_pct(signals[s], 36)
    for wn in ("full_2004-2026", "split_2004-2017", "split_2018-2026", "full_ex2022"):
        wm = masks[wn]
        for cn, cond in (("дёшево: pct>0.8", pct > 0.8), ("дорого: pct<0.2", pct < 0.2), ("очень дёшево: pct>0.9", pct > 0.9)):
            for h in (3, 6, 12):
                r = cond_stats(cond[wm], M[f"fwd_ex_{h}"][wm], nboot=1000)
                rows.append(dict(signal=s, window=wn, cond=cn, h=h, mean_ex=100 * r["mean_cond"], diff_ex=100 * r["diff"],
                                 hit=r["hit_cond"], n_cond=r["n_cond"], p_boot=r["p_boot"]))
q = pd.DataFrame(rows)
save(q, "ltr_quantiles")
print("\nКвинтили (избыток над ставкой, %), полная история:")
print(fmt(q[q.window == "full_2004-2026"].pivot_table(index=["signal", "cond"], columns="h", values=["mean_ex", "n_cond", "p_boot"]), 2))
print("\nКвинтили split/ex2022, h=12:")
print(fmt(q[q.h == 12].pivot_table(index=["signal", "cond"], columns="window", values=["mean_ex", "p_boot"]), 2))

# стратегии
pos_panel_m = ((M.cell != TOXIC) & M.cell.notna() & (M.hyst == 1)).astype(float)
p36 = expanding_pct(-M.tr36_real, 36)
p36n = expanding_pct(-M.tr36, 36)
pma = expanding_pct(-M.pma1000, 36)
variants = {
    "панель": monthly_to_daily(pos_panel_m, df),
    "панель ИЛИ tr36_real дёшево(pct>0.9)": monthly_to_daily(((p36 > 0.9) | (pos_panel_m == 1)).astype(float), df),
    "панель ИЛИ tr36_real дёшево(pct>0.8)": monthly_to_daily(((p36 > 0.8) | (pos_panel_m == 1)).astype(float), df),
    "панель ИЛИ tr36 дёшево(pct>0.9)": monthly_to_daily(((p36n > 0.9) | (pos_panel_m == 1)).astype(float), df),
    "панель ИЛИ p/MA1000<-20%": monthly_to_daily(((M.pma1000 < np.log(0.8)) | (pos_panel_m == 1)).astype(float), df),
    "панель И НЕ tr36_real дорого(pct<0.1)": monthly_to_daily(((pos_panel_m == 1) & ~(p36 < 0.1)).astype(float), df),
    "панель И НЕ p/MA1000>+30%": monthly_to_daily(((pos_panel_m == 1) & ~(M.pma1000 > np.log(1.3))).astype(float), df),
    "панель И НЕ tr36 дорого(pct<0.1)": monthly_to_daily(((pos_panel_m == 1) & ~(p36n < 0.1)).astype(float), df),
}
R, T = evaluate_variants(df, M, variants, "ltr", eps)
print("\nСтратегии:")
show_strats(R)
show_timeliness(T, ["панель ИЛИ tr36_real дёшево(pct>0.9)", "панель И НЕ tr36_real дорого(pct<0.1)"])
