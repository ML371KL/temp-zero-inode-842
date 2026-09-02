"""S2_core_skeptic — независимая библиотека верификации (не переиспользует C_core_lib / H_lib).

Определения (по брифу): решение на закрытии дня решения t -> позиция держится до следующего дня
решения; лонг = MCFTR (mcftr_ffill), флэт = mm_rate/252 в день; издержки 0,2 % за каждую смену
позиции (списываются в первый день новой позиции); метрики на календарных месяцах (Шарп x sqrt(12)),
просадка — и по дневной кривой, и по месячным точкам.
"""
import sys
import numpy as np
import pandas as pd
from scipy import stats

sys.stdout.reconfigure(encoding="utf-8")

DATA = "data"
RES = "results"
TOXIC = "bear|stress|stress"
COST = 0.002
MAIN = ("2010-01-01", "2026-08-31")
FULL = ("2004-01-01", "2026-08-31")
WINDOWS = {
    "MAIN 2010-26": MAIN,
    "FULL 2004-26": FULL,
    "A 2004-17": ("2004-01-01", "2017-12-31"),
    "B 2018-26": ("2018-01-01", "2026-08-31"),
    "2010-21": ("2010-01-01", "2022-02-18"),
    "2022-24": ("2022-03-24", "2024-12-31"),
    "2025-26": ("2025-01-01", "2026-08-31"),
}
LEGS_PROD = [("usd_mom63", +1), ("slope_10_2", +1), ("urals_rub_gap", -1)]


# ---------------------------------------------------------------- загрузка
def load_all():
    D = pd.read_csv(f"{DATA}/panel_prod_daily.csv", parse_dates=["date"]).set_index("date").sort_index()
    C = pd.read_csv(f"{DATA}/cash_and_tr.csv", parse_dates=["date"]).set_index("date").sort_index()
    M = pd.read_csv(f"{DATA}/panel_prod_monthly.csv", parse_dates=["date"]).set_index("date").sort_index()
    C = C.reindex(D.index).ffill()
    return D, C, M


def month_end_cuts(index):
    """Последний торговый день каждого месяца; незавершённый хвост (последняя дата — не конец
    месяца: день < 20 и месяц совпадает с месяцем последней даты) выбрасываем."""
    s = pd.Series(index, index=index)
    cuts = pd.DatetimeIndex(s.groupby(index.to_period("M")).last().values)
    last = index[-1]
    if cuts[-1] == last and last.day < 20:
        cuts = cuts[:-1]
    return cuts


def week_end_cuts(index):
    """Последний торговый день каждой ISO-недели (обычно пятница)."""
    s = pd.Series(index, index=index)
    iso = index.isocalendar()
    key = iso["year"].astype(str) + "-" + iso["week"].astype(str).str.zfill(2)
    cuts = pd.DatetimeIndex(s.groupby(key.values).last().sort_values().values)
    return cuts


# ---------------------------------------------------------------- z и композит
def zroll(x, w=60, mp=24, clip=3.0):
    """Скользящий z: окно включает текущую точку, ddof=1 (как pandas), обрезка."""
    m = x.rolling(w, min_periods=mp).mean()
    s = x.rolling(w, min_periods=mp).std(ddof=1)
    z = (x - m) / s.where(s > 0)
    return z.clip(-clip, clip) if clip is not None else z


def composite_mean(signed_z):
    """Среднее знаковых z по доступным ногам. signed_z: DataFrame (уже со знаками)."""
    return signed_z.mean(axis=1, skipna=True), signed_z.notna().sum(axis=1)


def live_daily_z(raw_daily, cuts, w=60, mp=24, clip=3.0):
    """Дневной z «как в проде для незавершённого месяца»: окно = (w-1) последних ЗАКРЫТЫХ месячных
    срезов до текущего месяца + значение дня t. На самом срезе совпадает с месячным z."""
    rm = raw_daily.reindex(cuts).values
    cut_month = np.array([c.year * 12 + c.month for c in cuts])
    day_month = raw_daily.index.year.values * 12 + raw_daily.index.month.values
    n_before = np.searchsorted(cut_month, day_month, side="left")  # число срезов с месяцем < месяца дня
    vals = raw_daily.values
    out = np.full(len(vals), np.nan)
    for i in range(len(vals)):
        v = vals[i]
        if not np.isfinite(v):
            continue
        j = n_before[i]
        hist = rm[max(0, j - (w - 1)):j]
        hist = hist[np.isfinite(hist)]
        arr = np.append(hist, v)
        if len(arr) < mp:
            continue
        sd = arr.std(ddof=1)
        if sd > 0:
            out[i] = np.clip((v - arr.mean()) / sd, -clip, clip)
    return pd.Series(out, index=raw_daily.index)


