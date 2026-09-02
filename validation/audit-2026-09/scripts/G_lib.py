"""G_decision: библиотека — признаки панели, автомат позиции, бэктест, метрики, бутстреп, своевременность.

Запуск скриптов из каталога audit/. Все ряды — дневные, календарь IMOEX, значения уже с честными лагами
публикации (panel_prod_daily.csv собран кодом панели). Композит закрытого месяца берётся из
panel_prod_monthly.csv (побитово совпадает с эталоном исследования).
"""
import os
import sys
import math
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
RES = os.path.join(ROOT, "results")

# --- параметры модели панели (constants.py / states.py), НЕ менять
CORE_LEGS = [("usd_mom63", +1), ("slope_10_2", +1), ("urals_rub_gap", -1)]
Z_WINDOW, Z_MIN, Z_CLIP = 60, 24, 3.0
CORE_HYST = 0.10
SIGNAL_Z_WINDOW, SIGNAL_Z_MIN, VERDICT_Z = 252, 120, 0.5
ERA_POST22 = pd.Timestamp("2022-03-24")
TOXIC = (0, 1, 1)
WINDOWS = {(1, 1, 0), (0, 1, 0)}
SECOND_LAYER = [
    {"id": "mom63", "sign": +1, "when": {"vol": 1}, "alt_when": {"trend": 0}},
    {"id": "dd252", "sign": -1, "when": {"bond": 0}},
    {"id": "switch_spread", "sign": +1, "when": {"rate_phase": -1}},
    {"id": "rb_gap", "sign": -1, "when": {"vol": 1}, "alt_when": {"trend": 0}},
    {"id": "futoi_z120", "sign": -1, "when": {"trend": 1, "vol": 0}},
    {"id": "dy_trail", "sign": +1, "when": {"era": "post22"}},
    {"id": "rgbi_mom21", "sign": +1, "when": {"trend": 0}},
]
SIG_IDS = [s["id"] for s in SECOND_LAYER]

# --- окна бэктеста (бриф, правило 4)
WINDOWS_BT = {
    "main_2010_2026": ("2010-01-01", "2026-08-31"),
    "full_2004_2026": ("2004-01-01", "2026-08-31"),
    "era_2010_2021": ("2010-01-01", "2022-02-18"),
    "era_2022_2024": ("2022-03-24", "2024-12-31"),
    "era_2025_2026": ("2025-01-01", "2026-08-31"),
    "train_2004_2017": ("2004-01-01", "2017-12-31"),
    "test_2018_2026": ("2018-01-01", "2026-08-31"),
}


# ============================================================ данные и признаки
def load_raw():
    d = pd.read_csv(os.path.join(DATA, "panel_prod_daily.csv"), parse_dates=["date"]).set_index("date")
    c = pd.read_csv(os.path.join(DATA, "cash_and_tr.csv"), parse_dates=["date"]).set_index("date")
    m = pd.read_csv(os.path.join(DATA, "panel_prod_monthly.csv"), parse_dates=["date"]).set_index("date")
    return d, c, m


def hysteresis_sign(xs, thr):
    out = np.full(len(xs), np.nan)
    s = 0
    for i, v in enumerate(xs):
        if v == v:
            if v > thr:
                s = 1
            elif v < -thr:
                s = -1
        out[i] = s if s else np.nan
    return out


