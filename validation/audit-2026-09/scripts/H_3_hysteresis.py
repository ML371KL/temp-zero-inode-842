"""H_weekly, задача 3: гистерезис знака и минимальное удержание как замена месячного якоря.
Сетка: порог {0, 0,1, 0,2, 0,3, 0,5} × удержание {1, 5, 10, 21, 42} торг. дней на дневном шаге
(композит D и M_live) и недельном (композит W, пятница); плюс «подтверждение» N дней подряд.
Split 2004–2017 / 2018–2026 и MAIN/FULL. Всего вариантов — печатается (плата за перебор).
Запуск: python scripts/H_3_hysteresis.py"""
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


def confirm(sig, n):
    """Сигнал переключается, только если новое значение держится n дней подряд."""
    if n <= 1:
        return sig
    v = sig.values
    out = np.full(len(v), np.nan)
    cur = np.nan
    run, run_val = 0, np.nan
    for i, s in enumerate(v):
        if not np.isfinite(s):
            out[i] = cur
            continue
        if s == run_val:
            run += 1
        else:
            run_val, run = s, 1
        if np.isnan(cur) or (s != cur and run >= n):
            cur = s
        out[i] = cur
    return pd.Series(out, index=sig.index)


wins = {"MAIN 2010-26": MAIN, "FULL 2004-26": FULL, "2004-2017": SPLITS["2004-2017"], "2018-2026": SPLITS["2018-2026"],
        "ex-2022": None}
rows = []
n_variants = 0
THR = [0.0, 0.1, 0.2, 0.3, 0.5]
HOLD = [1, 5, 10, 21, 42]
CONF = [1, 3, 5, 10]
for step, version in [("D", "D"), ("D", "M_live"), ("W", "W"), ("W", "D")]:
    for thr in THR:
        sgn = hysteresis_sign(comp[version], thr)
        sig = ((sgn > 0) & (gate > 0)).astype(float)
        for hold in HOLD:
            for conf in CONF:
                if conf > 1 and (hold > 1 or step == "W"):
                    continue  # подтверждение тестируем отдельно от удержания, только на дневном шаге
                pos = positions(confirm(sig, conf), dec[step], idx, min_hold=hold)
                bt = backtest_daily(pos, mk)
                n_variants += 1
                for wn, win in wins.items():
                    d = slice_ex2022(bt) if win is None else slice_win(bt, win)
                    mt = metrics(d)
                    rows.append(dict(step=step, version=version, thr=thr, hold=hold, confirm=conf, window=wn,
                                     sharpe=round(mt["sharpe"], 3), sharpe_ex=round(mt["sharpe_ex"], 3), cagr=round(mt["cagr"], 4),
                                     maxdd=round(mt["maxdd"], 4), trades_yr=round(mt["trades_yr"], 2), time_in=round(mt["time_in"], 3)))
# те же пороги на месячном шаге (прод) для сравнения
for thr in THR:
    sgn = hysteresis_sign(comp["M_closed"], thr)
    sig = ((sgn > 0) & (gate > 0)).astype(float)
    bt = backtest_daily(positions(sig, dec["M"], idx), mk)
    n_variants += 1
    for wn, win in wins.items():
        d = slice_ex2022(bt) if win is None else slice_win(bt, win)
        mt = metrics(d)
        rows.append(dict(step="M", version="M_closed", thr=thr, hold=1, confirm=1, window=wn,
                         sharpe=round(mt["sharpe"], 3), sharpe_ex=round(mt["sharpe_ex"], 3), cagr=round(mt["cagr"], 4),
                         maxdd=round(mt["maxdd"], 4), trades_yr=round(mt["trades_yr"], 2), time_in=round(mt["time_in"], 3)))
res = pd.DataFrame(rows)
res.to_csv(f"{RES}/H_hysteresis.csv", index=False)
print(f"вариантов перебрано: {n_variants}")

for step, version in [("D", "D"), ("D", "M_live"), ("W", "W"), ("W", "D")]:
    for wn in ["MAIN 2010-26", "FULL 2004-26", "2004-2017", "2018-2026"]:
        sub = res[(res.step == step) & (res.version == version) & (res.window == wn) & (res.confirm == 1)]
        print(f"\n=== шаг {step}, композит {version}, {wn}: Шарп (строки — порог, столбцы — удержание дн) ===")
        print(sub.pivot(index="thr", columns="hold", values="sharpe").to_string())
        print("переключений/год:")
        print(sub.pivot(index="thr", columns="hold", values="trades_yr").to_string())
        print("MDD:")
        print(sub.pivot(index="thr", columns="hold", values="maxdd").to_string())

print("\n=== подтверждение N дней подряд (дневной шаг, удержание 1) ===")
for version in ["D", "M_live"]:
    for wn in ["MAIN 2010-26", "FULL 2004-26", "2004-2017", "2018-2026"]:
        sub = res[(res.step == "D") & (res.version == version) & (res.window == wn) & (res.hold == 1)]
        print(f"\n{version}, {wn}: Шарп (строки — порог, столбцы — подтверждение дн)")
        print(sub.pivot(index="thr", columns="confirm", values="sharpe").to_string())
        print("переключений/год:")
        print(sub.pivot(index="thr", columns="confirm", values="trades_yr").to_string())

print("\n=== месячный шаг (прод) при разных порогах ===")
print(res[(res.step == "M")].pivot(index="thr", columns="window", values="sharpe").to_string())

# устойчивость: ранг варианта в обеих половинах
print("\n=== устойчивость: варианты, входящие в верхнюю треть по Шарпу и в 2004–2017, и в 2018–2026 ===")
a = res[(res.window == "2004-2017")].set_index(["step", "version", "thr", "hold", "confirm"]).sharpe
b = res[(res.window == "2018-2026")].set_index(["step", "version", "thr", "hold", "confirm"]).sharpe
m = res[(res.window == "MAIN 2010-26")].set_index(["step", "version", "thr", "hold", "confirm"])
j = pd.DataFrame({"sh_0417": a, "sh_1826": b, "sh_main": m.sharpe, "tr_main": m.trades_yr, "mdd_main": m.maxdd})
j["rank_0417"] = j.sh_0417.rank(ascending=False)
j["rank_1826"] = j.sh_1826.rank(ascending=False)
j["min_sh"] = j[["sh_0417", "sh_1826"]].min(axis=1)
top = j.sort_values("min_sh", ascending=False).head(15)
print(top.round(3).to_string())
j.round(3).to_csv(f"{RES}/H_hysteresis_split.csv")
print("\nготово: results/H_hysteresis.csv, H_hysteresis_split.csv")
