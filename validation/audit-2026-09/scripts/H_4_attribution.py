"""H_weekly, задача 4: откуда берётся выигрыш/проигрыш недельной и дневной частоты —
от ворот (быстрые биты trend/vol/bond) или от композита (быстрые ноги usd_mom63, slope против
медленной Urals). Атрибуция каждого переключения по триггеру и его ценность до следующего
переключения; скорость входов; плацебо «день недели»; собственные просадки стратегий.
Запуск: python scripts/H_4_attribution.py"""
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
cost = 0.002
ex = (mk["r_tr"] - mk["r_mm"]).fillna(0)

# знаковые z ног для дневной версии (для атрибуции разворотов композита)
_, native = build_composites(D, M)
Zd = native["Zd"]  # знаковые z (D-версия)
Zw = native["Zw"].reindex(idx).ffill()
bits = D[["st_trend", "st_vol", "st_bond"]]


def attribute(pos, sgn, step, Z, look):
    """Для каждого переключения: триггер (ворота: какой бит; композит: какая нога) и ценность
    переключения до следующего (сумма избытка лонга над флэтом с нужным знаком минус издержка)."""
    p = pos.values
    ch = np.where(p[1:] != p[:-1])[0] + 1
    rows = []
    g = gate.values
    s = sgn.values
    for n, i in enumerate(ch):
        t = idx[i]
        j = ch[n + 1] if n + 1 < len(ch) else len(idx) - 1
        # ценность: позиция действует с t (на доходности t+1..j), сравниваем с «не переключаться»
        seg = ex.iloc[i + 1:j + 1].sum()
        value = (seg if p[i] > p[i - 1] else -seg) - cost
        # что изменилось за look дней до решения
        i0 = max(0, i - look)
        gate_changed = g[i] != g[i0]
        sign_changed = (s[i] != s[i0]) if np.isfinite(s[i]) and np.isfinite(s[i0]) else False
        if gate_changed and not sign_changed:
            trig = "ворота"
        elif sign_changed and not gate_changed:
            trig = "композит"
        elif gate_changed and sign_changed:
            trig = "оба"
        else:
            trig = "неопределён"
        bit = ""
        if gate_changed:
            chg = [c for c in bits.columns if bits[c].iloc[i] != bits[c].iloc[i0]]
            bit = "+".join(x.replace("st_", "") for x in chg)
        leg = ""
        if sign_changed:
            dz = (Z.iloc[i] - Z.iloc[i0]).dropna()
            if len(dz):
                leg = dz.abs().idxmax()
        rows.append(dict(date=t.date(), to=("лонг" if p[i] > p[i - 1] else "флэт"), trigger=trig, bit=bit, leg=leg,
                         days_held=j - i, value_pct=round(value * 100, 2)))
    return pd.DataFrame(rows)


configs = {
    "D/D": (positions(((hysteresis_sign(comp["D"], 0.1) > 0) & (gate > 0)).astype(float), dec["D"], idx), hysteresis_sign(comp["D"], 0.1), "D", Zd, 1),
    "M_live/D": (positions(((hysteresis_sign(comp["M_live"], 0.1) > 0) & (gate > 0)).astype(float), dec["D"], idx), hysteresis_sign(comp["M_live"], 0.1), "D", Zd, 1),
    "W/W": (positions(((hysteresis_sign(comp["W"], 0.1) > 0) & (gate > 0)).astype(float), dec["W"], idx), hysteresis_sign(comp["W"], 0.1), "W", Zw, 5),
    "M_closed/M": (positions(((hysteresis_sign(comp["M_closed"], 0.1) > 0) & (gate > 0)).astype(float), dec["M"], idx), hysteresis_sign(comp["M_closed"], 0.1), "M", Zd, 21),
}
all_rows = []
for name, (pos, sgn, step, Z, look) in configs.items():
    a = attribute(pos, sgn, step, Z, look)
    a.insert(0, "strategy", name)
    all_rows.append(a)
att = pd.concat(all_rows)
att.to_csv(f"{RES}/H_switch_attribution.csv", index=False)
for wn, lo in [("MAIN 2010-26", "2010-01-01"), ("FULL 2004-26", "2004-01-01")]:
    print(f"\n=== атрибуция переключений, {wn} ===")
    sub = att[(pd.to_datetime(att.date) >= lo) & (pd.to_datetime(att.date) <= "2026-08-31")]
    g = sub.groupby(["strategy", "trigger"]).agg(n=("value_pct", "size"), total_pct=("value_pct", "sum"),
                                                  mean_pct=("value_pct", "mean"), hit=("value_pct", lambda x: (x > 0).mean()),
                                                  med_days=("days_held", "median")).round(2)
    print(g.to_string())
    print("\nпо направлению:")
    g2 = sub.groupby(["strategy", "to", "trigger"]).agg(n=("value_pct", "size"), total_pct=("value_pct", "sum"),
                                                        hit=("value_pct", lambda x: (x > 0).mean())).round(2)
    print(g2.to_string())
    print("\nворота: какой бит переключил:")
    print(sub[sub.trigger.isin(["ворота", "оба"])].groupby(["strategy", "bit"]).agg(n=("value_pct", "size"), total_pct=("value_pct", "sum")).round(2).to_string())
    print("\nкомпозит: какая нога:")
    print(sub[sub.trigger.isin(["композит", "оба"])].groupby(["strategy", "leg"]).agg(n=("value_pct", "size"), total_pct=("value_pct", "sum")).round(2).to_string())