def daily_composite(d, m_closed):
    """Дневное значение композита: 59 закрытых месяцев + сегодняшнее значение ноги (как monthly_frame
    панели для незавершённого месяца). На последний торговый день месяца совпадает с monthly composite."""
    month_of_day = d.index.to_period("M")
    m_per = m_closed.index.to_period("M")
    out_sum = np.zeros(len(d))
    out_n = np.zeros(len(d))
    for leg, sgn in CORE_LEGS:
        raw_m = m_closed["raw_" + leg].values.astype(float)
        vals_d = d[leg].groupby(month_of_day).ffill().values.astype(float)
        z_d = np.full(len(d), np.nan)
        for per, idx in pd.Series(np.arange(len(d)), index=month_of_day).groupby(level=0):
            ii = idx.values
            k = int(np.searchsorted(m_per.asi8, per.ordinal))  # число закрытых месяцев с периодом < per
            prev = raw_m[max(0, k - (Z_WINDOW - 1)):k]
            prev = prev[~np.isnan(prev)]
            n_prev = len(prev)
            s1, s2 = prev.sum(), (prev ** 2).sum()
            v = vals_d[ii]
            ok = ~np.isnan(v)
            n = n_prev + ok
            mean = (s1 + np.where(ok, v, 0.0)) / np.maximum(n, 1)
            # выборочная дисперсия по двум проходам не нужна: точность достаточна на нормированных рядах
            ss = s2 + np.where(ok, v, 0.0) ** 2
            var = (ss - n * mean ** 2) / np.maximum(n - 1, 1)
            sd = np.sqrt(np.maximum(var, 0.0))
            z = (v - mean) / sd
            z = np.clip(z, -Z_CLIP, Z_CLIP)
            z[~ok | (n < Z_MIN) | (sd <= 0)] = np.nan
            z_d[ii] = z
        good = ~np.isnan(z_d)
        out_sum[good] += sgn * z_d[good]
        out_n[good] += 1
    comp = np.where(out_n > 0, out_sum / np.maximum(out_n, 1), np.nan)
    return pd.Series(comp, index=d.index)


def gate_ok(cond, bits_row):
    for k, want in (cond or {}).items():
        if k == "era":
            if want == "post22" and not bits_row["era_post22"]:
                return False
            continue
        v = bits_row[k]
        if v != v or int(v) != want:
            return False
    return True


def build_features(d, c, m):
    F = pd.DataFrame(index=d.index)
    F["imoex"] = d["imoex"]
    F["mcftr"] = c["mcftr_ffill"].reindex(d.index)
    F["mm_rate"] = c["mm_rate"].reindex(d.index)
    for b in ("st_trend", "st_vol", "st_bond", "st_rate"):
        F[b] = d[b]
    F["cell"] = d["cell"]
    F["era_post22"] = F.index >= ERA_POST22
    bits_ok = F[["st_trend", "st_vol", "st_bond"]].notna().all(axis=1)
    F["toxic"] = bits_ok & (F.st_trend == 0) & (F.st_vol == 1) & (F.st_bond == 1)
    F["gate"] = bits_ok & ~F["toxic"]
    F["window"] = bits_ok & (F.st_vol == 1) & (F.st_bond == 0)  # (1,1,0) и (0,1,0)
    F["n_ok"] = (F.st_trend == 1).astype(int) + (F.st_vol == 0).astype(int) + (F.st_bond == 0).astype(int)
    F.loc[~bits_ok, "n_ok"] = np.nan

    # --- композит закрытого месяца: строка monthly = последний торговый день месяца; закрыт, если в
    # дневном календаре есть более поздний день другого месяца
    last_month = d.index[-1].to_period("M")
    m_closed = m[m.index.to_period("M") < last_month].copy()
    comp_m = m_closed["composite"]
    F["comp_closed"] = comp_m.reindex(d.index).ffill()
    F["comp_closed_date"] = pd.Series(comp_m.index, index=comp_m.index).reindex(d.index).ffill()
    sign_m = pd.Series(hysteresis_sign(comp_m.values, CORE_HYST), index=comp_m.index)
    F["sign_closed"] = sign_m.reindex(d.index).ffill()
    F["comp_daily"] = daily_composite(d, m_closed)

    # --- расстояния до переключения битов
    F["trend_gap"] = d["imoex"] / d["ma200"] - 1.0
    F["vol_gap"] = d["realized_vol_21"] - d["vol_thresh80"]
    F["bond_gap"] = d["rgbi_dd"] - (-0.04)

    # --- второй ряд: значение, z252 (min 120, без обрезки — как zscore_last), активность по состоянию
    bits_df = pd.DataFrame({"trend": F.st_trend, "vol": F.st_vol, "bond": F.st_bond,
                            "rate_phase": F.st_rate, "era_post22": F.era_post22})
    n_for, n_against, vote_sum, n_vote = np.zeros(len(F)), np.zeros(len(F)), np.zeros(len(F)), np.zeros(len(F))
    for sig in SECOND_LAYER:
        sid = sig["id"]
        x = d[sid].astype(float)
        z = (x - x.rolling(SIGNAL_Z_WINDOW, min_periods=SIGNAL_Z_MIN).mean()) / \
            x.rolling(SIGNAL_Z_WINDOW, min_periods=SIGNAL_Z_MIN).std()
        z = z.where(x.notna()).ffill()
        F[f"{sid}_val"] = x.ffill()
        F[f"{sid}_z"] = z
        act = np.zeros(len(F), dtype=bool)
        for k, cond in enumerate(("when", "alt_when")):
            cond = sig.get(cond)
            if not cond:
                continue
            ok = np.ones(len(F), dtype=bool)
            for key, want in cond.items():
                if key == "era":
                    ok &= F.era_post22.values
                else:
                    ok &= (bits_df[key].values == want)
            act |= ok
        F[f"{sid}_active"] = act
        contrib = sig["sign"] * z.values
        F[f"{sid}_contrib"] = np.where(act, contrib, np.nan)
        cz = np.clip(contrib, -3, 3)
        good = act & ~np.isnan(contrib)
        vote_sum[good] += cz[good]
        n_vote[good] += 1
        n_for[good & (contrib > VERDICT_Z)] += 1
        n_against[good & (contrib < -VERDICT_Z)] += 1
    F["n_for"], F["n_against"], F["vote_sum"], F["n_vote"] = n_for, n_against, vote_sum, n_vote
    F["vote_avg"] = np.where(n_vote > 0, vote_sum / np.maximum(n_vote, 1), 0.0)
    F["n_active"] = F[[f"{s}_active" for s in SIG_IDS]].sum(axis=1)
    return F


