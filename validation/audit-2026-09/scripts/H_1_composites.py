"""H_weekly, задачи 1 и 5: композит на дневном/недельном/месячном шаге, IC по горизонтам
1–13 недель, число разворотов, корреляция версий; горизонт информации ворот; лид-лаг профиль
(затухание информации внутри месяца = прямая цена задержки).
Запуск: python scripts/H_1_composites.py  (из каталога audit/)"""
import sys
sys.path.insert(0, "scripts")
import numpy as np
import pandas as pd
from scipy import stats
from H_lib import *

D, M, C = load()
comp, native = build_composites(D, M)
print(f"контроль M_closed против panel_prod_monthly: max|diff| = {native['check_M']:.2e}")
comp.to_csv(f"{RES}/H_composites_daily.csv")

we = native["we"]
me = native["me"]
px = D["imoex"]
logpx = np.log(px)

# ------------------------------------------------------------- недельная выборка
W = pd.DataFrame(index=we)
for v in ["M_closed", "M_live", "W", "D"]:
    W[v] = comp[v].reindex(we)
for h in [1, 2, 4, 8, 13]:
    W[f"fwd{h}w"] = logpx.reindex(we).shift(-h) - logpx.reindex(we)
W = W[W.index >= "2004-01-01"]

# --- 1a. корреляция версий (недельная выборка, MAIN)
Wm = W[(W.index >= MAIN[0]) & (W.index <= MAIN[1])]
corr_p = Wm[["M_closed", "M_live", "W", "D"]].corr()
corr_s = Wm[["M_closed", "M_live", "W", "D"]].corr(method="spearman")
print("\nКорреляция версий композита (недельная выборка, 2010–2026), Пирсон:")
print(corr_p.round(3))
print("Спирмен:")
print(corr_s.round(3))
corr_p.round(4).to_csv(f"{RES}/H_corr_versions_pearson.csv")
corr_s.round(4).to_csv(f"{RES}/H_corr_versions_spearman.csv")
# согласие знаков с гистерезисом
signs = pd.DataFrame({v: hysteresis_sign(comp[v], 0.1).reindex(we) for v in ["M_closed", "M_live", "W", "D"]})
signs = signs[(signs.index >= MAIN[0]) & (signs.index <= MAIN[1])]
print("\nСогласие знаков (доля недель с одинаковым знаком, 2010–2026):")
agree = pd.DataFrame(index=signs.columns, columns=signs.columns, dtype=float)
for a in signs.columns:
    for b in signs.columns:
        m = signs[a].notna() & signs[b].notna()
        agree.loc[a, b] = (signs.loc[m, a] == signs.loc[m, b]).mean()
print(agree.round(3))
agree.round(4).to_csv(f"{RES}/H_sign_agreement.csv")

# --- 1b. число разворотов знака в год (гистерезис 0,1 и без)
rows = []
for win_name, win in [("MAIN 2010-26", MAIN), ("FULL 2004-26", FULL), ("2018-2026", SPLITS["2018-2026"])]:
    for v in ["M_closed", "M_live", "W", "D"]:
        s = comp[v]
        s = s[(s.index >= win[0]) & (s.index <= win[1])]
        yrs = len(s) / TRADING_DAYS
        for thr in [0.0, 0.1, 0.2, 0.3]:
            hs = hysteresis_sign(s, thr).dropna()
            flips = (hs != hs.shift(1)).sum() - 1
            rows.append(dict(window=win_name, version=v, hyst=thr, flips_per_year=round(flips / yrs, 2),
                             n_flips=int(flips), years=round(yrs, 1)))
flips = pd.DataFrame(rows)
flips.to_csv(f"{RES}/H_flips.csv", index=False)
print("\nРазвороты знака в год (гистерезис 0,1):")
print(flips[flips.hyst == 0.1].pivot(index="version", columns="window", values="flips_per_year"))

