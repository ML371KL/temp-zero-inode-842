"""D1: потоки ОРФР (месячные, лаг публикации ~15 дней после месяца — применён явно).
IC от даты доступности, мощность при малом n, стратегии «правило панели + фильтр по потокам».
Выход: results/D1_orfr_ic.csv, results/D1_orfr_strat.csv, results/D1_orfr_power.csv,
results/D1_orfr_placebo.csv."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from D1_common import *

p = load_daily(); m = load_monthly()
pos_m, gate_m, slope_m = panel_rule_monthly(m)

o = pd.read_csv("data/orfr_flows.csv")
o["period"] = pd.PeriodIndex(o["month"], freq="M")
o = o.set_index("period").sort_index()
cats = ["fiz", "nfo_du", "nfo_own", "szko", "other_banks", "nonres"]
# дата доступности: 15-е число следующего месяца -> первый торговый день >= этой даты
avail = []
for per in o.index:
    d = (per + 1).to_timestamp() + pd.Timedelta(days=14)
    later = p.index[p.index >= d]
    avail.append(later[0] if len(later) else pd.NaT)
o["avail"] = avail
o = o[o["avail"].notna()]

# производные (по месячному порядку, до переноса на даты доступности)
inst = o[["nfo_du", "nfo_own", "szko", "other_banks", "nonres"]]
o["inst_total"] = inst.sum(axis=1, min_count=4)
o["du3m"] = o["nfo_du"].rolling(3, min_periods=2).sum()
o["du3m_chg"] = o["du3m"] - o["du3m"].shift(3)
o["exhaustion"] = ((o["du3m"] < 0) & (o["du3m_chg"] > 0)).astype(float)
o["fiz3m"] = o["fiz"].rolling(3, min_periods=2).sum()
o["nonres3m"] = o["nonres"].rolling(3, min_periods=2).sum()
o["du_chg"] = o["nfo_du"] - o["nfo_du"].shift(1)
o["fiz_chg"] = o["fiz"] - o["fiz"].shift(1)
# знак потока физлиц против тренда: тренд = mom63 индекса на конец месяца потока
me_all = pd.Index(month_ends(p.index))
mom_by_per = pd.Series(p["mom63"].reindex(me_all).values, index=me_all.to_period("M"))
trend_by_per = pd.Series(p["st_trend"].reindex(me_all).values, index=me_all.to_period("M"))
o["mom63_M"] = mom_by_per.reindex(o.index)
o["st_trend_M"] = trend_by_per.reindex(o.index)
o["fiz_x_trend"] = np.sign(o["fiz"]) * np.sign(o["mom63_M"])        # +1 = физики ПО тренду
o["fiz_contra_buy"] = ((o["fiz"] > 0) & (o["mom63_M"] < 0)).astype(float)  # покупают на падении
o["du_x_trend"] = np.sign(o["nfo_du"]) * np.sign(o["mom63_M"])
sigs = ["fiz", "nfo_du", "nfo_own", "szko", "other_banks", "nonres", "inst_total", "du3m", "du3m_chg",
        "exhaustion", "fiz3m", "nonres3m", "du_chg", "fiz_chg", "fiz_x_trend", "fiz_contra_buy", "du_x_trend"]
O = o.set_index("avail")[sigs + ["mom63_M"]]
O = O[~O.index.duplicated(keep="last")]
O.to_csv("results/D1_orfr_signals_avail.csv", float_format="%.3f")

# ------------------------------------------------------------------------- IC
rows, power = [], []
for H in (21, 63):
    fwd = fwd_returns(p, H).reindex(O.index)
    for s in sigs:
        x = O[s]
        base = ic_row(x, fwd, H, s, {"split": "all"})
        rows.append(base)
        for name, mask in [("ex2022", ~((O.index >= "2022-01-01") & (O.index <= "2022-12-31"))),
                           ("2022+", O.index >= "2022-01-01"),
                           ("2024+", O.index >= "2024-01-01"),
                           ("bear", p["st_trend"].reindex(O.index) == 0),
                           ("bull", p["st_trend"].reindex(O.index) == 1),
                           ("toxic", p["cell"].reindex(O.index) == TOXIC),
                           ("gate_open", (p["cell"].reindex(O.index) != TOXIC))]:
            mask = pd.Series(np.asarray(mask), index=O.index)
            rows.append(ic_row(x[mask], fwd[mask], H, s, {"split": name}))
        power.append(dict(signal=s, H=H, n=base["n"], rho_detectable_80pct=round(detectable_rho(base["n"]), 2)))
IC = pd.DataFrame(rows)[["signal", "H", "split", "n", "ic", "p_sp", "nw_t", "p_boot", "terc_hi", "terc_lo"]]
IC.to_csv("results/D1_orfr_ic.csv", index=False)
PW = pd.DataFrame(power).drop_duplicates("signal")
PW.to_csv("results/D1_orfr_power.csv", index=False)
print("=== IC от даты доступности ===")
print(IC.pivot_table(index=["signal", "H"], columns="split", values="ic").round(2).to_string())
print(IC[(IC.split == "all")][["signal", "H", "n", "ic", "p_sp", "nw_t", "p_boot"]].to_string())
print("\n=== мощность: обнаружимый |rho| при 80% ===")
print(PW.to_string())

# ------------------------------------------------------------------ стратегии
# на конце месяца t известен последний поток с avail <= t
me = me_all[me_all >= "2021-10-31"]
last_known = O.reindex(p.index).ffill().reindex(me)


def gate_override(base_m, fm):
    g = gate_m.reindex(base_m.index).astype(bool) | fm.fillna(False).astype(bool)
    return (g & slope_m.reindex(base_m.index).astype(bool)).astype(float)


L = last_known
F = {
    "du_veto(du<-10)": (~(L["nfo_du"] < -10), "veto"),
    "du_veto(du<-20)": (~(L["nfo_du"] < -20), "veto"),
    "du_confirm_veto(du>0)": (L["nfo_du"] > 0, "veto"),
    "du3m_veto(<-30)": (~(L["du3m"] < -30), "veto"),
    "exhaust_override": (L["exhaustion"] == 1, "override"),
    "exhaust_gateonly": (L["exhaustion"] == 1, gate_override),
    "exhaust_veto(du3m_chg>0)": (L["du3m_chg"] > 0, "veto"),
    "pressure_veto(du3m_chg<-20)": (~(L["du3m_chg"] < -20), "veto"),
    "fiz_veto(fiz>20)": (~(L["fiz"] > 20), "veto"),
    "fiz_override(fiz>20)": (L["fiz"] > 20, "override"),
    "fiz_contra_override": (L["fiz_contra_buy"] == 1, "override"),
    "fiz_contra_gateonly": (L["fiz_contra_buy"] == 1, gate_override),
    "fiz_sell_veto(fiz<-10)": (~(L["fiz"] < -10), "veto"),
    "nonres_override(>0)": (L["nonres"] > 0, "override"),
    "nonres_veto(<-5)": (~(L["nonres"] < -5), "veto"),
    "inst_veto(<-20)": (~(L["inst_total"] < -20), "veto"),
    "inst_override(>10)": (L["inst_total"] > 10, "override"),
    "szko_veto(<-10)": (~(L["szko"] < -10), "veto"),
}
print(f"\nвариантов фильтров: {len(F)}")
WIN = {"2022-01..2026-08": ("2022-01-01", "2026-08-31"), "2022-01..2023-12": ("2022-01-01", "2023-12-31"),
       "2024-01..2026-08": ("2024-01-01", "2026-08-31")}
T, tls = run_family(p, pos_m.reindex(me), F, WIN, "panel_m")
# ex-2022
ex = []
for name, (fm, mode) in [("panel_m", (None, None))] + list(F.items()):
    pos = to_daily_position(pos_m.reindex(me) if name == "panel_m" else
                            filtered_monthly_positions(pos_m.reindex(me), fm, mode), p.index)
    bt = backtest(p, pos, "2022-01-01", "2026-08-31")
    bt = bt[~((bt.index >= "2022-01-01") & (bt.index <= "2022-12-31"))]
    d = metrics(bt); d["name"] = name; d["window"] = "2023-01..2026-08"
    ex.append(d)
EX = pd.DataFrame(ex); b0 = EX[EX.name == "panel_m"].iloc[0]
EX["d_sharpe"] = EX["sharpe"] - b0["sharpe"]; EX["d_mdd"] = EX["maxdd"] - b0["maxdd"]
T = pd.concat([T, EX[["window", "name", "cagr", "vol", "sharpe", "sharpe_ex", "maxdd", "tim", "trades_yr",
                      "beat_bh_years", "hit_m", "d_sharpe", "d_mdd"]]], ignore_index=True)
T.to_csv("results/D1_orfr_strat.csv", index=False, float_format="%.4f")
print("\n=== стратегии, главное окно 2022-01..2026-08 ===")
main = T[T.window == "2022-01..2026-08"].sort_values("d_sharpe", ascending=False)
print(main[["name", "cagr", "sharpe", "maxdd", "tim", "d_sharpe", "ci_lo", "ci_hi", "p_le0", "d_mdd", "activity",
            "avoid_mean", "missed_mean"]].round(3).to_string())
print("\n=== d_sharpe по подокнам ===")
print(T.pivot_table(index="name", columns="window", values="d_sharpe").round(2).sort_values("2022-01..2026-08", ascending=False).to_string())
tl = pd.concat([t.assign(rule=k) for k, t in tls.items()])
tl.to_csv("results/D1_orfr_timeliness.csv", index=False)
print(tl[tl.rule.isin(["panel_m", "exhaust_override", "fiz_contra_override", "du_veto(du<-10)"])].to_string())

# --------------------------------------------------------------- плацебо
pl = []
for name, col, mk in [
    ("exhaust_override", "exhaustion", lambda s: (s.reindex(p.index).ffill().reindex(me) == 1, "override")),
    ("fiz_contra_override", "fiz_contra_buy", lambda s: (s.reindex(p.index).ffill().reindex(me) == 1, "override")),
    ("du_veto(du<-10)", "nfo_du", lambda s: (~(s.reindex(p.index).ffill().reindex(me) < -10), "veto")),
    ("fiz_veto(fiz>20)", "fiz", lambda s: (~(s.reindex(p.index).ffill().reindex(me) > 20), "veto")),
]:
    obs, pv, mean_pl, p95 = placebo_family(p, pos_m.reindex(me), mk, O[col].dropna(), WIN["2022-01..2026-08"],
                                           n_placebo=300, block=3)
    pl.append(dict(name=name, d_sharpe_obs=round(obs, 3), p_placebo=round(pv, 3), placebo_mean=round(mean_pl, 3),
                   placebo_p95=round(p95, 3)))
    print(name, pl[-1])
pd.DataFrame(pl).to_csv("results/D1_orfr_placebo.csv", index=False)

# --------------------------------------- события «исчерпание продавца» — список
print("\n=== месяцы с exhaustion=1 (по дате доступности) и fwd63 ===")
f63 = fwd_returns(p, 63).reindex(O.index); f21 = fwd_returns(p, 21).reindex(O.index)
ev = O[O.exhaustion == 1][["nfo_du", "du3m", "du3m_chg"]].copy()
ev["fwd21"] = (f21.reindex(ev.index) * 100).round(1); ev["fwd63"] = (f63.reindex(ev.index) * 100).round(1)
ev["cell"] = p["cell"].reindex(ev.index)
print(ev.to_string())
