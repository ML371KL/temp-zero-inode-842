"""F2_pm: общая библиотека — загрузка данных, бэктест long/flat, метрики, эпизоды просадок.
Соглашение: сигнал на закрытии дня t -> позиция с закрытия t (доход дня t+1). pos[t] = sig[t-1].
Лонг = MCFTR (полная доходность, с 2003-02-26; до этого IMOEX-цена с оговоркой), флэт = mm_rate/252.
Издержки: cost за КАЖДУЮ смену позиции (0.2% по умолчанию; round-trip 0.4%).
"""
import os, sys
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
AUDIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(AUDIT, "data")
RES = os.path.join(AUDIT, "results")
TOXIC = "bear|stress|stress"
ENTRY = ("bull|stress|ok", "bear|stress|ok")

WINDOWS = {
    "2010-2026.08 (осн.)": ("2010-01-01", "2026-08-31"),
    "2004+ (полная)": ("2004-01-01", "2026-08-31"),
    "2010-2021": ("2010-01-01", "2021-12-31"),
    "2022.03-2024": ("2022-03-24", "2024-12-31"),
    "2025-2026.08": ("2025-01-01", "2026-08-31"),
    "ex-2022 (2010-26 без 2022)": ("2010-01-01", "2026-08-31"),
}


def hysteresis_sign(xs, thr=0.10):
    out, s = [], 0
    for v in xs:
        if v is not None and v == v:
            if v > thr:
                s = 1
            elif v < -thr:
                s = -1
        out.append(s if s else np.nan)
    return np.array(out, dtype=float)


def load_daily():
    d = pd.read_csv(os.path.join(DATA, "panel_prod_daily.csv"), parse_dates=["date"]).set_index("date")
    c = pd.read_csv(os.path.join(DATA, "cash_and_tr.csv"), parse_dates=["date"]).set_index("date")
    d = d.join(c[["mm_rate", "mcftr_ffill"]])
    r_tr = np.log(d["mcftr_ffill"]).diff()
    r_px = np.log(d["imoex"]).diff()
    d["r_long"] = r_tr.where(r_tr.notna(), r_px)
    d["r_px"] = r_px
    d["r_flat"] = d["mm_rate"].ffill() / 100.0 / 252.0
    d["gate_open"] = (d["cell"] != TOXIC) & d["cell"].notna()
    return d


def load_monthly():
    m = pd.read_csv(os.path.join(DATA, "panel_prod_monthly.csv"), parse_dates=["date"]).set_index("date")
    # ЗАКРЫТЫЕ месяцы: форвард месяца известен только после закрытия следующего.
    # Последняя строка (2026-09-01) — незакрытый месяц; её и предыдущую пару из fwd выбрасываем.
    m["closed"] = False
    m.loc[m.index[:-2], "closed"] = True
    m["core_sign"] = hysteresis_sign(m["composite"].values, 0.10)
    return m


def core_sign_daily(d, m):
    """Знак композита ПОСЛЕДНЕГО ЗАКРЫТОГО месяца, протянутый на дни
    (известен на закрытии последнего торгового дня месяца)."""
    s = pd.Series(np.nan, index=d.index)
    common = m.index.intersection(d.index)
    s.loc[common] = m.loc[common, "core_sign"].values
    return s.ffill()


def backtest(d, sig, cost=0.002, start=None, end=None):
    """sig: Series (0/1 или доля 0..1) на дату t = решение на закрытии t. pos = sig.shift(1)."""
    pos = sig.astype(float).reindex(d.index).ffill().fillna(0.0).shift(1).fillna(0.0)
    r = pos * d["r_long"].fillna(0.0) + (1 - pos) * d["r_flat"].fillna(0.0)
    turn = pos.diff().abs().fillna(0.0)
    r = r + np.log1p(-cost * turn)
    out = pd.DataFrame({"r": r, "pos": pos, "turn": turn})
    if start:
        out = out[out.index >= start]
    if end:
        out = out[out.index <= end]
    return out


def bench(d, start=None, end=None):
    bh = pd.DataFrame({"r": d["r_long"].fillna(0.0), "pos": 1.0, "turn": 0.0}, index=d.index)
    mm = pd.DataFrame({"r": d["r_flat"].fillna(0.0), "pos": 0.0, "turn": 0.0}, index=d.index)
    if start:
        bh, mm = bh[bh.index >= start], mm[mm.index >= start]
    if end:
        bh, mm = bh[bh.index <= end], mm[mm.index <= end]
    return bh, mm


def metrics(bt, d, bh=None, ex2022=False):
    x = bt.copy()
    if ex2022:
        x = x[(x.index < "2022-01-01") | (x.index > "2022-12-31")]
    r = x["r"]
    n = len(r)
    yrs = n / 252.0
    if n < 30:
        return {}
    eq = np.exp(r.cumsum())
    dd = eq / eq.cummax() - 1
    rf = d["r_flat"].reindex(x.index).fillna(0.0)
    ex = r - rf
    mo = r.resample("ME").sum()
    mo_rf = rf.resample("ME").sum()
    out = {
        "CAGR%": round((np.exp(r.sum() / yrs) - 1) * 100, 2),
        "vol%": round(r.std() * np.sqrt(252) * 100, 2),
        "Sharpe": round(r.mean() / r.std() * np.sqrt(252), 3) if r.std() > 0 else np.nan,
        "Sharpe_ex_mm": round(ex.mean() / ex.std() * np.sqrt(252), 3) if ex.std() > 0 else np.nan,
        "MDD%": round(dd.min() * 100, 2),
        "in_mkt%": round(x["pos"].mean() * 100, 1),
        "trades/yr": round(x["turn"].sum() / yrs, 2),
        "hit_m_vs_mm%": round(((mo - mo_rf) > 0).mean() * 100, 1),
        "hit_m_pos%": round((mo > 0).mean() * 100, 1),
        "n_months": int(len(mo)),
    }
    if bh is not None:
        b = bh["r"].reindex(x.index).fillna(0.0)
        yr_s = r.groupby(r.index.year).sum()
        yr_b = b.groupby(b.index.year).sum()
        full = [y for y in yr_s.index if (r.index.year == y).sum() >= 120]
        out["beat_bh_years%"] = round((yr_s[full] > yr_b[full]).mean() * 100, 1) if full else np.nan
        out["n_years"] = int(len(full))
        out["CAGR_bh%"] = round((np.exp(b.sum() / yrs) - 1) * 100, 2)
    return out


