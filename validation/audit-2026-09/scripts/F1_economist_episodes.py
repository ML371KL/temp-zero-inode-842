"""F1_economist: своевременность ворот и правила панели по эпизодам просадок >15% от 252-дн максимума.
Запуск из audit/: python scripts/F1_economist_episodes.py
"""
import sys, os
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
os.makedirs("results", exist_ok=True)
pd.set_option("display.width", 260); pd.set_option("display.max_columns", 40)

D = pd.read_csv("data/panel_prod_daily.csv", parse_dates=["date"]).set_index("date")
M = pd.read_csv("data/panel_prod_monthly.csv", parse_dates=["date"]).set_index("date")
R = pd.read_csv("data/panel_daily.csv", parse_dates=["TRADEDATE"]).set_index("TRADEDATE")
TOX = "bear|stress|stress"

px = D.imoex.dropna().loc["2003-01-01":]
rmax = px.rolling(252, min_periods=120).max()
dd = px / rmax - 1
cell = D.cell.reindex(px.index)
bits = D[["st_trend", "st_vol", "st_bond"]].reindex(px.index)

# эпизоды: вход при dd<=-15%, пик = последняя дата, где px==rmax до входа; конец = новый 252-дн максимум после дна
episodes, i = [], 0
idx = px.index
while i < len(idx):
    if dd.iloc[i] <= -0.15:
        before = px.iloc[:i]
        peak = before[before == rmax.iloc[:i]].index[-1]
        # дно = минимум до момента, когда px снова == rmax (новый максимум)
        j = i
        while j < len(idx) and px.iloc[j] < rmax.iloc[j]:
            j += 1
        seg = px.iloc[idx.get_loc(peak):j + 1] if j < len(idx) else px.iloc[idx.get_loc(peak):]
        trough = seg.idxmin()
        episodes.append({"peak": peak, "trough": trough, "depth": seg.min() / px.loc[peak] - 1,
                         "end": idx[j] if j < len(idx) else pd.NaT})
        i = j + 1
    else:
        i += 1

# месячное правило панели: pos = (ячейка не токсичная) & (композит>0) на закрытии месяца → следующий месяц
Mm = M.loc["2004-01-01":].copy()
Mm["pos"] = ((Mm.cell != TOX) & (Mm.composite > 0)).astype(int)
Mm["gate"] = (Mm.cell != TOX).astype(int)

rows = []
for ep in episodes:
    pk, tr, end = ep["peak"], ep["trough"], ep["end"]
    if pk < pd.Timestamp("2004-01-01"):
        continue
    seg = cell.loc[pk:tr]
    tox = seg[seg == TOX].index
    first_tox = tox[0] if len(tox) else pd.NaT
    def first_on(col):
        s = bits[col].loc[pk:tr]
        on = s[s == (0 if col == "st_trend" else 1)].index
        return on[0] if len(on) else pd.NaT
    ft, fv, fb = first_on("st_trend"), first_on("st_vol"), first_on("st_bond")
    if pd.notna(first_tox):
        done = (px.loc[first_tox] / px.loc[pk] - 1) / ep["depth"]
        tdays = idx.get_loc(first_tox) - idx.get_loc(pk)
    else:
        done, tdays = np.nan, np.nan
    after = cell.loc[tr:]
    non = after[after != TOX].index
    exit_tox = non[0] if (len(non) and pd.notna(first_tox)) else pd.NaT
    if pd.notna(exit_tox):
        exit_days = idx.get_loc(exit_tox) - idx.get_loc(tr)
        missed = px.loc[exit_tox] / px.loc[tr] - 1
    else:
        exit_days, missed = np.nan, np.nan
    # правило панели (месячное): позиция на закрытии месяца ДО пика; первый флэт после пика; первый лонг после дна
    mpre = Mm.loc[:pk]
    pos_at_peak = int(mpre.pos.iloc[-1]) if len(mpre) else None
    mafter = Mm.loc[pk:tr]
    flat = mafter[mafter.pos == 0].index
    first_flat = flat[0] if len(flat) else pd.NaT
    if pos_at_peak == 0:
        first_flat = mpre.index[-1]
    fall_avoided = np.nan
    if pd.notna(first_flat):
        p_flat = px.asof(first_flat)
        fall_avoided = (px.loc[tr] / p_flat - 1) / ep["depth"] if p_flat > px.loc[tr] else 0.0
        if first_flat <= pk: fall_avoided = 1.0
    mrec = Mm.loc[tr:]
    lng = mrec[mrec.pos == 1].index
    first_long = lng[0] if len(lng) else pd.NaT
    rec_missed = (px.asof(first_long) / px.loc[tr] - 1) if pd.notna(first_long) else np.nan
    rows.append({"peak": pk.date(), "trough": tr.date(), "end": end.date() if pd.notna(end) else None,
                 "depth_pct": round(ep["depth"] * 100, 1),
                 "trend_off": ft.date() if pd.notna(ft) else None, "vol_on": fv.date() if pd.notna(fv) else None,
                 "bond_on": fb.date() if pd.notna(fb) else None,
                 "first_toxic": first_tox.date() if pd.notna(first_tox) else None,
                 "td_peak_to_toxic": tdays, "fall_done_at_toxic_pct": round(done * 100) if pd.notna(done) else None,
                 "exit_toxic": exit_tox.date() if pd.notna(exit_tox) else None, "td_trough_to_exit": exit_days,
                 "rebound_at_exit_pct": round(missed * 100, 1) if pd.notna(missed) else None,
                 "rule_pos_at_peak": pos_at_peak,
                 "rule_first_flat_me": first_flat.date() if pd.notna(first_flat) else None,
                 "rule_fall_avoided_pct": round(fall_avoided * 100) if pd.notna(fall_avoided) else None,
                 "rule_first_long_me": first_long.date() if pd.notna(first_long) else None,
                 "rule_rebound_missed_pct": round(rec_missed * 100, 1) if pd.notna(rec_missed) else None})
