"""D2_03b: плата за перебор для лучших доп.-выходов из D2_03.
(1) Плацебо: циркулярный сдвиг маски выхода и марковские маски той же доли/длины серий;
(2) walk-forward выбор порога (расширяющееся окно, порог только по прошлому) для семей y1−key и
реальной ставки; (3) split 2015–2019 / 2020–2026; (4) комбинация двух выходов; (5) дневная каденция."""
import numpy as np
import pandas as pd
from D2_lib import *

d = derive_rates(load_daily())
m = monthly_frame(d)
idx = d.index; me = month_ends(idx); me_dates = idx[me]
is_me = np.zeros(len(idx), dtype=bool); is_me[me] = True
comp_ok_d = monthly_to_daily_pos((m["comp_sign"] > 0).astype(float), idx)
toxic_d = d["toxic"].fillna(0.0); base_exit = toxic_d == 1; base_entry = toxic_d == 0
r_long_d = np.log(d["mcftr_ffill"]).diff(); r_cash_d = d["mm_rate"] / 100 / 252
for L in (10, 42, 63):
    d[f"y1_key_d{L}"] = d["y1_key"] - d["y1_key"].shift(L)
for L in (42, 126):
    d[f"real_saar_d{L}"] = d["real_saar"] - d["real_saar"].shift(L)


def build_pos(exit_cond, entry_cond, cadence="monthly"):
    ex = exit_cond.reindex(idx).fillna(False).values.astype(bool); en = entry_cond.reindex(idx).fillna(False).values.astype(bool)
    co = comp_ok_d.values > 0; pos = np.zeros(len(idx)); cur = 0.0
    for i in range(len(idx)):
        if cadence == "daily" or is_me[i]:
            if cur == 1.0 and ((ex[i] and not en[i]) or not co[i]): cur = 0.0
            elif cur == 0.0 and en[i] and co[i]: cur = 1.0
        pos[i] = cur
    return pd.Series(pos, index=idx)


def bt_monthly(pos, a, b, cost=COST):
    pm = pos.iloc[me]; pm.index = me_dates; return run_monthly(pm, m.reindex(me_dates), cost=cost, start=a, end=b)


def bt_daily(pos, a, b, cost=COST):
    df = pd.DataFrame({"pos": pos.shift(1), "r_long": r_long_d, "r_cash": r_cash_d}).dropna()
    df = df[(df.index >= a) & (df.index <= b)]; df["trade"] = df["pos"].diff().abs().fillna(0.0)
    df["ret"] = df["pos"] * df["r_long"] + (1 - df["pos"]) * df["r_cash"] - df["trade"] * cost; return df


def exit_pos(x, cadence="monthly"):
    x = x.fillna(False)
    return build_pos(base_exit | x, base_entry & ~x, cadence)


A, B = "2015-01-01", "2026-08-31"
CANDS = {
    "exit_y1key_d21_gt0.25": d["y1_key_d21"] > 0.25, "exit_y1key_d10_gt0.25": d["y1_key_d10"] > 0.25,
    "exit_y1key_d21_gt0.5": d["y1_key_d21"] > 0.5, "exit_y05key_d21_gt0.25": d["y05_key_d21"] > 0.25,
    "exit_d21_y1_gt0.25": d["d21_y1"] > 0.25, "exit_y2key_d21_gt0.25": d["y2_key_d21"] > 0.25,
    "exit_real_saar_d63_gt1.0": d["real_saar_d63"] > 1.0, "exit_real_saar_d126_gt1": d["real_saar_d126"] > 1.0,
    "exit_real_saar_d42_gt1": d["real_saar_d42"] > 1.0, "exit_real_saar_gt8": d["real_saar"] > 8.0,
    "exit_slope_10_1_neg": d["slope_10_1"] < 0, "exit_ig_d21_gt0.5": d["ig_spread_d21"] > 0.5,
    "combo_y1key0.25_or_realsaar1": (d["y1_key_d21"] > 0.25) | (d["real_saar_d63"] > 1.0),
    "combo_y1key0.25_and_realsaar1": (d["y1_key_d21"] > 0.25) & (d["real_saar_d63"] > 1.0),
}
COLS = {"exit_y1key_d21_gt0.25": "y1_key_d21", "exit_y1key_d10_gt0.25": "y1_key_d10", "exit_y1key_d21_gt0.5": "y1_key_d21",
        "exit_y05key_d21_gt0.25": "y05_key_d21", "exit_d21_y1_gt0.25": "d21_y1", "exit_y2key_d21_gt0.25": "y2_key_d21",
        "exit_real_saar_d63_gt1.0": "real_saar_d63", "exit_real_saar_d126_gt1": "real_saar_d126", "exit_real_saar_d42_gt1": "real_saar_d42",
        "exit_real_saar_gt8": "real_saar", "exit_slope_10_1_neg": "slope_10_1", "exit_ig_d21_gt0.5": "ig_spread_d21",
        "combo_y1key0.25_or_realsaar1": "y1_key_d21", "combo_y1key0.25_and_realsaar1": "y1_key_d21"}
