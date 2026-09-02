"""C_core задача 1 — вклад ног: соло/пары/тройка, джекнайф, знак по состояниям и эрам,
скользящий IC-36 по годам. Всё на месячных срезах панели, fwd = fwd1m_log IMOEX;
стратегия = long/flat в MCFTR против mm_rate с издержками 0,2% (правила брифа)."""
import sys, os, itertools
sys.path.insert(0, os.path.dirname(__file__))
from C_core_lib import *

D, M, C = load()
me = month_end_idx(D)
mk = monthly_market(D, C, me)
Mm = M.reindex(me)
fwd = mk["fwd_imoex"]

# знаковые z ног (как в проде)
Z = pd.DataFrame({k: sgn * Mm["z_" + k] for k, sgn in LEGS})
Z.columns = ["usd", "slope", "urals"]
names = {"usd": "usd_mom63(+)", "slope": "slope_10_2(+)", "urals": "urals_gap(−)"}
gate = (Mm["cell"] != TOXIC).astype(float)

print("корреляция знаковых z ног (общая выборка n_used=3):")
print(Z[Mm["n_used"] == 3].corr().round(2).to_string())

# ---------------------------------------------------------------- 1а. комбинации
combos = [c for r in (1, 2, 3) for c in itertools.combinations(["usd", "slope", "urals"], r)]
samples = {
    "common 2016-02..2026-08 (все 3 ноги)": (Mm["n_used"] == 3) & (Mm.index <= MAIN[1]),
    "MAIN 2010-01..2026-08 (адаптивно)": (Mm.index >= MAIN[0]) & (Mm.index <= MAIN[1]),
}
rows = []
strat_ret = {}
for sname, smask in samples.items():
    print(f"\n=== {sname}: n={int(smask.sum())} ===")
    for combo in combos:
        comp = Z[list(combo)].mean(axis=1)
        comp = comp.where(smask)
        r = ic_stats(comp, fwd)
        pos_ng = (hysteresis_sign(comp, 0.1) > 0).astype(float).where(smask, 0.0)
        pos_g = pos_ng * gate
        a, b = Mm.index[smask].min(), Mm.index[smask].max()
        m_ng = metrics(backtest(pos_ng, mk, start=a, end=b))
        m_g = metrics(backtest(pos_g, mk, start=a, end=b))
        strat_ret[(sname, combo)] = backtest(pos_g, mk, start=a, end=b)["ret"]
        label = "+".join(combo)
        rows.append(dict(sample=sname, combo=label, n=r["n"], ic=r["ic"], p_boot=r["p_boot"], nw_t=r["nw_t"],
                         ci_lo=r["ci_lo"], ci_hi=r["ci_hi"],
                         cagr_core=m_ng.get("cagr"), sharpe_core=m_ng.get("sharpe"), mdd_core=m_ng.get("maxdd"),
                         cagr_panel=m_g.get("cagr"), sharpe_panel=m_g.get("sharpe"), shex_panel=m_g.get("sharpe_ex"),
                         mdd_panel=m_g.get("maxdd"), time_in=m_g.get("time_in"), trades_yr=m_g.get("trades_yr"),
                         hit=m_g.get("hit"), bh_cagr=m_g.get("bh_cagr"), bh_sharpe=m_g.get("bh_sharpe"), bh_mdd=m_g.get("bh_maxdd")))
        print(f"{label:16s} IC={r['ic']:+.3f} (p_boot={r['p_boot']:.3f}, NW t={r['nw_t']:+.2f}) | ядро: {m_ng.get('cagr',np.nan)*100:+.1f}% Sh={m_ng.get('sharpe',np.nan):.2f} MDD={m_ng.get('maxdd',np.nan)*100:.1f}% | "
              f"с воротами: {m_g.get('cagr',np.nan)*100:+.1f}% Sh={m_g.get('sharpe',np.nan):.2f} MDD={m_g.get('maxdd',np.nan)*100:.1f}% in={m_g.get('time_in',np.nan):.0%} tr/y={m_g.get('trades_yr',np.nan):.1f}")
    # джекнайф: тройка минус пара
    trip = [x for x in rows if x["sample"] == sname and x["combo"] == "usd+slope+urals"][0]
    print("джекнайф (тройка − без ноги):")
    for leg in ["usd", "slope", "urals"]:
        pair = "+".join([l for l in ["usd", "slope", "urals"] if l != leg])
        pr = [x for x in rows if x["sample"] == sname and x["combo"] == pair][0]
        d_sh, p_sh, ci = sharpe_diff_boot(strat_ret[(sname, ("usd", "slope", "urals"))], strat_ret[(sname, tuple(pair.split("+")))])
        print(f"  без {leg:6s}: ΔIC={trip['ic']-pr['ic']:+.3f}  ΔШарп(панель)={d_sh:+.2f} (p={p_sh:.2f}, ДИ90 {ci[0]:+.2f}..{ci[1]:+.2f})")
        rows.append(dict(sample=sname, combo=f"jackknife: тройка − без {leg}", n=trip["n"], ic=trip["ic"] - pr["ic"],
                         p_boot=p_sh, sharpe_panel=d_sh, ci_lo=ci[0], ci_hi=ci[1]))
pd.DataFrame(rows).to_csv(f"{RES}/C_core_1_combos.csv", index=False, float_format="%.4f")

