# -*- coding: utf-8 -*-
"""B_timeliness — общая библиотека: данные, бэктест long/flat, зигзаг-эпизоды,
измерение запаздывания ворот, конструктор альтернативных ворот.
Запуск скриптов из каталога audit/. Все ряды панели уже с честными лагами публикации."""
import sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
D = "data"
COST = 0.002  # за смену позиции (round-trip 0.4%)

WINDOWS = {
    "FULL_2004+": ("2004-01-06", "2026-08-31"),
    "MAIN_2010+": ("2010-01-01", "2026-08-31"),
    "2010-2021": ("2010-01-01", "2021-12-31"),
    "2022-03..2024": ("2022-03-01", "2024-12-31"),
    "2025-2026": ("2025-01-01", "2026-08-31"),
    "A_2004-2017": ("2004-01-06", "2017-12-31"),
    "B_2018-2026": ("2018-01-01", "2026-08-31"),
}
EXCL = {
    "ex2022": ("2022-01-01", "2022-12-31"),
    "ex2008": ("2008-01-01", "2008-12-31"),
    "ex2008crash": ("2008-05-20", "2009-02-27"),
}


def load():
    p = pd.read_csv(f"{D}/panel_prod_daily.csv", parse_dates=["date"], index_col="date")
    c = pd.read_csv(f"{D}/cash_and_tr.csv", parse_dates=["date"], index_col="date")
    df = p.join(c[["mm_rate", "mcftr_ffill", "rgbi_ffill"]])
    df["r_long"] = np.log(df["mcftr_ffill"]).diff()
    df["r_flat"] = np.log1p(df["mm_rate"] / 100.0) / 252.0
    df["r_px"] = np.log(df["imoex"]).diff()
    # знак композита: гистерезис ±0.1 по месячному ряду; на день t — знак последнего закрытого среза <= t
    m = pd.read_csv(f"{D}/panel_prod_monthly.csv", parse_dates=["date"], index_col="date")
    s, out = 0, []
    for v in m["composite"].values:
        if np.isfinite(v):
            if v > 0.10:
                s = 1
            elif v < -0.10:
                s = -1
        out.append(s)
    m["sign"] = out
    df["comp_sign"] = m["sign"].reindex(df.index, method="ffill").fillna(0)
    df["comp_daily"] = m["composite"].reindex(df.index, method="ffill")
    return df


# ----------------------------------------------------------------------------- бэктест
def positions_from_gate(gate_off, comp_sign=None):
    """gate_off: True = ворота закрыты (риск-офф). Позиция 1 = лонг с закрытия t."""
    pos = (~gate_off.fillna(False).astype(bool)).astype(float)
    if comp_sign is not None:
        pos = pos * (comp_sign > 0).astype(float)
    return pos


def strat_returns(pos, df, cost=COST, lag=1):
    """lag=1: позиция с закрытия t работает на доходности t->t+1; издержка в день сделки."""
    p_prev = pos.shift(lag).fillna(0.0)
    trade = (pos != pos.shift(1)).astype(float)
    trade.iloc[0] = 0.0
    r = p_prev * df["r_long"] + (1 - p_prev) * df["r_flat"] + np.log1p(-cost) * trade
    return r


def _slice(r, start, end, excl=None):
    r = r.loc[start:end]
    if excl:
        a, b = excl
        r = r[(r.index < a) | (r.index > b)]
    return r


def metrics(r, df, pos=None, start=None, end=None, excl=None, bh=None):
    """r: дневные лог-доходности стратегии. Возвращает dict метрик."""
    r = _slice(r.dropna(), start, end, excl)
    if len(r) < 60:
        return {}
    years = len(r) / 252.0
    cum = r.cumsum()
    dd = cum - cum.cummax()
    rf = df["r_flat"].reindex(r.index).fillna(0)
    ex = r - rf
    out = {
        "cagr": float(np.expm1(r.sum() / years)),
        "vol": float(r.std() * np.sqrt(252)),
        "sharpe": float(r.mean() / r.std() * np.sqrt(252)) if r.std() > 0 else np.nan,
        "sharpe_ex": float(ex.mean() / ex.std() * np.sqrt(252)) if ex.std() > 0 else np.nan,
        "maxdd": float(np.expm1(dd.min())),
        "n_days": int(len(r)),
    }
    mr = r.resample("ME").sum()
    out["hit_m"] = float((mr > 0).mean())
    if pos is not None:
        p = pos.shift(1).reindex(r.index).fillna(0)
        out["in_mkt"] = float(p.mean())
        sw = (pos != pos.shift(1)).reindex(r.index).fillna(False)
        out["trades_yr"] = float(sw.sum() / years)
    if bh is not None:
        b = bh.reindex(r.index)
        ya = r.groupby(r.index.year).sum()
        yb = b.groupby(b.index.year).sum()
        out["beat_bh_yrs"] = float((ya > yb).mean())
        out["excess_cagr_vs_bh"] = float(np.expm1(r.sum() / years) - np.expm1(b.sum() / years))
    return out


