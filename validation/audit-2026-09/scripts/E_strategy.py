"""E_algopack шаг 4b: инкремент сигналов ALGOPACK к правилу панели (long/flat) и своевременность.
Правило панели восстановлено из panel_prod_monthly (композит, гистерезис ±0.1) и дневной ячейки.
Лонг = MCFTR, флэт = mm_rate/252, издержки 0.2% за смену позиции (чувствительность 0.1/0.3).
Позиция по сигналу на закрытии t применяется к доходности t->t+1 (lag0) и, консервативно, t+1->t+2 (lag1).
Выход: results/E_baseline.csv, results/E_overlays.csv, results/E_timeliness.csv, results/E_episodes.csv."""
import sys, os, itertools
import numpy as np, pandas as pd
if hasattr(sys.stdout, "reconfigure"): sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
D = os.path.join(ROOT, "data"); A = os.path.join(D, "algopack"); R = os.path.join(ROOT, "results")

START, END = "2020-04-01", "2026-08-31"   # z120 FUTOI готов с 2020-03-31
COST = 0.002

feat = pd.read_csv(os.path.join(A, "E_features_daily.csv"), parse_dates=["date"]).set_index("date")
# бесплатный аналог (ISS history): число сделок ядра, 21д z252 — для сравнения с платными признаками
_fp = os.path.join(A, "E_free_signals_daily.csv")
if os.path.exists(_fp):
    _fr = pd.read_csv(_fp, index_col=0, parse_dates=True)
    feat["ntr_free_z"] = _fr["n_trades|c3|21d z252"].reindex(feat.index)
    feat["ntr_free_z5"] = _fr["n_trades|c3|5d z252"].reindex(feat.index)
panel = pd.read_csv(os.path.join(D, "panel_prod_daily.csv"), parse_dates=["date"]).set_index("date")
pm = pd.read_csv(os.path.join(D, "panel_prod_monthly.csv"), parse_dates=["date"]).set_index("date")
cash = pd.read_csv(os.path.join(D, "cash_and_tr.csv"), parse_dates=["date"]).set_index("date")

idx = panel.index[(panel.index >= "2019-06-01") & (panel.index <= END)]
panel = panel.reindex(idx); cash = cash.reindex(idx); feat = feat.reindex(idx)
toxic = (panel.cell == "bear|stress|stress")

# ---------------------------------------------------------------- доходности ног
r_long = np.log(cash.mcftr_ffill).diff()                    # полная доходность
r_flat = np.log1p(cash.mm_rate / 100 / 252)
r_imoex = np.log(panel.imoex).diff()

# ---------------------------------------------------------------- правило панели
# знак композита с гистерезисом ±0.1 по месячным срезам
sign = pd.Series(index=pm.index, dtype=float)
prev = 0.0
for d, c in pm.composite.items():
    if np.isnan(c):
        s = prev
    elif c > 0.1:
        s = 1.0
    elif c < -0.1:
        s = -1.0
    else:
        s = prev if prev != 0 else np.sign(c)
    sign[d] = s; prev = s
sign_daily = sign.reindex(idx.union(sign.index)).ffill().reindex(idx).shift(1)  # известен со СЛЕДУЮЩЕГО дня после среза
# на сам день среза месяца знак ещё «старый» (решение принимает значение закрытого месяца) -> shift(1)
gate_m = (~(pm.cell == "bear|stress|stress")).astype(float)
gate_m_daily = gate_m.reindex(idx.union(gate_m.index)).ffill().reindex(idx).shift(1)
pos_panel_m = ((sign_daily > 0) & (gate_m_daily > 0)).astype(float)        # месячное решение
pos_panel_d = ((sign_daily > 0) & (~toxic)).astype(float)                   # ворота ежедневно


def backtest(pos, cost=COST, lag=0):
    pos = pos.reindex(idx).fillna(0.0).shift(lag).fillna(0.0)
    ret = pos.shift(1) * r_long + (1 - pos.shift(1)) * r_flat
    trades = pos.diff().abs().fillna(0.0)
    ret = ret - cost * trades
    return ret, pos, trades


