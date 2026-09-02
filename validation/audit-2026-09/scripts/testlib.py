"""Statistical test library for indicator validation.

Conventions:
- signal: pd.Series indexed by date (already lagged to availability date!)
- fwd: forward log-return series indexed by same dates (fwd over next H trading days)
- regimes: dict name -> (start, end) inclusive
"""
import numpy as np
import pandas as pd
from scipy import stats

REGIMES = {
    "R1_pre2022": ("2015-01-01", "2022-02-18"),
    "R2_2022_24": ("2022-03-24", "2024-12-31"),
    "R3_2025_26": ("2025-01-01", "2026-08-11"),
}


def regime_mask(idx, regime):
    a, b = REGIMES[regime]
    return (idx >= a) & (idx <= b)


def nw_tstat(x, y, lag):
    """OLS slope t-stat with Newey-West HAC errors, standardized vars."""
    m = (~np.isnan(x)) & (~np.isnan(y))
    x, y = x[m], y[m]
    n = len(x)
    if n < 30 or np.nanstd(x) == 0:
        return np.nan, np.nan, n
    xs = (x - x.mean()) / x.std()
    ys = (y - y.mean()) / y.std()
    beta = (xs * ys).mean()
    resid = ys - beta * xs
    u = xs * resid
    L = min(lag, n // 3)
    s = (u ** 2).sum()
    for l in range(1, L + 1):
        w = 1 - l / (L + 1)
        s += 2 * w * (u[:-l] * u[l:]).sum()
    se = np.sqrt(s) / n * np.sqrt(n) / np.sqrt(n)  # var(beta) = S / n^2 * n... simplify below
    var_beta = s / n ** 2
    t = beta / np.sqrt(var_beta) if var_beta > 0 else np.nan
    return beta, t, n


def spearman_ic(x, y):
    m = (~np.isnan(x)) & (~np.isnan(y))
    if m.sum() < 30:
        return np.nan, np.nan, int(m.sum())
    r, p = stats.spearmanr(x[m], y[m])
    return r, p, int(m.sum())


def block_bootstrap_ic(x, y, horizon, n_boot=2000, seed=42):
    """Moving-block bootstrap p-value for spearman IC under H0 (circularly shifting signal)."""
    m = (~np.isnan(x)) & (~np.isnan(y))
    x, y = x[m], y[m]
    n = len(x)
    if n < 60:
        return np.nan
    obs, _ = stats.spearmanr(x, y)
    rng = np.random.default_rng(seed)
    block = max(horizon * 2, 10)
    if n < 3 * block:
        block = max(5, n // 6)
    null = np.empty(n_boot)
    for b in range(n_boot):
        shift = rng.integers(block, n - block)
        null[b] = stats.spearmanr(np.roll(x, shift), y)[0]
    p = (np.abs(null) >= abs(obs)).mean()
    return p


def tercile_spread(sig, fwd):
    m = sig.notna() & fwd.notna()
    s, f = sig[m], fwd[m]
    if len(s) < 60:
        return np.nan, np.nan, np.nan
    q1, q2 = s.quantile([1 / 3, 2 / 3])
    lo, hi = f[s <= q1], f[s >= q2]
    return hi.mean(), lo.mean(), hi.mean() - lo.mean()


def run_ic_battery(sig, fwd_dict, name, out):
    """IC across horizons and regimes -> append dict rows to out list."""
    for hname, fwd in fwd_dict.items():
        H = int(hname.rstrip("d"))
        df = pd.concat([sig.rename("s"), fwd.rename("f")], axis=1)
        for reg in REGIMES:
            mask = regime_mask(df.index, reg)
            sub = df[mask]
            if sub.s.notna().sum() < 40:
                out.append(dict(signal=name, horizon=hname, regime=reg, n=int(sub.s.notna().sum()),
                                ic=np.nan, p_boot=np.nan, nw_t=np.nan, terc_spread=np.nan))
                continue
            ic, p_sp, n = spearman_ic(sub.s.values, sub.f.values)
            p_boot = block_bootstrap_ic(sub.s.values, sub.f.values, H)
            _, nw_t, _ = nw_tstat(sub.s.values, sub.f.values, H)
            hi, lo, spread = tercile_spread(sub.s, sub.f)
            out.append(dict(signal=name, horizon=hname, regime=reg, n=n, ic=round(ic, 4) if ic == ic else np.nan,
                            p_boot=round(p_boot, 4) if p_boot == p_boot else np.nan,
                            nw_t=round(nw_t, 2) if nw_t == nw_t else np.nan,
                            terc_spread=round(spread * 100, 2) if spread == spread else np.nan))


def event_study(event_dates, ret, windows=((0, 0), (0, 5), (0, 20), (-5, -1)), n_placebo=5000, seed=7):
    """CARs around event dates vs placebo (random dates from same index).
    ret: daily log-return series. Returns dict window -> (car_mean_%, p_placebo, n_events)."""
    ret = ret.dropna()
    idx = ret.index
    pos = {d: i for i, d in enumerate(idx)}
    evs = []
    for d in event_dates:
        d = pd.Timestamp(d)
        # next trading day at or after event date
        later = idx[idx >= d]
        if len(later) == 0:
            continue
        evs.append(pos[later[0]])
    evs = [e for e in evs if 10 < e < len(idx) - 25]
    if len(evs) < 3:
        return None
    rng = np.random.default_rng(seed)
    res = {}
    ra = ret.values
    for (a, b) in windows:
        cars = np.array([ra[e + a:e + b + 1].sum() for e in evs])
        obs = cars.mean()
        placebo = np.empty(n_placebo)
        for p in range(n_placebo):
            rd = rng.integers(11, len(idx) - 26, size=len(evs))
            placebo[p] = np.mean([ra[e + a:e + b + 1].sum() for e in rd])
        pval = (np.abs(placebo - placebo.mean()) >= abs(obs - placebo.mean())).mean()
        res[f"[{a},{b}]"] = (round(obs * 100, 2), round(pval, 4), len(evs))
    return res


def fdr_bh(pvals, alpha=0.10):
    """Benjamini-Hochberg: return boolean mask of discoveries and q-values."""
    p = np.asarray(pvals, dtype=float)
    n = np.isfinite(p).sum()
    order = np.argsort(np.where(np.isfinite(p), p, 2))
    q = np.full_like(p, np.nan)
    prev = 1.0
    ranked = [(i, p[i]) for i in order if np.isfinite(p[i])]
    for rank in range(len(ranked), 0, -1):
        i, pv = ranked[rank - 1]
        val = min(prev, pv * n / rank)
        q[i] = val
        prev = val
    return q
