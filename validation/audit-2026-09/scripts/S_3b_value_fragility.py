"""S_3b — хрупкость находки 3: гистерезис, окно z, причина расхождения реплики ноги."""
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

urals = load_raw("urals_tax")
per = urals.index.to_period("M").to_timestamp("M")
ur = pd.Series(urals.values, index=per).sort_index()
ur = ur[~ur.index.duplicated()]

# --- причина расхождения 1e-2: варианты среднемесячного курса
raw_usd = load_raw("usd_cbr")
v1 = d["usd"].groupby(idx.to_period("M")).mean()                              # торговые дни, ffill (S_3)
v2 = raw_usd.groupby(raw_usd.index.to_period("M")).mean()                     # все даты ЦБ (календарные)
ex = raw_usd.reindex(idx)                                                     # только точные значения на торговых днях
v3 = ex.groupby(idx.to_period("M")).mean()
v4 = raw_usd.resample("D").ffill().groupby(lambda t: t.to_period("M")).mean()  # календарные дни с протяжкой
for nm, v in [("торговые дни (ffill)", v1), ("даты ЦБ как есть", v2), ("торговые дни, только точные", v3), ("все календарные дни, ffill", v4)]:
    vv = v.copy(); vv.index = vv.index.to_timestamp("M")
    rb = ur * vv.reindex(ur.index)
    gap = np.log(rb / rb.rolling(24, min_periods=12).mean())
    gap.index = gap.index + pd.Timedelta(days=5)
    s = pd.Series(np.nan, index=idx)
    for dt, val in gap.dropna().items():
        p = idx.searchsorted(dt)
        if p < len(idx):
            s.iloc[p] = val
    s = s.ffill(limit=66)
    chk = pd.concat([s[me], m["raw_urals_rub_gap"].reindex(me_dates)], axis=1, keys=["mine", "prod"]).dropna()
    print("вариант курса %-32s max|diff| = %.2e" % (nm, (chk["mine"] - chk["prod"]).abs().max()))

# --- гистерезис × окно z для трёх ядер
brent = load_raw("brent").reindex(idx).ffill(limit=10)
br_m = brent.groupby(idx.to_period("M")).mean(); br_m.index = br_m.index.to_timestamp("M")
br_m = br_m[br_m.index >= "2015-01-31"]
usd_m = d["usd"].groupby(idx.to_period("M")).mean(); usd_m.index = usd_m.index.to_timestamp("M")


def align(series_m, lag_days=5, limit=66):
    a = series_m.copy(); a.index = a.index + pd.Timedelta(days=lag_days)
    s = pd.Series(np.nan, index=idx)
    for dt, v in a.dropna().items():
        p = idx.searchsorted(dt)
        if p < len(idx):
            s.iloc[p] = v
    return s.ffill(limit=limit)


def gap24(x, w=24):
    return np.log(x / x.rolling(w, min_periods=w // 2).mean())


legs_daily = {
    "прод ₽": align(gap24(ur * usd_m.reindex(ur.index))),
    "Urals $": align(gap24(ur)),
    "Brent $": align(gap24(br_m)),
}
tox_me = d.loc[me, "toxic"]


def comp_of(third, zwin=60, zmin=24):
    L = pd.DataFrame({"usd_mom63": d["usd_mom63"], "slope_10_2": d["slope_10_2"], "oil": third}).groupby(idx.to_period("M")).last()
    L.index = me_dates
    c, _ = composite_monthly(L, {"usd_mom63": 1, "slope_10_2": 1, "oil": -1}, window=zwin, min_periods=zmin)
    return c


def sh_of(comp, hyst, a="2010-01-01", b="2026-08-31"):
    sgn = hysteresis_sign(comp, hyst)
    dec = ((sgn > 0) & (tox_me == 0)).astype(float)
    dec[tox_me.isna() | (sgn == 0)] = np.nan
    pos = decision_to_daily(dec, idx)
    bt = run_daily(pos, d, start=a, end=b)
    mt = metrics(bt)
    mr = monthly_returns(bt)
    return mt["sharpe"], sharpe(mr.loc[mr.index.year != 2022, "strat"]), mt["cagr"], mt["mdd_daily"], pos


print("\n=== ГИСТЕРЕЗИС (окно z 60): Шарп 2010–26 / ex-2022 ===")
rows = []
for h in [0.0, 0.05, 0.08, 0.10, 0.12, 0.15, 0.20, 0.30]:
    r = dict(hyst=h)
    poss = {}
    for nm, leg in legs_daily.items():
        c = comp_of(leg)
        s, s22, cg, md, pos = sh_of(c, h)
        r[f"sh_{nm}"] = s; r[f"ex22_{nm}"] = s22
        poss[nm] = pos
    # позиция 2022-08-31 у долларового ядра
    r["usd_pos_2022-08"] = float(poss["Urals $"].loc["2022-08-31"])
    r["prod_pos_2022-08"] = float(poss["прод ₽"].loc["2022-08-31"])
    rows.append(r)
hs = pd.DataFrame(rows)
hs.to_csv("results/S_3b_hysteresis.csv", index=False)
print(hs.to_string(index=False, float_format=lambda v: f"{v:.2f}"))

print("\n=== ОКНО z (гистерезис 0,10): Шарп 2010–26 / ex-2022 ===")
rows = []
for zw, zm in [(36, 18), (48, 24), (60, 24), (72, 36), (84, 36)]:
    r = dict(zwin=zw)
    for nm, leg in legs_daily.items():
        s, s22, cg, md, pos = sh_of(comp_of(leg, zw, zm), 0.10)
        r[f"sh_{nm}"] = s; r[f"ex22_{nm}"] = s22
    rows.append(r)
zs = pd.DataFrame(rows)
zs.to_csv("results/S_3b_zwindow.csv", index=False)
print(zs.to_string(index=False, float_format=lambda v: f"{v:.2f}"))

print("\n=== ОКНО MA бочки (12/18/24/36 мес), гистерезис 0,10 ===")
rows = []
for w in [12, 18, 24, 36]:
    r = dict(ma=w)
    for nm, src in [("прод ₽", ur * usd_m.reindex(ur.index)), ("Urals $", ur), ("Brent $", br_m)]:
        s, s22, cg, md, pos = sh_of(comp_of(align(gap24(src, w))), 0.10)
        r[f"sh_{nm}"] = s; r[f"ex22_{nm}"] = s22
    rows.append(r)
ms = pd.DataFrame(rows)
ms.to_csv("results/S_3b_mawindow.csv", index=False)
print(ms.to_string(index=False, float_format=lambda v: f"{v:.2f}"))

# значение долларового композита на 2022-08-31 при разных окнах
print("\nкомпозит на 2022-08-31 (порог гистерезиса 0,10; знак до этого −1):")
for zw, zm in [(36, 18), (48, 24), (60, 24), (72, 36)]:
    print("  окно z %d: прод %+.3f, Urals $ %+.3f, Brent $ %+.3f" % (zw, comp_of(legs_daily["прод ₽"], zw, zm).loc["2022-08-31"],
                                                                     comp_of(legs_daily["Urals $"], zw, zm).loc["2022-08-31"],
                                                                     comp_of(legs_daily["Brent $"], zw, zm).loc["2022-08-31"]))
