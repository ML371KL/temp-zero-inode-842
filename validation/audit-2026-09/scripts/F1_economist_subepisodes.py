"""F1_economist: (1) под-эпизоды с ручными окнами (2011, 2012, 2014×2, 2024, 2025, 2026), (2) лента маркеров 2026,
(3) быстрый месячный long/flat вариантов ворот (справочно; полноценный бэктест — у квантов).
Запуск из audit/: python scripts/F1_economist_subepisodes.py
"""
import sys, os
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
pd.set_option("display.width", 260); pd.set_option("display.max_columns", 40)

D = pd.read_csv("data/panel_prod_daily.csv", parse_dates=["date"]).set_index("date")
M = pd.read_csv("data/panel_prod_monthly.csv", parse_dates=["date"]).set_index("date")
R = pd.read_csv("data/panel_daily.csv", parse_dates=["TRADEDATE"]).set_index("TRADEDATE")
C = pd.read_csv("data/cash_and_tr.csv", parse_dates=["date"]).set_index("date")
TOX = "bear|stress|stress"
px = D.imoex.dropna(); idx = px.index
cell = D.cell.reindex(idx); bits = D[["st_trend", "st_vol", "st_bond"]].reindex(idx)

Mm = M.loc["2004-01-01":].copy()
Mm["pos"] = ((Mm.cell != TOX) & (Mm.composite > 0)).astype(int)

windows = [("2011-03-01", "2011-12-31"), ("2012-03-01", "2012-07-31"), ("2013-01-01", "2013-08-31"),
           ("2014-01-15", "2014-04-30"), ("2014-11-01", "2015-01-31"), ("2024-05-01", "2025-01-31"),
           ("2025-02-01", "2025-05-31"), ("2026-02-01", "2026-09-01")]
rows = []
for a, b in windows:
    seg = px.loc[a:b]
    pk = seg.idxmax(); tr = seg.loc[pk:].idxmin(); depth = px.loc[tr] / px.loc[pk] - 1
    s = cell.loc[pk:tr]; tox = s[s == TOX].index; ft = tox[0] if len(tox) else pd.NaT
    def first_on(col):
        x = bits[col].loc[pk:tr]; on = x[x == (0 if col == "st_trend" else 1)].index
        return on[0] if len(on) else pd.NaT
    done = (px.loc[ft] / px.loc[pk] - 1) / depth if pd.notna(ft) else np.nan
    after = cell.loc[tr:]; non = after[after != TOX].index
    ex = non[0] if (len(non) and pd.notna(ft)) else pd.NaT
    reb = px.loc[ex] / px.loc[tr] - 1 if pd.notna(ex) else np.nan
    mpre = Mm.loc[:pk]; pos_pk = int(mpre.pos.iloc[-1])
    maft = Mm.loc[pk:tr]; fl = maft[maft.pos == 0].index
    first_flat = mpre.index[-1] if pos_pk == 0 else (fl[0] if len(fl) else pd.NaT)
    avoided = np.nan
    if pd.notna(first_flat):
        avoided = 1.0 if first_flat <= pk else max(0.0, (px.loc[tr] / px.asof(first_flat) - 1) / depth)
    mrec = Mm.loc[tr:]; lg = mrec[mrec.pos == 1].index; fl_long = lg[0] if len(lg) else pd.NaT
    miss = px.asof(fl_long) / px.loc[tr] - 1 if pd.notna(fl_long) else np.nan
    rows.append({"peak": pk.date(), "trough": tr.date(), "depth%": round(depth * 100, 1),
                 "trend_off": first_on("st_trend").date() if pd.notna(first_on("st_trend")) else None,
                 "vol_on": first_on("st_vol").date() if pd.notna(first_on("st_vol")) else None,
                 "bond_on": first_on("st_bond").date() if pd.notna(first_on("st_bond")) else None,
                 "bond_already_on_at_peak": int(bits.st_bond.asof(pk) == 1),
                 "first_toxic": ft.date() if pd.notna(ft) else None,
                 "td_peak_to_toxic": (idx.get_loc(ft) - idx.get_loc(pk)) if pd.notna(ft) else None,
                 "fall_done%": round(done * 100) if pd.notna(done) else None,
                 "exit_toxic": ex.date() if pd.notna(ex) else None,
                 "rebound_at_exit%": round(reb * 100, 1) if pd.notna(reb) else None,
                 "rule_pos_at_peak": pos_pk, "rule_first_flat": first_flat.date() if pd.notna(first_flat) else None,
                 "rule_avoided%": round(avoided * 100) if pd.notna(avoided) else None,
                 "rule_first_long": fl_long.date() if pd.notna(fl_long) else None,
                 "rule_missed%": round(miss * 100, 1) if pd.notna(miss) else None})
