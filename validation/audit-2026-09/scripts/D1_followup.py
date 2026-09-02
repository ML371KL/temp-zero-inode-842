"""D1: добивочные проверки лидеров четырёх семейств (ничего из готового не пересчитывает):
1) плацебо matched-null для вето-фильтров, показавших +ΔШарп (futoi gross/d21, оборот updown,
   ширина deterior, ОРФР nonres) — против случайного вето той же конструкции;
2) nfo_own (единственный ряд ОРФР с устойчивым IC) как фильтр;
3) оборот updown_veto: держаная выборка 2004–2009, лаг 5/10 дней, порог;
4) futoi gross_z120 в стрессе: p-value и дневной override.
Выход: results/D1_followup.csv, results/D1_followup_log.txt (stdout)."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from D1_common import *

p = load_daily(); m = load_monthly(); r = load_raw()
base = pd.read_csv("results/D1_baseline_positions.csv", parse_dates=["date"], index_col="date")
pos_m, gate_m, slope_m = panel_rule_monthly(m)
pos_dgate = base["pos_panel_dgate"]
ME = pd.Index(month_ends(p.index))
out = []

# ---------------------------------------------------------------- 1. плацебо вето-лидеров
fs = pd.read_csv("results/D1_futoi_signals.csv", parse_dates=["date"], index_col="date")
ts = pd.read_csv("results/D1_turnover_signals.csv", parse_dates=["date"], index_col="date")
bs = pd.read_csv("results/D1_breadth_signals.csv", parse_dates=["date"], index_col="date")
f = r[r.series.str.startswith("futoi_mx")].pivot(index="date", columns="series", values="value").reindex(p.index).ffill(limit=3)
gross = f["futoi_mx_long"].abs() + f["futoi_mx_short"].abs()
ratio = f["futoi_mx_pos"] / gross
v = raw_series(r, "imoex_value").reindex(p.index); v = v.where(v > 0)
up = p["px"] > 0


def updown(vv, w=21):
    return np.log(vv.where(~up).rolling(w, min_periods=5).mean() / vv.where(up).rolling(w, min_periods=5).mean())


tests = [
    ("futoi gross_veto(z>1) 2020-07..2026", ("2020-07-01", "2026-08-31"), ME[ME >= "2020-06-30"],
     lambda s: (zscore(np.log(s), 120, 60).reindex(ME[ME >= "2020-06-30"]) <= 1.0, "veto"), gross.dropna(), 21),
    ("futoi d21_veto(d21>0.05) 2020-07..2026", ("2020-07-01", "2026-08-31"), ME[ME >= "2020-06-30"],
     lambda s: ((s - s.shift(21)).reindex(ME[ME >= "2020-06-30"]) <= 0.05, "veto"), ratio.dropna(), 21),
    ("turnover updown_veto(lr21>0.1) 2010-2026", ("2010-01-01", "2026-08-31"), ME[ME >= "2009-12-31"],
     lambda s: (~(updown(s).reindex(ME[ME >= "2009-12-31"]) > 0.1), "veto"), v.loc["2009-01-01":].dropna(), 21),
    ("breadth deterior_veto(d21<-0.2) 2015-2026", ("2015-01-01", "2026-08-31"), ME[ME >= "2014-12-31"],
     lambda s: (~((s - s.shift(21)).reindex(ME[ME >= "2014-12-31"]) < -0.2), "veto"), p["breadth"].dropna(), 42),
]
for name, win, me, mk, sigser, block in tests:
    obs, pv, mean_pl, p95 = placebo_family(p, pos_m.reindex(me), mk, sigser, win, n_placebo=200, block=block)
    out.append(dict(test="placebo", name=name, d_sharpe_obs=round(obs, 3), p_placebo=round(pv, 3),
                    placebo_mean=round(mean_pl, 3), placebo_p95=round(p95, 3)))
    print("PLACEBO", name, out[-1])

# ------------------------------------------------- 2. nfo_own (ОРФР) как фильтр, лаг 15 дней
o = pd.read_csv("data/orfr_flows.csv"); o["period"] = pd.PeriodIndex(o["month"], freq="M"); o = o.set_index("period")
av = []
for per in o.index:
    d = (per + 1).to_timestamp() + pd.Timedelta(days=14); later = p.index[p.index >= d]
    av.append(later[0] if len(later) else pd.NaT)
o["avail"] = av; o = o[o.avail.notna()]
O = o.set_index("avail"); O = O[~O.index.duplicated(keep="last")]
me = ME[ME >= "2021-10-31"]
last = O.reindex(p.index).ffill().reindex(me)
own3 = O["nfo_own"].rolling(3, min_periods=2).sum().reindex(p.index).ffill().reindex(me)
F = {"own_veto(<-10)": (~(last["nfo_own"] < -10), "veto"), "own_veto(<0)": (~(last["nfo_own"] < 0), "veto"),
     "own_override(>10)": (last["nfo_own"] > 10, "override"), "own_override(>0)": (last["nfo_own"] > 0, "override"),
     "own3m_veto(<-20)": (~(own3 < -20), "veto"), "own3m_override(>10)": (own3 > 10, "override")}
WIN = {"2022-01..2026-08": ("2022-01-01", "2026-08-31"), "2022-01..2023-12": ("2022-01-01", "2023-12-31"),
       "2024-01..2026-08": ("2024-01-01", "2026-08-31")}
T, _ = run_family(p, pos_m.reindex(me), F, WIN, "panel_m")
T.to_csv("results/D1_orfr_own_strat.csv", index=False, float_format="%.4f")
print("\n=== nfo_own как фильтр ===")
print(T[T.window == "2022-01..2026-08"][["name", "cagr", "sharpe", "sharpe_ex", "maxdd", "tim", "d_sharpe", "ci_lo", "ci_hi", "p_le0", "activity"]].round(3).to_string())
print(T.pivot_table(index="name", columns="window", values="d_sharpe").round(2).to_string())
for nm in ("own_veto(<0)", "own_override(>0)"):
    mk = (lambda s: (~(s.reindex(p.index).ffill().reindex(me) < 0), "veto")) if "veto" in nm else \
         (lambda s: (s.reindex(p.index).ffill().reindex(me) > 0, "override"))
    obs, pv, mean_pl, p95 = placebo_family(p, pos_m.reindex(me), mk, O["nfo_own"].dropna(), WIN["2022-01..2026-08"], n_placebo=200, block=3)
    out.append(dict(test="placebo", name=f"orfr {nm}", d_sharpe_obs=round(obs, 3), p_placebo=round(pv, 3),
                    placebo_mean=round(mean_pl, 3), placebo_p95=round(p95, 3)))
    print("PLACEBO", nm, out[-1])
# IC nfo_own: контроль на реверсию — nfo_own против прошлой доходности (не покупают ли они просто просадку)
x = O["nfo_own"]; past21 = (np.log(p["mcftr_ffill"]) - np.log(p["mcftr_ffill"]).shift(42)).reindex(O.index)
print("nfo_own vs прошлой 42д доходности (Спирмен):", stats.spearmanr(x.dropna(), past21.reindex(x.dropna().index), nan_policy="omit")[0].round(3))
for H in (21, 63):
    fwd = fwd_returns(p, H).reindex(O.index)
    df = pd.concat([x, past21, fwd], axis=1, keys=["own", "past", "fwd"]).dropna()
    # частная корреляция own↔fwd при контроле past (ранговая)
    R = df.rank()
    res_own = R["own"] - np.polyval(np.polyfit(R["past"], R["own"], 1), R["past"])
    res_fwd = R["fwd"] - np.polyval(np.polyfit(R["past"], R["fwd"], 1), R["past"])
    print(f"H={H}: IC nfo_own {stats.spearmanr(df.own, df.fwd)[0]:.3f}; частный при контроле прошлой доходности {stats.pearsonr(res_own, res_fwd)[0]:.3f}, n={len(df)}")
    out.append(dict(test="nfo_own_partial", name=f"H{H}", ic=round(stats.spearmanr(df.own, df.fwd)[0], 3),
                    ic_partial=round(stats.pearsonr(res_own, res_fwd)[0], 3), n=len(df)))

# ------------------------------------ 3. оборот updown_veto: держаная выборка 2004–2009, лаг, порог
me04 = ME[ME >= "2003-12-31"]
ud = updown(v)
rows = []
for thr in (0.05, 0.1, 0.2):
    for L in (0, 5, 10):
        fm = ~(ud.shift(L).reindex(me04) > thr)
        for wname, win in {"2004-2009 (hold-out)": ("2004-01-01", "2009-12-31"), "2010-2026": ("2010-01-01", "2026-08-31"),
                           "2004-2026": ("2004-01-01", "2026-08-31")}.items():
            bt0 = backtest(p, to_daily_position(pos_m.reindex(me04), p.index), *win)
            bt = backtest(p, to_daily_position(filtered_monthly_positions(pos_m.reindex(me04), fm, "veto"), p.index), *win)
            d0, d = metrics(bt0), metrics(bt)
            rows.append(dict(thr=thr, lag=L, window=wname, sharpe0=round(d0["sharpe"], 3), sharpe=round(d["sharpe"], 3),
                             d_sharpe=round(d["sharpe"] - d0["sharpe"], 3), d_sharpe_ex=round(d["sharpe_ex"] - d0["sharpe_ex"], 3),
                             cagr0=round(d0["cagr"], 3), cagr=round(d["cagr"], 3), mdd0=round(d0["maxdd"], 3), mdd=round(d["maxdd"], 3),
                             tim0=round(d0["tim"], 3), tim=round(d["tim"], 3), activity=round((bt.pos != bt0.pos).mean(), 3)))
UD = pd.DataFrame(rows); UD.to_csv("results/D1_turnover_updown_holdout.csv", index=False)
print("\n=== оборот updown_veto: держаная 2004–2009 / лаг / порог ===")
print(UD.to_string())
# бутстреп разности на 2004–2026 для thr=0.1 lag=0
fm = ~(ud.reindex(me04) > 0.1)
bt0 = backtest(p, to_daily_position(pos_m.reindex(me04), p.index), "2004-01-01", "2026-08-31")
bt = backtest(p, to_daily_position(filtered_monthly_positions(pos_m.reindex(me04), fm, "veto"), p.index), "2004-01-01", "2026-08-31")
obs, lo, hi, pl, n = sharpe_diff_boot(bt, bt0)
print(f"updown_veto(0.1) 2004–2026: dSharpe {obs:.3f} CI [{lo:.3f},{hi:.3f}] p_le0 {pl:.3f} n={n}")
out.append(dict(test="updown_2004_2026", name="updown_veto(lr21>0.1)", d_sharpe_obs=round(obs, 3), ci_lo=round(lo, 3), ci_hi=round(hi, 3), p_le0=round(pl, 3), n=n))
tl = timeliness(bt); tl0 = timeliness(bt0)
print(pd.concat([tl0.assign(rule="panel_m"), tl.assign(rule="updown_veto")]).to_string())
obs, pv, mean_pl, p95 = placebo_family(p, pos_m.reindex(me04), lambda s: (~(updown(s).reindex(me04) > 0.1), "veto"),
                                       v.loc["2003-01-01":].dropna(), ("2004-01-01", "2026-08-31"), n_placebo=200, block=21)
out.append(dict(test="placebo", name="turnover updown_veto(lr21>0.1) 2004-2026", d_sharpe_obs=round(obs, 3), p_placebo=round(pv, 3),
                placebo_mean=round(mean_pl, 3), placebo_p95=round(p95, 3)))
print("PLACEBO 2004-2026", out[-1])

# -------------------------------------------- 4. futoi gross_z120 в стрессе: p и дневной override
IC = pd.read_csv("results/D1_futoi_ic.csv")
print("\n=== gross_z120 / avg_long_z120 IC с p ===")
print(IC[(IC.signal.isin(["gross_z120", "avg_long_z120", "holders_lr"])) & (IC.lag == 0)][["signal", "H", "split", "n", "ic", "p_sp", "nw_t", "p_boot"]].to_string())
gz = fs["gross_z120"]
FD = {"gross_override(z>1&toxic)": ((gz > 1.0) & (p["cell"] == TOXIC), "override"),
      "gross_override(z>1.5&dd<-15)": ((gz > 1.5) & (p["dd252"] < -0.15), "override"),
      "gross_veto(z<-1&bull)": (~((gz < -1.0) & (p["st_trend"] == 1)), "veto")}
TD, _ = run_family(p, pos_dgate, FD, {"2020-07..2026-08": ("2020-07-01", "2026-08-31"),
                                       "2020-07..2023-12": ("2020-07-01", "2023-12-31"), "2024-01..2026-08": ("2024-01-01", "2026-08-31")},
                   "panel_dgate", daily=True)
print(TD[["window", "name", "cagr", "sharpe", "sharpe_ex", "maxdd", "tim", "d_sharpe", "ci_lo", "ci_hi", "p_le0", "activity", "missed_mean", "entry_days_med"]].round(3).to_string())
TD.to_csv("results/D1_futoi_gross_strat_d.csv", index=False, float_format="%.4f")

pd.DataFrame(out).to_csv("results/D1_followup.csv", index=False)
