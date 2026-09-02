"""S_2 — атака на находку 2 (H_weekly / F2_pm): недельное решение по композиту
(гистерезис 0,2) + выход по воротам любым днём: Шарп 1,26 против 1,07.

(а) независимый дневной композит (сверен с продом в S_0: 4e-15) и недельный (z по 260 нед);
(б) разложение прироста: ворота ежедневно vs недельный композит;
(в) плацебо по дню недели и дню месяца;
(г) сплиты 2004–2017 / 2018–2026, ex-2022, 2010–2021, эры;
(д) гистерезис 0,1/0,2/0,3 и минимальное удержание;
(е) t против t+1 (и t+2) — эффект «день после триггера».
"""
import sys
sys.path.insert(0, "scripts")
import numpy as np
import pandas as pd
from S_lib import *

d = load_daily()
m = load_monthly()
idx = d.index
me = month_end_mask(idx)
we = week_end_mask(idx)
signs = {"usd_mom63": +1, "slope_10_2": +1, "urals_rub_gap": -1}
legs = d[["usd_mom63", "slope_10_2", "urals_rub_gap"]]

# ---------------------------------------------------------------- композиты
comp_live, _ = composite_live_daily(legs, signs)                 # M_live (прод, дневное значение)
comp_closed = m["composite"].reindex(idx).ffill()                # M_closed: значение последнего среза
# недельный: ноги на последний торговый день недели, z по 260 нед (min 104)
legs_w = legs[we]
zw = pd.DataFrame({c: signs[c] * zscore_rolling(legs_w[c], 260, 104, 3.0) for c in legs_w.columns})
comp_w = zw.mean(axis=1, skipna=True)
comp_w[zw.notna().sum(axis=1) == 0] = np.nan
comp_w_daily = comp_w.reindex(idx).ffill()
# дневной: z по 1260 дн (min 504)
zd = pd.DataFrame({c: signs[c] * zscore_rolling(legs[c], 1260, 504, 3.0) for c in legs.columns})
comp_d = zd.mean(axis=1, skipna=True)
comp_d[zd.notna().sum(axis=1) == 0] = np.nan
COMPS = {"M_closed": comp_closed, "M_live": comp_live, "W": comp_w_daily, "D": comp_d}
cc = pd.DataFrame(COMPS).loc["2010":"2026-08"]
print("корреляции версий композита (дни 2010+):")
print(cc.corr().round(3).to_string())


# ---------------------------------------------------------------- позиция
def build_position(comp, decision_mask, hyst=0.10, gate_exit_daily=False, hyst_mode="sampled",
                   min_hold=0, delay=0, gate=None):
    """Позиция по дням. Решение по композиту — на днях decision_mask; знак с гистерезисом:
    hyst_mode='sampled' — состояние обновляется только в дни решения по значению того дня;
    'running' — состояние бежит по дневному ряду, читается в день решения.
    Ворота: закрытие ворот (токсичная ячейка) — в дни решения или любым днём (gate_exit_daily);
    открытие — только в дни решения. min_hold — минимальное удержание позиции (торг. дни).
    delay — исполнение через delay дней после решения."""
    tox = (d["toxic"] if gate is None else gate).values
    cv = comp.values
    dm = decision_mask.values
    n = len(idx)
    if hyst_mode == "running":
        sgn_run = hysteresis_sign(comp, hyst).values
    pos = np.full(n, np.nan)
    s = 0
    cur = np.nan
    last_change = -10 ** 6
    gate_open = np.nan
    for i in range(n):
        if np.isnan(tox[i]):
            continue
        if dm[i]:
            if hyst_mode == "running":
                s = sgn_run[i]
            elif np.isfinite(cv[i]):
                if cv[i] > hyst:
                    s = 1
                elif cv[i] < -hyst:
                    s = -1
            gate_open = 1.0 - tox[i]
        elif gate_exit_daily and tox[i] == 1:
            gate_open = 0.0
        if np.isnan(gate_open) or s == 0:
            continue
        want = float(gate_open == 1.0 and s > 0)
        if np.isnan(cur):
            cur = want
            last_change = i
        elif want != cur and (i - last_change) >= min_hold:
            cur = want
            last_change = i
        pos[i] = cur
    p = pd.Series(pos, index=idx)
    if delay:
        p = p.shift(delay)
    return p