# ---------------------------------------------------------------- скорость входов
print("\n=== скорость входов (2010–2026): смен состояния в год, автокорреляция знакового z ===")
rows = []
sub = slice(pd.Timestamp("2010-01-01"), pd.Timestamp("2026-08-31"))
yrs = len(idx[(idx >= "2010-01-01") & (idx <= "2026-08-31")]) / 252
for c in ["st_trend", "st_vol", "st_bond"]:
    s = D[c].loc[sub].dropna()
    rows.append(dict(input=c, kind="бит", changes_per_year=round((s != s.shift(1)).sum() / yrs, 2), ac5=np.nan, ac21=np.nan, ac63=np.nan))
g = gate.loc[sub]
rows.append(dict(input="ворота (ячейка≠токсичная)", kind="бит", changes_per_year=round((g != g.shift(1)).sum() / yrs, 2), ac5=np.nan, ac21=np.nan, ac63=np.nan))
for k, _ in LEGS:
    z = Zd[k].loc[sub].dropna()
    rows.append(dict(input=k, kind="нога (z дневной)", changes_per_year=round((z.diff() != 0).sum() / yrs, 1),
                     ac5=round(z.autocorr(5), 3), ac21=round(z.autocorr(21), 3), ac63=round(z.autocorr(63), 3)))
for v in ["D", "W", "M_live", "M_closed"]:
    z = comp[v].loc[sub].dropna()
    rows.append(dict(input=f"композит {v}", kind="композит", changes_per_year=round((z.diff() != 0).sum() / yrs, 1),
                     ac5=round(z.autocorr(5), 3), ac21=round(z.autocorr(21), 3), ac63=round(z.autocorr(63), 3)))
sp = pd.DataFrame(rows)
sp.to_csv(f"{RES}/H_input_speed.csv", index=False)
print(sp.to_string(index=False))

# ---------------------------------------------------------------- плацебо: день недели для недельного решения
print("\n=== плацебо: недельное решение в разные дни недели (композит D + ворота, читаются в день решения) ===")
rows = []
sigD = ((hysteresis_sign(comp["D"], 0.1) > 0) & (gate > 0)).astype(float)
sigW = ((hysteresis_sign(comp["W"], 0.1) > 0) & (gate > 0)).astype(float)
for wd, wname in enumerate(["пн", "вт", "ср", "чт", "пт"]):
    # последний торговый день недели не позднее данного дня недели: берём дни с этим weekday
    decd = (idx.weekday == wd)
    for sname, sig in [("D", sigD), ("W", sigW)]:
        pos = positions(sig, decd, idx)
        bt = backtest_daily(pos, mk)
        for wn, win in [("MAIN 2010-26", MAIN), ("FULL 2004-26", FULL), ("2004-2017", SPLITS["2004-2017"]), ("2018-2026", SPLITS["2018-2026"])]:
            mt = metrics(slice_win(bt, win))
            rows.append(dict(weekday=wname, composite=sname, window=wn, sharpe=round(mt["sharpe"], 3), cagr=round(mt["cagr"], 4),
                             maxdd=round(mt["maxdd"], 4), trades_yr=round(mt["trades_yr"], 2)))
wdp = pd.DataFrame(rows)
wdp.to_csv(f"{RES}/H_placebo_weekday.csv", index=False)
print(wdp.pivot_table(index=["composite", "weekday"], columns="window", values="sharpe").round(3).to_string())

# ---------------------------------------------------------------- собственные просадки стратегий
print("\n=== три худшие просадки стратегий (2010–2026) ===")
rows = []
for name, (pos, *_ ) in configs.items():
    bt = slice_win(backtest_daily(pos, mk), MAIN)
    cum = (1 + bt["ret"]).cumprod()
    eps = drawdown_episodes(cum, 0.10)
    eps = sorted(eps, key=lambda e: e[3])[:3]
    for pk, tr, rc, depth in eps:
        rows.append(dict(strategy=name, peak=pk.date(), trough=tr.date(), recov=(rc.date() if rc is not None else None), depth=round(depth, 3)))
own = pd.DataFrame(rows)
own.to_csv(f"{RES}/H_own_drawdowns.csv", index=False)
print(own.to_string(index=False))
print("\nготово: results/H_switch_attribution.csv, H_input_speed.csv, H_placebo_weekday.csv, H_own_drawdowns.csv")