# ----------------------------------------------------------------------------- зигзаг
def zigzag(px, thr=0.15):
    """Пивоты чередующихся движений >= thr. Возвращает список (type, idx)."""
    v = px.values
    n = len(v)
    piv = []
    hi = lo = 0
    mode = None
    for i in range(1, n):
        if v[i] > v[hi]:
            hi = i
        if v[i] < v[lo]:
            lo = i
        if mode is None:
            if v[i] <= v[hi] * (1 - thr):
                mode = "down"; piv.append(("peak", hi)); lo = hi + int(np.argmin(v[hi:i + 1]))
            elif v[i] >= v[lo] * (1 + thr):
                mode = "up"; piv.append(("trough", lo)); hi = lo + int(np.argmax(v[lo:i + 1]))
        elif mode == "down":
            if v[i] >= v[lo] * (1 + thr):
                piv.append(("trough", lo)); mode = "up"; hi = lo + int(np.argmax(v[lo:i + 1]))
        else:
            if v[i] <= v[hi] * (1 - thr):
                piv.append(("peak", hi)); mode = "down"; lo = hi + int(np.argmin(v[hi:i + 1]))
    if mode == "down":
        piv.append(("trough?", lo))
    elif mode == "up":
        piv.append(("peak?", hi))
    return piv


def episodes(px, thr=0.15, start="2004-01-06"):
    """Эпизоды (пик -> дно -> следующий пик) по зигзагу; dd = дно/пик - 1."""
    px = px.dropna()
    px = px[px.index >= "2003-01-01"]
    piv = zigzag(px, thr)
    idx = px.index
    eps = []
    for k, (t, i) in enumerate(piv):
        if not t.startswith("peak"):
            continue
        if k + 1 >= len(piv):
            continue
        t2, j = piv[k + 1]
        nxt = piv[k + 2][1] if k + 2 < len(piv) else len(px) - 1
        if idx[j] < pd.Timestamp(start):
            continue
        eps.append(dict(peak=idx[i], trough=idx[j], next_peak=idx[nxt],
                        peak_px=px.iloc[i], trough_px=px.iloc[j], next_px=px.iloc[nxt],
                        dd=px.iloc[j] / px.iloc[i] - 1, rise=px.iloc[nxt] / px.iloc[j] - 1,
                        confirmed=(t2 == "trough"),
                        next_confirmed=(k + 2 < len(piv) and piv[k + 2][0] == "peak"),
                        days_fall=int(j - i), days_rise=int(nxt - j)))
    return pd.DataFrame(eps)


# ----------------------------------------------------------------------------- запаздывание
def timeliness(gate_off, px, eps):
    """Для каждого эпизода: день выхода/входа сигнала относительно пика/дна и доли движения.
    gate_off: bool Series (True = риск-офф) на индексе px."""
    g = gate_off.reindex(px.index).fillna(False).astype(bool)
    gv = g.values
    idx = px.index
    lpx = np.log(px.values)
    rows = []
    for _, e in eps.iterrows():
        P, T, N = idx.get_loc(e.peak), idx.get_loc(e.trough), idx.get_loc(e.next_peak)
        row = dict(peak=e.peak.date(), trough=e.trough.date(), dd=e.dd)
        # --- выход: первое включение риск-офф в [пик, дно]; если уже включён на пике — начало текущего пробега
        if gv[P]:
            k = P
            while k - 1 >= 0 and gv[k - 1]:
                k -= 1
            ex_i = k
        else:
            on = np.where(gv[P:T + 1])[0]
            ex_i = P + int(on[0]) if len(on) else None
        fall = lpx[P] - lpx[T]
        if ex_i is None:
            row.update(exit_lag=np.nan, exit_date=None, fall_before_exit=1.0, caught=False)
        else:
            row.update(exit_lag=ex_i - P, exit_date=idx[ex_i].date(), caught=True,
                       fall_before_exit=float(np.clip((lpx[P] - lpx[max(ex_i, P)]) / fall, 0, 1)))
        # доля падения, ПРОЙДЕННАЯ В РЫНКЕ (экспозиционно, с учётом дребезга; позиция с закрытия t-1)
        in_mkt_fall = sum((lpx[t - 1] - lpx[t]) for t in range(P + 1, T + 1) if not gv[t - 1])
        row["fall_taken_share"] = float(in_mkt_fall / fall) if fall > 0 else np.nan
        # --- вход: первое выключение риск-офф в [дно, следующий пик]
        rise = lpx[N] - lpx[T]
        if not gv[T]:
            if ex_i is not None and ex_i <= T:
                k = T
                while k - 1 > ex_i and not gv[k - 1]:
                    k -= 1
                en_i = k  # выключился ДО дна (отрицательный лаг)
            else:
                en_i = None
        else:
            off = np.where(~gv[T:N + 1])[0]
            en_i = T + int(off[0]) if len(off) else None
        if en_i is None:
            row.update(entry_lag=np.nan, entry_date=None,
                       rise_missed=(np.nan if ex_i is None else (1.0 if gv[T] else 0.0)))
        else:
            row.update(entry_lag=en_i - T, entry_date=idx[en_i].date(),
                       rise_missed=(float(np.clip((lpx[min(max(en_i, T), N)] - lpx[T]) / rise, 0, 1))
                                    if rise > 0 else np.nan))
        out_rise = sum((lpx[t] - lpx[t - 1]) for t in range(T + 1, N + 1) if gv[t - 1])
        row["rise_missed_share"] = float(out_rise / rise) if rise > 0 else np.nan
        rows.append(row)
    return pd.DataFrame(rows)


