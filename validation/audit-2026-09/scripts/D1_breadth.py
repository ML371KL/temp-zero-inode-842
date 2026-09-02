"""D1: ширина рынка — breadth панели (доля выше MA200, с 2014-08), собственная ширина из
prices_wide.csv (доля с 21-дн моментумом >0, доля на 52-нед минимумах/максимумах, дисперсия),
MCXSM/IMOEX. IC 2015–2026 по состояниям/эрам, стратегии «правило панели + фильтр».
Выход: results/D1_breadth_ic.csv, results/D1_breadth_strat_m.csv, results/D1_breadth_strat_d.csv,
results/D1_breadth_placebo.csv."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from D1_common import *

p = load_daily(); m = load_monthly()
base = pd.read_csv("results/D1_baseline_positions.csv", parse_dates=["date"], index_col="date")
pos_m, gate_m, slope_m = panel_rule_monthly(m)
pos_dgate = base["pos_panel_dgate"]

w = pd.read_csv("data/prices_wide.csv", parse_dates=["date"]).set_index("date")
w = w.reindex(p.index).ffill(limit=3)
w = w.where(w > 0)
lp = np.log(w)
MIN_T = 15
nval = w.notna().sum(axis=1)
mom21 = lp - lp.shift(21)
mom63 = lp - lp.shift(63)
S = {}
S["breadth"] = p["breadth"]                                     # доля выше MA200 (панель)
S["breadth_d21"] = p["breadth"] - p["breadth"].shift(21)
S["breadth_z252"] = zscore(p["breadth"], 252, 126)
S["breadth_low"] = (p["breadth"] < 0.10).astype(float).where(p["breadth"].notna())
S["breadth_high"] = (p["breadth"] > 0.90).astype(float).where(p["breadth"].notna())
S["share_mom21_pos"] = (mom21 > 0).sum(axis=1) / mom21.notna().sum(axis=1)
S["share_mom21_pos"] = S["share_mom21_pos"].where(nval >= MIN_T)
S["share_mom63_pos"] = ((mom63 > 0).sum(axis=1) / mom63.notna().sum(axis=1)).where(nval >= MIN_T)
low252 = w.rolling(252, min_periods=150).min(); high252 = w.rolling(252, min_periods=150).max()
S["share_at_low"] = ((w <= low252 * 1.02).sum(axis=1) / low252.notna().sum(axis=1)).where(nval >= MIN_T)
S["share_at_high"] = ((w >= high252 * 0.98).sum(axis=1) / high252.notna().sum(axis=1)).where(nval >= MIN_T)
S["disp21"] = mom21.std(axis=1).where(nval >= MIN_T)
S["disp21_z252"] = zscore(S["disp21"], 252, 126)
S["mcxsm_rel63"] = p["mcxsm_rel63"]
S["mcxsm_rel63_z252"] = zscore(p["mcxsm_rel63"], 252, 126)
# «бросок ширины»: share_mom21_pos поднялась выше 0.6, а за 21 день до этого была ниже 0.2
S["thrust"] = ((S["share_mom21_pos"] > 0.6) & (S["share_mom21_pos"].rolling(21, min_periods=10).min() < 0.2)).astype(float)
S["thrust"] = S["thrust"].where(S["share_mom21_pos"].notna())
sig = pd.DataFrame(S)
sig.to_csv("results/D1_breadth_signals.csv", float_format="%.4f")
print("покрытие:", sig.notna().sum().to_dict())

A, B = "2015-01-01", "2026-08-31"
splits = state_splits(p, A, B)
for nm, (a, b) in {"2015-2021": (A, "2022-02-18"), "2022-2024": ("2022-03-24", "2024-12-31"),
                   "2025-2026": ("2025-01-01", B)}.items():
    splits[nm] = pd.Series((p.index >= a) & (p.index <= b), index=p.index)
splits["ex2022"] = splits["all"] & ~pd.Series((p.index >= "2022-01-01") & (p.index <= "2022-12-31"), index=p.index)
rows = []
for name in sig.columns:
    rows += ic_battery(p, sig[name], name, splits)
IC = pd.DataFrame(rows)[["signal", "H", "split", "n", "ic", "p_sp", "nw_t", "p_boot", "terc_hi", "terc_lo"]]
IC.to_csv("results/D1_breadth_ic.csv", index=False)
print("=== IC ширина (месячные срезы) ===")
print(IC.pivot_table(index=["signal", "H"], columns="split", values="ic").round(2).to_string())
print(IC[IC.split == "all"][["signal", "H", "n", "ic", "p_sp", "nw_t", "p_boot", "terc_hi", "terc_lo"]].to_string())

# ------------------------------------------------------------------ стратегии
me = pd.Index(month_ends(p.index)); me = me[me >= "2014-12-31"]
Sm = sig.reindex(me); dd_m = p["dd252"].reindex(me); tr_m = p["st_trend"].reindex(me)


def gate_override(base_m, fm):
    g = gate_m.reindex(base_m.index).astype(bool) | fm.fillna(False).astype(bool)
    return (g & slope_m.reindex(base_m.index).astype(bool)).astype(float)


F = {}
for thr in (0.05, 0.10, 0.15, 0.20):
    F[f"washout_override(breadth<{thr})"] = (Sm["breadth"] < thr, "override")
    F[f"washout_gateonly(breadth<{thr})"] = (Sm["breadth"] < thr, gate_override)
F["washout_improving_override(<0.15&d21>0)"] = ((Sm["breadth"] < 0.15) & (Sm["breadth_d21"] > 0), "override")
F["washout_improving_override(<0.25&d21>0.05)"] = ((Sm["breadth"] < 0.25) & (Sm["breadth_d21"] > 0.05), "override")
for thr in (0.8, 0.9):
    F[f"overbought_veto(breadth>{thr})"] = (~(Sm["breadth"] > thr), "veto")
F["confirm_veto(d21>=0)"] = (Sm["breadth_d21"] >= 0, "veto")
F["confirm_veto(share_mom21>0.5)"] = (Sm["share_mom21_pos"] > 0.5, "veto")
F["deterior_veto(d21<-0.2)"] = (~(Sm["breadth_d21"] < -0.2), "veto")
for thr in (0.3, 0.5):
    F[f"lows_override(share_low>{thr})"] = (Sm["share_at_low"] > thr, "override")
F["lows_gateonly(share_low>0.3)"] = (Sm["share_at_low"] > 0.3, gate_override)
F["highs_veto(share_high>0.4)"] = (~(Sm["share_at_high"] > 0.4), "veto")
F["thrust_override"] = (Sm["thrust"] == 1, "override")
F["thrust_gateonly"] = (Sm["thrust"] == 1, gate_override)
F["disp_override(z>1.5&dd<-15)"] = ((Sm["disp21_z252"] > 1.5) & (dd_m < -0.15), "override")
F["disp_veto(z>1.5)"] = (~(Sm["disp21_z252"] > 1.5), "veto")
F["mcxsm_veto(rel63<-0.05)"] = (~(Sm["mcxsm_rel63"] < -0.05), "veto")
F["mcxsm_confirm_veto(rel63>0)"] = (Sm["mcxsm_rel63"] > 0, "veto")
F["mcxsm_override(z<-1.5&dd<-15)"] = ((Sm["mcxsm_rel63_z252"] < -1.5) & (dd_m < -0.15), "override")
print(f"\nвариантов месячных фильтров: {len(F)}")
WIN = {"2015-01..2026-08": ("2015-01-01", "2026-08-31"), "2015-2021": ("2015-01-01", "2022-02-18"),
       "2022-03..2024": ("2022-03-24", "2024-12-31"), "2025-2026": ("2025-01-01", "2026-08-31")}
T, tls = run_family(p, pos_m.reindex(me), F, WIN, "panel_m")
ex = []
for name, (fm, mode) in [("panel_m", (None, None))] + list(F.items()):
    pos = to_daily_position(pos_m.reindex(me) if name == "panel_m" else
                            filtered_monthly_positions(pos_m.reindex(me), fm, mode), p.index)
    bt = backtest(p, pos, "2015-01-01", "2026-08-31")
    bt = bt[~((bt.index >= "2022-01-01") & (bt.index <= "2022-12-31"))]
    d = metrics(bt); d["name"] = name; d["window"] = "2015-2026 ex2022"; ex.append(d)
EX = pd.DataFrame(ex); b0 = EX[EX.name == "panel_m"].iloc[0]
EX["d_sharpe"] = EX["sharpe"] - b0["sharpe"]; EX["d_mdd"] = EX["maxdd"] - b0["maxdd"]
T = pd.concat([T, EX[["window", "name", "cagr", "vol", "sharpe", "sharpe_ex", "maxdd", "tim", "trades_yr",
                      "beat_bh_years", "hit_m", "d_sharpe", "d_mdd"]]], ignore_index=True)
T.to_csv("results/D1_breadth_strat_m.csv", index=False, float_format="%.4f")
print("\n=== стратегии, главное окно 2015–2026 (месячное правило) ===")
main = T[T.window == "2015-01..2026-08"].sort_values("d_sharpe", ascending=False)
print(main[["name", "cagr", "sharpe", "maxdd", "tim", "d_sharpe", "ci_lo", "ci_hi", "p_le0", "d_mdd", "activity",
            "avoid_mean", "missed_mean", "entry_days_med"]].round(3).to_string())
print("\n=== d_sharpe по подокнам ===")
print(T.pivot_table(index="name", columns="window", values="d_sharpe").round(2).sort_values("2015-01..2026-08", ascending=False).to_string())
tl = pd.concat([t.assign(rule=k) for k, t in tls.items()])
tl.to_csv("results/D1_breadth_timeliness.csv", index=False)
print(tl[tl.rule.isin(["panel_m", "washout_override(breadth<0.10)", "washout_improving_override(<0.15&d21>0)",
                       "thrust_override", "lows_override(share_low>0.3)"])].to_string())

# ------------------------------------------------------------- дневной вариант
FD = {
    "washout_override(breadth<0.10)": (sig["breadth"] < 0.10, "override"),
    "washout_override(breadth<0.05)": (sig["breadth"] < 0.05, "override"),
    "washout_improving_override(<0.15&d21>0)": ((sig["breadth"] < 0.15) & (sig["breadth_d21"] > 0), "override"),
    "washout_improving_override(<0.25&d21>0.05)": ((sig["breadth"] < 0.25) & (sig["breadth_d21"] > 0.05), "override"),
    "thrust_override": (sig["thrust"] == 1, "override"),
    "lows_override(share_low>0.3)": (sig["share_at_low"] > 0.3, "override"),
    "overbought_veto(breadth>0.9)": (~(sig["breadth"] > 0.9), "veto"),
    "confirm_veto(d21>=0)": (sig["breadth_d21"] >= 0, "veto"),
    "mcxsm_veto(rel63<-0.05)": (~(sig["mcxsm_rel63"] < -0.05), "veto"),
}
TD, tld = run_family(p, pos_dgate, FD, WIN, "panel_dgate", daily=True)
TD.to_csv("results/D1_breadth_strat_d.csv", index=False, float_format="%.4f")
print("\n=== дневные ворота + дневной фильтр, 2015–2026 ===")
print(TD[TD.window == "2015-01..2026-08"][["name", "cagr", "sharpe", "maxdd", "tim", "trades_yr", "d_sharpe", "ci_lo", "ci_hi",
                                            "p_le0", "d_mdd", "activity", "avoid_mean", "missed_mean", "entry_days_med"]].round(3).to_string())
print(TD.pivot_table(index="name", columns="window", values="d_sharpe").round(2).to_string())
tld_all = pd.concat([t.assign(rule=k) for k, t in tld.items()])
tld_all.to_csv("results/D1_breadth_timeliness_d.csv", index=False)

# ----------------------------------------------------------------- плацебо
pl = []
for name, mk, daily in [
    ("m:washout_override(breadth<0.10)", lambda s: ((s < 0.10).reindex(me), "override"), False),
    ("m:washout_improving_override(<0.15&d21>0)", lambda s: (((s < 0.15) & ((s - s.shift(21)) > 0)).reindex(me), "override"), False),
    ("m:confirm_veto(d21>=0)", lambda s: (((s - s.shift(21)) >= 0).reindex(me), "veto"), False),
    ("d:washout_improving_override(<0.15&d21>0)", lambda s: (((s < 0.15) & ((s - s.shift(21)) > 0)), "override"), True),
    ("d:washout_override(breadth<0.10)", lambda s: ((s < 0.10), "override"), True),
]:
    basep = pos_dgate if daily else pos_m.reindex(me)
    obs, pv, mean_pl, p95 = placebo_family(p, basep, mk, p["breadth"].dropna(), WIN["2015-01..2026-08"],
                                           n_placebo=200, block=42, daily=daily)
    pl.append(dict(name=name, d_sharpe_obs=round(obs, 3), p_placebo=round(pv, 3), placebo_mean=round(mean_pl, 3),
                   placebo_p95=round(p95, 3)))
    print(name, pl[-1])
pd.DataFrame(pl).to_csv("results/D1_breadth_placebo.csv", index=False)

print("\n=== значения у ключевых дат ===")
for d in ["2020-03-18", "2022-02-24", "2022-09-26", "2022-10-10", "2024-09-03", "2024-12-17", "2025-02-25",
          "2026-03-09", "2026-07-17", "2026-07-31", "2026-08-15", "2026-08-31"]:
    dd = pd.Timestamp(d)
    if dd in sig.index:
        print(d, {k: round(sig.loc[dd, k], 3) for k in ["breadth", "breadth_d21", "share_mom21_pos", "share_at_low", "disp21_z252", "mcxsm_rel63", "thrust"]})
