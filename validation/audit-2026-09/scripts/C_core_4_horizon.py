"""C_core задача 4 — горизонт: IC композита на fwd 1/2/3/6 мес; профиль IC по лагу (t+k) и полураспад;
недельный шаг: дневной композит как в проде (z текущего значения против 59 прошлых месячных срезов),
IC на 1/2/4/8/13/26 нед, профиль по неделям; стратегия с недельным шагом против месячной."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from C_core_lib import *

D, M, C = load()
me = month_end_idx(D)
mk = monthly_market(D, C, me)
Mm = M.reindex(me)
comp = Mm["composite"]
f1 = mk["fwd_imoex"]
gate_m = (Mm["cell"] != TOXIC).astype(float)

# ------------------------------------------------ 4а. месячный горизонт
print("=== IC композита к fwd h месяцев (перекрывающиеся суммы; NW lag=h, бутстреп блок 2h+6) ===")
rows = []
for sname, (a, b) in {"MAIN": MAIN, "common 2016-02+": ("2016-02-01", "2026-08-31"), "2010-2021": ERAS["2010-2021"], "2022+": ("2022-03-24", "2026-08-31")}.items():
    m = (comp.index >= a) & (comp.index <= b)
    line = f"{sname:16s}"
    for h in (1, 2, 3, 6, 12):
        fh = sum(f1.shift(-k) for k in range(h))
        r = ic_stats(comp[m], fh[m], lag=h, block=2 * h + 6, n_boot=600)
        rows.append(dict(sample=sname, horizon_m=h, n=r["n"], ic=r["ic"], nw_t=r["nw_t"], p_boot=r["p_boot"]))
        line += f"  h={h:2d}: {r['ic']:+.3f} (t={r['nw_t']:+.2f}, n={r['n']})"
    print(line)
pd.DataFrame(rows).to_csv(f"{RES}/C_core_4_horizon_monthly.csv", index=False, float_format="%.4f")

print("\n=== профиль IC по лагу: composite(t) против fwd1m(t+k), k=0..11 (MAIN, n≈200) ===")
rows = []
m = (comp.index >= MAIN[0]) & (comp.index <= MAIN[1])
prof = []
for k in range(0, 12):
    r = ic_stats(comp[m], f1.shift(-k)[m], lag=1, n_boot=400)
    prof.append(r["ic"])
    rows.append(dict(lag_m=k, ic=r["ic"], nw_t=r["nw_t"], p_boot=r["p_boot"], n=r["n"]))
    print(f"k={k:2d}: IC={r['ic']:+.3f} t={r['nw_t']:+.2f}")
pd.DataFrame(rows).to_csv(f"{RES}/C_core_4_lag_profile_monthly.csv", index=False, float_format="%.4f")
ar1 = comp.loc[MAIN[0]:MAIN[1]].autocorr(1)
print(f"AR(1) месячного композита = {ar1:.3f} → полураспад самого сигнала {np.log(0.5)/np.log(ar1):.1f} мес")
half = next((k for k, v in enumerate(prof) if v < 0.5 * prof[0]), None)
print(f"полураспад IC (первый k, где IC(k) < ½·IC(0)): {half} мес; сумма IC k=0..5 = {sum(prof[:6]):+.2f}, k=6..11 = {sum(prof[6:]):+.2f}")

# ------------------------------------------------ 4б. дневной композит как в проде
RAWd = pd.DataFrame({k: D[k] for k, _ in LEGS})
RAWm = pd.DataFrame({k: Mm["raw_" + k] for k, _ in LEGS})
month_of = D.index.to_period("M")
me_month = me.to_period("M")


def daily_z_prod(k, w=60, mp=24, clip=3.0):
    """z дня t = (raw_t − mean)/std по окну из (w−1) ПРОШЛЫХ месячных срезов + raw_t (как monthly_frame с незавершённым месяцем)."""
    mv = RAWm[k].values
    out = np.full(len(D), np.nan)
    raw = RAWd[k].values
    # позиция месяца в me для каждого дня
    pos_month = np.searchsorted(me_month.asi8 if hasattr(me_month, "asi8") else np.array([p.ordinal for p in me_month]),
                                np.array([p.ordinal for p in month_of]))
    for i in range(len(D)):
        if not np.isfinite(raw[i]):
            continue
        j = pos_month[i]  # индекс текущего месяца в me
        hist = mv[max(0, j - (w - 1)):j]
        hist = hist[np.isfinite(hist)]
        vals = np.append(hist, raw[i])
        if len(vals) < mp:
            continue
        s = vals.std(ddof=1)
        if s > 0:
            out[i] = np.clip((raw[i] - vals.mean()) / s, -clip, clip)
    return pd.Series(out, index=D.index)


Zd = pd.DataFrame({k: sgn * daily_z_prod(k) for k, sgn in LEGS})
comp_d = Zd.mean(axis=1)
chk = (comp_d.reindex(me) - comp).abs().max()
print(f"\nпроверка: дневной композит на срезах = месячному, max|diff| = {chk:.2e}")

# недельная выборка: последний торговый день недели
wk = D.index.to_period("W")
we = pd.DatetimeIndex(pd.Series(D.index, index=D.index).groupby(wk).last().values)
px_w = D["imoex"].reindex(we)
comp_w = comp_d.reindex(we)
cell_w = D["cell"].reindex(we)
gate_w = (cell_w != TOXIC).astype(float)

print("\n=== недельный шаг: IC дневного композита к fwd h недель (2010+) ===")
rows = []
mw = (we >= pd.Timestamp(MAIN[0])) & (we <= pd.Timestamp(MAIN[1]))
for sname, mask in {"MAIN 2010+": mw, "common 2016-02+": mw & (we >= pd.Timestamp("2016-02-01"))}.items():
    line = f"{sname:16s}"
    for h in (1, 2, 4, 8, 13, 26):
        fh = np.log(px_w.shift(-h) / px_w)
        r = ic_stats(comp_w[mask], fh[mask], lag=h, block=2 * h + 4, n_boot=400)
        rows.append(dict(sample=sname, horizon_w=h, n=r["n"], ic=r["ic"], nw_t=r["nw_t"], p_boot=r["p_boot"]))
        line += f"  h={h:2d}w: {r['ic']:+.3f} (t={r['nw_t']:+.2f})"
    print(line + f"  n={r['n']}")
pd.DataFrame(rows).to_csv(f"{RES}/C_core_4_horizon_weekly.csv", index=False, float_format="%.4f")

print("\n=== профиль IC по неделям: composite_w(t) против доходности недели t+k, k=1..26 (2010+) ===")
r1w = np.log(px_w / px_w.shift(1))
rows = []
profw = []
for k in range(1, 27):
    r = ic_stats(comp_w[mw], r1w.shift(-k)[mw], lag=1, n_boot=200)
    profw.append(r["ic"])
    rows.append(dict(lag_w=k, ic=r["ic"], nw_t=r["nw_t"]))
print("  ".join(f"k{k}:{v:+.2f}" for k, v in zip(range(1, 27), profw)))
pd.DataFrame(rows).to_csv(f"{RES}/C_core_4_lag_profile_weekly.csv", index=False, float_format="%.4f")
print(f"сумма IC недель 1–4: {sum(profw[:4]):+.2f}, 5–8: {sum(profw[4:8]):+.2f}, 9–13: {sum(profw[8:13]):+.2f}, 14–26: {sum(profw[13:]):+.2f}")
ar1w = comp_w.loc[MAIN[0]:MAIN[1]].autocorr(1)
print(f"AR(1) недельного композита = {ar1w:.3f} → полураспад сигнала {np.log(0.5)/np.log(ar1w):.1f} нед")

# ------------------------------------------------ 4в. стратегия с недельным шагом
tr = C["mcftr_ffill"].reindex(D.index).ffill()
mm = C["mm_rate"].reindex(D.index).ffill() / 100 / 252
cum_mm = (1 + mm).cumprod()
tr_w = tr.reindex(we)
mk_w = pd.DataFrame({"fwd_tr": tr_w.shift(-1) / tr_w - 1, "fwd_mm": cum_mm.reindex(we).shift(-1) / cum_mm.reindex(we) - 1})


def bt_w(pos, cost=0.002, a=MAIN[0], b=MAIN[1]):
    df = mk_w.copy()
    df["pos"] = pos.reindex(df.index).fillna(0)
    df = df[(df.index >= a) & (df.index <= b)].dropna()
    df["trade"] = (df["pos"] != df["pos"].shift(1).fillna(0)).astype(int)
    df["ret"] = df["pos"] * df["fwd_tr"] + (1 - df["pos"]) * df["fwd_mm"] - cost * df["trade"]
    return df


def met_w(df):
    r = df["ret"]
    yrs = len(r) / 52
    cum = (1 + r).cumprod()
    bh = (1 + df["fwd_tr"]).cumprod()
    ex = r - df["fwd_mm"]
    return dict(cagr=cum.iloc[-1] ** (1 / yrs) - 1, sharpe=r.mean() / r.std() * np.sqrt(52), shex=ex.mean() / ex.std() * np.sqrt(52),
                mdd=(cum / cum.cummax() - 1).min(), time_in=df["pos"].mean(), trades_yr=df["trade"].sum() / yrs,
                bh_cagr=bh.iloc[-1] ** (1 / yrs) - 1, bh_sharpe=df["fwd_tr"].mean() / df["fwd_tr"].std() * np.sqrt(52), bh_mdd=(bh / bh.cummax() - 1).min())


print("\n=== стратегии на недельной сетке (2010+), MCFTR/mm, издержки 0,2% за смену ===")
rows = []
# месячное правило панели, перенесённое на недели: позиция = решение последнего закрытого месячного среза
pos_m = panel_positions(Mm)
pos_m_w = pos_m.reindex(D.index).ffill().reindex(we)  # на неделе действует последнее месячное решение
# ворота дневные (на закрытии недели) + знак закрытого месяца
hs_m = (hysteresis_sign(comp, 0.1) > 0).astype(float).reindex(D.index).ffill().reindex(we)
pos_gate_daily = hs_m * gate_w
# всё недельное: ворота на неделе + гистерезисный знак дневного композита на закрытии недели
hs_w = (hysteresis_sign(comp_w, 0.1) > 0).astype(float)
pos_all_w = hs_w * gate_w
pos_all_w_h3 = (hysteresis_sign(comp_w, 0.3) > 0).astype(float) * gate_w
for name, pos in [("месячное правило (как в проде)", pos_m_w), ("ворота недельные + знак закрытого месяца", pos_gate_daily),
                  ("ворота недельные + знак дневного композита (гист 0,1)", pos_all_w),
                  ("ворота недельные + знак дневного композита (гист 0,3)", pos_all_w_h3),
                  ("b&h", pd.Series(1.0, index=we))]:
    df = bt_w(pos)
    mtr = met_w(df)
    rows.append(dict(rule=name, **mtr))
    print(f"{name:55s} CAGR={mtr['cagr']*100:+.1f}% Sh={mtr['sharpe']:.2f} Shex={mtr['shex']:.2f} MDD={mtr['mdd']*100:.1f}% in={mtr['time_in']:.0%} tr/y={mtr['trades_yr']:.1f}")
    if name != "b&h":
        d, p, ci = sharpe_diff_boot(df["ret"], bt_w(pos_m_w)["ret"], block=26)
        print(f"{'':55s} ΔSh vs месячного = {d:+.2f} (p={p:.2f}, ДИ90 {ci[0]:+.2f}..{ci[1]:+.2f})")
        rows[-1].update(d_sharpe_vs_monthly=d, p=p)
pd.DataFrame(rows).to_csv(f"{RES}/C_core_4_weekly_strategies.csv", index=False, float_format="%.4f")

# своевременность недельных правил по просадкам
print("\n=== своевременность (MCFTR, просадки >15%, 2010+): дней после пика до флэта / доля избежанного / дней после дна до лонга / доля пропущенного ===")
for name, pos in [("месячное правило", pos_m), ("недельные ворота+месячный знак", pos_gate_daily), ("всё недельное (гист 0,1)", pos_all_w)]:
    t = timeliness(pos, tr, me, thr=0.15, start="2010-01-01")
    t["rule"] = name
    t.to_csv(f"{RES}/C_core_4_timeliness_{name.split()[0]}.csv", index=False, float_format="%.3f")
    print(name)
    print(t[["peak", "trough", "depth", "days_to_flat", "avoided", "days_to_long", "missed"]].to_string())
