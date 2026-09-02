"""S2 — находка 2, дополнение: откуда выигрыш P5 — по эрам ног (одна нога 2010–2015 / три ноги 2016+),
доля сентября-2022, семейство только трёхногих спецификаций, P3/P4 по годам. Запуск: python scripts/S2_2b_p5_extra.py
"""
import sys
sys.path.insert(0, "scripts")
import numpy as np
import pandas as pd
from S2_lib import *  # noqa

D, C, M = load_all()
cuts = month_end_cuts(D.index)
log = open(f"{RES}/S2_2b_log.txt", "w", encoding="utf-8")


def P(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    log.write(s + "\n")


usd = D["usd"]
LEG = {"usd_mom63": (D["usd_mom63"], +1), "usd_ma200": (np.log(usd / usd.rolling(200).mean()), +1),
       "slope_10_2": (D["slope_10_2"], +1), "urals_rub_gap": (D["urals_rub_gap"], -1), "y2_key": (D["y2"] - D["key_rate"], +1)}
ZS = pd.DataFrame({k: v[1] * zroll(v[0].reindex(cuts)) for k, v in LEG.items()})
gate_m = (D["cell"].reindex(cuts) != TOXIC).astype(float)
tr = C["mcftr_ffill"]
mmc = (1 + C["mm_rate"] / 100.0 / 252.0).cumprod()
t_, m_, px = tr.reindex(cuts), mmc.reindex(cuts), D["imoex"].reindex(cuts)
FM = pd.DataFrame({"fwd_tr": t_.shift(-1) / t_ - 1, "fwd_mm": m_.shift(-1) / m_ - 1, "fwd_imoex": np.log(px.shift(-1) / px)})
FM.loc[cuts[-1]] = np.nan
FM = FM[FM.index <= pd.Timestamp(MAIN[1])]


def comp(legs):
    return ZS[list(legs)].mean(axis=1, skipna=True)


def panel(c, thr=0.1):
    sg = hysteresis_sign(c, thr)
    pos = ((sg > 0) & (gate_m > 0)).astype(float)
    p = pos.reindex(FM.index).fillna(0).values
    trd = np.abs(np.diff(np.r_[p[0], p]))
    r = p * FM["fwd_tr"].values + (1 - p) * FM["fwd_mm"].values - COST * trd
    return pd.Series(r, index=FM.index), pos


def win(s, w):
    return s[(s.index >= pd.Timestamp(w[0])) & (s.index <= pd.Timestamp(w[1]))]


SP = {"P0": ["usd_mom63", "slope_10_2", "urals_rub_gap"], "P3": ["usd_ma200", "slope_10_2", "urals_rub_gap"],
      "P4": ["usd_mom63", "slope_10_2", "urals_rub_gap", "y2_key"], "P5": ["usd_ma200", "slope_10_2", "urals_rub_gap", "y2_key"]}
R = {k: win(panel(comp(v))[0], MAIN).dropna() for k, v in SP.items()}
CM = {k: comp(v) for k, v in SP.items()}
ERAS2 = {"одна нога 2010-01…2015-12": ("2010-01-01", "2015-12-31"), "две ноги 2016-01…2017-02": ("2016-01-01", "2017-02-28"),
         "три/четыре ноги 2017-03…2024-12": ("2017-03-01", "2024-12-31"), "2025-01…2026-08": ("2025-01-01", "2026-08-31"),
         "три/четыре ноги 2017-03…2026-08": ("2017-03-01", "2026-08-31")}
P("[эры] Шарп / IC по эрам состава (в одноногой эре P3 = соло usd/MA200, P5 = P3; y2−key появляется с 2017-03):")
rows = []
for ename, w in ERAS2.items():
    row = dict(era=ename, n=len(win(R["P0"], w)))
    for k in SP:
        rr = win(R[k], w)
        row[f"sh_{k}"] = sharpe(rr)
        row[f"ic_{k}"] = spearman_ic(win(CM[k], w), win(FM["fwd_imoex"], w))[0]
        row[f"cagr_{k}"] = (1 + rr).prod() ** (12 / len(rr)) - 1
    rows.append(row)
er = pd.DataFrame(rows)
er.to_csv(f"{RES}/S2_p5_eras.csv", index=False, float_format="%.4f")
P(er.round(3).to_string())

# вклад отдельных месяцев в P5 − P0
d = (R["P5"] - R["P0"])
P(f"\n[месяцы] P5 − P0: сумма {d.sum()*100:+.1f} п.п.; месяцев с разной позицией {int((d.abs() > 1e-9).sum())}; топ-8:")
P((d[d.abs() > 1e-9].sort_values(key=abs, ascending=False).head(8) * 100).round(2).to_string())
pos_ = d[d > 0].sort_values(ascending=False)
P(f"  доля 2022-09 в сумме плюсов: {d.get(pd.Timestamp('2022-08-31'), 0)/pos_.sum():.0%}; топ-3 плюсов / сумма плюсов: {pos_.head(3).sum()/pos_.sum():.0%}")
for k in ["P3", "P4"]:
    dk = (R[k] - R["P0"])
    pk = dk[dk > 0]
    P(f"  {k} − P0: сумма {dk.sum()*100:+.1f} п.п.; месяцев расхождения {int((dk.abs() > 1e-9).sum())}; топ-3 плюсов / сумма плюсов {pk.sort_values(ascending=False).head(3).sum()/pk.sum():.0%}; "
      f"крупнейшие: " + ", ".join(f"{i.strftime('%Y-%m')}:{v*100:+.1f}" for i, v in dk.sort_values(key=abs, ascending=False).head(4).items()))
# без 2022-09 и без одноногой эры
P("\n[устойчивость] ΔШарп к P0 без 2022-09 / без 2022 / только 2016+ / только 2017-03+ / без 2025–26:")
for label, mask_fn in [("полная", lambda i: np.ones(len(i), bool)), ("без 2022-09", lambda i: i.strftime("%Y-%m") != "2022-08"),
                       ("без 2022", lambda i: i.year != 2022), ("2016+", lambda i: i >= pd.Timestamp("2016-01-01")),
                       ("2017-03+", lambda i: i >= pd.Timestamp("2017-03-01")), ("без 2025–26", lambda i: i < pd.Timestamp("2025-01-01")),
                       ("2017-03+ без 2025–26", lambda i: (i >= pd.Timestamp("2017-03-01")) & (i < pd.Timestamp("2025-01-01")))]:
    m = mask_fn(R["P0"].index)
    s = f"  {label:22s} n={m.sum():3d}: " + "  ".join(f"{k} {sharpe(R[k][m]):.2f}" for k in SP)
    d5, p5, lo, hi = boot_sharpe_diff(R["P5"][m].values, R["P0"][m].values)
    d3, p3, _, _ = boot_sharpe_diff(R["P3"][m].values, R["P0"][m].values)
    d4, p4, _, _ = boot_sharpe_diff(R["P4"][m].values, R["P0"][m].values)
    P(s + f"  | Δ P3 {d3:+.2f} (p {p3:.2f}), P4 {d4:+.2f} (p {p4:.2f}), P5 {d5:+.2f} (p {p5:.2f})")

# по годам: P3 и P4 против P0
P("\n[годы] доходность за год: P0 / P3 / P4 / P5 и IC за год")
rows = []
for y in range(2010, 2027):
    m = R["P0"].index.year == y
    row = dict(year=y)
    for k in SP:
        row[f"ret_{k}"] = (1 + R[k][m]).prod() - 1
        row[f"ic_{k}"] = spearman_ic(win(CM[k], MAIN)[lambda s: s.index.year == y], win(FM["fwd_imoex"], MAIN)[lambda s: s.index.year == y])[0]
    row["bh"] = (1 + FM["fwd_tr"].reindex(R["P0"].index)[m]).prod() - 1
    row["mm"] = (1 + FM["fwd_mm"].reindex(R["P0"].index)[m]).prod() - 1
    rows.append(row)
yr = pd.DataFrame(rows)
yr.to_csv(f"{RES}/S2_p5_years.csv", index=False, float_format="%.4f")
P(yr.round(3).to_string())
P(f"  P3 > P0 в {int((yr.ret_P3 - yr.ret_P0 > 0.005).sum())} годах, < в {int((yr.ret_P3 - yr.ret_P0 < -0.005).sum())}; P4 > P0 в {int((yr.ret_P4 - yr.ret_P0 > 0.005).sum())}, < в {int((yr.ret_P4 - yr.ret_P0 < -0.005).sum())}")

# семейство только трёхногих (без 4-й ноги) — доля бьющих P0
fam = pd.read_csv(f"{RES}/S2_p5_family.csv", index_col=0)
three = fam[[n.count("+") == 2 for n in fam.index]]
four = fam[[n.count("+") == 3 for n in fam.index]]
P(f"\n[семейство] трёхногих {len(three)}: медиана Шарпа {three.sharpe.median():.2f}, доля > P0 (1,07): {(three.sharpe > 1.073).mean():.0%}; "
  f"четырёхногих {len(four)}: медиана {four.sharpe.median():.2f}, доля > P0 {(four.sharpe > 1.073).mean():.0%}")
for leg in ["usd_ma200", "usd_mom63", "y2_key", "y1_key", "y10_key", "slope_10_2", "urals_rub_gap", "brent_mom63", "rb_gap"]:
    has = fam[[leg in n.split("+") for n in fam.index]]
    not_ = fam[[leg not in n.split("+") for n in fam.index]]
    P(f"  с ногой {leg:14s}: n={len(has):3d}, медиана Шарпа {has.sharpe.median():.2f} (IC {has.ic.median():.3f}); без: {not_.sharpe.median():.2f} (IC {not_.ic.median():.3f})")
log.close()