# ------------------------------------------------- 1б. соло raw vs z (вся доступность)
print("\n=== соло-ноги: raw против z, полная доступность до 2026-08 ===")
rows = []
for k, sgn in LEGS:
    short = {"usd_mom63": "usd", "slope_10_2": "slope", "urals_rub_gap": "urals"}[k]
    m = Mm.index <= MAIN[1]
    r_raw = ic_stats(sgn * Mm["raw_" + k][m], fwd[m])
    r_z = ic_stats(Z[short][m], fwd[m])
    m10 = m & (Mm.index >= MAIN[0])
    r_raw10 = ic_stats(sgn * Mm["raw_" + k][m10], fwd[m10])
    r_z10 = ic_stats(Z[short][m10], fwd[m10])
    print(f"{names[short]:16s} raw: IC={r_raw['ic']:+.3f} n={r_raw['n']} p={r_raw['p_boot']:.3f} | z60: IC={r_z['ic']:+.3f} n={r_z['n']} p={r_z['p_boot']:.3f} | с 2010 raw {r_raw10['ic']:+.3f} z {r_z10['ic']:+.3f}")
    rows.append(dict(leg=short, ic_raw=r_raw["ic"], n_raw=r_raw["n"], p_raw=r_raw["p_boot"], ic_z=r_z["ic"], n_z=r_z["n"], p_z=r_z["p_boot"],
                     ic_raw_2010=r_raw10["ic"], ic_z_2010=r_z10["ic"]))
pd.DataFrame(rows).to_csv(f"{RES}/C_core_1_solo_raw_vs_z.csv", index=False, float_format="%.4f")

# ------------------------------------------------- 1в. знак по состояниям и эрам
print("\n=== IC по состояниям (месячный срез, все доступные месяцы ≤2026-08) ===")
comp_all = Mm["composite"]
sigs = {"usd": Z["usd"], "slope": Z["slope"], "urals": Z["urals"], "composite": comp_all}
conds = {
    "все": pd.Series(True, index=Mm.index),
    "бык (trend=1)": Mm["st_trend"] == 1, "медведь (trend=0)": Mm["st_trend"] == 0,
    "спокойно (vol=0)": Mm["st_vol"] == 0, "стресс (vol=1)": Mm["st_vol"] == 1,
    "bond ok": Mm["st_bond"] == 0, "bond stress": Mm["st_bond"] == 1,
    "токсичная ячейка": Mm["cell"] == TOXIC, "ворота открыты": (Mm["cell"] != TOXIC) & Mm["cell"].notna(),
    "ставка: смягчение": Mm["st_rate"] == -1, "ставка: пауза": Mm["st_rate"] == 0, "ставка: ужесточение": Mm["st_rate"] == 1,
}
for k, (a, b) in ERAS.items():
    conds[f"эра {k}"] = (Mm.index >= a) & (Mm.index <= b)
conds["2004-2009"] = (Mm.index >= "2004-01-01") & (Mm.index <= "2009-12-31")
conds["ex-2022 (2010+)"] = era_slices(Mm.index)["ex-2022"]
rows = []
hdr = f"{'условие':22s}" + "".join(f"{s:>22s}" for s in sigs)
print(hdr)
for cname, cm in conds.items():
    cm = cm & (Mm.index <= MAIN[1])
    line = f"{cname:22s}"
    for s, ser in sigs.items():
        r = ic_stats(ser[cm], fwd[cm], n_boot=500)
        rows.append(dict(cond=cname, signal=s, n=r["n"], ic=r["ic"], p_boot=r["p_boot"], nw_t=r["nw_t"]))
        line += f"  {r['ic']:+.2f} (n={r['n']:3d},p={r['p_boot']:.2f})" if np.isfinite(r["ic"]) else f"  {'n/a':>20s}"
    print(line)
pd.DataFrame(rows).to_csv(f"{RES}/C_core_1_ic_by_state.csv", index=False, float_format="%.4f")

# ------------------------------------------------- 1г. скользящий IC-36 по годам
print("\n=== скользящий IC-36 (окно заканчивается в декабре года) и IC календарного года (n=12) ===")
rows = []
years = range(2005, 2027)
tab = {}
for s, ser in sigs.items():
    df = pd.concat([ser.rename("s"), fwd.rename("f")], axis=1)
    roll = pd.Series(np.nan, index=df.index)
    for i in range(len(df)):
        w = df.iloc[max(0, i - 35):i + 1].dropna()
        if len(w) >= 30:
            roll.iloc[i] = stats.spearmanr(w["s"], w["f"])[0]
    col36, col12 = {}, {}
    for y in years:
        end = df[(df.index.year == y)]
        if len(end) == 0:
            continue
        col36[y] = roll[end.index].iloc[-1]
        w = end.dropna()
        col12[y] = stats.spearmanr(w["s"], w["f"])[0] if len(w) >= 10 else np.nan
    tab[s + "_IC36"] = col36
    tab[s + "_IC12"] = col12
T = pd.DataFrame(tab)
print(T.round(2).to_string())
T.to_csv(f"{RES}/C_core_1_rolling_ic_by_year.csv", float_format="%.3f")
print("\nдоля лет с IC36>0 (с 2018 / все):")
for s in sigs:
    c = T[s + "_IC36"].dropna()
    print(f"  {s:10s}: все {(c>0).mean():.2f} (n={len(c)}), 2018+ {(c[c.index>=2018]>0).mean():.2f}, 2023+ {(c[c.index>=2023]>0).mean():.2f}; последние 4: " + ", ".join(f"{y}:{v:+.2f}" for y, v in c.iloc[-4:].items()))
