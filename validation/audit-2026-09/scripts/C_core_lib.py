"""Общая библиотека аудита ядра (агент C_core).

Данные: data/panel_prod_daily.csv, data/panel_prod_monthly.csv, data/cash_and_tr.csv.
Все функции — без заглядывания вперёд: сигнал на закрытии месячного среза t → позиция
держится до следующего среза t+1. Издержки 0,2% за КАЖДУЮ смену позиции (round-trip 0,4%).
"""
import sys
import numpy as np
import pandas as pd
from scipy import stats

sys.stdout.reconfigure(encoding="utf-8")
DATA = "data"
RES = "results"

LEGS = [("usd_mom63", +1), ("slope_10_2", +1), ("urals_rub_gap", -1)]
LEG_SIGN = dict(LEGS)
TOXIC = "bear|stress|stress"

ERAS = {
    "2010-2021": ("2010-01-01", "2022-02-18"),
    "2022-2024": ("2022-03-24", "2024-12-31"),
    "2025-2026": ("2025-01-01", "2026-08-31"),
}
MAIN = ("2010-01-01", "2026-08-31")
FULL = ("2004-01-01", "2026-08-31")


# ------------------------------------------------------------------ загрузка
def load():
    D = pd.read_csv(f"{DATA}/panel_prod_daily.csv", index_col=0, parse_dates=True).sort_index()
    M = pd.read_csv(f"{DATA}/panel_prod_monthly.csv", index_col=0, parse_dates=True).sort_index()
    C = pd.read_csv(f"{DATA}/cash_and_tr.csv", index_col=0, parse_dates=True).sort_index()
    return D, M, C


def month_end_idx(D, drop_unfinished=True):
    """Индексы последних торговых дней каждого месяца (как calc.month_end_indices).

    drop_unfinished: последний «срез» панели — это СЕГОДНЯ (2026-09-01), не конец месяца; форвард от
    2026-08-31 к нему — один торговый день, а не месяц. Для IC и бэктестов такой срез выбрасываем."""
    keys = D.index.to_period("M")
    last = pd.Series(D.index, index=D.index).groupby(keys).last()
    me = pd.DatetimeIndex(last.values)
    if drop_unfinished and len(me) and me[-1] == D.index[-1] and D.index[-1].day < 25:
        me = me[:-1]
    return me


def daily_z_prod(D, me, k, w=60, mp=24, clip=3.0, raw_daily=None, raw_monthly=None):
    """z дня t как в проде для незавершённого месяца: окно = (w−1) прошлых месячных срезов + значение дня t."""
    rd = D[k] if raw_daily is None else raw_daily
    rm = rd.reindex(me) if raw_monthly is None else raw_monthly
    mv = rm.values
    raw = rd.values
    out = np.full(len(D), np.nan)
    me_ord = np.array([p.ordinal for p in me.to_period("M")])
    day_ord = np.array([p.ordinal for p in D.index.to_period("M")])
    pos_month = np.searchsorted(me_ord, day_ord)
    for i in range(len(D)):
        if not np.isfinite(raw[i]):
            continue
        j = pos_month[i]
        hist = mv[max(0, j - (w - 1)):j]
        hist = hist[np.isfinite(hist)]
        vals = np.append(hist, raw[i])
        if len(vals) < mp:
            continue
        s = vals.std(ddof=1)
        if s > 0:
            out[i] = np.clip((raw[i] - vals.mean()) / s, -clip, clip)
    return pd.Series(out, index=D.index)


def zroll(x, w=60, mp=24, clip=3.0):
    """Скользящий z как в проде: окно включает текущую точку, ddof=1, обрезка ±clip."""
    m = x.rolling(w, min_periods=mp).mean()
    s = x.rolling(w, min_periods=mp).std()
    z = (x - m) / s
    if clip is not None:
        z = z.clip(-clip, clip)
    return z


def composite_from_z(Z, legs=LEGS, how="mean"):
    """Композит = среднее знаковых z по доступным ногам (n_used адаптивно)."""
    cols = pd.DataFrame({k: sgn * Z[k] for k, sgn in legs if k in Z})
    if how == "mean":
        return cols.mean(axis=1), cols.notna().sum(axis=1)
    if how == "median":
        return cols.median(axis=1), cols.notna().sum(axis=1)
    if how == "vote":  # голосование знаков: +1/−1/0 по большинству
        sg = np.sign(cols)
        return sg.sum(axis=1), cols.notna().sum(axis=1)
    raise ValueError(how)


