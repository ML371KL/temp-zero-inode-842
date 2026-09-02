"""D2_08: ворота по УРОВНЮ цены ожиданий в направлении, согласованном с IC (+): «слишком много смягчения
заложено» = риск. Выход при y1−ключ < −X; вход только при y1−ключ > −X; то же для y0.5, y2, RUSFAR, 10Y−ключ,
дивдоходность−10Y. Месячная каденция, плацебо сдвиг/марков, split, walk-forward по сетке."""
import numpy as np, pandas as pd
from D2_lib import *
d = derive_rates(load_daily()); m = monthly_frame(d)
idx = d.index; me = month_ends(idx); me_dates = idx[me]; is_me = np.zeros(len(idx), dtype=bool); is_me[me] = True
comp_ok_d = monthly_to_daily_pos((m["comp_sign"] > 0).astype(float), idx)
toxic_d = d["toxic"].fillna(0.0); base_exit = toxic_d == 1; base_entry = toxic_d == 0
def build_pos(exit_cond, entry_cond):
    ex = exit_cond.reindex(idx).fillna(False).values.astype(bool); en = entry_cond.reindex(idx).fillna(False).values.astype(bool)
    co = comp_ok_d.values > 0; pos = np.zeros(len(idx)); cur = 0.0
    for i in range(len(idx)):
        if is_me[i]:
            if cur == 1.0 and ((ex[i] and not en[i]) or not co[i]): cur = 0.0
            elif cur == 0.0 and en[i] and co[i]: cur = 1.0
        pos[i] = cur
    return pd.Series(pos, index=idx)
def bt(pos, a, b): pm = pos.iloc[me]; pm.index = me_dates; return run_monthly(pm, m.reindex(me_dates), cost=COST, start=a, end=b)
def exit_pos(x): x = x.fillna(False); return build_pos(base_exit | x, base_entry & ~x)
def entry_pos(y, col): y = y | d[col].isna(); return build_pos(base_exit, base_entry & y)
A, B = "2015-01-01", "2026-08-31"
base_pos = build_pos(base_exit, base_entry); bm = metrics(bt(base_pos, A, B), freq=12)
rows = []; rng = np.random.default_rng(9)
V = {}
for X in (0.5, 1.0, 1.5, 2.0, 2.5, 3.0):
    V[f"exit_y1key_lt_m{X}"] = ("y1_key", d["y1_key"] < -X, "exit"); V[f"entry_y1key_gt_m{X}"] = ("y1_key", d["y1_key"] > -X, "entry")
