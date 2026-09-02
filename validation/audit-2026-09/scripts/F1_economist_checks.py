"""F1_economist: быстрые проверки к рецензии (не бэктест).
Запуск из каталога audit/:  python scripts/F1_economist_checks.py
Результаты: results/F1_economist_*.csv + печать.
"""
import sys, os, math
import numpy as np, pandas as pd
from scipy import stats
sys.stdout.reconfigure(encoding="utf-8")
os.makedirs("results", exist_ok=True)

M = pd.read_csv("data/panel_prod_monthly.csv", parse_dates=["date"]).set_index("date")
D = pd.read_csv("data/panel_prod_daily.csv", parse_dates=["date"]).set_index("date")
R = pd.read_csv("data/panel_daily.csv", parse_dates=["TRADEDATE"]).set_index("TRADEDATE")
C = pd.read_csv("data/cash_and_tr.csv", parse_dates=["date"]).set_index("date")

TOX = "bear|stress|stress"

def sp(x, y):
    m = x.notna() & y.notna()
    if m.sum() < 8: return (np.nan, np.nan, int(m.sum()))
    r, p = stats.spearmanr(x[m], y[m])
    return (r, p, int(m.sum()))

print("=" * 80)
print("1. КОРРЕЛЯЦИЯ НОГ ЯДРА (месячные z, 2017-12+, все три ноги)")
z = M.loc["2017-12-01":, ["z_usd_mom63", "z_slope_10_2", "z_urals_rub_gap", "st_vol", "st_bond", "st_trend", "cell", "fwd1m_log", "composite"]].dropna(subset=["z_usd_mom63", "z_slope_10_2", "z_urals_rub_gap"])
print("n =", len(z))
print(z[["z_usd_mom63", "z_slope_10_2", "z_urals_rub_gap"]].corr(method="spearman").round(2))
stress = z[(z.st_vol == 1) | (z.st_bond == 1)]
print("в стрессе (vol=1 или bond=1), n =", len(stress))
print(stress[["z_usd_mom63", "z_slope_10_2", "z_urals_rub_gap"]].corr(method="spearman").round(2))
tox = z[z.cell == TOX]
print("в токсичной ячейке, n =", len(tox))
print(tox[["z_usd_mom63", "z_slope_10_2", "z_urals_rub_gap"]].corr(method="spearman").round(2))
# знак ног в токсичной ячейке
print("Токсичная ячейка: доля месяцев с z_usd>0:", round((tox.z_usd_mom63 > 0).mean(), 2),
      "z_slope>0:", round((tox.z_slope_10_2 > 0).mean(), 2),
      "composite>0:", round((tox.composite > 0).mean(), 2))

print("=" * 80)
print("2. НЕЛИНЕЙНОСТЬ ДЕВАЛЬВАЦИИ: fwd1m по корзинам usd_mom63 (месячно, 2004+)")
m = M.loc["2004-01-01":].dropna(subset=["raw_usd_mom63", "fwd1m_log"]).copy()
bins = [-1, -0.05, 0, 0.05, 0.10, 0.20, 10]
labels = ["<-5%", "-5..0", "0..5", "5..10", "10..20", ">20%"]
m["bin"] = pd.cut(m.raw_usd_mom63, bins=bins, labels=labels)
g = m.groupby("bin", observed=True).fwd1m_log.agg(["mean", "median", "count", lambda s: (s > 0).mean()])
g.columns = ["mean", "median", "n", "hit"]
g[["mean", "median"]] = (g[["mean", "median"]] * 100).round(2)
g["hit"] = g["hit"].round(2)
print(g)
g.to_csv("results/F1_economist_usd_bins.csv")
# то же в токсичной ячейке и вне её
for name, sub in (("токсичная", m[m.cell == TOX]), ("не токсичная", m[m.cell != TOX])):
    gg = sub.groupby("bin", observed=True).fwd1m_log.agg(["mean", "count"])
    gg["mean"] = (gg["mean"] * 100).round(2)
    print(f"-- {name}:"); print(gg.T)
# Спирмен IC usd_mom63 в подвыборках
for name, sub in (("все", m), ("usd_mom63<10%", m[m.raw_usd_mom63 < 0.10]), ("usd_mom63>=10%", m[m.raw_usd_mom63 >= 0.10]),
                  ("токсичная", m[m.cell == TOX]), ("не токсичная", m[m.cell != TOX]), ("2022-03+", m.loc["2022-03-01":]), ("2025+", m.loc["2025-01-01":])):
    r, p, n = sp(sub.raw_usd_mom63, sub.fwd1m_log)
    print(f"IC usd_mom63 [{name}]: {r:+.3f} p={p:.3f} n={n}")

