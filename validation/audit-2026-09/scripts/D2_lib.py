"""D2_rates — общая библиотека: данные, производные ставочные ряды, бэктест long/flat,
метрики брифа, своевременность (правило 7), IC со стационарным бутстрепом.

Запуск скриптов D2_* — из каталога audit/ (пути считаются от расположения файла).
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

sys.stdout.reconfigure(encoding="utf-8")

AUDIT = Path(__file__).resolve().parents[1]
DATA = AUDIT / "data"
RES = AUDIT / "results"
RES.mkdir(exist_ok=True)

COST = 0.002          # за смену позиции (round-trip 0,4%)
TOXIC = "bear|stress|stress"

ERAS = {
    "main_2010_2026": ("2010-01-01", "2026-08-31"),
    "full_2004_2026": ("2004-01-01", "2026-08-31"),
    "era_2010_2021": ("2010-01-01", "2021-12-31"),
    "era_2022_2024": ("2022-03-01", "2024-12-31"),
    "era_2025_2026": ("2025-01-01", "2026-08-31"),
    "rates_2015_2026": ("2015-01-01", "2026-08-31"),
}


# ------------------------------------------------------------------ данные
def _raw_series(raw, name, idx=None, ffill_limit=5):
    s = raw[raw.series == name].set_index("date")["value"].sort_index()
    s = s[~s.index.duplicated(keep="last")]
    if idx is None:
        return s
    return s.reindex(idx.union(s.index)).ffill(limit=ffill_limit).reindex(idx)


def load_daily():
    """Дневная панель прод + деньги + ставочные ряды из raw_long (лаги как в проде)."""
    p = pd.read_csv(DATA / "panel_prod_daily.csv", parse_dates=["date"]).set_index("date").sort_index()
    c = pd.read_csv(DATA / "cash_and_tr.csv", parse_dates=["date"]).set_index("date").sort_index()
    p = p.join(c[["mm_rate", "rusfar3m", "mcftr_ffill", "rgbi_ffill"]], how="left")
    raw = pd.read_csv(DATA / "raw_long.csv", parse_dates=["date"])
    idx = p.index
    p["y05"] = _raw_series(raw, "zcyc_y0_5", idx)
    p["y5"] = _raw_series(raw, "zcyc_y5", idx)
    p["ig_yield"] = _raw_series(raw, "rucbcpns_yield", idx)
    p["hy_yield"] = _raw_series(raw, "rucbhycp_yield", idx)
    # ключевая по ДАТЕ РЕШЕНИЯ (в raw_long — по дате вступления в силу)
    dec = pd.read_csv(DATA / "cb_decisions.csv", parse_dates=["date"]).sort_values("date")
    kd = dec.set_index("date")["new_rate"]
    p["key_dec"] = kd.reindex(idx.union(kd.index)).ffill().reindex(idx)
    p.loc[p.index < kd.index[0], "key_dec"] = np.nan
    # месячный ИПЦ (SAAR3, yoy) с лагом публикации 13 дней после конца месяца
    cpi = pd.read_csv(DATA / "cpi_derived.csv")
    cpi["avail"] = pd.to_datetime(cpi["m"] + "-01") + pd.offsets.MonthEnd(0) + pd.Timedelta(days=13)
    for col in ["saar1", "saar3", "yoy"]:
        s = cpi.set_index("avail")[col]
        p[col] = s.reindex(idx.union(s.index)).ffill(limit=45).reindex(idx)
    # потоки ОРФР физлиц (месяц; доступно +15 дней после конца месяца)
    o = pd.read_csv(DATA / "orfr_flows.csv")
    o["avail"] = pd.to_datetime(o["month"] + "-01") + pd.offsets.MonthEnd(0) + pd.Timedelta(days=15)
    s = o.set_index("avail")["fiz"]
    p["orfr_fiz"] = s.reindex(idx.union(s.index)).ffill(limit=45).reindex(idx)
    return p


def derive_rates(p):
    """Производные ставочные ряды (все — по данным, доступным на закрытие дня)."""
    d = p.copy()
    k = d["key_rate"]
    # 1. цена ожиданий
    d["y05_key"] = d["y05"] - k
    d["y1_key"] = d["y1"] - k
    d["y2_key"] = d["y2"] - k
    d["rusfar_key"] = d["rusfar3m"] - k
    for c in ["y05_key", "y1_key", "y2_key", "rusfar_key"]:
        d[c + "_d21"] = d[c] - d[c].shift(21)
    # 2. кривая
    d["slope_10_1"] = d["y10"] - d["y1"]
    d["slope_5_1"] = d["y5"] - d["y1"]
    d["slope_10_5"] = d["y10"] - d["y5"]
    d["y10_key"] = d["y10"] - k
    d["erp"] = d["dy_trail"] - d["y10"]              # дивдоходность − 10Y
    d["d21_y10"] = d["y10"] - d["y10"].shift(21)
    d["d21_y2"] = d["y2"] - d["y2"].shift(21)
    d["d21_y1"] = d["y1"] - d["y1"].shift(21)
    d["d21_slope"] = d["slope_10_2"] - d["slope_10_2"].shift(21)
    # «хорошее» крутизнение: наклон вырос при падении короткого конца; «плохое»: за счёт роста длинного
    d["steep_good"] = ((d["d21_slope"] > 0) & (d["d21_y2"] < 0)).astype(float)
    d["steep_bad"] = ((d["d21_slope"] > 0) & (d["d21_y10"] > 0)).astype(float)
    d["long_end_up_bull"] = d["d21_y10"] - d["d21_y2"]   # = d21_slope (для справки)
    # 3. реальная ставка
    d["real_saar"] = k - d["saar3"]
    d["real_yoy"] = k - d["yoy"]
    d["real_saar_d21"] = d["real_saar"] - d["real_saar"].shift(21)
    d["real_saar_d63"] = d["real_saar"] - d["real_saar"].shift(63)
    d["real_saar_dd"] = d["real_saar_d21"] - d["real_saar_d21"].shift(21)   # вторая производная
    d["real_yoy_d63"] = d["real_yoy"] - d["real_yoy"].shift(63)
    d["key_d63"] = k - k.shift(63)
    d["key_d126"] = k - k.shift(126)
    # фаза ставки: по дате РЕШЕНИЯ (а не вступления в силу)
    kd = d["key_dec"]
    chg = kd.diff()
    ph = pd.Series(np.where(chg > 0, 1.0, np.where(chg < 0, -1.0, np.nan)), index=d.index)
    d["phase_dec"] = ph.ffill()
    # 4. кредитный стресс
    d["ig_spread"] = d["ig_yield"] - d["y2"]
    for c in ["hy_spread", "ig_spread"]:
        d[c + "_d21"] = d[c] - d[c].shift(21)
        d[c + "_d63"] = d[c] - d[c].shift(63)
        d[c + "_pct252"] = d[c].rolling(252, min_periods=120).rank(pct=True)
    rg = d["rgbi"]
    d["rgbi_ma50"] = rg.rolling(50, min_periods=40).mean()
    d["rgbi_ma100"] = rg.rolling(100, min_periods=80).mean()
    d["rgbi_ma200"] = rg.rolling(200, min_periods=160).mean()
    d["rgbi_vs_ma50"] = np.log(rg / d["rgbi_ma50"])
    d["rgbi_vs_ma100"] = np.log(rg / d["rgbi_ma100"])
    d["rgbi_vs_ma200"] = np.log(rg / d["rgbi_ma200"])
    d["rgbi_mom63"] = np.log(rg / rg.shift(63))
    d["rgbi_dd126"] = np.log(rg / rg.rolling(126, min_periods=60).max())
    # 6. депозитная альтернатива
    d["deposit_d21"] = d["deposit"] - d["deposit"].shift(21)
    d["deposit_d63"] = d["deposit"] - d["deposit"].shift(63)
    d["switch_d21"] = d["switch_spread"] - d["switch_spread"].shift(21)
    d["switch_d63"] = d["switch_spread"] - d["switch_spread"].shift(63)
    d["toxic"] = (d["cell"] == TOXIC).astype(float)
    d.loc[d["cell"].isna(), "toxic"] = np.nan
    return d


def month_ends(idx):
    """Индексы последних торговых дней месяца (как в ядре)."""
    s = pd.Series(np.arange(len(idx)), index=idx)
    return s.groupby([idx.year, idx.month]).last().values


def monthly_frame(d):
    """Месячный срез: значения на последний торговый день + форвардные доходности."""
    me = month_ends(d.index)
    m = d.iloc[me].copy()
    lm = np.log(m["mcftr_ffill"])
    li = np.log(m["imoex"])
    m["fwd1m_tr"] = lm.shift(-1) - lm
    m["fwd1m_px"] = li.shift(-1) - li
    m["fwd3m_tr"] = lm.shift(-3) - lm
    # кэш за следующий месяц: сумма дневных начислений mm_rate/252 по торговым дням
    daily_cash = (d["mm_rate"] / 100.0 / 252.0).fillna(0.0)
    cum = daily_cash.cumsum()
    cm = cum.iloc[me]
    m["fwd1m_cash"] = (cm.shift(-1) - cm).values
    m.loc[d["mm_rate"].iloc[me].isna().values, "fwd1m_cash"] = np.nan
    # композит панели
    pm = pd.read_csv(DATA / "panel_prod_monthly.csv", parse_dates=["date"]).set_index("date")
    m["composite"] = pm["composite"].reindex(m.index)
    m["comp_sign"] = hysteresis_sign(m["composite"].values, 0.10)
    return m


def hysteresis_sign(xs, thr=0.10):
    out = np.full(len(xs), np.nan)
    s = 0
    for i, v in enumerate(xs):
        if np.isfinite(v):
            if v > thr:
                s = 1
            elif v < -thr:
                s = -1
        out[i] = s if s else np.nan
    return out


def baseline_position(m):
    """Правило панели: лонг = ячейка не токсичная И знак композита (гистерезис) > 0."""
    gate = (m["cell"] != TOXIC) & m["cell"].notna()
    return ((m["comp_sign"] > 0) & gate).astype(float)


# ------------------------------------------------------------------ бэктест
def run_monthly(pos, m, cost=COST, start=None, end=None):
    """pos — 0/1 на месячном срезе t; доходность t→t+1. Возвращает DataFrame доходностей."""
    df = pd.DataFrame({"pos": pos.astype(float), "r_long": m["fwd1m_tr"], "r_cash": m["fwd1m_cash"]})
    df = df.dropna(subset=["r_long", "r_cash"])
    if start:
        df = df[df.index >= start]
    if end:
        df = df[df.index <= end]
    df["pos"] = df["pos"].fillna(0.0)
    sw = df["pos"].diff().abs().fillna(0.0)
    df["trade"] = sw
    df["ret"] = df["pos"] * df["r_long"] + (1 - df["pos"]) * df["r_cash"] - sw * cost
    return df


def metrics(df, freq=12, name=""):
    """Метрики брифа для помесячных (freq=12) или дневных (freq=252) лог-доходностей."""
    r = df["ret"].dropna()
    if len(r) < 6:
        return {}
    yrs = len(r) / freq
    cum = np.exp(r.cumsum())
    cagr = np.exp(r.sum() / yrs) - 1
    vol = r.std() * np.sqrt(freq)
    sharpe = r.mean() / r.std() * np.sqrt(freq) if r.std() > 0 else np.nan
    ex = (r - df.loc[r.index, "r_cash"])
    sharpe_ex = ex.mean() / ex.std() * np.sqrt(freq) if ex.std() > 0 else np.nan
    dd = (cum / cum.cummax() - 1).min()
    bh = df.loc[r.index, "r_long"]
    bh_cum = np.exp(bh.cumsum())
    bh_cagr = np.exp(bh.sum() / yrs) - 1
    bh_sh = bh.mean() / bh.std() * np.sqrt(freq) if bh.std() > 0 else np.nan
    bh_dd = (bh_cum / bh_cum.cummax() - 1).min()
    cash = df.loc[r.index, "r_cash"]
    cash_cagr = np.exp(cash.sum() / yrs) - 1
    # по годам: обгон b&h
    yr = pd.DataFrame({"s": r, "b": bh}).groupby(r.index.year).sum()
    beat = (yr["s"] > yr["b"]).mean()
    hit = (r > 0).mean()
    hit_vs_cash = (r > cash).mean()
    return dict(name=name, n=len(r), years=round(yrs, 2), cagr=cagr, vol=vol, sharpe=sharpe,
                sharpe_excess=sharpe_ex, maxdd=dd, time_in_mkt=df.loc[r.index, "pos"].mean(),
                trades_per_yr=df.loc[r.index, "trade"].sum() / yrs, beat_bh_years=beat,
                hit_rate=hit, hit_vs_cash=hit_vs_cash, bh_cagr=bh_cagr, bh_sharpe=bh_sh,
                bh_maxdd=bh_dd, cash_cagr=cash_cagr)


def sharpe_diff_bootstrap(ra, rb, n_boot=3000, block=9, seed=1):
    """Стационарный бутстреп разности Шарпов (месячные ряды, средний блок 9 мес).
    Возвращает (разность, p двусторонний, CI 5–95%)."""
    a, b = ra.values, rb.values
    n = len(a)
    if n < 24:
        return np.nan, np.nan, (np.nan, np.nan)
    rng = np.random.default_rng(seed)
    obs = _sh(a) - _sh(b)
    diffs = np.empty(n_boot)
    pr = 1.0 / block
    for i in range(n_boot):
        idx = np.empty(n, dtype=int)
        j = rng.integers(n)
        for t in range(n):
            if t == 0 or rng.random() < pr:
                j = rng.integers(n)
            else:
                j = (j + 1) % n
            idx[t] = j
        diffs[i] = _sh(a[idx]) - _sh(b[idx])
    p = 2 * min((diffs <= 0).mean(), (diffs >= 0).mean())
    return obs, p, (np.quantile(diffs, 0.05), np.quantile(diffs, 0.95))


def _sh(x, freq=12):
    s = x.std()
    return x.mean() / s * np.sqrt(freq) if s > 0 else 0.0


# ------------------------------------------------------------------ своевременность
def drawdown_episodes(price, min_dd=0.15):
    """Эпизоды просадок > min_dd по ряду цены: (пик, дно, дата восстановления пика|None)."""
    lp = np.log(price.dropna())
    peaks = lp.cummax()
    dd = lp - peaks
    eps = []
    i = 0
    idx = lp.index
    n = len(lp)
    while i < n:
        if dd.iloc[i] < -min_dd:
            # найти пик (последнее место, где dd==0 до i)
            j = i
            while j > 0 and dd.iloc[j] < 0:
                j -= 1
            peak = idx[j]
            # дно: минимум до восстановления
            k = i
            while k < n and dd.iloc[k] < 0:
                k += 1
            seg = dd.iloc[j:k]
            trough = seg.idxmin()
            rec = idx[k] if k < n else None
            eps.append(dict(peak=peak, trough=trough, recovery=rec,
                            depth=float(np.exp(seg.min()) - 1)))
            i = k
        else:
            i += 1
    return eps


def timeliness(pos_daily, price, cash_daily, eps):
    """Для каждой просадки: через сколько торг. дней после пика правило вышло, доля избегнутого
    падения, дни после дна до входа, доля пропущенного восстановления.
    pos_daily — дневная позиция (0/1, применяется к доходности следующего дня)."""
    lp = np.log(price)
    r = lp.diff()
    rows = []
    for e in eps:
        pk, tr, rc = e["peak"], e["trough"], e["recovery"]
        seg = pos_daily.loc[pk:tr]
        # доходность стратегии на отрезке пик→дно (позиция на закрытии t → доходность t+1)
        strat = (pos_daily.shift(1) * r + (1 - pos_daily.shift(1)) * cash_daily).loc[pk:tr].iloc[1:]
        fall = lp.loc[tr] - lp.loc[pk]
        avoided = 1 - strat.sum() / fall if fall < 0 else np.nan
        was_long = seg.iloc[0] == 1
        exit_day = None
        if was_long:
            z = seg[seg == 0]
            exit_day = z.index[0] if len(z) else None
        days_to_exit = (None if not was_long else
                        (int(price.loc[pk:exit_day].shape[0] - 1) if exit_day is not None else "не вышел"))
        # восстановление: от дна до даты, когда цена отыграла половину падения (или до полного восстановления)
        half = lp.loc[tr] + (-fall) * 0.5
        after = lp.loc[tr:]
        hr = after[after >= half]
        rec_date = hr.index[0] if len(hr) else after.index[-1]
        strat2 = (pos_daily.shift(1) * r + (1 - pos_daily.shift(1)) * cash_daily).loc[tr:rec_date].iloc[1:]
        gain = lp.loc[rec_date] - lp.loc[tr]
        missed = 1 - strat2.sum() / gain if gain > 0 else np.nan
        seg2 = pos_daily.loc[tr:]
        ent = seg2[seg2 == 1]
        entry_day = ent.index[0] if len(ent) else None
        days_to_entry = int(price.loc[tr:entry_day].shape[0] - 1) if entry_day is not None else "не вошёл"
        rows.append(dict(peak=pk.date(), trough=tr.date(), depth_pct=round(e["depth"] * 100, 1),
                         long_at_peak=bool(was_long), days_to_exit=days_to_exit,
                         fall_avoided_pct=round(avoided * 100, 0) if avoided == avoided else np.nan,
                         days_to_reentry=days_to_entry,
                         half_recovery_date=rec_date.date(),
                         recovery_missed_pct=round(missed * 100, 0) if missed == missed else np.nan))
    return pd.DataFrame(rows)


def monthly_to_daily_pos(pos_m, idx):
    """Месячная позиция (на срезе t) → дневная: действует со следующего дня после среза до следующего среза."""
    s = pos_m.reindex(idx)
    return s.ffill().fillna(0.0)


# ------------------------------------------------------------------ IC
def stationary_bootstrap_idx(n, block, rng):
    idx = np.empty(n, dtype=int)
    pr = 1.0 / block
    j = rng.integers(n)
    for t in range(n):
        if t == 0 or rng.random() < pr:
            j = rng.integers(n)
        else:
            j = (j + 1) % n
        idx[t] = j
    return idx


def ic_stats(sig, fwd, block=6, n_boot=2000, seed=3, min_n=24):
    """Спирмен IC на месячной выборке + стационарный бутстреп (CI, p) + Ньюи-Уэст t."""
    df = pd.DataFrame({"s": sig, "f": fwd}).dropna()
    n = len(df)
    if n < min_n:
        return dict(n=n, ic=np.nan, p_boot=np.nan, ci_lo=np.nan, ci_hi=np.nan, nw_t=np.nan)
    s, f = df["s"].values, df["f"].values
    if np.nanstd(s) == 0 or len(np.unique(s)) < 3 or np.nanstd(f) == 0:
        return dict(n=n, ic=np.nan, p_boot=np.nan, ci_lo=np.nan, ci_hi=np.nan, nw_t=np.nan)
    ic = stats.spearmanr(s, f)[0]
    rng = np.random.default_rng(seed)
    bs = np.empty(n_boot)
    for i in range(n_boot):
        ix = stationary_bootstrap_idx(n, block, rng)
        bs[i] = stats.spearmanr(s[ix], f[ix])[0]
    bs = bs[np.isfinite(bs)]
    if len(bs) < 100:
        return dict(n=n, ic=ic, p_boot=np.nan, ci_lo=np.nan, ci_hi=np.nan, nw_t=np.nan)
    p = 2 * min((bs <= 0).mean(), (bs >= 0).mean())
    # Ньюи-Уэст на рангах
    rs = stats.rankdata(s); rf = stats.rankdata(f)
    xs = (rs - rs.mean()) / rs.std(); ys = (rf - rf.mean()) / rf.std()
    beta = (xs * ys).mean()
    u = xs * (ys - beta * xs)
    L = min(block, n // 3)
    S = (u ** 2).sum()
    for l in range(1, L + 1):
        S += 2 * (1 - l / (L + 1)) * (u[:-l] * u[l:]).sum()
    t = beta / np.sqrt(S / n ** 2) if S > 0 else np.nan
    return dict(n=n, ic=ic, p_boot=p, ci_lo=np.quantile(bs, 0.05), ci_hi=np.quantile(bs, 0.95), nw_t=t)


def fmt_metrics_table(rows):
    df = pd.DataFrame(rows)
    for c in ["cagr", "vol", "maxdd", "time_in_mkt", "beat_bh_years", "hit_rate", "hit_vs_cash",
              "bh_cagr", "bh_maxdd", "cash_cagr"]:
        if c in df:
            df[c] = (df[c] * 100).round(1)
    for c in ["sharpe", "sharpe_excess", "bh_sharpe", "trades_per_yr"]:
        if c in df:
            df[c] = df[c].round(2)
    return df
