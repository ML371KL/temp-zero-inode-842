"""S_1b — дополнение к находке 1: бит поверх лучших вола-конкурентов (RVI>30, бонд-бит),
ожидаемая доходность в избегаемых месяцах, и «правило-то доходное или дисперсионное».
"""
import sys
sys.path.insert(0, "scripts")
import numpy as np
import pandas as pd
from S_lib import *

d = load_daily()
m = load_monthly()
idx = d.index
me = month_end_mask(idx)
me_dates = idx[me]
dec_base = panel_decision_monthly(d, m)
pos_base = decision_to_daily(dec_base, idx)
W = ("2015-01-01", "2026-08-31")
cb = pd.read_csv("data/cb_decisions.csv", parse_dates=["date"]).set_index("date")["new_rate"]
key_dec = cb.reindex(idx.union(cb.index)).ffill().reindex(idx)
key_dec[idx < cb.index[0]] = d["key_rate"][idx < cb.index[0]]
key_dec = key_dec.where(d["key_rate"].notna())
sp = d["y1"] - key_dec
dsp = sp - sp.shift(21)
bit = (dsp > 0.25).astype(float); bit[dsp.isna()] = np.nan


def apply_exit(bits, base_dec=dec_base):
    dec = base_dec.copy()
    for b in bits:
        bb = b.reindex(me_dates).fillna(0)
        dec = dec.where(~(bb == 1), 0.0)
    dec[base_dec.isna()] = np.nan
    return decision_to_daily(dec, idx)


def ev(pos, name, base):
    bt = run_daily(pos, d, start=W[0], end=W[1]); mt = metrics(bt, name); mr = monthly_returns(bt)
    btb = run_daily(base, d, start=W[0], end=W[1]); mrb = monthly_returns(btb)
    j = mr.join(mrb, lsuffix="_x", rsuffix="_b").dropna()
    dsh, p, lo, hi = sharpe_diff_boot(j["strat_x"], j["strat_b"])
    print("%-60s CAGR %5.1f%% Sh %4.2f MDDm %6.1f%% in %5.1f%% tr/y %4.2f | ΔSh к базе %+.2f (p %.3f)" % (
        name, mt["cagr"] * 100, mt["sharpe"], mt["mdd_monthly"] * 100, mt["in_market"] * 100, mt["trades_py"], dsh, p))
    return mt


rvi30 = (d["rvi"] > 30).astype(float)
rvi35 = (d["rvi"] > 35).astype(float)
bond = d["st_bond"]
print("=== БИТ ПОВЕРХ ВОЛА-КОНКУРЕНТОВ (2015–2026) ===")
p_rvi = apply_exit([rvi30]); ev(p_rvi, "эталон + RVI>30", pos_base)
ev(apply_exit([rvi30, bit]), "эталон + RVI>30 + бит", p_rvi)
p_rvi35 = apply_exit([rvi35]); ev(p_rvi35, "эталон + RVI>35", pos_base)
ev(apply_exit([rvi35, bit]), "эталон + RVI>35 + бит", p_rvi35)
p_bond = apply_exit([bond]); ev(p_bond, "эталон + бонд-бит", pos_base)
ev(apply_exit([bond, bit]), "эталон + бонд-бит + бит", p_bond)
p_vol = apply_exit([d["st_vol"]]); ev(p_vol, "эталон + st_vol", pos_base)
ev(apply_exit([d["st_vol"], bit]), "эталон + st_vol + бит", p_vol)
# и наоборот: RVI поверх бита
p_bit = apply_exit([bit]); ev(p_bit, "эталон + бит", pos_base)
ev(apply_exit([bit, rvi30]), "эталон + бит + RVI>30", p_bit)

print("\n=== ОЖИДАЕМАЯ ДОХОДНОСТЬ В ИЗБЕГАЕМЫХ МЕСЯЦАХ (срез с бит=1, эталон в лонге) ===")
mc = d["mcftr"].reindex(me_dates)
fwd = mc.shift(-1) / mc - 1
cash_m = (1 + d["mm_rate"].shift(1) / 100 / 252).groupby(idx.to_period("M")).prod() - 1
cash_next = pd.Series(cash_m.reindex(me_dates.to_period("M") + 1).values, index=me_dates)
tbl = pd.DataFrame({"bit": bit.reindex(me_dates), "base": dec_base, "fwd": fwd, "cash": cash_next, "ex": fwd - cash_next}).loc["2015":"2026-07"].dropna()
for lab, mask in [("бит=1 & эталон лонг (избегаемые)", (tbl["bit"] == 1) & (tbl["base"] == 1)),
                  ("бит=0 & эталон лонг (остаёмся)", (tbl["bit"] == 0) & (tbl["base"] == 1)),
                  ("бит=1 (все срезы)", tbl["bit"] == 1), ("бит=0 (все срезы)", tbl["bit"] == 0)]:
    s = tbl.loc[mask]
    t = s["ex"].mean() / s["ex"].std() * np.sqrt(len(s)) if len(s) > 1 else np.nan
    print("  %-38s n=%3d  MCFTR след. мес: средн %+5.2f%% медиана %+5.2f%% ско %5.2f%% доля<0 %3.0f%% | избыток над кэшем средн %+5.2f%% (t %.2f)" % (
        lab, len(s), s["fwd"].mean() * 100, s["fwd"].median() * 100, s["fwd"].std() * 100, (s["fwd"] < 0).mean() * 100, s["ex"].mean() * 100, t))
a = tbl.loc[(tbl["bit"] == 1) & (tbl["base"] == 1), "fwd"]; b = tbl.loc[(tbl["bit"] == 0) & (tbl["base"] == 1), "fwd"]
from scipy import stats
print("  Уэлч по средним (избегаемые vs остаёмся): t=%.2f p=%.3f; Левен по дисперсиям: p=%.3f; Манн-Уитни p=%.3f" % (
    *stats.ttest_ind(a, b, equal_var=False), stats.levene(a, b).pvalue, stats.mannwhitneyu(a, b).pvalue))