def hysteresis_sign(x, thr):
    """Знак с гистерезисом по определению calc.hysteresis_sign: переключение только при |x| > thr;
    NaN до первого уверенного пересечения."""
    v = x.values
    out = np.full(len(v), np.nan)
    s = 0
    for i in range(len(v)):
        if np.isfinite(v[i]):
            if v[i] > thr:
                s = 1
            elif v[i] < -thr:
                s = -1
        if s:
            out[i] = s
    return pd.Series(out, index=x.index)


def hysteresis_sign_confirm(x, thr, k):
    """Гистерезис с подтверждением: новый знак принимается, только если k подряд наблюдений |x|>thr
    с новым знаком."""
    v = x.values
    out = np.full(len(v), np.nan)
    s = 0
    streak = 0
    cand = 0
    for i in range(len(v)):
        if np.isfinite(v[i]):
            c = 1 if v[i] > thr else (-1 if v[i] < -thr else 0)
            if c != 0 and c != s:
                if c == cand:
                    streak += 1
                else:
                    cand, streak = c, 1
                if streak >= k:
                    s = c
                    streak, cand = 0, 0
            else:
                streak, cand = 0, 0
        if s:
            out[i] = s
    return pd.Series(out, index=x.index)


# ---------------------------------------------------------------- бэктест на дневной сетке
def daily_engine(pos_decisions, D, C, cost=COST, start=None, end=None):
    """pos_decisions: Series 0/1 на датах решений (закрытие дня). Позиция держится с закрытия дня
    решения до закрытия следующего дня решения. Возвращает дневной DataFrame: pos (в течение дня),
    r_tr, r_mm, ret, trade."""
    tr = C["mcftr_ffill"]
    r_tr = tr.pct_change()
    r_mm = C["mm_rate"] / 100.0 / 252.0
    pos_held = pos_decisions.reindex(D.index).ffill().shift(1)  # решение t действует с t+1
    df = pd.DataFrame({"pos": pos_held, "r_tr": r_tr, "r_mm": r_mm})
    if start is not None:
        # позиция в первый день окна — по последнему решению до окна
        df = df[df.index >= pd.Timestamp(start)]
    if end is not None:
        df = df[df.index <= pd.Timestamp(end)]
    df = df.dropna(subset=["r_tr", "r_mm"])
    df["pos"] = df["pos"].fillna(0.0).clip(0, 1)
    prev = df["pos"].shift(1)
    prev.iloc[0] = 0.0 if df["pos"].iloc[0] == 0 else df["pos"].iloc[0]  # вход в первый день окна не считаем сделкой
    df["trade"] = (df["pos"] != prev).astype(int)
    df["ret"] = df["pos"] * df["r_tr"] + (1 - df["pos"]) * df["r_mm"] - cost * df["trade"]
    return df


def to_monthly(df):
    g = df.groupby(df.index.to_period("M"))
    out = pd.DataFrame({
        "ret": g["ret"].apply(lambda x: (1 + x).prod() - 1),
        "r_tr": g["r_tr"].apply(lambda x: (1 + x).prod() - 1),
        "r_mm": g["r_mm"].apply(lambda x: (1 + x).prod() - 1),
        "pos": g["pos"].mean(),
        "trade": g["trade"].sum(),
    })
    out.index = out.index.to_timestamp(how="end").normalize()
    return out


def sharpe(r, ppy=12):
    r = pd.Series(r).dropna()
    return float(r.mean() / r.std(ddof=1) * np.sqrt(ppy)) if len(r) > 2 and r.std(ddof=1) > 0 else np.nan


def maxdd(r):
    c = (1 + pd.Series(r).fillna(0)).cumprod()
    return float((c / c.cummax() - 1).min())