def sharpe_diff_bootstrap(r1, r2, block=126, nboot=1000, seed=0):
    """Бутстреп разности Шарпов (дневные ряды, стационарный блочный бутстреп, средний блок ~6 мес).
    Возвращает (набл. разность, 2.5%, 97.5%, доля бутстрепов с разностью <= 0)."""
    rng = np.random.default_rng(seed)
    a = np.asarray(r1, dtype=float)
    b = np.asarray(r2, dtype=float)
    n = len(a)

    def sh(x):
        s = x.std()
        return x.mean() / s * np.sqrt(252) if s > 0 else 0.0

    obs = sh(a) - sh(b)
    diffs = np.empty(nboot)
    for k in range(nboot):
        idx = np.empty(n, dtype=int)
        i = 0
        while i < n:
            start = rng.integers(0, n)
            L = int(rng.geometric(1.0 / block))
            L = min(L, n - i)
            idx[i:i + L] = (start + np.arange(L)) % n
            i += L
        diffs[k] = sh(a[idx]) - sh(b[idx])
    return obs, float(np.percentile(diffs, 2.5)), float(np.percentile(diffs, 97.5)), float((diffs <= 0).mean())


def drawdown_episodes(px, thr=0.15):
    """Эпизоды просадок индекса глубже thr: (peak, trough, recovery|None, depth)."""
    px = px.dropna()
    runmax = px.cummax()
    dd = px / runmax - 1
    eps = []
    under = (dd < 0).values
    i, n = 0, len(px)
    while i < n:
        if under[i]:
            j = i
            while j < n and under[j]:
                j += 1
            seg = dd.iloc[i:j]
            if seg.min() <= -thr:
                trough = seg.idxmin()
                peak = px.index[i - 1] if i > 0 else px.index[i]
                rec = px.index[j] if j < n else None
                eps.append((peak, trough, rec, float(seg.min())))
            i = j
        else:
            i += 1
    return eps


def timeliness(pos, px, eps, horizon_rec=252):
    """Для каждого эпизода: лаг выхода после пика (торг. дней), доля падения избегнута,
    лаг входа после дна, доля роста (до восстановления, но не дольше 252 дн) пропущена."""
    rows = []
    px = px.dropna()
    lr = np.log(px).diff()
    for peak, trough, rec, depth in eps:
        fall = lr[(lr.index > peak) & (lr.index <= trough)]
        p_fall = pos.reindex(fall.index).fillna(0.0)
        avoided = 1 - (p_fall * fall).sum() / fall.sum() if fall.sum() != 0 else np.nan
        pos_at_peak = float(pos.reindex([peak]).fillna(0.0).iloc[0])
        if pos_at_peak > 0.5:
            z = p_fall[p_fall < 0.5]
            exit_lag = int(px.index.get_loc(z.index[0]) - px.index.get_loc(peak)) if len(z) else None
        else:
            exit_lag = 0
        it = px.index.get_loc(trough)
        end = rec if rec is not None else px.index[min(len(px) - 1, it + horizon_rec)]
        if rec is not None and px.index.get_loc(rec) - it > horizon_rec:
            end = px.index[it + horizon_rec]
        rise = lr[(lr.index > trough) & (lr.index <= end)]
        p_rise = pos.reindex(rise.index).fillna(0.0)
        missed = 1 - (p_rise * rise).sum() / rise.sum() if rise.sum() != 0 else np.nan
        pos_at_trough = float(pos.reindex([trough]).fillna(0.0).iloc[0])
        if pos_at_trough < 0.5:
            z = p_rise[p_rise > 0.5]
            entry_lag = int(px.index.get_loc(z.index[0]) - it) if len(z) else None
        else:
            entry_lag = 0
        rows.append({"peak": peak.date(), "trough": trough.date(), "depth%": round(depth * 100, 1),
                     "fall_days": int(len(fall)), "exit_lag_days": exit_lag,
                     "avoided%": round(avoided * 100) if avoided == avoided else np.nan,
                     "entry_lag_days": entry_lag, "rise_end": end.date(),
                     "rise%": round((np.exp(rise.sum()) - 1) * 100, 1),
                     "missed%": round(missed * 100) if missed == missed else np.nan})
    return pd.DataFrame(rows)


def run_windows(d, sig, label, cost=0.002, with_bench=True):
    rows = []
    for w, (a, b) in WINDOWS.items():
        ex = w.startswith("ex-2022")
        bt = backtest(d, sig, cost, a, b)
        bh, mm = bench(d, a, b)
        items = [("стратегия", bt)]
        if with_bench:
            items += [("b&h MCFTR", bh), ("деньги", mm)]
        for nm, x in items:
            mt = metrics(x, d, bh, ex2022=ex)
            mt.update({"rule": label if nm == "стратегия" else nm, "window": w})
            rows.append(mt)
    return pd.DataFrame(rows)