def ev(pos, name, start="2010-01-01", end="2026-08-31", cost=COST):
    bt = run_daily(pos, d, start=start, end=end, cost=cost)
    mt = metrics(bt, name)
    return bt, mt, monthly_returns(bt)


def line(mt, extra=""):
    return "%-62s CAGR %5.1f%% Sh %4.2f ex %5.2f MDDd %6.1f%% in %5.1f%% tr/y %4.2f beat %3.0f%% hit %3.0f%% %s" % (
        mt["name"], mt["cagr"] * 100, mt["sharpe"], mt["sharpe_ex"], mt["mdd_daily"] * 100,
        mt["in_market"] * 100, mt["trades_py"], mt["years_beat_bh"] * 100, mt["hit_months"] * 100, extra)


results = []
pos_prod = build_position(comp_closed, me, 0.10)
bt_prod, mt_prod, mr_prod = ev(pos_prod, "прод: M_closed / решение на срезе месяца")
print("\nсверка эталона:", line(mt_prod))
mr_prod_all = {}
for w, (a, b) in WINDOWS.items():
    mr_prod_all[w] = ev(pos_prod, "prod", a, b)[2]


def record(pos, name, fam, start="2010-01-01", end="2026-08-31", cost=COST, boot=True, base_pos=None):
    bt, mt, mr = ev(pos, name, start, end, cost)
    bp = pos_prod if base_pos is None else base_pos
    mrb = ev(bp, "base", start, end, cost)[2]
    j = mr.join(mrb, lsuffix="_x", rsuffix="_b").dropna()
    dsh, p, lo, hi = sharpe_diff_boot(j["strat_x"], j["strat_b"]) if boot else (np.nan,) * 4
    results.append(dict(fam=fam, **mt, d_sharpe=dsh, p_le0=p, ci90_lo=lo, ci90_hi=hi, window=f"{start}..{end}", cost=cost))
    print(line(mt, "ΔSh %+.2f p %.3f ДИ90 %+.2f…%+.2f" % (dsh, p, lo, hi) if boot else ""))
    return bt, mt, mr


# ================================================================ (а)+(б) сетка и разложение
print("\n=== (а)(б) СЕТКА композит × каденция × ворота, 2010–2026 ===")
grid_pos = {}
for cname, comp in COMPS.items():
    for dname, dmask in [("M", me), ("W", we), ("D", pd.Series(True, index=idx))]:
        for gname, gd in [("gate@cadence", False), ("gate_exit_daily", True)]:
            for hm in ["sampled", "running"]:
                if cname == "M_closed" and hm == "running":
                    continue
                key = f"{cname}/{dname}/{gname}/{hm}"
                pos = build_position(comp, dmask, 0.10, gate_exit_daily=gd, hyst_mode=hm)
                grid_pos[key] = pos
                record(pos, key, "grid")
# ключевое разложение (гистерезис 0,1, sampled):
print("\nразложение прироста (гистерезис 0,1):")
for key in ["M_closed/M/gate@cadence/sampled", "M_closed/M/gate_exit_daily/sampled",
            "M_closed/W/gate@cadence/sampled", "M_closed/W/gate_exit_daily/sampled",
            "M_live/W/gate@cadence/sampled", "M_live/W/gate_exit_daily/sampled",
            "W/W/gate@cadence/sampled", "W/W/gate_exit_daily/sampled",
            "M_live/D/gate@cadence/sampled", "D/D/gate@cadence/sampled"]:
    r = [x for x in results if x["name"] == key][0]
    print("  %-45s Sh %.2f (Δ %+.2f, p %.2f) MDDd %.1f%% tr/y %.1f" % (key, r["sharpe"], r["d_sharpe"], r["p_le0"], r["mdd_daily"] * 100, r["trades_py"]))

