"""Библиотека агента H_weekly: частота наблюдения/решения панели MOEX Radar.

Версии композита (одни и те же три ноги usd_mom63 +, slope_10_2 +, urals_rub_gap −):
  M_closed — месячный композит панели (z по 60 мес / min 24, обрезка ±3), значение
             ПОСЛЕДНЕГО ЗАКРЫТОГО месяца, растянутое на дни следующего месяца (прод).
  M_live   — «дневное справочное» значение прода: z по окну из 59 закрытых месяцев +
             сегодняшнее значение (ровно так считает monthly_frame с незакрытым месяцем).
  W        — недельный композит: значения ног на последний торговый день недели,
             z по 260 нед / min 104, обрезка ±3; растянут на дни следующей недели.
  D        — дневной композит: z по 1260 дн / min 504, обрезка ±3.

Бэктест — дневной: позиция pos_t принимается на закрытии t и действует на доходность t→t+1.
Лонг = MCFTR (полная доходность), флэт = mm_rate/252 в день. Издержки cost за КАЖДУЮ смену
позиции (по умолчанию 0,2%, round-trip 0,4%).
"""
import sys
import numpy as np
import pandas as pd
from scipy import stats

sys.stdout.reconfigure(encoding="utf-8")
DATA = "data"
RES = "results"

LEGS = [("usd_mom63", +1), ("slope_10_2", +1), ("urals_rub_gap", -1)]
TOXIC = "bear|stress|stress"
TRADING_DAYS = 252

MAIN = ("2010-01-01", "2026-08-31")
FULL = ("2004-01-01", "2026-08-31")
ERAS = {
    "2010-2021": ("2010-01-01", "2022-02-18"),
    "2022-2024": ("2022-03-24", "2024-12-31"),
    "2025-2026": ("2025-01-01", "2026-08-31"),
}
SPLITS = {"2004-2017": ("2004-01-01", "2017-12-31"), "2018-2026": ("2018-01-01", "2026-08-31")}


# ------------------------------------------------------------------ загрузка
def load():
    D = pd.read_csv(f"{DATA}/panel_prod_daily.csv", index_col=0, parse_dates=True).sort_index()
    M = pd.read_csv(f"{DATA}/panel_prod_monthly.csv", index_col=0, parse_dates=True).sort_index()
    C = pd.read_csv(f"{DATA}/cash_and_tr.csv", index_col=0, parse_dates=True).sort_index()
    C = C.reindex(D.index)
    return D, M, C


def month_end_idx(D):
    keys = D.index.to_period("M")
    return pd.DatetimeIndex(pd.Series(D.index, index=D.index).groupby(keys).last().values)


def week_end_idx(D):
    """Последний торговый день каждой недели (недели по пятницам)."""
    keys = D.index.to_period("W-FRI")
    return pd.DatetimeIndex(pd.Series(D.index, index=D.index).groupby(keys).last().values)


def zroll(x, w, mp, clip=3.0):
    m = x.rolling(w, min_periods=mp).mean()
    s = x.rolling(w, min_periods=mp).std()
    z = (x - m) / s
    return z.clip(-clip, clip) if clip is not None else z


def composite_from_raw(raw, w, mp, clip=3.0):
    """raw: DataFrame с колонками ног на нужной частоте → (композит, n_used)."""
    cols = {}
    for k, sgn in LEGS:
        if k in raw:
            cols[k] = sgn * zroll(raw[k], w, mp, clip)
    Z = pd.DataFrame(cols)
    return Z.mean(axis=1), Z.notna().sum(axis=1), Z


def hysteresis_sign(x, thr=0.1):
    """Знак с гистерезисом (бегущее состояние): +1 при x>thr, −1 при x<−thr, иначе прежний.
    NaN до первого уверенного пересечения."""
    out = np.full(len(x), np.nan)
    s = 0
    for i, v in enumerate(np.asarray(x, dtype=float)):
        if np.isfinite(v):
            if v > thr:
                s = 1
            elif v < -thr:
                s = -1
        out[i] = s if s else np.nan
    return pd.Series(out, index=x.index)


