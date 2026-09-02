"""G_decision, шаг 6: разложение вклада слоёв (ворота / композит / оба), подтверждение входа k дней,
своевременность итогового правила. Выход: results/G_decomp.csv, G_confirm.csv"""
import os
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from G_lib import *  # noqa
from G_rules import rule_pos  # noqa

d, c, m = load_raw()
F = build_features(d, c, m).loc["2003-06-01":]
bh, cash = bh_frame(F)
gate = F["gate"].values.astype(bool)
sign = F["sign_closed"].values
toxic = F["toxic"].values.astype(bool)

variants = {
    "gate_only": automaton(gate, ~gate, 0),
    "sign_only": automaton(sign > 0, ~(sign > 0), 0),
    "R0_both": automaton(gate & (sign > 0), ~(gate & (sign > 0)), 0),
    "R0_exit_toxic_only_daily_entry_monthly": None,
}
# вход только на конце месяца (как «решение раз в месяц»), выход по воротам ежедневно
me = month_end_mask(F)
buy = gate & (sign > 0) & me
sell = ~(gate & (sign > 0))
variants["R0_exit_toxic_only_daily_entry_monthly"] = automaton(buy, sell, 0)

rows = []
for label, pos in variants.items():
    bt = backtest(pos, F)
    for w in ("main_2010_2026", "full_2004_2026", "test_2018_2026", "era_2022_2024", "era_2025_2026"):
        a, b = WINDOWS_BT[w]
        mt = metrics(bt, bh, cash, a, b)
        rows.append({"variant": label, "window": w, **{k: mt[k] for k in ("cagr", "sharpe", "sharpe_ex_cash", "maxdd", "time_in_mkt", "switches_per_year")}})
D = pd.DataFrame(rows)
D.to_csv(os.path.join(RES, "G_decomp.csv"), index=False, float_format="%.4f")
print("=== разложение слоёв ===")
print(D.pivot(index="variant", columns="window", values="sharpe").round(3).to_string())
print(D.pivot(index="variant", columns="window", values="maxdd").round(3).to_string())
print(D.pivot(index="variant", columns="window", values="cagr").round(3).to_string())

# --- подтверждение входа: BUY требует ворота открытыми k дней подряд; SELL немедленный
print("\n=== подтверждение входа k дней (R0, выход немедленный) ===")
crow = []
run = np.zeros(len(F))
cnt = 0
for i in range(len(F)):
    cnt = cnt + 1 if gate[i] else 0
    run[i] = cnt
for k in (1, 2, 3, 5, 10, 15, 21):
    buy = (run >= k) & (sign > 0)
    sell = ~(gate & (sign > 0))
    pos = automaton(buy, sell, 0)
    bt = backtest(pos, F)
    r = {"k_confirm": k}
    for w in ("main_2010_2026", "full_2004_2026", "train_2004_2017", "test_2018_2026"):
        a, b = WINDOWS_BT[w]
        mt = metrics(bt, bh, cash, a, b)
        r[f"sharpe_{w}"] = mt["sharpe"]
        r[f"maxdd_{w}"] = mt["maxdd"]
        r[f"sw_{w}"] = mt["switches_per_year"]
    crow.append(r)
C = pd.DataFrame(crow)
C.to_csv(os.path.join(RES, "G_confirm.csv"), index=False, float_format="%.4f")
print(C.round(3).to_string(index=False))

# --- своевременность итогового правила
T = pd.read_csv(os.path.join(RES, "G_timeliness.csv"))
for label in ("R0_h0", "R1_th+0.3"):
    print(f"\n--- своевременность {label} ---")
    print(T[T.rule == label].drop(columns=["rule"]).to_string(index=False))
