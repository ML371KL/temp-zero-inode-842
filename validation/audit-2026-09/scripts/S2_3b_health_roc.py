"""S2 — находка 3, дополнение: ROC-развёртка критериев здоровья при одинаковой доле ложных тревог.
Для каждого семейства — сетка порогов; по каждому порогу: FA за 5 лет при IC 0,2; P(обнаружить слом IC 0,2→0
за 24/60 мес); FA при обвале −30 % в лонге (живая модель). Затем интерполяция P(60) при FA = 0,25 и 0,40.
Запуск: python scripts/S2_3b_health_roc.py
"""
import sys
sys.path.insert(0, "scripts")
import numpy as np
import pandas as pd
from scipy import stats
from S2_lib import *  # noqa

log = open(f"{RES}/S2_3b_log.txt", "w", encoding="utf-8")


def P(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    log.write(s + "\n")


RHO, SIG_R, MU, BETA = 0.81, 0.055, 0.002, 0.0097
NREP = 400
BURN, OBS, POST = 180, 60, 120
rng = np.random.default_rng(77)


def spearman_fast(x, y):
    rx = stats.rankdata(x)
    ry = stats.rankdata(y)
    rx = rx - rx.mean()
    ry = ry - ry.mean()
    d = np.sqrt((rx ** 2).sum() * (ry ** 2).sum())
    return (rx * ry).sum() / d if d > 0 else np.nan


def rolling_ic(sig, ret, w):
    n = len(sig)
    out = np.full(n, np.nan)
    for t in range(w - 1, n):
        s = sig[t - w + 1:t + 1]   # sig[i] уже сдвинут: пара (sig[i], ret[i]) закрыта в конце месяца i
        r = ret[t - w + 1:t + 1]
        m = np.isfinite(s) & np.isfinite(r)
        if m.sum() >= w // 2 + 1:
            out[t] = spearman_fast(s[m], r[m])
    return out


def streak_below(x, k, thr=0.0):
    n = len(x)
    out = np.zeros(n, dtype=bool)
    c = 0
    for t in range(n):
        c = c + 1 if (np.isfinite(x[t]) and x[t] < thr) else 0
        out[t] = c >= k
    return out


def skill_series(pos_prev, ex, w=60):
    pbar = pd.Series(pos_prev).rolling(w, min_periods=24).mean().values
    return (pos_prev - pbar) * ex


def page_cusum(c, k_frac, mu0_w=120, min_n=60):
    s = pd.Series(c)
    mu0 = s.rolling(mu0_w, min_periods=min_n).mean().shift(1).values
    sig = s.rolling(mu0_w, min_periods=min_n).std().shift(1).values
    n = len(c)
    S = np.full(n, np.nan)
    cur = 0.0
    for t in range(n):
        if np.isfinite(c[t]) and np.isfinite(mu0[t]) and np.isfinite(sig[t]) and sig[t] > 0:
            cur = max(0.0, cur + (max(mu0[t], 0.0) * k_frac - c[t]) / sig[t])
            S[t] = cur
    return S


def simulate(beta_pre, beta_post, n, break_at, rng, crash_at=None):
    s = np.empty(n)
    s[0] = rng.normal()
    e = rng.normal(0, np.sqrt(1 - RHO ** 2), n)
    for t in range(1, n):
        s[t] = RHO * s[t - 1] + e[t]
    eps = rng.standard_t(4, n) * SIG_R / np.sqrt(2.0)
    b = np.full(n, beta_pre)
    if break_at is not None:
        b[break_at:] = beta_post
    ex = MU + b * np.r_[0, s[:-1]] + eps
    sg = hysteresis_sign(pd.Series(s), 0.1).values
    pos = (sg > 0).astype(float)
    pos_prev = np.r_[0.0, pos[:-1]]
    sig_prev = np.r_[np.nan, s[:-1]]
    if crash_at is not None:
        pos_prev[crash_at:crash_at + 3] = 1.0
        ex[crash_at:crash_at + 3] = -0.11
    return sig_prev, pos_prev, ex


FAM = {}  # name -> list of (label, function(stats)->bool array)


def build(sig_prev, pos_prev, ex):
    st = {}
    st["ic24"] = rolling_ic(sig_prev, ex, 24)
    st["ic36"] = rolling_ic(sig_prev, ex, 36)
    st["ic60"] = rolling_ic(sig_prev, ex, 60)
    sk = skill_series(pos_prev, ex)
    st["cusum_half"] = page_cusum(sk, 0.5)
    st["cusum_0"] = page_cusum(sk, 0.0)
    st["ex24"] = pd.Series(pos_prev * ex).rolling(24).sum().values
    st["ex36"] = pd.Series(pos_prev * ex).rolling(36).sum().values
    right = np.where(pos_prev > 0.5, ex > 0, ex <= 0).astype(float)
    st["share24"] = pd.Series(right).rolling(24).mean().values
    st["share36"] = pd.Series(right).rolling(36).mean().values
    sk_s = pd.Series(sk)
    st["skill_t36"] = (sk_s.rolling(36).mean() / sk_s.rolling(36).std() * np.sqrt(36)).values
    st["skill_t60"] = (sk_s.rolling(60).mean() / sk_s.rolling(60).std() * np.sqrt(60)).values
    return st


CRIT = []
for k in [1, 3, 6, 9, 12, 18, 24]:
    CRIT.append((f"IC-24<0 ×{k}", "A: IC-24 подряд", k, lambda st, k=k: streak_below(st["ic24"], k)))
for k in [1, 3, 6, 12, 18]:
    CRIT.append((f"IC-36<0 ×{k}", "A36: IC-36 подряд", k, lambda st, k=k: streak_below(st["ic36"], k)))
for k in [1, 3, 6, 12]:
    CRIT.append((f"IC-60<0 ×{k}", "A60: IC-60 подряд", k, lambda st, k=k: streak_below(st["ic60"], k)))
for thr in [-0.1, -0.2]:
    for k in [1, 6]:
        CRIT.append((f"IC-24<{thr} ×{k}", "A': IC-24 < −0,1/−0,2", k, lambda st, k=k, thr=thr: streak_below(st["ic24"], k, thr)))
for h in [4, 6, 8, 10, 12, 15, 20, 25]:
    CRIT.append((f"CUSUM(k=½μ0)>{h}σ", "B: CUSUM k=½μ0", h, lambda st, h=h: st["cusum_half"] > h))
for h in [4, 6, 8, 10, 12, 15, 20, 25]:
    CRIT.append((f"CUSUM(k=0)>{h}σ", "B0: CUSUM k=0", h, lambda st, h=h: st["cusum_0"] > h))
for z in [-0.5, -1.0, -1.5, -2.0]:
    CRIT.append((f"t(умение 36м)<{z}", "B36: t умения 36", z, lambda st, z=z: st["skill_t36"] < z))
    CRIT.append((f"t(умение 60м)<{z}", "B60: t умения 60", z, lambda st, z=z: st["skill_t60"] < z))
for x in [0.0, -0.05, -0.10, -0.15, -0.20, -0.30]:
    CRIT.append((f"избыток24<{x:.0%}", "C: избыток над деньгами 24м", x, lambda st, x=x: st["ex24"] < x))
    CRIT.append((f"избыток36<{x:.0%}", "C36: избыток 36м", x, lambda st, x=x: st["ex36"] < x))
for q in [0.50, 0.46, 0.42, 0.40, 0.375, 0.35, 0.30]:
    CRIT.append((f"доля верных24<{q:.3f}", "C': доля верных 24м", q, lambda st, q=q: st["share24"] < q))
    CRIT.append((f"доля верных36<{q:.3f}", "C'36: доля верных 36м", q, lambda st, q=q: st["share36"] < q))
for x in [-0.10, -0.20]:
    CRIT.append((f"F2: избыток24<{x:.0%} | доля<40%", "C: F2 комбинированный", x, lambda st, x=x: (st["ex24"] < x) | (st["share24"] < 0.40)))
for q in [0.42, 0.40, 0.375]:
    CRIT.append((f"доля24<{q} И IC-24<0", "E: доля И IC", q, lambda st, q=q: (st["share24"] < q) & (st["ic24"] < 0)))
    CRIT.append((f"доля24<{q} И t36<−0.5", "E': доля И умение", q, lambda st, q=q: (st["share24"] < q) & (st["skill_t36"] < -0.5)))

names = [c[0] for c in CRIT]
fa = {n: [] for n in names}
fa_crash = {n: [] for n in names}
det = {n: [] for n in names}
det_neg = {n: [] for n in names}
for rep in range(NREP):
    st = build(*simulate(BETA, BETA, BURN + OBS, None, rng))
    for n_, _, _, f in CRIT:
        fa[n_].append(bool(f(st)[BURN:BURN + OBS].any()))
    st = build(*simulate(BETA, BETA, BURN + OBS, None, rng, crash_at=BURN + 12))
    for n_, _, _, f in CRIT:
        fa_crash[n_].append(bool(f(st)[BURN:BURN + OBS].any()))
    st = build(*simulate(BETA, 0.0, BURN + POST, BURN, rng))
    for n_, _, _, f in CRIT:
        a = f(st)[BURN:BURN + POST]
        fr = np.where(a)[0]
        det[n_].append(int(fr[0]) + 1 if len(fr) else np.nan)
    st = build(*simulate(BETA, -BETA / 2, BURN + POST, BURN, rng))
    for n_, _, _, f in CRIT:
        a = f(st)[BURN:BURN + POST]
        fr = np.where(a)[0]
        det_neg[n_].append(int(fr[0]) + 1 if len(fr) else np.nan)
rows = []
for n_, famname, par, _ in CRIT:
    d = np.array(det[n_], float)
    dn = np.array(det_neg[n_], float)
    rows.append(dict(family=famname, criterion=n_, param=par, FA_5y=np.mean(fa[n_]), FA_crash_5y=np.mean(fa_crash[n_]),
                     P_det_24=np.nanmean(d <= 24), P_det_60=np.nanmean(d <= 60), P_det_120=np.mean(np.isfinite(d)), med_delay=np.nanmedian(d),
                     P_detneg_24=np.nanmean(dn <= 24), P_detneg_60=np.nanmean(dn <= 60), med_delay_neg=np.nanmedian(dn)))
roc = pd.DataFrame(rows)
roc.to_csv(f"{RES}/S2_health_roc.csv", index=False, float_format="%.4f")
P(f"[ROC] {NREP} повторов; FA_5y — ложная тревога за 5 лет при IC 0,2; FA_crash — то же при обвале −30 % за 3 мес в лонге; P_det — слом IC 0,2→0; P_detneg — слом 0,2→−0,1")
P(roc.round(3).to_string())

# интерполяция P_det_60 и FA_crash при FA_5y = 0,25 / 0,40 внутри семейства
P("\n[ROC] мощность при одинаковой ложной тревоге (линейная интерполяция по семейству):")
rows = []
for famname, g in roc.groupby("family", sort=False):
    g = g.sort_values("FA_5y")
    if len(g) < 2:
        continue
    row = dict(family=famname)
    for target in [0.15, 0.25, 0.40]:
        if g.FA_5y.min() <= target <= g.FA_5y.max():
            row[f"P60@FA{target}"] = np.interp(target, g.FA_5y, g.P_det_60)
            row[f"P24@FA{target}"] = np.interp(target, g.FA_5y, g.P_det_24)
            row[f"crash@FA{target}"] = np.interp(target, g.FA_5y, g.FA_crash_5y)
            row[f"Pneg60@FA{target}"] = np.interp(target, g.FA_5y, g.P_detneg_60)
        else:
            row[f"P60@FA{target}"] = np.nan
    rows.append(row)
iso = pd.DataFrame(rows)
iso.to_csv(f"{RES}/S2_health_roc_iso.csv", index=False, float_format="%.4f")
P(iso.round(2).to_string())
log.close()
