"""S2 — находка 3, дополнение: кандидаты-критерии на реальной истории 2004–2026 (фронты, текущие значения,
умение в следующие 12 мес). Читает results/S2_health_series.csv (из S2_3_health.py).
Запуск: python scripts/S2_3c_health_history.py
"""
import sys
sys.path.insert(0, "scripts")
import numpy as np
import pandas as pd
from S2_lib import *  # noqa

R = pd.read_csv(f"{RES}/S2_health_series.csv", index_col=0, parse_dates=True)
log = open(f"{RES}/S2_3c_log.txt", "w", encoding="utf-8")


def P(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    log.write(s + "\n")


right = np.where(R.pos_prev > 0.5, R.ex_mkt > 0, R.ex_mkt <= 0).astype(float)
R["share36"] = pd.Series(right, index=R.index).rolling(36).mean()
sk = R.skill
R["t36"] = sk.rolling(36).mean() / sk.rolling(36).std() * np.sqrt(36)
R["t60"] = sk.rolling(60).mean() / sk.rolling(60).std() * np.sqrt(60)


def page_cusum(c, k_frac, w=120, min_n=60):
    s = pd.Series(c)
    mu0 = s.rolling(w, min_periods=min_n).mean().shift(1).values
    sig = s.rolling(w, min_periods=min_n).std().shift(1).values
    S = np.full(len(c), np.nan)
    cur = 0.0
    for t in range(len(c)):
        if np.isfinite(c[t]) and np.isfinite(mu0[t]) and np.isfinite(sig[t]) and sig[t] > 0:
            cur = max(0.0, cur + (max(mu0[t], 0.0) * k_frac - c[t]) / sig[t])
            S[t] = cur
    return S


R["cusum0"] = page_cusum(sk.values, 0.0)


def fronts(a):
    a = np.asarray(a, bool)
    prev = np.r_[False, a[:-1]]
    return np.where(a & ~prev)[0]


CR = {
    "доля верных 36м < 0,42": R.share36 < 0.42,
    "доля верных 36м < 0,40": R.share36 < 0.40,
    "доля верных 24м < 0,40": R.share24 < 0.40,
    "доля верных 24м < 0,42 И IC-24 < 0": (R.share24 < 0.42) & (R.ic24 < 0),
    "t(умение 36м) < −1": R.t36 < -1,
    "t(умение 60м) < −1": R.t60 < -1,
    "CUSUM(k=0) > 10σ": R.cusum0 > 10,
    "CUSUM(k=½μ0) > 10σ": R.cusum > 10,
    "IC-24 < 0 ×12": pd.Series(np.r_[[False] * 11, [all(R.ic24.values[i - 11:i + 1] < 0) for i in range(11, len(R))]], index=R.index),
    "IC-24 < 0 ×9": pd.Series(np.r_[[False] * 8, [all(R.ic24.values[i - 8:i + 1] < 0) for i in range(8, len(R))]], index=R.index),
    "IC-36 < 0 ×6": pd.Series(np.r_[[False] * 5, [all(R.ic36.values[i - 5:i + 1] < 0) for i in range(5, len(R))]], index=R.index),
    "IC-24 < −0,1 ×6": pd.Series(np.r_[[False] * 5, [all(R.ic24.values[i - 5:i + 1] < -0.1) for i in range(5, len(R))]], index=R.index),
    "IC-24 < 0 ×6 (регламент)": pd.Series(np.r_[[False] * 5, [all(R.ic24.values[i - 5:i + 1] < 0) for i in range(5, len(R))]], index=R.index),
    "избыток24 < −20 %": R.ex24 < -0.20,
}
rows = []
for name, al in CR.items():
    fr = fronts(al.fillna(False).values)
    rows.append(dict(criterion=name, fronts=len(fr), months_in_alarm=float(al.fillna(False).mean()),
                     dates=", ".join(R.index[i].strftime("%Y-%m") for i in fr),
                     fwd12_skill_after=np.nanmean(R.fwd12_skill.values[fr]) if len(fr) else np.nan,
                     fwd12_strat_ex_after=np.nanmean(R.fwd12_strat_ex.values[fr]) if len(fr) else np.nan,
                     now=bool(al.fillna(False).iloc[-1])))
h = pd.DataFrame(rows)
h.to_csv(f"{RES}/S2_health_candidates_history.csv", index=False, float_format="%.4f")
P("[история] кандидаты-критерии 2004–2026: фронты, доля месяцев в тревоге, умение/избыток стратегии в следующие 12 мес после фронта (безусловно: "
  f"умение {R.fwd12_skill.mean()*100:+.2f} %/мес, избыток {R.fwd12_strat_ex.mean()*100:+.1f} %):")
for _, r in h.iterrows():
    P(f"  {r.criterion:38s} фронтов {r.fronts:2d}, в тревоге {r.months_in_alarm:5.1%}, после: умение {r.fwd12_skill_after*100:+.2f}, избыток {r.fwd12_strat_ex_after*100:+.1f}; сейчас: {'ТРЕВОГА' if r['now'] else 'нет'};  {r.dates}")
last = R.iloc[-1]
P(f"\n[сейчас, 2026-08] доля верных 24м {last.share24:.3f}, 36м {last.share36:.3f}; IC-24 {last.ic24:+.3f}, IC-36 {last.ic36:+.3f}; t(умение 36м) {last.t36:+.2f}, 60м {last.t60:+.2f}; "
  f"CUSUM k=0 {last.cusum0:.1f}σ, k=½μ0 {last.cusum:.1f}σ; избыток24 {last.ex24*100:+.1f} %, избыток12 {last.ex12*100:+.1f} %; умение за 24 мес {last.skill24*100:+.3f} %/мес")
P("[траектория 2025-01…2026-08]")
P(R.loc["2025-01-01":, ["pos_prev", "ex_mkt", "skill", "ic24", "share24", "share36", "t36", "cusum0", "cusum", "ex24"]].round(3).to_string())
log.close()