def summarize_timeliness(tl, cap=-126):
    """Средние лаги считаются с обрезкой отрицательных значений на cap (уже был выключен задолго до пика)."""
    c = tl[tl.caught]
    return dict(
        n_eps=len(tl), n_caught=int(tl.caught.sum()),
        already_off_share=float((c.exit_lag <= 0).mean()) if len(c) else np.nan,
        exit_lag_med=float(c.exit_lag.median()) if len(c) else np.nan,
        exit_lag_mean=float(c.exit_lag.clip(lower=cap).mean()) if len(c) else np.nan,
        exit_lag_med_late=float(c.exit_lag[c.exit_lag > 0].median()) if (c.exit_lag > 0).any() else np.nan,
        fall_before_exit_med=float(tl.fall_before_exit.median()),
        fall_before_exit_mean=float(tl.fall_before_exit.mean()),
        fall_taken_mean=float(tl.fall_taken_share.mean()),
        entry_lag_med=float(c.entry_lag.median()) if len(c) else np.nan,
        entry_lag_mean=float(c.entry_lag.mean()) if len(c) else np.nan,
        rise_missed_med=float(c.rise_missed.median()) if len(c) else np.nan,
        rise_missed_mean=float(c.rise_missed.mean()) if len(c) else np.nan,
        rise_missed_share_mean=float(tl.rise_missed_share.mean()),
    )


# ----------------------------------------------------------------------------- конструктор ворот
def rv(px, w):
    return np.log(px).diff().rolling(w).std() * np.sqrt(252)


def roll_q(s, q, lookback=756, minp=252):
    return s.rolling(lookback, min_periods=minp).quantile(q)


def hyst_below(px, ma, h):
    """Риск-офф, когда px < ma*(1-h); риск-он, когда px > ma*(1+h); между — держим."""
    lo = (px < ma * (1 - h)).values
    hi = (px > ma * (1 + h)).values
    out = np.zeros(len(px), dtype=bool)
    s = False
    for i in range(len(px)):
        if lo[i]:
            s = True
        elif hi[i]:
            s = False
        out[i] = s
    return pd.Series(out, index=px.index)


def state_machine(exit_cond, entry_cond):
    """Риск-офф включается по exit_cond, выключается по entry_cond (асимметрия)."""
    ex = exit_cond.fillna(False).astype(bool).values
    en = entry_cond.fillna(False).astype(bool).values
    out = np.zeros(len(ex), dtype=bool)
    s = False
    for i in range(len(ex)):
        s = bool(ex[i]) if not s else (not bool(en[i]))
        out[i] = s
    return pd.Series(out, index=exit_cond.index)


def confirm(sig, n_on, n_off=None):
    """Подтверждение: риск-офф после n_on дней подряд True, снятие после n_off дней подряд False."""
    n_off = n_on if n_off is None else n_off
    v = sig.fillna(False).astype(bool).values
    out = np.zeros(len(v), dtype=bool)
    s, run_on, run_off = False, 0, 0
    for i in range(len(v)):
        run_on = run_on + 1 if v[i] else 0
        run_off = run_off + 1 if not v[i] else 0
        if not s and run_on >= n_on:
            s = True
        elif s and run_off >= n_off:
            s = False
        out[i] = s
    return pd.Series(out, index=sig.index)


