"""E_algopack шаг 4d: устойчивость двух выживших кандидатов (HI2 nf-концентрация и средний размер сделки):
по годам, по подмножествам тикеров, по окнам сглаживания/нормировки, корреляция с сигналами панели и
частичный IC после контроля mom63/dd252/vol. Выход: results/E_robust.csv."""
import sys, os
import numpy as np, pandas as pd
from scipy import stats
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from testlib import nw_tstat, spearman_ic
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
D = os.path.join(ROOT, "data"); A = os.path.join(D, "algopack"); R = os.path.join(ROOT, "results")

feat = pd.read_csv(os.path.join(A, "E_features_daily.csv"), parse_dates=["date"]).set_index("date")
panel = pd.read_csv(os.path.join(D, "panel_prod_daily.csv"), parse_dates=["date"]).set_index("date").reindex(feat.index)
lp = np.log(panel.imoex)
fwd21 = lp.shift(-21) - lp; fwd63 = lp.shift(-63) - lp
ME = pd.DatetimeIndex(pd.Series(feat.index, index=feat.index).groupby([feat.index.year, feat.index.month]).last().values)


def zroll(s, win, minp):
    return (s - s.rolling(win, min_periods=minp).mean()) / s.rolling(win, min_periods=minp).std()


def ic_m(sig, f=fwd21):
    ic, p, n = spearman_ic(sig.reindex(ME).values, f.reindex(ME).values)
    return round(ic, 3), round(p, 3), n


def ic_d(sig, f=fwd21, h=21):
    ic, p, n = spearman_ic(sig.values, f.values)
    _, t, _ = nw_tstat(sig.values, f.values, h)
    return round(ic, 3), round(t, 2), n


rows = []
# ------------------------------------------------ HI2: варианты построения
hi = pd.read_csv(os.path.join(A, "E_hi2_daily.csv"), parse_dates=["date"])
hi = hi[hi.date.dt.weekday < 5]
hi["nf"] = hi.hhi_netflow_buy - hi.hhi_netflow_sell
hi["lnf"] = np.log(hi.hhi_netflow_buy) - np.log(hi.hhi_netflow_sell)
subsets = {"все12": None, "SBER,LKOH,GAZP": ["SBER", "LKOH", "GAZP"], "без SBER,LKOH,GAZP": None,
           "SBER": ["SBER"], "нефтегаз(LKOH,GAZP,ROSN,NVTK,TATN,SNGS)": ["LKOH", "GAZP", "ROSN", "NVTK", "TATN", "SNGS"],
           "прочие(GMKN,PLZL,MGNT,VTBR,MOEX)": ["GMKN", "PLZL", "MGNT", "VTBR", "MOEX"]}
