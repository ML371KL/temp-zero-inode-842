"""F2_pm §3 (доп.): своевременность по ВСЕМ значимым колебаниям IMOEX (зигзаг ±15%, а не только
просадки от исторического максимума — тот метод терял 2011, 2014, 2024) и крупнейшие просадки самих
стратегий с датами. Запуск: python scripts/F2_timeliness.py (после F2_switching.py)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from F2_lib import *

d = load_daily()
POS = pd.read_csv(os.path.join(RES, "F2_rules_positions.csv"), index_col=0, parse_dates=True)


def zigzag(px, thr=0.15):
    px = px.dropna(); v = px.values; n = len(v)
    piv = []; mode = None; imax = 0; imin = 0
    for i in range(1, n):
        if mode is None:
            if v[i] > v[imax]: imax = i
            if v[i] < v[imin]: imin = i
            if v[i] <= v[imax] * (1 - thr):
                piv.append(("peak", imax)); mode = "down"; imin = i
            elif v[i] >= v[imin] * (1 + thr):
                piv.append(("trough", imin)); mode = "up"; imax = i
        elif mode == "up":
            if v[i] > v[imax]: imax = i
            elif v[i] <= v[imax] * (1 - thr):
                piv.append(("peak", imax)); mode = "down"; imin = i
        else:
            if v[i] < v[imin]: imin = i
            elif v[i] >= v[imin] * (1 + thr):
                piv.append(("trough", imin)); mode = "up"; imax = i
    eps = []
    for k in range(len(piv) - 1):
        if piv[k][0] == "peak" and piv[k + 1][0] == "trough":
            p = px.index[piv[k][1]]; t = px.index[piv[k + 1][1]]
            rec = px.index[piv[k + 2][1]] if k + 2 < len(piv) else None
            eps.append((p, t, rec, v[piv[k + 1][1]] / v[piv[k][1]] - 1))
    return eps


px = d["imoex"][d.index >= "2004-01-01"]
eps = zigzag(px, 0.15)
print(f"колебаний IMOEX глубже 15% (зигзаг) с 2004: {len(eps)}")
RULES = ["PANEL_M: ворота+ядро, месячная оценка", "PANEL_W: ворота+ядро, недельная оценка",
         "PANEL_D: ворота(день)+ядро(закр.мес)", "GATE_D: только ворота (день)", "CORE_M: только знак ядра",
         "HYST ворота (bond off -3.0%, тренд ±2%, vol off q60) + ядро",
         "SM3: HYST(−3%,±2%,q60), недельная оценка"]
TL = []
for nm in RULES:
    pos = POS[nm].shift(1).fillna(0)
    t = timeliness(pos, px, eps); t.insert(0, "rule", nm); TL.append(t)
TL = pd.concat(TL, ignore_index=True)
TL.to_csv(os.path.join(RES, "F2_timeliness_zigzag.csv"), index=False)
pd.set_option("display.width", 250)
for nm in RULES:
    print(f"\n--- {nm}")
    print(TL[TL.rule == nm].drop(columns=["rule"]).to_string(index=False))
print("\nсводка (медианы по эпизодам):")
S = TL.groupby("rule")[["exit_lag_days", "avoided%", "entry_lag_days", "missed%"]].median().round(0)
S["avoided_mean%"] = TL.groupby("rule")["avoided%"].mean().round(0)
S["missed_mean%"] = TL.groupby("rule")["missed%"].mean().round(0)
S["n_fully_missed_exit"] = TL.groupby("rule")["exit_lag_days"].apply(lambda s: s.isna().sum())
print(S.to_string())
# доля падений ≥15%, в которых правило вообще было в лонге на пике
print("\nдоля эпизодов, где правило стояло в лонге на пике (т.е. падение касалось портфеля):")
for nm in RULES:
    pos = POS[nm].shift(1).fillna(0)
    inlong = [float(pos.reindex([p]).fillna(0).iloc[0]) > 0.5 for p, _, _, _ in eps]
    print(f"  {nm[:55]:55s} {np.mean(inlong)*100:.0f}% ({sum(inlong)} из {len(eps)})")

# ---------------------------------------------------------------- крупнейшие просадки стратегий
print("\n=== пять крупнейших просадок стратегий (дневной MCFTR/деньги, 2010-2026.08) ===")
rows = []
for nm in ["PANEL_M: ворота+ядро, месячная оценка", "PANEL_W: ворота+ядро, недельная оценка",
           "HYST ворота (bond off -3.0%, тренд ±2%, vol off q60) + ядро", "SM3: HYST(−3%,±2%,q60), недельная оценка"]:
    bt = backtest(d, POS[nm], 0.002, "2010-01-01", "2026-08-31")
    eq = np.exp(bt["r"].cumsum()); dd = eq / eq.cummax() - 1
    under = (dd < 0).values; i = 0; segs = []
    while i < len(dd):
        if under[i]:
            j = i
            while j < len(dd) and under[j]: j += 1
            seg = dd.iloc[i:j]; segs.append((seg.min(), dd.index[i - 1] if i else dd.index[i], seg.idxmin(), dd.index[j] if j < len(dd) else None, bt["pos"].iloc[i:j].mean()))
            i = j
        else:
            i += 1
    segs.sort()
    for depth, p, t, r, share in segs[:5]:
        rows.append({"rule": nm[:45], "depth%": round(depth * 100, 1), "peak": p.date(), "trough": t.date(),
                     "recovered": r.date() if r is not None else None, "days_to_trough": int((t - p).days),
                     "share_long_in_dd%": round(share * 100)})
DD = pd.DataFrame(rows); print(DD.to_string(index=False))
DD.to_csv(os.path.join(RES, "F2_strategy_drawdowns.csv"), index=False)
print("\nготово: results/F2_timeliness_zigzag.csv, F2_strategy_drawdowns.csv")