print("=" * 80)
print("3. НАКЛОН КРИВОЙ: разложение и «плохое крутизнение» (месячно, 2015+)")
# месячные срезы из panel_daily (исследовательская панель, y1/y2/y10/key)
Rm = R.resample("ME").last()
Rm["fwd1m"] = np.log(Rm.imoex.shift(-1) / Rm.imoex)
Rm = Rm.loc["2015-01-01":"2026-07-31"]
for col in ["slope_10_2", "y1_minus_key", "y10_minus_key", "rusfar_minus_key", "real_key_saar", "real_key_yoy", "hy_spread", "d_hy_spread21", "breadth_pct200", "futoi_MX_pct", "ig_spread", "d_y1_21"]:
    if col in Rm:
        r, p, n = sp(Rm[col], Rm.fwd1m)
        print(f"IC {col:18s}: {r:+.3f} p={p:.3f} n={n}")
# y2-key
Rm["y2_minus_key"] = Rm["y2.0"] - Rm["key"]
r, p, n = sp(Rm.y2_minus_key, Rm.fwd1m); print(f"IC y2_minus_key     : {r:+.3f} p={p:.3f} n={n}")
# Плохое крутизнение: наклон вырос за 3 мес И y10 вырос за 3 мес
Rm["d_slope3"] = Rm.slope_10_2.diff(3)
Rm["d_y10_3"] = Rm["y10.0"].diff(3)
Rm["d_y2_3"] = Rm["y2.0"].diff(3)
bad = Rm[(Rm.d_slope3 > 0) & (Rm.d_y10_3 > 0)]
good = Rm[(Rm.d_slope3 > 0) & (Rm.d_y10_3 <= 0)]
flat = Rm[(Rm.d_slope3 <= 0)]
for name, sub in (("плохое крутизнение (наклон↑, y10↑)", bad), ("хорошее крутизнение (наклон↑, y10↓)", good), ("уплощение", flat)):
    r, p, n = sp(sub.slope_10_2, sub.fwd1m)
    print(f"{name:40s}: n={n:3d} fwd1m mean={sub.fwd1m.mean()*100:+.2f}% hit={(sub.fwd1m>0).mean():.2f}  IC(slope)={r:+.3f} p={p:.3f}")
# 2026 помесячно
print("2026 помесячно: key, y1, y2, y10, slope, y10-key, y1-key")
print(Rm.loc["2025-10-01":, ["key", "y1.0", "y2.0", "y10.0", "slope_10_2", "y10_minus_key", "y1_minus_key", "rusfar_minus_key", "fwd1m"]].round(2))
# IC наклона по эрам
for name, sub in (("2015-2021", Rm.loc[:"2022-02-28"]), ("2022-03..2024", Rm.loc["2022-03-01":"2024-12-31"]), ("2025+", Rm.loc["2025-01-01":])):
    r, p, n = sp(sub.slope_10_2, sub.fwd1m); r2, p2, _ = sp(sub.y10_minus_key, sub.fwd1m); r3, p3, _ = sp(sub.y1_minus_key, sub.fwd1m)
    print(f"[{name}] IC slope {r:+.3f} (p={p:.2f}) | y10-key {r2:+.3f} (p={p2:.2f}) | y1-key {r3:+.3f} (p={p3:.2f}) n={n}")

print("=" * 80)
print("4. СВОЕВРЕМЕННОСТЬ ТОКСИЧНОЙ ЯЧЕЙКИ: эпизоды просадок >15% (дневные данные)")
px = D.imoox if "imoox" in D else D.imoex
px = px.dropna()
cell = D.cell.reindex(px.index)
bits = D[["st_trend", "st_vol", "st_bond"]].reindex(px.index)
# эпизоды: скользящий максимум, просадка <-15%
roll_max = px.cummax()
dd = px / roll_max - 1
episodes = []
in_ep = False
for i, (d, v) in enumerate(dd.items()):
    if not in_ep and v <= -0.15:
        # найти пик = последняя дата где dd==0 до i
        peak_idx = dd.iloc[:i][dd.iloc[:i] == 0].index[-1]
        in_ep = True; ep = {"peak": peak_idx, "start15": d}
    elif in_ep and v == 0:
        seg = px.loc[ep["peak"]:d]
        ep["trough"] = seg.idxmin(); ep["depth"] = seg.min() / seg.iloc[0] - 1; ep["recovered"] = d
        episodes.append(ep); in_ep = False
