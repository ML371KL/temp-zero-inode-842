"""S_skeptic — независимая библиотека верификатора.

Ничего из scripts/D2_*, H_*, F2_*, D3_* не импортируется: только данные из data/.
Движок: позиция pos_t ∈ {0,1} принимается на ЗАКРЫТИИ дня t и применяется к доходности
t→t+1. Лонг = MCFTR (полная доходность), флэт = mm_rate/252 в день, издержки COST за
каждую смену позиции (списываются в день смены).
"""
import sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")

DATA = "data"
COST = 0.002
TOXIC = "bear|stress|stress"
WINDOWS = {
    "2010-2026": ("2010-01-01", "2026-08-31"),
    "2004-2026": ("2004-01-01", "2026-08-31"),
    "2015-2026": ("2015-01-01", "2026-08-31"),
    "2010-2021": ("2010-01-01", "2021-12-31"),
    "2022-03-2024": ("2022-03-01", "2024-12-31"),
    "2025-2026": ("2025-01-01", "2026-08-31"),
}


# ------------------------------------------------------------------ загрузка
def load_daily():
    d = pd.read_csv(f"{DATA}/panel_prod_daily.csv", parse_dates=["date"]).set_index("date")
    c = pd.read_csv(f"{DATA}/cash_and_tr.csv", parse_dates=["date"]).set_index("date")
    assert (d.index == c.index).all(), "календари panel_prod_daily и cash_and_tr расходятся"
    d["mcftr"] = c["mcftr_ffill"]
    d["mm_rate"] = c["mm_rate"]
    d["rusfar3m"] = c["rusfar3m"]
    d["toxic"] = (d["cell"] == TOXIC).astype(float)
    d.loc[d["cell"].isna(), "toxic"] = np.nan
    return d


def load_monthly():
    m = pd.read_csv(f"{DATA}/panel_prod_monthly.csv", parse_dates=["date"]).set_index("date")
    return m


def load_raw(series):
    r = pd.read_csv(f"{DATA}/raw_long.csv", parse_dates=["date"])
    r = r[r["series"] == series].set_index("date")["value"].sort_index()
    return r[~r.index.duplicated(keep="last")]


def month_end_mask(idx):
    """True на последнем торговом дне каждого календарного месяца."""
    key = idx.to_period("M")
    nxt = np.r_[key[1:], pd.Period("2999-12", "M")]
    return pd.Series(key != nxt, index=idx)


def week_end_mask(idx):
    """True на последнем торговом дне каждой ISO-недели."""
    key = idx.to_period("W")
    nxt = np.r_[key[1:], pd.Period("2999-12-31", "W")]
    return pd.Series(key != nxt, index=idx)


# ------------------------------------------------------------------ сигналы
def hysteresis_sign(x, thr=0.10):
    """Знак с гистерезисом (как calc.hysteresis_sign): +1/−1, 0 до первого пробоя."""
    out = np.zeros(len(x))
    s = 0
    v = x.values
    for i in range(len(v)):
        if np.isfinite(v[i]):
            if v[i] > thr:
                s = 1
            elif v[i] < -thr:
                s = -1
        out[i] = s
    return pd.Series(out, index=x.index)


def zscore_rolling(x, window=60, min_periods=24, clip=3.0):
    m = x.rolling(window, min_periods=min_periods).mean()
    s = x.rolling(window, min_periods=min_periods).std(ddof=1)
    z = (x - m) / s
    z = z.where(s > 0)
    return z.clip(-clip, clip)


def composite_monthly(legs, signs, window=60, min_periods=24, clip=3.0):
    """legs: DataFrame месячных срезов (последний торговый день); signs: dict id→±1."""
    zs = pd.DataFrame({c: signs[c] * zscore_rolling(legs[c], window, min_periods, clip)
                       for c in legs.columns})
    comp = zs.mean(axis=1, skipna=True)
    comp[zs.notna().sum(axis=1) == 0] = np.nan
    return comp, zs