for name, tk in subsets.items():
    sub = hi if tk is None else hi[hi.secid.isin(tk)]
    if name.startswith("без"):
        sub = hi[~hi.secid.isin(["SBER", "LKOH", "GAZP"])]
    nf = sub.groupby("date").nf.mean().reindex(feat.index)
    for win in (5, 10, 21, 42, 63):
        base = nf.rolling(win, min_periods=int(win * 0.7)).mean()
        for zw in (0, 126, 252, 504):
            s = base if zw == 0 else zroll(base, zw, zw // 2)
            icm, pm, nm = ic_m(s); icd, td, nd = ic_d(s)
            icm63, pm63, _ = ic_m(s, fwd63)
            rows.append(dict(family="hi2_nf", variant=f"{name}|win{win}|z{zw}", ic21_m=icm, p21_m=pm, n_m=nm,
                             ic21_d=icd, nw_t21=td, ic63_m=icm63, p63_m=pm63))
# ------------------------------------------------ средний размер сделки: варианты
ts = pd.read_csv(os.path.join(A, "E_tradestats_daily.csv"), parse_dates=["date"])
for name, tk in {"c3": ["SBER", "LKOH", "GAZP"], "SBER": ["SBER"], "LKOH": ["LKOH"], "GAZP": ["GAZP"]}.items():
    sub = ts[ts.ticker.isin(tk)]
    full = sub.groupby("date").ticker.nunique(); sub = sub[sub.date.isin(full[full == len(tk)].index)]
    g = sub.groupby("date")
    lat = np.log(g.val.sum() / g.trades.sum()).reindex(feat.index)
    for win in (1, 5, 21):
        base = lat.rolling(win, min_periods=max(1, int(win * 0.7))).mean()
        for zw in (126, 252, 504):
            s = zroll(base, zw, zw // 2)
            icm, pm, nm = ic_m(s); icd, td, nd = ic_d(s); icm63, pm63, _ = ic_m(s, fwd63)
            rows.append(dict(family="avg_trade", variant=f"{name}|win{win}|z{zw}", ic21_m=icm, p21_m=pm, n_m=nm,
                             ic21_d=icd, nw_t21=td, ic63_m=icm63, p63_m=pm63))
        # относительный: 5д/126д (как avgtr_c3)
    s = lat.rolling(5, min_periods=4).mean() - lat.rolling(126, min_periods=60).mean()
    icm, pm, nm = ic_m(s); icd, td, nd = ic_d(s); icm63, pm63, _ = ic_m(s, fwd63)
    rows.append(dict(family="avg_trade", variant=f"{name}|5d-126d", ic21_m=icm, p21_m=pm, n_m=nm, ic21_d=icd, nw_t21=td,
                     ic63_m=icm63, p63_m=pm63))
    # компоненты: оборот и число сделок по отдельности (z252 от лог-уровня 5д)
    lv = np.log(g.val.sum()).reindex(feat.index); lt = np.log(g.trades.sum()).reindex(feat.index)
    for nm_, ser in (("оборот", lv), ("число сделок", lt)):
        s = zroll(ser.rolling(5, min_periods=4).mean(), 252, 126)
        icm, pm, nn = ic_m(s); icd, td, nd = ic_d(s); icm63, pm63, _ = ic_m(s, fwd63)
        rows.append(dict(family="avg_trade_components", variant=f"{name}|{nm_}|5d z252", ic21_m=icm, p21_m=pm, n_m=nn,
                         ic21_d=icd, nw_t21=td, ic63_m=icm63, p63_m=pm63))
# число сделок из бесплатного ISS (imoex_value = оборот индекса) — есть ли аналог без ALGOPACK?
raw = pd.read_csv(os.path.join(D, "raw_long.csv"))
iv = raw[raw.series == "imoex_value"].copy(); iv["date"] = pd.to_datetime(iv.date)
iv = iv.set_index("date").value.reindex(feat.index)
s = zroll(np.log(iv).rolling(5, min_periods=4).mean(), 252, 126)
icm, pm, nn = ic_m(s); icd, td, nd = ic_d(s); icm63, pm63, _ = ic_m(s, fwd63)
rows.append(dict(family="free_ISS", variant="оборот IMOEX 5d z252 (бесплатно)", ic21_m=icm, p21_m=pm, n_m=nn, ic21_d=icd, nw_t21=td,
                 ic63_m=icm63, p63_m=pm63))

# ------------------------------------------------ по годам (месячная выборка) для двух кандидатов
for sid in ("hi_nf21z", "avgtr_z_c3", "avgtr_c3", "fz_Si", "fz_MX"):
    s = feat[sid]
    for y in range(2020, 2027):
        m = ME.year == y
        sm = s.reindex(ME)[m]; fm = fwd21.reindex(ME)[m]
        ok = sm.notna() & fm.notna()
        if ok.sum() >= 6:
            r, p = stats.spearmanr(sm[ok], fm[ok])
            rows.append(dict(family="by_year", variant=f"{sid}|{y}", ic21_m=round(r, 3), p21_m=round(p, 3), n_m=int(ok.sum())))

# ------------------------------------------------ корреляция с сигналами панели и частичный IC
ctrl_cols = ["mom63", "dd252", "realized_vol_21", "rvi", "breadth", "futoi_z120", "rgbi_mom21", "usd_mom63", "dy_trail", "mcxsm_rel63"]
ctrl = panel[ctrl_cols]
corr_rows = []
for sid in ("hi_nf21z", "avgtr_z_c3", "avgtr_c3", "fz_Si"):
    s = feat[sid]
    rec = dict(family="corr_with_panel", variant=sid)
    for c in ctrl_cols:
        ok = s.notna() & ctrl[c].notna()
        rec[c] = round(stats.spearmanr(s[ok], ctrl[c][ok])[0], 2) if ok.sum() > 100 else np.nan
    corr_rows.append(rec)
    # частичный IC: остаток сигнала после регрессии на контроль (rolling-free, in-sample OLS — только диагностика)
    for c in ("mom63", "dd252", "realized_vol_21", "breadth"):
        df = pd.DataFrame({"s": s, "c": ctrl[c], "f": fwd21}).dropna()
        X = np.column_stack([np.ones(len(df)), df.c.values])
        beta = np.linalg.lstsq(X, df.s.values, rcond=None)[0]
        resid = pd.Series(df.s.values - X @ beta, index=df.index)
        icm, pm, nm = ic_m(resid, df.f)
        icd, td, nd = ic_d(resid, df.f)
        rows.append(dict(family="partial_ic", variant=f"{sid}|resid vs {c}", ic21_m=icm, p21_m=pm, n_m=nm, ic21_d=icd, nw_t21=td))
    # IC самого контроля на том же окне (для сравнения)
for c in ("mom63", "dd252", "breadth", "futoi_z120", "realized_vol_21"):
    s = ctrl[c].where(feat["hi_nf21z"].notna())
    icm, pm, nm = ic_m(s); icd, td, nd = ic_d(s)
    rows.append(dict(family="panel_signal_same_window", variant=c, ic21_m=icm, p21_m=pm, n_m=nm, ic21_d=icd, nw_t21=td))

res = pd.DataFrame(rows)
res.to_csv(os.path.join(R, "E_robust.csv"), index=False)
pd.DataFrame(corr_rows).to_csv(os.path.join(R, "E_robust_corr.csv"), index=False)
pd.set_option("display.width", 250); pd.set_option("display.max_rows", 500)
for fam in res.family.unique():
    print(f"\n=== {fam} ===")
    print(res[res.family == fam].drop(columns=["family"]).to_string(index=False))
print("\n=== корреляции Спирмена с сигналами панели ===")
print(pd.DataFrame(corr_rows).to_string(index=False))
