"""F1_economist: добавочные проверки — цена ожиданий (y1−key) по фазам ставки, сюрпризы ЦБ, состав «окон входа»,
доля битов по годам (структурная «залипаемость» облигационного флага после 2022).
Запуск из audit/: python scripts/F1_economist_extra.py
"""
import sys
import numpy as np, pandas as pd
from scipy import stats
sys.stdout.reconfigure(encoding="utf-8")
pd.set_option("display.width", 250); pd.set_option("display.max_columns", 40)

D = pd.read_csv("data/panel_prod_daily.csv", parse_dates=["date"]).set_index("date")
M = pd.read_csv("data/panel_prod_monthly.csv", parse_dates=["date"]).set_index("date")
R = pd.read_csv("data/panel_daily.csv", parse_dates=["TRADEDATE"]).set_index("TRADEDATE")
TOX = "bear|stress|stress"

def sp(x, y):
    m = x.notna() & y.notna()
    if m.sum() < 8: return (np.nan, np.nan, int(m.sum()))
    r, p = stats.spearmanr(x[m], y[m]); return (r, p, int(m.sum()))

print("A. ЦЕНА ОЖИДАНИЙ y1−key: по фазам ставки и терцилям (месячно, 2015+)")
Rm = R.resample("ME").last().loc["2015-01-01":"2026-07-31"]
Rm["fwd1m"] = np.log(R.imoex.resample("ME").last().shift(-1) / R.imoex.resample("ME").last()).reindex(Rm.index)
Rm["st_rate"] = M.st_rate.reindex(Rm.index, method="ffill")
Rm["cell"] = M.cell.reindex(Rm.index, method="ffill")
for ph, name in ((-1, "смягчение"), (1, "ужесточение")):
    sub = Rm[Rm.st_rate == ph]
    for col in ["y1_minus_key", "y10_minus_key", "slope_10_2", "rusfar_minus_key", "real_key_saar"]:
        r, p, n = sp(sub[col], sub.fwd1m); print(f"  [{name}] IC {col:17s} {r:+.3f} p={p:.3f} n={n}")
Rm["terc"] = pd.qcut(Rm.y1_minus_key, 3, labels=["низ (куплено много снижений)", "середина", "верх (в цене ужесточение)"])
g = Rm.groupby("terc", observed=True).fwd1m.agg(["mean", "median", "count", lambda s: (s > 0).mean()]); g.columns = ["mean", "median", "n", "hit"]
g[["mean", "median"]] = (g[["mean", "median"]] * 100).round(2); print(g.round(2))
print("границы терцилей y1−key (п.п.):", Rm.y1_minus_key.quantile([1/3, 2/3]).round(2).tolist())
print("корреляции (Спирмен) slope / y10−key / y1−key / y2−key / rusfar−key:")
Rm["y2_minus_key"] = Rm["y2.0"] - Rm.key
print(Rm[["slope_10_2", "y10_minus_key", "y2_minus_key", "y1_minus_key", "rusfar_minus_key"]].corr(method="spearman").round(2))

print("\nB. СЮРПРИЗЫ ЦБ: доходность IMOEX от закрытия накануне решения (дневная панель)")
cb = pd.read_csv("data/cb_decisions.csv", parse_dates=["date"])
px = D.imoex.dropna(); idx = px.index
rows = []
for _, r in cb.iterrows():
    d = r.date
    if d < idx[0] or d > idx[-1]: continue
    k = idx.searchsorted(d)  # первый торговый день >= d
    if k >= len(idx): continue
    k0 = k - 1
    if k0 < 0: continue
    p0 = px.iloc[k0]
    def ret(h):
        j = min(k + h, len(idx) - 1); return px.iloc[j] / p0 - 1
    rows.append({"date": d.date(), "surprise": r.surprise, "delta_bp": round((r.new_rate - r.prev_rate) * 100),
                 "unsched": int("ВНЕПЛАНОВ" in str(r.signal_note)), "r0": ret(0), "r5": ret(5), "r21": ret(21), "r63": ret(63)})
S = pd.DataFrame(rows)
for u in (0, 1):
    sub = S[S.unsched == u]
    print(f"-- плановые={1-u}: n={len(sub)}")
    g = sub.groupby("surprise")[["r0", "r5", "r21", "r63"]].agg(["mean", "median", "count"])
    print((g * 100).round(2).to_string())
print("Список сюрпризов (плановые), r0/r21:")
print((S[(S.surprise != "in-line") & (S.unsched == 0)].assign(r0=lambda x: (x.r0 * 100).round(1), r21=lambda x: (x.r21 * 100).round(1))[["date", "surprise", "delta_bp", "r0", "r21"]]).to_string())
S.to_csv("results/F1_economist_cb_surprises.csv", index=False)

print("\nC. СОСТАВ «ОКОН ВХОДА» и токсичных месяцев (месячные метки, fwd1m %)")
mm = M.loc["2004-01-01":].dropna(subset=["cell", "fwd1m_log"])
for c in ("bull|stress|ok", "bear|stress|ok", "bull|stress|stress"):
    sub = mm[mm.cell == c]
    print(f"  {c}: " + ", ".join(f"{d.strftime('%Y-%m')}({v*100:+.1f})" for d, v in sub.fwd1m_log.items()))

print("\nD. ДОЛЯ ДНЕЙ С ВКЛЮЧЁННЫМ БИТОМ ПО ГОДАМ (структурная залипаемость)")
b = D[["st_trend", "st_vol", "st_bond"]].dropna().copy()
b["toxic"] = (D.cell.reindex(b.index) == TOX).astype(int)
b["year"] = b.index.year
t = b.groupby("year")[["st_trend", "st_vol", "st_bond", "toxic"]].mean().round(2)
t.columns = ["bull", "vol_stress", "bond_stress", "toxic"]
print(t.loc[2004:].T.to_string())
print("Доля дней bond_stress: 2004–2021 =", round(b.loc[:"2021-12-31", "st_bond"].mean(), 2), "; 2022-03+ =", round(b.loc["2022-03-24":, "st_bond"].mean(), 2))
print("Средняя просадка RGBI (лог) 2022-03+:", round(D.rgbi_dd.loc["2022-03-24":].mean(), 3), "; до 2022:", round(D.rgbi_dd.loc["2012-01-01":"2021-12-31"].mean(), 3))

print("\nE. RGBI-флаг: что он ловит после 2022 — fwd1m по bond при фиксированных trend/vol (2022-03+ и 2010–2021)")
for a, bnd, name in (("2010-01-01", "2021-12-31", "2010–2021"), ("2022-03-01", "2026-08-31", "2022-03+")):
    sub = mm.loc[a:bnd]
    g = sub.groupby(["st_trend", "st_vol", "st_bond"]).fwd1m_log.agg(["mean", "count"]); g["mean"] = (g["mean"] * 100).round(2)
    print(f"-- {name}"); print(g.T.to_string())

print("\nF. ДЕВАЛЬВАЦИЯ В ВОССТАНОВЛЕНИИ: z_usd в первые 6 месяцев после дна эпизодов (композит против отскока)")
for tr in ("2008-10-31", "2009-02-27", "2014-12-30", "2020-03-31", "2022-09-30", "2024-12-30", "2026-07-31"):
    d = pd.Timestamp(tr); seg = M.loc[d:].iloc[:7]
    print(f"  дно~{tr}: " + " | ".join(f"{i.strftime('%y-%m')} z_usd {z:+.1f} comp {c:+.2f} fwd {f*100:+.1f}" for i, z, c, f in zip(seg.index, seg.z_usd_mom63, seg.composite, seg.fwd1m_log.fillna(0))))
