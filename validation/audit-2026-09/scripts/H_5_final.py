"""H_weekly, финал: (а) развёртка порога гистерезиса 0–0,8 по шагам M/W/D — плато или точка;
(б) вариант «ворота выходят любым днём, композит читается по пятницам» (W+gateExitD);
(в) итоговая таблица кандидатов против прода/b&h/денег по всем окнам, бутстреп ΔШарпа,
своевременность; (г) сколько разворотов знака внутри месяца доживает до его конца.
Запуск: python scripts/H_5_final.py"""
import sys
sys.path.insert(0, "scripts")
import numpy as np
import pandas as pd
from H_lib import *

D, M, C = load()
comp = pd.read_csv(f"{RES}/H_composites_daily.csv", index_col=0, parse_dates=True)
mk = market_daily(D, C)
idx = D.index
gate = gate_series(D)
me = month_end_idx(D)
we = week_end_idx(D)
dec = {"D": np.ones(len(idx), bool), "W": idx.isin(we), "M": idx.isin(me)}
wins = {"MAIN 2010-26": MAIN, "FULL 2004-26": FULL, **ERAS, "ex-2022": None, "2004-2017": SPLITS["2004-2017"], "2018-2026": SPLITS["2018-2026"]}


def run(pos, cost=0.002):
    bt = backtest_daily(pos, mk, cost)
    out = {}
    for wn, win in wins.items():
        d = slice_ex2022(bt) if win is None else slice_win(bt, win)
        out[wn] = metrics(d)
    return bt, out


# ---------------------------------------------------------------- (а) развёртка порога
print("=== развёртка порога гистерезиса: Шарп / переключений в год / доля времени в рынке ===")
rows = []
for step, version in [("M", "M_closed"), ("W", "W"), ("D", "D"), ("D", "M_live")]:
    for thr in [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]:
        sgn = hysteresis_sign(comp[version], thr)
        sig = ((sgn > 0) & (gate > 0)).astype(float)
        _, out = run(positions(sig, dec[step], idx))
        for wn, mt in out.items():
            rows.append(dict(step=step, version=version, thr=thr, window=wn, sharpe=round(mt["sharpe"], 3), cagr=round(mt["cagr"], 4),
                             maxdd=round(mt["maxdd"], 4), trades_yr=round(mt["trades_yr"], 2), time_in=round(mt["time_in"], 3)))
sw = pd.DataFrame(rows)
sw.to_csv(f"{RES}/H_threshold_sweep.csv", index=False)
for step, version in [("M", "M_closed"), ("W", "W"), ("D", "D")]:
    s = sw[(sw.step == step) & (sw.version == version)]
    print(f"\n{step}/{version}: Шарп")
    print(s.pivot(index="thr", columns="window", values="sharpe")[["MAIN 2010-26", "FULL 2004-26", "2004-2017", "2018-2026", "ex-2022", "2010-2021", "2022-2024", "2025-2026"]].to_string())
    print("переключений/год (MAIN), доля в рынке (MAIN):")
    print(s[s.window == "MAIN 2010-26"].set_index("thr")[["trades_yr", "time_in", "maxdd"]].T.to_string())

# ---------------------------------------------------------------- (б) кандидаты
sW1 = hysteresis_sign(comp["W"], 0.1)
sW2 = hysteresis_sign(comp["W"], 0.2)
sM1 = hysteresis_sign(comp["M_closed"], 0.1)
sM5 = hysteresis_sign(comp["M_closed"], 0.5)
sD1 = hysteresis_sign(comp["D"], 0.1)
sL1 = hysteresis_sign(comp["M_live"], 0.1)
# композит по пятницам, растянутый на неделю (чтобы внутри недели композит не менял сигнал)
compW_held = positions((sW1 > 0).astype(float), dec["W"], idx)
compW2_held = positions((sW2 > 0).astype(float), dec["W"], idx)
compM_held = positions((sM1 > 0).astype(float), dec["M"], idx)
cands = {
    "прод: M_closed/M (порог 0,1)": positions(((sM1 > 0) & (gate > 0)).astype(float), dec["M"], idx),
    "M_closed/M, порог 0,5": positions(((sM5 > 0) & (gate > 0)).astype(float), dec["M"], idx),
    "M_closed/M + ворота-выход любым днём": positions(((compM_held > 0) & (gate > 0)).astype(float), dec["M"], idx, exit_anytime=True),
    "W/W (порог 0,1)": positions(((sW1 > 0) & (gate > 0)).astype(float), dec["W"], idx),
    "W/W (порог 0,2)": positions(((sW2 > 0) & (gate > 0)).astype(float), dec["W"], idx),
    "W/W + ворота-выход любым днём (порог 0,1)": positions(((compW_held > 0) & (gate > 0)).astype(float), dec["W"], idx, exit_anytime=True),
    "W/W + ворота-выход любым днём (порог 0,2)": positions(((compW2_held > 0) & (gate > 0)).astype(float), dec["W"], idx, exit_anytime=True),
    "W/W + выход любым днём (порог 0,1)": positions(((sW1 > 0) & (gate > 0)).astype(float), dec["W"], idx, exit_anytime=True),
    "W/W, удержание 10 дн (порог 0,2)": positions(((sW2 > 0) & (gate > 0)).astype(float), dec["W"], idx, min_hold=10),
    "M_live/D (дневной шаг, прод-композит)": positions(((sL1 > 0) & (gate > 0)).astype(float), dec["D"], idx),
    "D/D (дневной шаг, дневной композит)": positions(((sD1 > 0) & (gate > 0)).astype(float), dec["D"], idx),
    "b&h MCFTR": pd.Series(1.0, index=idx),
    "деньги": pd.Series(0.0, index=idx),
}
rows = []
bts = {}
for name, pos in cands.items():
    bt, out = run(pos)
    bts[name] = bt
    for wn, mt in out.items():
        rows.append(dict(strategy=name, window=wn, **{k: round(v, 4) for k, v in mt.items()}))