def hysteresis_sign(x, thr=0.1):
    """Знак с гистерезисом, как calc.hysteresis_sign: NaN до первого уверенного пересечения."""
    out = np.full(len(x), np.nan)
    s = 0
    vals = x.values
    for i, v in enumerate(vals):
        if np.isfinite(v):
            if v > thr:
                s = 1
            elif v < -thr:
                s = -1
        out[i] = s if s else np.nan
    return pd.Series(out, index=x.index)


# ------------------------------------------------------- месячные доходности
def monthly_market(D, C, me=None):
    """Месячные ряды на срезах: fwd лог-доходность IMOEX, простая доходность MCFTR,
    денежный рынок (compounded по дням mm_rate/252) за СЛЕДУЮЩИЙ месяц."""
    if me is None:
        me = month_end_idx(D)
    px = D["imoex"].reindex(me)
    fwd_imoex = np.log(px.shift(-1) / px)
    tr = C["mcftr_ffill"].reindex(D.index).ffill()
    tr_m = tr.reindex(me)
    fwd_tr = tr_m.shift(-1) / tr_m - 1.0
    mm = C["mm_rate"].reindex(D.index).ffill() / 100.0 / 252.0
    # накопленная дневная ставка внутри каждого месяца, отнесённая к ПРЕДЫДУЩЕМУ срезу
    cum = (1 + mm).cumprod()
    cum_m = cum.reindex(me)
    fwd_mm = cum_m.shift(-1) / cum_m - 1.0
    out = pd.DataFrame({"fwd_imoex": fwd_imoex, "fwd_tr": fwd_tr, "fwd_mm": fwd_mm})
    out["mm_rate"] = C["mm_rate"].reindex(D.index).ffill().reindex(me)
    return out


# ------------------------------------------------------------------ бэктест
def backtest(pos, mk, cost=0.002, start=None, end=None):
    """pos: 0/1 на срезе t (решение), доходность за t→t+1. Возвращает DataFrame месяцев."""
    df = mk.copy()
    df["pos"] = pos.reindex(df.index).fillna(0).clip(0, 1)
    if start is not None:
        df = df[df.index >= pd.Timestamp(start)]
    if end is not None:
        df = df[df.index <= pd.Timestamp(end)]
    df = df.dropna(subset=["fwd_tr", "fwd_mm"])
    # смена позиции относительно предыдущего месяца (первый месяц — вход бесплатно, если лонг с нуля считаем сделкой)
    prev = df["pos"].shift(1).fillna(0)
    df["trade"] = (df["pos"] != prev).astype(int)
    df["ret"] = df["pos"] * df["fwd_tr"] + (1 - df["pos"]) * df["fwd_mm"] - cost * df["trade"]
    return df


def metrics(df, ret_col="ret", bh_col="fwd_tr", mm_col="fwd_mm"):
    r = df[ret_col]
    bh = df[bh_col]
    mm = df[mm_col]
    n = len(r)
    if n < 6:
        return {}
    yrs = n / 12.0
    cum = (1 + r).cumprod()
    cagr = cum.iloc[-1] ** (1 / yrs) - 1
    vol = r.std() * np.sqrt(12)
    sharpe = r.mean() / r.std() * np.sqrt(12) if r.std() > 0 else np.nan
    ex = r - mm
    sharpe_ex = ex.mean() / ex.std() * np.sqrt(12) if ex.std() > 0 else np.nan
    dd = (cum / cum.cummax() - 1).min()
    bh_cum = (1 + bh).cumprod()
    bh_cagr = bh_cum.iloc[-1] ** (1 / yrs) - 1
    bh_sh = bh.mean() / bh.std() * np.sqrt(12)
    bh_dd = (bh_cum / bh_cum.cummax() - 1).min()
    pos = df["pos"] if "pos" in df else pd.Series(1.0, index=df.index)
    tim = pos.mean()
    trades_yr = df["trade"].sum() / yrs if "trade" in df else 0.0
    # доля лет обгона b&h
    yr = pd.DataFrame({"s": r, "b": bh}).groupby(df.index.year).apply(lambda g: (1 + g["s"]).prod() > (1 + g["b"]).prod())
    beat_years = yr.mean()
    # hit-rate месяцев: позиция на правильной стороне относительно денег
    right = np.where(pos > 0.5, bh > mm, bh <= mm)
    hit = float(np.mean(right))
    return dict(n=n, cagr=cagr, vol=vol, sharpe=sharpe, sharpe_ex=sharpe_ex, maxdd=dd,
                time_in=tim, trades_yr=trades_yr, beat_years=beat_years, hit=hit,
                bh_cagr=bh_cagr, bh_sharpe=bh_sh, bh_maxdd=bh_dd,
                mm_cagr=(1 + mm).prod() ** (1 / yrs) - 1)