base_pos = build_pos(base_exit, base_entry, "monthly")
base_m = metrics(bt_monthly(base_pos, A, B), freq=12)
print(f"эталон 2015–2026: Шарп {base_m['sharpe']:.3f}, MDD {base_m['maxdd']*100:.1f}%, в рынке {base_m['time_in_mkt']*100:.0f}%")
rng = np.random.default_rng(5)
rows = []
for name, x in CANDS.items():
    av = d[COLS[name]].notna(); span = idx[av & (idx >= A) & (idx <= B)]
    xs = x.reindex(span).fillna(False).values.astype(bool); n = len(xs); on = xs.mean()
    edges = np.diff(np.r_[0, xs.astype(int), 0]); on_runs = np.flatnonzero(edges == -1) - np.flatnonzero(edges == 1)
    mean_on = on_runs.mean() if len(on_runs) else 1.0
    obs_df = bt_monthly(exit_pos(x), A, B); obs = metrics(obs_df, freq=12)
    shifts, blocks, sh_mdd, bl_mdd = [], [], [], []
    for k in range(200):
        s = rng.integers(21, n - 21); xm = pd.Series(False, index=idx); xm.loc[span] = np.roll(xs, s)
        mt = metrics(bt_monthly(exit_pos(xm), A, B), freq=12); shifts.append(mt["sharpe"]); sh_mdd.append(mt["maxdd"])
        p_off = 1.0 / max(mean_on, 1.0); p_on = on * p_off / max(1.0 - on, 1e-9)
        rm = np.zeros(n, dtype=bool); st = rng.random() < on
        for t in range(n):
            rm[t] = st; st = (rng.random() >= p_off) if st else (rng.random() < p_on)
        xm2 = pd.Series(False, index=idx); xm2.loc[span] = rm
        mt = metrics(bt_monthly(exit_pos(xm2), A, B), freq=12); blocks.append(mt["sharpe"]); bl_mdd.append(mt["maxdd"])
    shifts, blocks = np.array(shifts), np.array(blocks)
    # split
    s1 = metrics(bt_monthly(exit_pos(x), "2015-01-01", "2019-12-31"), freq=12); b1 = metrics(bt_monthly(base_pos, "2015-01-01", "2019-12-31"), freq=12)
    s2 = metrics(bt_monthly(exit_pos(x), "2020-01-01", "2026-08-31"), freq=12); b2 = metrics(bt_monthly(base_pos, "2020-01-01", "2026-08-31"), freq=12)
    dd = metrics(bt_daily(exit_pos(x, "daily"), A, B), freq=252); bd = metrics(bt_daily(base_pos, A, B), freq=252)
    rows.append(dict(name=name, on_share=round(on, 3), mean_on_run_days=round(mean_on, 1), sharpe=round(obs["sharpe"], 3),
                     maxdd=round(obs["maxdd"] * 100, 1), tim=round(obs["time_in_mkt"] * 100, 1), trades=round(obs["trades_per_yr"], 2),
                     placebo_shift_sharpe=round(shifts.mean(), 3), p_shift=round((shifts >= obs["sharpe"]).mean(), 3),
                     placebo_shift_q95=round(np.quantile(shifts, 0.95), 3),
                     placebo_markov_sharpe=round(blocks.mean(), 3), p_markov=round((blocks >= obs["sharpe"]).mean(), 3),
                     placebo_markov_q95=round(np.quantile(blocks, 0.95), 3), placebo_mdd_mean=round(np.mean(bl_mdd) * 100, 1),
                     sharpe_2015_19=round(s1["sharpe"], 2), base_2015_19=round(b1["sharpe"], 2), mdd_2015_19=round(s1["maxdd"] * 100, 1),
                     sharpe_2020_26=round(s2["sharpe"], 2), base_2020_26=round(b2["sharpe"], 2), mdd_2020_26=round(s2["maxdd"] * 100, 1),
                     daily_sharpe=round(dd["sharpe"], 3), daily_base=round(bd["sharpe"], 3), daily_mdd=round(dd["maxdd"] * 100, 1), daily_base_mdd=round(bd["maxdd"] * 100, 1),
                     daily_trades=round(dd["trades_per_yr"], 1)))
    print(rows[-1])
