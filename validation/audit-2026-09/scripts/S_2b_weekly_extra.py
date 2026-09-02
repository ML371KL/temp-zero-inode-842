"""S_2b — дополнение к находке 2: точные ранги недельного решения среди плацебо
«месячное решение в k-й день», бутстреп разностей при задержке исполнения 1 день,
хрупкость по гистерезису при задержке."""
import sys
sys.path.insert(0, "scripts")
import numpy as np
import pandas as pd
from S_lib import *

pl = pd.read_csv("results/S_2_placebo.csv")
print("=== РАНГ НЕДЕЛЬНОГО РЕШЕНИЯ СРЕДИ 21 «МЕСЯЧНЫХ В k-Й ДЕНЬ» (MAIN 2010–2026) ===")
for comp in ["M_live", "W"]:
    for gd in [False, True]:
        dom = pl[(pl["kind"] == "day_of_month") & (pl["comp"] == comp) & (pl["gate_exit_daily"] == gd) & (pl["k"] != "last")]["sharpe"]
        wk = pl[(pl["kind"] == "weekday") & (pl["comp"] == comp) & (pl["gate_exit_daily"] == gd)]
        fri = wk[wk["k"].astype(str) == "4"]["sharpe"].iloc[0]
        last = pl[(pl["kind"] == "day_of_month") & (pl["comp"] == comp) & (pl["gate_exit_daily"] == gd) & (pl["k"] == "last")]["sharpe"].iloc[0]
        print("  %-7s ворота %-8s пятница %.2f: выше %2d из %d месячных дней (медиана %.2f, 75-й перц. %.2f, макс %.2f); конец месяца %.2f выше %d из %d; средн. по дням недели %.2f, мин %.2f" % (
            comp, "ежедн." if gd else "в такт", fri, int((dom < fri).sum()), len(dom), dom.median(), dom.quantile(0.75), dom.max(),
            last, int((dom < last).sum()), len(dom), wk["sharpe"].mean(), wk["sharpe"].min()))

d = load_daily()
m = load_monthly()
idx = d.index
me = month_end_mask(idx)
we = week_end_mask(idx)
signs = {"usd_mom63": +1, "slope_10_2": +1, "urals_rub_gap": -1}
legs = d[["usd_mom63", "slope_10_2", "urals_rub_gap"]]
comp_live, _ = composite_live_daily(legs, signs)
comp_closed = m["composite"].reindex(idx).ffill()
legs_w = legs[we]
zw = pd.DataFrame({c: signs[c] * zscore_rolling(legs_w[c], 260, 104, 3.0) for c in legs_w.columns})
comp_w = zw.mean(axis=1, skipna=True); comp_w[zw.notna().sum(axis=1) == 0] = np.nan
comp_w = comp_w.reindex(idx).ffill()


def build(comp, dmask, hyst, gate_exit_daily, delay=0):
    tox = d["toxic"].values; cv = comp.values; dm = dmask.values
    pos = np.full(len(idx), np.nan); s = 0; cur = np.nan; g = np.nan
    for i in range(len(idx)):
        if np.isnan(tox[i]):
            continue
        if dm[i]:
            if np.isfinite(cv[i]):
                if cv[i] > hyst: s = 1
                elif cv[i] < -hyst: s = -1
            g = 1.0 - tox[i]
        elif gate_exit_daily and tox[i] == 1:
            g = 0.0
        if np.isnan(g) or s == 0:
            continue
        cur = float(g == 1.0 and s > 0)
        pos[i] = cur
    p = pd.Series(pos, index=idx)
    return p.shift(delay) if delay else p


def mr_of(pos, a="2010-01-01", b="2026-08-31"):
    return monthly_returns(run_daily(pos, d, start=a, end=b))


print("\n=== БУТСТРЕП РАЗНОСТЕЙ ПРИ ЗАДЕРЖКЕ ИСПОЛНЕНИЯ (все против прода с ТОЙ ЖЕ задержкой) ===")
rows = []
for delay in [0, 1, 2]:
    base = mr_of(build(comp_closed, me, 0.10, False, delay))
    for nm, comp, dmask, h, gd in [("W/W h=0,1 ворота в такт", comp_w, we, 0.10, False),
                                   ("W/W h=0,2 ворота в такт", comp_w, we, 0.20, False),
                                   ("W/W h=0,2 + ворота ежедн.", comp_w, we, 0.20, True),
                                   ("W/W h=0,1 + ворота ежедн.", comp_w, we, 0.10, True),
                                   ("M_live/W h=0,2 + ворота ежедн.", comp_live, we, 0.20, True),
                                   ("M_live/W h=0,1 в такт", comp_live, we, 0.10, False),
                                   ("M_closed/M + ворота ежедн.", comp_closed, me, 0.10, True)]:
        x = mr_of(build(comp, dmask, h, gd, delay))
        for lab, a, b in [("2010-2026", "2010-01-01", "2026-08-31"), ("2010-2021", "2010-01-01", "2021-12-31"), ("2004-2026", "2004-01-01", "2026-08-31")]:
            xx = mr_of(build(comp, dmask, h, gd, delay), a, b); bb = mr_of(build(comp_closed, me, 0.10, False, delay), a, b)
            j = xx.join(bb, lsuffix="_x", rsuffix="_b").dropna()
            dsh, p, lo, hi = sharpe_diff_boot(j["strat_x"], j["strat_b"], n_boot=1500)
            rows.append(dict(delay=delay, name=nm, window=lab, sharpe=sharpe(j["strat_x"]), prod=sharpe(j["strat_b"]), d_sharpe=dsh, p_le0=p))
r = pd.DataFrame(rows)
r.to_csv("results/S_2b_delay_boot.csv", index=False)
for lab in ["2010-2026", "2010-2021", "2004-2026"]:
    print(f"--- {lab} ---  ΔШарп к проду (p) при задержке 0 / 1 / 2 дней")
    s = r[r["window"] == lab]
    for nm in s["name"].unique():
        ss = s[s["name"] == nm].sort_values("delay")
        print("  %-36s " % nm + "  ".join("%+.2f (p %.2f)" % (a, b) for a, b in zip(ss["d_sharpe"], ss["p_le0"])) + "   | прод сам: " + " ".join(f"{v:.2f}" for v in ss["prod"]))