if in_ep:
    seg = px.loc[ep["peak"]:]
    ep["trough"] = seg.idxmin(); ep["depth"] = seg.min() / seg.iloc[0] - 1; ep["recovered"] = pd.NaT
    episodes.append(ep)
rows = []
for ep in episodes:
    if ep["peak"] < pd.Timestamp("2004-01-01"): continue
    pk, tr = ep["peak"], ep["trough"]
    seg = cell.loc[pk:tr]
    tox_dates = seg[seg == TOX].index
    first_tox = tox_dates[0] if len(tox_dates) else pd.NaT
    # первое включение каждого бита после пика
    def first_on(col):
        s = bits[col].loc[pk:tr]
        on = s[s == (0 if col == "st_trend" else 1)].index
        return on[0] if len(on) else pd.NaT
    ft, fv, fb = first_on("st_trend"), first_on("st_vol"), first_on("st_bond")
    # доля падения, уже случившаяся к моменту токсичности
    if pd.notna(first_tox):
        done = (px.loc[first_tox] / px.loc[pk] - 1) / ep["depth"]
        tdays = px.index.get_loc(first_tox) - px.index.get_loc(pk)
    else:
        done, tdays = np.nan, np.nan
    # выход из токсичности после дна
    after = cell.loc[tr:]
    non = after[after != TOX].index
    exit_tox = non[0] if len(non) and pd.notna(first_tox) else pd.NaT
    if pd.notna(exit_tox):
        exit_days = px.index.get_loc(exit_tox) - px.index.get_loc(tr)
        missed = px.loc[exit_tox] / px.loc[tr] - 1
    else:
        exit_days, missed = np.nan, np.nan
    # была ли ячейка токсичной уже ДО пика (ложная тревога/раннее)
    pre = cell.loc[:pk].iloc[-120:]
    pre_tox = (pre == TOX).sum()
    rows.append({"peak": pk.date(), "trough": tr.date(), "depth_pct": round(ep["depth"] * 100, 1),
                 "first_trend_off": ft.date() if pd.notna(ft) else None,
                 "first_vol_on": fv.date() if pd.notna(fv) else None,
                 "first_bond_on": fb.date() if pd.notna(fb) else None,
                 "first_toxic": first_tox.date() if pd.notna(first_tox) else None,
                 "tdays_peak_to_toxic": tdays, "share_of_fall_done_pct": round(done * 100, 0) if pd.notna(done) else None,
                 "exit_toxic": exit_tox.date() if pd.notna(exit_tox) else None,
                 "tdays_trough_to_exit": exit_days, "recovery_missed_pct": round(missed * 100, 1) if pd.notna(missed) else None,
                 "toxic_days_in_120d_before_peak": int(pre_tox)})
E = pd.DataFrame(rows)
pd.set_option("display.width", 250); pd.set_option("display.max_columns", 30)
print(E.to_string())
E.to_csv("results/F1_economist_episodes.csv", index=False)

print("=" * 80)
print("5. КОМПОЗИТ ВНУТРИ ЯЧЕЕК: fwd1m по знаку композита (месячно, 2004+)")
mm = M.loc["2004-01-01":].dropna(subset=["composite", "fwd1m_log", "cell"]).copy()
mm["csign"] = np.where(mm.composite > 0, "+", "-")
t = mm.groupby(["cell", "csign"]).fwd1m_log.agg(["mean", "median", "count", lambda s: (s > 0).mean()])
t.columns = ["mean%", "median%", "n", "hit"]
t[["mean%", "median%"]] = (t[["mean%", "median%"]] * 100).round(2); t["hit"] = t["hit"].round(2)
print(t)
t.to_csv("results/F1_economist_cell_by_sign.csv")
for c in [TOX, "bear|stress|ok", "bull|stress|ok", "bull|calm|ok"]:
    sub = mm[mm.cell == c]; r, p, n = sp(sub.composite, sub.fwd1m_log)
    print(f"IC композита внутри [{c}]: {r:+.3f} p={p:.3f} n={n}")
