"""A_baseline: общая библиотека — загрузка данных, дневной композит «как на витрине»,
позиции всех вариантов long/flat, бэктест и метрики.
Запуск скриптов из каталога audit/ (пути относительные: data/, results/).
"""
import sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")

DATA = "data"
RES = "results"
TOXIC = "bear|stress|stress"
# «окна входа» (1,1,0)=bull|stress|ok, (0,1,0)=bear|stress|ok и «рабочий режим» (1,0,0)=bull|calm|ok
ENTRY_CELLS = {"bull|calm|ok", "bull|stress|ok", "bear|stress|ok"}
HYST = 0.10
Z_WIN, Z_MIN, Z_CLIP = 60, 24, 3.0
LEGS = [("usd_mom63", +1), ("slope_10_2", +1), ("urals_rub_gap", -1)]
BT_START = "2004-01-06"          # первый день с ячейкой
BT_END = "2026-08-31"            # последний закрытый месяц
COST = 0.002

WINDOWS = {
    "2010-2026 (основное)": ("2010-01-01", "2026-08-31"),
    "2004-2026 (полное)": ("2004-01-06", "2026-08-31"),
    "2010-2021": ("2010-01-01", "2021-12-31"),
    "2022-03..2024": ("2022-03-24", "2024-12-31"),
    "2025-2026-08": ("2025-01-01", "2026-08-31"),
    "2010-2026 ex-2022": ("2010-01-01", "2026-08-31"),
}

VARIANTS = {
    "a": "только композит (знак закрытого месяца, гистерезис ±0,1)",
    "b": "только ворота (ячейка не токсичная)",
    "c": "ПРАВИЛО ПАНЕЛИ: ворота И знак закрытого месяца",
    "d": "ворота И дневной композит (гистерезис поверх состояния закрытого месяца, как compute_core)",
    "d0": "ворота И дневной композит > 0 без гистерезиса (так рисует «наклон» витрина app.js)",
    "e": "правило панели, решение только на закрытии месяца (месячный шаг)",
    "e2": "правило панели, дневное наблюдение, но не чаще одной смены в 21 торг. день",
    "f": "buy&hold MCFTR",
    "g": "100% денежный рынок (mm_rate)",
    "h": "ворота И композит закрытого месяца > +0,3 («умеренный лонг»)",
    "i": "строгие ворота (только bull|calm|ok, bull|stress|ok, bear|stress|ok) И знак закрытого месяца",
    "i2": "только строгие ворота (без композита)",
}


def load():
    D = pd.read_csv(f"{DATA}/panel_prod_daily.csv", parse_dates=["date"]).set_index("date")
    M = pd.read_csv(f"{DATA}/panel_prod_monthly.csv", parse_dates=["date"]).set_index("date")
    C = pd.read_csv(f"{DATA}/cash_and_tr.csv", parse_dates=["date"]).set_index("date")
    assert D.index.equals(C.index), "индексы daily и cash не совпадают"
    return D, M, C


def closed_month_ends(idx):
    """Даты последних торговых дней ЗАКРЫТЫХ месяцев (последний месяц ряда считается незакрытым)."""
    per = idx.to_period("M")
    last_in_month = (idx.to_series().groupby(per).transform("max") == idx.to_series()).values
    closed = (per < per[-1])
    return idx[last_in_month & closed]


def hyst_states(vals, thr=HYST):
    """Состояние гистерезиса после каждой точки: +1/−1, 0 = ещё не определялось (как calc.hysteresis_sign)."""
    out = np.zeros(len(vals), dtype=int)
    s = 0
    for i, v in enumerate(vals):
        if np.isfinite(v):
            if v > thr:
                s = 1
            elif v < -thr:
                s = -1
        out[i] = s
    return out


def daily_composite(D, Mc):
    """Композит «как показала бы панель сегодня»: ряд закрытых месяцев + сегодняшнее значение
    как последняя точка, z по окну 60 точек (включая текущую), min 24, ddof=1, обрезка ±3.
    Возвращает Series по дневному индексу (NaN до появления ног) и массив k (число закрытых месяцев до t)."""
    me = Mc.index
    raw = {leg: Mc[f"raw_{leg}"].values.astype(float) for leg, _ in LEGS}
    daily = {leg: D[leg].groupby(D.index.to_period("M")).ffill().values.astype(float) for leg, _ in LEGS}
    k_arr = np.searchsorted(me.values, D.index.values, side="left")  # число закрытых месяцев до дня t
    out = np.full(len(D), np.nan)
    for i in range(len(D)):
        k = k_arr[i]
        s, n = 0.0, 0
        for leg, sgn in LEGS:
            x = daily[leg][i]
            if not np.isfinite(x):
                continue
            win = raw[leg][max(0, k - (Z_WIN - 1)):k]
            vals = win[np.isfinite(win)]
            vals = np.append(vals, x)
            if len(vals) < Z_MIN or len(vals) < 2:
                continue
            sd = vals.std(ddof=1)
            if sd <= 0:
                continue
            z = (x - vals.mean()) / sd
            z = max(-Z_CLIP, min(Z_CLIP, z))
            s += sgn * z
            n += 1
        if n:
            out[i] = s / n
    return pd.Series(out, index=D.index), k_arr


