"""S2 — находка 2: четвёртая нога y2−key и usd/MA200 вместо usd_mom63 (вариант P5).

(a) независимое построение ног и композитов; (b) leave-one-year-out по Шарпу и IC; (c) плата за перебор —
reality-check по семейству спецификаций того же класса + нулевое семейство со сдвигом ног; (d) двойной счёт
с наклоном / y1−key, эпизоды 2014-12 и 2022-02; (e) помесячно 2024–2026. Запуск: python scripts/S2_2_p5.py
"""
import sys
sys.path.insert(0, "scripts")
import itertools
import numpy as np
import pandas as pd
from S2_lib import *  # noqa

D, C, M = load_all()
cuts = month_end_cuts(D.index)
log = open(f"{RES}/S2_2_log.txt", "w", encoding="utf-8")


def P(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    log.write(s + "\n")


usd = D["usd"]
LEG_DEF = {  # id: (дневной ряд, знак)
    "usd_mom21": (np.log(usd / usd.shift(21)), +1),
    "usd_mom63": (D["usd_mom63"], +1),
    "usd_mom126": (np.log(usd / usd.shift(126)), +1),
    "usd_ma100": (np.log(usd / usd.rolling(100).mean()), +1),
    "usd_ma200": (np.log(usd / usd.rolling(200).mean()), +1),
    "slope_10_2": (D["slope_10_2"], +1),
    "slope_10_1": (D["y10"] - D["y1"], +1),
    "y1_key": (D["y1"] - D["key_rate"], +1),
    "y2_key": (D["y2"] - D["key_rate"], +1),
    "y10_key": (D["y10"] - D["key_rate"], +1),
    "dy2_63": (D["y2"] - D["y2"].shift(63), -1),
    "urals_rub_gap": (D["urals_rub_gap"], -1),
    "rb_gap": (D["rb_gap"], -1),
    "brent_mom63": (D["brent_mom63"], -1),
}
RAW = pd.DataFrame({k: v[0].reindex(cuts) for k, v in LEG_DEF.items()})
Z = pd.DataFrame({k: zroll(RAW[k]) for k in LEG_DEF})            # без знака
ZS = pd.DataFrame({k: LEG_DEF[k][1] * Z[k] for k in LEG_DEF})    # со знаком
gate_m = (D["cell"].reindex(cuts) != TOXIC).astype(float)
tr = C["mcftr_ffill"]
mmc = (1 + C["mm_rate"] / 100.0 / 252.0).cumprod()
t_, m_, px = tr.reindex(cuts), mmc.reindex(cuts), D["imoex"].reindex(cuts)
FM = pd.DataFrame({"fwd_tr": t_.shift(-1) / t_ - 1, "fwd_mm": m_.shift(-1) / m_ - 1, "fwd_imoex": np.log(px.shift(-1) / px)})
FM.loc[cuts[-1], ["fwd_tr", "fwd_mm", "fwd_imoex"]] = np.nan  # последний закрытый срез: форвард = незавершённый месяц → не используем
FM = FM[FM.index <= pd.Timestamp(MAIN[1])]


def comp_of(legs):
    return ZS[list(legs)].mean(axis=1, skipna=True)


def fast_ret(pos, cost=COST):
    p = pos.reindex(FM.index).fillna(0).values
    tr_ = np.abs(np.diff(np.r_[p[0], p]))
    r = p * FM["fwd_tr"].values + (1 - p) * FM["fwd_mm"].values - cost * tr_
    return pd.Series(r, index=FM.index), pd.Series(tr_, index=FM.index)


def win(s, w):
    return s[(s.index >= pd.Timestamp(w[0])) & (s.index <= pd.Timestamp(w[1]))]


def panel(comp, thr=0.1):
    sg = hysteresis_sign(comp, thr)
    pos = ((sg > 0) & (gate_m > 0)).astype(float)
    return fast_ret(pos)[0], pos


def evaluate(comp, name, windows=None):
    r, pos = panel(comp)
    out = dict(spec=name)
    for wname, w in (windows or WINDOWS).items():
        rr = win(r, w).dropna()
        cc = win(comp, w)
        ff = win(FM["fwd_imoex"], w)
        ic, n = spearman_ic(cc, ff)
        out[f"ic_{wname}"] = ic
        out[f"sh_{wname}"] = sharpe(rr)
        if wname.startswith("MAIN"):
            out["n"] = n
            out["nw_t"] = nw_t_rank(cc, ff, 1)
            out["p_boot"] = boot_ic_p(cc, ff)
            out["cagr"] = (1 + rr).prod() ** (12 / len(rr)) - 1
            out["mdd"] = maxdd(rr)
            out["trades_yr"] = fast_ret(pos)[1].reindex(rr.index).sum() / (len(rr) / 12)
            out["time_in"] = pos.reindex(rr.index).mean()
            out["sh_ex2022"] = sharpe(rr[rr.index.year != 2022])
            out["ic_ex2022"] = spearman_ic(cc[cc.index.year != 2022], ff[ff.index.year != 2022])[0]
    return out, r


SPECS = {
    "P0 прод": ["usd_mom63", "slope_10_2", "urals_rub_gap"],
    "P3 usd/MA200": ["usd_ma200", "slope_10_2", "urals_rub_gap"],
    "P4 прод + y2−key": ["usd_mom63", "slope_10_2", "urals_rub_gap", "y2_key"],
    "P5 usd/MA200 + y2−key": ["usd_ma200", "slope_10_2", "urals_rub_gap", "y2_key"],
    "соло usd_mom63": ["usd_mom63"], "соло usd/MA200": ["usd_ma200"], "соло slope": ["slope_10_2"], "соло urals": ["urals_rub_gap"],
    "соло y2−key": ["y2_key"], "соло y1−key": ["y1_key"], "соло y10−key": ["y10_key"],
}
P("[a] независимое построение (z 60/24/±3 на месячных срезах; панель = ворота + гистерезис 0,1; MCFTR/mm; 0,2 %)")
rows = []
RETS = {}
for name, legs in SPECS.items():
    o, r = evaluate(comp_of(legs), name)
    rows.append(o)
    RETS[name] = r
tab = pd.DataFrame(rows)
tab.to_csv(f"{RES}/S2_p5_specs.csv", index=False, float_format="%.4f")
cols = ["spec", "n", "ic_MAIN 2010-26", "nw_t", "p_boot", "sh_MAIN 2010-26", "cagr", "mdd", "trades_yr", "time_in", "ic_A 2004-17", "ic_B 2018-26",
        "sh_A 2004-17", "sh_B 2018-26", "sh_2010-21", "sh_2022-24", "sh_2025-26", "sh_ex2022", "ic_ex2022"]
P(tab[cols].round(3).to_string())
r0 = RETS["P0 прод"]
for name in ["P3 usd/MA200", "P4 прод + y2−key", "P5 usd/MA200 + y2−key"]:
    a = win(RETS[name], MAIN).dropna()
    b = win(r0, MAIN).reindex(a.index)
    d, p, lo, hi = boot_sharpe_diff(a.values, b.values)
    P(f"  ΔШарп({name} − P0) MAIN = {d:+.2f}, p={p:.3f}, ДИ90 [{lo:+.2f}; {hi:+.2f}]")
# проверка знака y2−key: перевёрнутый
o_rev, _ = evaluate(ZS[["usd_ma200", "slope_10_2", "urals_rub_gap"]].join(-ZS["y2_key"]).mean(axis=1), "P5 с y2−key со знаком −")
P(f"  контроль: P5 с перевёрнутым знаком y2−key: IC {o_rev['ic_MAIN 2010-26']:.3f}, Шарп {o_rev['sh_MAIN 2010-26']:.2f}")

# ------------------------------------------------------------ (b) leave-one-year-out
P("\n[b] leave-one-year-out (MAIN): Шарп и IC без года Y; по годам — доходность P5 − P0")
years = list(range(2010, 2027))
rows = []
for y in years:
    row = dict(year_out=y)
    for name in ["P0 прод", "P3 usd/MA200", "P4 прод + y2−key", "P5 usd/MA200 + y2−key", "соло usd/MA200", "соло y2−key"]:
        r = win(RETS[name], MAIN).dropna()
        c = win(comp_of(SPECS[name]), MAIN)
        f = win(FM["fwd_imoex"], MAIN)
        m = r.index.year != y
        row[f"sh_{name}"] = sharpe(r[m])
        row[f"ic_{name}"] = spearman_ic(c[c.index.year != y], f[f.index.year != y])[0]
    ry5 = win(RETS["P5 usd/MA200 + y2−key"], MAIN)
    ry0 = win(RETS["P0 прод"], MAIN)
    row["year_ret_P0"] = (1 + ry0[ry0.index.year == y]).prod() - 1
    row["year_ret_P5"] = (1 + ry5[ry5.index.year == y]).prod() - 1
    row["year_ic_P0"] = spearman_ic(win(comp_of(SPECS["P0 прод"]), MAIN)[lambda s: s.index.year == y], win(FM["fwd_imoex"], MAIN)[lambda s: s.index.year == y])[0]
    row["year_ic_P5"] = spearman_ic(win(comp_of(SPECS["P5 usd/MA200 + y2−key"]), MAIN)[lambda s: s.index.year == y], win(FM["fwd_imoex"], MAIN)[lambda s: s.index.year == y])[0]
    rows.append(row)
loyo = pd.DataFrame(rows)
loyo.to_csv(f"{RES}/S2_p5_loyo.csv", index=False, float_format="%.4f")
loyo["d_sh_P5_P0"] = loyo["sh_P5 usd/MA200 + y2−key"] - loyo["sh_P0 прод"]
loyo["d_ic_P5_P0"] = loyo["ic_P5 usd/MA200 + y2−key"] - loyo["ic_P0 прод"]
P(loyo[["year_out", "sh_P0 прод", "sh_P5 usd/MA200 + y2−key", "d_sh_P5_P0", "ic_P0 прод", "ic_P5 usd/MA200 + y2−key", "d_ic_P5_P0", "year_ret_P0", "year_ret_P5", "year_ic_P0", "year_ic_P5"]].round(3).to_string())
P(f"  ΔШарп(P5−P0) без года: min {loyo.d_sh_P5_P0.min():+.2f}, max {loyo.d_sh_P5_P0.max():+.2f}; ΔIC: min {loyo.d_ic_P5_P0.min():+.3f}, max {loyo.d_ic_P5_P0.max():+.3f}")
yd = loyo.year_ret_P5 - loyo.year_ret_P0
P(f"  годовая разность доходности P5−P0: лет с плюсом {int((yd > 0.005).sum())}, с минусом {int((yd < -0.005).sum())}, равных {int((yd.abs() <= 0.005).sum())}; крупнейшие: " +
  ", ".join(f"{y}:{v*100:+.1f}" for y, v in zip(loyo.year_out, yd) if abs(v) > 0.03))

# ------------------------------------------------------------ (c) плата за перебор
P("\n[c] семейство спецификаций того же класса: usd-нога × ставочная × нефтяная × (нет / +y1−key / +y2−key / +y10−key)")
USD = ["usd_mom21", "usd_mom63", "usd_mom126", "usd_ma100", "usd_ma200"]
RATE = ["slope_10_2", "slope_10_1", "y1_key", "y2_key", "y10_key", "dy2_63"]
OIL = ["urals_rub_gap", "rb_gap", "brent_mom63"]
EXTRA = [None, "y1_key", "y2_key", "y10_key"]
fam = {}
for u, rt, o, e in itertools.product(USD, RATE, OIL, EXTRA):
    legs = [u, rt, o] + ([e] if e and e != rt else [])
    fam["+".join(legs)] = legs
P(f"  всего спецификаций в полном семействе: {len(fam)}")
famR = {}
famIC = {}
for name, legs in fam.items():
    r, _ = panel(comp_of(legs))
    rr = win(r, MAIN).dropna()
    famR[name] = rr
    famIC[name] = spearman_ic(win(comp_of(legs), MAIN), win(FM["fwd_imoex"], MAIN))[0]
famR = pd.DataFrame(famR)
base = famR["usd_mom63+slope_10_2+urals_rub_gap"]
shs = famR.apply(sharpe).sort_values(ascending=False)
ics = pd.Series(famIC).sort_values(ascending=False)
p5name = "usd_ma200+slope_10_2+urals_rub_gap+y2_key"
P(f"  Шарп P0 {shs['usd_mom63+slope_10_2+urals_rub_gap']:.2f}; P5 {shs[p5name]:.2f} — ранг {int((shs > shs[p5name]).sum()) + 1} из {len(shs)}; "
  f"лучшая: {shs.index[0]} {shs.iloc[0]:.2f}; медиана семейства {shs.median():.2f}; доля спецификаций с Шарпом > P0: {(shs > shs['usd_mom63+slope_10_2+urals_rub_gap']).mean():.0%}")
P(f"  IC: P0 {ics['usd_mom63+slope_10_2+urals_rub_gap']:.3f}; P5 {ics[p5name]:.3f} — ранг {int((ics > ics[p5name]).sum()) + 1}; лучшая {ics.index[0]} {ics.iloc[0]:.3f}; медиана {ics.median():.3f}")
top = pd.DataFrame({"sharpe": shs.head(15), "ic": ics.reindex(shs.head(15).index)})
P(top.round(3).to_string())
pd.DataFrame({"sharpe": shs, "ic": ics.reindex(shs.index)}).to_csv(f"{RES}/S2_p5_family.csv", float_format="%.4f")

# reality check: H0 — ни одна спецификация не лучше P0 по Шарпу; max по семейству ΔШарп
rng = np.random.default_rng(11)
Rmat = famR.values
b = base.values
n = len(b)
obs_d = np.array([sharpe(Rmat[:, j]) - sharpe(b) for j in range(Rmat.shape[1])])
names = list(famR.columns)
j5 = names.index(p5name)
# подсемейство «как у C» (≈74): одиночные подмены + 4-я нога + P5-подобные сочетания
sub = [n_ for n_ in names if (n_.count("+") == 2 and sum(x != y for x, y in zip(n_.split("+"), ["usd_mom63", "slope_10_2", "urals_rub_gap"])) <= 1)
       or (n_.count("+") == 3 and n_.split("+")[:3] == ["usd_mom63", "slope_10_2", "urals_rub_gap"])
       or (n_.count("+") == 3 and n_.split("+")[1:3] == ["slope_10_2", "urals_rub_gap"] and n_.split("+")[0] in USD)]
sub = sorted(set(sub))
jsub = [names.index(x) for x in sub]
P(f"  подсемейство «одна подмена / 4-я нога / usd-подмена + 4-я нога»: {len(sub)} спецификаций (P5 внутри: {p5name in sub})")
REPS = 2000
mx_full, mx_sub, d5 = [], [], []
for k in range(REPS):
    ix = stationary_bootstrap_indices(n, 8, rng)
    sb = sharpe(b[ix])
    d = np.array([sharpe(Rmat[ix, j]) - sb for j in range(Rmat.shape[1])])
    d5.append(d[j5])
    mx_full.append(d)
mx_full = np.array(mx_full)
cen = mx_full - mx_full.mean(axis=0)
p_rc_full = float(np.mean(cen.max(axis=1) >= obs_d.max()))
p_rc_sub = float(np.mean(cen[:, jsub].max(axis=1) >= obs_d[jsub].max()))
p_rc_p5_full = float(np.mean(cen.max(axis=1) >= obs_d[j5]))
p_rc_p5_sub = float(np.mean(cen[:, jsub].max(axis=1) >= obs_d[j5]))
p_naive = float(np.mean(np.abs(cen[:, j5]) >= abs(obs_d[j5])))
P(f"  наблюдаемый ΔШарп(P5−P0) = {obs_d[j5]:+.2f}; наивный p (без поправки) = {p_naive:.3f}")
P(f"  reality check: p(max ΔШарп по подсемейству {len(sub)} ≥ ΔP5) = {p_rc_p5_sub:.3f}; p(max по полному семейству {len(names)} ≥ ΔP5) = {p_rc_p5_full:.3f}; "
  f"наблюдаемый max ΔШарп: подсемейство {obs_d[jsub].max():+.2f} (p={p_rc_sub:.3f}), полное {obs_d.max():+.2f} (p={p_rc_full:.3f})")
P(f"  типичный max ΔШарп под H0 (центрированный бутстреп): подсемейство {np.percentile(cen[:, jsub].max(axis=1), 50):+.2f} (95-й {np.percentile(cen[:, jsub].max(axis=1), 95):+.2f}); "
  f"полное {np.percentile(cen.max(axis=1), 50):+.2f} (95-й {np.percentile(cen.max(axis=1), 95):+.2f})")

# нулевое семейство: 74 случайные спецификации с циклическим сдвигом ног (та же автокорреляция, без связи с рынком)
P("\n[c] нулевое семейство: 74 случайные спецификации того же класса, каждая нога циклически сдвинута на 24…96 мес (200 повторов)")
rng2 = np.random.default_rng(5)
keys = list(fam.keys())
best_null, best_null_ic, p5_like = [], [], []


def shifted_z(k, lag):
    z = ZS[k].dropna()
    v = np.roll(z.values, lag)
    return pd.Series(v, index=z.index).reindex(ZS.index)


for rep in range(200):
    pick = rng2.choice(len(keys), 74, replace=False)
    best = -9
    best_ic = -9
    for i in pick:
        legs = fam[keys[i]]
        cols = []
        for k in legs:
            lag = int(rng2.integers(24, 97))
            cols.append(shifted_z(k, lag))
        comp = pd.concat(cols, axis=1).mean(axis=1, skipna=True)
        r, _ = panel(comp)
        s_ = sharpe(win(r, MAIN).dropna())
        ic_ = spearman_ic(win(comp, MAIN), win(FM["fwd_imoex"], MAIN))[0]
        best = max(best, s_)
        best_ic = max(best_ic, ic_)
    best_null.append(best)
    best_null_ic.append(best_ic)
best_null = np.array(best_null)
best_null_ic = np.array(best_null_ic)
P(f"  лучший Шарп из 74 нулевых спецификаций: медиана {np.median(best_null):.2f}, 95-й перцентиль {np.percentile(best_null, 95):.2f}, max {best_null.max():.2f}; "
  f"P(≥ P5 1,57) = {(best_null >= shs[p5name]).mean():.3f}; P(≥ P0 1,07) = {(best_null >= shs['usd_mom63+slope_10_2+urals_rub_gap']).mean():.3f}")
P(f"  лучший IC из 74 нулевых: медиана {np.median(best_null_ic):.3f}, 95-й {np.percentile(best_null_ic, 95):.3f}; P(≥ P5 {ics[p5name]:.3f}) = {(best_null_ic >= ics[p5name]).mean():.3f}")
pd.DataFrame({"best_sharpe_null74": best_null, "best_ic_null74": best_null_ic}).to_csv(f"{RES}/S2_p5_null_family.csv", index=False, float_format="%.4f")

# кросс-валидация выбора спецификации: выбрать лучшую по Шарпу на других годах, применить к году
P("\n[c] кросс-валидация выбора спецификации (LOYO по семейству): выбираем max Шарпа на остальных годах")
cvr = pd.Series(index=famR.index, dtype=float)
cvp = {}
for y in years:
    m = famR.index.year != y
    pick = famR[m].apply(sharpe).idxmax()
    cvp[y] = pick
    cvr.loc[~m] = famR.loc[~m, pick]
P(f"  выбранные по годам: {cvp}")
P(f"  CV-Шарп выбора из {len(names)}: {sharpe(cvr):.2f}; P0 {sharpe(base):.2f}; P5 in-sample {sharpe(famR[p5name]):.2f}")
cvr2 = pd.Series(index=famR.index, dtype=float)
cvp2 = {}
for y in years:
    m = famR.index.year != y
    pick = famR.loc[m, sub].apply(sharpe).idxmax()
    cvp2[y] = pick
    cvr2.loc[~m] = famR.loc[~m, pick]
P(f"  CV-Шарп выбора из подсемейства {len(sub)}: {sharpe(cvr2):.2f}; выбранные: {sorted(set(cvp2.values()))}")

# ------------------------------------------------------------ (d) двойной счёт и эпизоды
P("\n[d] корреляции z ног (MAIN, общая выборка с 2017): Пирсон / Спирмен")
zz = win(Z[["slope_10_2", "y1_key", "y2_key", "y10_key", "usd_mom63", "usd_ma200", "urals_rub_gap"]], MAIN).dropna()
P(f"  n = {len(zz)}")
P(zz.corr().round(2).to_string())
P(zz.corr(method="spearman").round(2).to_string())
zz.corr().to_csv(f"{RES}/S2_p5_corr.csv", float_format="%.3f")
rr = win(RAW[["slope_10_2", "y1_key", "y2_key", "y10_key"]], MAIN).dropna()
P("  сырьё: " + ", ".join(f"corr({a},{b})={rr[a].corr(rr[b]):.2f}" for a, b in [("y2_key", "slope_10_2"), ("y2_key", "y1_key"), ("y2_key", "y10_key")]))
# IC y2−key при контроле наклона: частная корреляция рангов
from scipy import stats
d = pd.DataFrame({"y2k": win(Z["y2_key"], MAIN), "sl": win(Z["slope_10_2"], MAIN), "f": win(FM["fwd_imoex"], MAIN)}).dropna()
rk = d.rank()
import statsmodels.api as sm
res = sm.OLS(rk["f"], sm.add_constant(rk[["y2k", "sl"]])).fit(cov_type="HAC", cov_kwds={"maxlags": 1})
P(f"  ранговая регрессия fwd ~ y2−key + slope (n={len(d)}): t(y2−key)={res.tvalues['y2k']:.2f}, t(slope)={res.tvalues['sl']:.2f}; соло IC y2−key {stats.spearmanr(d.y2k, d.f).correlation:.3f}, slope {stats.spearmanr(d.sl, d.f).correlation:.3f}")

P("\n[d] эпизоды экстренных повышений ставки: y2 доступен с 2015-01-05 (2014-12 отсутствует в данных прода) — показываем 2015-01…06 и 2021-12…2022-08")
comp0 = comp_of(SPECS["P0 прод"])
comp5 = comp_of(SPECS["P5 usd/MA200 + y2−key"])
r5, pos5 = panel(comp5)
r0_, pos0 = panel(comp0)
ep = pd.DataFrame({"y2": RAW["y2_key"] + D["key_rate"].reindex(cuts), "key": D["key_rate"].reindex(cuts), "y2_key": RAW["y2_key"], "z_y2key": Z["y2_key"],
                   "z_slope": Z["slope_10_2"], "z_usd_ma200": Z["usd_ma200"], "z_usd_mom63": Z["usd_mom63"], "z_urals(−)": ZS["urals_rub_gap"],
                   "P0": comp0, "P5": comp5, "gate": gate_m, "pos0": pos0, "pos5": pos5, "fwd_tr−mm": FM["fwd_tr"] - FM["fwd_mm"]})
for a, b in [("2015-01-01", "2015-06-30"), ("2021-11-01", "2022-09-30"), ("2023-07-01", "2024-03-31")]:
    P(ep.loc[a:b].round(2).to_string())
ep.to_csv(f"{RES}/S2_p5_episodes.csv", float_format="%.4f")

# ------------------------------------------------------------ (e) 2024–2026 помесячно
P("\n[e] помесячно 2024-01…2026-08: z ног, композиты, позиции, избыток MCFTR над деньгами")
tbl = ep.loc["2024-01-01":"2026-08-31"].copy()
tbl["cum_ex_P0"] = ((1 + win(r0_, ("2024-01-01", "2026-08-31"))).cumprod() / (1 + win(FM["fwd_mm"], ("2024-01-01", "2026-08-31"))).cumprod() - 1).reindex(tbl.index)
tbl["cum_ex_P5"] = ((1 + win(r5, ("2024-01-01", "2026-08-31"))).cumprod() / (1 + win(FM["fwd_mm"], ("2024-01-01", "2026-08-31"))).cumprod() - 1).reindex(tbl.index)
P(tbl[["y2", "key", "y2_key", "z_y2key", "z_slope", "z_usd_ma200", "z_usd_mom63", "z_urals(−)", "P0", "P5", "gate", "pos0", "pos5", "fwd_tr−mm", "cum_ex_P0", "cum_ex_P5"]].round(2).to_string())
tbl.to_csv(f"{RES}/S2_p5_2024_2026.csv", float_format="%.4f")
zk = win(Z["y2_key"], ("2024-01-01", "2026-08-31"))
P(f"  z(y2−key) в 2024–26: min {zk.min():.2f}, медиана {zk.median():.2f}, доля месяцев на обрезке −3: {(zk <= -2.999).mean():.0%}; доля месяцев z<−1: {(zk < -1).mean():.0%}")
# что даёт y2−key вне 2025–26: Шарп P4 vs P0 на 2017–2024
o4, _ = evaluate(comp_of(SPECS["P4 прод + y2−key"]), "P4", {"2017-24": ("2017-03-01", "2024-12-31")})
o0, _ = evaluate(comp_of(SPECS["P0 прод"]), "P0", {"2017-24": ("2017-03-01", "2024-12-31")})
o5, _ = evaluate(comp_of(SPECS["P5 usd/MA200 + y2−key"]), "P5", {"2017-24": ("2017-03-01", "2024-12-31")})
P(f"  окно 2017-03…2024-12 (нога доступна, без эры 2025–26): Шарп P0 {o0['sh_2017-24']:.2f}, P4 {o4['sh_2017-24']:.2f}, P5 {o5['sh_2017-24']:.2f}; IC P0 {o0['ic_2017-24']:.3f}, P4 {o4['ic_2017-24']:.3f}, P5 {o5['ic_2017-24']:.3f}")
log.close()
