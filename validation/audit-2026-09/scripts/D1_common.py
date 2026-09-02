"""D1_positioning — общий модуль: данные, правило панели, бэктест, метрики, IC, своевременность.

Запуск скриптов из каталога audit/: python scripts/D1_*.py
Соглашения:
  * позиция на закрытии дня t применяется к доходности t -> t+1 (никакого заглядывания);
  * месячное решение: значение на последний торговый день месяца -> позиция на весь следующий месяц;
  * лонг = MCFTR (полная доходность), флэт = mm_rate/252 в день; издержки 0,2% за смену позиции;
  * IC — Спирмен на месячной невырожденной выборке + Ньюи-Уэст + стационарный бутстреп.
"""
import sys
import numpy as np
import pandas as pd
from scipy import stats

sys.stdout.reconfigure(encoding="utf-8")
D = "data"
TOXIC = "bear|stress|stress"
COST = 0.002
HYST = 0.10


# ----------------------------------------------------------------------------- данные
def load_daily():
    p = pd.read_csv(f"{D}/panel_prod_daily.csv", parse_dates=["date"]).set_index("date")
    c = pd.read_csv(f"{D}/cash_and_tr.csv", parse_dates=["date"]).set_index("date")
    p = p.join(c[["mm_rate", "mcftr_ffill", "rusfar3m"]])
    p["tr"] = np.log(p["mcftr_ffill"]).diff()          # дневная лог-доходность MCFTR
    p["px"] = np.log(p["imoex"]).diff()                 # ценовая (для справки)
    p["mm_d"] = p["mm_rate"].shift(1) / 100 / 252       # ставка, известная на закрытии t-1
    # заполняем пустые дни mm ставкой ffill (декады)
    p["mm_d"] = p["mm_d"].ffill()
    p["gate"] = (p["cell"] != TOXIC) & p["cell"].notna()
    return p


def load_monthly():
    m = pd.read_csv(f"{D}/panel_prod_monthly.csv", parse_dates=["date"]).set_index("date")
    # выкидываем незакрытый срез (последняя строка = сегодня, не конец месяца)
    m = m[m.index.to_period("M") != m.index[-1].to_period("M")] if m.index[-1].day < 20 else m
    return m


def load_raw():
    r = pd.read_csv(f"{D}/raw_long.csv", parse_dates=["date"])
    return r


def raw_series(r, name):
    s = r[r.series == name].set_index("date")["value"].sort_index()
    return s[~s.index.duplicated()]


def month_ends(idx):
    """Последний торговый день каждого месяца по дневному индексу."""
    s = pd.Series(idx, index=idx)
    return s.groupby(idx.to_period("M")).last().values


# --------------------------------------------------------------------- правило панели
def hysteresis_sign(comp, h=HYST):
    out = np.zeros(len(comp))
    s = 0
    for i, v in enumerate(comp):
        if np.isnan(v):
            out[i] = np.nan if s == 0 else s
            continue
        if s == 0:
            s = 1 if v > 0 else -1
        elif v > h:
            s = 1
        elif v < -h:
            s = -1
        out[i] = s
    return pd.Series(out, index=comp.index)


def panel_rule_monthly(m):
    """Позиция на следующий месяц по решению на конце месяца: ворота И знак композита (гистерезис)."""
    slope = hysteresis_sign(m["composite"])
    gate = m["cell"] != TOXIC
    pos = (gate & (slope > 0)).astype(float)
    pos[m["composite"].isna() & m["cell"].isna()] = np.nan
    return pos, gate.astype(float), (slope > 0).astype(float)


def to_daily_position(pos_m, daily_index):
    """Решение на конце месяца t -> позиция на закрытии всех дней (t, next_t]. Возвращает позицию
    'на закрытии дня d' (ещё не сдвинутую)."""
    s = pos_m.reindex(daily_index).ffill()
    return s