# ================================================================ (в) плацебо
print("\n=== (в) ПЛАЦЕБО: день недели и день месяца ===")
pl = []
for cname in ["M_live", "W"]:
    comp = COMPS[cname]
    for wd in range(5):
        # решение в k-й день недели (если торговый); гистерезис 0,2 как в рекомендации
        dmask = pd.Series(idx.weekday == wd, index=idx)
        for gd in [False, True]:
            pos = build_position(comp, dmask, 0.20, gate_exit_daily=gd)
            bt, mt, mr = ev(pos, f"{cname} weekday {wd}")
            btf, mtf, _ = ev(pos, "", "2004-01-01", "2026-08-31")
            pl.append(dict(kind="weekday", comp=cname, k=wd, gate_exit_daily=gd, sharpe=mt["sharpe"], sharpe_full=mtf["sharpe"],
                           mdd_d=mt["mdd_daily"], trades=mt["trades_py"], cagr=mt["cagr"]))
    # k-й торговый день месяца
    tdn = idx.to_series().groupby(idx.to_period("M")).cumcount() + 1
    for k in range(1, 22):
        dmask = (tdn == k)
        # последний торговый день месяца = обычный прод (k = 'last')
        for gd in [False, True]:
            pos = build_position(comp, dmask, 0.10, gate_exit_daily=gd)
            bt, mt, mr = ev(pos, f"{cname} day {k}")
            btf, mtf, _ = ev(pos, "", "2004-01-01", "2026-08-31")
            pl.append(dict(kind="day_of_month", comp=cname, k=k, gate_exit_daily=gd, sharpe=mt["sharpe"], sharpe_full=mtf["sharpe"],
                           mdd_d=mt["mdd_daily"], trades=mt["trades_py"], cagr=mt["cagr"]))
    for gd in [False, True]:
        pos = build_position(comp, me, 0.10, gate_exit_daily=gd)
        bt, mt, mr = ev(pos, f"{cname} month-end")
        btf, mtf, _ = ev(pos, "", "2004-01-01", "2026-08-31")
        pl.append(dict(kind="day_of_month", comp=cname, k="last", gate_exit_daily=gd, sharpe=mt["sharpe"], sharpe_full=mtf["sharpe"],
                       mdd_d=mt["mdd_daily"], trades=mt["trades_py"], cagr=mt["cagr"]))
pl = pd.DataFrame(pl)
pl.to_csv("results/S_2_placebo.csv", index=False)
for cname in ["M_live", "W"]:
    for gd in [False, True]:
        s = pl[(pl["kind"] == "weekday") & (pl["comp"] == cname) & (pl["gate_exit_daily"] == gd)]
        print("  %s, ворота %s — по дню недели (пн..пт), Шарп MAIN: %s; FULL: %s" % (
            cname, "ежедн." if gd else "в такт", " ".join(f"{v:.2f}" for v in s["sharpe"]), " ".join(f"{v:.2f}" for v in s["sharpe_full"])))
        s = pl[(pl["kind"] == "day_of_month") & (pl["comp"] == cname) & (pl["gate_exit_daily"] == gd)]
        sk = s[s["k"] != "last"]["sharpe"]
        last = s[s["k"] == "last"]["sharpe"].iloc[0]
        print("  %s, ворота %s — по дню месяца k=1..21: медиана %.2f, мин %.2f, макс %.2f; конец месяца %.2f (ранг %d из %d); k=1..5: %s" % (
            cname, "ежедн." if gd else "в такт", sk.median(), sk.min(), sk.max(), last, int((sk <= last).sum()) + 1, len(sk) + 1,
            " ".join(f"{v:.2f}" for v in sk.iloc[:5])))