def bits(df):
    """Базовые компоненты для конструктора (все — bool Series 'риск-офф')."""
    px = df["imoex"]
    B = {}
    ma200 = px.rolling(200).mean()
    for n in (50, 100, 150, 200, 250):
        B[f"trend_ma{n}"] = px < px.rolling(n).mean()
    for h in (0.02, 0.03):
        B[f"trend_ma200_h{int(h*100)}"] = hyst_below(px, ma200, h)
    B["trend_slope200"] = ma200 < ma200.shift(20)
    B["trend_dma50_200"] = px.rolling(50).mean() < ma200
    for w in (10, 21, 42):
        v = rv(px, w)
        for q in (0.70, 0.80, 0.90):
            B[f"vol_w{w}_p{int(q*100)}"] = v > roll_q(v, q)
    B["vol_jump_21_63"] = (rv(px, 21) / rv(px, 63)) > 1.5
    B["vol_jump_10_63"] = (rv(px, 10) / rv(px, 63)) > 1.5
    if "rvi" in df:
        r = df["rvi"]
        for lvl in (35, 40, 45, 50):
            B[f"rvi_gt{lvl}"] = r > lvl
        B["rvi_p80"] = r > roll_q(r, 0.80)
        B["rvi_p90"] = r > roll_q(r, 0.90)
        B["rvi_jump5_gt8"] = (r - r.shift(5)) > 8
        B["rvi_jump_ratio21"] = (r / r.rolling(21).mean()) > 1.3
    rg = df["rgbi"]
    rgdd = np.log(rg / rg.rolling(252, min_periods=120).max())
    for x in (2, 3, 4, 6):
        B[f"bond_dd{x}"] = rgdd < -x / 100.0
    for k in (21, 42):
        B[f"bond_mom{k}"] = np.log(rg / rg.shift(k)) < 0
    for n in (50, 100):
        B[f"bond_ma{n}"] = rg < rg.rolling(n).mean()
    if "breadth" in df:
        b = df["breadth"]
        for x in (0.3, 0.4, 0.5):
            B[f"breadth_lt{int(x*100)}"] = b < x
        B["breadth_chg21_lt-20"] = (b - b.shift(21)) < -0.20
    for x in (5, 8, 10):
        B[f"mom21_lt-{x}"] = np.log(px / px.shift(21)) < -x / 100.0
    for x in (10, 15, 20):
        B[f"dd252_lt-{x}"] = np.log(px / px.rolling(252).max()) < -x / 100.0
    B["prod_trend"] = df["st_trend"] == 0
    B["prod_vol"] = df["st_vol"] == 1
    B["prod_bond"] = df["st_bond"] == 1
    B["prod_toxic"] = df["cell"] == "bear|stress|stress"
    return B


def gate_from_bits(t, v, b, k=3):
    """Ворота из трёх компонент: закрыты, если >= k из трёх риск-офф."""
    s = t.fillna(False).astype(int) + v.fillna(False).astype(int) + b.fillna(False).astype(int)
    return s >= k


TR = ["trend_ma50", "trend_ma100", "trend_ma150", "trend_ma200", "trend_ma250",
      "trend_ma200_h2", "trend_ma200_h3", "trend_slope200", "trend_dma50_200"]
VO = [f"vol_w{w}_p{q}" for w in (10, 21, 42) for q in (70, 80, 90)] + ["vol_jump_21_63", "vol_jump_10_63"]
BO = ["bond_dd2", "bond_dd3", "bond_dd4", "bond_dd6", "bond_mom21", "bond_mom42", "bond_ma50", "bond_ma100"]
EARLY = ["mom21_lt-5", "mom21_lt-8", "mom21_lt-10", "dd252_lt-10", "dd252_lt-15", "dd252_lt-20",
         "vol_jump_21_63", "vol_jump_10_63"]
RVI = ["rvi_gt35", "rvi_gt40", "rvi_gt45", "rvi_gt50", "rvi_p80", "rvi_p90", "rvi_jump5_gt8", "rvi_jump_ratio21"]
BR = ["breadth_lt30", "breadth_lt40", "breadth_lt50", "breadth_chg21_lt-20"]


