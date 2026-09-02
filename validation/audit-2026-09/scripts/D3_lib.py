"""D3_lib — общие функции аудита блока value / mean-reversion / сезонность (агент D3_value).

Соглашения (бриф, правила 1–5):
- сигнал на закрытии дня t -> позиция с закрытия t (доходность дня t+1);
- лонг = MCFTR (полная доходность), флэт = mm_rate/252 в день;
- издержки 0,2% за смену позиции (round-trip 0,4%);
- месячные решения: значение на последний торговый день месяца -> доходность следующего месяца;
- IC: Спирмен на месячной выборке + Ньюи-Уэст (лаг = горизонт) + стационарный бутстреп
  (Politis–Romano, средний блок 12 мес) с ресемплингом СИГНАЛА при фиксированной доходности.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

sys.stdout.reconfigure(encoding="utf-8")

AUD = Path(__file__).resolve().parents[1]
DATA, RES = AUD / "data", AUD / "results"
RES.mkdir(exist_ok=True)

TOXIC = "bear|stress|stress"
WINDOWS = {
    "main_2010-2026": ("2010-01-01", "2026-08-31"),
    "full_2004-2026": ("2004-01-01", "2026-08-31"),
    "era_2010-2021": ("2010-01-01", "2021-12-31"),
    "era_2022_03-2024": ("2022-03-01", "2024-12-31"),
    "era_2025-2026": ("2025-01-01", "2026-08-31"),
    "split_2004-2017": ("2004-01-01", "2017-12-31"),
    "split_2018-2026": ("2018-01-01", "2026-08-31"),
    "pre2022_2004-2021": ("2004-01-01", "2021-12-31"),
    "post2022_03-2026": ("2022-03-01", "2026-08-31"),
}
COST = 0.002


# ------------------------------------------------------------------ загрузка
def load_daily():
    P = pd.read_csv(DATA / "panel_prod_daily.csv", parse_dates=["date"]).set_index("date").sort_index()
    C = pd.read_csv(DATA / "cash_and_tr.csv", parse_dates=["date"]).set_index("date").sort_index()
    df = P.join(C[["mm_rate", "mcftr_ffill", "rusfar3m"]])
    df["r_tr"] = np.log(df["mcftr_ffill"]).diff()
    df["r_px"] = np.log(df["imoex"]).diff()
    df["rf"] = df["mm_rate"] / 100.0 / 252.0
    df["toxic"] = (df["cell"] == TOXIC).astype(float).where(df["cell"].notna())
    df["ym"] = df.index.to_period("M")
    return df


def load_raw(names):
    R = pd.read_csv(DATA / "raw_long.csv", parse_dates=["date"])
    return {n: R[R.series == n].set_index("date")["value"].sort_index().astype(float) for n in names}


def load_monthly_panel():
    return pd.read_csv(DATA / "panel_prod_monthly.csv", parse_dates=["date"]).set_index("date").sort_index()


def month_end_dates(df):
    return df.index.to_series().groupby(df["ym"]).last().values


def monthly_frame(df, horizons=(1, 3, 6, 12)):
    """Срез на последний торговый день месяца + форвардные доходности (лог).
    fwd_tr_h — MCFTR, fwd_px_h — IMOEX, fwd_rf_h — накопленная ставка флэта, fwd_ex_h — избыток над ставкой."""
    me = month_end_dates(df)
    M = df.loc[me].copy()
    rf_m = df["rf"].groupby(df["ym"]).sum()
    rf_m.index = me
    M["rf_m"] = rf_m
    ltr, lpx = np.log(M["mcftr_ffill"]), np.log(M["imoex"])
    for h in horizons:
        M[f"fwd_tr_{h}"] = ltr.shift(-h) - ltr
        M[f"fwd_px_{h}"] = lpx.shift(-h) - lpx
        M[f"fwd_rf_{h}"] = M["rf_m"].shift(-h).rolling(h).sum()
        M[f"fwd_ex_{h}"] = M[f"fwd_tr_{h}"] - M[f"fwd_rf_{h}"]
    mp = load_monthly_panel()
    M["composite"] = mp["composite"].reindex(M.index)
    M["hyst"] = hysteresis_sign(M["composite"], 0.10)
    return M


def hysteresis_sign(x, thr=0.10):
    out, s = np.zeros(len(x)), 0
    for i, v in enumerate(np.asarray(x, dtype=float)):
        if np.isfinite(v):
            if v > thr:
                s = 1
            elif v < -thr:
                s = -1
        out[i] = s
    return pd.Series(out, index=x.index)


def in_window(idx, win):
    a, b = WINDOWS[win] if isinstance(win, str) else win
    return (idx >= pd.Timestamp(a)) & (idx <= pd.Timestamp(b))


def ex2022(idx):
    return idx.year != 2022


# ------------------------------------------------------------------ бутстреп
def stat_boot_idx(n, mean_block, rng):
    """Индексы стационарного бутстрепа (Politis–Romano) длины n."""
    p = 1.0 / mean_block
    flags = rng.random(n) < p
    flags[0] = True
    starts = rng.integers(0, n, n)
    bid = np.cumsum(flags) - 1
    bstart = starts[flags][bid]
    pos = np.arange(n)
    first_pos = pos[flags][bid]
    return (bstart + (pos - first_pos)) % n


def _hac_t(x, y, lag):
    """t-статистика наклона стандартизованных x,y с HAC (Бартлетт)."""
    n = len(x)
    xs = (x - x.mean()) / x.std()
    ys = (y - y.mean()) / y.std()
    beta = (xs * ys).mean()
    u = xs * (ys - beta * xs)
    L = int(min(lag, n // 3))
    s = (u ** 2).sum()
    for l in range(1, L + 1):
        w = 1 - l / (L + 1)
        s += 2 * w * (u[:-l] * u[l:]).sum()
    var_beta = s / n ** 2
    return beta / np.sqrt(var_beta) if var_beta > 0 else np.nan


def ic_stats(sig, fwd, h=1, nboot=0, block=12, seed=0, min_n=24):
    """Спирмен-IC сигнала к форвардной доходности на месячной выборке.
    p_nw — HAC-t на рангах (лаг = h); p_boot — стационарный бутстреп сигнала (H0: нет связи)."""
    s, f = pd.Series(sig).astype(float), pd.Series(fwd).astype(float)
    m = s.notna() & f.notna()
    n = int(m.sum())
    out = dict(ic=np.nan, t_nw=np.nan, p_nw=np.nan, p_boot=np.nan, n=n)
    if n < min_n or s[m].nunique() < 3:
        return out
    x, y = s[m].values, f[m].values
    rho = stats.spearmanr(x, y)[0]
    rx, ry = stats.rankdata(x), stats.rankdata(y)
    t = _hac_t(rx, ry, lag=max(h, 1))
    out.update(ic=rho, t_nw=t, p_nw=2 * (1 - stats.norm.cdf(abs(t))) if np.isfinite(t) else np.nan)
    if nboot:
        rng = np.random.default_rng(seed)
        cnt = 0
        for _ in range(nboot):
            xb = rx[stat_boot_idx(n, block, rng)]
            if abs(np.corrcoef(xb, ry)[0, 1]) >= abs(rho):
                cnt += 1
        out["p_boot"] = (cnt + 1) / (nboot + 1)
    return out


def cond_stats(cond, y, nboot=2000, block=12, seed=0):
    """Средняя форвардная доходность при условии cond (bool) против остальных.
    p_boot — стационарный бутстреп индикатора cond (та же персистентность, H0: условие ничего не выделяет)."""
    c, yy = pd.Series(cond).astype(float), pd.Series(y).astype(float)
    m = c.notna() & yy.notna()
    c, yy = c[m].values.astype(bool), yy[m].values
    n, k = len(yy), int(c.sum())
    out = dict(n=n, n_cond=k, mean_cond=np.nan, mean_rest=np.nan, diff=np.nan, hit_cond=np.nan, p_boot=np.nan,
               t_simple=np.nan)
    if k < 3 or k == n:
        return out
    mc, mr = yy[c].mean(), yy[~c].mean()
    out.update(mean_cond=mc, mean_rest=mr, diff=mc - mr, hit_cond=(yy[c] > 0).mean(),
               t_simple=(mc - mr) / np.sqrt(yy[c].var(ddof=1) / k + yy[~c].var(ddof=1) / (n - k)))
    if nboot:
        rng = np.random.default_rng(seed)
        cnt = 0
        for _ in range(nboot):
            cb = c[stat_boot_idx(n, block, rng)]
            kb = cb.sum()
            if kb < 2 or kb == n:
                cnt += 1
                continue
            if abs(yy[cb].mean() - yy[~cb].mean()) >= abs(mc - mr):
                cnt += 1
        out["p_boot"] = (cnt + 1) / (nboot + 1)
    return out


def fdr_bh(p):
    p = np.asarray(p, dtype=float)
    q = np.full_like(p, np.nan)
    m = np.isfinite(p)
    pv = p[m]
    k = len(pv)
    if k == 0:
        return q
    order = np.argsort(pv)
    ranked = pv[order] * k / (np.arange(k) + 1)
    ranked = np.minimum.accumulate(ranked[::-1])[::-1]
    qq = np.empty(k)
    qq[order] = np.minimum(ranked, 1.0)
    q[m] = qq
    return q


# ------------------------------------------------------------------ стратегии
def backtest(df, pos, win, cost=COST):
    """pos — дневной ряд 0/1 (решение на закрытии t). Возвращает DataFrame дневных результатов в окне."""
    a, b = WINDOWS[win] if isinstance(win, str) else win
    p = pos.reindex(df.index).ffill().fillna(0.0).clip(0, 1)
    held = p.shift(1).fillna(0.0)
    trade = p.diff().abs().fillna(0.0)
    r = held * df["r_tr"] + (1 - held) * df["rf"] - cost * trade
    out = pd.DataFrame({"r": r, "pos": p, "held": held, "trade": trade, "r_tr": df["r_tr"], "rf": df["rf"]})
    out = out.loc[a:b].dropna(subset=["r"])
    return out


def metrics(bt, name=""):
    r = bt["r"]
    ym = r.index.to_period("M")
    years = len(r) / 252.0
    mo = r.groupby(ym).sum()
    mo_bh = bt["r_tr"].groupby(ym).sum()
    mo_rf = bt["rf"].groupby(ym).sum()
    cum = r.cumsum()
    dd = cum - cum.cummax()
    yr = r.groupby(r.index.year).sum()
    yr_bh = bt["r_tr"].groupby(bt.index.year).sum()
    ex = mo - mo_rf
    return dict(
        name=name,
        cagr_pct=100 * (np.exp(r.sum() / years) - 1),
        vol_pct=100 * mo.std() * np.sqrt(12),
        sharpe=mo.mean() / mo.std() * np.sqrt(12) if mo.std() > 0 else np.nan,
        sharpe_ex=ex.mean() / ex.std() * np.sqrt(12) if ex.std() > 0 else np.nan,
        maxdd_pct=100 * (np.exp(dd.min()) - 1),
        time_in_mkt=bt["held"].mean(),
        trades_per_yr=bt["trade"].sum() / years,
        beat_bh_years=(yr > yr_bh).mean(),
        hit_months=(mo > 0).mean(),
        hit_vs_bh=(mo > mo_bh).mean(),
        n_months=len(mo),
        years=years,
    )


def sharpe_diff_boot(mo_a, mo_b, nboot=2000, block=12, seed=0):
    """Бутстреп разности Шарпов (стационарный, блок 12 мес) двух месячных рядов."""
    m = mo_a.notna() & mo_b.notna()
    a, b = mo_a[m].values, mo_b[m].values
    n = len(a)
    rng = np.random.default_rng(seed)

    def sh(x):
        return x.mean() / x.std() * np.sqrt(12) if x.std() > 0 else np.nan

    d0 = sh(a) - sh(b)
    ds = []
    for _ in range(nboot):
        idx = stat_boot_idx(n, block, rng)
        ds.append(sh(a[idx]) - sh(b[idx]))
    ds = np.array(ds)
    return dict(d_sharpe=d0, ci_lo=np.nanpercentile(ds, 2.5), ci_hi=np.nanpercentile(ds, 97.5),
                p_le0=float(np.mean(ds <= 0)))


def monthly_returns(bt):
    return bt["r"].groupby(bt.index.to_period("M")).sum()


# ------------------------------------------------------------------ правило панели
def panel_positions(df, M, mode="monthly"):
    """Текущее правило панели: лонг = ячейка не токсичная И знак композита (гистерезис ±0,1) = +1.
    mode='monthly' — оба слоя на последний торговый день месяца (позиция на следующий месяц);
    mode='daily_gate' — ворота ежедневно, знак композита по последнему закрытому месяцу."""
    sign_m = M["hyst"]
    if mode == "monthly":
        pos_m = ((M["cell"] != TOXIC) & M["cell"].notna() & (sign_m == 1)).astype(float)
        return pos_m.reindex(df.index).ffill().fillna(0.0)
    sign_d = sign_m.reindex(df.index).ffill().fillna(0.0)
    gate_d = ((df["cell"] != TOXIC) & df["cell"].notna()).astype(float)
    return (gate_d * (sign_d == 1)).astype(float)


def monthly_to_daily(pos_m, df):
    return pos_m.astype(float).reindex(df.index).ffill().fillna(0.0)


# ------------------------------------------------------------------ эпизоды и своевременность
def episodes(df, start="2004-01-01", min_dd=0.15, col="mcftr_ffill"):
    s = df.loc[start:, col].dropna()
    lp = np.log(s)
    dd = lp - lp.cummax()
    eps, i, n = [], 0, len(lp)
    idx = lp.index
    while i < n:
        if dd.iloc[i] < 0:
            j = i
            while j < n and dd.iloc[j] < 0:
                j += 1
            seg = dd.iloc[i:j]
            if seg.min() <= np.log(1 - min_dd):
                trough = seg.idxmin()
                eps.append(dict(peak=idx[i - 1], trough=trough, recovery=idx[j] if j < n else pd.NaT,
                                depth_pct=100 * (np.exp(seg.min()) - 1),
                                days_to_trough=int(idx.get_loc(trough) - (i - 1)),
                                days_to_recovery=int(j - idx.get_loc(trough)) if j < n else np.nan))
            i = j
        else:
            i += 1
    return pd.DataFrame(eps)


def timeliness(pos, df, eps, end="2026-08-31"):
    """Для каждого эпизода: через сколько торговых дней после пика правило вышло, какую долю падения
    избежало, через сколько дней после дна вошло, какую долю восстановления взяло."""
    p = pos.reindex(df.index).ffill().fillna(0.0)
    held = p.shift(1).fillna(0.0)
    r_s = held * df["r_tr"] + (1 - held) * df["rf"]
    rows = []
    for _, e in eps.iterrows():
        peak, trough = e["peak"], e["trough"]
        rec = e["recovery"] if pd.notna(e["recovery"]) else pd.Timestamp(end)
        ipk, itr, irc = df.index.get_loc(peak), df.index.get_loc(trough), df.index.get_loc(rec)
        seg_fall = slice(ipk + 1, itr + 1)
        seg_rise = slice(itr + 1, irc + 1)
        idx_fall, s_fall = df["r_tr"].iloc[seg_fall].sum(), r_s.iloc[seg_fall].sum()
        idx_rise, s_rise = df["r_tr"].iloc[seg_rise].sum(), r_s.iloc[seg_rise].sum()
        pos_peak = p.iloc[ipk]
        ex = p.iloc[ipk:itr + 1]
        exit_lag = np.nan
        if pos_peak == 1:
            z = ex[ex == 0]
            exit_lag = (df.index.get_loc(z.index[0]) - ipk) if len(z) else np.nan
        en = p.iloc[itr:irc + 1]
        z = en[en == 1]
        entry_lag = (df.index.get_loc(z.index[0]) - itr) if len(z) else np.nan
        # первый вход после ПИКА (мог быть до дна — «слишком рано»)
        first_in_after_peak = np.nan
        if pos_peak == 0 or np.isfinite(exit_lag):
            start_search = ipk if pos_peak == 0 else ipk + int(exit_lag)
            seq = p.iloc[start_search:irc + 1]
            zz = seq[seq == 1]
            if len(zz):
                first_in_after_peak = df.index.get_loc(zz.index[0]) - itr  # относительно дна (минус = до дна)
        rows.append(dict(peak=peak.date(), trough=trough.date(), depth_pct=e["depth_pct"],
                         pos_at_peak=int(pos_peak), exit_lag_days=exit_lag,
                         avoided_share=1 - s_fall / idx_fall if idx_fall < 0 else np.nan,
                         entry_lag_days=entry_lag, first_entry_vs_trough=first_in_after_peak,
                         captured_share=s_rise / idx_rise if idx_rise > 0 else np.nan,
                         idx_fall_pct=100 * (np.exp(idx_fall) - 1), strat_fall_pct=100 * (np.exp(s_fall) - 1),
                         idx_rise_pct=100 * (np.exp(idx_rise) - 1), strat_rise_pct=100 * (np.exp(s_rise) - 1),
                         time_in_mkt_fall=held.iloc[seg_fall].mean(), time_in_mkt_rise=held.iloc[seg_rise].mean()))
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ прочее
def expanding_pct(x, min_n=36):
    """Перцентиль x_t среди x_{<=t} (только прошлое + сегодня), NaN пока истории < min_n."""
    v = pd.Series(x).astype(float)
    out = pd.Series(np.nan, index=v.index)
    hist = []
    for i, (t, val) in enumerate(v.items()):
        if np.isfinite(val):
            hist.append(val)
            if len(hist) >= min_n:
                arr = np.asarray(hist)
                out.iloc[i] = (arr < val).mean() + 0.5 * (arr == val).mean()
    return out


def rolling_z(x, win=60, min_n=24, clip=3.0):
    v = pd.Series(x).astype(float)
    mu = v.rolling(win, min_periods=min_n).mean()
    sd = v.rolling(win, min_periods=min_n).std()
    return ((v - mu) / sd).clip(-clip, clip)


def save(df, name):
    p = RES / f"D3_{name}.csv"
    pd.DataFrame(df).to_csv(p, index=False, encoding="utf-8")
    print(f"[saved] {p.name}  ({len(df)} строк)")


def std_masks(M):
    idx = M.index
    return {
        "full_2004-2026": in_window(idx, "full_2004-2026"),
        "main_2010-2026": in_window(idx, "main_2010-2026"),
        "split_2004-2017": in_window(idx, "split_2004-2017"),
        "split_2018-2026": in_window(idx, "split_2018-2026"),
        "full_ex2022": in_window(idx, "full_2004-2026") & ex2022(idx),
        "pre2022_2004-2021": in_window(idx, ("2004-01-01", "2021-12-31")),
        "post2022_03-2026": in_window(idx, ("2022-03-01", "2026-08-31")),
    }


def ic_table(M, signals, horizons=(1, 3, 6), masks=None, nboot=0, target="fwd_tr_", extra=None):
    """Таблица IC: сигналы × срезы × горизонты. Знак сигнала подаётся уже «в сторону лонга»."""
    masks = masks if masks is not None else std_masks(M)
    rows = []
    for sname, s in signals.items():
        s = pd.Series(s).reindex(M.index)
        for mname, mask in masks.items():
            for h in horizons:
                r = ic_stats(s[mask], M[f"{target}{h}"][mask], h=h, nboot=nboot)
                row = dict(signal=sname, sample=mname, h=h, **r)
                if extra:
                    row.update(extra)
                rows.append(row)
    return pd.DataFrame(rows)


STRAT_WINDOWS = ("main_2010-2026", "full_2004-2026", "split_2004-2017", "split_2018-2026",
                 "era_2010-2021", "era_2022_03-2024", "era_2025-2026")


def evaluate_variants(df, M, variants, family, eps=None, windows=STRAT_WINDOWS, nboot=1000, cost=COST):
    """Метрики набора вариантов (дневные позиции) по окнам + ex-2022 + Δ Шарп к правилу панели
    (бутстреп на основном и полном окне) + своевременность по эпизодам."""
    pos_panel = panel_positions(df, M, "monthly")
    rows, tl = [], []
    for nm, pos in variants.items():
        for win in windows:
            bt, btp = backtest(df, pos, win, cost), backtest(df, pos_panel, win, cost)
            m = metrics(bt, nm)
            m.update(window=win, family=family, cost=cost)
            m["d_sharpe_vs_panel"] = m["sharpe"] - metrics(btp)["sharpe"]
            m["d_maxdd_vs_panel"] = m["maxdd_pct"] - metrics(btp)["maxdd_pct"]
            if win in ("main_2010-2026", "full_2004-2026") and nboot:
                sb = sharpe_diff_boot(monthly_returns(bt), monthly_returns(btp), nboot=nboot)
                m.update(d_sharpe_ci_lo=sb["ci_lo"], d_sharpe_ci_hi=sb["ci_hi"], d_sharpe_p_le0=sb["p_le0"])
            rows.append(m)
        bt = backtest(df, pos, "full_2004-2026", cost)
        btp = backtest(df, pos_panel, "full_2004-2026", cost)
        bt, btp = bt[bt.index.year != 2022], btp[btp.index.year != 2022]
        m = metrics(bt, nm)
        m.update(window="full_ex2022", family=family, cost=cost)
        m["d_sharpe_vs_panel"] = m["sharpe"] - metrics(btp)["sharpe"]
        m["d_maxdd_vs_panel"] = m["maxdd_pct"] - metrics(btp)["maxdd_pct"]
        rows.append(m)
        for c in (0.001, 0.003):
            m = metrics(backtest(df, pos, "main_2010-2026", c), nm)
            m.update(window="main_2010-2026", family=family, cost=c)
            m["d_sharpe_vs_panel"] = m["sharpe"] - metrics(backtest(df, pos_panel, "main_2010-2026", c))["sharpe"]
            rows.append(m)
        if eps is not None:
            t = timeliness(pos, df, eps)
            t.insert(0, "rule", nm)
            t.insert(0, "family", family)
            tl.append(t)
    R = pd.DataFrame(rows)
    save(R, f"strat_{family}")
    T = None
    if tl:
        T = pd.concat(tl)
        save(T, f"timeliness_{family}")
    return R, T


STRAT_COLS = ["window", "name", "cagr_pct", "sharpe", "sharpe_ex", "maxdd_pct", "time_in_mkt", "trades_per_yr",
              "beat_bh_years", "hit_months", "d_sharpe_vs_panel", "d_maxdd_vs_panel", "d_sharpe_p_le0"]


def show_strats(R, windows=("main_2010-2026", "full_2004-2026", "full_ex2022", "split_2004-2017", "split_2018-2026")):
    r = R[(R.cost == COST) & R.window.isin(windows)]
    print(fmt(r[[c for c in STRAT_COLS if c in r.columns]], 2))


def show_timeliness(T, rules=None):
    t = T if rules is None else T[T.rule.isin(rules)]
    cols = ["rule", "peak", "trough", "depth_pct", "pos_at_peak", "exit_lag_days", "avoided_share",
            "first_entry_vs_trough", "entry_lag_days", "captured_share", "time_in_mkt_rise"]
    print(fmt(t[cols], 2))


def fmt(df, nd=3):
    return df.to_string(float_format=lambda v: f"{v:.{nd}f}")