def build_positions(D, M, C):
    """Дневные позиции всех вариантов (0/1) по закрытию дня t. Возвращает (DataFrame, месячный срез закрытых месяцев)."""
    me = closed_month_ends(D.index)
    Mc = M.loc[M.index.isin(me)].copy()
    assert Mc.index[-1] == pd.Timestamp(BT_END), Mc.index[-1]
    # сверка: raw ноги месячного среза = дневная панель в ту же дату
    for leg, _ in LEGS:
        a = Mc[f"raw_{leg}"]; b = D[leg].reindex(Mc.index)
        m = a.notna() & b.notna()
        assert np.allclose(a[m], b[m]), f"raw_{leg} расходится с дневной панелью"

    comp_closed_m = Mc["composite"].astype(float)
    states_m = hyst_states(comp_closed_m.values)
    sign_closed_m = pd.Series(np.where(states_m == 0, np.nan, states_m), index=Mc.index)

    P = pd.DataFrame(index=D.index)
    P["cell"] = D["cell"]
    P["comp_closed"] = comp_closed_m.reindex(D.index).ffill()
    P["sign_closed"] = sign_closed_m.reindex(D.index).ffill()
    comp_d, k_arr = daily_composite(D, Mc)
    P["comp_daily"] = comp_d
    # сверка: в последний торговый день закрытого месяца дневной композит = месячному
    chk = (comp_d.reindex(Mc.index) - comp_closed_m).abs().max()
    assert chk < 1e-9, f"дневной композит на конце месяца расходится с месячным: {chk}"
    # знак с гистерезисом «как compute_core»: состояние после закрытых месяцев < t, обновлённое сегодняшним значением
    prev_state = np.where(k_arr > 0, states_m[np.maximum(k_arr - 1, 0)], 0)
    v = comp_d.values
    sd = prev_state.copy()
    sd = np.where(np.isfinite(v) & (v > HYST), 1, sd)
    sd = np.where(np.isfinite(v) & (v < -HYST), -1, sd)
    P["sign_daily"] = np.where(sd == 0, np.nan, sd)

    gate = (P["cell"].notna() & (P["cell"] != TOXIC)).astype(int)
    gate_strict = P["cell"].isin(ENTRY_CELLS).astype(int)
    sc = (P["sign_closed"] == 1).astype(int)
    P["pos_a"] = sc
    P["pos_b"] = gate
    P["pos_c"] = gate * sc
    P["pos_d"] = gate * (P["sign_daily"] == 1).astype(int)
    P["pos_d0"] = gate * (P["comp_daily"] > 0).astype(int)
    # e: решение только на закрытии закрытого месяца
    P["pos_e"] = P["pos_c"].where(P.index.isin(me)).ffill().fillna(0).astype(int)
    # e2: дневное наблюдение, но после смены позиции — блокировка 21 торговый день
    pc = P["pos_c"].values
    e2 = np.zeros(len(pc), dtype=int); cur = 0; last_change = -10**9
    for i in range(len(pc)):
        if pc[i] != cur and i - last_change >= 21:
            cur = pc[i]; last_change = i
        e2[i] = cur
    P["pos_e2"] = e2
    P["pos_f"] = 1
    P["pos_g"] = 0
    P["pos_h"] = gate * (P["comp_closed"] > 0.3).astype(int)
    P["pos_i"] = gate_strict * sc
    P["pos_i2"] = gate_strict
    # до старта бэктеста — флэт
    for c in [c for c in P.columns if c.startswith("pos_")]:
        P.loc[P.index < BT_START, c] = 0
        P[c] = P[c].astype(int)
    P["ret_mcftr"] = C["mcftr_ffill"].pct_change()
    P["logret_mcftr"] = np.log(C["mcftr_ffill"]).diff()
    P["ret_mm"] = (C["mm_rate"] / 100.0 / 252.0).shift(1)   # ставка, известная на закрытии t−1, за день t
    P["imoex"] = D["imoex"]
    P["mm_rate"] = C["mm_rate"]
    return P, Mc


