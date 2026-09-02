"""Walk-forward horse race: universal-fixed vs adaptive vs state-modulated composites.
Monthly, expanding window, strictly past-only information at each step."""
import sys
import numpy as np
import pandas as pd
from scipy import stats

sys.stdout.reconfigure(encoding="utf-8")
D = "data"
P = pd.read_csv(f"{D}/panel_long.csv", index_col=0, parse_dates=True)

M = P.resample("ME").last()
M["fwd1m"] = np.log(M["imoex"].shift(-1) / M["imoex"])
M = M[(M.index >= "2004-01-31") & (M.index <= "2026-07-31")]

SIGS = ["dd252", "dy_trail", "rb_gap", "urals_rub_gap", "usd_mom63", "rgbi_mom21",
        "mom63", "brent_mom63", "vol_z", "switch_spread", "mcxsm_rel63", "rvi",
        "slope_10_2", "hy_spread", "futoi_z120", "breadth"]

# rolling z-scores (past-only): 60m window min 24
Z = pd.DataFrame(index=M.index)
for s in SIGS:
    x = M[s]
    Z[s] = (x - x.rolling(60, min_periods=24).mean()) / x.rolling(60, min_periods=24).std()
Z = Z.clip(-3, 3)

fwd = M["fwd1m"]
OOS_START = "2010-01-31"
idx = M.index
oos_mask = idx >= OOS_START

def trailing_ic(sig_vals, fwd_vals):
    m = sig_vals.notna() & fwd_vals.notna()
    if m.sum() < 36:
        return np.nan, 0
    r, _ = stats.spearmanr(sig_vals[m], fwd_vals[m])
    return r, int(m.sum())

pred = {"M1_fixed": [], "M2_adapt": [], "M3_state": []}
dates = []
diag_selected = []

STATE_COLS = ["st_trend", "st_vol"]

for i, t in enumerate(idx):
    if t < pd.Timestamp(OOS_START):
        continue
    past = slice(0, i)  # months strictly before t (fwd of month i-1 uses month i price -> known at t month-end? fwd1m[i-1] = log(px[i]/px[i-1]) known at t. Use fwd up to i-1 => rows 0..i-2 have complete fwd known by t.)
    hist_sig = M[SIGS].iloc[:i - 1]
    hist_fwd = fwd.iloc[:i - 1]

    # M1: fixed universal (usd_mom63 +, slope +, urals_rub_gap -, fallback rb_gap -)
    comp = 0.0
    nn = 0
    for s, sgn in [("usd_mom63", +1), ("slope_10_2", +1), ("urals_rub_gap", -1)]:
        v = Z[s].iloc[i]
        if v == v:
            comp += sgn * v
            nn += 1
    if nn == 0:
        comp = np.nan
    pred["M1_fixed"].append(comp / max(nn, 1) if comp == comp else np.nan)

    # M2: adaptive - trailing IC selection
    comp2, w2 = 0.0, 0.0
    sel = []
    for s in SIGS:
        r, n = trailing_ic(hist_sig[s], hist_fwd)
        if r == r and abs(r) >= 0.08 and n >= 36:
            v = Z[s].iloc[i]
            if v == v:
                comp2 += np.sign(r) * min(abs(r), 0.4) * v
                w2 += min(abs(r), 0.4)
                sel.append(f"{s}:{r:+.2f}")
    pred["M2_adapt"].append(comp2 / w2 if w2 > 0 else np.nan)
    diag_selected.append((str(t.date()), sel))

    # M3: state-modulated - trailing IC computed on past months in SAME state
    cur_state = tuple(M[c].iloc[i] for c in STATE_COLS)
    state_mask = np.ones(i - 1, dtype=bool)
    for j, c in enumerate(STATE_COLS):
        state_mask &= (M[c].iloc[:i - 1] == cur_state[j]).values
    comp3, w3 = 0.0, 0.0
    for s in SIGS:
        sv = hist_sig[s][state_mask]
        fv = hist_fwd[state_mask]
        r, n = trailing_ic(sv, fv)
        if not (r == r and n >= 24):
            r, n = trailing_ic(hist_sig[s], hist_fwd)  # fallback overall
        if r == r and abs(r) >= 0.08 and n >= 24:
            v = Z[s].iloc[i]
            if v == v:
                comp3 += np.sign(r) * min(abs(r), 0.4) * v
                w3 += min(abs(r), 0.4)
    pred["M3_state"].append(comp3 / w3 if w3 > 0 else np.nan)
    dates.append(t)

R = pd.DataFrame(pred, index=dates)
R["fwd"] = fwd.reindex(dates)
R = R.dropna(subset=["fwd"])

print("=" * 90)
print(f"WALK-FORWARD OOS {R.index[0].date()} .. {R.index[-1].date()}  ({len(R)} months)")
print("=" * 90)
bh_ann = R["fwd"].mean() * 12 * 100
bh_sh = R["fwd"].mean() / R["fwd"].std() * np.sqrt(12)
print(f"buy&hold: ann={bh_ann:+.1f}%  Sharpe={bh_sh:+.2f}")
print()
for m in ["M1_fixed", "M2_adapt", "M3_state"]:
    sub = R.dropna(subset=[m])
    icv, icp = stats.spearmanr(sub[m], sub["fwd"])
    hit = ((sub[m] > 0) == (sub["fwd"] > 0)).mean()
    stra = sub["fwd"].where(sub[m] > 0, 0.0)
    ann = stra.mean() * 12 * 100
    sh = stra.mean() / stra.std() * np.sqrt(12) if stra.std() > 0 else np.nan
    tin = (sub[m] > 0).mean()
    # long/short variant
    ls = np.where(sub[m] > 0, sub["fwd"], -sub["fwd"])
    ls_ann = ls.mean() * 12 * 100
    ls_sh = ls.mean() / ls.std() * np.sqrt(12)
    print(f"{m}: n={len(sub)}  IC={icv:+.3f} (p={icp:.4f})  hit={hit:.2f}  "
          f"L/F ann={ann:+.1f}% Sh={sh:+.2f} in={tin:.0%}  |  L/S ann={ls_ann:+.1f}% Sh={ls_sh:+.2f}")

print()
print("--- by era ---")
for era, lo, hi in [("2010-2021", "2010-01-31", "2022-02-18"), ("2022-2024", "2022-03-24", "2024-12-31"),
                    ("2025-2026", "2025-01-01", "2026-07-31")]:
    sub = R[(R.index >= lo) & (R.index <= hi)]
    line = f"{era}: bh={sub['fwd'].mean()*12*100:+.1f}%"
    for m in ["M1_fixed", "M2_adapt", "M3_state"]:
        s2 = sub.dropna(subset=[m])
        if len(s2) < 8:
            line += f"  {m}=n/a"
            continue
        icv, _ = stats.spearmanr(s2[m], s2["fwd"])
        stra = s2["fwd"].where(s2[m] > 0, 0.0)
        line += f"  {m}: IC{icv:+.2f} ann={stra.mean()*12*100:+.1f}%"
    print(line)

print()
print("--- M2 selections over time (every 24th month) ---")
for d, sel in diag_selected[::24]:
    print(f"{d}: {', '.join(sel) if sel else '(none)'}")

R.to_csv(f"{D}/walkforward_results.csv")
print("\nsaved data/walkforward_results.csv")
