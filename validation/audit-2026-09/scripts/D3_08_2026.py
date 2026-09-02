"""D3_08 — разбор эпизода 2025–2026 по дням (что и когда сказали бы value-правила), первые срабатывания
кандидатов после дна 17.07.2026, календарь активности правил выхода, эры для вариантов ядра."""
import numpy as np
import pandas as pd
from D3_lib import *

df = load_daily()
px = df["imoex"]
df["ret21"] = np.log(px / px.shift(21))
df["dy_mm"] = df.dy_trail - df.mm_rate
M = monthly_frame(df)
M["dy_mm"] = df.dy_mm.reindex(M.index)
M["tr36"] = np.log(M.mcftr_ffill) - np.log(M.mcftr_ffill).shift(36)
p_tr36 = expanding_pct(-M.tr36, 36)
z60 = rolling_z(M.dy_trail, 60, 24)
p_rb = expanding_pct(M.rb_gap, 36)
val = pd.read_csv(RES / "D3_composite_monthly_series.csv", parse_dates=["date"]).set_index("date")
# дневной перцентиль value-композита — пересчёт как в D3_06
comps_m = {"-dd252": -M.dd252, "dy_mm": M.dy_mm, "-rb_gap": -M.rb_gap}
comps_d = {"-dd252": -df.dd252, "dy_mm": df.dy_mm, "-rb_gap": -df.rb_gap}


def daily_pct(daily, monthly_hist, min_n=36):
    out = pd.Series(np.nan, index=daily.index)
    mh = monthly_hist.dropna()
    for ym, g in daily.groupby(daily.index.to_period("M")):
        hist = np.sort(mh[mh.index < ym.start_time].values)
        if len(hist) < min_n:
            continue
        v = g.values
        ok = np.isfinite(v)
        out.loc[g.index[ok]] = (np.searchsorted(hist, v[ok], "left") + np.searchsorted(hist, v[ok], "right")) / 2.0 / len(hist)
    return out


dp = pd.DataFrame({k: daily_pct(comps_d[k], comps_m[k]) for k in comps_d})
df["val_d"] = dp.mean(axis=1).where(dp.notna().sum(axis=1) >= 2)
df["val_d_pct"] = daily_pct(df.val_d, val["val"])
df["stab_d"] = (df.dd252 < np.log(0.85)) & (df.ret21 > 0)
# дневной z дивдоходности по месячной истории (60 мес до начала месяца)
zz = pd.Series(np.nan, index=df.index)
for ym, g in df.groupby(df.index.to_period("M")):
    hist = M.dy_trail[M.index < ym.start_time].dropna().tail(60)
    if len(hist) >= 24:
        zz.loc[g.index] = ((g.dy_trail - hist.mean()) / hist.std()).clip(-3, 3)
df["dy_z60_d"] = zz

print("=== Эпизод 2025-02-25 → дно 2026-07-17: недельные точки ===")
seg = df.loc["2026-06-01":"2026-09-01", ["imoex", "dd252", "ret21", "cell", "val_d_pct", "dy_z60_d", "rb_gap", "stab_d"]].copy()
seg["dd252"] = 100 * (np.exp(seg.dd252) - 1)
seg["ret21"] = 100 * (np.exp(seg.ret21) - 1)
wk = seg.iloc[::5]
print(fmt(wk, 2))
save(seg.reset_index(), "2026_daily")

trough = pd.Timestamp("2026-07-17")
after = df.loc[trough:]
fires = {
    "stab (dd<-15% & ret21>0), дневн.": after.index[after.stab_d.fillna(False)],
    "val_d_pct>=0.8": after.index[after.val_d_pct >= 0.8],
    "val_d_pct>=0.9": after.index[after.val_d_pct >= 0.9],
    "dd252<-20%": after.index[after.dd252 < np.log(0.8)],
    "dy_z60_d>1": after.index[after.dy_z60_d > 1],
    "снятие ворот (cell != toxic)": after.index[(after.cell != TOXIC) & after.cell.notna()],
}
print("\nПервые срабатывания после дна 17.07.2026 (и сколько раз до 01.09):")
for k, idx in fires.items():
    print(f"  {k:38s} first={idx[0].date() if len(idx) else None}  n_days={len(idx)}  lag={ (df.index.get_loc(idx[0]) - df.index.get_loc(trough)) if len(idx) else None}")
