"""H_weekly, задачи 2 и 4: стратегии long/flat «ворота ∧ композит>0» с частотой решения
дневной / недельной (пятница) / месячной (закрытый месяц, как в проде) при одинаковых входах;
сетка «версия композита × частота решения» разделяет вклад ворот и композита; асимметрия
«выход любым днём, вход по расписанию»; плацебо по дню месяца; цена «месячного якоря»
(событийный разбор); своевременность (правило 7); бутстреп разности Шарпов; издержки 0,1/0,2/0,3%.
Запуск: python scripts/H_2_strategies.py  (из каталога audit/)"""
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
VERSIONS = ["M_closed", "M_live", "W", "D"]
HYST = 0.1

signs = {v: hysteresis_sign(comp[v], HYST) for v in VERSIONS}
# сигнал = ворота открыты ∧ знак > 0 (NaN знака → флэт)
signal = {v: ((signs[v] > 0) & (gate > 0)).astype(float) for v in VERSIONS}
signal_core_only = {v: (signs[v] > 0).astype(float) for v in VERSIONS}
signal_gate_only = gate.copy()

strategies = {}
# --- основная сетка: версия композита × частота решения (ворота и композит читаются в день решения)
for v in VERSIONS:
    for f in ["D", "W", "M"]:
        strategies[f"{v}/{f}"] = positions(signal[v], dec[f], idx)
# --- асимметрия: выход любым днём, вход только по расписанию
for v in ["M_live", "W", "D", "M_closed"]:
    for f in ["W", "M"]:
        strategies[f"{v}/{f}+exitD"] = positions(signal[v], dec[f], idx, exit_anytime=True)
# --- разложение: только ворота / только композит
for f in ["D", "W", "M"]:
    strategies[f"gate_only/{f}"] = positions(signal_gate_only, dec[f], idx)
for v in ["M_closed", "D"]:
    for f in ["D", "M"]:
        strategies[f"core_only:{v}/{f}"] = positions(signal_core_only[v], dec[f], idx)
# --- смешанные: ворота дневные, композит месячный (M_closed) — это ровно M_closed/D; и наоборот:
# композит дневной, но читается только на конце месяца — это D/M. Добавим «ворота M + композит D ежедневно»:
sig_mix = ((signs["D"] > 0) & (positions(gate, dec["M"], idx) > 0)).astype(float)
strategies["gateM+compD/D"] = positions(sig_mix, dec["D"], idx)
sig_mix2 = ((positions((signs["D"] > 0).astype(float), dec["M"], idx) > 0) & (gate > 0)).astype(float)
strategies["gateD+compM/D"] = positions(sig_mix2, dec["D"], idx)
strategies["b&h"] = pd.Series(1.0, index=idx)
strategies["cash"] = pd.Series(0.0, index=idx)

PROD = "M_closed/M"
wins = {"MAIN 2010-26": MAIN, "FULL 2004-26": FULL, **ERAS, "ex-2022": None,
        "2004-2017": SPLITS["2004-2017"], "2018-2026": SPLITS["2018-2026"]}

rows = []
bt = {}
for name, pos in strategies.items():
    bt[name] = backtest_daily(pos, mk, cost=0.002)
    for wn, win in wins.items():
        d = slice_ex2022(bt[name]) if win is None else slice_win(bt[name], win)
        mt = metrics(d)
        if mt:
            rows.append(dict(strategy=name, window=wn, **{k: round(v, 4) for k, v in mt.items()}))
res = pd.DataFrame(rows)
res.to_csv(f"{RES}/H_strategies.csv", index=False)

def show(wn, names=None):
    sub = res[res.window == wn].set_index("strategy")
    if names:
        sub = sub.loc[[n for n in names if n in sub.index]]
    cols = ["cagr", "vol", "sharpe", "sharpe_ex", "maxdd", "time_in", "trades_yr", "beat_years", "hit"]
    print(f"\n=== {wn} ===  (b&h: CAGR {sub.bh_cagr.iloc[0]*100:+.1f}%, Sh {sub.bh_sharpe.iloc[0]:.2f}, MDD {sub.bh_maxdd.iloc[0]*100:.1f}%; mm {sub.mm_cagr.iloc[0]*100:.1f}%)")
    print(sub[cols].to_string(float_format=lambda x: f"{x:.3f}"))

main_names = [f"{v}/{f}" for v in VERSIONS for f in ["M", "W", "D"]]
for wn in ["MAIN 2010-26", "FULL 2004-26", "2010-2021", "2022-2024", "2025-2026", "ex-2022", "2004-2017", "2018-2026"]:
    show(wn, main_names + ["b&h", "cash"])
print("\n--- асимметрия и разложение ---")
for wn in ["MAIN 2010-26", "FULL 2004-26", "ex-2022"]:
    show(wn, [PROD, "M_live/D", "W/W", "D/D", "M_live/M+exitD", "W/W+exitD", "D/W+exitD", "D/M+exitD", "M_closed/M+exitD",
              "gate_only/M", "gate_only/W", "gate_only/D", "core_only:M_closed/M", "core_only:D/D", "core_only:D/M",
              "gateM+compD/D", "gateD+compM/D"])