# Композит в токсичных месяцах-обвалах
print("Композит и usd_mom63 в худших токсичных месяцах:")
worst = mm[mm.cell == TOX].nsmallest(6, "fwd1m_log")[["fwd1m_log", "composite", "raw_usd_mom63", "z_usd_mom63", "z_slope_10_2", "z_urals_rub_gap"]]
worst["fwd1m_log"] = (worst.fwd1m_log * 100).round(1)
print(worst.round(2))

print("=" * 80)
print("6. РАННИЕ МАРКЕРЫ ПЕРЕД ТОКСИЧНОСТЬЮ: значения на пике и на дате токсичности")
mk = ["rgbi_dd", "hy_spread", "futoi_z120", "breadth", "dd252", "realized_vol_21", "usd_mom63", "urals_rub_gap", "slope_10_2", "switch_spread", "rvi"]
rk = ["y1_minus_key", "rusfar_minus_key", "real_key_saar", "y10_minus_key", "futoi_MX_pct", "orfr_du3m"]
rows = []
for _, e in E.iterrows():
    for label, d in (("peak", e.peak), ("toxic", e.first_toxic), ("trough", e.trough)):
        if d is None: continue
        d = pd.Timestamp(d)
        row = {"episode": str(e.peak), "point": label, "date": d.date()}
        for c in mk:
            if c in D: row[c] = D[c].asof(d)
        for c in rk:
            if c in R: row[c] = R[c].asof(d)
        rows.append(row)
MK = pd.DataFrame(rows)
print(MK.round(3).to_string())
MK.to_csv("results/F1_economist_markers.csv", index=False)

print("=" * 80)
print("7. ПРАВИЛО ПАНЕЛИ ПО МЕСЯЦАМ В ЭПИЗОДАХ: позиция = (ячейка не токсичная) & (композит>0), решение на закрытии месяца → следующий месяц")
mm2 = M.loc["2004-01-01":].copy()
mm2["pos"] = ((mm2.cell != TOX) & (mm2.composite > 0)).astype(int)
mm2["pos_gate_only"] = (mm2.cell != TOX).astype(int)
mm2["chg"] = mm2.pos.diff()
for a, b in (("2008-01-01", "2009-12-31"), ("2011-03-01", "2012-06-30"), ("2014-01-01", "2015-06-30"), ("2017-01-01", "2017-12-31"), ("2020-01-01", "2020-12-31"), ("2021-09-01", "2023-03-31"), ("2024-03-01", "2025-06-30"), ("2026-01-01", "2026-09-01")):
    sub = mm2.loc[a:b, ["imoex", "fwd1m_log", "composite", "cell", "pos"]]
    sub = sub.assign(fwd1m_pct=(sub.fwd1m_log * 100).round(1)).drop(columns="fwd1m_log")
    print(f"--- {a}..{b}"); print(sub.round(2).to_string())
mm2[["imoex", "fwd1m_log", "composite", "cell", "pos", "pos_gate_only"]].to_csv("results/F1_economist_rule_positions.csv")

print("=" * 80)
print("8. СКОЛЬКО ВРЕМЕНИ ВОРОТА ЗАКРЫТЫ и что рынок делал в это время (2004+, месяцы)")
tx = mm[mm.cell == TOX]
print("токсичных месяцев:", len(tx), "из", len(mm), f"({len(tx)/len(mm):.0%})")
print("средний fwd1m в токсичной:", round(tx.fwd1m_log.mean() * 100, 2), "медиана", round(tx.fwd1m_log.median() * 100, 2))
print("fwd1m токсичной без 4 худших:", round(tx.fwd1m_log.drop(tx.fwd1m_log.nsmallest(4).index).mean() * 100, 2))
# Сколько токсичных месяцев пришлись на первые 3 месяца после дна (пропуск отскока)?
print("Токсичные месяцы с fwd1m>+5%:", (tx.fwd1m_log > 0.05).sum(), "; сумма их лог-доходностей:", round(tx.fwd1m_log[tx.fwd1m_log > 0.05].sum() * 100, 1))
print("Токсичные месяцы с fwd1m<-5%:", (tx.fwd1m_log < -0.05).sum(), "; сумма:", round(tx.fwd1m_log[tx.fwd1m_log < -0.05].sum() * 100, 1))
print("Даты токсичных месяцев:", [d.strftime("%Y-%m") for d in tx.index])