# --- 1c/5. IC к форварду 1/2/4/8/13 недель — недельная выборка, невырожденная + NW
rows = []
for win_name, win in [("MAIN 2010-26", MAIN), ("FULL 2004-26", FULL), ("2018-2026", SPLITS["2018-2026"]),
                      ("2010-2021", ERAS["2010-2021"]), ("ex-2022", None)]:
    if win is None:
        sub = Wm[~((Wm.index >= "2022-01-01") & (Wm.index <= "2022-12-31"))]
    else:
        sub = W[(W.index >= win[0]) & (W.index <= win[1])]
    for v in ["M_closed", "M_live", "W", "D"]:
        for h in [1, 2, 4, 8, 13]:
            r = ic_horizon(sub[v], sub[f"fwd{h}w"], h)
            rows.append(dict(window=win_name, version=v, h_weeks=h, **{k: (round(x, 3) if isinstance(x, float) else x) for k, x in r.items()}))
ic = pd.DataFrame(rows)
ic.to_csv(f"{RES}/H_ic_horizon.csv", index=False)
print("\nIC (Спирмен, перекрывающаяся недельная выборка) и NW-t, MAIN 2010–2026:")
print(ic[ic.window == "MAIN 2010-26"].pivot(index="version", columns="h_weeks", values="ic"))
print("NW-t:")
print(ic[ic.window == "MAIN 2010-26"].pivot(index="version", columns="h_weeks", values="nw_t"))
print("IC невырожденный (среднее по смещениям):")
print(ic[ic.window == "MAIN 2010-26"].pivot(index="version", columns="h_weeks", values="ic_nonovl_mean"))
print("\n2018–2026 (три ноги):")
print(ic[ic.window == "2018-2026"].pivot(index="version", columns="h_weeks", values="ic"))
print(ic[ic.window == "2018-2026"].pivot(index="version", columns="h_weeks", values="nw_t"))

# контроль: месячный IC на месячной выборке (как в валидации)
Mm = pd.DataFrame({"comp": native["M"], "fwd": M["fwd1m_log"].reindex(native["M"].index)})
for win_name, win in [("MAIN 2010-26", MAIN), ("FULL 2004-26", FULL)]:
    s = Mm[(Mm.index >= win[0]) & (Mm.index <= win[1])].dropna()
    r, p = stats.spearmanr(s["comp"], s["fwd"])
    print(f"контроль месячный IC {win_name}: {r:+.3f} p={p:.4f} n={len(s)}")

# --- 5a. лид-лаг профиль: сигнал на дату t → доходность недели k (k=1..13) ПОСЛЕ t
rows = []
for v in ["M_closed", "M_live", "W", "D"]:
    for k in range(1, 14):
        f = logpx.reindex(we).shift(-k) - logpx.reindex(we).shift(-(k - 1))
        sub = pd.concat([Wm[v], f.reindex(Wm.index).rename("f")], axis=1).dropna()
        r, p = stats.spearmanr(sub[v], sub["f"])
        t = nw_t(stats.rankdata(sub[v]), stats.rankdata(sub["f"]), 1)
        rows.append(dict(version=v, week_ahead=k, ic=round(r, 3), nw_t=round(t, 2), n=len(sub)))
ll = pd.DataFrame(rows)
ll.to_csv(f"{RES}/H_leadlag_weekly.csv", index=False)
print("\nЛид-лаг: IC сигнала к доходности k-й недели после сигнала (MAIN):")
print(ll.pivot(index="version", columns="week_ahead", values="ic"))

# то же для M_closed на МЕСЯЧНОЙ выборке: сигнал на конце месяца → доходность недели k следующего месяца
rows = []
mE = native["M"]
mE = mE[(mE.index >= MAIN[0]) & (mE.index <= MAIN[1])]
pos_in_idx = {d: i for i, d in enumerate(px.index)}
for k in range(1, 7):
    f = []
    for d in mE.index:
        i = pos_in_idx[d]
        a, b = i + 5 * (k - 1), i + 5 * k
        f.append(logpx.iloc[b] - logpx.iloc[a] if b < len(px) else np.nan)
    f = pd.Series(f, index=mE.index)
    sub = pd.concat([mE.rename("s"), f.rename("f")], axis=1).dropna()
    r, p = stats.spearmanr(sub["s"], sub["f"])
    rows.append(dict(week_after_month_end=k, ic=round(r, 3), p=round(p, 3), n=len(sub)))
