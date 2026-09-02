"""S2 — находка 1, эпизоды: пятничный композит M_live внутри ключевых месяцев (2022-08, 2011-08, 2017-01,
2025-06, 2010-01) — где недельный порог 0,4 «сорвался» раньше среза. Запуск: python scripts/S2_1c_hyst_episodes.py
"""
import sys
sys.path.insert(0, "scripts")
import numpy as np
import pandas as pd
from S2_lib import *  # noqa

D, C, M = load_all()
cuts = month_end_cuts(D.index)
wcuts = week_end_cuts(D.index)
Zd = pd.DataFrame({k: sgn * live_daily_z(D[k], cuts) for k, sgn in LEGS_PROD})
comp_d, _ = composite_mean(Zd)
raw_m = pd.DataFrame({k: D[k].reindex(cuts) for k, _ in LEGS_PROD})
Zm = pd.DataFrame({k: sgn * zroll(raw_m[k]) for k, sgn in LEGS_PROD})
comp_m, _ = composite_mean(Zm)
tr = C["mcftr_ffill"]
log = open(f"{RES}/S2_1c_log.txt", "w", encoding="utf-8")


def P(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    log.write(s + "\n")


rows = []
for a, b, label in [("2022-06-01", "2022-10-31", "2022 лето-осень"), ("2011-06-01", "2011-10-31", "2011"), ("2016-11-01", "2017-03-31", "2017-01"),
                    ("2025-04-01", "2025-10-31", "2025-06"), ("2009-11-01", "2010-03-31", "2010-01"), ("2026-03-01", "2026-08-31", "2026")]:
    w = comp_d.reindex(wcuts).loc[a:b]
    me = comp_m.loc[a:b]
    P(f"\n[{label}] пятничный M_live (и месячные срезы ▶):")
    out = []
    for d, v in w.items():
        mark = "▶" if d in me.index else " "
        tr_next = tr.reindex(wcuts).shift(-1).loc[d] / tr.loc[d] - 1
        out.append(f"  {d.date()} {mark} {v:+.3f}  (MCFTR до след. пятницы {tr_next*100:+.1f}%)")
        rows.append(dict(episode=label, date=d.date(), is_month_end=d in me.index, comp_live=v, next_week_tr=tr_next))
    P("\n".join(out))
pd.DataFrame(rows).to_csv(f"{RES}/S2_hyst_episodes_weekly.csv", index=False, float_format="%.4f")

# по всем месяцам MAIN: доля месяцев, где max пятничного > 0,4 при срезе в (0,1; 0,4] — и что рынок сделал в следующем месяце
P("\n[all] месяцы MAIN со срезом в (0,1; 0,4] (0,1 → лонг, 0,4 → не входит): был ли пятничный пробой >0,4 внутри месяца, и fwd MCFTR−mm следующего месяца")
mmc = (1 + C["mm_rate"] / 100.0 / 252.0).cumprod()
t_, m_ = tr.reindex(cuts), mmc.reindex(cuts)
fwd_ex = (t_.shift(-1) / t_ - 1) - (m_.shift(-1) / m_ - 1)
wk = comp_d.reindex(wcuts)
res = []
for d in cuts[(cuts >= "2010-01-01") & (cuts <= "2026-07-31")]:
    c = comp_m.loc[d]
    if not (0.1 < c <= 0.4):
        continue
    month = wk[(wk.index.to_period("M") == d.to_period("M"))]
    res.append(dict(date=d.date(), c_end=c, wmax=month.max(), breakout=bool(month.max() > 0.4), fwd_ex=fwd_ex.loc[d]))
res = pd.DataFrame(res)
P(res.round(3).to_string())
P(f"  n={len(res)}; с пробоем {int(res.breakout.sum())}: средний fwd избыток {res[res.breakout].fwd_ex.mean()*100:+.2f} п.п.; без пробоя {int((~res.breakout).sum())}: {res[~res.breakout].fwd_ex.mean()*100:+.2f} п.п.")
res.to_csv(f"{RES}/S2_hyst_border_breakouts.csv", index=False, float_format="%.4f")
log.close()