def month_end_mask(F):
    """True на последний торговый день каждого месяца (для варианта «решать раз в месяц»)."""
    per = F.index.to_period("M")
    nxt = np.append(per[1:].asi8, -1)
    return per.asi8 != nxt


def recompute_votes(F, signs, futoi_shift=0):
    """Голос второго ряда при других знаках (плацебо) и/или доп. лаге futoi (в торговых днях)."""
    n_for, n_against, vote_sum, n_vote = np.zeros(len(F)), np.zeros(len(F)), np.zeros(len(F)), np.zeros(len(F))
    for sig, sgn in zip(SECOND_LAYER, signs):
        sid = sig["id"]
        z = F[f"{sid}_z"]
        if sid == "futoi_z120" and futoi_shift:
            z = z.shift(futoi_shift)
        act = F[f"{sid}_active"].values
        contrib = sgn * z.values
        cz = np.clip(contrib, -3, 3)
        good = act & ~np.isnan(contrib)
        vote_sum[good] += cz[good]
        n_vote[good] += 1
        n_for[good & (contrib > VERDICT_Z)] += 1
        n_against[good & (contrib < -VERDICT_Z)] += 1
    vote_avg = np.where(n_vote > 0, vote_sum / np.maximum(n_vote, 1), 0.0)
    return {"n_for": n_for, "n_against": n_against, "vote_sum": vote_sum, "n_vote": n_vote, "vote_avg": vote_avg}


# ============================================================ автомат и бэктест
def automaton(buy, sell, min_hold=0, start_state=0):
    """BUY→LONG→SELL→FLAT. buy/sell — булевы массивы решений на закрытии дня t. Возвращает pos[t]∈{0,1}."""
    n = len(buy)
    pos = np.zeros(n)
    state = start_state
    last = -10 ** 9
    for t in range(n):
        if state == 0:
            if buy[t] and t - last >= min_hold:
                state, last = 1, t
        else:
            if sell[t] and t - last >= min_hold:
                state, last = 0, t
        pos[t] = state
    return pos


def automaton_frac(target, min_hold=0):
    n = len(target)
    pos = np.zeros(n)
    state, last = 0.0, -10 ** 9
    for t in range(n):
        tv = target[t]
        if tv == tv and tv != state and t - last >= min_hold:
            state, last = tv, t
        pos[t] = state
    return pos


