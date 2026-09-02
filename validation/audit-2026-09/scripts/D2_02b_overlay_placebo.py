"""D2_02b: плацебо и разложение для наложения «флэт на K мес после заседания ЦБ» поверх правила панели.
Плацебо (а) случайные торговые дни того же числа; (б) случайные ЗАСЕДАНИЯ того же числа (проверка:
это про снижения или про заседания вообще); split 2015–2019 / 2020–2026; ex-2020Q1, ex-2022; вклад
каждого события в разность доходностей."""
import numpy as np
import pandas as pd
from D2_lib import *

d = derive_rates(load_daily())
m = monthly_frame(d)
pos_m = baseline_position(m)
base_d = monthly_to_daily_pos(pos_m, d.index)
dec = pd.read_csv(DATA / "cb_decisions.csv", parse_dates=["date"]).sort_values("date")
dec["step_bp"] = (dec["new_rate"] - dec["prev_rate"]) * 100
dec["kind"] = np.where(dec["surprise"] == "dovish", "dovish", np.where(dec["surprise"] == "hawkish", "hawkish", "inline"))
dec["scheduled"] = dec["consensus"].notna()
prev = d[["y1_key", "rusfar_key"]].shift(1)
dec = dec.merge(prev, left_on="date", right_index=True, how="left")
r_long = np.log(d["mcftr_ffill"]).diff()
r_cash = d["mm_rate"] / 100 / 252
idx = d.index


def bt(pos, start, end, cost=COST):
    df = pd.DataFrame({"pos": pos.shift(1), "r_long": r_long, "r_cash": r_cash}).dropna()
    df = df[(df.index >= start) & (df.index <= end)]
    df["trade"] = df["pos"].diff().abs().fillna(0.0)
    df["ret"] = df["pos"] * df["r_long"] + (1 - df["pos"]) * df["r_cash"] - df["trade"] * cost
    return df


def overlay(base, events, months, force_long=False):
    p = np.array(base.values, dtype=float, copy=True)
    for t in events:
        later = idx[idx >= t]
        if len(later) == 0: continue
        i = idx.get_loc(later[0]); j = min(len(idx), i + 21 * months + 1)
        p[i:j] = 1.0 if force_long else 0.0
    return pd.Series(p, index=idx)


EVENTS = {
    "inline_cut": dec[(dec.kind == "inline") & (dec.step_bp < 0)]["date"].values,
    "any_cut": dec[dec.step_bp < 0]["date"].values,
    "any_scheduled_meeting": dec[dec.scheduled]["date"].values,
    "inline_hold": dec[(dec.kind == "inline") & (dec.step_bp == 0)]["date"].values,
    "inline_hike": dec[(dec.kind == "inline") & (dec.step_bp > 0)]["date"].values,
    "hawkish_scheduled": dec[(dec.kind == "hawkish") & dec.scheduled]["date"].values,
    "hold_when_y1key_lt_m1": dec[(dec.step_bp == 0) & (dec.y1_key < -1.0)]["date"].values,
    "cut_or_hold_when_y1key_lt_m1": dec[(dec.step_bp <= 0) & (dec.y1_key < -1.0)]["date"].values,
}
WINDOWS = {"2015_2026": ("2015-01-01", "2026-08-31"), "2015_2019": ("2015-01-01", "2019-12-31"),
           "2020_2026": ("2020-01-01", "2026-08-31"), "2022_2026": ("2022-03-24", "2026-08-31")}
