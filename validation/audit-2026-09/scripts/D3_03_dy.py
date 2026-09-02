"""D3_03 — дивдоходность: уровень, z, спред к депозиту/ставке ДР/10Y ОФЗ/ключу, изменение за 63 дн.
Проверка «dy — только пост-2022»: не потому ли, что до 2022 дивиденды были малы (нормировка на ставку)."""
import numpy as np
import pandas as pd
from D3_lib import *

df = load_daily()
df["dy_mm"] = df.dy_trail - df.mm_rate
df["ss63"] = df.switch_spread - df.switch_spread.shift(63)
df["dymm63"] = df.dy_mm - df.dy_mm.shift(63)
df["dy63"] = df.dy_trail - df.dy_trail.shift(63)
M = monthly_frame(df)
for c in ("dy_mm", "ss63", "dymm63", "dy63"):
    M[c] = df[c].reindex(M.index)
eps = episodes(df)
full = in_window(M.index, "full_2004-2026")

M["dy"] = M.dy_trail
M["dy_z60"] = rolling_z(M.dy, 60, 24)
M["dy_dep"] = M.switch_spread
M["dy_y10"] = M.dy - M.y10
M["dy_key"] = M.dy - M.key_rate
M["dy_ratio"] = M.dy / M.mm_rate
M["dy_mm_z60"] = rolling_z(M.dy_mm, 60, 24)
M["dy_ratio_z60"] = rolling_z(M.dy_ratio, 60, 24)

# уровни по эрам — «дивиденды были малы?»
lvl = []
for wn in ("split_2004-2017", "split_2018-2026", "pre2022_2004-2021", "post2022_03-2026", "era_2010-2021", "era_2022_03-2024", "era_2025-2026"):
    mm = in_window(M.index, wn)
    lvl.append(dict(window=wn, dy_mean=M.dy[mm].mean(), dy_med=M.dy[mm].median(), mm_rate_mean=M.mm_rate[mm].mean(),
                    dy_mm_mean=M.dy_mm[mm].mean(), dy_ratio_mean=M.dy_ratio[mm].mean(), dy_sd=M.dy[mm].std(), n=int(mm.sum())))
lvl = pd.DataFrame(lvl)
print("Уровни дивдоходности и ставки по эрам:")
print(fmt(lvl, 2))
save(lvl, "dy_levels")

signals = {"dy": M.dy, "dy_z60": M.dy_z60, "dy_dep(switch_spread)": M.dy_dep, "dy_mm": M.dy_mm, "dy_mm_z60": M.dy_mm_z60,
           "dy_ratio": M.dy_ratio, "dy_ratio_z60": M.dy_ratio_z60, "dy_y10": M.dy_y10, "dy_key": M.dy_key,
           "ss63(Δспреда 63д)": M.ss63, "dymm63": M.dymm63, "dy63": M.dy63}
masks = std_masks(M)
state_masks = {"rate=-1(смягчение)": M.st_rate == -1, "rate=+1": M.st_rate == 1, "bond=0": M.st_bond == 0, "bond=1": M.st_bond == 1,
               "toxic": M.cell == TOXIC, "trend=0": M.st_trend == 0, "trend=1": M.st_trend == 1,
               "rate=-1&pre2022": (M.st_rate == -1) & masks["pre2022_2004-2021"], "rate=-1&post2022": (M.st_rate == -1) & masks["post2022_03-2026"]}
state_masks = {k: v & full for k, v in state_masks.items()}
ic1 = ic_table(M, signals, (1, 3, 6), masks, nboot=1000)
ic2 = ic_table(M, signals, (1, 3, 6), state_masks, nboot=1000)
ic = pd.concat([ic1, ic2])
ic["q_bh_family"] = fdr_bh(ic.p_nw)
save(ic, "dy_ic")
print("\nIC dy-семейства, ключевой вопрос — до/после 2022 (h=1,3,6):")
key = ic1[ic1["sample"].isin(("pre2022_2004-2021", "post2022_03-2026", "full_2004-2026", "full_ex2022"))]
print(fmt(key.pivot_table(index=["signal", "h"], columns="sample", values=["ic", "p_boot", "n"]), 3))
print("\nIC split 2004–2017 / 2018–2026:")
print(fmt(ic1[ic1["sample"].isin(("split_2004-2017", "split_2018-2026"))].pivot_table(index=["signal", "h"], columns="sample", values=["ic", "p_boot"]), 3))
print("\nIC по состояниям (h=3):")
print(fmt(ic2[ic2.h == 3].pivot_table(index="signal", columns="sample", values="ic"), 3))
print(fmt(ic2[ic2.h == 3].pivot_table(index="signal", columns="sample", values="p_boot"), 3))