def backtest(pos, F, cost=0.002, lag=1):
    """Сигнал на закрытии t, исполнение на закрытии t+lag; доходность дня s использует held[s]=pos[s-1-lag].
    Лонг = MCFTR, флэт = mm_rate/252 (ставка на день решения). Издержки cost×|Δheld|."""
    r_eq = F["mcftr"].pct_change().values
    r_cash = (F["mm_rate"].shift(1).values / 100.0) / 252.0
    held = np.full(len(pos), np.nan)
    k = 1 + lag
    held[k:] = pos[:-k]
    held = np.where(np.isnan(held), 0.0, held)
    dpos = np.abs(np.diff(held, prepend=0.0))
    ret = held * np.nan_to_num(r_eq) + (1 - held) * np.nan_to_num(r_cash) - cost * dpos
    ret = np.where(np.isnan(r_eq) & (held > 0), np.nan, ret)
    return pd.DataFrame({"ret": ret, "held": held, "r_eq": r_eq, "r_cash": r_cash}, index=F.index)


def bh_frame(F):
    r_eq = F["mcftr"].pct_change().values
    r_cash = (F["mm_rate"].shift(1).values / 100.0) / 252.0
    return pd.DataFrame({"ret": r_eq, "held": 1.0, "r_eq": r_eq, "r_cash": r_cash}, index=F.index), \
        pd.DataFrame({"ret": r_cash, "held": 0.0, "r_eq": r_eq, "r_cash": r_cash}, index=F.index)


def monthly_returns(bt):
    return (1 + bt["ret"].fillna(0)).resample("ME").prod() - 1


def metrics(bt, bh, cash, start, end):
    sub = bt.loc[start:end].copy()
    sub = sub[sub["ret"].notna() & sub["r_eq"].notna()]
    if len(sub) < 60:
        return None
    b = bh.loc[sub.index]
    ca = cash.loc[sub.index]
    eq = (1 + sub["ret"]).cumprod()
    years = (sub.index[-1] - sub.index[0]).days / 365.25
    mr = monthly_returns(sub)
    mb = monthly_returns(b)
    mc = monthly_returns(ca)
    n_m = len(mr)
    cagr = eq.iloc[-1] ** (1 / years) - 1 if years > 0 else np.nan
    vol = mr.std() * math.sqrt(12)
    sharpe = mr.mean() / mr.std() * math.sqrt(12) if mr.std() > 0 else np.nan
    ex = mr - mc
    sharpe_ex = ex.mean() / ex.std() * math.sqrt(12) if ex.std() > 0 else np.nan
    dd = (eq / eq.cummax() - 1).min()
    eq_b = (1 + b["ret"]).cumprod()
    dd_b = (eq_b / eq_b.cummax() - 1).min()
    tim = sub["held"].mean()
    switches = int((np.abs(np.diff(sub["held"].values)) > 1e-9).sum())
    yr_s = (1 + sub["ret"]).groupby(sub.index.year).prod()
    yr_b = (1 + b["ret"]).groupby(b.index.year).prod()
    beat_years = float((yr_s > yr_b).mean())
    return {
        "start": sub.index[0].date().isoformat(), "end": sub.index[-1].date().isoformat(),
        "n_months": n_m, "cagr": cagr, "vol": vol, "sharpe": sharpe, "sharpe_ex_cash": sharpe_ex,
        "maxdd": dd, "bh_cagr": (eq_b.iloc[-1] ** (1 / years) - 1) if years > 0 else np.nan,
        "bh_sharpe": mb.mean() / mb.std() * math.sqrt(12) if mb.std() > 0 else np.nan,
        "bh_maxdd": dd_b, "time_in_mkt": tim, "switches_per_year": switches / years if years > 0 else np.nan,
        "beat_bh_years": beat_years, "hit_months_pos": float((mr > 0).mean()),
        "hit_months_vs_bh": float((mr >= mb).mean()),
        "cash_cagr": ((1 + ca["ret"]).prod() ** (1 / years) - 1) if years > 0 else np.nan,
    }


# ============================================================ бутстреп разности Шарпа
def stationary_bootstrap_idx(n, mean_block, rng):
    p = 1.0 / mean_block
    idx = np.empty(n, dtype=int)
    i = rng.integers(0, n)
    for t in range(n):
        if t == 0 or rng.random() < p:
            i = rng.integers(0, n)
        else:
            i = (i + 1) % n
        idx[t] = i
    return idx


