"""D2_02: сюрпризы ЦБ (решение − консенсус) — event study с плацебо (случайные даты той же эры),
ex-2022, дедуп; затем НАЛОЖЕНИЕ на правило панели: после голубиного сюрприза принудительный лонг
на 1/2/3 мес, после ястребиного — принудительный флэт; метрики брифа на дневном шаге."""
import numpy as np
import pandas as pd
from D2_lib import *

d = derive_rates(load_daily())
m = monthly_frame(d)
pos_m = baseline_position(m)

dec = pd.read_csv(DATA / "cb_decisions.csv", parse_dates=["date"]).sort_values("date")
dec["surprise_bp"] = (dec["new_rate"] - dec["consensus"]) * 100
dec["step_bp"] = (dec["new_rate"] - dec["prev_rate"]) * 100
dec["scheduled"] = dec["consensus"].notna()
dec["kind"] = np.where(dec["surprise"] == "dovish", "dovish",
                       np.where(dec["surprise"] == "hawkish", "hawkish", "inline"))
dec["inline_cut"] = (dec["kind"] == "inline") & (dec["step_bp"] < 0)
dec["inline_hike"] = (dec["kind"] == "inline") & (dec["step_bp"] > 0)
dec["inline_hold"] = (dec["kind"] == "inline") & (dec["step_bp"] == 0)
# цена ожиданий накануне (t−1): y1−key, rusfar−key
prev = d[["y1_key", "rusfar_key", "y05_key"]].shift(1)
dec = dec.merge(prev, left_on="date", right_index=True, how="left")

ret = np.log(d["imoex"]).diff().dropna()
ret_tr = np.log(d["mcftr_ffill"]).diff().dropna()
idx = ret.index
posmap = {t: i for i, t in enumerate(idx)}


def car(ev_dates, a, b, r=ret, aligned=False):
    """CAR по событиям; aligned=True — NaN вместо пропуска (сохраняет длину списка событий)."""
    ra = r.values
    out = []
    for t in ev_dates:
        later = idx[idx >= t]
        if len(later) == 0 or posmap[later[0]] + a < 0 or posmap[later[0]] + b + 1 > len(ra):
            if aligned: out.append(np.nan)
            continue
        e = posmap[later[0]]
        out.append(ra[e + a:e + b + 1].sum())
    return np.array(out)


def placebo_p(ev_dates, a, b, era=("2014-01-01", "2026-08-31"), n=4000, seed=11, r=ret):
    obs = car(ev_dates, a, b, r)
    if len(obs) < 3:
        return np.nan, np.nan, len(obs)
    rng = np.random.default_rng(seed)
    pool = np.where((idx >= era[0]) & (idx <= era[1]))[0]
    pool = pool[(pool + a >= 0) & (pool + b + 1 <= len(idx))]
    ra = r.values
    cs = np.cumsum(np.insert(ra, 0, 0.0))
    pl = np.empty(n)
    for i in range(n):
        e = rng.choice(pool, size=len(obs), replace=True)
        pl[i] = (cs[e + b + 1] - cs[e + a]).mean()
    om = obs.mean()
    p = (np.abs(pl - pl.mean()) >= abs(om - pl.mean())).mean()
    return om, p, len(obs)


WINDOWS = [(0, 0), (1, 5), (0, 5), (0, 21), (0, 42), (-5, -1)]
groups = {
    "dovish_all": dec[dec.kind == "dovish"],
    "dovish_scheduled": dec[(dec.kind == "dovish") & dec.scheduled],
    "hawkish_all": dec[dec.kind == "hawkish"],
    "hawkish_scheduled": dec[(dec.kind == "hawkish") & dec.scheduled],
    "inline_all": dec[dec.kind == "inline"],
    "inline_cut": dec[dec.inline_cut],
    "inline_hike": dec[dec.inline_hike],
    "inline_hold": dec[dec.inline_hold],
    "dovish_ex2022": dec[(dec.kind == "dovish") & (dec.date.dt.year != 2022)],
    "hawkish_ex2022": dec[(dec.kind == "hawkish") & (dec.date.dt.year != 2022) & dec.scheduled],
    "dovish_ex2022_exDec2024": dec[(dec.kind == "dovish") & (dec.date.dt.year != 2022) & (dec.date != "2024-12-20")],
    "dovish_post2022": dec[(dec.kind == "dovish") & (dec.date >= "2022-03-24")],
    "hawkish_post2022": dec[(dec.kind == "hawkish") & (dec.date >= "2022-03-24") & dec.scheduled],
    "inline_cut_post2022": dec[dec.inline_cut & (dec.date >= "2022-03-24")],
    "cut_when_rusfar_above_key": dec[(dec.step_bp < 0) & (dec.rusfar_key > 0)],
    "cut_when_y1_below_key_by_100": dec[(dec.step_bp < 0) & (dec.y1_key < -1.0)],
    "hold_when_y1_below_key_by_100": dec[(dec.step_bp == 0) & (dec.y1_key < -1.0)],
}
rows = []
for g, sub in groups.items():
    for (a, b) in WINDOWS:
        om, p, n = placebo_p(sub["date"].values, a, b)
        rows.append(dict(group=g, window=f"[{a},{b}]", n=n, car_pct=round(om * 100, 2) if om == om else np.nan,
                         p_placebo=round(p, 3) if p == p else np.nan))
