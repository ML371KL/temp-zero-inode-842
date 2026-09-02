"""D3_04 — нефть/бочка: urals_rub_gap (ядро), дневной rb_gap (Brent×курс к MA504, с 1999), разложение
нефть/курс, бочка к бюджетному ориентиру (приближённые якоря 2011+, иначе 24-мес среднее).
Знак «контрариан» — подтверждение на split 2004–2017 / 2018–2026 и ex-2022."""
import numpy as np
import pandas as pd
from D3_lib import *

df = load_daily()
df["rbrub"] = df.brent * df.usd
df["brent_gap"] = np.log(df.brent / df.brent.rolling(504).mean())
df["usd_gap"] = np.log(df.usd / df.usd.rolling(504).mean())
df["rb_gap63"] = df.rb_gap - df.rb_gap.shift(63)
df["rb_gap252"] = np.log(df.rbrub / df.rbrub.rolling(252).mean())
df["rb_gap1008"] = np.log(df.rbrub / df.rbrub.rolling(1008, min_periods=504).mean())
# Бюджетные ориентиры рублёвой бочки (цена Urals в законе о бюджете × курс в законе; с 2017 — цена отсечения
# бюджетного правила × курс). ПРИБЛИЖЁННЫЕ значения по памяти, без сверки с первоисточником — см. отчёт.
BUDGET_RUB = {2011: 2288, 2012: 2870, 2013: 3143, 2014: 3373, 2015: 3075, 2016: 3165, 2017: 2700, 2018: 2640,
              2019: 2658, 2020: 2786, 2021: 3135, 2022: 3187, 2023: 4788, 2024: 5406, 2025: 5790, 2026: 5440}
df["rb_budget"] = np.log(df.rbrub / pd.Series(df.index.year, index=df.index).map(BUDGET_RUB))
M = monthly_frame(df)
for c in ("rbrub", "brent_gap", "usd_gap", "rb_gap63", "rb_gap252", "rb_gap1008", "rb_budget"):
    M[c] = df[c].reindex(M.index)
eps = episodes(df)
full = in_window(M.index, "full_2004-2026")

signals = {"-rb_gap(MA504)": -M.rb_gap, "-urals_rub_gap(ядро)": -M.urals_rub_gap, "-brent_gap": -M.brent_gap, "-usd_gap": -M.usd_gap,
           "-rb_gap252": -M.rb_gap252, "-rb_gap1008": -M.rb_gap1008, "-rb_gap63(Δ)": -M.rb_gap63, "-rb_budget": -M.rb_budget,
           "-brent_mom63": -M.brent_mom63}
masks = std_masks(M)
masks["budget_2011-2026"] = in_window(M.index, ("2011-01-01", "2026-08-31"))
state_masks = {"vol=1": M.st_vol == 1, "vol=0": M.st_vol == 0, "trend=0": M.st_trend == 0, "trend=1": M.st_trend == 1,
               "bond=1": M.st_bond == 1, "bond=0": M.st_bond == 0, "toxic": M.cell == TOXIC,
               "vol=1&ex2022": (M.st_vol == 1) & masks["full_ex2022"], "trend=0&ex2022": (M.st_trend == 0) & masks["full_ex2022"]}
state_masks = {k: v & full for k, v in state_masks.items()}
ic1 = ic_table(M, signals, (1, 3, 6), masks, nboot=1000)
ic2 = ic_table(M, signals, (1, 3, 6), state_masks, nboot=1000)
ic = pd.concat([ic1, ic2])
ic["q_bh_family"] = fdr_bh(ic.p_nw)
save(ic, "oil_ic")
print("IC нефтяного блока (знак контрариан подан как +), стандартные срезы:")
print(fmt(ic1.pivot_table(index=["signal", "h"], columns="sample", values="ic"), 3))
print("\np_boot:")
print(fmt(ic1.pivot_table(index=["signal", "h"], columns="sample", values="p_boot"), 3))
print("\nn:")
print(fmt(ic1[ic1.h == 1].pivot_table(index="signal", columns="sample", values="n"), 0))
print("\nIC по состояниям (h=3):")
print(fmt(ic2[ic2.h == 3].pivot_table(index="signal", columns="sample", values="ic"), 3))
print(fmt(ic2[ic2.h == 3].pivot_table(index="signal", columns="sample", values="p_boot"), 3))

# условия по перцентилям
rows = []
prb = expanding_pct(M.rb_gap, 36)
conds = {"rb_gap pct>0.9 (бочка дорога)": prb > 0.9, "rb_gap pct>0.8": prb > 0.8, "rb_gap pct<0.1 (бочка дешева)": prb < 0.1,
         "rb_gap pct<0.2": prb < 0.2, "rb_gap>+0.3": M.rb_gap > 0.3, "rb_gap<-0.2": M.rb_gap < -0.2,
         "rb_budget>+0.3": M.rb_budget > 0.3, "rb_budget<0": M.rb_budget < 0, "rb_budget<-0.15": M.rb_budget < -0.15,
         "rb_gap pct<0.2 & toxic": (prb < 0.2) & (M.cell == TOXIC), "rb_gap pct<0.2 & vol=1": (prb < 0.2) & (M.st_vol == 1)}
for wn in ("full_2004-2026", "split_2004-2017", "split_2018-2026", "full_ex2022"):
    wm = masks[wn]
    for cn, cond in conds.items():
        for h in (1, 3, 6):
            r = cond_stats(cond.fillna(False)[wm], M[f"fwd_ex_{h}"][wm], nboot=1000)
            rows.append(dict(window=wn, cond=cn, h=h, mean_ex=100 * r["mean_cond"], diff_ex=100 * r["diff"], hit=r["hit_cond"],
                             n_cond=r["n_cond"], p_boot=r["p_boot"]))
ct = pd.DataFrame(rows)
save(ct, "oil_conditions")
print("\nУсловия (избыток над ставкой, %), полная история:")
print(fmt(ct[ct.window == "full_2004-2026"].pivot_table(index="cond", columns="h", values=["mean_ex", "n_cond", "p_boot"]), 2))
print("\nУсловия h=3 по окнам:")
print(fmt(ct[ct.h == 3].pivot_table(index="cond", columns="window", values=["mean_ex", "p_boot"]), 2))

# стратегии
pos_panel_m = ((M.cell != TOXIC) & M.cell.notna() & (M.hyst == 1)).astype(float)
variants = {
    "панель": monthly_to_daily(pos_panel_m, df),
    "панель И НЕ rb_gap pct>0.9": monthly_to_daily(((pos_panel_m == 1) & ~(prb > 0.9)).astype(float), df),
    "панель И НЕ rb_gap>+0.3": monthly_to_daily(((pos_panel_m == 1) & ~(M.rb_gap > 0.3)).astype(float), df),
    "панель И НЕ rb_budget>+0.3": monthly_to_daily(((pos_panel_m == 1) & ~(M.rb_budget > 0.3)).astype(float), df),
    "панель ИЛИ rb_gap pct<0.1": monthly_to_daily(((prb < 0.1) | (pos_panel_m == 1)).astype(float), df),
    "панель ИЛИ rb_gap<-0.2": monthly_to_daily(((M.rb_gap < -0.2) | (pos_panel_m == 1)).astype(float), df),
    "панель ИЛИ rb_budget<-0.15": monthly_to_daily(((M.rb_budget < -0.15) | (pos_panel_m == 1)).astype(float), df),
}
R, T = evaluate_variants(df, M, variants, "oil", eps)
print("\nСтратегии:")
show_strats(R)