ll_m = pd.DataFrame(rows)
ll_m.to_csv(f"{RES}/H_leadlag_month_end.csv", index=False)
print("\nМесячный композит на закрытии месяца → IC к 5-дневным окнам k=1..6 после закрытия (MAIN, месячная выборка):")
print(ll_m.to_string(index=False))

# --- 5b. биты ворот: информация по горизонтам (недельная выборка)
bits = pd.DataFrame({
    "trend": D["st_trend"], "vol_stress": D["st_vol"], "bond_stress": D["st_bond"],
    "toxic": (D["cell"] == TOXIC).astype(float).where(D["cell"].notna()),
    "gate_open": gate_series(D).where(D["cell"].notna()),
}).reindex(we)
rows = []
for win_name, win in [("MAIN 2010-26", MAIN), ("FULL 2004-26", FULL), ("ex-2022", None)]:
    if win is None:
        sub = W[(W.index >= MAIN[0]) & (W.index <= MAIN[1])]
        sub = sub[~((sub.index >= "2022-01-01") & (sub.index <= "2022-12-31"))]
    else:
        sub = W[(W.index >= win[0]) & (W.index <= win[1])]
    for b in bits.columns:
        for h in [1, 2, 4, 8, 13]:
            df = pd.concat([bits[b].reindex(sub.index).rename("b"), sub[f"fwd{h}w"].rename("f")], axis=1).dropna()
            on, off = df[df.b == 1]["f"], df[df.b == 0]["f"]
            if len(on) < 10 or len(off) < 10:
                continue
            diff_w = (on.mean() - off.mean()) / h  # разница средней недельной лог-доходности
            t = nw_t(df["b"].values, df["f"].values, h)
            rows.append(dict(window=win_name, bit=b, h_weeks=h, n_on=len(on), n_off=len(off),
                             mean_on_pct=round(on.mean() * 100, 2), mean_off_pct=round(off.mean() * 100, 2),
                             diff_per_week_bp=round(diff_w * 1e4, 1), nw_t=round(t, 2),
                             worst_on_pct=round(on.min() * 100, 1), worst_off_pct=round(off.min() * 100, 1)))
bt = pd.DataFrame(rows)
bt.to_csv(f"{RES}/H_bits_horizon.csv", index=False)
print("\nБиты ворот: разница средней доходности (on − off), б.п./нед, и NW-t, MAIN:")
print(bt[bt.window == "MAIN 2010-26"].pivot(index="bit", columns="h_weeks", values="diff_per_week_bp"))
print(bt[bt.window == "MAIN 2010-26"].pivot(index="bit", columns="h_weeks", values="nw_t"))
print("\nex-2022:")
print(bt[bt.window == "ex-2022"].pivot(index="bit", columns="h_weeks", values="diff_per_week_bp"))
print(bt[bt.window == "ex-2022"].pivot(index="bit", columns="h_weeks", values="nw_t"))

# --- 5c. IC ног по горизонтам (недельная выборка, D-версия z каждой ноги)
rows = []
Zd = native["Zd"].reindex(we)
for leg, sgn in LEGS:
    for h in [1, 2, 4, 8, 13]:
        r = ic_horizon(Zd[leg].reindex(Wm.index), Wm[f"fwd{h}w"], h)
        rows.append(dict(leg=leg, h_weeks=h, ic=round(r["ic"], 3), nw_t=round(r["nw_t"], 2), n=r["n"]))
lg = pd.DataFrame(rows)
lg.to_csv(f"{RES}/H_legs_horizon.csv", index=False)
print("\nНоги (знаковый дневной z, недельная выборка, MAIN): IC по горизонтам")
print(lg.pivot(index="leg", columns="h_weeks", values="ic"))
print(lg.pivot(index="leg", columns="h_weeks", values="nw_t"))
print("\nготово: results/H_composites_daily.csv, H_ic_horizon.csv, H_flips.csv, H_leadlag_*.csv, H_bits_horizon.csv, H_legs_horizon.csv")
