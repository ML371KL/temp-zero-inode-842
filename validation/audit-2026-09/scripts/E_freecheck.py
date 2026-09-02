"""E_algopack шаг 4e: нужен ли ALGOPACK сигналу «число сделок / средний размер сделки»? Проверка на
БЕСПЛАТНОЙ дневной истории ISS (NUMTRADES, VALUE) 2011-2026 по тяжеловесам: IC на 2011-2019 (вне окна
ALGOPACK), 2020-2026 и полной; частичный IC после контроля mom63/vol. Выход: results/E_freecheck.csv."""
import sys, os
import numpy as np, pandas as pd
from scipy import stats
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from testlib import nw_tstat, spearman_ic
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
D = os.path.join(ROOT, "data"); A = os.path.join(D, "algopack"); R = os.path.join(ROOT, "results")

panel = pd.read_csv(os.path.join(D, "panel_prod_daily.csv"), parse_dates=["date"]).set_index("date")
panel = panel[panel.index >= "2010-01-01"]
lp = np.log(panel.imoex); fwd21 = lp.shift(-21) - lp; fwd63 = lp.shift(-63) - lp
ME = pd.DatetimeIndex(pd.Series(panel.index, index=panel.index).groupby([panel.index.year, panel.index.month]).last().values)
h = pd.read_csv(os.path.join(A, "iss_history_daily.csv"), parse_dates=["tradedate"])
h = h.dropna(subset=["numtrades", "value"])
h = h[(h.numtrades > 0) & (h.value > 0)]
print("ISS history:", h.groupby("secid").tradedate.agg(["min", "max", "count"]).to_string())


def zroll(s, win, minp):
    return (s - s.rolling(win, min_periods=minp).mean()) / s.rolling(win, min_periods=minp).std()


def tests(sig, name, fam):
    out = []
    for wname, ab in {"2014-2019 (вне окна ALGOPACK)": ("2014-01-01", "2019-12-31"), "2020-2026": ("2020-01-01", "2026-08-31"),
                      "2014-2026": ("2014-01-01", "2026-08-31"), "2014-2017": ("2014-01-01", "2017-12-31"),
                      "2018-2021": ("2018-01-01", "2021-12-31"), "ex2022": None}.items():
        if wname == "ex2022":
            m = (panel.index.year != 2022) & (panel.index >= "2014-01-01") & (panel.index <= "2026-08-31")
        else:
            m = (panel.index >= ab[0]) & (panel.index <= ab[1])
        s = sig.where(m)
        sm = s.reindex(ME); fm = fwd21.reindex(ME); fm63 = fwd63.reindex(ME)
        icm, pm, nm = spearman_ic(sm.values, fm.values)
        icm63, pm63, _ = spearman_ic(sm.values, fm63.values)
        icd, pd_, nd = spearman_ic(s.values, fwd21.values)
        _, t, _ = nw_tstat(s.values, fwd21.values, 21)
        out.append(dict(family=fam, signal=name, window=wname, ic21_m=round(icm, 3), p21_m=round(pm, 3), n_m=nm,
                        ic63_m=round(icm63, 3), p63_m=round(pm63, 3), ic21_d=round(icd, 3), nw_t21=round(t, 2)))
    return out


rows = []
sets = {"c3": ["SBER", "LKOH", "GAZP"], "c5": ["SBER", "LKOH", "GAZP", "GMKN", "ROSN"], "SBER": ["SBER"]}
built = {}
for sname, tk in sets.items():
    sub = h[h.secid.isin(tk)]
    full = sub.groupby("tradedate").secid.nunique(); sub = sub[sub.tradedate.isin(full[full == len(tk)].index)]
    g = sub.groupby("tradedate")
    val = g.value.sum().reindex(panel.index); ntr = g.numtrades.sum().reindex(panel.index)
    lat = np.log(val / ntr); lnt = np.log(ntr); lv = np.log(val)
    cands = {
        f"avg_trade|{sname}|5d z252": zroll(lat.rolling(5, min_periods=4).mean(), 252, 126),
        f"avg_trade|{sname}|21d z252": zroll(lat.rolling(21, min_periods=15).mean(), 252, 126),
        f"avg_trade|{sname}|5d-126d": lat.rolling(5, min_periods=4).mean() - lat.rolling(126, min_periods=60).mean(),
        f"n_trades|{sname}|5d z252": zroll(lnt.rolling(5, min_periods=4).mean(), 252, 126),
        f"n_trades|{sname}|21d z252": zroll(lnt.rolling(21, min_periods=15).mean(), 252, 126),
        f"n_trades|{sname}|5d-126d": lnt.rolling(5, min_periods=4).mean() - lnt.rolling(126, min_periods=60).mean(),
        f"turnover|{sname}|5d z252": zroll(lv.rolling(5, min_periods=4).mean(), 252, 126),
    }
    for name, s in cands.items():
        built[name] = s
        rows.append(pd.DataFrame(tests(s, name, "free_iss")))
res = pd.concat(rows)
# частичный IC (полная история) после контроля mom63 / realized_vol_21 / dd252 для ключевых
prow = []
for name in ["avg_trade|c3|5d z252", "n_trades|c3|5d z252", "n_trades|c3|21d z252", "avg_trade|c3|5d-126d"]:
    s = built[name]
    for c in ("mom63", "realized_vol_21", "dd252", "breadth"):
        df = pd.DataFrame({"s": s, "c": panel[c], "f": fwd21}).dropna()
        df = df[(df.index >= "2014-01-01") & (df.index <= "2026-08-31")]
        X = np.column_stack([np.ones(len(df)), df.c.values])
        beta = np.linalg.lstsq(X, df.s.values, rcond=None)[0]
        resid = pd.Series(df.s.values - X @ beta, index=df.index)
        icm, pm, nm = spearman_ic(resid.reindex(ME).values, df.f.reindex(ME).values)
        _, t, _ = nw_tstat(resid.values, df.f.values, 21)
        corr = stats.spearmanr(df.s, df.c)[0]
        prow.append(dict(family="partial", signal=name, window=f"resid vs {c} (corr {corr:.2f})", ic21_m=round(icm, 3), p21_m=round(pm, 3), n_m=nm,
                         nw_t21=round(t, 2)))
res = pd.concat([res, pd.DataFrame(prow)])
res.to_csv(os.path.join(R, "E_freecheck.csv"), index=False)
pd.set_option("display.width", 250); pd.set_option("display.max_rows", 500)
print(res.to_string(index=False))
# сохранить бесплатный сигнал для стратегии
pd.DataFrame({k: v for k, v in built.items() if "c3" in k or "c5" in k}).to_csv(os.path.join(A, "E_free_signals_daily.csv"))