fin = pd.DataFrame(rows)
fin.to_csv(f"{RES}/H_final_candidates.csv", index=False)
for wn in wins:
    sub = fin[fin.window == wn].set_index("strategy")
    print(f"\n=== {wn} ===")
    print(sub[["cagr", "vol", "sharpe", "sharpe_ex", "maxdd", "time_in", "trades_yr", "beat_years", "hit"]].to_string(float_format=lambda x: f"{x:.3f}"))

print("\n=== бутстреп ΔШарпа кандидатов против прода ===")
base_name = "прод: M_closed/M (порог 0,1)"
rows = []
for wn, win in [("MAIN 2010-26", MAIN), ("FULL 2004-26", FULL), ("ex-2022", None)]:
    base = to_monthly(slice_ex2022(bts[base_name]) if win is None else slice_win(bts[base_name], win))["ret"]
    for name in cands:
        if name in (base_name, "деньги"):
            continue
        r = to_monthly(slice_ex2022(bts[name]) if win is None else slice_win(bts[name], win))["ret"]
        diff, p, ci = sharpe_diff_boot(r, base)
        rows.append(dict(window=wn, strategy=name, sharpe_diff=round(diff, 3), p=round(p, 3), ci5=round(ci[0], 3), ci95=round(ci[1], 3)))
        print(f"{wn:14s} {name:45s} ΔSh={diff:+.3f} p={p:.3f} ДИ90=[{ci[0]:+.2f},{ci[1]:+.2f}]")
pd.DataFrame(rows).to_csv(f"{RES}/H_final_boot.csv", index=False)

print("\n=== своевременность кандидатов (просадки MCFTR >15% с 2004) ===")
rows = []
for name in [base_name, "M_closed/M + ворота-выход любым днём", "W/W (порог 0,1)", "W/W + ворота-выход любым днём (порог 0,1)", "W/W + ворота-выход любым днём (порог 0,2)", "M_closed/M, порог 0,5"]:
    t = timeliness(cands[name], mk["tr"], 0.15, "2004-01-01")
    t.insert(0, "strategy", name)
    rows.append(t)
tl = pd.concat(rows)
tl.to_csv(f"{RES}/H_final_timeliness.csv", index=False)
for name in tl.strategy.unique():
    print(f"\n{name}:")
    print(tl[tl.strategy == name].drop(columns="strategy").to_string(index=False))

print("\n=== издержки 0,1/0,2/0,3% для кандидатов (MAIN / FULL Шарп) ===")
rows = []
for cost in [0.001, 0.002, 0.003]:
    for name in [base_name, "W/W (порог 0,1)", "W/W + ворота-выход любым днём (порог 0,1)", "W/W + ворота-выход любым днём (порог 0,2)", "M_closed/M, порог 0,5"]:
        _, out = run(cands[name], cost)
        rows.append(dict(cost=cost, strategy=name, sh_main=round(out["MAIN 2010-26"]["sharpe"], 3), sh_full=round(out["FULL 2004-26"]["sharpe"], 3),
                         cagr_main=round(out["MAIN 2010-26"]["cagr"], 4)))
cs = pd.DataFrame(rows)
cs.to_csv(f"{RES}/H_final_cost.csv", index=False)
print(cs.pivot(index="strategy", columns="cost", values="sh_main").to_string())

# ---------------------------------------------------------------- (г) внутримесячные развороты знака
print("\n=== развороты знака M_live внутри месяца (2010–2026): доживают ли до закрытия месяца ===")
s = hysteresis_sign(comp["M_live"], 0.1)
sm = hysteresis_sign(comp["M_closed"], 0.1)
df = pd.DataFrame({"live": s, "closed": sm})
df = df[(df.index >= MAIN[0]) & (df.index <= MAIN[1])]
df["month"] = df.index.to_period("M")
rows = []
for m, g in df.groupby("month"):
    start_sign = g["closed"].iloc[0]  # знак закрытого месяца, действующий в этом месяце
    dev = g[g["live"] != start_sign]
    if len(dev) == 0 or not np.isfinite(start_sign):
        continue
    first = dev.index[0]
    end_sign = g["live"].iloc[-1]
    rows.append(dict(month=str(m), first_dev=first.date(), day_of_month=int((g.index < first).sum()) + 1,
                     n_days_dev=len(dev), survived=bool(end_sign != start_sign),
                     ret_from_dev_to_end_pct=round((np.log(mk["tr"].loc[g.index[-1]] / mk["tr"].loc[first]) - mk["r_mm"].loc[first:g.index[-1]].sum()) * 100 * (1 if end_sign > 0 or start_sign < 0 else -1), 2)))
fl = pd.DataFrame(rows)
fl.to_csv(f"{RES}/H_intramonth_flips.csv", index=False)
n_months = df["month"].nunique()
print(f"месяцев с внутримесячным разворотом знака: {len(fl)} из {n_months}; дожили до закрытия: {fl.survived.sum()} "
      f"({fl.survived.mean():.0%}); медианный день первого отклонения: {fl.day_of_month.median():.0f}-й торговый день")
print(f"разворотов знака M_closed за период: {int((sm.dropna() != sm.dropna().shift(1)).sum() - 1)}")
print(fl.to_string(index=False))
print("\nготово: results/H_threshold_sweep.csv, H_final_candidates.csv, H_final_boot.csv, H_final_timeliness.csv, H_final_cost.csv, H_intramonth_flips.csv")