def metrics(ret, pos, trades, name, a=START, b=END):
    m = (ret.index >= a) & (ret.index <= b)
    r = ret[m].dropna(); p = pos[m]; tr = trades[m]
    if len(r) < 120:
        return dict(name=name, sharpe=np.nan, sharpe_ex=np.nan, cagr=np.nan, maxdd=np.nan)
    yrs = len(r) / 252
    eq = r.cumsum()
    dd = (eq - eq.cummax()).min()
    rf = r_flat.reindex(r.index)
    mret = r.groupby([r.index.year, r.index.month]).sum()
    bh = r_long.reindex(r.index); bhm = bh.groupby([bh.index.year, bh.index.month]).sum()
    yr = r.groupby(r.index.year).sum(); bhy = bh.groupby(bh.index.year).sum()
    return dict(name=name, cagr=round((np.exp(r.sum() / yrs) - 1) * 100, 2),
                vol=round(r.std() * np.sqrt(252) * 100, 2),
                sharpe=round(r.mean() / r.std() * np.sqrt(252), 3) if r.std() > 0 else np.nan,
                sharpe_ex=round((r - rf).mean() / (r - rf).std() * np.sqrt(252), 3) if (r - rf).std() > 0 else np.nan,
                maxdd=round((np.exp(dd) - 1) * 100, 2), in_mkt=round(p.mean() * 100, 1),
                trades_yr=round(tr.sum() / yrs, 2),
                beat_bh_years=f"{int((yr > bhy).sum())}/{len(yr)}",
                hit_m=round((mret > 0).mean() * 100, 1), hit_m_vs_bh=round((mret > bhm).mean() * 100, 1),
                n_days=len(r))


chk = pd.DataFrame({"composite": pm.composite, "sign": sign, "cell": pm.cell, "gate": gate_m})
chk["pos_next_month"] = ((chk.sign > 0) & (chk.gate > 0)).astype(int)
chk[chk.index >= "2019-06-01"].to_csv(os.path.join(R, "E_panel_rule_monthly.csv"))
print("Правило панели по месяцам (последние 30):"); print(chk.tail(30).to_string())

rows = []
for name, pos in (("b&h MCFTR", pd.Series(1.0, index=idx)), ("100% денежный рынок", pd.Series(0.0, index=idx)),
                  ("панель (месячное решение)", pos_panel_m), ("панель (ворота ежедневно)", pos_panel_d)):
    for cost in (0.001, 0.002, 0.003):
        ret, p, tr = backtest(pos, cost)
        rec = metrics(ret, p, tr, name); rec["cost"] = cost
        rows.append(rec)
        if cost == COST:
            for tag, ab in {"2020-04..2021": (START, "2021-12-31"), "2022-03..2024": ("2022-03-01", "2024-12-31"),
                            "2025..2026-08": ("2025-01-01", END), "ex2022": None}.items():
                if tag == "ex2022":
                    r2 = ret[(ret.index.year != 2022)]
                    rec2 = metrics(r2, p[(p.index.year != 2022)], tr[(tr.index.year != 2022)], name, START, END)
                else:
                    rec2 = metrics(ret, p, tr, name, ab[0], ab[1])
                rec2["cost"] = cost; rec2["window"] = tag; rows.append(rec2)
base = pd.DataFrame(rows); base["window"] = base.get("window", pd.Series(dtype=object)).fillna("2020-04..2026-08")
base.to_csv(os.path.join(R, "E_baseline.csv"), index=False)
pd.set_option("display.width", 250); pd.set_option("display.max_columns", 40)
print("=== БАЗОВЫЕ ЛИНИИ ===")
print(base[base.cost == COST].to_string(index=False))

# ---------------------------------------------------------------- оверлеи сигналов ALGOPACK
# (сигнал, знак ориентации: +1 => высокие значения = «за лонг», уже z-скор или нет)
CANDS = [
    ("fz_MX", -1, True), ("fz_idx", -1, True), ("fz252_idx", -1, True), ("fz_IMOEXF", -1, True),
    ("fz_Si", -1, True), ("fnlong_z_idx", -1, True), ("fretail_z_MX", +1, True), ("fgross_g21_MX", +1, False),
    ("fhold_g21_idx", +1, False), ("hi_vol_z", -1, True), ("hi_nf21z", +1, True),
    ("fyur_g21_MX", -1, False), ("foi_g21_MX", +1, False), ("fz_oi_idx", -1, True),
    ("avgtr_z_c3", +1, True), ("avgtr_c3", +1, False), ("hi_lnf21", +1, False), ("hi_nf21", +1, False),
    ("obimb21_sber", +1, False), ("obimb5_sber", +1, False), ("ntr_z_c3", -1, True),
    ("ntr_free_z", -1, True), ("ntr_free_z5", -1, True),
    ("obimb21_c3", +1, False), ("obimb5_c3", +1, False), ("imb21z_c3", +1, True), ("imb21_c3", +1, False),
    ("obimb21_all", +1, False), ("imbtr21_all", +1, False), ("trades_g_all", +1, False),
]
CANDS = [c for c in CANDS if c[0] in feat.columns and feat[c[0]].notna().sum() > 250]


