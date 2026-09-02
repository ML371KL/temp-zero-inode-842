"""D1: FUTOI физлиц во фьючерсе MX (с 2020-06) — IC по конструкциям/лагам/состояниям и
стратегии «правило панели + futoi-фильтр». Выход: results/D1_futoi_ic.csv,
results/D1_futoi_strat_m.csv, results/D1_futoi_strat_d.csv, results/D1_futoi_lag.csv,
results/D1_futoi_placebo.csv, results/D1_futoi_timeliness.csv."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from D1_common import *

p = load_daily(); m = load_monthly(); r = load_raw()
base = pd.read_csv("results/D1_baseline_positions.csv", parse_dates=["date"], index_col="date")
pos_m, gate_m, slope_m = panel_rule_monthly(m)
pos_dgate = base["pos_panel_dgate"]

# --------------------------------------------------------------- сырьё и конструкции
f = r[r.series.str.startswith("futoi_mx")].pivot(index="date", columns="series", values="value")
f = f.reindex(p.index).ffill(limit=3)
long_, short_, pos_ = f["futoi_mx_long"], f["futoi_mx_short"], f["futoi_mx_pos"]
hl, hs = f["futoi_mx_holders_long"], f["futoi_mx_holders_short"]
gross = long_.abs() + short_.abs()
ratio = pos_ / gross                                   # нетто/брутто (как панель)
S = {}
S["z120"] = zscore(ratio, 120, 60)                      # ровно как в проде
S["z60"] = zscore(ratio, 60, 30)
S["z252"] = zscore(ratio, 252, 120)
S["pct252"] = pct_rank(ratio, 252, 120)                 # сломанная нормировка каталога
S["ratio_raw"] = ratio                                  # уровень
S["ratio_detr120"] = ratio - ratio.rolling(120, min_periods=60).mean()
S["net_z120"] = zscore(pos_, 120, 60)                   # нетто в контрактах
S["net_rel"] = pos_ / gross.rolling(126, min_periods=60).mean()
S["d5_ratio"] = ratio - ratio.shift(5)
S["d21_ratio"] = ratio - ratio.shift(21)
S["d21_net_rel"] = (pos_ - pos_.shift(21)) / gross.rolling(21, min_periods=10).mean()
lhr = np.log(hl / hs)
S["holders_lr"] = lhr                                   # лог отношения числа лонгистов к шортистам
S["holders_lr_z120"] = zscore(lhr, 120, 60)
S["d21_holders_lr"] = lhr - lhr.shift(21)
S["holders_share_long"] = hl / (hl + hs)
S["avg_long_z120"] = zscore(np.log(long_ / hl), 120, 60)      # средний лонг на держателя
S["avg_short_z120"] = zscore(np.log(short_.abs() / hs), 120, 60)
S["gross_z120"] = zscore(np.log(gross), 120, 60)            # вовлечённость физиков (брутто)
S["holders_total_z120"] = zscore(np.log(hl + hs), 120, 60)
sig = pd.DataFrame(S)
sig.to_csv("results/D1_futoi_signals.csv", float_format="%.5f")

A, B = "2020-07-01", "2026-08-31"
splits = state_splits(p, A, B)
splits["2020-07..2023"] = pd.Series((p.index >= A) & (p.index <= "2023-12-31"), index=p.index)
splits["2024..2026"] = pd.Series((p.index >= "2024-01-01") & (p.index <= B), index=p.index)
splits["ex2022"] = splits["all"] & ~pd.Series((p.index >= "2022-01-01") & (p.index <= "2022-12-31"), index=p.index)

# ------------------------------------------------------------------------- IC
rows = []
for name in sig.columns:
    for L in ([0, 5, 10, 14] if name in ("z120", "ratio_detr120", "d21_ratio", "holders_lr_z120") else [0]):
        s = sig[name].shift(L)
        for row in ic_battery(p, s, name, splits):
            row["lag"] = L
            rows.append(row)
IC = pd.DataFrame(rows)
IC = IC[["signal", "lag", "H", "split", "n", "ic", "p_sp", "nw_t", "p_boot", "terc_hi", "terc_lo"]]
IC.to_csv("results/D1_futoi_ic.csv", index=False)
print("=== IC (lag 0, all/split), H=21/63 ===")
print(IC[(IC.lag == 0) & (IC.split.isin(["all", "bull", "bear", "stress", "toxic", "2020-07..2023", "2024..2026", "ex2022"]))]
      .pivot_table(index=["signal", "H"], columns="split", values="ic").round(2).to_string())
print("\n=== z120: IC by lag ===")
print(IC[(IC.signal == "z120") & (IC.split.isin(["all", "2020-07..2023", "2024..2026"]))]
      .pivot_table(index=["H", "split"], columns="lag", values="ic").round(3).to_string())
print("n by split (H=21):"); print(IC[(IC.signal == "z120") & (IC.lag == 0) & (IC.H == 21)][["split", "n", "p_boot", "nw_t"]].to_string())

# ------------------------------------------------------- стратегии: месячное правило
me = pd.Index(month_ends(p.index))
me = me[(me >= "2020-06-30")]
Sm = sig.reindex(me)
dd_m = p["dd252"].reindex(me)


def gate_override(base_m, fm):
    """Фильтр открывает ТОЛЬКО ворота: лонг = (ворота | фильтр) & наклон."""
    g = gate_m.reindex(base_m.index).astype(bool) | fm.fillna(False).astype(bool)
    return (g & slope_m.reindex(base_m.index).astype(bool)).astype(float)


def build_filters(Sm, dd_m):
    F = {}
    for thr in (0.5, 1.0, 1.5):
        F[f"crowd_veto_z>{thr}"] = (Sm["z120"] <= thr, "veto")
    for thr in (-1.0, -1.5, -2.0):
        F[f"capit_override_z<{thr}"] = (Sm["z120"] <= thr, "override")
        F[f"capit_gateonly_z<{thr}"] = (Sm["z120"] <= thr, gate_override)
    F["capit_dd_override(z<-1.5&dd<-15%)"] = ((Sm["z120"] <= -1.5) & (dd_m < -0.15), "override")
    F["confirm_veto(z<0)"] = (Sm["z120"] < 0, "veto")
    F["trend_confirm_veto(z>0)"] = (Sm["z120"] > 0, "veto")          # «подтверждение»: физики наращивают
    for thr in (0.05, 0.10):
        F[f"d21_veto(d21>{thr})"] = (Sm["d21_ratio"] <= thr, "veto")
    F["d21_override(d21<-0.10)"] = (Sm["d21_ratio"] <= -0.10, "override")
    F["holders_veto(zlr>1)"] = (Sm["holders_lr_z120"] <= 1.0, "veto")
    F["holders_override(zlr<-1.5)"] = (Sm["holders_lr_z120"] <= -1.5, "override")
    F["level_veto(ratio>0.5)"] = (Sm["ratio_raw"] <= 0.5, "veto")
    F["pct252_veto(>0.9)"] = (Sm["pct252"] <= 0.9, "veto")
    F["z60_crowd_veto(>1)"] = (Sm["z60"] <= 1.0, "veto")
    F["z60_capit_override(<-1.5)"] = (Sm["z60"] <= -1.5, "override")
    F["gross_veto(z>1)"] = (Sm["gross_z120"] <= 1.0, "veto")          # рост вовлечённости = риск
    F["gross_override(z>1&dd<-15%)"] = ((Sm["gross_z120"] > 1.0) & (dd_m < -0.15), "override")
    return F


WIN = {"2020-07..2026-08": ("2020-07-01", "2026-08-31"), "2020-07..2023-12": ("2020-07-01", "2023-12-31"),
       "2024-01..2026-08": ("2024-01-01", "2026-08-31")}
F = build_filters(Sm, dd_m)
print(f"\nвариантов месячных фильтров: {len(F)}")
T, tls = run_family(p, pos_m.reindex(me), F, WIN, "panel_m")
# ex-2022 для основного окна
ex = []
for name, (fm, mode) in [("panel_m", (None, None))] + list(F.items()):
    if name == "panel_m":
        pos = to_daily_position(pos_m.reindex(me), p.index)
    else:
        pos = to_daily_position(filtered_monthly_positions(pos_m.reindex(me), fm, mode), p.index)
    bt = backtest(p, pos, "2020-07-01", "2026-08-31")
    bt = bt[~((bt.index >= "2022-01-01") & (bt.index <= "2022-12-31"))]
    d = metrics(bt); d["name"] = name; d["window"] = "2020-07..2026 ex2022"
    ex.append(d)
EX = pd.DataFrame(ex)
b0 = EX[EX.name == "panel_m"].iloc[0]
EX["d_sharpe"] = EX["sharpe"] - b0["sharpe"]; EX["d_mdd"] = EX["maxdd"] - b0["maxdd"]
T = pd.concat([T, EX[["window", "name", "cagr", "vol", "sharpe", "sharpe_ex", "maxdd", "tim", "trades_yr",
                      "beat_bh_years", "hit_m", "d_sharpe", "d_mdd"]]], ignore_index=True)
T.to_csv("results/D1_futoi_strat_m.csv", index=False, float_format="%.4f")
print("\n=== стратегии (месячное правило) — главное окно ===")
main = T[T.window == "2020-07..2026-08"].sort_values("d_sharpe", ascending=False)
print(main[["name", "cagr", "sharpe", "maxdd", "tim", "d_sharpe", "ci_lo", "ci_hi", "p_le0", "d_mdd", "activity",
            "avoid_mean", "missed_mean"]].round(3).to_string())
print("\n=== устойчивость по подокнам (d_sharpe) ===")
print(T.pivot_table(index="name", columns="window", values="d_sharpe").round(2).sort_values("2020-07..2026-08", ascending=False).to_string())

# ----------------------------------------------- лаг: 0/5/10/14 торговых дней, ключевые варианты
lag_rows = []
for L in (0, 5, 10, 14):
    SmL = sig.shift(L).reindex(me)
    FL = {k: v for k, v in build_filters(SmL, dd_m).items()
          if k in ("crowd_veto_z>1.0", "capit_override_z<-1.5", "capit_gateonly_z<-1.5",
                   "capit_dd_override(z<-1.5&dd<-15%)", "confirm_veto(z<0)", "d21_veto(d21>0.05)",
                   "holders_override(zlr<-1.5)", "z60_capit_override(<-1.5)")}
    TL, _ = run_family(p, pos_m.reindex(me), FL, {"2020-07..2026-08": WIN["2020-07..2026-08"]}, "panel_m", boot=False)
    TL["lag"] = L
    lag_rows.append(TL)
LAG = pd.concat(lag_rows)
LAG.to_csv("results/D1_futoi_lag.csv", index=False, float_format="%.4f")
print("\n=== цена лага (месячное правило, d_sharpe по лагу) ===")
print(LAG.pivot_table(index="name", columns="lag", values="d_sharpe").round(2).to_string())
print(LAG.pivot_table(index="name", columns="lag", values="maxdd").round(3).to_string())

# ------------------------------------------------- дневной вариант (ворота дневные), с лагами
drows = []
for L in (0, 5, 10, 14):
    sL = sig.shift(L)
    FD = {
        f"crowd_veto_z>1.0": (sL["z120"] <= 1.0, "veto"),
        f"capit_override_z<-1.5": (sL["z120"] <= -1.5, "override"),
        f"capit_override_z<-2.0": (sL["z120"] <= -2.0, "override"),
        f"capit_dd_override(z<-1.5&dd<-15%)": ((sL["z120"] <= -1.5) & (p["dd252"] < -0.15), "override"),
        f"confirm_veto(z<0)": (sL["z120"] < 0, "veto"),
        f"holders_override(zlr<-1.5)": (sL["holders_lr_z120"] <= -1.5, "override"),
        f"z60_capit_override(<-1.5)": (sL["z60"] <= -1.5, "override"),
    }
    TD, tld = run_family(p, pos_dgate, FD, WIN, "panel_dgate", daily=True, boot=(L == 0))
    TD["lag"] = L
    drows.append(TD)
    if L == 0:
        tl_all = pd.concat([t.assign(rule=k) for k, t in tld.items()])
DS = pd.concat(drows)
DS.to_csv("results/D1_futoi_strat_d.csv", index=False, float_format="%.4f")
print("\n=== дневные ворота + дневной futoi-фильтр, главное окно, лаг 0 ===")
d0 = DS[(DS.window == "2020-07..2026-08") & (DS.lag == 0)]
print(d0[["name", "cagr", "sharpe", "maxdd", "tim", "trades_yr", "d_sharpe", "ci_lo", "ci_hi", "p_le0", "d_mdd",
          "activity", "avoid_mean", "missed_mean", "entry_days_med"]].round(3).to_string())
print("\n=== дневной: d_sharpe по лагу ===")
print(DS[DS.window == "2020-07..2026-08"].pivot_table(index="name", columns="lag", values="d_sharpe").round(2).to_string())
print(DS.pivot_table(index="name", columns=["window", "lag"], values="d_sharpe").round(2).to_string())

# ---------------------------------------------------------------- своевременность
tl_m = pd.concat([t.assign(rule=k) for k, t in tls.items()])
tl = pd.concat([tl_m.assign(mode="monthly"), tl_all.assign(mode="daily")])
tl.to_csv("results/D1_futoi_timeliness.csv", index=False)
print("\n=== своевременность (эпизоды >15% в 2020-07..2026-08) ===")
print(tl[tl.rule.isin(["panel_m", "panel_dgate", "capit_override_z<-1.5", "capit_dd_override(z<-1.5&dd<-15%)",
                       "crowd_veto_z>1.0"])].to_string())

# -------------------------------------------------------------------- плацебо
pl_rows = []
for name, mk, daily in [
    ("m:capit_override_z<-1.5", lambda s: (zscore(s, 120, 60).reindex(me) <= -1.5, "override"), False),
    ("m:crowd_veto_z>1.0", lambda s: (zscore(s, 120, 60).reindex(me) <= 1.0, "veto"), False),
    ("m:confirm_veto(z<0)", lambda s: (zscore(s, 120, 60).reindex(me) < 0, "veto"), False),
    ("d:capit_override_z<-1.5", lambda s: (zscore(s, 120, 60) <= -1.5, "override"), True),
    ("d:crowd_veto_z>1.0", lambda s: (zscore(s, 120, 60) <= 1.0, "veto"), True),
]:
    basep = pos_dgate if daily else pos_m.reindex(me)
    obs, pv, mean_pl, p95 = placebo_family(p, basep, mk, ratio, WIN["2020-07..2026-08"], n_placebo=300,
                                           block=21, daily=daily)
    pl_rows.append(dict(name=name, d_sharpe_obs=round(obs, 3), p_placebo=round(pv, 3),
                        placebo_mean=round(mean_pl, 3), placebo_p95=round(p95, 3)))
    print(name, pl_rows[-1])
pd.DataFrame(pl_rows).to_csv("results/D1_futoi_placebo.csv", index=False)

# ------------------------------------------ что показывал z120 у важных точек
print("\n=== z120 у ключевых дат ===")
for d in ["2020-10-30", "2021-10-20", "2022-02-18", "2022-09-26", "2022-10-10", "2023-09-01", "2024-05-17",
          "2024-12-17", "2025-02-25", "2026-03-09", "2026-07-17", "2026-07-31", "2026-08-31"]:
    dd = pd.Timestamp(d)
    if dd in sig.index:
        print(d, "ratio", round(ratio.loc[dd], 3), "z120", round(sig.loc[dd, "z120"], 2), "z60", round(sig.loc[dd, "z60"], 2),
              "holders_lr", round(sig.loc[dd, "holders_lr"], 2), "gross_z", round(sig.loc[dd, "gross_z120"], 2))