def composite_live_daily(daily_legs, signs, window=60, min_periods=24, clip=3.0):
    """Дневной «живой» композит как в core.monthly_frame с незакрытым месяцем:
    окно = (window−1) ЗАКРЫТЫХ месячных срезов + сегодняшнее значение."""
    idx = daily_legs.index
    me = month_end_mask(idx)
    out = {}
    for c in daily_legs.columns:
        x = daily_legs[c]
        # месячные срезы: последнее непустое значение в месяце
        xm = x.groupby(idx.to_period("M")).last()
        # для каждого месяца: сумма и сумма квадратов предыдущих (window−1) закрытых срезов
        prev = xm.shift(1)
        cnt = prev.notna().rolling(window - 1, min_periods=1).sum()
        s1 = prev.fillna(0).rolling(window - 1, min_periods=1).sum()
        s2 = (prev.fillna(0) ** 2).rolling(window - 1, min_periods=1).sum()
        per = idx.to_period("M")
        cnt_d = cnt.reindex(per).values
        s1_d = s1.reindex(per).values
        s2_d = s2.reindex(per).values
        v = x.values
        ok = np.isfinite(v)
        n = cnt_d + ok
        S1 = s1_d + np.where(ok, v, 0)
        S2 = s2_d + np.where(ok, v ** 2, 0)
        with np.errstate(invalid="ignore", divide="ignore"):
            mean = S1 / n
            var = (S2 - n * mean ** 2) / (n - 1)
            sd = np.sqrt(np.maximum(var, 0))
            z = (v - mean) / sd
        z[~ok | (n < min_periods) | ~(sd > 0)] = np.nan
        out[c] = np.clip(z, -clip, clip) * signs[c]
    zs = pd.DataFrame(out, index=idx)
    comp = zs.mean(axis=1, skipna=True)
    comp[zs.notna().sum(axis=1) == 0] = np.nan
    return comp, zs


# ------------------------------------------------------------------ движок
def run_daily(pos, d, cost=COST, start=None, end=None):
    """pos: Series 0/1 по дням (решение на закрытии t). Возвращает DataFrame дневных
    доходностей стратегии, b&h и кэша на окне [start, end]."""
    pos = pos.reindex(d.index).astype(float)
    r = d["mcftr"].pct_change()
    cash = (d["mm_rate"].shift(1) / 100.0 / 252.0)
    p = pos.shift(1)
    strat = p * r + (1 - p) * cash
    chg = pos.diff().abs().fillna(0)
    strat = strat - cost * chg
    out = pd.DataFrame({"strat": strat, "bh": r, "cash": cash, "pos": p, "chg": chg})
    if start:
        out = out.loc[start:]
    if end:
        out = out.loc[:end]
    out = out.dropna(subset=["strat", "bh"])
    return out


def _mdd(eq):
    return float((eq / eq.cummax() - 1).min())


mdd = _mdd


def metrics(bt, name=""):
    """Метрики по правилу 3 брифа. Шарп — на месячных агрегатах ×√12."""
    if len(bt) < 30:
        return {"name": name}
    eq = (1 + bt["strat"]).cumprod()
    eq_bh = (1 + bt["bh"]).cumprod()
    m = (1 + bt[["strat", "bh", "cash"]]).groupby(bt.index.to_period("M")).prod() - 1
    years = len(bt) / 252.0
    cagr = eq.iloc[-1] ** (1 / years) - 1
    cagr_bh = eq_bh.iloc[-1] ** (1 / years) - 1
    vol = m["strat"].std() * np.sqrt(12)
    sh = m["strat"].mean() / m["strat"].std() * np.sqrt(12) if m["strat"].std() > 0 else np.nan
    ex = m["strat"] - m["cash"]
    sh_ex = ex.mean() / ex.std() * np.sqrt(12) if ex.std() > 0 else np.nan
    sh_bh = m["bh"].mean() / m["bh"].std() * np.sqrt(12)
    ym = (1 + bt[["strat", "bh"]]).groupby(bt.index.year).prod() - 1
    eq_m = (1 + m["strat"]).cumprod()
    return {
        "name": name, "cagr": cagr, "vol": vol, "sharpe": sh, "sharpe_ex": sh_ex,
        "mdd_daily": _mdd(eq), "mdd_monthly": _mdd(eq_m),
        "in_market": float(bt["pos"].mean()), "trades_py": float(bt["chg"].sum() / years),
        "years_beat_bh": float((ym["strat"] > ym["bh"]).mean()),
        "hit_months": float((m["strat"] > 0).mean()),
        "cagr_bh": cagr_bh, "sharpe_bh": sh_bh, "mdd_bh": _mdd(eq_bh),
        "cash_cagr": float((1 + m["cash"]).prod() ** (1 / years) - 1),
        "n_months": len(m),
    }