def build_composites(D, M):
    """Возвращает DataFrame на дневном индексе: M_closed, M_live, W, D (+ n_used) и
    словарь «родных» рядов (monthly/weekly) для IC на их частоте."""
    idx = D.index
    me = month_end_idx(D)
    we = week_end_idx(D)
    legs = [k for k, _ in LEGS]
    out = pd.DataFrame(index=idx)

    # --- M_closed: месячный композит прода (сверен побитово: invariant_check.txt)
    raw_m = D[legs].reindex(me)  # значение на последнем торговом дне месяца
    comp_m, n_m, Zm = composite_from_raw(raw_m, 60, 24)
    # контроль: совпадение с panel_prod_monthly
    chk = (comp_m - M["composite"].reindex(comp_m.index)).abs().max()
    out["M_closed"] = comp_m.reindex(idx).ffill()  # значение закрытого месяца видно с его последнего дня
    out["M_closed_n"] = n_m.reindex(idx).ffill()

    # --- M_live: 59 закрытых месяцев + сегодняшнее значение (прод-справочное дневное)
    live = {}
    month_of = idx.to_period("M")
    me_month = me.to_period("M")
    for k in legs:
        rm = raw_m[k].values  # по месяцам
        xd = D[k].values
        res = np.full(len(idx), np.nan)
        # для каждого месяца m: окно = последние 59 позиций месячного ряда до m
        month_list = list(me_month)
        pos_of_month = {m: i for i, m in enumerate(month_list)}
        cache = {}
        for i in range(len(idx)):
            m = month_of[i]
            j = pos_of_month[m]
            if m not in cache:
                lo = max(0, j - 59)
                prev = rm[lo:j]
                prev = prev[np.isfinite(prev)]
                cache[m] = prev
            prev = cache[m]
            v = xd[i]
            if not np.isfinite(v):
                continue
            vals = np.append(prev, v)
            if len(vals) < 24:
                continue
            mu, sd = vals.mean(), vals.std(ddof=1)
            if sd > 0:
                res[i] = np.clip((v - mu) / sd, -3, 3)
        live[k] = res
    Zl = pd.DataFrame({k: sgn * live[k] for k, sgn in LEGS}, index=idx)
    out["M_live"] = Zl.mean(axis=1)
    out["M_live_n"] = Zl.notna().sum(axis=1)

    # --- W: недельный
    raw_w = D[legs].reindex(we)
    comp_w, n_w, Zw = composite_from_raw(raw_w, 260, 104)
    out["W"] = comp_w.reindex(idx).ffill()
    out["W_n"] = n_w.reindex(idx).ffill()

    # --- D: дневной
    comp_d, n_d, Zd = composite_from_raw(D[legs], 1260, 504)
    out["D"] = comp_d
    out["D_n"] = n_d

    native = {"M": comp_m, "W": comp_w, "D": comp_d, "Zm": Zm, "Zw": Zw, "Zd": Zd, "Zl": Zl,
              "me": me, "we": we, "check_M": chk}
    return out, native


# ------------------------------------------------------------------ рынок
def market_daily(D, C):
    tr = C["mcftr_ffill"].ffill()
    r_tr = tr / tr.shift(1) - 1.0
    mm = C["mm_rate"].ffill() / 100.0 / TRADING_DAYS
    return pd.DataFrame({"r_tr": r_tr, "r_mm": mm, "tr": tr, "imoex": D["imoex"],
                         "mm_rate": C["mm_rate"].ffill()})


def gate_series(D):
    """Ворота: 1 = ячейка не токсичная (открыты). NaN до 2004 → считаем закрытыми."""
    g = (D["cell"] != TOXIC) & D["cell"].notna()
    return g.astype(float)


# ------------------------------------------------------------------ позиции
def positions(signal, decision_days, index, min_hold=1, exit_anytime=False):
    """signal: дневной 0/1 (что сигнал говорит на закрытии t). Решение принимается только
    в decision_days (булева маска на index); иначе позиция удерживается.
    min_hold: после смены позиции следующая смена не раньше чем через min_hold дней решения
    (в торговых днях). exit_anytime: выход во флэт разрешён любым днём, вход — только в
    дни решения."""
    sig = signal.reindex(index).values
    dec = np.asarray(decision_days, dtype=bool)
    pos = np.zeros(len(index))
    cur = 0.0
    last_change = -10 ** 9
    for i in range(len(index)):
        s = sig[i]
        if not np.isfinite(s):
            pos[i] = cur
            continue
        can = dec[i] or (exit_anytime and s < cur)
        if can and s != cur and (i - last_change) >= min_hold:
            cur = s
            last_change = i
        pos[i] = cur
    return pd.Series(pos, index=index)


def backtest_daily(pos, mk, cost=0.002):
    """pos_t действует на доходность t→t+1. Возвращает дневной DataFrame."""
    df = mk[["r_tr", "r_mm"]].copy()
    p = pos.reindex(df.index).ffill().fillna(0.0)
    p_prev = p.shift(1).fillna(0.0)
    trade = (p != p_prev).astype(float)  # смена позиции на закрытии t
    # доходность дня t+1 определяется позицией на закрытии t; издержка списывается в день смены
    df["pos"] = p
    df["trade"] = trade
    df["ret"] = p_prev * df["r_tr"] + (1 - p_prev) * df["r_mm"] - cost * trade
    df["pos_eff"] = p_prev
    return df


