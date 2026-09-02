"""S2 — находка 1: гистерезис знака композита 0,4 на месячном шаге против 0,1–0,2 на недельном.

(a) сетка порогов 0…0,8 на месячном и недельном шаге; (b) leave-one-year-out и leave-one-episode-out;
(c) плацебо AR(1)-двойников + reality-check бутстреп (плата за выбор порога); (d) механизм;
(e) объединяющие конструкции. Запуск из audit/: python scripts/S2_1_hyst.py
"""
import sys
sys.path.insert(0, "scripts")
import numpy as np
import pandas as pd
from S2_lib import *  # noqa

D, C, M = load_all()
cuts = month_end_cuts(D.index)
wcuts = week_end_cuts(D.index)
wcuts = wcuts[wcuts <= cuts[-1]]  # до последнего закрытого месяца
log = open(f"{RES}/S2_1_log.txt", "w", encoding="utf-8")


def P(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    log.write(s + "\n")


# ------------------------------------------------------------ 0. композит независимо
raw_m = pd.DataFrame({k: D[k].reindex(cuts) for k, _ in LEGS_PROD})
Zm = pd.DataFrame({k: sgn * zroll(raw_m[k]) for k, sgn in LEGS_PROD})
comp_m, n_used = composite_mean(Zm)
ref = M["composite"].reindex(cuts)
diff = (comp_m - ref).abs().max()
P(f"[0] композит воспроизведён независимо: max|diff| к panel_prod_monthly = {diff:.2e} на {comp_m.notna().sum()} срезах")

# дневной композит «как в проде» (M_live) и недельный по пятницам
Zd = pd.DataFrame({k: sgn * live_daily_z(D[k], cuts) for k, sgn in LEGS_PROD})
comp_d, _ = composite_mean(Zd)
chk = (comp_d.reindex(cuts) - comp_m).abs().max()
P(f"[0] дневной M_live на срезах = месячному: max|diff| {chk:.2e}")
comp_w = comp_d.reindex(wcuts)
# версия W: недельные сырые ноги, z по 260 нед / min 104
raw_w = pd.DataFrame({k: D[k].reindex(wcuts) for k, _ in LEGS_PROD})
Zw = pd.DataFrame({k: sgn * zroll(raw_w[k], w=260, mp=104) for k, sgn in LEGS_PROD})
comp_W, _ = composite_mean(Zw)

gate_m = (D["cell"].reindex(cuts) != TOXIC).astype(float)
gate_w = (D["cell"].reindex(wcuts) != TOXIC).astype(float)

# быстрые движки на срезах (эквивалентны дневному для решений на срезах)
tr = C["mcftr_ffill"]
mmc = (1 + C["mm_rate"] / 100.0 / 252.0).cumprod()


def fwd_at(cutsx):
    t = tr.reindex(cutsx)
    m = mmc.reindex(cutsx)
    px = D["imoex"].reindex(cutsx)
    return pd.DataFrame({"fwd_tr": t.shift(-1) / t - 1, "fwd_mm": m.shift(-1) / m - 1,
                         "fwd_imoex": np.log(px.shift(-1) / px)})


FM = fwd_at(cuts)
FW = fwd_at(wcuts)


def fast_ret(pos, F, cost=COST):
    """pos на срезе t (0/1) → доходность t→t+1; издержка на смене; первый срез — без сделки."""
    p = pos.reindex(F.index).fillna(0).values
    tr_ = np.abs(np.diff(np.r_[p[0], p]))
    r = p * F["fwd_tr"].values + (1 - p) * F["fwd_mm"].values - cost * tr_
    return pd.Series(r, index=F.index), pd.Series(tr_, index=F.index)


def win(s, w):
    return s[(s.index >= pd.Timestamp(w[0])) & (s.index <= pd.Timestamp(w[1]))]


def ex2022(s):
    return s[s.index.year != 2022]


def weekly_to_monthly(r):
    """Недельные доходности → календарные месяцы (по дате конца недели)."""
    g = r.groupby(r.index.to_period("M"))
    return g.apply(lambda x: (1 + x).prod() - 1)


def sh_monthly_from_weekly(r):
    return sharpe(weekly_to_monthly(r))


THR = np.round(np.arange(0.0, 0.81, 0.1), 2)
WIN_LIST = list(WINDOWS.items()) + [("ex-2022", None)]


def series_win(r, name, w):
    return ex2022(win(r, MAIN)) if name == "ex-2022" else win(r, w)


# ------------------------------------------------------------ (a) сетка порогов
rows = []
pos_m = {}
ret_m = {}
for thr in THR:
    sgn = hysteresis_sign(comp_m, thr)
    pos = ((sgn > 0) & (gate_m > 0)).astype(float)
    pos_core = (sgn > 0).astype(float)
    r, t = fast_ret(pos, FM)
    rc, tc = fast_ret(pos_core, FM)
    pos_m[thr], ret_m[thr] = pos, r
    for name, w in WIN_LIST:
        rr = series_win(r, name, w)
        rcc = series_win(rc, name, w)
        tt = series_win(t, name, w)
        yrs = len(rr) / 12
        rows.append(dict(step="monthly", composite="M_closed", window=name, thr=thr,
                         sharpe=sharpe(rr), cagr=(1 + rr).prod() ** (1 / yrs) - 1, mdd=maxdd(rr),
                         trades_yr=tt.sum() / yrs, time_in=series_win(pos, name, w).mean(),
                         sharpe_core=sharpe(rcc), mdd_core=maxdd(rcc), n=len(rr)))

# недельный шаг: два композита (M_live по пятницам, W)
pos_w = {}
ret_w = {}
for cname, cw in [("M_live", comp_w), ("W", comp_W)]:
    for thr in THR:
        sgn = hysteresis_sign(cw, thr)
        pos = ((sgn > 0) & (gate_w > 0)).astype(float)
        pos_core = (sgn > 0).astype(float)
        r, t = fast_ret(pos, FW)
        rc, tc = fast_ret(pos_core, FW)
        if cname == "M_live":
            pos_w[thr], ret_w[thr] = pos, r
        for name, w in WIN_LIST:
            rr = series_win(r, name, w)
            rcc = series_win(rc, name, w)
            tt = series_win(t, name, w)
            rm_ = weekly_to_monthly(rr)
            yrs = len(rm_) / 12
            rows.append(dict(step="weekly", composite=cname, window=name, thr=thr,
                             sharpe=sharpe(rm_), cagr=(1 + rr).prod() ** (1 / yrs) - 1, mdd=maxdd(rr),
                             trades_yr=tt.sum() / yrs, time_in=series_win(pos, name, w).mean(),
                             sharpe_core=sh_monthly_from_weekly(rcc), mdd_core=maxdd(rcc), n=len(rm_),
                             sharpe_weekly=sharpe(rr, 52)))
grid = pd.DataFrame(rows)
grid.to_csv(f"{RES}/S2_hyst_grid.csv", index=False, float_format="%.4f")

P("\n[a] Шарп панели (ворота + знак) по порогам, месячный шаг (композит закрытого месяца):")
pv = grid[(grid.step == "monthly")].pivot(index="window", columns="thr", values="sharpe").loc[[n for n, _ in WIN_LIST]]
P(pv.round(2).to_string())
P("\n[a] сделок/год, месячный:")
P(grid[(grid.step == "monthly") & (grid.window == "MAIN 2010-26")].set_index("thr")[["trades_yr", "time_in", "mdd", "cagr"]].round(3).T.to_string())
P("\n[a] Шарп (месячные агрегаты), недельный шаг, композит M_live по пятницам:")
pv = grid[(grid.step == "weekly") & (grid.composite == "M_live")].pivot(index="window", columns="thr", values="sharpe").loc[[n for n, _ in WIN_LIST]]
P(pv.round(2).to_string())
P("\n[a] Шарп (месячные агрегаты), недельный шаг, композит W (z по 260 нед):")
pv = grid[(grid.step == "weekly") & (grid.composite == "W")].pivot(index="window", columns="thr", values="sharpe").loc[[n for n, _ in WIN_LIST]]
P(pv.round(2).to_string())
P("\n[a] ядро без ворот, Шарп MAIN: месячный / недельный M_live:")
P(grid[(grid.step == "monthly") & (grid.window == "MAIN 2010-26")].set_index("thr")["sharpe_core"].round(2).to_string())
P(grid[(grid.step == "weekly") & (grid.composite == "M_live") & (grid.window == "MAIN 2010-26")].set_index("thr")["sharpe_core"].round(2).to_string())

# бутстреп разности к 0,1 на MAIN
P("\n[a] бутстреп ΔШарп к порогу 0,1 (MAIN, блок 8 мес, 2000 реплик):")
bs = []
r01 = win(ret_m[0.1], MAIN)
for thr in THR:
    d, p, lo, hi = boot_sharpe_diff(win(ret_m[thr], MAIN).values, r01.values)
    bs.append(dict(step="monthly", thr=thr, dsharpe=d, p=p, ci90_lo=lo, ci90_hi=hi))
r01w = weekly_to_monthly(win(ret_w[0.1], MAIN))
for thr in THR:
    d, p, lo, hi = boot_sharpe_diff(weekly_to_monthly(win(ret_w[thr], MAIN)).values, r01w.values)
    bs.append(dict(step="weekly", thr=thr, dsharpe=d, p=p, ci90_lo=lo, ci90_hi=hi))
bs = pd.DataFrame(bs)
bs.to_csv(f"{RES}/S2_hyst_boot.csv", index=False, float_format="%.4f")
P(bs.round(3).to_string())

# ------------------------------------------------------------ (b) leave-one-year-out
P("\n[b] leave-one-year-out (MAIN): ΔШарп(0,4−0,1) без года Y; и кросс-валидация выбора порога")
years = sorted(set(win(ret_m[0.1], MAIN).index.year))
Rm = pd.DataFrame({thr: win(ret_m[thr], MAIN) for thr in THR})
Rw = pd.DataFrame({thr: weekly_to_monthly(win(ret_w[thr], MAIN)) for thr in THR})
loyo = []
cv_ret_m = pd.Series(index=Rm.index, dtype=float)
cv_ret_w = pd.Series(index=Rw.index, dtype=float)
cv_pick_m, cv_pick_w = {}, {}
for y in years:
    tr_m = Rm[Rm.index.year != y]
    te_m = Rm[Rm.index.year == y]
    sh_tr = tr_m.apply(sharpe)
    pick = float(sh_tr.idxmax())
    cv_pick_m[y] = pick
    cv_ret_m.loc[te_m.index] = te_m[pick]
    tr_w = Rw[Rw.index.year != y]
    te_w = Rw[Rw.index.year == y]
    pick_w = float(tr_w.apply(sharpe).idxmax())
    cv_pick_w[y] = pick_w
    cv_ret_w.loc[te_w.index] = te_w[pick_w]
    loyo.append(dict(year_out=y, sh01_m=sh_tr[0.1], sh04_m=sh_tr[0.4], d_m=sh_tr[0.4] - sh_tr[0.1],
                     best_thr_m=pick, best_sh_m=sh_tr.max(),
                     sh01_w=tr_w.apply(sharpe)[0.1], sh02_w=tr_w.apply(sharpe)[0.2], sh04_w=tr_w.apply(sharpe)[0.4],
                     best_thr_w=pick_w,
                     year_ret_01=(1 + te_m[0.1]).prod() - 1, year_ret_04=(1 + te_m[0.4]).prod() - 1,
                     year_ret_w02=(1 + te_w[0.2]).prod() - 1))
loyo = pd.DataFrame(loyo)
loyo.to_csv(f"{RES}/S2_hyst_loyo.csv", index=False, float_format="%.4f")
P(loyo.round(3).to_string())
P(f"  ΔШарп(0,4−0,1) без года: min {loyo.d_m.min():.2f}, max {loyo.d_m.max():.2f}, все > 0: {bool((loyo.d_m > 0).all())}")
P(f"  CV-выбор порога, месячный: пороги по годам {cv_pick_m}")
P(f"  CV-стратегия (порог выбран без тестового года): Шарп {sharpe(cv_ret_m):.2f} против 0,1: {sharpe(Rm[0.1]):.2f}, 0,4 in-sample: {sharpe(Rm[0.4]):.2f}")
P(f"  CV-выбор порога, недельный: {cv_pick_w}")
P(f"  CV-стратегия недельная: Шарп {sharpe(cv_ret_w):.2f} против 0,1: {sharpe(Rw[0.1]):.2f}, 0,2: {sharpe(Rw[0.2]):.2f}")

# walk-forward (расширяющееся окно, min 60 мес, выбор порога по max Шарпа на прошлом)
def walk_forward(R, min_n=60):
    out = pd.Series(index=R.index, dtype=float)
    picks = []
    for i in range(min_n, len(R)):
        past = R.iloc[:i]
        pick = float(past.apply(sharpe).idxmax())
        out.iloc[i] = R.iloc[i][pick]
        picks.append((R.index[i], pick))
    return out.dropna(), picks


Rm_full = pd.DataFrame({thr: win(ret_m[thr], FULL) for thr in THR})
wf, picks = walk_forward(Rm_full)
wf_main = wf[wf.index >= "2010-01-01"]
P(f"  walk-forward (с 2004, min 60 мес): Шарп на 2010+ {sharpe(wf_main):.2f}; пороги по годам: "
  + str({y: sorted(set(p for d, p in picks if d.year == y)) for y in range(2009, 2027)}))

# leave-one-episode-out
P("\n[b] leave-one-episode-out (MAIN, месячный): Шарп 0,1 / 0,4 / Δ без месяца(ев)")
diffm = (Rm[0.4] - Rm[0.1])
top = diffm.abs().sort_values(ascending=False).head(8)
epi_rows = []
# ВНИМАНИЕ: индекс = дата СРЕЗА (решения); доходность сентября 2022 лежит на срезе 2022-08-31
for label, drop in [("полная", []), ("без сентября 2022 (срез 2022-08)", ["2022-08"]),
                    ("без 2022 (срезы 2021-12…2022-11)", ["2021-12"] + [f"2022-{m:02d}" for m in range(1, 12)]),
                    ("без сентября 2011 (срез 2011-08)", ["2011-08"]), ("без февраля 2017 (срез 2017-01)", ["2017-01"]),
                    ("без июня–июля 2026 (срезы 2026-05/06)", ["2026-05", "2026-06"]),
                    ("без 3 лучших для 0,4", [d.strftime("%Y-%m") for d in diffm.sort_values(ascending=False).head(3).index]),
                    ("без 3 худших для 0,4", [d.strftime("%Y-%m") for d in diffm.sort_values().head(3).index]),
                    ("без 5 лучших для 0,4", [d.strftime("%Y-%m") for d in diffm.sort_values(ascending=False).head(5).index])]:
    mask = ~Rm.index.strftime("%Y-%m").isin(drop)
    sub = Rm[mask]
    subw = Rw[~Rw.index.strftime("%Y-%m").isin(drop)]
    d, p, lo, hi = boot_sharpe_diff(sub[0.4].values, sub[0.1].values)
    epi_rows.append(dict(episode=label, dropped=",".join(drop) if len(drop) <= 3 else f"{len(drop)} мес",
                         sh01=sharpe(sub[0.1]), sh04=sharpe(sub[0.4]), sh05=sharpe(sub[0.5]), d04=d, p=p,
                         sh_w01=sharpe(subw[0.1]), sh_w02=sharpe(subw[0.2]), sh_w04=sharpe(subw[0.4])))
epi = pd.DataFrame(epi_rows)
epi.to_csv(f"{RES}/S2_hyst_loeo.csv", index=False, float_format="%.4f")
P(epi.round(3).to_string())

# ------------------------------------------------------------ (d) механизм: месяцы расхождений
P("\n[d] механизм: месячные позиции 0,1 vs 0,4")
p01, p04 = win(pos_m[0.1], MAIN), win(pos_m[0.4], MAIN)
s01 = hysteresis_sign(comp_m, 0.1)
s04 = hysteresis_sign(comp_m, 0.4)
flips01 = int((s01.diff().abs() > 0).sum())
flips04 = int((s04.diff().abs() > 0).sum())
flips01m = int((win(s01, MAIN).diff().abs() > 0).sum())
flips04m = int((win(s04, MAIN).diff().abs() > 0).sum())
P(f"  разворотов знака: 0,1 — {flips01} (MAIN {flips01m}); 0,4 — {flips04} (MAIN {flips04m}); убрано порогом 0,4: {flips01m - flips04m} на MAIN")
diffmo = pd.DataFrame({"composite": win(comp_m, MAIN), "gate": win(gate_m, MAIN), "pos01": p01, "pos04": p04,
                       "sign01": win(s01, MAIN), "sign04": win(s04, MAIN),
                       "fwd_tr": win(FM["fwd_tr"], MAIN), "fwd_mm": win(FM["fwd_mm"], MAIN),
                       "ret01": Rm[0.1], "ret04": Rm[0.4]})
diffmo["gain04"] = diffmo["ret04"] - diffmo["ret01"]
dd = diffmo[diffmo.pos01 != diffmo.pos04].copy()
dd.to_csv(f"{RES}/S2_hyst_diff_months.csv", float_format="%.4f")
P(f"  месяцев с разной позицией: {len(dd)}; суммарный выигрыш 0,4: {dd.gain04.sum()*100:+.1f} п.п.; "
  f"из них 0,4 во флэте, а 0,1 в лонге: {int((dd.pos04 == 0).sum())} мес ({dd[dd.pos04 == 0].gain04.sum()*100:+.1f} п.п.), "
  f"0,4 в лонге, а 0,1 во флэте: {int((dd.pos04 == 1).sum())} мес ({dd[dd.pos04 == 1].gain04.sum()*100:+.1f} п.п.)")
P("  топ месяцев по |выигрышу|:")
P(dd.reindex(dd.gain04.abs().sort_values(ascending=False).index).head(12)[["composite", "gate", "pos01", "pos04", "fwd_tr", "gain04"]].round(3).to_string())
tot = dd.gain04.sum()
top1 = dd.gain04.max()
P(f"  доля лучшего месяца в сумме выигрыша: {top1/tot:.0%}; доля топ-3: {dd.gain04.sort_values(ascending=False).head(3).sum()/tot:.0%}; "
  f"медиана выигрыша по месяцам расхождения: {dd.gain04.median()*100:+.2f} п.п.; доля месяцев с плюсом: {(dd.gain04 > 0).mean():.0%}")

# «дребезг»: развороты знака при 0,1, которые 0,4 не повторил; и задержки
P("\n[d] какие развороты 0,1 порог 0,4 убрал (знак 0,1 сменился, 0,4 остался) — и что было с рынком дальше (3 мес MCFTR):")
ch = win(s01, MAIN)
ch04 = win(s04, MAIN)
fl = ch[(ch.diff().abs() > 0)]
rows = []
for d in fl.index:
    followed = ch04.loc[d] == ch.loc[d]
    # когда 0,4 догнал
    later = ch04[(ch04.index > d) & (ch04 == ch.loc[d])]
    lag = None
    if not followed and len(later):
        lag = int(((later.index[0].year - d.year) * 12 + later.index[0].month - d.month))
    # знак 0,1 через 3 месяца: разворот обратно?
    nxt = ch[(ch.index > d)]
    reverted_in = None
    for k, (d2, v2) in enumerate(nxt.items()):
        if v2 != ch.loc[d]:
            reverted_in = k + 1
            break
    i = list(FM.index).index(d)
    r3 = (1 + FM["fwd_tr"].iloc[i:i + 3]).prod() - 1 - ((1 + FM["fwd_mm"].iloc[i:i + 3]).prod() - 1)
    rows.append(dict(date=d.date(), new_sign01=int(ch.loc[d]), composite=round(comp_m.loc[d], 3), sign04_same=bool(followed),
                     lag04_months=lag, reverted01_after=reverted_in, fwd3m_tr_minus_mm=round(r3, 4)))
fl_df = pd.DataFrame(rows)
fl_df.to_csv(f"{RES}/S2_hyst_flips.csv", index=False)
P(fl_df.to_string())
P(f"  из {len(fl_df)} разворотов 0,1 на MAIN порог 0,4 сразу повторил {int(fl_df.sign04_same.sum())}; "
  f"с задержкой (мес): {fl_df.lag04_months.dropna().astype(int).tolist()}; вовсе не повторил (дребезг): {int((~fl_df.sign04_same & fl_df.lag04_months.isna()).sum())}")
notf = fl_df[~fl_df.sign04_same]
P(f"  развороты 0,1, не повторённые сразу: 0,1 сам развернулся обратно в среднем через {notf.reverted01_after.mean():.1f} мес (медиана {notf.reverted01_after.median()}); "
  f"средний 3-мес избыток MCFTR над деньгами со знаком нового знака: "
  f"{(notf.new_sign01 * notf.fwd3m_tr_minus_mm).mean()*100:+.2f} п.п. против {(fl_df[fl_df.sign04_same].new_sign01 * fl_df[fl_df.sign04_same].fwd3m_tr_minus_mm).mean()*100:+.2f} у повторённых")

# недельные ложные пробои
P("\n[d] недельный шаг: внутримесячные пробои ±0,4 композитом M_live по пятницам, которых нет на срезе")
wk = pd.DataFrame({"c": comp_w})
wk["ym"] = wk.index.to_period("M")
me = pd.Series(comp_m.values, index=comp_m.index.to_period("M"))
me_prev = me.shift(1)
rows = []
for ym, g in wk.groupby("ym"):
    if ym not in me.index or not np.isfinite(me.get(ym, np.nan)):
        continue
    c_end = me[ym]
    c_prev = me_prev.get(ym, np.nan)
    mx, mn = g["c"].max(), g["c"].min()
    rows.append(dict(ym=str(ym), c_prev=c_prev, c_end=c_end, wmax=mx, wmin=mn,
                     false_up=(mx > 0.4) and not (c_end > 0.4), false_dn=(mn < -0.4) and not (c_end < -0.4),
                     false_up01=(mx > 0.1) and not (c_end > 0.1), false_dn01=(mn < -0.1) and not (c_end < -0.1),
                     range_w=mx - mn))
br = pd.DataFrame(rows).set_index("ym")
br = br[br.index >= "2010-01"]
br.to_csv(f"{RES}/S2_hyst_weekly_breakouts.csv", float_format="%.4f")
P(f"  месяцев с пятничным пробоем >+0,4, когда срез ≤0,4: {int(br.false_up.sum())}; пробой <−0,4 при срезе ≥−0,4: {int(br.false_dn.sum())}; "
  f"итого {int((br.false_up | br.false_dn).sum())} из {len(br)} ({(br.false_up | br.false_dn).mean():.0%}); "
  f"для порога 0,1: {int((br.false_up01 | br.false_dn01).sum())} ({(br.false_up01 | br.false_dn01).mean():.0%}); "
  f"медианный размах композита внутри месяца по пятницам: {br.range_w.median():.2f}, σ месячного композита {win(comp_m, MAIN).std():.2f}")
# число разворотов на недельной сетке при разных порогах
for thr in [0.1, 0.2, 0.4]:
    sw = win(hysteresis_sign(comp_w, thr), MAIN)
    P(f"  разворотов знака на недельном шаге при пороге {thr}: {int((sw.diff().abs() > 0).sum())} за {len(sw)/52:.1f} лет; месячный при том же пороге: {int((win(hysteresis_sign(comp_m, thr), MAIN).diff().abs() > 0).sum())}")
# автокорреляция и σ недельного композита; вклад недельного шума
dw = comp_w.diff().dropna()
dm = comp_m.diff().dropna()
P(f"  AR(1) месячного композита {win(comp_m, MAIN).autocorr():.3f}, недельного {win(comp_w, MAIN).autocorr():.3f}; σ шага: месячный {win(dm, MAIN).std():.3f}, недельный {win(dw, MAIN).std():.3f}")

# ------------------------------------------------------------ (c) плацебо: AR(1)-двойники
P("\n[c] плацебо AR(1)-двойников композита (без связи с рынком), реальные ворота и доходности")
cm = win(comp_m, FULL).dropna()
rho = cm.autocorr()
sig_e = (cm - rho * cm.shift(1)).dropna().std()
mu = cm.mean()
P(f"  месячный: rho={rho:.3f}, σ_eps={sig_e:.3f}, mean={mu:.3f}, σ={cm.std():.3f}")
rng = np.random.default_rng(42)
NP = 1000
idx_full = FM.index[(FM.index >= comp_m.dropna().index[0]) & (FM.index <= pd.Timestamp(MAIN[1]))]
gate_full = gate_m.reindex(idx_full).fillna(1)
Ffull = FM.reindex(idx_full)
main_mask = idx_full >= pd.Timestamp(MAIN[0])
plc = []
for k in range(NP):
    e = rng.normal(0, sig_e, len(idx_full))
    x = np.empty(len(idx_full))
    x[0] = mu + rng.normal(0, cm.std())
    for i in range(1, len(x)):
        x[i] = mu * (1 - rho) + rho * x[i - 1] + e[i]
    xs = pd.Series(x, index=idx_full)
    shs = {}
    for thr in THR:
        sg = hysteresis_sign(xs, thr)
        pos = ((sg > 0) & (gate_full > 0)).astype(float)
        r, _ = fast_ret(pos, Ffull)
        shs[thr] = sharpe(r[main_mask])
    plc.append(shs)
plc = pd.DataFrame(plc)
plc["max"] = plc[THR].max(axis=1)
plc["gain_max_vs_01"] = plc["max"] - plc[0.1]
plc["gain_04_vs_01"] = plc[0.4] - plc[0.1]
plc.to_csv(f"{RES}/S2_hyst_placebo_monthly.csv", index=False, float_format="%.4f")
obs01, obs04, obsmax = sharpe(Rm[0.1]), sharpe(Rm[0.4]), Rm.apply(sharpe).max()
P(f"  наблюдаемое: Шарп(0,1)={obs01:.2f}, Шарп(0,4)={obs04:.2f}, max по сетке={obsmax:.2f}, прирост 0,4−0,1 = {obs04-obs01:+.2f}, max−0,1 = {obsmax-obs01:+.2f}")
P(f"  плацебо: Шарп(0,1) {plc[0.1].mean():.2f}±{plc[0.1].std():.2f}; max по сетке {plc['max'].mean():.2f}±{plc['max'].std():.2f} (95-й перцентиль {plc['max'].quantile(.95):.2f});")
P(f"  прирост 0,4−0,1: {plc.gain_04_vs_01.mean():+.2f}±{plc.gain_04_vs_01.std():.2f}, P(≥ наблюдаемого {obs04-obs01:+.2f}) = {(plc.gain_04_vs_01 >= obs04-obs01).mean():.3f}")
P(f"  плата за выбор порога: max−0,1 в плацебо {plc.gain_max_vs_01.mean():+.2f}±{plc.gain_max_vs_01.std():.2f}, 95-й перцентиль {plc.gain_max_vs_01.quantile(.95):+.2f}; P(≥ наблюдаемого {obsmax-obs01:+.2f}) = {(plc.gain_max_vs_01 >= obsmax-obs01).mean():.3f}")
P(f"  P(плацебо max по сетке ≥ наблюдаемого 1,53) = {(plc['max'] >= obsmax).mean():.3f} — ноль означает, что абсолютный уровень Шарпа от плацебо недостижим (ворота+рынок), но это не тест порога")

# reality check: бутстреп под H0 «все пороги равны 0,1» с максимумом по сетке
P("\n[c] reality-check (стационарный бутстреп матрицы месячных доходностей, блок 8, 2000 реплик): max по сетке ΔШарп к 0,1")
rngb = np.random.default_rng(7)
Rmat = Rm.values
n = len(Rmat)
base = Rm[0.1].values
obs_d = np.array([sharpe(Rmat[:, j]) - sharpe(base) for j in range(Rmat.shape[1])])
maxes = []
per = []
for k in range(2000):
    ix = stationary_bootstrap_indices(n, 8, rngb)
    d = np.array([sharpe(Rmat[ix, j]) - sharpe(base[ix]) for j in range(Rmat.shape[1])])
    per.append(d)
per = np.array(per)
cen = per - per.mean(axis=0)  # центрируем под H0
maxes = cen.max(axis=1)
P(f"  наблюдаемый max ΔШарп по сетке: {obs_d.max():+.2f} (порог {THR[obs_d.argmax()]}); p(reality check) = {(maxes >= obs_d.max()).mean():.3f}; "
  f"p для 0,4 без поправки: {(np.abs(cen[:, list(THR).index(0.4)]) >= abs(obs_d[list(THR).index(0.4)])).mean():.3f}")
# то же для недельного
Rwm = Rw.values
basew = Rw[0.1].values
obs_dw = np.array([sharpe(Rwm[:, j]) - sharpe(basew) for j in range(Rwm.shape[1])])
perw = []
for k in range(2000):
    ix = stationary_bootstrap_indices(len(Rwm), 8, rngb)
    perw.append([sharpe(Rwm[ix, j]) - sharpe(basew[ix]) for j in range(Rwm.shape[1])])
perw = np.array(perw)
cenw = perw - perw.mean(axis=0)
P(f"  недельный: наблюдаемый max ΔШарп к 0,1 по сетке {obs_dw.max():+.2f} (порог {THR[obs_dw.argmax()]}); p(reality check) = {(cenw.max(axis=1) >= obs_dw.max()).mean():.3f}")

# плацебо недельный
P("\n[c] плацебо AR(1)-двойников на недельном шаге (M_live по пятницам)")
cw = win(comp_w, FULL).dropna()
rho_w = cw.autocorr()
sig_w = (cw - rho_w * cw.shift(1)).dropna().std()
mu_w = cw.mean()
P(f"  недельный: rho={rho_w:.3f}, σ_eps={sig_w:.3f}")
idxw = FW.index[(FW.index >= cw.index[0]) & (FW.index <= pd.Timestamp(MAIN[1]))]
gw = gate_w.reindex(idxw).fillna(1)
Fw_ = FW.reindex(idxw)
mask_w = idxw >= pd.Timestamp(MAIN[0])
plw = []
for k in range(400):
    e = rng.normal(0, sig_w, len(idxw))
    x = np.empty(len(idxw))
    x[0] = mu_w + rng.normal(0, cw.std())
    for i in range(1, len(x)):
        x[i] = mu_w * (1 - rho_w) + rho_w * x[i - 1] + e[i]
    xs = pd.Series(x, index=idxw)
    shs = {}
    for thr in THR:
        sg = hysteresis_sign(xs, thr)
        pos = ((sg > 0) & (gw > 0)).astype(float)
        r, _ = fast_ret(pos, Fw_)
        shs[thr] = sh_monthly_from_weekly(r[mask_w])
    plw.append(shs)
plw = pd.DataFrame(plw)
plw["max"] = plw[THR].max(axis=1)
plw.to_csv(f"{RES}/S2_hyst_placebo_weekly.csv", index=False, float_format="%.4f")
P(f"  плацебо недельный: Шарп(0,1) {plw[0.1].mean():.2f}±{plw[0.1].std():.2f}, Шарп(0,2) {plw[0.2].mean():.2f}, Шарп(0,4) {plw[0.4].mean():.2f}; max по сетке {plw['max'].mean():.2f}±{plw['max'].std():.2f}; "
  f"max−0,1: {(plw['max']-plw[0.1]).mean():+.2f}±{(plw['max']-plw[0.1]).std():.2f}; наблюдаемое: 0,1 {sharpe(Rw[0.1]):.2f}, 0,2 {sharpe(Rw[0.2]):.2f}, max {Rw.apply(sharpe).max():.2f}")

# ------------------------------------------------------------ (e) объединяющие конструкции
P("\n[e] объединяющие конструкции (полный дневной движок; метрики MAIN)")
ma3 = comp_m.rolling(3, min_periods=2).mean()
comp_w13 = comp_d.rolling(63, min_periods=40).mean().reindex(wcuts)   # ≈ MA3 месяцев на дневном ряду
comp_closed_w = comp_m.reindex(wcuts, method="ffill")                  # закрытый месяц, читаемый по пятницам
gate_closed_w = gate_m.reindex(wcuts, method="ffill")


def pos_from(sgn, gate):
    return ((sgn > 0) & (gate > 0)).astype(float)


def asym(x, enter, exit_):
    v = x.values
    out = np.full(len(v), np.nan)
    s = 0
    for i in range(len(v)):
        if np.isfinite(v[i]):
            if v[i] > enter:
                s = 1
            elif v[i] < exit_:
                s = -1
        if s:
            out[i] = s
    return pd.Series(out, index=x.index)


designs = {
    "P0 прод: месячный, 0,1": pos_from(hysteresis_sign(comp_m, 0.1), gate_m),
    "M 0,4": pos_from(hysteresis_sign(comp_m, 0.4), gate_m),
    "M 0,5": pos_from(hysteresis_sign(comp_m, 0.5), gate_m),
    "M знак MA3, 0,1": pos_from(hysteresis_sign(ma3, 0.1), gate_m),
    "M знак MA3, 0,0": pos_from(hysteresis_sign(ma3, 0.0), gate_m),
    "M 0,4 + ворота по пятницам": pos_from(hysteresis_sign(comp_m, 0.4).reindex(wcuts, method="ffill"), gate_w),
    "M 0,1 + ворота по пятницам": pos_from(hysteresis_sign(comp_m, 0.1).reindex(wcuts, method="ffill"), gate_w),
    "W 0,1": pos_from(hysteresis_sign(comp_w, 0.1), gate_w),
    "W 0,2 (рек. H)": pos_from(hysteresis_sign(comp_w, 0.2), gate_w),
    "W 0,3": pos_from(hysteresis_sign(comp_w, 0.3), gate_w),
    "W 0,4": pos_from(hysteresis_sign(comp_w, 0.4), gate_w),
    "W 0,2 + подтверждение 2 нед": pos_from(hysteresis_sign_confirm(comp_w, 0.2, 2), gate_w),
    "W 0,2 + подтверждение 3 нед": pos_from(hysteresis_sign_confirm(comp_w, 0.2, 3), gate_w),
    "W 0,4 + подтверждение 2 нед": pos_from(hysteresis_sign_confirm(comp_w, 0.4, 2), gate_w),
    "W 0,3 + подтверждение 2 нед": pos_from(hysteresis_sign_confirm(comp_w, 0.3, 2), gate_w),
    "W знак MA63д, 0,1": pos_from(hysteresis_sign(comp_w13, 0.1), gate_w),
    "W знак MA63д, 0,2": pos_from(hysteresis_sign(comp_w13, 0.2), gate_w),
    "W асимм. вход>0,4 выход<0": pos_from(asym(comp_w, 0.4, 0.0), gate_w),
    "W асимм. вход>0,4 выход<−0,2": pos_from(asym(comp_w, 0.4, -0.2), gate_w),
    "W асимм. вход>0,2 выход<−0,4": pos_from(asym(comp_w, 0.2, -0.4), gate_w),
    "M асимм. вход>0,4 выход<0": pos_from(asym(comp_m, 0.4, 0.0), gate_m),
    "M асимм. вход>0,5 выход<0": pos_from(asym(comp_m, 0.5, 0.0), gate_m),
    "W: знак ⅔ (MA63д, 0,2) + ⅓ свежий (0,2), лонг если оба": pos_from(((hysteresis_sign(comp_w13, 0.2) > 0) & (hysteresis_sign(comp_w, 0.2) > 0)).astype(float) * 2 - 1, gate_w),
    "W: лонг если MA63д>0,2 ИЛИ свежий>0,4; флэт если оба <0": None,
}
# «или»-конструкция
sw13 = hysteresis_sign(comp_w13, 0.2)
sw = hysteresis_sign(comp_w, 0.4)
v = np.full(len(wcuts), np.nan)
s = 0
for i, d in enumerate(wcuts):
    a, b = sw13.iloc[i], sw.iloc[i]
    if (a > 0) or (b > 0):
        s = 1
    elif (a < 0) and (b < 0):
        s = -1
    v[i] = s if s else np.nan
designs["W: лонг если MA63д>0,2 ИЛИ свежий>0,4; флэт если оба <0"] = pos_from(pd.Series(v, index=wcuts), gate_w)

rows = []
dfs = {}
for name, pos in designs.items():
    m_main, df_main = run(pos, D, C, MAIN)
    dfs[name] = df_main
    row = dict(design=name, **{k: m_main[k] for k in ["cagr", "vol", "sharpe", "sharpe_ex", "mdd_daily", "mdd_monthly", "time_in", "trades_yr", "beat_years", "hit", "n_months"]})
    for wname, w in [("FULL", FULL), ("A", WINDOWS["A 2004-17"]), ("B", WINDOWS["B 2018-26"]), ("2010-21", WINDOWS["2010-21"]),
                     ("2022-24", WINDOWS["2022-24"]), ("2025-26", WINDOWS["2025-26"])]:
        mm_, _ = run(pos, D, C, w)
        row[f"sh_{wname}"] = mm_["sharpe"]
        row[f"mdd_{wname}"] = mm_["mdd_daily"]
    dm_ = to_monthly(df_main)
    row["sh_ex2022"] = sharpe(dm_["ret"][dm_.index.year != 2022])
    rows.append(row)
combos = pd.DataFrame(rows)
combos.to_csv(f"{RES}/S2_hyst_combos.csv", index=False, float_format="%.4f")
P(combos[["design", "cagr", "sharpe", "mdd_daily", "trades_yr", "time_in", "hit", "sh_FULL", "sh_A", "sh_B", "sh_2010-21", "sh_2022-24", "sh_2025-26", "sh_ex2022"]].round(2).to_string())

# бутстреп ΔШарп к проду для ключевых конструкций
P("\n[e] бутстреп ΔШарп к проду (MAIN, месячные агрегаты):")
base_m = to_monthly(dfs["P0 прод: месячный, 0,1"])["ret"]
brow = []
for name in ["M 0,4", "M знак MA3, 0,1", "W 0,2 (рек. H)", "W 0,2 + подтверждение 2 нед", "W 0,4 + подтверждение 2 нед", "W знак MA63д, 0,1",
             "W знак MA63д, 0,2", "M 0,4 + ворота по пятницам", "W асимм. вход>0,4 выход<0", "W: лонг если MA63д>0,2 ИЛИ свежий>0,4; флэт если оба <0",
             "W: знак ⅔ (MA63д, 0,2) + ⅓ свежий (0,2), лонг если оба", "W асимм. вход>0,4 выход<−0,2", "M 0,5", "W 0,1"]:
    r = to_monthly(dfs[name])["ret"].reindex(base_m.index)
    d, p, lo, hi = boot_sharpe_diff(r.values, base_m.values)
    brow.append(dict(design=name, dsharpe=d, p=p, ci90_lo=lo, ci90_hi=hi))
brow = pd.DataFrame(brow)
brow.to_csv(f"{RES}/S2_hyst_combos_boot.csv", index=False, float_format="%.4f")
P(brow.round(3).to_string())

# своевременность
P("\n[e] своевременность (просадки MCFTR >15 % с 2004; дней от пика до флэта — отрицательно = ушли раньше; доля избежанного; дней от дна до лонга; доля пропущенного)")
eps = drawdown_episodes(tr[tr.index >= "2004-01-01"], 0.15)
trows = []
for name in ["P0 прод: месячный, 0,1", "M 0,4", "M знак MA3, 0,1", "W 0,2 (рек. H)", "W 0,2 + подтверждение 2 нед", "W знак MA63д, 0,1", "M 0,4 + ворота по пятницам"]:
    _, dfull = run(designs[name], D, C, FULL)
    t = timeliness(dfull, tr, eps)
    t.insert(0, "design", name)
    trows.append(t)
tl = pd.concat(trows)
tl.to_csv(f"{RES}/S2_hyst_timeliness.csv", index=False, float_format="%.3f")
for name, g in tl.groupby("design", sort=False):
    P(f"  {name}: медиана дней до флэта {g.days_to_flat.median():+.0f}, избежано {g.avoided.median():.0%}, дней до лонга {g.days_to_long.median():.0f}, пропущено {g.missed.median():.0%}")
P(tl[tl.depth < -0.19].round(2).to_string())

log.close()