rng = np.random.default_rng(21)
meet_dates = dec["date"].values
rows = []
for wn, (a, b) in WINDOWS.items():
    bdf = bt(base_d, a, b); bm = metrics(bdf, freq=252)
    rows.append(dict(window=wn, events="baseline", K=0, n_ev=0, cagr=bm["cagr"], sharpe=bm["sharpe"], maxdd=bm["maxdd"], tim=bm["time_in_mkt"]))
    for ev_name, ev in EVENTS.items():
        ev_in = ev[(ev >= np.datetime64(a)) & (ev <= np.datetime64(b))]
        for K in (1, 2, 3):
            p = overlay(base_d, ev_in, K)
            mt = metrics(bt(p, a, b), freq=252)
            # плацебо: (а) случайные торговые дни окна; (б) случайные заседания окна
            days = idx[(idx >= a) & (idx <= b)]
            mi = meet_dates[(meet_dates >= np.datetime64(a)) & (meet_dates <= np.datetime64(b))]
            pa, pb = [], []
            for k in range(300):
                rd = rng.choice(days.values, size=len(ev_in), replace=False)
                pa.append(metrics(bt(overlay(base_d, rd, K), a, b), freq=252)["sharpe"])
                if len(mi) >= len(ev_in):
                    rm = rng.choice(mi, size=len(ev_in), replace=False)
                    pb.append(metrics(bt(overlay(base_d, rm, K), a, b), freq=252)["sharpe"])
            pa = np.array(pa); pb = np.array(pb)
            # ex-2020Q1 и ex-2022: события в эти годы убираем
            yrs = pd.DatetimeIndex(ev_in).year
            ev_ex = ev_in[(yrs != 2022) & ~((yrs == 2020) & (pd.DatetimeIndex(ev_in).month <= 3))]
            mt_ex = metrics(bt(overlay(base_d, ev_ex, K), a, b), freq=252)
            rows.append(dict(window=wn, events=ev_name, K=K, n_ev=len(ev_in), cagr=mt["cagr"], sharpe=mt["sharpe"],
                             maxdd=mt["maxdd"], tim=mt["time_in_mkt"], d_sharpe=mt["sharpe"] - bm["sharpe"],
                             placebo_days_mean=pa.mean(), p_days=(pa >= mt["sharpe"]).mean(),
                             placebo_meet_mean=pb.mean() if len(pb) else np.nan, p_meet=(pb >= mt["sharpe"]).mean() if len(pb) else np.nan,
                             sharpe_ex2020Q1_ex2022=mt_ex["sharpe"], maxdd_ex=mt_ex["maxdd"]))
res = pd.DataFrame(rows)
for c in ["cagr", "maxdd", "tim", "maxdd_ex"]: res[c] = (res[c] * 100).round(1)
for c in ["sharpe", "d_sharpe", "placebo_days_mean", "placebo_meet_mean", "sharpe_ex2020Q1_ex2022"]: res[c] = res[c].round(3)
res.to_csv(RES / "D2_02b_overlay_placebo.csv", index=False)
print(res.to_string())

# --- вклад событий: K=2, inline_cut, 2015–2026: разность доходности наложения и эталона по окнам
a, b = WINDOWS["2015_2026"]
ev = EVENTS["inline_cut"]; ev = ev[(ev >= np.datetime64(a)) & (ev <= np.datetime64(b))]
p2 = overlay(base_d, ev, 2)
db = bt(base_d, a, b); do = bt(p2, a, b)
diff = (do["ret"] - db["ret"])
contrib = []
for t in ev:
    later = idx[idx >= t]; i = idx.get_loc(later[0]); seg = idx[i:i + 43]
    contrib.append(dict(date=str(pd.Timestamp(t).date()), base_long_share=round(base_d.loc[seg].mean(), 2),
                        mcftr_2m_pct=round((np.exp(r_long.loc[seg].sum()) - 1) * 100, 1),
                        diff_pct=round(diff.reindex(seg).sum() * 100, 2)))
contrib = pd.DataFrame(contrib); contrib.to_csv(RES / "D2_02b_inline_cut_contrib.csv", index=False)
print("\nвклад окон после ОЖИДАЕМЫХ снижений (K=2), п.п. к эталону:")
print(contrib.to_string(index=False))
print("сумма вклада:", round(contrib["diff_pct"].sum(), 2), "; из них 2020-02/2022-09:",
      round(contrib[contrib.date.isin(["2020-02-07", "2022-09-16"])]["diff_pct"].sum(), 2))