# ---------------------------------------------------------------- бутстреп разности Шарпов
print("\n--- бутстреп разности Шарпов против прода (месячные доходности, блоки 8 мес) ---")
rows = []
for wn, win in [("MAIN 2010-26", MAIN), ("FULL 2004-26", FULL), ("ex-2022", None)]:
    base = to_monthly(slice_ex2022(bt[PROD]) if win is None else slice_win(bt[PROD], win))["ret"]
    for name in ["M_live/D", "M_live/W", "W/W", "D/D", "D/W", "M_closed/D", "M_closed/W", "D/M", "W/M",
                 "M_live/M+exitD", "W/W+exitD", "D/M+exitD", "M_closed/M+exitD", "gate_only/M", "b&h"]:
        r = to_monthly(slice_ex2022(bt[name]) if win is None else slice_win(bt[name], win))["ret"]
        diff, p, ci = sharpe_diff_boot(r, base)
        rows.append(dict(window=wn, strategy=name, vs=PROD, sharpe_diff=round(diff, 3), p=round(p, 3),
                         ci5=round(ci[0], 3), ci95=round(ci[1], 3)))
        print(f"{wn:14s} {name:18s} ΔSh={diff:+.3f} p={p:.3f} ДИ90=[{ci[0]:+.2f},{ci[1]:+.2f}]")
pd.DataFrame(rows).to_csv(f"{RES}/H_sharpe_boot.csv", index=False)

# ---------------------------------------------------------------- издержки 0,1 / 0,3
print("\n--- чувствительность к издержкам (MAIN) ---")
rows = []
for cost in [0.001, 0.002, 0.003]:
    for name in [PROD, "M_live/D", "W/W", "D/D", "M_closed/D", "D/M", "W/W+exitD", "D/M+exitD"]:
        for wn, win in [("MAIN 2010-26", MAIN), ("FULL 2004-26", FULL)]:
            mt = metrics(slice_win(backtest_daily(strategies[name], mk, cost=cost), win))
            rows.append(dict(cost=cost, strategy=name, window=wn, cagr=round(mt["cagr"], 4), sharpe=round(mt["sharpe"], 3),
                             maxdd=round(mt["maxdd"], 4), trades_yr=round(mt["trades_yr"], 2)))
cs = pd.DataFrame(rows)
cs.to_csv(f"{RES}/H_cost_sensitivity.csv", index=False)
print(cs[cs.window == "MAIN 2010-26"].pivot(index="strategy", columns="cost", values="sharpe"))

# ---------------------------------------------------------------- плацебо: месячное решение в k-й торговый день месяца
print("\n--- плацебо: месячное решение в k-й торговый день месяца (входы читаются в день решения) ---")
day_in_month = idx.to_series().groupby(idx.to_period("M")).cumcount().values + 1
rows = []
for v in ["M_live", "D", "M_closed"]:
    for k in range(1, 20):
        decd = day_in_month == k
        pos = positions(signal[v], decd, idx)
        for wn, win in [("MAIN 2010-26", MAIN), ("FULL 2004-26", FULL)]:
            mt = metrics(slice_win(backtest_daily(pos, mk), win))
            rows.append(dict(version=v, day_k=k, window=wn, cagr=round(mt["cagr"], 4), sharpe=round(mt["sharpe"], 3),
                             maxdd=round(mt["maxdd"], 4), trades_yr=round(mt["trades_yr"], 2)))
    for wn, win in [("MAIN 2010-26", MAIN), ("FULL 2004-26", FULL)]:
        mt = metrics(slice_win(bt[f"{v}/M"], win))
        rows.append(dict(version=v, day_k=0, window=wn, cagr=round(mt["cagr"], 4), sharpe=round(mt["sharpe"], 3),
                         maxdd=round(mt["maxdd"], 4), trades_yr=round(mt["trades_yr"], 2)))
pl = pd.DataFrame(rows)
pl.to_csv(f"{RES}/H_placebo_day_of_month.csv", index=False)
for v in ["M_live", "D", "M_closed"]:
    s = pl[(pl.version == v) & (pl.window == "MAIN 2010-26")]
    eom = s[s.day_k == 0].sharpe.iloc[0]
    oth = s[s.day_k > 0].sharpe
    print(f"{v}: конец месяца Sh={eom:.3f}; дни 1..19: медиана {oth.median():.3f}, мин {oth.min():.3f}, макс {oth.max():.3f}, "
          f"ранг конца месяца {int((oth > eom).sum()) + 1} из 20; недельное W/W Sh={res[(res.strategy=='W/W')&(res.window=='MAIN 2010-26')].sharpe.iloc[0]:.3f}")

