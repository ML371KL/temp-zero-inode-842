"""D2_00: эталон — текущее правило панели (ворота + знак закрытого месяца композита).
Проверка движка на числах брифа и базовые метрики по всем окнам + своевременность."""
import numpy as np
import pandas as pd
from D2_lib import *

d = derive_rates(load_daily())
m = monthly_frame(d)
pos = baseline_position(m)

# --- 1. санити: price-only IMOEX, флэт = 0, без издержек, с 2004 (бриф: +7,3%/0,48/−19,9%)
chk = pd.DataFrame({"pos": pos, "r_long": m["fwd1m_px"], "r_cash": 0.0}).dropna()
chk = chk[chk.index >= "2004-01-01"]
chk["trade"] = chk["pos"].diff().abs().fillna(0)
chk["ret"] = chk["pos"] * chk["r_long"]
mt = metrics(chk, name="sanity_price_only_2004")
print("САНИТИ price-only 2004+: cagr=%.1f%% sharpe=%.2f mdd=%.1f%% (бриф: 7,3 / 0,48 / −19,9)" %
      (mt["cagr"] * 100, mt["sharpe"], mt["maxdd"] * 100))
# без гистерезиса
pos_nh = ((m["composite"] > 0) & (m["cell"] != TOXIC) & m["cell"].notna()).astype(float)
chk2 = chk.copy(); chk2["pos"] = pos_nh.reindex(chk2.index).fillna(0); chk2["ret"] = chk2["pos"] * chk2["r_long"]
chk2["trade"] = chk2["pos"].diff().abs().fillna(0)
mt2 = metrics(chk2, name="sanity_nohyst")
print("   без гистерезиса: cagr=%.1f%% sharpe=%.2f mdd=%.1f%%" % (mt2["cagr"] * 100, mt2["sharpe"], mt2["maxdd"] * 100))

# --- 2. эталон в полной доходности по окнам
rows = []
for nm, (a, b) in ERAS.items():
    for cost in (0.001, 0.002, 0.003):
        df = run_monthly(pos, m, cost=cost, start=a, end=b)
        r = metrics(df, name=f"baseline|{nm}|cost{cost}")
        r["window"] = nm; r["cost"] = cost
        rows.append(r)
    # ex-2022
    df = run_monthly(pos, m, cost=COST, start=a, end=b)
    df = df[df.index.year != 2022]
    r = metrics(df, name=f"baseline|{nm}|ex2022"); r["window"] = nm + "_ex2022"; r["cost"] = COST
    rows.append(r)
    # чистые ворота (без композита) и чистый композит (без ворот)
    for lbl, pp in [("gate_only", ((m["cell"] != TOXIC) & m["cell"].notna()).astype(float)),
                    ("core_only", (m["comp_sign"] > 0).astype(float)),
                    ("always_long", pd.Series(1.0, index=m.index))]:
        df = run_monthly(pp, m, cost=COST, start=a, end=b)
        r = metrics(df, name=f"{lbl}|{nm}"); r["window"] = nm; r["cost"] = COST
        rows.append(r)
tab = fmt_metrics_table(rows)
tab.to_csv(RES / "D2_00_baseline_metrics.csv", index=False)
print(tab[["name", "n", "cagr", "vol", "sharpe", "sharpe_excess", "maxdd", "time_in_mkt", "trades_per_yr",
           "beat_bh_years", "hit_rate", "bh_cagr", "bh_sharpe", "bh_maxdd", "cash_cagr"]].to_string())

# --- 3. своевременность эталона (правило 7): просадки MCFTR > 15% с 2004
price = d["mcftr_ffill"].dropna()
price = price[price.index >= "2004-01-01"]
eps = drawdown_episodes(price, 0.15)
cash_daily = (d["mm_rate"] / 100 / 252).reindex(price.index).fillna(0)
pos_d = monthly_to_daily_pos(pos, price.index)
tl = timeliness(pos_d, price, cash_daily, eps)
tl.to_csv(RES / "D2_00_baseline_timeliness.csv", index=False)
print(tl.to_string())