E = pd.DataFrame(rows)
print("ПОД-ЭПИЗОДЫ (ручные окна):"); print(E.to_string())
E.to_csv("results/F1_economist_subepisodes.csv", index=False)

print("\nЛЕНТА МАРКЕРОВ 2026 (исследовательская панель до 2026-08-10, прод-панель до 2026-09-01)")
dates = ["2025-12-30", "2026-01-30", "2026-02-27", "2026-03-09", "2026-03-31", "2026-04-24", "2026-04-30", "2026-05-29",
         "2026-06-19", "2026-06-29", "2026-07-15", "2026-07-17", "2026-07-24", "2026-08-10", "2026-08-31", "2026-09-01"]
cols_d = ["imoex", "usd", "usd_mom63", "slope_10_2", "urals_rub_gap", "rgbi_dd", "realized_vol_21", "hy_spread", "futoi_z120", "breadth", "switch_spread", "rvi", "key_rate", "cell"]
cols_r = ["y1_minus_key", "rusfar_minus_key", "real_key_saar", "y10_minus_key", "saar3", "orfr_du3m", "futoi_MX_pct"]
rows = []
for d in dates:
    d = pd.Timestamp(d); row = {"date": d.date()}
    for c in cols_d: row[c] = D[c].asof(d) if c in D else None
    for c in cols_r: row[c] = R[c].asof(d) if (c in R and d <= R.index[-1]) else None
    rows.append(row)
T = pd.DataFrame(rows).set_index("date")
print(T.round(3).T.to_string())
T.to_csv("results/F1_economist_timeline_2026.csv")

print("\nБЫСТРЫЙ МЕСЯЧНЫЙ LONG/FLAT (справочно): лонг = MCFTR (IMOEX до появления MCFTR), флэт = mm_rate/12; издержки 0,2% за смену")
Cm = C.resample("ME").last()
mc = Cm.mcftr_ffill; im = M.imoex
# доходность лонга: MCFTR где есть, иначе IMOEX
r_mc = np.log(mc / mc.shift(1)); r_im = np.log(im / im.shift(1)).reindex(r_mc.index)
r_long = r_mc.where(r_mc.notna(), r_im)
r_cash = (Cm.mm_rate / 100 / 12).reindex(r_long.index)
Mx = M.reindex(r_long.index)
def run(pos, start, end, cost=0.002):
    p = pos.reindex(r_long.index).shift(1).loc[start:end]  # решение на закрытии месяца → следующий месяц
    rl, rc = r_long.loc[start:end], r_cash.loc[start:end]
    m = p.notna() & rl.notna() & rc.notna()
    p, rl, rc = p[m], rl[m], rc[m]
    trades = p.diff().abs().fillna(0)
    r = p * rl + (1 - p) * rc - trades * cost
    eq = np.exp(r.cumsum()); n = len(r)
    cagr = np.exp(r.mean() * 12) - 1; vol = r.std() * np.sqrt(12); sharpe = (r.mean() * 12) / vol if vol > 0 else np.nan
    ex = r - rc; sh_ex = (ex.mean() * 12) / (ex.std() * np.sqrt(12)) if ex.std() > 0 else np.nan
    mdd = (eq / eq.cummax() - 1).min()
    return {"CAGR%": round(cagr * 100, 1), "vol%": round(vol * 100, 1), "Sharpe": round(sharpe, 2), "Sharpe_ex_mm": round(sh_ex, 2),
            "MaxDD%": round(mdd * 100, 1), "time_in%": round(p.mean() * 100), "trades/yr": round(trades.sum() / n * 12, 1), "n": n}
tox = (Mx.cell == TOX)
variants = {
    "b&h": pd.Series(1.0, index=r_long.index),
    "cash": pd.Series(0.0, index=r_long.index),
    "panel: gate&comp>0": ((~tox) & (Mx.composite > 0)).astype(float),
    "gate only (not toxic)": (~tox).astype(float),
    "composite>0 only": (Mx.composite > 0).astype(float),
    "bond flag off only": (Mx.st_bond == 0).astype(float),
    "trend on only (MA200)": (Mx.st_trend == 1).astype(float),
    "bond off OR trend on": ((Mx.st_bond == 0) | (Mx.st_trend == 1)).astype(float),
    "gate & (comp>0 or bond off)": ((~tox) & ((Mx.composite > 0) | (Mx.st_bond == 0))).astype(float),
    "not(bond & trend off)": (~((Mx.st_bond == 1) & (Mx.st_trend == 0))).astype(float),
}
for start, end in (("2004-02-28", "2026-08-31"), ("2010-01-31", "2026-08-31"), ("2022-03-31", "2026-08-31")):
    print(f"--- окно {start}..{end}")
    out = pd.DataFrame({k: run(v, start, end) for k, v in variants.items()}).T
    print(out.to_string())
    out.to_csv(f"results/F1_economist_lf_{start[:4]}.csv")