def panel_rule_daily_gate(p, m):
    """Вариант: ворота считаются ежедневно, наклон — по последнему закрытому месяцу."""
    slope = hysteresis_sign(m["composite"])
    slope_d = slope.reindex(p.index).ffill()
    pos = (p["gate"] & (slope_d > 0)).astype(float)
    pos[slope_d.isna()] = np.nan
    return pos


# --------------------------------------------------------------------------- бэктест
def backtest(p, pos_close, start, end, cost=COST):
    """pos_close — позиция (0/1) на закрытии дня d (решение уже принято). Доходность дня d+1
    = pos_d * tr_{d+1} + (1-pos_d) * mm_d - cost*|pos_d - pos_{d-1}| (издержка списывается
    в день смены). Возвращает DataFrame дневных доходностей стратегии, b&h и кэша."""
    sub = p.loc[start:end]
    pos = pos_close.reindex(sub.index).ffill().fillna(0.0)
    prev = pos.shift(1).fillna(pos.iloc[0])
    r_tr = sub["tr"].fillna(0.0)
    r_mm = sub["mm_d"].fillna(0.0)
    strat = prev * r_tr + (1 - prev) * r_mm - cost * (pos - prev).abs()
    return pd.DataFrame({"strat": strat, "bh": r_tr, "cash": r_mm, "pos": prev}, index=sub.index)


def metrics(bt, col="strat"):
    r = bt[col]
    n = len(r)
    yrs = n / 252
    cum = r.cumsum()
    cagr = np.exp(cum.iloc[-1] / yrs) - 1 if yrs > 0 else np.nan
    vol = r.std() * np.sqrt(252)
    sharpe = r.mean() / r.std() * np.sqrt(252) if r.std() > 0 else np.nan
    ex = r - bt["cash"]
    sharpe_ex = ex.mean() / ex.std() * np.sqrt(252) if ex.std() > 0 else np.nan
    eq = np.exp(cum)
    dd = (eq / eq.cummax() - 1).min()
    tim = bt["pos"].mean() if col == "strat" else (1.0 if col == "bh" else 0.0)
    trades = (bt["pos"].diff().abs().sum()) / yrs if col == "strat" else 0.0
    # по годам / месяцам
    ry = r.groupby(r.index.year).sum()
    by = bt["bh"].groupby(bt.index.year).sum()
    beat_y = (ry > by).mean()
    rm = r.groupby(r.index.to_period("M")).sum()
    bm = bt["bh"].groupby(bt.index.to_period("M")).sum()
    hit_m = (rm > 0).mean()
    beat_m = (rm > bm).mean()
    return dict(cagr=cagr, vol=vol, sharpe=sharpe, sharpe_ex=sharpe_ex, maxdd=dd, tim=tim,
                trades_yr=trades, beat_bh_years=beat_y, hit_m=hit_m, beat_bh_m=beat_m, n_days=n)


def fmt_metrics(d):
    return (f"CAGR {d['cagr']*100:+.1f}% vol {d['vol']*100:.1f}% Sh {d['sharpe']:.2f} "
            f"Sh_ex {d['sharpe_ex']:.2f} MDD {d['maxdd']*100:.1f}% in {d['tim']*100:.0f}% "
            f"tr/yr {d['trades_yr']:.1f} beatY {d['beat_bh_years']:.2f} hitM {d['hit_m']:.2f}")


# ------------------------------------------------------ бутстреп разности Шарпов
def stationary_bootstrap_idx(n, avg_block, rng):
    """Politis–Romano: индексы длины n со средней длиной блока avg_block."""
    p = 1.0 / avg_block
    idx = np.empty(n, dtype=int)
    idx[0] = rng.integers(n)
    for i in range(1, n):
        if rng.random() < p:
            idx[i] = rng.integers(n)
        else:
            idx[i] = (idx[i - 1] + 1) % n
    return idx