for X in (1.0, 2.0): V[f"exit_y05key_lt_m{X}"] = ("y05_key", d["y05_key"] < -X, "exit"); V[f"exit_y2key_lt_m{X}"] = ("y2_key", d["y2_key"] < -X, "exit")
for X in (0.25, 0.5, 1.0): V[f"exit_rusfar_key_lt_m{X}"] = ("rusfar_key", d["rusfar_key"] < -X, "exit")
for X in (0.0, 1.0, 2.0): V[f"exit_y10key_lt_m{X}"] = ("y10_key", d["y10_key"] < -X, "exit")
for X in (4.0, 5.0, 6.0): V[f"exit_erp_lt_m{X}"] = ("erp", d["erp"] < -X, "exit")
for X in (1.0, 2.0): V[f"exit_y1key_lt_m{X}_or_d21"] = ("y1_key", (d["y1_key"] < -X) | (d["y1_key_d21"] > 0.25), "exit")
print(f"эталон 2015–2026: Шарп {bm['sharpe']:.3f} MDD {bm['maxdd']*100:.1f}% TIM {bm['time_in_mkt']*100:.0f}%")
for name, (col, cond, kind) in V.items():
    pos = exit_pos(cond) if kind == "exit" else entry_pos(cond, col)
    df = bt(pos, A, B); mt = metrics(df, freq=12, name=name)
    dsh, p, ci = sharpe_diff_bootstrap(df["ret"], bt(base_pos, A, B)["ret"].reindex(df.index), n_boot=800)
    s1 = metrics(bt(pos, "2015-01-01", "2019-12-31"), freq=12)["sharpe"]; s2 = metrics(bt(pos, "2020-01-01", "2026-08-31"), freq=12)["sharpe"]
    e1 = metrics(bt(pos, "2015-01-01", "2021-12-31"), freq=12); e2 = metrics(bt(pos, "2022-03-01", "2024-12-31"), freq=12); e3 = metrics(bt(pos, "2025-01-01", "2026-08-31"), freq=12)
    dfx = df[df.index.year != 2022]; sx = metrics(dfx, freq=12)["sharpe"]
    r = dict(name=name, kind=kind, cagr=mt["cagr"], sharpe=mt["sharpe"], maxdd=mt["maxdd"], tim=mt["time_in_mkt"], trades=mt["trades_per_yr"],
             d_sharpe=dsh, p_dsharpe=p, sharpe_ex2022=sx, s2015_19=s1, s2020_26=s2, e2015_21=e1["sharpe"], e2022_24=e2["sharpe"], e2025_26=e3["sharpe"], tim2025_26=e3["time_in_mkt"])
    # плацебо для выходов
    if kind == "exit":
        av = d[col].notna(); span = idx[av & (idx >= A) & (idx <= B)]; xs = cond.reindex(span).fillna(False).values.astype(bool); n = len(xs); on = xs.mean()
        if 0 < on < 1:
            edges = np.diff(np.r_[0, xs.astype(int), 0]); on_runs = np.flatnonzero(edges == -1) - np.flatnonzero(edges == 1); mean_on = on_runs.mean()
            sh, bl = [], []
            for k in range(120):
                s_ = rng.integers(21, n - 21); xm = pd.Series(False, index=idx); xm.loc[span] = np.roll(xs, s_); sh.append(metrics(bt(exit_pos(xm), A, B), freq=12)["sharpe"])
                p_off = 1.0 / max(mean_on, 1.0); p_on = on * p_off / max(1 - on, 1e-9); rm = np.zeros(n, dtype=bool); st = rng.random() < on
                for t in range(n): rm[t] = st; st = (rng.random() >= p_off) if st else (rng.random() < p_on)
                xm2 = pd.Series(False, index=idx); xm2.loc[span] = rm; bl.append(metrics(bt(exit_pos(xm2), A, B), freq=12)["sharpe"])
            sh, bl = np.array(sh), np.array(bl)
            r.update(on_share=on, mean_on_run=mean_on, placebo_shift=sh.mean(), p_shift=(sh >= mt["sharpe"]).mean(), placebo_markov=bl.mean(), p_markov=(bl >= mt["sharpe"]).mean())
    rows.append(r); print({k: (round(v, 3) if isinstance(v, float) else v) for k, v in r.items()})
res = pd.DataFrame(rows); res.to_csv(RES / "D2_08_level_gates.csv", index=False)
# walk-forward по сетке уровней y1key (6 порогов выхода)
grid = {f"t{X}": d["y1_key"] < -X for X in (0.5, 1.0, 1.5, 2.0, 2.5, 3.0)}
oos = []; picks = []
for yr in range(2018, 2027):
    best, bs = None, -9
    for k, x in grid.items():
        s_ = metrics(bt(exit_pos(x), A, f"{yr-1}-12-31"), freq=12)["sharpe"]
        if s_ > bs: best, bs = k, s_
    picks.append((yr, best, round(bs, 2))); oos.append(bt(exit_pos(grid[best]), f"{yr}-01-01", f"{yr}-12-31" if yr < 2026 else "2026-08-31"))
oos = pd.concat(oos); mt = metrics(oos, freq=12); bdf = bt(base_pos, "2018-01-01", "2026-08-31"); bmm = metrics(bdf, freq=12)
dsh, p, ci = sharpe_diff_bootstrap(oos["ret"], bdf["ret"].reindex(oos.index), n_boot=1500)
print("walk-forward уровня y1key:", picks); print(f"  OOS 2018–26 Шарп {mt['sharpe']:.2f} (эталон {bmm['sharpe']:.2f}) MDD {mt['maxdd']*100:.1f}% (эталон {bmm['maxdd']*100:.1f}%) CAGR {mt['cagr']*100:.1f}% ({bmm['cagr']*100:.1f}%) TIM {mt['time_in_mkt']*100:.0f}% Δ {dsh:+.2f} p={p:.3f}")