ev = pd.DataFrame(rows)
ev.to_csv(RES / "D2_02_events_car.csv", index=False)
print("=== CAR IMOEX вокруг решений ЦБ (плацебо: случайные даты 2014–2026) ===")
print(ev.pivot_table(index="group", columns="window", values="car_pct").to_string())
print(ev.pivot_table(index="group", columns="window", values="p_placebo").to_string())
print(ev.groupby("group")["n"].first().to_string())

# --- сюрприз как НЕПРЕРЫВНАЯ переменная: регрессия CAR на surprise_bp (плановые заседания)
sch = dec[dec.scheduled].copy()
for (a, b) in [(0, 0), (0, 5), (0, 21), (0, 42)]:
    c = car(sch["date"].values, a, b, aligned=True)
    x = sch["surprise_bp"].values
    ok = np.isfinite(c) & np.isfinite(x)
    if ok.sum() > 10:
        sl, ic_, r, p, se = stats.linregress(x[ok], c[ok])
        rho, prho = stats.spearmanr(x[ok], c[ok])
        print(f"CAR[{a},{b}] ~ surprise_bp: n={ok.sum()} beta={sl*100*100:.3f}%/100бп p={p:.3f} | Спирмен {rho:.3f} p={prho:.3f}")
        okx = ok & (sch["date"].dt.year.values != 2022)
        sl, ic_, r, p, se = stats.linregress(x[okx], c[okx]); rho, prho = stats.spearmanr(x[okx], c[okx])
        print(f"   ex-2022: n={okx.sum()} beta={sl*100*100:.3f}%/100бп p={p:.3f} | Спирмен {rho:.3f} p={prho:.3f}")

# --- список сюрпризов с CAR (для отчёта)
lst = dec[dec.kind != "inline"].copy()
for (a, b) in [(0, 0), (0, 5), (0, 21), (0, 42)]:
    vals = []
    for t in lst["date"]:
        c = car([t], a, b); vals.append(c[0] * 100 if len(c) else np.nan)
    lst[f"car{a}_{b}"] = np.round(vals, 2)
lst[["date", "new_rate", "prev_rate", "consensus", "surprise_bp", "kind", "scheduled", "y1_key", "rusfar_key",
     "car0_0", "car0_5", "car0_21", "car0_42"]].to_csv(RES / "D2_02_surprise_list.csv", index=False)
print(lst[["date", "surprise_bp", "kind", "scheduled", "y1_key", "rusfar_key", "car0_0", "car0_5", "car0_21", "car0_42"]].to_string())

# --- НАЛОЖЕНИЕ на правило панели (дневной шаг): позиция на закрытии t → доходность t+1
def daily_bt(pos_d, start, end, cost=COST):
    r_long = ret_tr.reindex(d.index)
    r_cash = (d["mm_rate"] / 100 / 252)
    df = pd.DataFrame({"pos": pos_d.shift(1), "r_long": r_long, "r_cash": r_cash}).dropna()
    df = df[(df.index >= start) & (df.index <= end)]
    df["trade"] = df["pos"].diff().abs().fillna(0.0)
    df["ret"] = df["pos"] * df["r_long"] + (1 - df["pos"]) * df["r_cash"] - df["trade"] * cost
    return df


base_d = monthly_to_daily_pos(pos_m, d.index)


def overlay(base, events, months, force_long):
    p = base.copy()
    for t in events:
        later = d.index[d.index >= t]
        if len(later) == 0: continue
        i = d.index.get_loc(later[0])
        j = min(len(d.index), i + 21 * months + 1)
        p.iloc[i:j] = 1.0 if force_long else 0.0
    return p


rows = []
for start, end, lbl in [("2015-01-01", "2026-08-31", "2015_2026"), ("2022-03-24", "2026-08-31", "post2022"),
                        ("2015-01-01", "2021-12-31", "2015_2021")]:
    for nm, p in [("baseline_daily", base_d)]:
        mt = metrics(daily_bt(p, start, end), freq=252, name=f"{nm}|{lbl}"); mt["window"] = lbl; rows.append(mt)
    for months in (1, 2, 3):
        dv = dec[(dec.kind == "dovish") & dec.scheduled]["date"].values
        hk = dec[(dec.kind == "hawkish") & dec.scheduled]["date"].values
        ic_ = dec[dec.inline_cut]["date"].values
        for nm, p in [(f"dovish_long_{months}m", overlay(base_d, dv, months, True)),
                      (f"hawkish_flat_{months}m", overlay(base_d, hk, months, False)),
                      (f"both_{months}m", overlay(overlay(base_d, dv, months, True), hk, months, False)),
                      (f"inline_cut_flat_{months}m", overlay(base_d, ic_, months, False))]:
            mt = metrics(daily_bt(p, start, end), freq=252, name=f"{nm}|{lbl}"); mt["window"] = lbl; rows.append(mt)
tab = fmt_metrics_table(rows)
tab.to_csv(RES / "D2_02_overlay_metrics.csv", index=False)
print("\n=== наложение сюрпризов на правило панели (дневной шаг, MCFTR + mm_rate, издержки 0,2%) ===")
print(tab[["name", "cagr", "vol", "sharpe", "sharpe_excess", "maxdd", "time_in_mkt", "trades_per_yr", "bh_cagr", "bh_sharpe"]].to_string())
