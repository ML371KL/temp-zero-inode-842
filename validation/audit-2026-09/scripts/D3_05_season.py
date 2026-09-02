"""D3_05 — сезонность: календарные месяцы (MCFTR 2004+, IMOEX 1998+), дивидендный сезон, сентябрь,
январь, «sell in May» — с плацебо по месяцам/ротациям и поправкой на 12 сравнений; ex-2022; split."""
import numpy as np
import pandas as pd
from scipy import stats
from D3_lib import *

df = load_daily()
M = monthly_frame(df)
eps = episodes(df)
ltr, lpx = np.log(M.mcftr_ffill), np.log(M.imoex)
M["r_tr_m"] = ltr - ltr.shift(1)          # доходность MCFTR ЗА месяц, оканчивающийся в дату среза
M["r_px_m"] = lpx - lpx.shift(1)
M["ex_m"] = M.r_tr_m - M.rf_m
M["month"] = M.index.month
M["year"] = M.index.year

SAMPLES = {
    "MCFTR_ex 2004-2026": (in_window(M.index, "full_2004-2026"), "ex_m"),
    "MCFTR_ex 2004-2026 ex2022": (in_window(M.index, "full_2004-2026") & ex2022(M.index), "ex_m"),
    "MCFTR_ex 2004-2017": (in_window(M.index, "split_2004-2017"), "ex_m"),
    "MCFTR_ex 2018-2026": (in_window(M.index, "split_2018-2026"), "ex_m"),
    "MCFTR_ex 2022_03-2026": (in_window(M.index, "post2022_03-2026") if False else in_window(M.index, ("2022-03-01", "2026-08-31")), "ex_m"),
    "MCFTR_tr 2004-2026": (in_window(M.index, "full_2004-2026"), "r_tr_m"),
    "IMOEX_px 1998-2026": (in_window(M.index, ("1998-01-01", "2026-08-31")), "r_px_m"),
    "IMOEX_px 2004-2026": (in_window(M.index, "full_2004-2026"), "r_px_m"),
}
rows = []
for sn, (mask, col) in SAMPLES.items():
    x = M.loc[mask, [col, "month"]].dropna()
    for mo in range(1, 13):
        a = x[x.month == mo][col].values * 100
        b = x[x.month != mo][col].values * 100
        t, p = stats.ttest_ind(a, b, equal_var=False)
        rows.append(dict(sample=sn, month=mo, mean=a.mean(), median=np.median(a), n=len(a), hit=(a > 0).mean(), diff_vs_rest=a.mean() - b.mean(), t=t, p=p))
mt = pd.DataFrame(rows)
mt["q_bh12"] = np.nan
mt["p_bonf12"] = np.nan
for sn in SAMPLES:
    m = mt["sample"] == sn
    mt.loc[m, "q_bh12"] = fdr_bh(mt.loc[m, "p"].values)
    mt.loc[m, "p_bonf12"] = np.minimum(mt.loc[m, "p"] * 12, 1)
    mt.loc[m, "rank_of_12"] = mt.loc[m, "mean"].rank(ascending=False)
save(mt, "season_months")
print("Календарные месяцы: средняя (%), n, hit, p, q_BH(12), ранг:")
for sn in ("MCFTR_ex 2004-2026", "MCFTR_ex 2004-2026 ex2022", "MCFTR_ex 2004-2017", "MCFTR_ex 2018-2026", "IMOEX_px 1998-2026", "MCFTR_ex 2022_03-2026"):
    print(f"\n-- {sn}")
    print(fmt(mt[mt["sample"] == sn][["month", "mean", "median", "n", "hit", "diff_vs_rest", "t", "p", "q_bh12", "p_bonf12", "rank_of_12"]].set_index("month"), 2))

# знак стабилен между половинами?
a = mt[mt["sample"] == "MCFTR_ex 2004-2017"].set_index("month")["mean"]
b = mt[mt["sample"] == "MCFTR_ex 2018-2026"].set_index("month")["mean"]
print("\nЗнак средней по месяцам совпал в двух половинах:", int((np.sign(a) == np.sign(b)).sum()), "из 12; корреляция профилей:", round(np.corrcoef(a, b)[0, 1], 2))