# ================================================================ (г) сплиты
print("\n=== (г) СПЛИТЫ ===")
CANDS = {
    "прод M_closed/M": pos_prod,
    "M_closed/M + ворота-выход ежедневно": grid_pos["M_closed/M/gate_exit_daily/sampled"],
    "W/W (h=0,1)": grid_pos["W/W/gate@cadence/sampled"],
    "W/W + ворота-выход ежедневно (h=0,1)": grid_pos["W/W/gate_exit_daily/sampled"],
    "W/W + ворота-выход ежедневно (h=0,2) [рекоменд. H]": build_position(comp_w_daily, we, 0.20, gate_exit_daily=True),
    "M_live/W + ворота-выход ежедневно (h=0,2)": build_position(comp_live, we, 0.20, gate_exit_daily=True),
    "M_live/W (h=0,2, ворота в такт)": build_position(comp_live, we, 0.20, gate_exit_daily=False),
}
split_rows = []
for lab, (a, b) in [("2010-2026", WINDOWS["2010-2026"]), ("2004-2026", WINDOWS["2004-2026"]), ("2004-2017", ("2004-01-01", "2017-12-31")),
                    ("2018-2026", ("2018-01-01", "2026-08-31")), ("2010-2021", WINDOWS["2010-2021"]),
                    ("2022-03-2024", WINDOWS["2022-03-2024"]), ("2025-2026", WINDOWS["2025-2026"]), ("2010-2019", ("2010-01-01", "2019-12-31"))]:
    for nm, pos in CANDS.items():
        bt, mt, mr = ev(pos, nm, a, b)
        mrb = ev(pos_prod, "", a, b)[2]
        j = mr.join(mrb, lsuffix="_x", rsuffix="_b").dropna()
        k22 = j.index.year != 2022
        dsh, p, lo, hi = sharpe_diff_boot(j["strat_x"], j["strat_b"], n_boot=1000)
        split_rows.append(dict(window=lab, name=nm, sharpe=mt["sharpe"], sharpe_ex2022=sharpe(j.loc[k22, "strat_x"]),
                               prod_ex2022=sharpe(j.loc[k22, "strat_b"]), d_sharpe=dsh, p_le0=p, mdd_d=mt["mdd_daily"], cagr=mt["cagr"], trades=mt["trades_py"]))
sp = pd.DataFrame(split_rows)
sp.to_csv("results/S_2_splits.csv", index=False)
print(sp.pivot(index="name", columns="window", values="sharpe").to_string(float_format=lambda v: f"{v:.2f}"))
print("ex-2022 (внутри окна):")
print(sp.pivot(index="name", columns="window", values="sharpe_ex2022").to_string(float_format=lambda v: f"{v:.2f}"))
print("p(Δ≤0) к проду:")
print(sp.pivot(index="name", columns="window", values="p_le0").to_string(float_format=lambda v: f"{v:.2f}"))

# ================================================================ (д) гистерезис и удержание
print("\n=== (д) ГИСТЕРЕЗИС × УДЕРЖАНИЕ (W/W, ворота-выход ежедневно) ===")
hrows = []
for cname in ["W", "M_live"]:
    for h in [0.0, 0.1, 0.2, 0.3, 0.5]:
        for hold in [0, 5, 10, 21, 42]:
            for gd in [False, True]:
                pos = build_position(COMPS[cname], we, h, gate_exit_daily=gd, min_hold=hold)
                bt, mt, mr = ev(pos, "")
                btf, mtf, _ = ev(pos, "", "2004-01-01", "2026-08-31")
                bt1, mt1, _ = ev(pos, "", "2004-01-01", "2017-12-31")
                bt2, mt2, _ = ev(pos, "", "2018-01-01", "2026-08-31")
                hrows.append(dict(comp=cname, hyst=h, hold=hold, gate_exit_daily=gd, sharpe=mt["sharpe"], sharpe_full=mtf["sharpe"],
                                  sh_2004_17=mt1["sharpe"], sh_2018_26=mt2["sharpe"], mdd_d=mt["mdd_daily"], trades=mt["trades_py"]))
hr = pd.DataFrame(hrows)
hr.to_csv("results/S_2_hysteresis.csv", index=False)
for cname in ["W", "M_live"]:
    for gd in [False, True]:
        s = hr[(hr["comp"] == cname) & (hr["gate_exit_daily"] == gd)]
        print(f"{cname}, ворота {'ежедн.' if gd else 'в такт'} — Шарп MAIN (строки гистерезис, столбцы удержание):")
        print(s.pivot(index="hyst", columns="hold", values="sharpe").to_string(float_format=lambda v: f"{v:.2f}"))
        print("  сделок/год:")
        print(s.pivot(index="hyst", columns="hold", values="trades").to_string(float_format=lambda v: f"{v:.1f}"))