res = pd.DataFrame(rows); res.to_csv(RES / "D2_03b_placebo.csv", index=False)

# --- walk-forward выбор порога (расширяющееся окно; первые 3 года — обучение)
print("\n=== walk-forward порога (месячная каденция): порог выбирается по Шарпу на прошлом, применяется на следующий год ===")
def wf(family, grid, first_oos=2018):
    out = []; oos = []
    for yr in range(first_oos, 2027):
        tr_end = f"{yr-1}-12-31"; best, best_sh = None, -9
        for key, x in grid.items():
            sh = metrics(bt_monthly(exit_pos(x), A, tr_end), freq=12)["sharpe"]
            if sh > best_sh: best, best_sh = key, sh
        pos = exit_pos(grid[best]); df = bt_monthly(pos, f"{yr}-01-01", f"{yr}-12-31" if yr < 2026 else "2026-08-31")
        oos.append(df); out.append((yr, best, round(best_sh, 2)))
    oos = pd.concat(oos); mt = metrics(oos, freq=12)
    bdf = bt_monthly(base_pos, f"{first_oos}-01-01", "2026-08-31"); bm = metrics(bdf, freq=12)
    dsh, p, ci = sharpe_diff_bootstrap(oos["ret"], bdf["ret"].reindex(oos.index), n_boot=1500)
    print(f"{family}: выборы по годам {out}")
    print(f"   OOS {first_oos}–2026: Шарп {mt['sharpe']:.2f} (эталон {bm['sharpe']:.2f}), MDD {mt['maxdd']*100:.1f}% (эталон {bm['maxdd']*100:.1f}%), "
          f"CAGR {mt['cagr']*100:.1f}% (эталон {bm['cagr']*100:.1f}%), в рынке {mt['time_in_mkt']*100:.0f}%, Δшарп {dsh:+.2f} p={p:.3f}")
    return dict(family=family, oos_sharpe=mt["sharpe"], base_sharpe=bm["sharpe"], oos_mdd=mt["maxdd"], base_mdd=bm["maxdd"],
                oos_cagr=mt["cagr"], base_cagr=bm["cagr"], tim=mt["time_in_mkt"], d_sharpe=dsh, p=p, choices=str(out))
grid_y1 = {f"L{L}_t{t}": (d[f"y1_key_d{L}"] if L != 21 else d["y1_key_d21"]) > t for L in (10, 21, 42) for t in (0.1, 0.15, 0.25, 0.35, 0.5, 0.75, 1.0)}
grid_rs = {f"L{L}_t{t}": (d[f"real_saar_d{L}"] if L != 63 else d["real_saar_d63"]) > t for L in (42, 63, 126) for t in (0.5, 1.0, 1.5, 2.0, 3.0)}
grid_all = {**{"y1_" + k: v for k, v in grid_y1.items()}, **{"rs_" + k: v for k, v in grid_rs.items()}}
wfr = [wf("y1_key (21 порогов×3 окна)", grid_y1), wf("real_saar (5 порогов×3 окна)", grid_rs), wf("объединённая сетка (36 вариантов)", grid_all)]
pd.DataFrame(wfr).to_csv(RES / "D2_03b_walkforward.csv", index=False)

# --- чувствительность к издержкам для двух главных кандидатов
print("\n=== издержки 0.1/0.2/0.3% (месячная каденция, 2015–2026) ===")
for name in ["exit_y1key_d21_gt0.25", "exit_real_saar_d63_gt1.0", "combo_y1key0.25_or_realsaar1"]:
    for c in (0.001, 0.002, 0.003):
        mt = metrics(bt_monthly(exit_pos(CANDS[name]), A, B, cost=c), freq=12)
        print(f"  {name:32s} cost={c}: Шарп {mt['sharpe']:.2f} CAGR {mt['cagr']*100:.1f}% MDD {mt['maxdd']*100:.1f}%")