def oriented(sid, sgn, is_z):
    s = feat[sid] * sgn
    if not is_z:
        # расширяющееся z без заглядывания вперёд (min 120 дней)
        m = s.expanding(120).mean(); sd = s.expanding(120).std()
        s = (s - m) / sd
    return s


def month_end_hold(sig):
    """Значение сигнала фиксируется на конец месяца и держится месяц (месячное решение)."""
    me = sig.groupby([sig.index.year, sig.index.month]).transform(lambda x: x.iloc[-1])
    # значение конца месяца известно на последний день -> применяется со следующего дня
    last = sig.index.to_series().groupby([sig.index.year, sig.index.month]).transform("last")
    v = sig.where(sig.index == last).ffill()
    return v


orows = []
n_variants = 0
for sid, sgn, is_z in CANDS:
    S = oriented(sid, sgn, is_z)
    first = S.dropna().index[0]
    a = max(pd.Timestamp(START), first)
    for freq in ("daily", "monthly"):
        Sf = S if freq == "daily" else month_end_hold(S)
        # базовые линии на том же окне
        for lag in (0, 1):
            ret_b, p_b, t_b = backtest(pos_panel_d if freq == "daily" else pos_panel_m, COST, lag)
            mb = metrics(ret_b, p_b, t_b, "panel", a, END)
            ret_h, p_h, t_h = backtest(pd.Series(1.0, index=idx), COST, lag)
            mh = metrics(ret_h, p_h, t_h, "bh", a, END)
            for k in (0.5, 1.0, 1.5):
                for kind in ("veto", "entry", "both", "solo"):
                    if kind == "solo" and k != 0.5:
                        continue
                    n_variants += 1
                    base_pos = pos_panel_d if freq == "daily" else pos_panel_m
                    if kind == "veto":
                        pos = base_pos * (Sf > -k).astype(float)
                    elif kind == "entry":
                        pos = ((base_pos > 0) | (Sf > k)).astype(float)
                    elif kind == "both":
                        pos = (((base_pos > 0) | (Sf > k)) & (Sf > -k)).astype(float)
                    else:
                        pos = (Sf > 0).astype(float)
                    pos = pos.where(Sf.notna(), base_pos)   # до старта сигнала — правило панели
                    ret, p, tr = backtest(pos, COST, lag)
                    m = metrics(ret, p, tr, f"{sid}|{kind}|k={k}|{freq}|lag{lag}", a, END)
                    # половины окна сигнала
                    mid = S.dropna().index[len(S.dropna()) // 2]
                    m1 = metrics(ret, p, tr, "h1", a, mid); m2 = metrics(ret, p, tr, "h2", mid, END)
                    mb1 = metrics(ret_b, p_b, t_b, "b1", a, mid); mb2 = metrics(ret_b, p_b, t_b, "b2", mid, END)
                    m.update(signal=sid, kind=kind, k=k, freq=freq, lag=lag, start=a.date(),
                             d_sharpe=round(m["sharpe"] - mb["sharpe"], 3), d_cagr=round(m["cagr"] - mb["cagr"], 2),
                             d_sharpe_ex=round(m["sharpe_ex"] - mb["sharpe_ex"], 3), panel_sharpe_ex=mb["sharpe_ex"],
                             bh_sharpe_ex=mh["sharpe_ex"],
                             d_sharpe_ex_h1=round(m1.get("sharpe_ex", np.nan) - mb1.get("sharpe_ex", np.nan), 3),
                             d_sharpe_ex_h2=round(m2.get("sharpe_ex", np.nan) - mb2.get("sharpe_ex", np.nan), 3),
                             d_maxdd=round(m["maxdd"] - mb["maxdd"], 2),
                             panel_sharpe=mb["sharpe"], panel_cagr=mb["cagr"], panel_maxdd=mb["maxdd"],
                             bh_sharpe=mh["sharpe"], bh_cagr=mh["cagr"], bh_maxdd=mh["maxdd"],
                             d_sharpe_h1=round(m1.get("sharpe", np.nan) - mb1.get("sharpe", np.nan), 3),
                             d_sharpe_h2=round(m2.get("sharpe", np.nan) - mb2.get("sharpe", np.nan), 3))
                    orows.append(m)
ov = pd.DataFrame(orows)
ov.to_csv(os.path.join(R, "E_overlays.csv"), index=False)
print(f"\n=== ОВЕРЛЕИ: вариантов {n_variants} (сигналов {len(CANDS)} x (3 порога x 3 типа + solo) x 2 частоты x 2 лага) ===")
cols = ["signal", "kind", "k", "freq", "lag", "start", "cagr", "sharpe", "sharpe_ex", "maxdd", "in_mkt", "trades_yr",
        "panel_cagr", "panel_sharpe_ex", "panel_maxdd", "d_cagr", "d_sharpe_ex", "d_maxdd", "d_sharpe_ex_h1", "d_sharpe_ex_h2", "hit_m_vs_bh"]
best = ov.sort_values("d_sharpe_ex", ascending=False)
print("Лучшие по приросту ИЗБЫТОЧНОГО Шарпа над денежным рынком (lag1 — консервативно):")
print(best[best.lag == 1][cols].head(25).to_string(index=False))
print("\nЛучшие по приросту сырого Шарпа (lag1):")
print(ov[ov.lag == 1].sort_values("d_sharpe", ascending=False)[cols + ["d_sharpe"]].head(10).to_string(index=False))
print("\nДоля вариантов с приростом изб. Шарпа > 0:", round((ov.d_sharpe_ex > 0).mean(), 3),
      "; с приростом в ОБЕИХ половинах:", round(((ov.d_sharpe_ex_h1 > 0) & (ov.d_sharpe_ex_h2 > 0)).mean(), 3),
      "; с приростом CAGR > 0:", round((ov.d_cagr > 0).mean(), 3))
print("По сигналам: медиана/макс d_sharpe_ex, доля вариантов с приростом в обеих половинах, медиана d_cagr, d_maxdd")
g = ov.groupby("signal").agg(med_d_sh_ex=("d_sharpe_ex", "median"), max_d_sh_ex=("d_sharpe_ex", "max"),
                             both_halves=("d_sharpe_ex_h1", lambda x: ((x > 0) & (ov.loc[x.index, "d_sharpe_ex_h2"] > 0)).mean()),
                             med_d_cagr=("d_cagr", "median"), med_d_maxdd=("d_maxdd", "median"), n=("d_cagr", "size"))
print(g.sort_values("med_d_sh_ex", ascending=False).round(3).to_string())

# ---------------------------------------------------------------- эпизоды и своевременность
px = panel.imoex[(panel.index >= "2020-01-01")]
thr = 0.15
# зигзаг: чередование пиков и дн с порогом 15%
piv = []  # (type, date, price)
mode = None; ext_d = px.index[0]; ext_p = px.iloc[0]
for d, p in px.items():
    if mode is None:
        if p >= ext_p * (1 + thr):
            piv.append(("trough", ext_d, ext_p)); mode = "up"; ext_d, ext_p = d, p
        elif p <= ext_p * (1 - thr):
            piv.append(("peak", ext_d, ext_p)); mode = "down"; ext_d, ext_p = d, p
        else:
            if p > ext_p: pass
            ext_d, ext_p = (d, p) if (p > ext_p) else (ext_d, ext_p)
    elif mode == "up":
        if p > ext_p:
            ext_d, ext_p = d, p
        elif p <= ext_p * (1 - thr):
            piv.append(("peak", ext_d, ext_p)); mode = "down"; ext_d, ext_p = d, p
    else:
        if p < ext_p:
            ext_d, ext_p = d, p
        elif p >= ext_p * (1 + thr):
            piv.append(("trough", ext_d, ext_p)); mode = "up"; ext_d, ext_p = d, p
piv.append(("peak" if mode == "up" else "trough", ext_d, ext_p))
episodes = []
for i in range(len(piv) - 1):
    t0, d0, p0 = piv[i]; t1, d1, p1 = piv[i + 1]
    episodes.append(dict(leg="decline" if t0 == "peak" else "rally", start=d0, end=d1, p_start=p0, p_end=p1,
                         move=round((p1 / p0 - 1) * 100, 1), days=int(((px.index > d0) & (px.index <= d1)).sum())))
ep = pd.DataFrame(episodes)
ep.to_csv(os.path.join(R, "E_episodes.csv"), index=False)
print("\n=== ЭПИЗОДЫ (зигзаг 15%, IMOEX) ===")
print(ep.to_string(index=False))


def timeliness(pos, name):
    """Для каждого плеча зигзага: react_days = торговых дней после экстремума до первой «правильной» позиции
    (флэт после пика / лонг после дна); отрицательное = позиция уже была правильной, и это давность её
    установления. avoided/missed = 1 - (лог-доходность стратегии по IMOEX на плече / лог-движение индекса)."""
    pos = pos.reindex(idx).fillna(0.0)
    out = []
    for e in episodes:
        d0, d1, p0, p1 = e["start"], e["end"], e["p_start"], e["p_end"]
        leg_idx = px.index[(px.index > d0) & (px.index <= d1)]
        strat = float((pos.shift(1).reindex(leg_idx) * r_imoex.reindex(leg_idx)).sum())
        legmove = float(np.log(p1 / p0))
        share = strat / legmove if legmove != 0 else np.nan
        seg = pos[(pos.index >= d0) & (pos.index <= d1)]
        want = 0.0 if e["leg"] == "decline" else 1.0
        if pos.loc[d0] == want:
            before = pos[pos.index <= d0]
            sw = before[(before == want) & (before.shift(1) != want)]
            since = sw.index[-1] if len(sw) else None
            react = -int(((px.index > since) & (px.index <= d0)).sum()) if since is not None else None
        else:
            ok = seg[seg == want]
            react = int(((px.index > d0) & (px.index <= ok.index[0])).sum()) if len(ok) else None
        rec = dict(rule=name, leg=e["leg"], start=d0.date(), end=d1.date(), move=e["move"], days=e["days"],
                   react_days=react, strat_move=round((np.exp(strat) - 1) * 100, 1),
                   in_mkt=round(float(pos.shift(1).reindex(leg_idx).mean()) * 100, 0))
        if e["leg"] == "decline":
            rec["avoided_pct"] = round((1 - share) * 100, 1)
        else:
            rec["missed_pct"] = round((1 - share) * 100, 1)
        out.append(rec)
    return out


trows = []
trows += timeliness(pos_panel_m, "панель (месячное)")
trows += timeliness(pos_panel_d, "панель (ворота ежедневно)")
# лучшие оверлеи по устойчивому приросту: обе половины > 0, lag1, среди daily и monthly
stable = ov[(ov.d_sharpe_ex_h1 > 0) & (ov.d_sharpe_ex_h2 > 0) & (ov.lag == 1)].sort_values("d_sharpe_ex", ascending=False)
picked = []
for _, r in stable.iterrows():
    if r.signal in [p[0] for p in picked]:
        continue
    picked.append((r.signal, r.kind, r.k, r.freq))
    if len(picked) >= 6:
        break
for sid, kind, k, freq in picked:
    sgn, is_z = [(c[1], c[2]) for c in CANDS if c[0] == sid][0]
    S = oriented(sid, sgn, is_z); Sf = S if freq == "daily" else month_end_hold(S)
    base_pos = pos_panel_d if freq == "daily" else pos_panel_m
    if kind == "veto":
        pos = base_pos * (Sf > -k).astype(float)
    elif kind == "entry":
        pos = ((base_pos > 0) | (Sf > k)).astype(float)
    elif kind == "both":
        pos = (((base_pos > 0) | (Sf > k)) & (Sf > -k)).astype(float)
    else:
        pos = (Sf > 0).astype(float)
    pos = pos.where(Sf.notna(), base_pos)
    trows += timeliness(pos, f"{sid}|{kind}|k={k}|{freq}")
tl = pd.DataFrame(trows)
tl.to_csv(os.path.join(R, "E_timeliness.csv"), index=False)
print("\n=== СВОЕВРЕМЕННОСТЬ (react_days: торговых дней после пика/дна до смены позиции; отрицательное = позиция сменена ДО экстремума) ===")
print(tl.to_string(index=False))
