"""D1: базовая линия — правило панели (ворота + знак закрытого месяца с гистерезисом),
b&h MCFTR, 100% денежный рынок. Пишет results/D1_baseline_positions.csv (дневные позиции)
и results/D1_baseline_metrics.csv, results/D1_baseline_timeliness.csv."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from D1_common import *

p = load_daily()
m = load_monthly()
pos_m, gate_m, slope_m = panel_rule_monthly(m)
pos_d_monthly = to_daily_position(pos_m, p.index)
pos_d_dgate = panel_rule_daily_gate(p, m)
# «только ворота» и «только наклон» — для разложения
pos_gate_only = to_daily_position(gate_m, p.index)
pos_slope_only = to_daily_position(slope_m, p.index)

out = pd.DataFrame({"pos_panel_m": pos_d_monthly, "pos_panel_dgate": pos_d_dgate,
                    "pos_gate_only": pos_gate_only, "pos_slope_only": pos_slope_only},
                   index=p.index)
out.to_csv("results/D1_baseline_positions.csv")

WINDOWS = {"2010-2026": ("2010-01-01", "2026-08-31"), "2004-2026": ("2004-01-01", "2026-08-31"),
           "2010-2021": ("2010-01-01", "2022-02-18"), "2022-2024": ("2022-03-24", "2024-12-31"),
           "2025-2026": ("2025-01-01", "2026-08-31"), "2020-07..2026": ("2020-07-01", "2026-08-31"),
           "2015-2026": ("2015-01-01", "2026-08-31")}
rows = []
tl_rows = []
for wname, (a, b) in WINDOWS.items():
    bts = {}
    for nm, pos in [("panel_m", pos_d_monthly), ("panel_dgate", pos_d_dgate),
                    ("gate_only", pos_gate_only), ("slope_only", pos_slope_only)]:
        bt, d = eval_strategy(p, pos, a, b, nm)
        d["window"] = wname
        rows.append(d)
        bts[nm] = bt
    bt = bts["panel_m"]
    for nm, col in [("bh", "bh"), ("cash", "cash")]:
        d = metrics(bt, col); d["name"] = nm; d["window"] = wname; rows.append(d)
    # ex-2022 для основного окна
    if wname == "2010-2026":
        for nm in ["panel_m", "panel_dgate"]:
            b2 = bts[nm]
            mask = ~((b2.index >= "2022-01-01") & (b2.index <= "2022-12-31"))
            d = metrics(b2[mask]); d["name"] = nm; d["window"] = "2010-2026 ex2022"; rows.append(d)
        d = metrics(bt[mask], "bh"); d["name"] = "bh"; d["window"] = "2010-2026 ex2022"; rows.append(d)
    if wname in ("2004-2026",):
        for nm in ["panel_m", "panel_dgate"]:
            t = timeliness(bts[nm]); t.insert(0, "rule", nm); tl_rows.append(t)

R = pd.DataFrame(rows)
cols = ["window", "name", "cagr", "vol", "sharpe", "sharpe_ex", "maxdd", "tim", "trades_yr",
        "beat_bh_years", "hit_m", "beat_bh_m", "n_days"]
R = R[cols]
R.to_csv("results/D1_baseline_metrics.csv", index=False, float_format="%.4f")
print(R.to_string())
T = pd.concat(tl_rows)
T.to_csv("results/D1_baseline_timeliness.csv", index=False)
print(T.to_string())

# чувствительность к издержкам
for c in (0.001, 0.003):
    bt, d = eval_strategy(p, pos_d_monthly, "2010-01-01", "2026-08-31", "panel_m", cost=c)
    print(f"cost {c}: {fmt_metrics(d)}")