def slice_win(df, win):
    a, b = win
    return df[(df.index >= pd.Timestamp(a)) & (df.index <= pd.Timestamp(b))]


def to_monthly(df):
    g = df.groupby(df.index.to_period("M"))
    m = pd.DataFrame({
        "ret": g["ret"].apply(lambda x: (1 + x).prod() - 1),
        "bh": g["r_tr"].apply(lambda x: (1 + x).prod() - 1),
        "mm": g["r_mm"].apply(lambda x: (1 + x).prod() - 1),
        "pos": g["pos_eff"].mean(),
    })
    m.index = m.index.to_timestamp("M")
    return m


def metrics(df):
    """df — дневной результат backtest_daily, уже нарезанный окном."""
    df = df.dropna(subset=["r_tr", "r_mm"])
    if len(df) < 120:
        return {}
    m = to_monthly(df)
    n = len(m)
    yrs = len(df) / TRADING_DAYS
    cum = (1 + df["ret"]).cumprod()
    cagr = cum.iloc[-1] ** (1 / yrs) - 1
    vol = m["ret"].std() * np.sqrt(12)
    sharpe = m["ret"].mean() / m["ret"].std() * np.sqrt(12) if m["ret"].std() > 0 else np.nan
    ex = m["ret"] - m["mm"]
    sharpe_ex = ex.mean() / ex.std() * np.sqrt(12) if ex.std() > 0 else np.nan
    sharpe_d = df["ret"].mean() / df["ret"].std() * np.sqrt(TRADING_DAYS) if df["ret"].std() > 0 else np.nan
    mdd = (cum / cum.cummax() - 1).min()
    bh_cum = (1 + df["r_tr"]).cumprod()
    bh_cagr = bh_cum.iloc[-1] ** (1 / yrs) - 1
    bh_sh = m["bh"].mean() / m["bh"].std() * np.sqrt(12)
    bh_mdd = (bh_cum / bh_cum.cummax() - 1).min()
    mm_cagr = (1 + df["r_mm"]).prod() ** (1 / yrs) - 1
    time_in = df["pos_eff"].mean()
    trades_yr = df["trade"].sum() / yrs
    yr = pd.DataFrame({"s": m["ret"], "b": m["bh"]}).groupby(m.index.year).apply(
        lambda g: (1 + g["s"]).prod() > (1 + g["b"]).prod())
    right = np.where(m["pos"] > 0.5, m["bh"] > m["mm"], m["bh"] <= m["mm"])
    return dict(n_months=n, cagr=cagr, vol=vol, sharpe=sharpe, sharpe_ex=sharpe_ex, sharpe_daily=sharpe_d,
                maxdd=mdd, time_in=time_in, trades_yr=trades_yr, beat_years=yr.mean(), hit=float(np.mean(right)),
                bh_cagr=bh_cagr, bh_sharpe=bh_sh, bh_maxdd=bh_mdd, mm_cagr=mm_cagr)


def fmt(mt):
    if not mt:
        return "n/a"
    return (f"CAGR={mt['cagr']*100:+.1f}% vol={mt['vol']*100:.1f}% Sh={mt['sharpe']:.2f} Shex={mt['sharpe_ex']:.2f} "
            f"MDD={mt['maxdd']*100:.1f}% in={mt['time_in']:.0%} tr/y={mt['trades_yr']:.1f} beat={mt['beat_years']:.0%} "
            f"hit={mt['hit']:.2f} | b&h {mt['bh_cagr']*100:+.1f}%/{mt['bh_sharpe']:.2f}/{mt['bh_maxdd']*100:.1f}% "
            f"mm {mt['mm_cagr']*100:.1f}%")


def windows(index):
    out = {"MAIN 2010-26": MAIN, "FULL 2004-26": FULL}
    out.update(ERAS)
    return out


def slice_ex2022(df):
    d = slice_win(df, MAIN)
    return d[~((d.index >= "2022-01-01") & (d.index <= "2022-12-31"))]


# ------------------------------------------------------------------ статистика
def stationary_bootstrap_idx(n, mean_block, rng):
    p = 1.0 / mean_block
    idx = np.empty(n, dtype=int)
    idx[0] = rng.integers(n)
    for i in range(1, n):
        idx[i] = rng.integers(n) if rng.random() < p else (idx[i - 1] + 1) % n
    return idx


def sharpe_diff_boot(r1, r2, n_boot=2000, block=8, seed=2):
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


