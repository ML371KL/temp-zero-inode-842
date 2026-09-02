"""F2_pm §2: статистика ячеек — пересчёт CELL_STATS по закрытым месяцам, ДИ, попарные тесты,
объединение ячеек, in-sample якорь, walk-forward оценка ячеек.
Запуск: python scripts/F2_cells.py (из audit/)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from F2_lib import *
from scipy import stats

FROZEN = {  # constants.CELL_STATS (mean — лог-%, median/worst/best — простые %)
    (1, 0, 0): (0.93, 110, 0.59, 1.22, -16.7, 22.1, "рабочий режим"),
    (1, 0, 1): (0.85, 54, 0.61, 1.28, -8.9, 11.2, "бык с долговой тенью"),
    (1, 1, 0): (3.83, 8, 0.75, 3.78, -4.7, 12.1, "окно входа"),
    (1, 1, 1): (-0.80, 8, 0.50, -0.51, -9.8, 6.8, "перегрев на стрессе"),
    (0, 0, 0): (0.54, 31, 0.61, 1.13, -14.7, 15.5, "вялый медведь"),
    (0, 0, 1): (0.51, 25, 0.64, 1.53, -18.4, 11.8, "медведь с долговой тенью"),
    (0, 1, 0): (1.42, 10, 0.60, 0.48, -9.9, 19.1, "окно входа"),
    (0, 1, 1): (-2.94, 25, 0.54, 0.64, -30.0, 18.0, "токсичная ячейка"),
}

m = load_monthly()
M = m[(m.index >= "2004-01-01") & m["closed"] & m["cell"].notna() & m["fwd1m_log"].notna()].copy()
M["key"] = list(zip(M.st_trend.astype(int), M.st_vol.astype(int), M.st_bond.astype(int)))
M["fwd_pct"] = M["fwd1m_log"] * 100
M["fwd_simple"] = np.expm1(M["fwd1m_log"]) * 100
print(f"закрытых пар с ячейкой: {len(M)}  ({M.index[0].date()} .. {M.index[-1].date()})")
rng = np.random.default_rng(1)


def boot_ci(x, fn, nb=5000):
    x = np.asarray(x)
    if len(x) < 3:
        return (np.nan, np.nan)
    idx = rng.integers(0, len(x), size=(nb, len(x)))
    v = np.array([fn(x[i]) for i in idx])
    return (np.percentile(v, 2.5), np.percentile(v, 97.5))


# ---------------------------------------------------------------- 1. пересчёт по ячейкам
rows = []
for key, fz in FROZEN.items():
    g = M[M.key == key]
    x = g["fwd_pct"].values
    n = len(x)
    mean = x.mean() if n else np.nan
    se = x.std(ddof=1) / np.sqrt(n) if n > 1 else np.nan
    t = mean / se if n > 1 else np.nan
    tcrit = stats.t.ppf(0.975, n - 1) if n > 1 else np.nan
    lo_b, hi_b = boot_ci(x, np.mean)
    lo_med, hi_med = boot_ci(g["fwd_simple"].values, np.median)
    hit = (x > 0).mean() if n else np.nan
    p_hit = stats.binomtest(int((x > 0).sum()), n, 0.5).pvalue if n else np.nan
    rows.append({
        "cell": "|".join(("bull" if key[0] else "bear", "stress" if key[1] else "calm", "stress" if key[2] else "ok")),
        "label": fz[6], "n_frozen": fz[1], "n_closed_now": n,
        "mean_frozen": fz[0], "mean_now": round(mean, 2), "se": round(se, 2), "t": round(t, 2),
        "ci95_t_lo": round(mean - tcrit * se, 2), "ci95_t_hi": round(mean + tcrit * se, 2),
        "ci95_boot_lo": round(lo_b, 2), "ci95_boot_hi": round(hi_b, 2),
        "median_frozen": fz[3], "median_now": round(np.median(g["fwd_simple"]), 2),
        "median_ci_lo": round(lo_med, 2), "median_ci_hi": round(hi_med, 2),
        "hit_frozen": fz[2], "hit_now": round(hit, 2), "p_hit_vs_0.5": round(p_hit, 2),
        "worst_now": round(g["fwd_simple"].min(), 1), "best_now": round(g["fwd_simple"].max(), 1),
        "std": round(x.std(ddof=1), 2), "skew": round(stats.skew(x), 2) if n > 3 else np.nan,
    })
T1 = pd.DataFrame(rows).sort_values("mean_now", ascending=False)
pd.set_option("display.width", 250)
print("\n=== 1. Ячейки: замороженное против пересчёта по закрытым месяцам ===")
print(T1.to_string(index=False))
T1.to_csv(os.path.join(RES, "F2_cells_recount.csv"), index=False)
base = M["fwd_pct"]
print(f"\nбезусловно: n={len(base)} mean={base.mean():+.2f} se={base.std(ddof=1)/np.sqrt(len(base)):.2f} "
      f"median={np.median(M['fwd_simple']):+.2f} hit={(base>0).mean():.2f}")

# ---------------------------------------------------------------- 2. попарные тесты
keys = list(FROZEN)
names = {k: FROZEN[k][6] + " " + str(k) for k in keys}
pw = []
for i, a in enumerate(keys):
    for b in keys[i + 1:]:
        xa, xb = M[M.key == a]["fwd_pct"].values, M[M.key == b]["fwd_pct"].values
        if len(xa) < 3 or len(xb) < 3:
            continue
        tw = stats.ttest_ind(xa, xb, equal_var=False)
        mw = stats.mannwhitneyu(xa, xb, alternative="two-sided")
        # перестановочный тест разности средних
        pool = np.concatenate([xa, xb]); na = len(xa); obs = xa.mean() - xb.mean()
        cnt = 0; NP = 4000
        for _ in range(NP):
            rng.shuffle(pool)
            if abs(pool[:na].mean() - pool[na:].mean()) >= abs(obs):
                cnt += 1
        pw.append({"A": names[a], "B": names[b], "nA": len(xa), "nB": len(xb),
                   "diff_mean": round(obs, 2), "p_welch": round(tw.pvalue, 3),
                   "p_mannwhitney": round(mw.pvalue, 3), "p_perm": round(cnt / NP, 3)})
PW = pd.DataFrame(pw).sort_values("p_welch")
print("\n=== 2. Попарные тесты (28 пар) — отсортировано по p Уэлча ===")
print(PW.to_string(index=False))
PW.to_csv(os.path.join(RES, "F2_cells_pairwise.csv"), index=False)
print(f"\nпар с p_welch<0.05: {(PW.p_welch<0.05).sum()} из {len(PW)}; с p_mannwhitney<0.05: {(PW.p_mannwhitney<0.05).sum()}; "
      f"с p_perm<0.05: {(PW.p_perm<0.05).sum()}")

# токсичная против всех остальных вместе
tox = M[M.key == (0, 1, 1)]["fwd_pct"].values
rest = M[M.key != (0, 1, 1)]["fwd_pct"].values
print(f"токсичная ({len(tox)}) против остальных ({len(rest)}): diff={tox.mean()-rest.mean():+.2f} "
      f"p_welch={stats.ttest_ind(tox, rest, equal_var=False).pvalue:.3f} p_MW={stats.mannwhitneyu(tox, rest).pvalue:.3f}; "
      f"токсичная против нуля: t={tox.mean()/(tox.std(ddof=1)/np.sqrt(len(tox))):+.2f} p={stats.ttest_1samp(tox,0).pvalue:.3f}")
ent = M[M.key.isin([(1, 1, 0), (0, 1, 0)])]["fwd_pct"].values
oth = M[~M.key.isin([(1, 1, 0), (0, 1, 0), (0, 1, 1)])]["fwd_pct"].values
print(f"окна входа ({len(ent)}) против прочих нетоксичных ({len(oth)}): diff={ent.mean()-oth.mean():+.2f} "
      f"p_welch={stats.ttest_ind(ent, oth, equal_var=False).pvalue:.3f} p_MW={stats.mannwhitneyu(ent, oth).pvalue:.3f}")

# ---------------------------------------------------------------- 3. разбиения: in-sample и walk-forward
def part(name):
    k = M.key
    if name == "P8: 8 ячеек": return k.map(lambda t: str(t))
    if name == "P3: токсичная/окна входа/прочие":
        return k.map(lambda t: "tox" if t == (0, 1, 1) else ("entry" if t in [(1, 1, 0), (0, 1, 0)] else "rest"))
    if name == "P2: токсичная/прочие": return k.map(lambda t: "tox" if t == (0, 1, 1) else "rest")
    if name == "P2b: облигации стресс/ок": return k.map(lambda t: t[2])
    if name == "P2v: вола стресс/спокойно": return k.map(lambda t: t[1])
    if name == "P2t: бык/медведь": return k.map(lambda t: t[0])
    if name == "P4: вола×облигации": return k.map(lambda t: (t[1], t[2]))
    if name == "P4b: тренд×облигации": return k.map(lambda t: (t[0], t[2]))
    if name == "P1: без разбиения": return k.map(lambda t: 0)


parts = ["P1: без разбиения", "P2: токсичная/прочие", "P2b: облигации стресс/ок", "P2v: вола стресс/спокойно",
         "P2t: бык/медведь", "P4: вола×облигации", "P4b: тренд×облигации", "P3: токсичная/окна входа/прочие", "P8: 8 ячеек"]
OOS = M.index >= "2010-01-01"
res = []
y = M["fwd_pct"].values
for p in parts:
    g = part(p).astype(str).values
    groups = [y[g == u] for u in pd.unique(g)]
    kw = stats.kruskal(*groups).pvalue if len(groups) > 1 else np.nan
    an = stats.f_oneway(*groups).pvalue if len(groups) > 1 else np.nan
    # in-sample R2
    pred_is = pd.Series(y).groupby(g).transform("mean").values
    r2_is = 1 - ((y - pred_is) ** 2).sum() / ((y - y.mean()) ** 2).sum()
    # walk-forward: расширяющееся окно, прогноз = среднее группы по парам СТРОГО до t
    pred, act, pred_shr, flat_signal = [], [], [], []
    for i in np.where(OOS)[0]:
        past = slice(0, i)
        gp, yp = g[past], y[past]
        sel = yp[gp == g[i]]
        grand = yp.mean()
        pv = sel.mean() if len(sel) >= 5 else grand
        # усадка к общему среднему с k=12
        shr = (len(sel) * sel.mean() + 12 * grand) / (len(sel) + 12) if len(sel) else grand
        pred.append(pv); pred_shr.append(shr); act.append(y[i])
    pred, act, pred_shr = map(np.array, (pred, act, pred_shr))
    ic = stats.spearmanr(pred, act)[0] if np.std(pred) > 0 else np.nan
    ic_shr = stats.spearmanr(pred_shr, act)[0] if np.std(pred_shr) > 0 else np.nan
    # OOS R2 относительно расширяющегося безусловного среднего
    base_pred = np.array([y[:i].mean() for i in np.where(OOS)[0]])
    r2_oos = 1 - ((act - pred) ** 2).sum() / ((act - base_pred) ** 2).sum()
    r2_oos_shr = 1 - ((act - pred_shr) ** 2).sum() / ((act - base_pred) ** 2).sum()
    # OOS ворота: флэт, если прогноз группы < 0 (лог-доходность IMOEX, флэт=0)
    lf = np.where(pred < 0, 0.0, act)
    lf_ann, lf_sh = lf.mean() * 12, lf.mean() / lf.std() * np.sqrt(12)
    res.append({"partition": p, "n_groups": len(groups), "p_kruskal": round(kw, 3), "p_anova": round(an, 3),
                "R2_in_sample%": round(r2_is * 100, 2), "IC_oos": round(ic, 3), "IC_oos_shrunk": round(ic_shr, 3),
                "R2_oos%": round(r2_oos * 100, 2), "R2_oos_shrunk%": round(r2_oos_shr * 100, 2),
                "gate_oos_ann%": round(lf_ann, 1), "gate_oos_sharpe": round(lf_sh, 2),
                "share_flat_oos%": round((pred < 0).mean() * 100, 1)})
R3 = pd.DataFrame(res)
print("\n=== 3. Разбиения: in-sample против walk-forward (OOS 2010+, прогноз = среднее группы по прошлым парам) ===")
print(R3.to_string(index=False))
R3.to_csv(os.path.join(RES, "F2_cells_partitions.csv"), index=False)
bh = act
print(f"справочно OOS b&h (IMOEX-цена, лог): ann {bh.mean()*12:+.1f}% sharpe {bh.mean()/bh.std()*np.sqrt(12):.2f}")

# ---------------------------------------------------------------- 4. in-sample якорь: траектория токсичной ячейки
traj = []
cum = []
for t, row in M.iterrows():
    if row.key == (0, 1, 1):
        cum.append(row.fwd_pct)
    if row.key == (0, 1, 1) or t.month == 12:
        n = len(cum)
        if n:
            mu = np.mean(cum); se = np.std(cum, ddof=1) / np.sqrt(n) if n > 1 else np.nan
            traj.append({"date": t.date(), "n_tox": n, "mean_tox": round(mu, 2), "t": round(mu / se, 2) if n > 1 else np.nan,
                         "median": round(np.median(cum), 2), "hit": round(np.mean(np.array(cum) > 0), 2)})
TR = pd.DataFrame(traj).drop_duplicates("date")
TR.to_csv(os.path.join(RES, "F2_cells_toxic_trajectory.csv"), index=False)
print("\n=== 4. Токсичная ячейка: как выглядела её статистика в реальном времени (расширяющееся окно) ===")
show = TR[TR.date.astype(str).isin(["2008-07-31", "2008-08-29", "2008-09-30", "2008-10-31", "2008-12-31", "2009-12-31",
                                    "2012-12-31", "2015-12-31", "2019-12-30", "2021-12-30", "2022-01-31", "2022-12-30",
                                    "2024-12-30", "2026-07-31"]) | (TR.n_tox <= 3)]
print(show.to_string(index=False))
first10 = TR[(TR.n_tox >= 10) & (TR.mean_tox < 0)]
print("первый момент n>=10 и mean<0:", first10.iloc[0].to_dict() if len(first10) else "нет")
firstt = TR[(TR.n_tox >= 10) & (TR.t < -1.0)]
print("первый момент n>=10 и t<-1:", firstt.iloc[0].to_dict() if len(firstt) else "нет")
tx = M[M.key == (0, 1, 1)]
print("\nтоксичные месяцы по эпизодам:")
ep = tx.groupby(tx.index.year)["fwd_pct"].agg(["count", "mean", "sum"]).round(2)
print(ep.to_string())
for nm, mask in [("без 2008-09", ~tx.index.year.isin([2008, 2009])), ("без 2022", tx.index.year != 2022),
                 ("без 2024-26", ~tx.index.year.isin([2024, 2025, 2026])),
                 ("без 4 худших", tx.fwd_pct.rank() > 4)]:
    z = tx.fwd_pct[mask]
    print(f"  токсичная {nm}: n={len(z)} mean={z.mean():+.2f} median={np.median(np.expm1(z/100))*100:+.2f} hit={(z>0).mean():.2f} "
          f"t={z.mean()/(z.std(ddof=1)/np.sqrt(len(z))):+.2f}")
print(f"  вклад 2008 в сумму токсичной ячейки: {tx[tx.index.year==2008].fwd_pct.sum():.1f} из {tx.fwd_pct.sum():.1f} "
      f"({tx[tx.index.year==2008].fwd_pct.sum()/tx.fwd_pct.sum()*100:.0f}%)")

# ---------------------------------------------------------------- 5. ячейка × знак ядра (совместное правило панели)
M["core_pos"] = (M["core_sign"] == 1)
J = M.groupby(["cell", "core_pos"])["fwd_pct"].agg(n="count", mean="mean", median="median",
                                                    hit=lambda s: (s > 0).mean()).round(2).reset_index()
print("\n=== 5. Ячейка × знак закрытого месяца композита (fwd1m, лог-%) ===")
print(J.to_string(index=False))
J.to_csv(os.path.join(RES, "F2_cells_x_core.csv"), index=False)
for nm, mask in [("ворота открыты & ядро>0 (ЛОНГ по панели)", (M.key != (0, 1, 1)) & M.core_pos),
                 ("ворота открыты & ядро<0 (флэт по ядру)", (M.key != (0, 1, 1)) & ~M.core_pos),
                 ("токсичная & ядро>0 (СЕГОДНЯ)", (M.key == (0, 1, 1)) & M.core_pos),
                 ("токсичная & ядро<0", (M.key == (0, 1, 1)) & ~M.core_pos)]:
    z = M.fwd_pct[mask]
    print(f"  {nm}: n={len(z)} mean={z.mean():+.2f} se={z.std(ddof=1)/np.sqrt(len(z)):.2f} median={z.median():+.2f} hit={(z>0).mean():.2f} "
          f"worst={np.expm1(z.min()/100)*100:+.1f}")
z1 = M.fwd_pct[(M.key != (0, 1, 1)) & M.core_pos]; z0 = M.fwd_pct[(M.key != (0, 1, 1)) & ~M.core_pos]
print(f"  знак ядра внутри открытых ворот: diff={z1.mean()-z0.mean():+.2f} p_welch={stats.ttest_ind(z1,z0,equal_var=False).pvalue:.3f}")
z1 = M.fwd_pct[M.core_pos]; z0 = M.fwd_pct[~M.core_pos]
print(f"  знак ядра без ворот: diff={z1.mean()-z0.mean():+.2f} p_welch={stats.ttest_ind(z1,z0,equal_var=False).pvalue:.3f} n={len(z1)}/{len(z0)}")
print("\nготово: results/F2_cells_*.csv")