# ---- составные сезонные правила
rules = {
    "дивсезон июнь-июль": [6, 7], "август (реинвест)": [8], "сентябрь": [9], "январь": [1], "декабрь-январь": [12, 1],
    "sell in May (май-окт)": [5, 6, 7, 8, 9, 10], "ноябрь-апрель": [11, 12, 1, 2, 3, 4], "июнь-сентябрь": [6, 7, 8, 9],
}
rows = []
for sn, (mask, col) in SAMPLES.items():
    x = M.loc[mask, [col, "month", "year"]].dropna()
    for rn, months in rules.items():
        inn = x[x.month.isin(months)][col].values * 100
        out = x[~x.month.isin(months)][col].values * 100
        t, p = stats.ttest_ind(inn, out, equal_var=False)
        # плацебо: все 12 ротаций окна той же длины
        L = len(months)
        diffs = []
        for s in range(1, 13):
            mm = [((s - 1 + k) % 12) + 1 for k in range(L)]
            diffs.append(x[x.month.isin(mm)][col].mean() * 100 - x[~x.month.isin(mm)][col].mean() * 100)
        diffs = np.array(diffs)
        obs = inn.mean() - out.mean()
        rank = int((diffs >= obs).sum()) if obs >= 0 else int((diffs <= obs).sum())
        rows.append(dict(sample=sn, rule=rn, mean_in=inn.mean(), mean_out=out.mean(), diff=obs, n_in=len(inn), hit_in=(inn > 0).mean(),
                         t=t, p=p, placebo_rank_of_12=rank, placebo_p=rank / 12))
rt = pd.DataFrame(rows)
save(rt, "season_rules")
print("\nСоставные сезонные правила (средняя за месяц, %; плацебо — ранг среди 12 ротаций окна той же длины):")
print(fmt(rt[rt["sample"].isin(("MCFTR_ex 2004-2026", "MCFTR_ex 2004-2026 ex2022", "MCFTR_ex 2004-2017", "MCFTR_ex 2018-2026", "IMOEX_px 1998-2026", "MCFTR_ex 2022_03-2026"))]
          .pivot_table(index="rule", columns="sample", values=["diff", "p", "placebo_rank_of_12"]), 2))

# дивидендный сезон: механический гэп IMOEX vs MCFTR
x = M[in_window(M.index, "full_2004-2026")]
gap = (x.r_tr_m - x.r_px_m) * 100
print("\nДивидендная разница MCFTR−IMOEX по месяцам (п.п., 2004+): ")
print(fmt(gap.groupby(x.month).agg(["mean", "median", "count"]).T, 2))
print("то же 2022-03+:")
x2 = M[in_window(M.index, ("2022-03-01", "2026-08-31"))]
print(fmt(((x2.r_tr_m - x2.r_px_m) * 100).groupby(x2.month).agg(["mean", "count"]).T, 2))

# сентябрь пост-2022 по годам
print("\nСентябрь по годам (MCFTR избыток, %):")
s = M[(M.month == 9) & (M.year >= 2015)][["ex_m", "r_tr_m", "r_px_m"]] * 100
print(fmt(s, 2))

# ---- стратегии
pos_panel_m = ((M.cell != TOXIC) & M.cell.notna() & (M.hyst == 1)).astype(float)
# позиция на СЛЕДУЮЩИЙ месяц: флаг «следующий месяц в списке»
next_month = ((M.index.month % 12) + 1)
nm = pd.Series(next_month, index=M.index)
variants = {
    "панель": monthly_to_daily(pos_panel_m, df),
    "b&h кроме мая-окт (Halloween)": monthly_to_daily((~nm.isin([5, 6, 7, 8, 9, 10])).astype(float), df),
    "b&h кроме сентября": monthly_to_daily((~nm.isin([9])).astype(float), df),
    "панель И НЕ сентябрь": monthly_to_daily(((pos_panel_m == 1) & ~nm.isin([9])).astype(float), df),
    "панель И НЕ май-окт": monthly_to_daily(((pos_panel_m == 1) & ~nm.isin([5, 6, 7, 8, 9, 10])).astype(float), df),
    "панель И НЕ июнь-сентябрь": monthly_to_daily(((pos_panel_m == 1) & ~nm.isin([6, 7, 8, 9])).astype(float), df),
    "панель ИЛИ (ворота откр. & дек-янв)": monthly_to_daily(((pos_panel_m == 1) | (((M.cell != TOXIC) & M.cell.notna()) & nm.isin([12, 1]))).astype(float), df),
}
R, T = evaluate_variants(df, M, variants, "season", eps)
print("\nСтратегии:")
show_strats(R)