def metrics(df_daily):
    """Метрики по дневному DataFrame (после daily_engine): месячные агрегаты + дневная просадка."""
    m = to_monthly(df_daily)
    n = len(m)
    yrs = n / 12.0
    r = m["ret"]
    cum = (1 + r).prod()
    ex = r - m["r_mm"]
    right = np.where(m["pos"] > 0.5, m["r_tr"] > m["r_mm"], m["r_tr"] <= m["r_mm"])
    yr = pd.DataFrame({"s": r, "b": m["r_tr"]}).groupby(m.index.year).apply(
        lambda g: (1 + g["s"]).prod() > (1 + g["b"]).prod())
    return dict(
        n_months=n,
        cagr=cum ** (1 / yrs) - 1,
        vol=r.std(ddof=1) * np.sqrt(12),
        sharpe=sharpe(r),
        sharpe_ex=sharpe(ex),
        mdd_daily=maxdd(df_daily["ret"]),
        mdd_monthly=maxdd(r),
        time_in=float(df_daily["pos"].mean()),
        trades_yr=df_daily["trade"].sum() / yrs,
        beat_years=float(yr.mean()),
        hit=float(np.mean(right)),
        bh_cagr=(1 + m["r_tr"]).prod() ** (1 / yrs) - 1,
        bh_sharpe=sharpe(m["r_tr"]),
        bh_mdd_daily=maxdd(df_daily["r_tr"]),
        mm_cagr=(1 + m["r_mm"]).prod() ** (1 / yrs) - 1,
    )


def run(pos_decisions, D, C, window=MAIN, cost=COST):
    df = daily_engine(pos_decisions, D, C, cost=cost, start=window[0], end=window[1])
    return metrics(df), df


# ---------------------------------------------------------------- статистика
def stationary_bootstrap_indices(n, mean_block, rng):
    p = 1.0 / mean_block
    idx = np.empty(n, dtype=int)
    idx[0] = rng.integers(n)
    for i in range(1, n):
        idx[i] = rng.integers(n) if rng.random() < p else (idx[i - 1] + 1) % n
    return idx


def boot_sharpe_diff(r_a, r_b, block=8, reps=2000, seed=0):
    """Бутстреп разности Шарпов (стационарный, блок в месяцах). p — двусторонний под H0: diff=0
    (центрированный бутстреп)."""
    rng = np.random.default_rng(seed)
    a = np.asarray(r_a, float)
    b = np.asarray(r_b, float)
    n = len(a)
    obs = sharpe(a) - sharpe(b)
    diffs = np.empty(reps)
    for k in range(reps):
        ix = stationary_bootstrap_indices(n, block, rng)
        diffs[k] = sharpe(a[ix]) - sharpe(b[ix])
    centered = diffs - diffs.mean()
    p = float(np.mean(np.abs(centered) >= abs(obs)))
    lo, hi = np.percentile(diffs, [5, 95])
    return obs, p, lo, hi


def spearman_ic(x, y):
    d = pd.DataFrame({"x": x, "y": y}).dropna()
    if len(d) < 6:
        return np.nan, len(d)
    return float(stats.spearmanr(d["x"], d["y"]).correlation), len(d)


def nw_t_rank(x, y, lag=1):
    """t Ньюи-Уэста для ранговой регрессии y_rank ~ x_rank."""
    import statsmodels.api as sm
    d = pd.DataFrame({"x": x, "y": y}).dropna()
    if len(d) < 8:
        return np.nan
    rx = d["x"].rank().values
    ry = d["y"].rank().values
    rx = (rx - rx.mean()) / rx.std()
    ry = (ry - ry.mean()) / ry.std()
    res = sm.OLS(ry, sm.add_constant(rx)).fit(cov_type="HAC", cov_kwds={"maxlags": lag})
    return float(res.tvalues[1])


def boot_ic_p(x, y, block=6, reps=1000, seed=0):
    """p-значение IC под H0 (стационарный бутстреп пар, центрированный)."""
    d = pd.DataFrame({"x": x, "y": y}).dropna()
    n = len(d)
    if n < 10:
        return np.nan
    rng = np.random.default_rng(seed)
    obs = stats.spearmanr(d["x"], d["y"]).correlation
    xs, ys = d["x"].values, d["y"].values
    vals = np.empty(reps)
    for k in range(reps):
        ix = stationary_bootstrap_indices(n, block, rng)
        vals[k] = stats.spearmanr(xs[ix], ys[ix]).correlation
    c = vals - vals.mean()
    return float(np.mean(np.abs(c) >= abs(obs)))