# ---------------------------------------------------------------- цена месячного якоря: событийный разбор
print("\n--- цена «месячного якоря»: дни, когда дневной сигнал (ворота ∧ M_live) расходится с позицией прода ---")
sig_d = signal["M_live"]
pos_prod = strategies[PROD]
ex = (mk["r_tr"] - mk["r_mm"])  # избыток лонга над флэтом на день
# позиция действует на следующий день: расхождение на закрытии t влияет на доходность t+1
diff_pos = (sig_d - pos_prod).shift(1).fillna(0)
gain_if_follow = diff_pos * ex  # >0: следование дневному сигналу дало бы больше
df = pd.DataFrame({"sig": sig_d, "prod": pos_prod, "gain": gain_if_follow, "ex": ex})
df["month"] = df.index.to_period("M")
rows = []
for m, g in df.groupby("month"):
    dd = g[g["sig"] != g["prod"]]
    if len(dd) == 0:
        continue
    first = dd.index[0]
    direction = "выход-раньше" if dd["sig"].iloc[0] < dd["prod"].iloc[0] else "вход-раньше"
    # сохранился ли сигнал к концу месяца (то есть прод переключился на следующем срезе)?
    # «дожил»: прод на срезе конца месяца перешёл туда, где дневной сигнал уже стоял
    # (позиция прода после обновления на последнем дне ≠ позиции прода днём раньше)
    persisted = bool(len(g) > 1 and g["prod"].iloc[-1] != g["prod"].iloc[-2] and g["sig"].iloc[-1] == g["prod"].iloc[-1])
    rows.append(dict(month=str(m), first_diff=first.date(), direction=direction, days_diff=len(dd),
                     persisted_to_month_end=persisted, gain_if_follow_pct=round(g["gain"].sum() * 100, 2)))
ev = pd.DataFrame(rows)
ev.to_csv(f"{RES}/H_anchor_events.csv", index=False)
evm = ev[(ev.month >= "2010-01") & (ev.month <= "2026-08")]
print(f"месяцев с расхождением: {len(evm)} из {len(df[(df.index>=MAIN[0])&(df.index<=MAIN[1])].month.unique())}; "
      f"суммарный выигрыш следования = {evm.gain_if_follow_pct.sum():+.1f} п.п. за 2010–2026 "
      f"(в год {evm.gain_if_follow_pct.sum()/16.67:+.2f} п.п.)")
for d_, g in evm.groupby("direction"):
    print(f"  {d_}: n={len(g)}, сумма {g.gain_if_follow_pct.sum():+.1f} п.п., медиана {g.gain_if_follow_pct.median():+.2f}, "
          f"доля сохранившихся до конца месяца {g.persisted_to_month_end.mean():.0%}")
for p_, g in evm.groupby("persisted_to_month_end"):
    print(f"  сохранился={p_}: n={len(g)}, сумма {g.gain_if_follow_pct.sum():+.1f} п.п.")
print("топ-10 эпизодов по |выигрышу|:")
print(evm.reindex(evm.gain_if_follow_pct.abs().sort_values(ascending=False).index).head(10).to_string(index=False))
print("сумма по годам:")
print(evm.assign(y=evm.month.str[:4]).groupby("y").gain_if_follow_pct.sum().round(1).to_string())

# ---------------------------------------------------------------- своевременность (правило 7)
print("\n--- своевременность: просадки MCFTR > 15% с 2004 ---")
rows = []
for name in [PROD, "M_closed/D", "M_live/D", "M_live/W", "W/W", "D/D", "W/W+exitD", "D/M+exitD", "gate_only/D", "gate_only/M"]:
    t = timeliness(strategies[name], mk["tr"], 0.15, "2004-01-01")
    t.insert(0, "strategy", name)
    rows.append(t)
tl = pd.concat(rows)
tl.to_csv(f"{RES}/H_timeliness.csv", index=False)
for name in [PROD, "M_live/D", "W/W", "D/D", "W/W+exitD"]:
    print(f"\n{name}:")
    print(tl[tl.strategy == name].drop(columns="strategy").to_string(index=False))
summ = tl.groupby("strategy").agg(n=("depth", "size"), med_days_to_flat=("days_to_flat", "median"),
                                  mean_avoided=("avoided", "mean"), med_days_to_long=("days_to_long", "median"),
                                  mean_missed=("missed", "mean")).round(2)
print("\nсводка по эпизодам:")
print(summ.to_string())
summ.to_csv(f"{RES}/H_timeliness_summary.csv")

# сохранить дневные позиции ключевых стратегий
pd.DataFrame({k: strategies[k] for k in [PROD, "M_closed/D", "M_live/D", "M_live/W", "W/W", "D/D", "W/W+exitD", "D/M+exitD"]}).to_csv(f"{RES}/H_positions_daily.csv")
print("\nготово: results/H_strategies.csv, H_sharpe_boot.csv, H_cost_sensitivity.csv, H_placebo_day_of_month.csv, H_anchor_events.csv, H_timeliness*.csv, H_positions_daily.csv")