def monthly_returns(bt):
    return (1 + bt[["strat", "bh", "cash"]]).groupby(bt.index.to_period("M")).prod() - 1


def sharpe(x):
    x = np.asarray(x, float)
    s = x.std(ddof=1)
    return x.mean() / s * np.sqrt(12) if s > 0 else np.nan


def stationary_bootstrap_idx(n, block, rng):
    p = 1.0 / block
    idx = np.empty(n, dtype=int)
    i = rng.integers(n)
    for k in range(n):
        if k == 0 or rng.random() < p:
            i = rng.integers(n)
        else:
            i = (i + 1) % n
        idx[k] = i
    return idx


def sharpe_diff_boot(ra, rb, n_boot=2000, block=9, seed=7):
    """Стационарный бутстреп разности Шарпов ra − rb на месячных парах.
    Возвращает (Δ, p = P(Δ* ≤ 0), ДИ90)."""
    ra = np.asarray(ra, float)
    rb = np.asarray(rb, float)
    n = len(ra)
    rng = np.random.default_rng(seed)
    obs = sharpe(ra) - sharpe(rb)
    ds = np.empty(n_boot)
    for b in range(n_boot):
        ix = stationary_bootstrap_idx(n, block, rng)
        ds[b] = sharpe(ra[ix]) - sharpe(rb[ix])
    ds = ds[np.isfinite(ds)]
    p = float((ds <= 0).mean()) if len(ds) else np.nan
    lo, hi = np.percentile(ds, [5, 95]) if len(ds) else (np.nan, np.nan)
    return obs, p, lo, hi


def spearman_nw(sig, fwd, lag=1):
    """Спирмен + Ньюи-Уэст t на рангах (HAC, лаг = горизонт)."""
    df = pd.DataFrame({"s": sig, "f": fwd}).dropna()
    n = len(df)
    if n < 24:
        return np.nan, np.nan, n
    rs = df["s"].rank().values
    rf = df["f"].rank().values
    rs = (rs - rs.mean()) / rs.std(ddof=1)
    rf = (rf - rf.mean()) / rf.std(ddof=1)
    rho = float(np.mean(rs * rf) * n / (n - 1))
    u = rs * rf - rho
    var = np.sum(u ** 2)
    for L in range(1, lag + 1):
        w = 1 - L / (lag + 1)
        var += 2 * w * np.sum(u[L:] * u[:-L])
    se = np.sqrt(var) / n
    t = rho / se if se > 0 else np.nan
    return rho, t, n


def ic_boot(sig, fwd, n_boot=1000, block=12, seed=3):
    """Стационарный бутстреп сигнала (доходность фиксирована): p = P(IC* ≥ |IC|) двусторонний."""
    df = pd.DataFrame({"s": sig, "f": fwd}).dropna()
    n = len(df)
    if n < 24:
        return np.nan
    s = df["s"].values
    f = df["f"].values
    obs = pd.Series(s).corr(pd.Series(f), method="spearman")
    rng = np.random.default_rng(seed)
    cnt = 0
    for b in range(n_boot):
        ix = stationary_bootstrap_idx(n, block, rng)
        ic = pd.Series(s[ix]).corr(pd.Series(f), method="spearman")
        if abs(ic) >= abs(obs):
            cnt += 1
    return cnt / n_boot


def fmt_row(mt, keys=("cagr", "vol", "sharpe", "sharpe_ex", "mdd_monthly", "mdd_daily",
                      "in_market", "trades_py", "years_beat_bh", "hit_months")):
    pct = {"cagr", "vol", "mdd_monthly", "mdd_daily", "in_market", "years_beat_bh", "hit_months",
           "cagr_bh", "mdd_bh", "cash_cagr"}
    parts = []
    for k in keys:
        v = mt.get(k)
        if v is None or (isinstance(v, float) and not np.isfinite(v)):
            parts.append("—")
        elif k in pct:
            parts.append(f"{v * 100:.1f}%")
        else:
            parts.append(f"{v:.2f}")
    return parts