# пороги / условия
rows = []
pmm = expanding_pct(M.dy_mm, 36)
conds = {"dy_mm>0 (дивиденды > ставки)": M.dy_mm > 0, "dy_mm<-5": M.dy_mm < -5, "dy_mm<-8": M.dy_mm < -8,
         "dy_mm pct>0.8": pmm > 0.8, "dy_mm pct>0.9": pmm > 0.9, "dy_mm pct<0.2": pmm < 0.2, "dy_mm pct<0.1": pmm < 0.1,
         "dy_mm pct>0.8 & rate=-1": (pmm > 0.8) & (M.st_rate == -1), "dy_mm pct>0.8 & bond=0": (pmm > 0.8) & (M.st_bond == 0),
         "dy_z60>1": M.dy_z60 > 1, "dy_z60<-1": M.dy_z60 < -1, "dy_ratio>0.8": M.dy_ratio > 0.8, "dy_ratio<0.3": M.dy_ratio < 0.3}
for wn in ("full_2004-2026", "split_2004-2017", "split_2018-2026", "full_ex2022", "pre2022_2004-2021", "post2022_03-2026"):
    wm = masks[wn]
    for cn, cond in conds.items():
        for h in (1, 3, 6):
            r = cond_stats(cond.fillna(False)[wm], M[f"fwd_ex_{h}"][wm], nboot=1000)
            rows.append(dict(window=wn, cond=cn, h=h, mean_ex=100 * r["mean_cond"], diff_ex=100 * r["diff"], hit=r["hit_cond"],
                             n_cond=r["n_cond"], p_boot=r["p_boot"]))
ct = pd.DataFrame(rows)
save(ct, "dy_conditions")
print("\nУсловия (избыток над ставкой, %), полная история:")
print(fmt(ct[ct.window == "full_2004-2026"].pivot_table(index="cond", columns="h", values=["mean_ex", "n_cond", "p_boot"]), 2))
print("\nУсловия h=3 по окнам:")
print(fmt(ct[ct.h == 3].pivot_table(index="cond", columns="window", values=["mean_ex", "n_cond"]), 2))

# стратегии
pos_panel_m = ((M.cell != TOXIC) & M.cell.notna() & (M.hyst == 1)).astype(float)
gate_m = (M.cell != TOXIC) & M.cell.notna()
variants = {
    "панель": monthly_to_daily(pos_panel_m, df),
    "панель И НЕ dy_mm дорого(pct<0.1)": monthly_to_daily(((pos_panel_m == 1) & ~(pmm < 0.1)).astype(float), df),
    "панель И НЕ dy_mm<-8": monthly_to_daily(((pos_panel_m == 1) & ~(M.dy_mm < -8)).astype(float), df),
    "панель И НЕ dy_z60<-1": monthly_to_daily(((pos_panel_m == 1) & ~(M.dy_z60 < -1)).astype(float), df),
    "панель ИЛИ dy_mm>0": monthly_to_daily(((M.dy_mm > 0) | (pos_panel_m == 1)).astype(float), df),
    "панель ИЛИ dy_mm pct>0.9": monthly_to_daily(((pmm > 0.9) | (pos_panel_m == 1)).astype(float), df),
    "панель ИЛИ (dy_mm pct>0.8 & rate=-1)": monthly_to_daily((((pmm > 0.8) & (M.st_rate == -1)) | (pos_panel_m == 1)).astype(float), df),
    "ворота И (ядро>0 ИЛИ dy_mm pct>0.8)": monthly_to_daily((gate_m & ((M.hyst == 1) | (pmm > 0.8))).astype(float), df),
    "ворота И (ядро>0 ИЛИ dy_z60>1)": monthly_to_daily((gate_m & ((M.hyst == 1) | (M.dy_z60 > 1))).astype(float), df),
}
R, T = evaluate_variants(df, M, variants, "dy", eps)
print("\nСтратегии:")
show_strats(R)