print("Индекс: дно 1898,5 интрадей (закрытие 17.07 %.0f), 10.08 %.0f, 31.08 %.0f" % (df.imoex.loc["2026-07-17"], df.imoex.loc["2026-08-10"], df.imoex.loc["2026-08-31"]))

# то же для двух предыдущих токсичных эпизодов 2024-12 и 2025-10
for tr_date in ("2024-12-17", "2025-10-16", "2022-09-26", "2020-03-18"):
    t = pd.Timestamp(tr_date)
    # локальное дно MCFTR в ±30 дней
    loc = df.loc[t - pd.Timedelta(days=45): t + pd.Timedelta(days=45), "mcftr_ffill"]
    t = loc.idxmin()
    after = df.loc[t:]
    print(f"\nДно {t.date()}:")
    for k, cond in (("stab дневн.", after.stab_d.fillna(False)), ("val_d_pct>=0.8", after.val_d_pct >= 0.8), ("dd252<-20%", after.dd252 < np.log(0.8)),
                    ("снятие ворот", (after.cell != TOXIC) & after.cell.notna()), ("ворота на срезе месяца + ядро>0", pd.Series(False, index=after.index))):
        idx = after.index[cond.values] if k != "ворота на срезе месяца + ядро>0" else M.index[(M.index >= t) & (M.cell != TOXIC) & (M.hyst == 1)]
        print(f"  {k:32s} first={idx[0].date() if len(idx) else None} lag={(df.index.get_loc(idx[0]) - df.index.get_loc(t)) if len(idx) else None}")

# календарь активности правил выхода
print("\n=== Месяцы, когда правила выхода были активны (при открытых воротах и ядре>0) ===")
pos_panel_m = ((M.cell != TOXIC) & M.cell.notna() & (M.hyst == 1))
for nm, cond in (("rb_gap>+0.3", M.rb_gap > 0.3), ("val_pct<0.2", val["val_pct"].reindex(M.index) < 0.2), ("dy_z60<-1", z60 < -1), ("tr36 pct>0.9 (вход)", p_tr36 > 0.9)):
    act = M.index[(cond.fillna(False)) & (M.index >= "2004-01-01")]
    act_panel_long = M.index[(cond.fillna(False)) & pos_panel_m & (M.index >= "2004-01-01")]
    # сжать в отрезки
    def runs(idx):
        out, start, prev = [], None, None
        for d in idx:
            if start is None:
                start = prev = d
            elif (d.to_period("M") - prev.to_period("M")).n > 1:
                out.append(f"{start:%Y-%m}…{prev:%Y-%m}")
                start = prev = d
            else:
                prev = d
        if start is not None:
            out.append(f"{start:%Y-%m}…{prev:%Y-%m}")
        return out
    print(f"{nm}: всего {len(act)} мес, из них при лонге панели {len(act_panel_long)}: {runs(act_panel_long)}")
    fwd = M.fwd_ex_1.reindex(act_panel_long)
    print(f"   средний избыток след. месяца в этих месяцах: {100*fwd.mean():+.2f}% (hit {(fwd>0).mean():.2f}), b&h за те же месяцы {100*M.fwd_tr_1.reindex(act_panel_long).mean():+.2f}%")

# эры для вариантов ядра
R = pd.read_csv(RES / "D3_strat_core_variants.csv")
print("\n=== Варианты ядра по эрам (издержки 0,2%) ===")
r = R[(R.cost == 0.002) & R.window.isin(("era_2010-2021", "era_2022_03-2024", "era_2025-2026", "full_ex2022"))]
print(fmt(r.pivot_table(index="name", columns="window", values=["sharpe", "cagr_pct", "maxdd_pct"]), 2))
R2 = pd.read_csv(RES / "D3_strat_combo.csv")
r2 = R2[(R2.cost == 0.002) & R2.window.isin(("era_2010-2021", "era_2022_03-2024", "era_2025-2026"))]
print(fmt(r2.pivot_table(index="name", columns="window", values=["sharpe", "maxdd_pct"]), 2))
