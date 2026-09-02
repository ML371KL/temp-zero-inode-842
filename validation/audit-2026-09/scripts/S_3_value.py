"""S_3 — атака на находку 3 (D3 §6, §11): замена ноги −urals_rub_gap на −urals_usd_gap.

(а) обе ноги строятся из raw_long (urals_tax, usd) с честным лагом 5 дней из реестра;
    сверка рублёвой ноги с продом;
(б) leave-one-year-out по Шарпу и IC;
(в) альтернатива Brent $ к 24-мес среднему (с тем же лагом и без; с той же доступностью, что Urals);
(г) двойной счёт курса: разложение рублёвой ноги, корреляции с usd_mom63, IC компонент,
    «обратный дефект» долларовой ноги;
(д) what-if по эпизодам 2014 (по Brent), 2020, 2022, 2026.
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
me_dates = idx[me]
PUB_LAG_DAYS = 5     # registry.py: urals_tax pub_lag_days=5
FFILL_LIMIT = 66     # panel.py: URALS_FFILL_LIMIT

# ---------------------------------------------------------------- (а) ноги из сырья
urals = load_raw("urals_tax")                       # $ за баррель, дата = конец месяца периода
usd = d["usd"]                                      # официальный курс по дате применения (= usd_cbr)
usd_mavg = usd.groupby(idx.to_period("M")).mean()   # среднемесячный курс по торговым дням
usd_mavg.index = usd_mavg.index.to_timestamp("M")   # календарный конец месяца
per = urals.index.to_period("M").to_timestamp("M")
ur = pd.Series(urals.values, index=per).sort_index()
ur = ur[~ur.index.duplicated()]
rb = ur * usd_mavg.reindex(ur.index)


def gap24(x):
    return np.log(x / x.rolling(24, min_periods=12).mean())


def align_lag(series_m, lag_days=PUB_LAG_DAYS, limit=FFILL_LIMIT):
    """Месячная точка (дата = конец периода) становится видна через lag_days календарных
    дней; на дневной сетке протягивается не дольше limit торговых дней."""
    avail = series_m.copy()
    avail.index = avail.index + pd.Timedelta(days=lag_days)
    s = pd.Series(np.nan, index=idx)
    for dt, v in avail.items():
        pos = idx.searchsorted(dt)      # первый торговый день ≥ даты доступности
        if pos < len(idx):
            s.iloc[pos] = v
    return s.ffill(limit=limit)


leg_rub = align_lag(gap24(rb))
leg_usd = align_lag(gap24(ur))
# компонента курса: log(среднемесячный курс / его 24-мес среднее) с тем же лагом
leg_fx24 = align_lag(gap24(usd_mavg.reindex(ur.index)))
# сверка с продом
chk = pd.concat([leg_rub[me], m["raw_urals_rub_gap"].reindex(me_dates)], axis=1, keys=["mine", "prod"]).dropna()
print("сверка urals_rub_gap с продом на срезах: n=%d, max|diff|=%.2e, corr=%.6f" % (len(chk), (chk["mine"] - chk["prod"]).abs().max(), chk["mine"].corr(chk["prod"])))
if (chk["mine"] - chk["prod"]).abs().max() > 1e-9:
    bad = chk[(chk["mine"] - chk["prod"]).abs() > 1e-9]
    print("  расхождения (первые 10):"); print(bad.head(10).to_string())

# Brent: месячное среднее по дневным ценам; версии — с лагом Urals (сопоставимо) и без лага (реальное время)
brent = load_raw("brent")
brent = brent.reindex(idx).ffill(limit=10)
br_mavg = brent.groupby(idx.to_period("M")).mean()
br_mavg.index = br_mavg.index.to_timestamp("M")
# доступность как у Urals: только с 2015-01 и лаг 5 дней
br_like_urals = br_mavg[br_mavg.index >= "2015-01-31"]
leg_brent_lag = align_lag(gap24(br_like_urals))
leg_brent_rub_lag = align_lag(gap24(br_like_urals * usd_mavg.reindex(br_like_urals.index)))
# Brent на полной истории (с 1989), лаг тот же 5 дней — «другая модель»
leg_brent_full = align_lag(gap24(br_mavg))
# Brent в реальном времени: месячное среднее видно на конце месяца (лаг 0)
leg_brent_rt = align_lag(gap24(br_like_urals), lag_days=0)

# ---------------------------------------------------------------- композиты
base_legs = {"usd_mom63": d["usd_mom63"], "slope_10_2": d["slope_10_2"]}
VARIANTS = {
    "прод: −urals_rub_gap": ("urals_rub_gap", leg_rub, -1),
    "−urals_usd_gap (Urals $, лаг 5 дн)": ("urals_usd_gap", leg_usd, -1),
    "−brent_usd_gap (Brent $ мес. среднее, лаг 5 дн, с 2015)": ("brent_usd_gap", leg_brent_lag, -1),
    "−brent_usd_gap в реальном времени (лаг 0, с 2015)": ("brent_rt", leg_brent_rt, -1),
    "−brent_rub_gap (Brent×курс, лаг 5 дн, с 2015)": ("brent_rub_gap", leg_brent_rub_lag, -1),
    "−brent_usd_gap на полной истории (с 1989)": ("brent_full", leg_brent_full, -1),
    "две ноги (без бочки)": (None, None, 0),
    "−usd_fx24 (только курсовая компонента бочки)": ("fx24", leg_fx24, -1),
    "прод + отдельно −usd_fx24 (4 ноги)": ("both", None, 0),
    "−urals_usd_gap + отдельно −usd_fx24 (4 ноги)": ("both_usd", None, 0),
}


def make_composite(third):
    cid, s, sign = third
    L = dict(base_legs)
    signs = {"usd_mom63": +1, "slope_10_2": +1}
    if cid == "both":
        L["urals_rub_gap"] = leg_rub; signs["urals_rub_gap"] = -1
        L["fx24"] = leg_fx24; signs["fx24"] = -1
    elif cid == "both_usd":
        L["urals_usd_gap"] = leg_usd; signs["urals_usd_gap"] = -1
        L["fx24"] = leg_fx24; signs["fx24"] = -1
    elif cid is not None:
        L[cid] = s; signs[cid] = sign
    legs_m = pd.DataFrame(L).groupby(idx.to_period("M")).last()
    legs_m.index = me_dates
    comp, zs = composite_monthly(legs_m, signs)
    return comp, zs


comps = {}
for nm, third in VARIANTS.items():
    comps[nm] = make_composite(third)
chk2 = pd.concat([comps["прод: −urals_rub_gap"][0], m["composite"]], axis=1, keys=["mine", "prod"]).dropna()
print("сверка композита прода: n=%d max|diff|=%.2e" % (len(chk2), (chk2["mine"] - chk2["prod"]).abs().max()))

# ---------------------------------------------------------------- стратегии
tox_me = d.loc[me, "toxic"]
mc_me = d["mcftr"].reindex(me_dates)
fwd1 = np.log(mc_me.shift(-1) / mc_me)
fwd3 = np.log(mc_me.shift(-3) / mc_me)


def position_from(comp, hyst=0.10):
    sgn = hysteresis_sign(comp, hyst)
    dec = ((sgn > 0) & (tox_me == 0)).astype(float)
    dec[tox_me.isna() | (sgn == 0)] = np.nan
    return decision_to_daily(dec, idx)


def line(mt, extra=""):
    return "%-58s CAGR %5.1f%% Sh %4.2f ex %5.2f MDDm %6.1f%% MDDd %6.1f%% in %5.1f%% tr/y %4.2f beat %3.0f%% %s" % (
        mt["name"], mt["cagr"] * 100, mt["sharpe"], mt["sharpe_ex"], mt["mdd_monthly"] * 100,
        mt["mdd_daily"] * 100, mt["in_market"] * 100, mt["trades_py"], mt["years_beat_bh"] * 100, extra)


pos_v = {nm: position_from(c[0]) for nm, c in comps.items()}
pos_prod = pos_v["прод: −urals_rub_gap"]
rows = []
mr_prod = {}
print("\n=== СТРАТЕГИИ (ворота + знак композита, месячный шаг) ===")
for w, (a, b) in [("2010-2026", WINDOWS["2010-2026"]), ("2017-2026", ("2017-01-01", "2026-08-31")), ("2004-2026", WINDOWS["2004-2026"]),
                  ("2010-2021", WINDOWS["2010-2021"]), ("2022-03-2024", WINDOWS["2022-03-2024"]), ("2025-2026", WINDOWS["2025-2026"]),
                  ("2017-2021", ("2017-01-01", "2021-12-31"))]:
    btp = run_daily(pos_prod, d, start=a, end=b)
    mrp = monthly_returns(btp)
    mr_prod[w] = mrp
    print(f"--- окно {w} ---")
    for nm, pos in pos_v.items():
        bt = run_daily(pos, d, start=a, end=b)
        mt = metrics(bt, nm)
        mr = monthly_returns(bt)
        j = mr.join(mrp, lsuffix="_x", rsuffix="_b").dropna()
        k22 = j.index.year != 2022
        dsh, p, lo, hi = sharpe_diff_boot(j["strat_x"], j["strat_b"], n_boot=1500, block=12)
        sh_ex22 = sharpe(j.loc[k22, "strat_x"]); shp_ex22 = sharpe(j.loc[k22, "strat_b"])
        rows.append(dict(window=w, **mt, d_sharpe=dsh, p_le0=p, ci90_lo=lo, ci90_hi=hi, sharpe_ex2022=sh_ex22, prod_ex2022=shp_ex22))
        print(line(mt, "ΔSh %+.2f p %.3f | ex-2022 %.2f (прод %.2f)" % (dsh, p, sh_ex22, shp_ex22)))
res = pd.DataFrame(rows)
res.to_csv("results/S_3_strategies.csv", index=False)

# ---------------------------------------------------------------- IC
print("\n=== IC композитов и ног (Спирмен → fwd1m/fwd3m MCFTR, месячная выборка) ===")
ic_rows = []
subs = {"2017-2026": ("2017-01-01", "2026-08-31"), "2017-2019": ("2017-01-01", "2019-12-31"), "2020-2021": ("2020-01-01", "2021-12-31"),
        "2022-2024": ("2022-01-01", "2024-12-31"), "2025-2026": ("2025-01-01", "2026-08-31"), "2016-2026": ("2016-01-01", "2026-08-31")}
sigs = {nm: c[0] for nm, c in comps.items()}
sigs["нога −urals_rub_gap"] = -leg_rub[me]
sigs["нога −urals_usd_gap"] = -leg_usd[me]
sigs["нога −usd_fx24"] = -leg_fx24[me]
sigs["нога −brent_usd_gap (лаг)"] = -leg_brent_lag[me]
sigs["нога usd_mom63"] = d["usd_mom63"][me]
sigs["нога slope_10_2"] = d["slope_10_2"][me]
for nm, s in sigs.items():
    r = dict(name=nm)
    for sub, (a, b) in subs.items():
        ss = s.loc[a:b]; ff = fwd1.loc[a:b]
        rho, t, n = spearman_nw(ss, ff, 1)
        r[f"ic1_{sub}"] = rho; r[f"t1_{sub}"] = t; r[f"n_{sub}"] = n
        rho3, t3, n3 = spearman_nw(ss, fwd3.loc[a:b], 3)
        r[f"ic3_{sub}"] = rho3
    ss = s.loc["2017":"2026-08"]; ff = fwd1.loc["2017":"2026-08"]
    k22 = ss.index.year != 2022
    rho, t, n = spearman_nw(ss[k22], ff[k22], 1)
    r["ic1_ex2022"] = rho; r["t1_ex2022"] = t
    r["p_boot_2017_26"] = ic_boot(s.loc["2017":"2026-08"], fwd1.loc["2017":"2026-08"], n_boot=800)
    ic_rows.append(r)
ic = pd.DataFrame(ic_rows)
ic.to_csv("results/S_3_ic.csv", index=False)
cols = ["ic1_2017-2026", "t1_2017-2026", "p_boot_2017_26", "ic1_ex2022", "ic1_2017-2019", "ic1_2020-2021", "ic1_2022-2024", "ic1_2025-2026", "ic3_2017-2026"]
print(ic.set_index("name")[cols].to_string(float_format=lambda v: f"{v:.3f}"))

# ---------------------------------------------------------------- (б) leave-one-year-out
print("\n=== (б) LEAVE-ONE-YEAR-OUT, 2010–2026: Шарп прод / долларовая / Brent$ / две ноги; IC 2017–26 ===")
mrp = mr_prod["2010-2026"]
cand_names = ["прод: −urals_rub_gap", "−urals_usd_gap (Urals $, лаг 5 дн)", "−brent_usd_gap (Brent $ мес. среднее, лаг 5 дн, с 2015)", "две ноги (без бочки)"]
mrs = {nm: monthly_returns(run_daily(pos_v[nm], d, start="2010-01-01", end="2026-08-31"))["strat"] for nm in cand_names}
loyo = []
for y in range(2010, 2027):
    keep = mrp.index.year != y
    r = dict(year=y)
    for nm in cand_names:
        r["sh_" + nm[:12]] = sharpe(mrs[nm][keep])
    r["d_usd_minus_prod"] = r["sh_" + cand_names[1][:12]] - r["sh_" + cand_names[0][:12]]
    r["d_brent_minus_prod"] = r["sh_" + cand_names[2][:12]] - r["sh_" + cand_names[0][:12]]
    # IC LOYO 2017-2026
    for nm in cand_names[:3]:
        s = comps[nm][0].loc["2017":"2026-08"]; f = fwd1.loc["2017":"2026-08"]
        kk = s.index.year != y
        r["ic_" + nm[:12]] = spearman_nw(s[kk], f[kk], 1)[0]
    r["d_ic_usd_minus_prod"] = r["ic_" + cand_names[1][:12]] - r["ic_" + cand_names[0][:12]]
    loyo.append(r)
loyo = pd.DataFrame(loyo)
loyo.to_csv("results/S_3_loyo.csv", index=False)
print(loyo.to_string(index=False, float_format=lambda v: f"{v:.3f}"))
print("ΔШарп (USD − прод) без одного года: min %+.2f, медиана %+.2f, макс %+.2f; лет с Δ<0: %d из %d" % (
    loyo["d_usd_minus_prod"].min(), loyo["d_usd_minus_prod"].median(), loyo["d_usd_minus_prod"].max(), int((loyo["d_usd_minus_prod"] < 0).sum()), len(loyo)))
print("ΔIC (USD − прод) без одного года: min %+.3f, медиана %+.3f; лет с Δ<0: %d" % (
    loyo["d_ic_usd_minus_prod"].min(), loyo["d_ic_usd_minus_prod"].median(), int((loyo["d_ic_usd_minus_prod"] < 0).sum())))
# вклад по годам разности месячных доходностей
jj = pd.concat([mrs[cand_names[1]], mrs[cand_names[0]]], axis=1, keys=["usd", "prod"]).dropna()
dif = (jj["usd"] - jj["prod"]) * 100
by = dif.groupby(dif.index.year).sum()
print("вклад по годам (п.п. USD − прод):", " ".join(f"{y}:{v:+.1f}" for y, v in by.items() if abs(v) > 0.05))
# месяцы расхождения позиций
pd_ = pd.concat([pos_v[cand_names[1]][me], pos_prod[me]], axis=1, keys=["usd", "prod"]).dropna()
diffm = pd_[pd_["usd"] != pd_["prod"]]
print("срезов, где позиции расходятся: %d из %d (2017+: %d)" % (len(diffm), len(pd_.loc["2017":]), len(diffm.loc["2017":])))
dm = diffm.copy()
dm["fwd1_minus_cash_pct"] = (np.exp(fwd1.reindex(dm.index)) - 1) * 100 - mrp["cash"].reindex(dm.index.to_period("M") + 1).values * 100
dm.to_csv("results/S_3_position_diffs.csv")
print(dm.to_string(float_format=lambda v: f"{v:.1f}"))

# ---------------------------------------------------------------- (г) двойной счёт
print("\n=== (г) ДВОЙНОЙ СЧЁТ КУРСА ===")
zs_prod = comps["прод: −urals_rub_gap"][1]
zs_usd = comps["−urals_usd_gap (Urals $, лаг 5 дн)"][1]
zz = pd.DataFrame({"z_usd_mom63": zs_prod["usd_mom63"], "z_rub_gap(−)": zs_prod["urals_rub_gap"], "z_usd_gap(−)": zs_usd["urals_usd_gap"],
                   "z_fx24(−)": comps["−usd_fx24 (только курсовая компонента бочки)"][1]["fx24"]}).loc["2017":"2026-08"].dropna()
print("корреляции z-ног (2017–2026, n=%d):" % len(zz))
print(zz.corr().round(2).to_string())
# разложение: urals_rub_gap ≈ urals_usd_gap + fx24 (точно? проверим)
dec = pd.DataFrame({"rub": leg_rub[me], "usd": leg_usd[me], "fx": leg_fx24[me]}).dropna()
resid = dec["rub"] - dec["usd"] - dec["fx"]
print("тождество rub_gap = usd_gap + fx24: средн. остаток %.4f, ско %.4f (нетождественно из-за MA произведения)" % (resid.mean(), resid.std()))
print("вклад дисперсии: var(usd_gap)=%.4f var(fx24)=%.4f cov=%.4f var(rub)=%.4f" % (dec["usd"].var(), dec["fx"].var(), dec[["usd", "fx"]].cov().iloc[0, 1], dec["rub"].var()))
# доля месяцев, где знаки компонент противоположны и fx доминирует
opp = ((np.sign(dec["usd"]) != np.sign(dec["fx"])) & (dec["fx"].abs() > dec["usd"].abs())).mean()
print("доля срезов, где курсовая компонента доминирует и противоположна нефтяной: %.0f%%" % (opp * 100))
# «обратный дефект»: композит с долларовой ногой — как он ведёт себя при девальвации без ЦБ-контроля (usd_mom63 берёт всё?)
# корреляция композитов с usd_mom63 и с fx24
for nm in cand_names[:3]:
    c = comps[nm][0].loc["2017":"2026-08"]
    print("  %-50s corr(композит, z usd_mom63) = %.2f; corr(композит, −fx24) = %.2f" % (nm, c.corr(zz["z_usd_mom63"]), c.corr(zz["z_fx24(−)"])))

# ---------------------------------------------------------------- (д) эпизоды
print("\n=== (д) WHAT-IF ПО ЭПИЗОДАМ (помесячно: композит, знак, ворота, позиция, MCFTR след. месяца) ===")
ep_rows = []
for lab, a, b in [("2020", "2019-11-30", "2020-07-31"), ("2022", "2021-10-31", "2023-01-31"), ("2026", "2025-10-31", "2026-08-31"), ("2024-25", "2024-09-30", "2025-08-31")]:
    print(f"--- эпизод {lab} ---")
    tbl = pd.DataFrame({"comp_prod": comps[cand_names[0]][0], "comp_usd": comps[cand_names[1]][0], "comp_brent": comps[cand_names[2]][0],
                        "z_rub(−)": zs_prod["urals_rub_gap"], "z_usd(−)": zs_usd["urals_usd_gap"], "z_fx24(−)": zz["z_fx24(−)"].reindex(me_dates),
                        "toxic": tox_me, "pos_prod": pos_prod[me], "pos_usd": pos_v[cand_names[1]][me], "pos_brent": pos_v[cand_names[2]][me],
                        "fwd1m_pct": (np.exp(fwd1) - 1) * 100, "urals_$": ur.reindex(me_dates.to_period("M").to_timestamp("M")).values,
                        "usd_avg": usd_mavg.reindex(me_dates.to_period("M").to_timestamp("M")).values}).loc[a:b]
    tbl["episode"] = lab
    ep_rows.append(tbl)
    print(tbl.drop(columns="episode").to_string(float_format=lambda v: f"{v:.2f}"))
# 2014: Urals нет — прокси по Brent (полная история) в $ и в рублях
print("--- эпизод 2014 (Urals-ряда нет; прокси Brent $ и Brent×курс на полной истории, лаг 5 дн) ---")
legs14 = {"usd_mom63": d["usd_mom63"], "brent_usd": leg_brent_full,
          "brent_rub": align_lag(gap24(br_mavg * usd_mavg.reindex(br_mavg.index)))}
lm14 = pd.DataFrame(legs14).groupby(idx.to_period("M")).last(); lm14.index = me_dates
c14_usd, z14u = composite_monthly(lm14[["usd_mom63", "brent_usd"]], {"usd_mom63": 1, "brent_usd": -1})
c14_rub, z14r = composite_monthly(lm14[["usd_mom63", "brent_rub"]], {"usd_mom63": 1, "brent_rub": -1})
t14 = pd.DataFrame({"comp_brent$": c14_usd, "comp_brent₽": c14_rub, "z_brent$(−)": z14u["brent_usd"], "z_brent₽(−)": z14r["brent_rub"],
                    "z_usd_mom63": z14u["usd_mom63"], "toxic": tox_me, "fwd1m_pct": (np.exp(fwd1) - 1) * 100}).loc["2014-06-30":"2015-06-30"]
print(t14.to_string(float_format=lambda v: f"{v:.2f}"))
t14["episode"] = "2014"
pd.concat(ep_rows + [t14]).to_csv("results/S_3_episodes.csv")
# 2014-15 стратегии по двум прокси (2 ноги usd_mom63 + brent) на 2013-2016
for nm, c in [("usd_mom63 + Brent$ (2 ноги)", c14_usd), ("usd_mom63 + Brent₽ (2 ноги)", c14_rub)]:
    p_ = position_from(c)
    for w, (a, b) in [("2013-2016", ("2013-01-01", "2016-12-31")), ("2010-2026", WINDOWS["2010-2026"]), ("2004-2026", WINDOWS["2004-2026"])]:
        mt = metrics(run_daily(p_, d, start=a, end=b), f"{nm} [{w}]")
        print(line(mt))

# ---------------------------------------------------------------- своевременность
print("\n=== СВОЕВРЕМЕННОСТЬ (правило 7), MCFTR > 15%, 2017+ ===")
tl = None
for nm in cand_names[:3]:
    t = timeliness(pos_v[nm].loc["2017":], d["mcftr"].loc["2017":])
    t = t.rename(columns={c: f"{c}|{nm[:10]}" for c in ["long_at_peak", "days_to_exit", "avoided", "days_to_entry", "missed_half_rec"]})
    tl = t if tl is None else tl.merge(t, on=["peak", "trough", "depth"])
tl.to_csv("results/S_3_timeliness.csv", index=False)
print(tl.to_string(index=False, float_format=lambda v: f"{v:.2f}"))
print("\nготово: results/S_3_*.csv")