def sharpe_diff_boot(bt_a, bt_b, n_boot=2000, block=9, seed=11):
    """Разность Шарпов (a − b) на месячных доходностях, стационарный бутстреп парных месяцев."""
    ra = bt_a["strat"].groupby(bt_a.index.to_period("M")).sum()
    rb = bt_b["strat"].groupby(bt_b.index.to_period("M")).sum()
    df = pd.concat([ra, rb], axis=1, keys=["a", "b"]).dropna()
    a, b = df["a"].values, df["b"].values
    n = len(a)

    def sh(x):
        return x.mean() / x.std() * np.sqrt(12) if x.std() > 0 else 0.0

    obs = sh(a) - sh(b)
    rng = np.random.default_rng(seed)
    boots = np.empty(n_boot)
    for k in range(n_boot):
        ix = stationary_bootstrap_idx(n, block, rng)
        boots[k] = sh(a[ix]) - sh(b[ix])
    ci = np.percentile(boots, [5, 95])
    p_le0 = (boots <= 0).mean()
    return obs, ci[0], ci[1], p_le0, n


# ------------------------------------------------------------------ своевременность
def drawdown_episodes(tr_log, min_depth=0.15):
    """Эпизоды просадок b&h (MCFTR) глубже min_depth: (peak_date, trough_date, depth, recov_date)."""
    eq = np.exp(tr_log.cumsum())
    peak = eq.cummax()
    dd = eq / peak - 1
    eps = []
    in_dd = False
    for i, d in enumerate(dd.index):
        if not in_dd and dd.iloc[i] < 0:
            in_dd = True
            start = i
            pk_val = peak.iloc[i]
        if in_dd and (dd.iloc[i] == 0 or i == len(dd) - 1):
            seg = dd.iloc[start:i + 1]
            depth = seg.min()
            if depth <= -min_depth:
                tr_i = start + int(np.argmin(seg.values))
                pk_i = start - 1 if start > 0 else 0
                eps.append((dd.index[pk_i], dd.index[tr_i], depth, d))
            in_dd = False
    return eps


def timeliness(bt, min_depth=0.15, recov_days=252):
    """Для каждой просадки b&h >15%: через сколько торговых дней после пика правило вышло
    в флэт (если было в лонге), доля падения, которой избежали, через сколько дней после дна
    вошло, доля восстановления (дно -> min(прежний пик, дно+252д)), которую пропустили."""
    tr = bt["bh"]
    pos = bt["pos"]
    rows = []
    idx = tr.index
    eq = np.exp(tr.cumsum())
    for pk, trg, depth, rec in drawdown_episodes(tr, min_depth):
        i_pk, i_tr = idx.get_loc(pk), idx.get_loc(trg)
        # выход
        p_at_peak = pos.iloc[i_pk + 1] if i_pk + 1 < len(pos) else np.nan
        seg = pos.iloc[i_pk + 1:i_tr + 1]
        exit_day = None
        if p_at_peak == 1:
            z = np.where(seg.values == 0)[0]
            exit_day = int(z[0]) if len(z) else None
        fall_bh = tr.iloc[i_pk + 1:i_tr + 1].sum()
        fall_st = bt["strat"].iloc[i_pk + 1:i_tr + 1].sum()
        avoided = 1 - fall_st / fall_bh if fall_bh != 0 else np.nan
        # восстановление
        # конец ноги: первый день после дна, когда eq >= eq на пике, либо дно+recov_days
        target = eq.iloc[i_pk]
        j_end = min(i_tr + recov_days, len(idx) - 1)
        after = eq.iloc[i_tr + 1:j_end + 1]
        hit = np.where(after.values >= target)[0]
        if len(hit):
            j_end = i_tr + 1 + int(hit[0])
        seg2 = pos.iloc[i_tr + 1:j_end + 1]
        o = np.where(seg2.values == 1)[0]
        entry_day = int(o[0]) if len(o) else None
        p_at_trough = pos.iloc[i_tr + 1] if i_tr + 1 < len(pos) else np.nan
        rise_bh = tr.iloc[i_tr + 1:j_end + 1].sum()
        rise_st = bt["strat"].iloc[i_tr + 1:j_end + 1].sum()
        missed = 1 - rise_st / rise_bh if rise_bh != 0 else np.nan
        rows.append(dict(peak=pk.date(), trough=trg.date(), depth=round(depth * 100, 1),
                         long_at_peak=int(p_at_peak) if p_at_peak == p_at_peak else None,
                         exit_days_after_peak=exit_day, fall_avoided=round(avoided, 2),
                         long_at_trough=int(p_at_trough) if p_at_trough == p_at_trough else None,
                         entry_days_after_trough=entry_day, rise_missed=round(missed, 2),
                         leg_end=idx[j_end].date()))
    return pd.DataFrame(rows)