def backtest(pos, ret_eq, ret_mm, cost=COST, exec_lag=0):
    """pos — позиция по закрытию t (0/1). exec_lag=0: позиция действует с закрытия t; 1: с закрытия t+1.
    Возвращает DataFrame: p (действующая позиция), ret (дневная доходность), turn, nav."""
    p = pos.shift(exec_lag).fillna(0).astype(int) if exec_lag else pos.astype(int)
    p_prev = p.shift(1).fillna(0)
    r_eq = ret_eq.fillna(0.0); r_mm = ret_mm.fillna(0.0)
    ret = p_prev * r_eq + (1 - p_prev) * r_mm
    turn = (p - p_prev).abs()
    nav = ((1 + ret) * (1 - cost * turn)).cumprod()
    return pd.DataFrame({"p": p, "ret": ret, "turn": turn, "nav": nav})


def slice_nav(nav, s, e):
    """NAV в окне, нормированный на закрытие последнего дня ПЕРЕД окном (чтобы первый день окна вошёл)."""
    idx = nav.index
    before = idx[idx < pd.Timestamp(s)]
    base_i = before[-1] if len(before) else idx[0]
    w = nav.loc[base_i:e]
    return w / w.iloc[0]


def max_drawdown(nav):
    dd = nav / nav.cummax() - 1
    return dd.min(), dd.idxmin()


def metrics(bt, bh_bt, mm_bt, s, e, ex2022=False):
    """Метрики стратегии в окне [s,e]. bt/bh_bt/mm_bt — результат backtest()."""
    nav = slice_nav(bt["nav"], s, e)
    nav_bh = slice_nav(bh_bt["nav"], s, e)
    nav_mm = slice_nav(mm_bt["nav"], s, e)
    p = bt["p"].loc[nav.index[0]:nav.index[-1]]
    trades = bt["turn"].loc[nav.index[1]:nav.index[-1]]
    if ex2022:
        keep = nav.index.year != 2022
        r = nav.pct_change().fillna(0)[keep]; rb = nav_bh.pct_change().fillna(0)[keep]; rm = nav_mm.pct_change().fillna(0)[keep]
        nav = (1 + r).cumprod(); nav_bh = (1 + rb).cumprod(); nav_mm = (1 + rm).cumprod()
        p = p[p.index.year != 2022]; trades = trades[trades.index.year != 2022]
    years = (nav.index[-1] - nav.index[0]).days / 365.25 - (1.0 if ex2022 else 0.0)
    cagr = nav.iloc[-1] ** (1 / years) - 1
    cagr_bh = nav_bh.iloc[-1] ** (1 / years) - 1
    cagr_mm = nav_mm.iloc[-1] ** (1 / years) - 1
    rm_ = np.log(nav.resample("ME").last()).diff().dropna()
    rb_ = np.log(nav_bh.resample("ME").last()).diff().dropna()
    rmm_ = np.log(nav_mm.resample("ME").last()).diff().dropna()
    vol = rm_.std() * np.sqrt(12)
    sharpe = sharpe_m(rm_)
    sharpe_ex = sharpe_m(rm_ - rmm_)
    mdd, mdd_date = max_drawdown(nav)
    tim = p.mean()
    trades_py = trades.sum() / years
    yr = nav.groupby(nav.index.year).last(); yr_bh = nav_bh.groupby(nav_bh.index.year).last()
    yr_ret = yr.pct_change().dropna(); yr_ret_bh = yr_bh.pct_change().dropna()
    beat = float((yr_ret > yr_ret_bh).mean()) if len(yr_ret) else np.nan
    # hit-rate месяцев: позиция месяца оказалась лучшей из двух (в среднем лонг → обогнали ММ; флэт → обогнали b&h)
    pm = p.resample("ME").mean().reindex(rm_.index)
    right = np.where(pm >= 0.5, rm_ > rmm_, rm_ > rb_)
    hit = float(np.mean(right))
    hit_pos = float((rm_ > 0).mean())
    return dict(cagr=cagr, vol=vol, sharpe=sharpe, sharpe_ex_mm=sharpe_ex, mdd=mdd, mdd_date=str(mdd_date.date()),
                time_in_mkt=tim, trades_per_year=trades_py, years_beat_bh=beat, hit_month=hit, hit_month_pos=hit_pos,
                n_months=len(rm_), cagr_bh=cagr_bh, cagr_mm=cagr_mm, final_nav=nav.iloc[-1], years=years)


def sharpe_m(x):
    return x.mean() / x.std() * np.sqrt(12) if x.std() > 0 else np.nan