def nw_t(x, y, lag):
    """NW-t наклона регрессии y на x (центрированные), лаг Бартлетта."""
    x = np.asarray(x, float); y = np.asarray(y, float)
    xs = (x - x.mean()) / x.std()
    ys = (y - y.mean()) / y.std()
    n = len(xs)
    beta = (xs * ys).mean()
    u = xs * (ys - beta * xs)
    L = int(min(lag, max(1, n // 3)))
    s = (u ** 2).sum()
    for l in range(1, L + 1):
        s += 2 * (1 - l / (L + 1)) * (u[:-l] * u[l:]).sum()
    var = s / n ** 2
    return beta / np.sqrt(var) if var > 0 else np.nan


def ic_horizon(sig, fwd, h_steps, min_n=24):
    """IC Спирмена сигнала к форварду на h шагов: полная перекрывающаяся выборка + NW-t(lag=h),
    и невырожденная — среднее/мин/макс IC по h смещениям (каждое h-е наблюдение)."""
    df = pd.concat([sig.rename("s"), fwd.rename("f")], axis=1).dropna()
    n = len(df)
    if n < min_n:
        return dict(ic=np.nan, n=n, nw_t=np.nan, ic_nonovl_mean=np.nan, ic_nonovl_min=np.nan,
                    ic_nonovl_max=np.nan, n_nonovl=0, p_nonovl_med=np.nan)
    rho = stats.spearmanr(df["s"], df["f"])[0]
    t = nw_t(stats.rankdata(df["s"]), stats.rankdata(df["f"]), h_steps)
    ics, ps = [], []
    for o in range(h_steps):
        sub = df.iloc[o::h_steps]
        if len(sub) >= min_n:
            r, p = stats.spearmanr(sub["s"], sub["f"])
            ics.append(r); ps.append(p)
    return dict(ic=rho, n=n, nw_t=t, ic_nonovl_mean=np.mean(ics) if ics else np.nan,
                ic_nonovl_min=np.min(ics) if ics else np.nan, ic_nonovl_max=np.max(ics) if ics else np.nan,
                n_nonovl=int(n / h_steps), p_nonovl_med=np.median(ps) if ps else np.nan)


# ------------------------------------------------------------ своевременность
def drawdown_episodes(price, thr=0.15):
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


def timeliness(pos_d, price_d, thr=0.15, start="2004-01-01"):
    """pos_d — дневная позиция (решение на закрытии t, действует t→t+1).
    Для каждой просадки >thr: дней после пика до ухода во флэт (отриц. — ушёл раньше пика),
    доля падения, которой избежали; дней после дна до входа в лонг, доля восстановления, которую пропустили."""
    price = price_d.dropna()
    price = price[price.index >= pd.Timestamp(start)]
    pos_eff = pos_d.reindex(price.index).ffill().fillna(0).shift(1).fillna(0)  # позиция, действующая в день d
    logret = np.log(price / price.shift(1)).fillna(0)
    rows = []
    for peak, trough, recov, depth in drawdown_episodes(price, thr):
        tot_decl = np.log(price.loc[trough] / price.loc[peak])
        incurred = (logret.loc[peak:trough].iloc[1:] * pos_eff.loc[peak:trough].iloc[1:]).sum()
        avoided = 1 - incurred / tot_decl if tot_decl != 0 else np.nan
        pe = pos_eff.loc[peak:]
        if pos_eff.loc[peak] == 0 and (pos_eff.loc[:peak] > 0).any():
            before = pos_eff.loc[:peak]
            last_long = before[before > 0].index.max()
            days_to_flat = -(len(price.loc[last_long:peak]) - 1)
        elif pos_eff.loc[peak] == 0:
            days_to_flat = np.nan
        else:
            flat = pe[pe == 0]
            days_to_flat = (len(price.loc[peak:flat.index[0]]) - 1) if len(flat) else np.nan
        end_r = recov if recov is not None else price.index[-1]
        tot_up = np.log(price.loc[end_r] / price.loc[trough])
        captured = (logret.loc[trough:end_r].iloc[1:] * pos_eff.loc[trough:end_r].iloc[1:]).sum()
        missed = 1 - captured / tot_up if tot_up > 0 else np.nan
        at = pos_eff.loc[trough:]
        if pos_eff.loc[trough] > 0:
            days_to_long = 0
        else:
            lg = at[at > 0]
            days_to_long = (len(price.loc[trough:lg.index[0]]) - 1) if len(lg) else np.nan
        rows.append(dict(peak=peak.date(), trough=trough.date(), recov=(recov.date() if recov is not None else None),
                         depth=round(depth, 3), days_to_flat=days_to_flat, avoided=round(avoided, 2) if np.isfinite(avoided) else np.nan,
                         days_to_long=days_to_long, missed=round(missed, 2) if np.isfinite(missed) else np.nan))
    return pd.DataFrame(rows)