# ---------------------------------------------------------------- своевременность
def drawdown_episodes(tr, min_dd=0.15):
    """Эпизоды просадок MCFTR > min_dd: (пик, дно, глубина, дата восстановления/None)."""
    tr = tr.dropna()
    peak_val = tr.iloc[0]
    peak_dt = tr.index[0]
    episodes = []
    in_dd = False
    trough_val, trough_dt = peak_val, peak_dt
    for dt, v in tr.items():
        if v >= peak_val:
            if in_dd and (peak_val - trough_val) / peak_val >= min_dd:
                episodes.append(dict(peak=peak_dt, trough=trough_dt, depth=trough_val / peak_val - 1, recovered=dt))
            peak_val, peak_dt = v, dt
            trough_val, trough_dt = v, dt
            in_dd = False
        else:
            in_dd = True
            if v < trough_val:
                trough_val, trough_dt = v, dt
    if in_dd and (peak_val - trough_val) / peak_val >= min_dd:
        episodes.append(dict(peak=peak_dt, trough=trough_dt, depth=trough_val / peak_val - 1, recovered=None))
    return episodes


def timeliness(df_daily, tr, episodes):
    """Для каждого эпизода: дней (торговых) от пика до ухода во флэт (отрицательно = ушли раньше),
    доля избежанного падения, дней от дна до входа в лонг, доля пропущенного восстановления
    (до следующего пика или до конца данных)."""
    pos = df_daily["pos"]
    idx = pos.index
    rows = []
    for e in episodes:
        pk, tr_dt = e["peak"], e["trough"]
        if pk < idx[0] or tr_dt > idx[-1]:
            continue
        # выход во флэт: если на пике уже флэт — дата последнего лонга до пика (отрицательно);
        # если на пике лонг — первый флэт после пика (до дна); если лонг весь путь — NaN
        at_peak = pos.loc[pk] if pk in pos.index else np.nan
        seg = pos[(pos.index >= pk) & (pos.index <= tr_dt)]
        if at_peak < 0.5:
            before = pos[(pos.index < pk) & (pos.index >= pk - pd.Timedelta(days=400))]
            lastlong = before[before > 0.5]
            exit_day = lastlong.index[-1] if len(lastlong) else None
            d_exit = -(idx.get_loc(pk) - idx.get_loc(exit_day) - 1) if exit_day is not None else -400
        else:
            flats = seg[seg < 0.5]
            if len(flats) == 0:
                d_exit = np.nan
                exit_day = None
            else:
                exit_day = flats.index[0]
                d_exit = idx.get_loc(exit_day) - idx.get_loc(pk)
        # доля избежанного падения: лог-путь стратегии vs лог-путь MCFTR от пика до дна
        r = df_daily.loc[pk:tr_dt]
        s_path = (1 + r["ret"]).prod() - 1
        m_path = (1 + r["r_tr"]).prod() - 1
        avoided = 1 - s_path / m_path if m_path < 0 else np.nan
        # вход после дна
        end = e["recovered"] if e["recovered"] is not None else idx[-1]
        seg2 = pos[(pos.index > tr_dt) & (pos.index <= end)]
        longs = seg2[seg2 > 0.5]
        if len(longs):
            entry = longs.index[0]
            d_entry = idx.get_loc(entry) - idx.get_loc(tr_dt)
        else:
            entry, d_entry = None, np.nan
        r2 = df_daily.loc[tr_dt:end]
        s2 = (1 + r2["ret"]).prod() - 1
        m2 = (1 + r2["r_tr"]).prod() - 1
        missed = 1 - s2 / m2 if m2 > 0 else np.nan
        rows.append(dict(peak=pk.date(), trough=tr_dt.date(), depth=e["depth"], days_to_flat=d_exit,
                         avoided=avoided, days_to_long=d_entry, missed=missed, recovered=end.date()))
    return pd.DataFrame(rows)


def fmt(m):
    return (f"CAGR {m['cagr']*100:+.1f}%  vol {m['vol']*100:.1f}%  Sh {m['sharpe']:.2f}  Shex {m['sharpe_ex']:.2f}  "
            f"MDDd {m['mdd_daily']*100:.1f}%  MDDm {m['mdd_monthly']*100:.1f}%  in {m['time_in']:.0%}  "
            f"tr/y {m['trades_yr']:.1f}  beat {m['beat_years']:.0%}  hit {m['hit']:.2f}  n={m['n_months']}")