def build_gates(df, B, mode="core"):
    """Словарь ворот name -> dict(gate, family, ...). mode='core' — без RVI/ширины (история с 2004);
    mode='late' — конструкции с RVI/шириной (только с 2014-2015)."""
    G = {}
    toxic = B["prod_toxic"]
    two3 = gate_from_bits(B["prod_trend"], B["prod_vol"], B["prod_bond"], 2)
    any3 = gate_from_bits(B["prod_trend"], B["prod_vol"], B["prod_bond"], 1)
    if mode == "core":
        G["prod_toxic"] = dict(gate=toxic, family="prod")
        G["prod_two_of_three"] = dict(gate=two3, family="prod_k2")
        G["prod_any_bit"] = dict(gate=any3, family="prod_k1")
        for n in TR + VO + BO + EARLY:
            G["single:" + n] = dict(gate=B[n], family="single", comp=n)
        for t in TR:
            for v in VO:
                for b in BO:
                    for k in (2, 3):
                        G[f"cell[{t}|{v}|{b}]k{k}"] = dict(gate=gate_from_bits(B[t], B[v], B[b], k),
                                                          family=f"grid_k{k}", trend=t, vol=v, bond=b, k=k)
        for trig in EARLY:
            G[f"toxic OR {trig}"] = dict(gate=toxic | B[trig].fillna(False), family="addon_toxic", comp=trig)
            G[f"two3 OR {trig}"] = dict(gate=two3 | B[trig].fillna(False), family="addon_two3", comp=trig)
        exits = {"toxic": toxic, "two3": two3, "mom21<-8": B["mom21_lt-8"], "mom21<-10": B["mom21_lt-10"],
                 "two3|mom21<-8": two3 | B["mom21_lt-8"], "toxic|mom21<-10": toxic | B["mom21_lt-10"],
                 "toxic|dd252<-15": toxic | B["dd252_lt-15"], "vol_p80|bond_dd4": B["vol_w21_p80"] | B["bond_dd4"]}
        entries = {"!toxic": ~toxic, "!two3": ~two3, "px>MA200": ~B["trend_ma200"], "px>MA50": ~B["trend_ma50"],
                   "!toxic&px>MA200": ~toxic & ~B["trend_ma200"], "!any_bit": ~any3,
                   "px>MA100": ~B["trend_ma100"], "mom21>0": ~(np.log(df["imoex"] / df["imoex"].shift(21)) < 0)}
        for en, es in exits.items():
            for nn, ns in entries.items():
                G[f"asym[exit={en}|entry={nn}]"] = dict(gate=state_machine(es, ns), family="asym", exit=en, entry=nn)
        for n in (3, 5, 10):
            G[f"toxic_confirm{n}"] = dict(gate=confirm(toxic, n), family="confirm", n_on=n, n_off=n)
        for a, b in ((1, 5), (1, 10), (1, 21), (5, 1), (10, 1), (3, 10)):
            G[f"toxic_confirm_on{a}_off{b}"] = dict(gate=confirm(toxic, a, b), family="confirm", n_on=a, n_off=b)
    else:
        G["prod_toxic"] = dict(gate=toxic, family="prod")
        G["prod_two_of_three"] = dict(gate=two3, family="prod_k2")
        for n in RVI + BR:
            G["single:" + n] = dict(gate=B[n], family="single_late", comp=n)
        for t in ("trend_ma200", "trend_ma100", "trend_ma50"):
            for v in RVI[:6]:
                for b in ("bond_dd4", "bond_dd3", "bond_mom21", "bond_ma50"):
                    for k in (2, 3):
                        G[f"cell[{t}|{v}|{b}]k{k}"] = dict(gate=gate_from_bits(B[t], B[v], B[b], k),
                                                          family=f"grid_rvi_k{k}", trend=t, vol=v, bond=b, k=k)
        for trig in RVI + BR:
            G[f"toxic OR {trig}"] = dict(gate=toxic | B[trig].fillna(False), family="addon_toxic_late", comp=trig)
            G[f"two3 OR {trig}"] = dict(gate=two3 | B[trig].fillna(False), family="addon_two3_late", comp=trig)
        # ширина как четвёртый бит
        for br in BR[:3]:
            G[f"toxic AND {br}"] = dict(gate=toxic & B[br].fillna(False), family="and_breadth", comp=br)
            G[f"two3 AND {br}"] = dict(gate=two3 & B[br].fillna(False), family="and_breadth", comp=br)
    return G


def gate_stats(gate, start, end):
    g = gate.reindex(pd.date_range(start, end, freq="D")).dropna() if False else gate.loc[start:end].fillna(False).astype(bool)
    years = len(g) / 252.0
    sw = (g != g.shift(1)).sum() / years
    return dict(off_share=float(g.mean()), switches_yr=float(sw))
