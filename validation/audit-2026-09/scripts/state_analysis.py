"""Signal x State analysis on full history (monthly non-overlapping), plus sign stability."""
import sys
import numpy as np
import pandas as pd
from scipy import stats

sys.stdout.reconfigure(encoding="utf-8")
D = "data"
P = pd.read_csv(f"{D}/panel_long.csv", index_col=0, parse_dates=True)

# monthly frame: month-end signal values, next-month return
M = P.resample("ME").last()
M["fwd1m"] = np.log(M["imoex"].shift(-1) / M["imoex"])
M = M[M.index >= "2004-01-01"]
M = M[M.index <= "2026-07-31"]

SIGNALS = ["dd252", "dy_trail", "rb_gap", "urals_rub_gap", "usd_mom63", "rgbi_mom21",
           "mom63", "brent_mom63", "vol_z", "switch_spread", "mcxsm_rel63", "rvi",
           "slope_10_2", "hy_spread", "futoi_z120", "futoi_chg21", "breadth", "sep_node"]

def ic(s, f):
    m = s.notna() & f.notna()
    if m.sum() < 24:
        return np.nan, np.nan, int(m.sum())
    r, p = stats.spearmanr(s[m], f[m])
    return r, p, int(m.sum())

print("=" * 110)
print("1. FULL-HISTORY monthly IC (2004-2026, non-overlapping 1m fwd) + sign stability")
print("=" * 110)
rows = []
for sig in SIGNALS:
    r, p, n = ic(M[sig], M["fwd1m"])
    # rolling 36m IC sign stability
    stab = np.nan
    s_, f_ = M[sig], M["fwd1m"]
    ics = []
    for i in range(36, len(M)):
        w = slice(i - 36, i)
        mm = s_.iloc[w].notna() & f_.iloc[w].notna()
        if mm.sum() >= 30:
            ics.append(stats.spearmanr(s_.iloc[w][mm], f_.iloc[w][mm])[0])
    if len(ics) >= 20 and r == r:
        stab = np.mean(np.sign(ics) == np.sign(r))
    rows.append(dict(signal=sig, n=n, ic=round(r, 3) if r == r else np.nan,
                     p=round(p, 3) if p == p else np.nan,
                     stab=round(stab, 2) if stab == stab else np.nan,
                     n_roll=len(ics)))
t1 = pd.DataFrame(rows).sort_values("p")
print(t1.to_string(index=False))

print()
print("=" * 110)
print("2. STATE-CONDITIONAL monthly IC (full history pooled, NOT eras)")
print("=" * 110)
states = {
    "bull(trend=1)": M["st_trend"] == 1, "bear(trend=0)": M["st_trend"] == 0,
    "vol_hi": M["st_vol"] == 1, "vol_lo": M["st_vol"] == 0,
    "easing": M["st_rate"] == -1, "tightening": M["st_rate"] == 1,
    "bond_stress": M["st_bond"] == 1, "bond_ok": M["st_bond"] == 0,
    "era_pre22": M["era_post22"] == 0, "era_post22": M["era_post22"] == 1,
}
core = ["dd252", "dy_trail", "rb_gap", "usd_mom63", "mom63", "switch_spread",
        "breadth", "rvi", "slope_10_2", "futoi_z120", "rgbi_mom21"]
out = []
for sig in core:
    row = {"signal": sig}
    for nm, mask in states.items():
        r, p, n = ic(M[sig][mask], M["fwd1m"][mask])
        row[nm] = f"{r:+.2f}({n})" + ("*" if p == p and p < 0.05 else "")
    out.append(row)
t2 = pd.DataFrame(out)
pd.set_option("display.width", 250)
print(t2.to_string(index=False))

print()
print("=" * 110)
print("3. Key interactions: dd252 conditioned on bond stress / vol / era")
print("=" * 110)
for cond_name, mask in [("dd<-10% & bond_ok", (M["dd252"] < -0.10) & (M["st_bond"] == 0)),
                        ("dd<-10% & bond_stress", (M["dd252"] < -0.10) & (M["st_bond"] == 1)),
                        ("dd<-10% & easing", (M["dd252"] < -0.10) & (M["st_rate"] == -1)),
                        ("dd<-10% & tightening", (M["dd252"] < -0.10) & (M["st_rate"] == 1))]:
    f = M["fwd1m"][mask]
    if len(f.dropna()) >= 5:
        print(f"{cond_name:26s} n={f.notna().sum():3d} mean_fwd1m={f.mean()*100:+.2f}% median={f.median()*100:+.2f}% hit={(f>0).mean():.2f}")

print()
print("=" * 110)
print("4. STATE MAP: share of months in each state by era + CURRENT state (last row)")
print("=" * 110)
sm = M[["st_trend", "st_vol", "st_rate", "st_bond"]].copy()
sm["era"] = np.where(M["era_post22"] == 1, "post22", "pre22")
print(sm.groupby("era").mean(numeric_only=True).round(2).to_string())
last_day = P.dropna(subset=["st_trend"]).iloc[-1]
print(f"\nCURRENT ({P.index[-1].date()}): trend={'BULL' if last_day['st_trend']==1 else 'BEAR'}, "
      f"vol={'HI' if last_day['st_vol']==1 else 'LO'}, "
      f"rate={'EASING' if last_day['st_rate']==-1 else ('TIGHT' if last_day['st_rate']==1 else 'HOLD')}, "
      f"bond_stress={'YES' if last_day['st_bond']==1 else 'NO'}, dd252={last_day['dd252']*100:.1f}%, "
      f"key={last_day['key']}, rvi={last_day['rvi']:.1f}")
