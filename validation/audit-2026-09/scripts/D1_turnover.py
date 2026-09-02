"""D1: оборот IMOEX (imoex_value, с 2002-11): z лог-оборота (60/120), оборот на падении vs росте,
объёмная капитуляция, оборот к волатильности. IC 2003–2026 по состояниям/эрам и стратегии
2010–2026 «правило панели + фильтр по обороту». Выход: results/D1_turnover_ic.csv,
results/D1_turnover_strat_m.csv, results/D1_turnover_strat_d.csv, results/D1_turnover_placebo.csv."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from D1_common import *

p = load_daily(); m = load_monthly(); r = load_raw()
base = pd.read_csv("results/D1_baseline_positions.csv", parse_dates=["date"], index_col="date")
pos_m, gate_m, slope_m = panel_rule_monthly(m)
pos_dgate = base["pos_panel_dgate"]

v = raw_series(r, "imoex_value").reindex(p.index)
v = v.where(v > 0)
lv = np.log(v)
S = {}
S["z60"] = zscore(lv, 60, 30)
S["z120"] = zscore(lv, 120, 60)
S["z252"] = zscore(lv, 252, 126)
S["rel252"] = lv - lv.rolling(252, min_periods=126).median()
S["trend21_126"] = np.log(v.rolling(21, min_periods=10).mean() / v.rolling(126, min_periods=60).mean())
up = p["px"] > 0
vd = v.where(~up).rolling(21, min_periods=5).mean()
vu = v.where(up).rolling(21, min_periods=5).mean()
S["updown_lr21"] = np.log(vd / vu)                          # >0: обороты выше на падениях
vd63 = v.where(~up).rolling(63, min_periods=15).mean(); vu63 = v.where(up).rolling(63, min_periods=15).mean()
S["updown_lr63"] = np.log(vd63 / vu63)
S["turn_vs_vol"] = S["z120"] - zscore(np.log(p["realized_vol_21"]), 120, 60)
S["capit_z1.5_dd15"] = ((S["z120"] > 1.5) & (p["dd252"] < -0.15)).astype(float)
S["capit_z1_dd10"] = ((S["z120"] > 1.0) & (p["dd252"] < -0.10)).astype(float)
S["z120_x_ret21"] = S["z120"] * np.sign(p["mom63"])       # объём по тренду / против
# «пик объёма позади»: z120 упал от максимума за 21 день более чем на 1
S["z120_off_peak21"] = S["z120"].rolling(21, min_periods=10).max() - S["z120"]
sig = pd.DataFrame(S)
sig.to_csv("results/D1_turnover_signals.csv", float_format="%.4f")

A, B = "2003-06-01", "2026-08-31"
splits = state_splits(p, A, B)
for nm, (a, b) in {"2003-2009": ("2003-06-01", "2009-12-31"), "2010-2021": ("2010-01-01", "2022-02-18"),
                   "2022-2024": ("2022-03-24", "2024-12-31"), "2025-2026": ("2025-01-01", B)}.items():
    splits[nm] = pd.Series((p.index >= a) & (p.index <= b), index=p.index)
splits["ex2022"] = splits["all"] & ~pd.Series((p.index >= "2022-01-01") & (p.index <= "2022-12-31"), index=p.index)
rows = []
for name in sig.columns:
    rows += ic_battery(p, sig[name], name, splits)
IC = pd.DataFrame(rows)[["signal", "H", "split", "n", "ic", "p_sp", "nw_t", "p_boot", "terc_hi", "terc_lo"]]
IC.to_csv("results/D1_turnover_ic.csv", index=False)
print("=== IC оборот (месячные срезы) ===")
print(IC.pivot_table(index=["signal", "H"], columns="split", values="ic").round(2).to_string())
print(IC[IC.split == "all"][["signal", "H", "n", "ic", "p_sp", "nw_t", "p_boot", "terc_hi", "terc_lo"]].to_string())
# условные средние: капитуляция
for H in (21, 63):
    fwd = fwd_returns(p, H)
    me = pd.Index(month_ends(p.index)); me = me[me >= A]
    for flag in ("capit_z1.5_dd15", "capit_z1_dd10"):
        f = sig[flag].reindex(me); fw = fwd.reindex(me)
        print(f"{flag} H={H}: n_on={int((f == 1).sum())} mean_on={fw[f == 1].mean()*100:.2f}% "
              f"mean_off={fw[f == 0].mean()*100:.2f}% | toxic&on n={int(((f == 1) & (p['cell'].reindex(me) == TOXIC)).sum())} "
              f"mean={fw[(f == 1) & (p['cell'].reindex(me) == TOXIC)].mean()*100:.2f}%")

# ------------------------------------------------------------------ стратегии
me = pd.Index(month_ends(p.index)); me = me[me >= "2009-12-31"]
Sm = sig.reindex(me); dd_m = p["dd252"].reindex(me); tr_m = p["st_trend"].reindex(me)


def gate_override(base_m, fm):
    g = gate_m.reindex(base_m.index).astype(bool) | fm.fillna(False).astype(bool)
    return (g & slope_m.reindex(base_m.index).astype(bool)).astype(float)


def build(Sm, dd_m, tr_m):
    F = {}
    for z, d in ((1.0, 0.10), (1.5, 0.15), (1.0, 0.15), (1.5, 0.10)):
        F[f"capit_override(z>{z}&dd<-{int(d*100)})"] = ((Sm["z120"] > z) & (dd_m < -d), "override")
        F[f"capit_gateonly(z>{z}&dd<-{int(d*100)})"] = ((Sm["z120"] > z) & (dd_m < -d), gate_override)
    F["capit_offpeak_override(z_off>1&dd<-15)"] = ((Sm["z120_off_peak21"] > 1.0) & (dd_m < -0.15), "override")
    F["fragile_veto(bull&z120<-1)"] = (~((tr_m == 1) & (Sm["z120"] < -1.0)), "veto")
    F["fragile_veto(z120<-1)"] = (~(Sm["z120"] < -1.0), "veto")
    F["frenzy_veto(rel252>0.5)"] = (~(Sm["rel252"] > 0.5), "veto")
    F["frenzy_veto(z252>1.5)"] = (~(Sm["z252"] > 1.5), "veto")
    F["frenzy_veto(bull&z252>1.5)"] = (~((tr_m == 1) & (Sm["z252"] > 1.5)), "veto")
    for thr in (0.1, 0.2, 0.3):
        F[f"updown_veto(lr21>{thr})"] = (~(Sm["updown_lr21"] > thr), "veto")
    F["updown_override(lr21>0.2&dd<-15)"] = ((Sm["updown_lr21"] > 0.2) & (dd_m < -0.15), "override")
    F["updown_confirm_veto(lr63>0)"] = (~(Sm["updown_lr63"] > 0.0), "veto")
    F["turnvol_veto(<-1)"] = (~(Sm["turn_vs_vol"] < -1.0), "veto")
    F["turnvol_override(>1&dd<-15)"] = ((Sm["turn_vs_vol"] > 1.0) & (dd_m < -0.15), "override")
    F["trend_veto(trend21_126<-0.3)"] = (~(Sm["trend21_126"] < -0.3), "veto")
    F["trend_confirm_veto(trend21_126>0)"] = (Sm["trend21_126"] > 0, "veto")
    return F


WIN = {"2010-01..2026-08": ("2010-01-01", "2026-08-31"), "2010-2017": ("2010-01-01", "2017-12-31"),
       "2018-2026": ("2018-01-01", "2026-08-31"), "2022-03..2024": ("2022-03-24", "2024-12-31"),
       "2025-2026": ("2025-01-01", "2026-08-31")}
F = build(Sm, dd_m, tr_m)
print(f"\nвариантов месячных фильтров: {len(F)}")
T, tls = run_family(p, pos_m.reindex(me), F, WIN, "panel_m")
ex = []
for name, (fm, mode) in [("panel_m", (None, None))] + list(F.items()):
    pos = to_daily_position(pos_m.reindex(me) if name == "panel_m" else
                            filtered_monthly_positions(pos_m.reindex(me), fm, mode), p.index)
    bt = backtest(p, pos, "2010-01-01", "2026-08-31")
    bt = bt[~((bt.index >= "2022-01-01") & (bt.index <= "2022-12-31"))]
    d = metrics(bt); d["name"] = name; d["window"] = "2010-2026 ex2022"; ex.append(d)
EX = pd.DataFrame(ex); b0 = EX[EX.name == "panel_m"].iloc[0]
EX["d_sharpe"] = EX["sharpe"] - b0["sharpe"]; EX["d_mdd"] = EX["maxdd"] - b0["maxdd"]
T = pd.concat([T, EX[["window", "name", "cagr", "vol", "sharpe", "sharpe_ex", "maxdd", "tim", "trades_yr",
                      "beat_bh_years", "hit_m", "d_sharpe", "d_mdd"]]], ignore_index=True)
T.to_csv("results/D1_turnover_strat_m.csv", index=False, float_format="%.4f")
print("\n=== стратегии, главное окно 2010–2026 (месячное правило) ===")
main = T[T.window == "2010-01..2026-08"].sort_values("d_sharpe", ascending=False)
print(main[["name", "cagr", "sharpe", "maxdd", "tim", "d_sharpe", "ci_lo", "ci_hi", "p_le0", "d_mdd", "activity",
            "avoid_mean", "missed_mean", "entry_days_med"]].round(3).to_string())
print("\n=== d_sharpe по подокнам ===")
print(T.pivot_table(index="name", columns="window", values="d_sharpe").round(2).sort_values("2010-01..2026-08", ascending=False).to_string())
tl = pd.concat([t.assign(rule=k) for k, t in tls.items()])
tl.to_csv("results/D1_turnover_timeliness.csv", index=False)
print(tl[tl.rule.isin(["panel_m", "capit_override(z>1.0&dd<-10)", "capit_override(z>1.5&dd<-15)",
                       "updown_override(lr21>0.2&dd<-15)", "fragile_veto(bull&z120<-1)"])].to_string())

# ------------------------------------------------------------- дневной вариант
FD = {
    "capit_override(z>1.0&dd<-10)": ((sig["z120"] > 1.0) & (p["dd252"] < -0.10), "override"),
    "capit_override(z>1.5&dd<-15)": ((sig["z120"] > 1.5) & (p["dd252"] < -0.15), "override"),
    "capit_offpeak_override(z_off>1&dd<-15)": ((sig["z120_off_peak21"] > 1.0) & (p["dd252"] < -0.15), "override"),
    "updown_override(lr21>0.2&dd<-15)": ((sig["updown_lr21"] > 0.2) & (p["dd252"] < -0.15), "override"),
    "fragile_veto(bull&z120<-1)": (~((p["st_trend"] == 1) & (sig["z120"] < -1.0)), "veto"),
    "frenzy_veto(z252>1.5)": (~(sig["z252"] > 1.5), "veto"),
    "updown_veto(lr21>0.2)": (~(sig["updown_lr21"] > 0.2), "veto"),
}
TD, tld = run_family(p, pos_dgate, FD, WIN, "panel_dgate", daily=True)
TD.to_csv("results/D1_turnover_strat_d.csv", index=False, float_format="%.4f")
print("\n=== дневные ворота + дневной фильтр, 2010–2026 ===")
print(TD[TD.window == "2010-01..2026-08"][["name", "cagr", "sharpe", "maxdd", "tim", "trades_yr", "d_sharpe", "ci_lo", "ci_hi",
                                            "p_le0", "d_mdd", "activity", "avoid_mean", "missed_mean"]].round(3).to_string())
print(TD.pivot_table(index="name", columns="window", values="d_sharpe").round(2).to_string())
tld_all = pd.concat([t.assign(rule=k) for k, t in tld.items()])
tld_all.to_csv("results/D1_turnover_timeliness_d.csv", index=False)

# ----------------------------------------------------------------- плацебо
pl = []
for name, mk, daily in [
    ("m:capit_override(z>1.0&dd<-10)", lambda s: (((zscore(s, 120, 60) > 1.0) & (p["dd252"] < -0.10)).reindex(me), "override"), False),
    ("m:capit_override(z>1.5&dd<-15)", lambda s: (((zscore(s, 120, 60) > 1.5) & (p["dd252"] < -0.15)).reindex(me), "override"), False),
    ("m:fragile_veto(bull&z120<-1)", lambda s: ((~((p["st_trend"] == 1) & (zscore(s, 120, 60) < -1.0))).reindex(me), "veto"), False),
    ("d:capit_override(z>1.0&dd<-10)", lambda s: (((zscore(s, 120, 60) > 1.0) & (p["dd252"] < -0.10)), "override"), True),
]:
    basep = pos_dgate if daily else pos_m.reindex(me)
    obs, pv, mean_pl, p95 = placebo_family(p, basep, mk, lv.loc["2009-01-01":], WIN["2010-01..2026-08"],
                                           n_placebo=200, block=21, daily=daily)
    pl.append(dict(name=name, d_sharpe_obs=round(obs, 3), p_placebo=round(pv, 3), placebo_mean=round(mean_pl, 3),
                   placebo_p95=round(p95, 3)))
    print(name, pl[-1])
pd.DataFrame(pl).to_csv("results/D1_turnover_placebo.csv", index=False)

print("\n=== значения у ключевых дат ===")
for d in ["2008-10-24", "2008-10-31", "2011-10-04", "2014-12-16", "2020-03-18", "2020-03-31", "2022-02-24", "2022-09-26",
          "2022-09-30", "2024-09-03", "2024-12-17", "2025-02-25", "2026-07-17", "2026-07-31", "2026-08-31"]:
    dd = pd.Timestamp(d)
    if dd in sig.index:
        print(d, {k: round(sig.loc[dd, k], 2) for k in ["z120", "z252", "updown_lr21", "turn_vs_vol", "z120_off_peak21"]},
              "dd252", round(p.loc[dd, "dd252"], 3))