# ================================================================ (е) t vs t+1
print("\n=== (е) ИСПОЛНЕНИЕ t / t+1 / t+2 ===")
dl_rows = []
for nm, comp, dmask, h, gd in [("прод M_closed/M", comp_closed, me, 0.10, False),
                               ("W/W h=0,2 + ворота ежедн.", comp_w_daily, we, 0.20, True),
                               ("W/W h=0,1 ворота в такт", comp_w_daily, we, 0.10, False),
                               ("M_live/W h=0,2 + ворота ежедн.", comp_live, we, 0.20, True),
                               ("M_live/D h=0,1", comp_live, pd.Series(True, index=idx), 0.10, False),
                               ("M_closed/M + ворота ежедн.", comp_closed, me, 0.10, True)]:
    for delay in [0, 1, 2, 3, 5]:
        pos = build_position(comp, dmask, h, gate_exit_daily=gd, delay=delay)
        for lab, (a, b) in [("2010-2026", WINDOWS["2010-2026"]), ("2004-2026", WINDOWS["2004-2026"])]:
            bt, mt, mr = ev(pos, nm, a, b)
            dl_rows.append(dict(name=nm, delay=delay, window=lab, sharpe=mt["sharpe"], cagr=mt["cagr"], mdd_d=mt["mdd_daily"], trades=mt["trades_py"]))
dl = pd.DataFrame(dl_rows)
dl.to_csv("results/S_2_delay.csv", index=False)
for lab in ["2010-2026", "2004-2026"]:
    print(lab, "— Шарп по задержке исполнения 0/1/2/3/5 дней:")
    print(dl[dl["window"] == lab].pivot(index="name", columns="delay", values="sharpe").to_string(float_format=lambda v: f"{v:.2f}"))

# «день после триггера»: доходность MCFTR в день t+1 после смены позиции недельного правила
pos_w = CANDS["W/W + ворота-выход ежедневно (h=0,2) [рекоменд. H]"]
chg = pos_w.diff()
r1 = d["mcftr"].pct_change()
for lab, mask in [("вход (0→1)", chg == 1), ("выход (1→0)", chg == -1)]:
    days = idx[mask.fillna(False).values]
    days = days[(days >= "2010-01-01") & (days <= "2026-08-31")]
    nxt = [r1.iloc[idx.get_loc(t) + 1] for t in days if idx.get_loc(t) + 1 < len(idx)]
    nxt5 = [np.log(d["mcftr"].iloc[min(idx.get_loc(t) + 5, len(idx) - 1)] / d["mcftr"].iloc[idx.get_loc(t)]) for t in days]
    print("  %s: n=%d, MCFTR t+1 средн. %+.2f%% (медиана %+.2f%%), t+1..t+5 %+.2f%%" % (lab, len(days), np.mean(nxt) * 100, np.median(nxt) * 100, np.mean(nxt5) * 100))

# ================================================================ своевременность
print("\n=== СВОЕВРЕМЕННОСТЬ (правило 7), просадки MCFTR > 15% с 2004 ===")
tl = None
for nm in ["прод M_closed/M", "M_closed/M + ворота-выход ежедневно", "W/W + ворота-выход ежедневно (h=0,2) [рекоменд. H]"]:
    t = timeliness(CANDS[nm].loc["2004":], d["mcftr"].loc["2004":])
    t = t.rename(columns={c: f"{c}|{nm[:12]}" for c in ["long_at_peak", "days_to_exit", "avoided", "days_to_entry", "missed_half_rec"]})
    tl = t if tl is None else tl.merge(t, on=["peak", "trough", "depth"])
tl.to_csv("results/S_2_timeliness.csv", index=False)
print(tl.to_string(index=False, float_format=lambda v: f"{v:.2f}"))

pd.DataFrame(results).to_csv("results/S_2_variants.csv", index=False)
print("\nготово: results/S_2_*.csv")