def fmt_metrics(m):
    if not m:
        return "n/a"
    return (f"n={m['n']} CAGR={m['cagr']*100:+.1f}% vol={m['vol']*100:.1f}% Sh={m['sharpe']:.2f} "
            f"Shex={m['sharpe_ex']:.2f} MDD={m['maxdd']*100:.1f}% in={m['time_in']:.0%} "
            f"tr/y={m['trades_yr']:.1f} beat={m['beat_years']:.0%} hit={m['hit']:.2f} | "
            f"b&h {m['bh_cagr']*100:+.1f}%/{m['bh_sharpe']:.2f}/{m['bh_maxdd']*100:.1f}% mm {m['mm_cagr']*100:.1f}%")


# ------------------------------------------------------------------ статистика
def stationary_bootstrap_idx(n, mean_block, rng):
    p = 1.0 / mean_block
    idx = np.empty(n, dtype=int)
    idx[0] = rng.integers(n)
    for i in range(1, n):
        if rng.random() < p:
            idx[i] = rng.integers(n)
        else:
            idx[i] = (idx[i - 1] + 1) % n
    return idx


def nw_t_ranks(x, y, lag):
    """t-статистика Ньюи-Уэста для наклона регрессии рангов y на ранги x."""
    xr = stats.rankdata(x)
    yr = stats.rankdata(y)
    xs = (xr - xr.mean()) / xr.std()
    ys = (yr - yr.mean()) / yr.std()
    n = len(xs)
    beta = (xs * ys).mean()
    u = xs * (ys - beta * xs)
    L = int(min(lag, max(1, n // 3)))
    s = (u ** 2).sum()
    for l in range(1, L + 1):
        w = 1 - l / (L + 1)
        s += 2 * w * (u[:-l] * u[l:]).sum()
    var_beta = s / n ** 2
    return beta / np.sqrt(var_beta) if var_beta > 0 else np.nan


def ic_stats(sig, fwd, lag=1, n_boot=1000, block=6, seed=1, min_n=12):
    """Спирмен IC + NW-t по рангам + стационарный бутстреп (SE, p, ДИ 90%)."""
    df = pd.concat([sig.rename("s"), fwd.rename("f")], axis=1).dropna()
    n = len(df)
    if n < min_n:
        return dict(ic=np.nan, n=n, p_sp=np.nan, nw_t=np.nan, p_boot=np.nan, ci_lo=np.nan, ci_hi=np.nan)
    x, y = df["s"].values, df["f"].values
    rho, p_sp = stats.spearmanr(x, y)
    t_nw = nw_t_ranks(x, y, lag)
    rng = np.random.default_rng(seed)
    bs = np.empty(n_boot)
    for b in range(n_boot):
        ii = stationary_bootstrap_idx(n, block, rng)
        bs[b] = stats.spearmanr(x[ii], y[ii])[0]
    se = np.nanstd(bs)
    p_boot = 2 * (1 - stats.norm.cdf(abs(rho) / se)) if se > 0 else np.nan
    lo, hi = np.nanpercentile(bs, [5, 95])
    return dict(ic=rho, n=n, p_sp=p_sp, nw_t=t_nw, p_boot=p_boot, ci_lo=lo, ci_hi=hi)


def sharpe_diff_boot(r1, r2, n_boot=2000, block=8, seed=2):
    """Бутстреп разности Шарпов (стационарный, блоки ~8 мес). Возвращает (diff, p, ci)."""
    df = pd.concat([r1.rename("a"), r2.rename("b")], axis=1).dropna()
    a, b = df["a"].values, df["b"].values
    n = len(a)

    def sh(v):
        return v.mean() / v.std() * np.sqrt(12) if v.std() > 0 else 0.0

    obs = sh(a) - sh(b)
    rng = np.random.default_rng(seed)
    bs = np.empty(n_boot)
    for k in range(n_boot):
        ii = stationary_bootstrap_idx(n, block, rng)
        bs[k] = sh(a[ii]) - sh(b[ii])
    se = bs.std()
    p = 2 * (1 - stats.norm.cdf(abs(obs) / se)) if se > 0 else np.nan
    return obs, p, (np.percentile(bs, 5), np.percentile(bs, 95))


# ------------------------------------------------------------ своевременность
def drawdown_episodes(price, thr=0.15):
    """Эпизоды просадок > thr по дневному ряду: (peak_date, trough_date, recov_date|None, depth)."""
    p = price.dropna()
    peak_val, peak_dt = p.iloc[0], p.index[0]
    episodes = []
    in_dd, trough_val, trough_dt = False, None, None
    for dt, v in p.items():
        if v >= peak_val:
            if in_dd and (peak_val - trough_val) / peak_val >= thr:
                episodes.append((peak_dt, trough_dt, dt, trough_val / peak_val - 1))
            peak_val, peak_dt = v, dt
            in_dd, trough_val, trough_dt = False, None, None
        else:
            if not in_dd:
                in_dd, trough_val, trough_dt = True, v, dt
            elif v < trough_val:
                trough_val, trough_dt = v, dt
    if in_dd and (peak_val - trough_val) / peak_val >= thr:
        episodes.append((peak_dt, trough_dt, None, trough_val / peak_val - 1))
    return episodes


def timeliness(pos_m, price_d, me, thr=0.15, start=None):
    """Для каждой просадки >thr: через сколько торговых дней после пика правило ушло во флэт,
    доля падения, которой избежали, дней после дна до возврата в лонг, доля восстановления, которую пропустили.
    pos_m — позиция на срезе t (держится t→t+1); переводим в дневную."""
    price = price_d.dropna()
    if start is not None:
        price = price[price.index >= pd.Timestamp(start)]
    # позиция, действующая на доходность дня d, = решение на последнем срезе СТРОГО до d
    dec = pd.Series(np.nan, index=price.index)
    common = pos_m.index.intersection(price.index)
    dec.loc[common] = pos_m.reindex(common).values
    pos_d = dec.shift(1).ffill().fillna(0)
    logret = np.log(price / price.shift(1)).fillna(0)
    rows = []
    for peak, trough, recov, depth in drawdown_episodes(price, thr):
        seg = price.loc[peak:trough]
        tot_decl = np.log(price.loc[trough] / price.loc[peak])
        incurred = (logret.loc[peak:trough].iloc[1:] * pos_d.loc[peak:trough].iloc[1:]).sum()
        avoided = 1 - incurred / tot_decl if tot_decl != 0 else np.nan
        # первый день во флэте после пика (если на пике уже флэт — 0 или отрицательно: ищем последний вход во флэт до пика)
        pd_after = pos_d.loc[peak:]
        if pos_d.loc[peak] == 0:
            # уже во флэте: когда ушёл во флэт
            before = pos_d.loc[:peak]
            last_long = before[before > 0].index.max() if (before > 0).any() else None
            days_to_flat = -(len(price.loc[last_long:peak]) - 1) if last_long is not None else np.nan
        else:
            flat = pd_after[pd_after == 0]
            days_to_flat = (len(price.loc[peak:flat.index[0]]) - 1) if len(flat) else np.nan
        # восстановление: дно → recov (или до конца)
        end_r = recov if recov is not None else price.index[-1]
        tot_up = np.log(price.loc[end_r] / price.loc[trough])
        captured = (logret.loc[trough:end_r].iloc[1:] * pos_d.loc[trough:end_r].iloc[1:]).sum()
        missed = 1 - captured / tot_up if tot_up > 0 else np.nan
        after_t = pos_d.loc[trough:]
        if pos_d.loc[trough] > 0:
            days_to_long = 0
        else:
            lg = after_t[after_t > 0]
            days_to_long = (len(price.loc[trough:lg.index[0]]) - 1) if len(lg) else np.nan
        rows.append(dict(peak=peak.date(), trough=trough.date(), recov=(recov.date() if recov is not None else None),
                         depth=depth, days_to_flat=days_to_flat, avoided=avoided,
                         days_to_long=days_to_long, missed=missed))
    return pd.DataFrame(rows)


# ------------------------------------------------------------ правила панели
def panel_positions(M, comp=None, hyst=0.1, gate=True):
    """Позиция панели на срезе: ворота (ячейка не токсичная) И знак композита с гистерезисом > 0."""
    c = M["composite"] if comp is None else comp
    s = hysteresis_sign(c, hyst)
    pos = (s > 0).astype(float)
    if gate:
        pos = pos * (M["cell"] != TOXIC).astype(float)
    return pos


def era_slices(index):
    out = {"MAIN 2010-26": (index >= MAIN[0]) & (index <= MAIN[1]),
           "FULL 2004-26": (index >= FULL[0]) & (index <= FULL[1])}
    for k, (a, b) in ERAS.items():
        out[k] = (index >= a) & (index <= b)
    out["ex-2022"] = ((index >= MAIN[0]) & (index <= MAIN[1]) & ~((index >= "2022-01-01") & (index <= "2022-12-31")))
    return out