def sharpe_diff_bootstrap(mr_a, mr_b, n_boot=2000, mean_block=8, seed=7):
    """Стационарный бутстреп месячных пар: распределение Sharpe(a)−Sharpe(b)."""
    df = pd.concat([mr_a.rename("a"), mr_b.rename("b")], axis=1).dropna()
    a, b = df["a"].values, df["b"].values
    n = len(a)
    if n < 24:
        return {"n": n}
    rng = np.random.default_rng(seed)

    def sh(x):
        s = x.std(ddof=1)
        return x.mean() / s * math.sqrt(12) if s > 0 else 0.0

    obs = sh(a) - sh(b)
    diffs = np.empty(n_boot)
    for k in range(n_boot):
        ix = stationary_bootstrap_idx(n, mean_block, rng)
        diffs[k] = sh(a[ix]) - sh(b[ix])
    return {"n": n, "delta_sharpe": obs, "ci05": float(np.percentile(diffs, 5)),
            "ci95": float(np.percentile(diffs, 95)), "p_le0": float((diffs <= 0).mean())}


# ============================================================ своевременность (правило 7)
def find_episodes(px, thresh=0.15):
    px = px.dropna()
    v = px.values
    runmax = np.maximum.accumulate(v)
    dd = v / runmax - 1
    eps = []
    i, in_ep, peak = 0, False, None
    while i < len(v):
        if not in_ep and dd[i] < -thresh:
            peak = int(np.where(v[:i + 1] == runmax[i])[0][-1])
            in_ep = True
        if in_ep and dd[i] >= 0:
            seg = v[peak:i + 1]
            trough = peak + int(np.argmin(seg))
            eps.append((px.index[peak], px.index[trough], px.index[i]))
            in_ep = False
        i += 1
    if in_ep:
        seg = v[peak:]
        trough = peak + int(np.argmin(seg))
        eps.append((px.index[peak], px.index[trough], None))
    return eps


def timeliness(bt, F, episodes):
    """Для каждого эпизода: когда правило вышло/вошло относительно пика/дна, доля падения избегнута,
    доля роста пропущена (по лог-доходностям стратегии против b&h)."""
    idx = bt.index
    held = bt["held"].values
    lr_s = np.log1p(bt["ret"].fillna(0).values)
    lr_b = np.log1p(bt["r_eq"].fillna(0).values)
    pos = {d: i for i, d in enumerate(idx)}
    rows = []
    for peak, trough, rec in episodes:
        p, t = pos[peak], pos[trough]
        e = pos[rec] if rec is not None else min(t + 252, len(idx) - 1)
        # выход
        if held[p] < 0.5:
            j = p
            while j > 0 and held[j] < 0.5:
                j -= 1
            exit_rel = -(p - j)  # ушёл в деньги за столько дней ДО пика
        else:
            k = np.where(held[p:t + 1] < 0.5)[0]
            exit_rel = int(k[0]) if len(k) else None
        fall_bh = lr_b[p + 1:t + 1].sum()
        fall_s = lr_s[p + 1:t + 1].sum()
        avoided = 1 - fall_s / fall_bh if fall_bh < 0 else np.nan
        # вход
        if held[t] > 0.5:
            reentry = 0
        else:
            k = np.where(held[t:e + 1] > 0.5)[0]
            reentry = int(k[0]) if len(k) else None
        rise_bh = lr_b[t + 1:e + 1].sum()
        rise_s = lr_s[t + 1:e + 1].sum()
        missed = 1 - rise_s / rise_bh if rise_bh > 0 else np.nan
        rows.append({"peak": peak.date().isoformat(), "trough": trough.date().isoformat(),
                     "recovery": rec.date().isoformat() if rec is not None else "не восстановился",
                     "bh_fall_pct": round((math.exp(fall_bh) - 1) * 100, 1),
                     "strat_fall_pct": round((math.exp(fall_s) - 1) * 100, 1),
                     "exit_days_after_peak": exit_rel, "share_fall_avoided": round(avoided, 2) if avoided == avoided else None,
                     "reentry_days_after_trough": reentry,
                     "bh_rise_pct": round((math.exp(rise_bh) - 1) * 100, 1),
                     "strat_rise_pct": round((math.exp(rise_s) - 1) * 100, 1),
                     "share_rise_missed": round(missed, 2) if missed == missed else None})
    return rows