# ------------------------------------------------------------------------------ IC
def nw_t(x, y, lag):
    m = np.isfinite(x) & np.isfinite(y)
    x, y = x[m], y[m]
    n = len(x)
    if n < 12 or x.std() == 0 or y.std() == 0:
        return np.nan
    xs = stats.rankdata(x); ys = stats.rankdata(y)
    xs = (xs - xs.mean()) / xs.std(); ys = (ys - ys.mean()) / ys.std()
    beta = (xs * ys).mean()
    u = xs * (ys - beta * xs)
    L = min(lag, n // 3)
    s = (u ** 2).sum()
    for l in range(1, L + 1):
        s += 2 * (1 - l / (L + 1)) * (u[:-l] * u[l:]).sum()
    var = s / n ** 2
    return beta / np.sqrt(var) if var > 0 else np.nan


def ic_boot_p(x, y, n_boot=2000, block=6, seed=3):
    """p-value IC при нуле: стационарный бутстреп СИГНАЛА (сохраняет его автокорреляцию)
    при фиксированном форварде -> matched-null."""
    m = np.isfinite(x) & np.isfinite(y)
    x, y = x[m], y[m]
    n = len(x)
    if n < 12:
        return np.nan
    obs = stats.spearmanr(x, y)[0]
    rng = np.random.default_rng(seed)
    null = np.empty(n_boot)
    for k in range(n_boot):
        ix = stationary_bootstrap_idx(n, block, rng)
        null[k] = stats.spearmanr(x[ix], y)[0]
    return float((np.abs(null - np.nanmean(null)) >= abs(obs - np.nanmean(null))).mean())


def ic_row(sig, fwd, H, label, extra=None):
    """Спирмен IC, NW-t, бутстреп-p и терцильный спред для одной выборки."""
    df = pd.concat([sig.rename("s"), fwd.rename("f")], axis=1).dropna()
    n = len(df)
    row = dict(signal=label, H=H, n=n)
    if extra:
        row.update(extra)
    if n < 12 or df.s.nunique() < 3:
        row.update(ic=np.nan, p_sp=np.nan, nw_t=np.nan, p_boot=np.nan, terc_hi=np.nan, terc_lo=np.nan)
        return row
    ic, p = stats.spearmanr(df.s, df.f)
    lag = max(int(np.ceil(H / 21)) - 1, 0)
    t = nw_t(df.s.values, df.f.values, lag)
    pb = ic_boot_p(df.s.values, df.f.values, block=max(3, lag * 2 + 3))
    q1, q2 = df.s.quantile([1 / 3, 2 / 3])
    hi, lo = df.f[df.s >= q2].mean(), df.f[df.s <= q1].mean()
    row.update(ic=round(ic, 3), p_sp=round(p, 3), nw_t=round(t, 2), p_boot=round(pb, 3),
               terc_hi=round(hi * 100, 2), terc_lo=round(lo * 100, 2))
    return row


def fwd_returns(p, H):
    """Форвардная лог-доходность MCFTR за H торговых дней от закрытия d."""
    lt = np.log(p["mcftr_ffill"])
    return (lt.shift(-H) - lt)


def ic_battery(p, sig, label, splits, horizons=(21, 63)):
    """IC на месячных срезах (последний торговый день месяца) по разрезам.
    splits: dict name -> boolean mask (по дневному индексу)."""
    me = month_ends(p.index)
    rows = []
    for H in horizons:
        fwd = fwd_returns(p, H)
        for name, mask in splits.items():
            sel = pd.Index(me)
            sel = sel[mask.reindex(sel).fillna(False).values.astype(bool)]
            rows.append(ic_row(sig.reindex(sel), fwd.reindex(sel), H, label, {"split": name}))
    return rows


def state_splits(p, start, end):
    base = (p.index >= start) & (p.index <= end)
    base = pd.Series(base, index=p.index)
    return {
        "all": base,
        "bull": base & (p["st_trend"] == 1),
        "bear": base & (p["st_trend"] == 0),
        "calm": base & (p["st_vol"] == 0),
        "stress": base & (p["st_vol"] == 1),
        "toxic": base & (p["cell"] == TOXIC),
        "gate_open": base & (p["cell"] != TOXIC) & p["cell"].notna(),
    }


def zscore(s, w, minp=None):
    minp = minp or max(w // 2, 10)
    return (s - s.rolling(w, min_periods=minp).mean()) / s.rolling(w, min_periods=minp).std()


def pct_rank(s, w, minp=None):
    minp = minp or max(w // 2, 10)
    return s.rolling(w, min_periods=minp).apply(lambda a: (a[:-1] < a[-1]).mean(), raw=True)


# ------------------------------------------------------------ стратегии с фильтром
def eval_strategy(p, pos_close, start, end, name, cost=COST):
    bt = backtest(p, pos_close, start, end, cost)
    d = metrics(bt)
    d["name"] = name
    return bt, d


def filtered_monthly_positions(base_pos_m, filt_m, mode):
    """Комбинация месячного правила панели с фильтром (булев ряд на месячных датах).
    mode: 'veto' — лонг только если фильтр True; 'override' — лонг если правило ИЛИ фильтр;
    'gate_override' — фильтр открывает только ворота (наклон остаётся)."""
    f = filt_m.reindex(base_pos_m.index)
    if callable(mode):
        return mode(base_pos_m, f)
    if mode == "veto":
        return (base_pos_m.astype(bool) & f.fillna(True).astype(bool)).astype(float)
    if mode == "override":
        return (base_pos_m.astype(bool) | f.fillna(False).astype(bool)).astype(float)
    raise ValueError(mode)


def yearly_table(bt_dict):
    out = {}
    for k, bt in bt_dict.items():
        out[k] = bt["strat"].groupby(bt.index.year).sum() * 100
    out["bh"] = list(bt_dict.values())[0]["bh"].groupby(list(bt_dict.values())[0].index.year).sum() * 100
    return pd.DataFrame(out).round(1)


# ------------------------------------------------------ семейный прогон фильтров
def filtered_daily_positions(base_pos_d, filt_d, mode):
    f = filt_d.reindex(base_pos_d.index)
    if callable(mode):
        return mode(base_pos_d, f)
    if mode == "veto":
        return (base_pos_d.astype(bool) & f.fillna(True).astype(bool)).astype(float)
    if mode == "override":
        return (base_pos_d.astype(bool) | f.fillna(False).astype(bool)).astype(float)
    raise ValueError(mode)


def _tl_summary(bt, d):
    t = timeliness(bt)
    d["n_dd"] = len(t)
    d["avoid_mean"] = t["fall_avoided"].mean() if len(t) else np.nan
    d["missed_mean"] = t["rise_missed"].mean() if len(t) else np.nan
    d["exit_days_med"] = t["exit_days_after_peak"].median() if len(t) else np.nan
    d["entry_days_med"] = t["entry_days_after_trough"].median() if len(t) else np.nan
    return t


def run_family(p, base_pos, filters, windows, base_label, daily=False, cost=COST, boot=True):
    """filters: name -> (bool Series, mode). Месячный (base_pos на концах месяцев) или дневной режим.
    Возвращает (таблица метрик, dict своевременности по стратегиям в первом окне)."""
    rows, tls = [], {}
    base_d = base_pos if daily else to_daily_position(base_pos, p.index)
    for wname, (a, b) in windows.items():
        bt0, d0 = eval_strategy(p, base_d, a, b, base_label, cost)
        d0.update(window=wname, d_sharpe=0.0, ci_lo=np.nan, ci_hi=np.nan, p_le0=np.nan,
                  d_mdd=0.0, activity=0.0, n_months=np.nan)
        t0 = _tl_summary(bt0, d0)
        rows.append(d0)
        if wname == list(windows)[0]:
            tls[base_label] = t0
        for name, (f, mode) in filters.items():
            if daily:
                pos = filtered_daily_positions(base_pos, f, mode)
            else:
                pos_m = filtered_monthly_positions(base_pos, f, mode)
                pos = to_daily_position(pos_m, p.index)
            bt, d = eval_strategy(p, pos, a, b, name, cost)
            if boot:
                obs, lo, hi, pl, n = sharpe_diff_boot(bt, bt0)
            else:
                obs, lo, hi, pl, n = d["sharpe"] - d0["sharpe"], np.nan, np.nan, np.nan, np.nan
            act = (bt["pos"] != bt0["pos"]).mean()
            d.update(window=wname, d_sharpe=obs, ci_lo=lo, ci_hi=hi, p_le0=pl,
                     d_mdd=d["maxdd"] - d0["maxdd"], activity=act, n_months=n)
            t = _tl_summary(bt, d)
            rows.append(d)
            if wname == list(windows)[0]:
                tls[name] = t
    cols = ["window", "name", "cagr", "vol", "sharpe", "sharpe_ex", "maxdd", "tim", "trades_yr",
            "beat_bh_years", "hit_m", "d_sharpe", "ci_lo", "ci_hi", "p_le0", "d_mdd", "activity",
            "n_months", "n_dd", "avoid_mean", "missed_mean", "exit_days_med", "entry_days_med"]
    return pd.DataFrame(rows)[cols], tls


def placebo_family(p, base_pos, make_filter, sig, windows_first, n_placebo=300, block=21, seed=5,
                   daily=False, cost=COST):
    """Плацебо: пересобираем сигнал стационарным бутстрепом (блок ~block дней), заново строим
    фильтр той же конструкции и смотрим распределение ΔШарпа. make_filter(sig) -> (bool Series, mode)."""
    a, b = windows_first
    base_d = base_pos if daily else to_daily_position(base_pos, p.index)
    bt0, d0 = eval_strategy(p, base_d, a, b, "base", cost)
    f, mode = make_filter(sig)
    pos = filtered_daily_positions(base_pos, f, mode) if daily else \
        to_daily_position(filtered_monthly_positions(base_pos, f, mode), p.index)
    bt, d = eval_strategy(p, pos, a, b, "real", cost)
    obs = d["sharpe"] - d0["sharpe"]
    rng = np.random.default_rng(seed)
    vals = sig.dropna()
    out = np.empty(n_placebo)
    for k in range(n_placebo):
        ix = stationary_bootstrap_idx(len(vals), block, rng)
        fake = pd.Series(vals.values[ix], index=vals.index)
        f2, mode2 = make_filter(fake)
        pos2 = filtered_daily_positions(base_pos, f2, mode2) if daily else \
            to_daily_position(filtered_monthly_positions(base_pos, f2, mode2), p.index)
        bt2, d2 = eval_strategy(p, pos2, a, b, "fake", cost)
        out[k] = d2["sharpe"] - d0["sharpe"]
    return obs, float((out >= obs).mean()), float(np.nanmean(out)), float(np.nanpercentile(out, 95))


def detectable_rho(n, alpha=0.05, power=0.8):
    """Минимальный |ρ| Спирмена, который обнаруживается при n с заданной мощностью (Фишер z)."""
    if n < 5:
        return np.nan
    za = stats.norm.ppf(1 - alpha / 2); zb = stats.norm.ppf(power)
    z = (za + zb) / np.sqrt(n - 3)
    return float(np.tanh(z))