# ------------------------------------------------------------------ правило панели
def panel_decision_monthly(d, m, hyst=0.10, gate_col="toxic"):
    """Решение панели на последний торговый день месяца: ворота открыты И знак
    закрытого композита (гистерезис) > 0. Возвращает Series по датам-срезам."""
    comp = m["composite"]
    sgn = hysteresis_sign(comp, hyst)
    me = month_end_mask(d.index)
    tox = d.loc[me, gate_col]
    dec = ((sgn.reindex(tox.index) > 0) & (tox == 0)).astype(float)
    dec[tox.isna() | sgn.reindex(tox.index).isna()] = np.nan
    return dec


def decision_to_daily(dec, idx):
    """Решение на датах-срезах → дневная позиция (держится до следующего среза)."""
    return dec.reindex(idx).ffill()


def timeliness(pos, price, min_dd=0.15):
    """Правило 7: по каждой просадке price > min_dd — дни от пика до выхода, доля
    избегнутого падения, дни от дна до входа, доля пропущенного восстановления
    (до возврата на 50% пути к пику)."""
    eq = price.dropna()
    peak = eq.cummax()
    dd = eq / peak - 1
    eps = []
    in_dd = False
    for i in range(len(eq)):
        if not in_dd and dd.iloc[i] < -min_dd:
            # ищем пик
            pk = eq.index[eq.iloc[:i + 1].values.argmax()]
            in_dd = True
            trough_i = i
        elif in_dd:
            if dd.iloc[i] < dd.iloc[trough_i]:
                trough_i = i
            if dd.iloc[i] >= 0:
                eps.append((pk, eq.index[trough_i], eq.index[i]))
                in_dd = False
    if in_dd:
        eps.append((pk, eq.index[trough_i], None))
    rows = []
    p = pos.reindex(eq.index)
    for pk, tr, rec in eps:
        seg = eq.loc[pk:tr]
        depth = seg.iloc[-1] / seg.iloc[0] - 1
        pseg = p.loc[pk:tr]
        long_at_peak = bool(pseg.iloc[0] == 1)
        ex = pseg[pseg == 0]
        exit_day = None if not long_at_peak else (ex.index[0] if len(ex) else None)
        days_to_exit = None
        if long_at_peak:
            days_to_exit = int(seg.index.get_loc(exit_day)) if exit_day is not None else "не вышел"
        # доля избегнутого падения = 1 − (лог-падение, пережитое в лонге)/(лог-падение)
        lr = np.log(seg).diff().fillna(0)
        suffered = float((lr * pseg.shift(1).fillna(pseg.iloc[0])).sum())
        avoided = 1 - suffered / np.log(seg.iloc[-1] / seg.iloc[0]) if depth < 0 else np.nan
        # вход после дна: до первого лонга после tr
        after = p.loc[tr:]
        ent = after[after == 1]
        days_to_entry = int(after.index.get_loc(ent.index[0])) if len(ent) else "не вошёл"
        # пропущенная доля половины восстановления
        half = eq.loc[tr] * np.sqrt(eq.loc[pk] / eq.loc[tr])
        rec_seg = eq.loc[tr:]
        hit = rec_seg[rec_seg >= half]
        missed = np.nan
        if len(hit):
            hseg = eq.loc[tr:hit.index[0]]
            lr2 = np.log(hseg).diff().fillna(0)
            taken = float((lr2 * p.loc[tr:hit.index[0]].shift(1).fillna(0)).sum())
            missed = 1 - taken / np.log(hseg.iloc[-1] / hseg.iloc[0])
        rows.append(dict(peak=pk.date(), trough=tr.date(), depth=depth, long_at_peak=long_at_peak,
                         days_to_exit=days_to_exit, avoided=avoided, days_to_entry=days_to_entry,
                         missed_half_rec=missed))
    return pd.DataFrame(rows)
