"""S_0 — санити: эталон правила панели и сверка дневного композита с продом."""
import sys
sys.path.insert(0, "scripts")
import numpy as np
import pandas as pd
from S_lib import *

d = load_daily()
m = load_monthly()

# 1. Эталон: ворота (ячейка на срезе) И знак закрытого композита с гистерезисом 0,1
dec = panel_decision_monthly(d, m)
pos = decision_to_daily(dec, d.index)
rows = []
for w, (a, b) in WINDOWS.items():
    bt = run_daily(pos, d, start=a, end=b)
    mt = metrics(bt, f"эталон {w}")
    rows.append(mt)
    print(w, "CAGR %.1f%% vol %.1f%% Sharpe %.2f ex %.2f MDDm %.1f%% MDDd %.1f%% in %.1f%% tr/y %.2f beat %.0f%% hit %.1f%% | b&h %.1f%%/%.2f/%.1f%% cash %.1f%%" % (
        mt["cagr"] * 100, mt["vol"] * 100, mt["sharpe"], mt["sharpe_ex"], mt["mdd_monthly"] * 100,
        mt["mdd_daily"] * 100, mt["in_market"] * 100, mt["trades_py"], mt["years_beat_bh"] * 100,
        mt["hit_months"] * 100, mt["cagr_bh"] * 100, mt["sharpe_bh"], mt["mdd_bh"] * 100, mt["cash_cagr"] * 100))
pd.DataFrame(rows).to_csv("results/S_0_baseline.csv", index=False)

# 2. Дневной живой композит из дневных ног против panel_prod_monthly
legs = d[["usd_mom63", "slope_10_2", "urals_rub_gap"]]
signs = {"usd_mom63": +1, "slope_10_2": +1, "urals_rub_gap": -1}
comp_live, zs = composite_live_daily(legs, signs)
me = month_end_mask(d.index)
cmp = pd.concat([comp_live[me], m["composite"].reindex(d.index[me])], axis=1, keys=["mine", "prod"]).dropna()
print("сверка живого композита на концах месяцев: n=%d max|diff|=%.2e" % (len(cmp), (cmp["mine"] - cmp["prod"]).abs().max()))
# месячный композит из месячных срезов
legs_m = legs.groupby(d.index.to_period("M")).last()
legs_m.index = d.index[me]
comp_m, _ = composite_monthly(legs_m, signs)
cmp2 = pd.concat([comp_m, m["composite"].reindex(comp_m.index)], axis=1, keys=["mine", "prod"]).dropna()
print("сверка месячного композита: n=%d max|diff|=%.2e" % (len(cmp2), (cmp2["mine"] - cmp2["prod"]).abs().max()))
comp_live.to_csv("results/S_composite_live_daily.csv", header=["composite_live"])