E = pd.DataFrame(rows)
print("ЭПИЗОДЫ ПРОСАДОК >15% ОТ 252-ДН МАКСИМУМА (2004+):")
print(E.to_string())
E.to_csv("results/F1_economist_episodes.csv", index=False)

print("\nРАННИЕ МАРКЕРЫ: значения на пике, за 21 торг. день до токсичности, на дате токсичности, на дне")
mk = ["rgbi_dd", "hy_spread", "futoi_z120", "breadth", "dd252", "realized_vol_21", "usd_mom63", "slope_10_2", "switch_spread", "rvi", "key_rate"]
rk = ["y1_minus_key", "rusfar_minus_key", "real_key_saar", "y10_minus_key", "futoi_MX_pct", "orfr_du3m", "saar3"]
rows = []
for _, e in E.iterrows():
    pts = [("peak", e.peak)]
    if e.first_toxic is not None:
        ft = pd.Timestamp(e.first_toxic); k = idx.get_loc(ft)
        pts.append(("toxic-21td", idx[max(0, k - 21)].date()))
        pts.append(("toxic", e.first_toxic))
    pts.append(("trough", e.trough))
    for label, d in pts:
        d = pd.Timestamp(d)
        row = {"episode": str(e.peak), "point": label, "date": d.date(), "imoex": round(px.asof(d), 0)}
        for c in mk:
            if c in D: row[c] = D[c].asof(d)
        for c in rk:
            if c in R: row[c] = R[c].asof(d)
        rows.append(row)
MK = pd.DataFrame(rows)
print(MK.round(3).to_string())
MK.to_csv("results/F1_economist_markers.csv", index=False)

# ложные токсичности: токсичные дни вне эпизодов
tox_days = cell[cell == TOX].index
in_ep = pd.Series(False, index=px.index)
for ep in episodes:
    in_ep.loc[ep["peak"]:(ep["end"] if pd.notna(ep["end"]) else px.index[-1])] = True
print("\nТоксичных дней всего:", len(tox_days), "; вне эпизодов просадок >15%:", int((~in_ep.reindex(tox_days)).sum()))
runs = []
prev = None
for d in tox_days:
    if prev is None or (idx.get_loc(d) - idx.get_loc(prev)) > 5:
        runs.append([d, d])
    else:
        runs[-1][1] = d
    prev = d
print("Отрезки токсичности (старт, конец, дней, доходность индекса за отрезок, за 63 дня после конца):")
for a, b in runs:
    n = idx.get_loc(b) - idx.get_loc(a) + 1
    r = px.loc[b] / px.loc[a] - 1
    k = idx.get_loc(b); r63 = px.iloc[min(k + 63, len(px) - 1)] / px.loc[b] - 1
    print(f"  {a.date()} – {b.date()}  {n:4d} дн  за отрезок {r*100:+6.1f}%  63д после {r63*100:+6.1f}%")
