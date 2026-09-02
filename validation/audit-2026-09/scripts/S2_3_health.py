"""S2 — находка 3: критерий здоровья. Сравнение трёх критериев на истории 2004–2026 и в симуляции
(истинный IC 0,2 → 0 с некоторого месяца; 500 повторов): ложные тревоги за 5 лет и время до обнаружения.
Критерии: (A) IC-24 < 0 k месяцев подряд; (B) CUSUM умения тайминга (Пейдж, односторонний);
(C) F2: 24-мес избыток над деньгами < −10 % или доля верных месяцев < 40 %; плюс справочные.
Запуск: python scripts/S2_3_health.py
"""
import sys
sys.path.insert(0, "scripts")
import numpy as np
import pandas as pd
from scipy import stats
from S2_lib import *  # noqa

log = open(f"{RES}/S2_3_log.txt", "w", encoding="utf-8")


def P(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    log.write(s + "\n")


# ------------------------------------------------------------ общие функции критериев
def rank(a):
    return stats.rankdata(a)


def rolling_ic(sig, ret, w=24):
    """IC-24 на закрытых месяцах. sig[i] — сигнал на срезе i−1 (уже сдвинут), ret[i] — доходность месяца i
    по этому сигналу; пара i закрыта в конце месяца i. На месяце t берём последние w закрытых пар (t−w+1..t) —
    как health.py."""
    n = len(sig)
    out = np.full(n, np.nan)
    for t in range(w - 1, n):
        s = sig[t - w + 1:t + 1]
        r = ret[t - w + 1:t + 1]
        m = np.isfinite(s) & np.isfinite(r)
        if m.sum() >= w // 2 + 1:
            out[t] = stats.spearmanr(s[m], r[m]).correlation
    return out


def streak_below(x, k):
    """True в месяце t, если x < 0 в последних k месяцах подряд (включая t)."""
    n = len(x)
    out = np.zeros(n, dtype=bool)
    c = 0
    for t in range(n):
        c = c + 1 if (np.isfinite(x[t]) and x[t] < 0) else 0
        out[t] = c >= k
    return out


def skill_series(pos_prev, ex, pbar_w=60):
    """c_t = (pos_{t-1} − p̄)(r_tr − r_mm)_t, p̄ — скользящее среднее позиции за pbar_w (min 24) до t-1 включительно."""
    p = pd.Series(pos_prev)
    pbar = p.rolling(pbar_w, min_periods=24).mean().values
    return (pos_prev - pbar) * ex


def page_cusum(c, mu0_w=120, sig_w=120, k_frac=0.5, min_n=60):
    """Односторонний CUSUM Пейджа на снижение среднего умения: S_t = max(0, S_{t-1} + (mu0*k_frac − c_t)/σ),
    mu0 и σ — по скользящему окну до t−1 (min min_n). Возвращает S_t (в σ)."""
    s = pd.Series(c)
    mu0 = s.rolling(mu0_w, min_periods=min_n).mean().shift(1).values
    sig = s.rolling(sig_w, min_periods=min_n).std().shift(1).values
    n = len(c)
    S = np.full(n, np.nan)
    cur = 0.0
    for t in range(n):
        if np.isfinite(c[t]) and np.isfinite(mu0[t]) and np.isfinite(sig[t]) and sig[t] > 0:
            cur = max(0.0, cur + (max(mu0[t], 0.0) * k_frac - c[t]) / sig[t])
            S[t] = cur
    return S


def cum_excess(strat_ex, w):
    return pd.Series(strat_ex).rolling(w, min_periods=w).sum().values


def share_correct(pos_prev, ex, w=24):
    right = np.where(pos_prev > 0.5, ex > 0, ex <= 0).astype(float)
    right[~np.isfinite(ex)] = np.nan
    return pd.Series(right).rolling(w, min_periods=w).mean().values


def fronts(alarm):
    """Индексы первых месяцев тревоги (переход False→True)."""
    a = np.asarray(alarm, bool)
    prev = np.r_[False, a[:-1]]
    return np.where(a & ~prev)[0]


# ------------------------------------------------------------ 1. история 2004–2026
D, C, M = load_all()
cuts = month_end_cuts(D.index)
raw_m = pd.DataFrame({k: D[k].reindex(cuts) for k, _ in LEGS_PROD})
Zm = pd.DataFrame({k: sgn * zroll(raw_m[k]) for k, sgn in LEGS_PROD})
comp_m, _ = composite_mean(Zm)
gate_m = (D["cell"].reindex(cuts) != TOXIC).astype(float)
tr = C["mcftr_ffill"]
mmc = (1 + C["mm_rate"] / 100.0 / 252.0).cumprod()
t_, m_, px = tr.reindex(cuts), mmc.reindex(cuts), D["imoex"].reindex(cuts)
F = pd.DataFrame({"fwd_tr": t_.shift(-1) / t_ - 1, "fwd_mm": m_.shift(-1) / m_ - 1, "fwd_imoex": np.log(px.shift(-1) / px)})
sg = hysteresis_sign(comp_m, 0.1)
pos = ((sg > 0) & (gate_m > 0)).astype(float)
H = pd.DataFrame({"comp": comp_m, "pos": pos, "fwd_tr": F.fwd_tr, "fwd_mm": F.fwd_mm, "fwd_imoex": F.fwd_imoex})
H = H[(H.index >= "2003-06-01") & (H.index <= "2026-08-31")]  # последняя закрытая пара: срез 2026-07-31 → август 2026
# переиндексуем «по месяцу доходности»: строка t = доходность месяца t (решение на срезе t−1)
R = pd.DataFrame(index=H.index[1:])
R["ret_month"] = (H.pos.shift(1) * H.fwd_tr.shift(1) + (1 - H.pos.shift(1)) * H.fwd_mm.shift(1)).iloc[1:].values
R["ex_mkt"] = (H.fwd_tr.shift(1) - H.fwd_mm.shift(1)).iloc[1:].values       # MCFTR − деньги за месяц t
R["pos_prev"] = H.pos.shift(1).iloc[1:].values
R["sig_prev"] = H.comp.shift(1).iloc[1:].values                              # композит на срезе t−1
R["strat_ex"] = R.pos_prev * R.ex_mkt                                        # избыток стратегии над деньгами (без издержек)
R["fwd_imoex_m"] = H.fwd_imoex.shift(1).iloc[1:].values
R["ic24"] = rolling_ic(R.sig_prev.values, R.fwd_imoex_m.values, 24)
R["ic36"] = rolling_ic(R.sig_prev.values, R.fwd_imoex_m.values, 36)
R["skill"] = skill_series(R.pos_prev.values, R.ex_mkt.values)
R["cusum"] = page_cusum(R.skill.values)
R["ex24"] = cum_excess(R.strat_ex.values, 24)
R["ex12"] = cum_excess(R.strat_ex.values, 12)
R["share24"] = share_correct(R.pos_prev.values, R.ex_mkt.values, 24)
R["skill24"] = pd.Series(R.skill.values).rolling(24, min_periods=24).mean().values
R["fwd12_skill"] = pd.Series(R.skill.values[::-1]).rolling(12).mean().values[::-1]          # среднее умение в следующие 12 мес (включая t? нет — сдвинем)
R["fwd12_skill"] = R["fwd12_skill"].shift(-1)
R["fwd12_strat_ex"] = pd.Series(R.strat_ex.values[::-1]).rolling(12).sum().values[::-1]
R["fwd12_strat_ex"] = R["fwd12_strat_ex"].shift(-1)
R["fwd12_ic"] = np.nan
sv, fv = R.sig_prev.values, R.fwd_imoex_m.values
for t in range(len(R) - 12):
    R.iloc[t, R.columns.get_loc("fwd12_ic")] = stats.spearmanr(sv[t + 1:t + 13], fv[t + 1:t + 13]).correlation
R = R[R.index >= "2004-01-01"]
R.to_csv(f"{RES}/S2_health_series.csv", float_format="%.4f")
P(f"[1] история: месяцев {len(R)} (2004-01…2026-08); умение c_t среднее {R.skill.mean()*100:+.3f} %/мес (t={R.skill.mean()/R.skill.std()*np.sqrt(len(R)):.2f}); "
  f"2010–24: {R.skill['2010':'2024'].mean()*100:+.3f}; 2025–26: {R.skill['2025':].mean()*100:+.3f}; IC-24 на 2026-08: {R.ic24.iloc[-1]:.3f}; CUSUM сейчас {R.cusum.iloc[-1]:.2f}σ")

CRIT = {
    "A1: IC-24 < 0 (первый месяц)": streak_below(R.ic24.values, 1),
    "A6: IC-24 < 0 шесть подряд (регламент)": streak_below(R.ic24.values, 6),
    "A3: IC-24 < 0 три подряд": streak_below(R.ic24.values, 3),
    "A36: IC-36 < 0": streak_below(R.ic36.values, 1),
    "B: CUSUM умения > 5σ": R.cusum.values > 5,
    "B: CUSUM умения > 8σ": R.cusum.values > 8,
    "B: CUSUM умения > 3σ": R.cusum.values > 3,
    "B24: умение за 24 мес < 0": R.skill24.values < 0,
    "C: избыток над деньгами 24м < −10 %": R.ex24.values < -0.10,
    "C: доля верных 24м < 40 %": R.share24.values < 0.40,
    "C: F2 (избыток24 < −10 % ИЛИ доля < 40 %)": (R.ex24.values < -0.10) | (R.share24.values < 0.40),
    "D: избыток над деньгами 12м < 0": R.ex12.values < 0,
}
rows = []
unc_skill = R.fwd12_skill.mean()
unc_ex = R.fwd12_strat_ex.mean()
unc_ic = R.fwd12_ic.mean()
for name, al in CRIT.items():
    fr = fronts(al)
    fr_dates = [R.index[i].strftime("%Y-%m") for i in fr]
    after_skill = R.fwd12_skill.values[fr]
    after_ex = R.fwd12_strat_ex.values[fr]
    after_ic = R.fwd12_ic.values[fr]
    rows.append(dict(criterion=name, fronts=len(fr), months_in_alarm=np.nanmean(al.astype(float)),
                     fwd12_skill_after=np.nanmean(after_skill) if len(fr) else np.nan, fwd12_skill_uncond=unc_skill,
                     fwd12_strat_ex_after=np.nanmean(after_ex) if len(fr) else np.nan, fwd12_strat_ex_uncond=unc_ex,
                     fwd12_ic_after=np.nanmean(after_ic) if len(fr) else np.nan, fwd12_ic_uncond=unc_ic,
                     share_neg_skill_after=np.nanmean(after_skill < 0) if len(fr) else np.nan,
                     share_neg_skill_uncond=np.nanmean(R.fwd12_skill < 0),
                     dates=", ".join(fr_dates)))
hist = pd.DataFrame(rows)
hist.to_csv(f"{RES}/S2_health_history.csv", index=False, float_format="%.4f")
P("\n[1] критерии на истории 2004–2026: фронты тревог, доля месяцев в тревоге, среднее умение / избыток стратегии / IC в следующие 12 мес после фронта против безусловного")
P(hist.drop(columns="dates").round(4).to_string())
for _, r in hist.iterrows():
    P(f"  {r.criterion}: {r.dates}")
# прогнозная сила статистик: Спирмен(статистика_t, умение в следующие 12 мес)
P("\n[1] прогнозная сила статистик здоровья на следующие 12 мес (Спирмен; NW-t с лагом 12 — перекрывающиеся окна):")
for col in ["ic24", "ic36", "cusum", "skill24", "ex24", "share24", "ex12"]:
    d = R[[col, "fwd12_skill", "fwd12_strat_ex", "fwd12_ic"]].dropna()
    P(f"  {col:8s} n={len(d)}: → умение {stats.spearmanr(d[col], d.fwd12_skill).correlation:+.3f} (t={nw_t_rank(d[col], d.fwd12_skill, 12):+.2f}); "
      f"→ избыток стратегии {stats.spearmanr(d[col], d.fwd12_strat_ex).correlation:+.3f} (t={nw_t_rank(d[col], d.fwd12_strat_ex, 12):+.2f}); → IC-12 {stats.spearmanr(d[col], d.fwd12_ic).correlation:+.3f}")

# ------------------------------------------------------------ 2. симуляция
P("\n[2] симуляция: сигнал AR(1) ρ=0,81; избыток рынка над деньгами = μ + β·s_{t-1} + ε (t-Стьюдент df=4, σ=5,5 %/мес); "
  "позиция = знак с гистерезисом 0,1 (без ворот); IC калибруется на 0,20; слом: β=0 с месяца T")
rng = np.random.default_rng(2026)
RHO, SIG_R, MU = 0.81, 0.055, 0.002
NREP = 500
BURN = 180          # разгон для скользящих окон (15 лет)
OBS = 60            # окно наблюдения ложных тревог (5 лет)
POST = 120          # наблюдение после слома (10 лет)


def simulate(beta, n, rng, break_at=None):
    s = np.empty(n)
    s[0] = rng.normal(0, 1)
    e = rng.normal(0, np.sqrt(1 - RHO ** 2), n)
    for t in range(1, n):
        s[t] = RHO * s[t - 1] + e[t]
    eps = rng.standard_t(4, n) * SIG_R / np.sqrt(2.0)   # var t4 = 2 → масштаб до SIG_R
    b = np.full(n, beta)
    if break_at is not None:
        b[break_at:] = 0.0
    ex = MU + b * np.r_[0, s[:-1]] + eps          # доходность месяца t зависит от s_{t-1}
    sg = hysteresis_sign(pd.Series(s), 0.1).values
    pos = (sg > 0).astype(float)
    pos_prev = np.r_[0.0, pos[:-1]]
    sig_prev = np.r_[np.nan, s[:-1]]
    return sig_prev, pos_prev, ex


# калибровка β на IC 0,20
def realized_ic(beta, reps=200):
    v = []
    for k in range(reps):
        sp, pp, ex = simulate(beta, 300, rng)
        v.append(stats.spearmanr(sp[1:], ex[1:]).correlation)
    return np.mean(v)


betas = np.linspace(0.004, 0.02, 9)
ics = [realized_ic(b, 60) for b in betas]
BETA = float(np.interp(0.20, ics, betas))
P(f"  калибровка: β={BETA:.4f} даёт IC≈{realized_ic(BETA, 200):.3f}; среднее умение при IC 0,2 ≈ "
  f"{np.mean([np.nanmean(skill_series(*simulate(BETA, 300, rng)[1:])) for _ in range(100)])*100:+.3f} %/мес")


def criteria_from(sig_prev, pos_prev, ex):
    ic24 = rolling_ic(sig_prev, ex, 24)
    ic36 = rolling_ic(sig_prev, ex, 36)
    sk = skill_series(pos_prev, ex)
    cs = page_cusum(sk)
    cs_k0 = page_cusum(sk, k_frac=0.0)
    ex24 = cum_excess(pos_prev * ex, 24)
    ex12 = cum_excess(pos_prev * ex, 12)
    sh24 = share_correct(pos_prev, ex, 24)
    sk24 = pd.Series(sk).rolling(24, min_periods=24).mean().values
    sk36 = pd.Series(sk).rolling(36, min_periods=36).mean().values
    out = {
        "A1: IC-24<0": streak_below(ic24, 1),
        "A3: IC-24<0 ×3": streak_below(ic24, 3),
        "A6: IC-24<0 ×6": streak_below(ic24, 6),
        "A12: IC-24<0 ×12": streak_below(ic24, 12),
        "A36: IC-36<0": streak_below(ic36, 1),
        "A36×6: IC-36<0 ×6": streak_below(ic36, 6),
        "B24: умение24<0": sk24 < 0,
        "B36: умение36<0": sk36 < 0,
        "C: избыток24<−10%": ex24 < -0.10,
        "C: избыток24<−20%": ex24 < -0.20,
        "C: доля верных24<40%": sh24 < 0.40,
        "C: F2 (изб<−10% | доля<40%)": (ex24 < -0.10) | (sh24 < 0.40),
        "D: избыток12<0": ex12 < 0,
    }
    for h in [3, 4, 5, 6, 8, 10]:
        out[f"B: CUSUM(k=½μ0)>{h}σ"] = cs > h
    for h in [5, 8, 10, 15]:
        out[f"B0: CUSUM(k=0)>{h}σ"] = cs_k0 > h
    return out


names = None
fa = {}
det = {}
for rep in range(NREP):
    # (i) без слома: ложные тревоги в окне [BURN, BURN+OBS)
    sp, pp, ex = simulate(BETA, BURN + OBS, rng)
    cr = criteria_from(sp, pp, ex)
    if names is None:
        names = list(cr.keys())
        fa = {k: [] for k in names}
        det = {k: [] for k in names}
    for k in names:
        fa[k].append(bool(cr[k][BURN:BURN + OBS].any()))
    # (ii) слом в месяце BURN
    sp, pp, ex = simulate(BETA, BURN + POST, rng, break_at=BURN)
    cr = criteria_from(sp, pp, ex)
    for k in names:
        a = cr[k][BURN:BURN + POST]
        # тревога уже горит на момент слома? тогда первый фронт после слома
        fr = np.where(a)[0]
        det[k].append(int(fr[0]) + 1 if len(fr) else np.nan)
rows = []
for k in names:
    d = np.array(det[k], float)
    rows.append(dict(criterion=k, false_alarm_5y=np.mean(fa[k]),
                     p_detect_12=np.nanmean(d <= 12), p_detect_24=np.nanmean(d <= 24), p_detect_36=np.nanmean(d <= 36), p_detect_60=np.nanmean(d <= 60),
                     p_detect_120=np.mean(np.isfinite(d)), median_delay=np.nanmedian(d), mean_delay_detected=np.nanmean(d)))
sim = pd.DataFrame(rows)
sim.to_csv(f"{RES}/S2_health_sim.csv", index=False, float_format="%.4f")
P(f"\n[2] результаты ({NREP} повторов): ложная тревога за 5 лет при истинном IC 0,2; P(обнаружить слом за 12/24/36/60/120 мес); медиана задержки")
P(sim.round(3).to_string())

# критерии при одинаковой ложной тревоге ~10 %: интерполяция по семействам
P("\n[2] сравнение при одинаковой доле ложных тревог: для каждого семейства — вариант с FA ближе всего к 10 % и его медианная задержка")
for famname, pref in [("A (IC-24 подряд)", "A"), ("B (CUSUM k=½μ0)", "B: CUSUM"), ("B0 (CUSUM k=0)", "B0"), ("B24/36 (умение)", "B2"), ("C (F2)", "C"), ("D", "D")]:
    sub = sim[sim.criterion.str.startswith(pref)].copy()
    if len(sub) == 0:
        continue
    sub["dist"] = (sub.false_alarm_5y - 0.10).abs()
    b = sub.sort_values("dist").iloc[0]
    P(f"  {famname}: {b.criterion} — FA {b.false_alarm_5y:.2f}, P(24) {b.p_detect_24:.2f}, P(60) {b.p_detect_60:.2f}, медиана задержки {b.median_delay:.0f} мес")

# ------------------------------------------------------------ 3. слом не в ноль, а в отрицательное умение / шок рынка при живой модели
P("\n[3] дополнительно: (a) слом IC 0,2 → −0,1 (модель вредит); (b) без слома, но обвал −30 % за 3 месяца в лонге (ложная тревога по результату)")
rows = []
for scen in ["to_neg", "crash"]:
    fa2 = {k: [] for k in names}
    det2 = {k: [] for k in names}
    for rep in range(300):
        if scen == "to_neg":
            sp, pp, ex = simulate(BETA, BURN + POST, rng)
            # после слома: β отрицательный (IC ≈ −0,1)
            s_full = None
            # проще: пересобрать с β по кускам
            s = np.empty(BURN + POST); s[0] = rng.normal(); e = rng.normal(0, np.sqrt(1 - RHO ** 2), BURN + POST)
            for t in range(1, len(s)):
                s[t] = RHO * s[t - 1] + e[t]
            eps = rng.standard_t(4, len(s)) * SIG_R / np.sqrt(2.0)
            b = np.full(len(s), BETA); b[BURN:] = -BETA / 2
            ex = MU + b * np.r_[0, s[:-1]] + eps
            sg = hysteresis_sign(pd.Series(s), 0.1).values
            pos = (sg > 0).astype(float)
            pp = np.r_[0.0, pos[:-1]]; sp = np.r_[np.nan, s[:-1]]
            cr = criteria_from(sp, pp, ex)
            for k in names:
                a = cr[k][BURN:BURN + POST]; fr = np.where(a)[0]
                det2[k].append(int(fr[0]) + 1 if len(fr) else np.nan)
        else:
            sp, pp, ex = simulate(BETA, BURN + OBS, rng)
            # обвал: три месяца по −11 % в лонге, начиная с BURN+12
            t0 = BURN + 12
            pp[t0:t0 + 3] = 1.0
            ex[t0:t0 + 3] = -0.11
            cr = criteria_from(sp, pp, ex)
            for k in names:
                fa2[k].append(bool(cr[k][BURN:BURN + OBS].any()))
    for k in names:
        if scen == "to_neg":
            d = np.array(det2[k], float)
            rows.append(dict(scenario="IC 0,2 → −0,1", criterion=k, p_detect_24=np.nanmean(d <= 24), p_detect_60=np.nanmean(d <= 60), median_delay=np.nanmedian(d)))
        else:
            rows.append(dict(scenario="обвал −30 % при живой модели", criterion=k, false_alarm_5y=np.mean(fa2[k])))
extra = pd.DataFrame(rows)
extra.to_csv(f"{RES}/S2_health_sim_extra.csv", index=False, float_format="%.4f")
P(extra.round(3).to_string())
log.close()
