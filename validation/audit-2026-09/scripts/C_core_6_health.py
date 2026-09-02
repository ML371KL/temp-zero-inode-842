"""C_core задача 6 — здоровье: IC-24 ушёл в минус. Скользящие IC по ногам 2023–2026; тест на слом
(sup-Wald Эндрюса по рангам, разность IC до/после с бутстрепом); ложные тревоги критерия IC-24<0 при
истинном IC 0,2 (симуляция); CUSUM избыточной доходности стратегии; пробит-тест по знаку;
мощность: сколько месяцев нужно, чтобы отличить IC 0 от 0,2."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from C_core_lib import *

D, M, C = load()
me = month_end_idx(D)
mk = monthly_market(D, C, me)
Mm = M.reindex(me)
comp = Mm["composite"]
fwd = mk["fwd_imoex"]
gate = (Mm["cell"] != TOXIC).astype(float)
Z = pd.DataFrame({k: sgn * Mm["z_" + k] for k, sgn in LEGS})
Z.columns = ["usd", "slope", "urals"]
sigs = {"composite": comp, **{k: Z[k] for k in Z}}


def rolling_ic(sig, f, w):
    df = pd.concat([sig.rename("s"), f.rename("f")], axis=1)
    out = pd.Series(np.nan, index=df.index)
    for i in range(len(df)):
        sub = df.iloc[max(0, i - w + 1):i + 1].dropna()
        if len(sub) >= max(12, int(w * 0.8)):
            out.iloc[i] = stats.spearmanr(sub["s"], sub["f"])[0]
    return out


# ---------------------------------------------------------------- 6а. скользящие IC (закрытые месяцы до 2026-07 включительно: fwd 2026-08 известен)
print("=== скользящие IC-24 / IC-36 / IC-60 по ногам и композиту, последние срезы ===")
ic24 = {k: rolling_ic(s, fwd, 24) for k, s in sigs.items()}
ic36 = {k: rolling_ic(s, fwd, 36) for k, s in sigs.items()}
ic60 = {k: rolling_ic(s, fwd, 60) for k, s in sigs.items()}
T = pd.DataFrame({f"{k}_ic24": v for k, v in ic24.items()} | {f"{k}_ic36": v for k, v in ic36.items()} | {f"{k}_ic60": v for k, v in ic60.items()})
print(T.loc["2024-06-30":].round(3).to_string())
T.to_csv(f"{RES}/C_core_6_rolling_ic.csv", float_format="%.4f")
last = T.dropna(subset=["composite_ic24"]).index[-1]
print(f"\nпоследний срез с известным fwd: {last.date()}; IC-24 композита = {T.loc[last,'composite_ic24']:+.3f}; по ногам: " + ", ".join(f"{k} {T.loc[last, k+'_ic24']:+.3f}" for k in ["usd", "slope", "urals"]))
# сколько месяцев IC-24 композита < 0 в истории при IC полной выборки +0,22
h = ic24["composite"].loc["2010-01-01":]
print(f"история 2010+: доля месяцев с IC-24 композита < 0 = {(h < 0).mean():.2f} (n={h.notna().sum()}); эпизоды подряд <0: ", end="")
runs, cur = [], 0
for v in h.values:
    if np.isfinite(v) and v < 0:
        cur += 1
    else:
        if cur:
            runs.append(cur)
        cur = 0
if cur:
    runs.append(cur)
print(runs)
neg = h[h < 0]
print("месяцы с IC-24<0 (2010+):", ", ".join(str(d.date())[:7] for d in neg.index))

# IC по календарным годам 2023–2026 по ногам
print("\n=== IC по календарным годам (n=12; 2026 — 7 мес) ===")
rows = []
for y in (2022, 2023, 2024, 2025, 2026):
    line = f"{y}: "
    for k, s in sigs.items():
        m = s.index.year == y
        r = ic_stats(s[m], fwd[m], n_boot=200, min_n=6)
        rows.append(dict(year=y, signal=k, n=r["n"], ic=r["ic"]))
        line += f"{k} {r['ic']:+.2f} (n={r['n']})  "
    print(line)
pd.DataFrame(rows).to_csv(f"{RES}/C_core_6_ic_by_year.csv", index=False, float_format="%.4f")

# ---------------------------------------------------------------- 6б. тесты на слом
print("\n=== тест на слом связи composite→fwd (ранговая регрессия, 2010-01..2026-07) ===")
m = (comp.index >= MAIN[0]) & (comp.index <= MAIN[1])
df = pd.DataFrame({"s": comp[m], "f": fwd[m]}).dropna()
xr = stats.rankdata(df["s"]); yr = stats.rankdata(df["f"])
xs = (xr - xr.mean()) / xr.std(); ys = (yr - yr.mean()) / yr.std()
n = len(xs)
# sup-Wald Эндрюса (β до/после, HAC lag 3) по датам в средних 70% выборки
def wald_at(k):
    d1 = np.arange(n) < k
    Xd = np.column_stack([np.ones(n), xs, xs * (~d1)])  # β_pre + Δβ_post
    beta, *_ = np.linalg.lstsq(Xd, ys, rcond=None)
    u = ys - Xd @ beta
    # HAC
    L = 3
    S = (Xd * u[:, None]).T @ (Xd * u[:, None])
    for l in range(1, L + 1):
        w = 1 - l / (L + 1)
        G = (Xd[l:] * u[l:, None]).T @ (Xd[:-l] * u[:-l, None])
        S += w * (G + G.T)
    XtX_inv = np.linalg.inv(Xd.T @ Xd)
    V = XtX_inv @ S @ XtX_inv
    return beta[2] ** 2 / V[2, 2], beta[1], beta[1] + beta[2]
ks = range(int(0.15 * n), int(0.85 * n))
W = [(df.index[k], *wald_at(k)) for k in ks]
best = max(W, key=lambda t: t[1])
print(f"sup-Wald = {best[1]:.2f} при разрыве {best[0].date()} (β_pre={best[2]:+.3f}, β_post={best[3]:+.3f}); крит. значения Эндрюса (1 параметр, π0=0,15): 10% 7,17; 5% 8,85; 1% 12,35")
for dt in ("2022-03-31", "2024-01-31", "2025-01-31", "2025-07-31"):
    k = int((df.index < pd.Timestamp(dt)).sum())
    w_, b1, b2 = wald_at(k)
    print(f"  разрыв на {dt}: Wald={w_:.2f} β_pre={b1:+.3f} β_post={b2:+.3f} (n_post={n-k})")
pd.DataFrame(W, columns=["date", "wald", "beta_pre", "beta_post"]).to_csv(f"{RES}/C_core_6_supwald.csv", index=False, float_format="%.4f")

# разность IC до/после 2025-01 с бутстрепом
print("\nразность IC (2025-01..2026-07) − (2010-01..2024-12), стационарный бутстреп:")
pre = df[df.index < "2025-01-01"]; post = df[df.index >= "2025-01-01"]
ic_pre = stats.spearmanr(pre.s, pre.f)[0]; ic_post = stats.spearmanr(post.s, post.f)[0]
rng = np.random.default_rng(3)
bs = []
for _ in range(2000):
    i1 = stationary_bootstrap_idx(len(pre), 6, rng); i2 = stationary_bootstrap_idx(len(post), 6, rng)
    bs.append(stats.spearmanr(post.s.values[i2], post.f.values[i2])[0] - stats.spearmanr(pre.s.values[i1], pre.f.values[i1])[0])
bs = np.array(bs)
print(f"  IC_pre={ic_pre:+.3f} (n={len(pre)}), IC_post={ic_post:+.3f} (n={len(post)}), Δ={ic_post-ic_pre:+.3f}, SE={bs.std():.3f}, p={2*(1-stats.norm.cdf(abs(ic_post-ic_pre)/bs.std())):.2f}")
print(f"  под H0 «IC_post = IC_pre = {ic_pre:+.2f}»: вероятность наблюдать IC_post ≤ {ic_post:+.2f} при n={len(post)} ≈ {stats.norm.cdf((np.arctanh(ic_post)-np.arctanh(ic_pre))*np.sqrt(len(post)-3)):.2f} (Фишер z)")

# ---------------------------------------------------------------- 6в. симуляция ложных тревог IC-24
print("\n=== симуляция: сигнал AR(1) φ=0,8, истинный IC ρ; ложные тревоги критериев на 24/36/60-мес окне ===")
rng = np.random.default_rng(11)
def simulate(rho, n_months=200, n_sim=2000, w=24):
    phi = 0.8
    fa_neg, fa_6run = 0, 0
    for _ in range(n_sim):
        e = rng.normal(0, np.sqrt(1 - phi ** 2), n_months)
        s = np.zeros(n_months); s[0] = rng.normal()
        for i in range(1, n_months):
            s[i] = phi * s[i - 1] + e[i]
        # fwd = rho-скоррелированный шум (Пирсон ≈ Спирмен для нормальных)
        f = rho * (s / s.std()) + np.sqrt(1 - rho ** 2) * rng.normal(size=n_months)
        ics = np.array([stats.spearmanr(s[i - w + 1:i + 1], f[i - w + 1:i + 1])[0] for i in range(w - 1, n_months)])
        if (ics < 0).any():
            fa_neg += 1
        # 6 подряд < 0
        run = 0; hit6 = False
        for v in ics:
            run = run + 1 if v < 0 else 0
            if run >= 6:
                hit6 = True; break
        if hit6:
            fa_6run += 1
    return fa_neg / n_sim, fa_6run / n_sim
rows = []
for w in (24, 36, 60):
    for rho in (0.0, 0.1, 0.2):
        p_neg, p_6 = simulate(rho, w=w, n_sim=500)
        rows.append(dict(window=w, true_ic=rho, p_any_negative_in_200m=p_neg, p_6run_negative=p_6))
        print(f"окно {w:3d}, истинный IC {rho:.1f}: за 200 мес хотя бы раз IC<0 — {p_neg:.2f}; хотя бы раз 6 подряд <0 — {p_6:.2f}")
pd.DataFrame(rows).to_csv(f"{RES}/C_core_6_false_alarms.csv", index=False, float_format="%.4f")
se24 = 1 / np.sqrt(24 - 3)
print(f"аналитически: SE(IC-24) ≈ {se24:.2f}; при истинном IC 0,20 P(IC-24<0) на одном окне ≈ {stats.norm.cdf(-np.arctanh(0.2)/se24):.2f}; при IC 0,10 ≈ {stats.norm.cdf(-np.arctanh(0.1)/se24):.2f}")

# ---------------------------------------------------------------- 6г. мощность
print("\n=== мощность: n месяцев, чтобы отличить IC=0 от IC=0,2 (Фишер z; SE=1/√(n−3)) ===")
rows = []
for alpha, beta_ in ((0.05, 0.8), (0.10, 0.8), (0.05, 0.5)):
    za = stats.norm.ppf(1 - alpha); zb = stats.norm.ppf(beta_)
    n1 = ((za + zb) / np.arctanh(0.2)) ** 2 + 3
    za2 = stats.norm.ppf(1 - alpha / 2)
    n2 = ((za2 + zb) / np.arctanh(0.2)) ** 2 + 3
    rows.append(dict(alpha=alpha, power=beta_, n_one_sided=n1, n_two_sided=n2))
    print(f"α={alpha}, мощность {beta_:.0%}: односторонний n≈{n1:.0f} мес ({n1/12:.1f} лет), двусторонний n≈{n2:.0f}")
# отличить IC 0,2 от IC 0 при уже накопленных 200 мес истории: тест «IC последних n = 0» против «= 0,2»
n_die = ((stats.norm.ppf(0.9) + stats.norm.ppf(0.8)) / np.arctanh(0.2)) ** 2 + 3
print(f"чтобы ЗАМЕТИТЬ смерть (H0: IC=0,2 против H1: IC=0) с α=0,10 и мощностью 80%: n≈{n_die:.0f} мес")
pd.DataFrame(rows).to_csv(f"{RES}/C_core_6_power.csv", index=False, float_format="%.2f")

# ---------------------------------------------------------------- 6д. CUSUM стратегии и пробит по знаку
print("\n=== CUSUM избыточной доходности стратегии панели (MCFTR/mm, 0,2%) против её же истории 2010–2024 ===")
pos = panel_positions(Mm)
bt = backtest(pos, mk, start=MAIN[0], end=MAIN[1])
ex = bt["ret"] - bt["fwd_mm"]
ref = ex.loc[:"2024-12-31"]
mu, sd = ref.mean(), ref.std()
cus = ((ex - mu) / sd).loc["2025-01-01":].cumsum()
# граница: 2σ·√k (простая) и бутстреп-квантиль минимума CUSUM длины k из истории
k = len(cus)
rng = np.random.default_rng(5)
mins = []
for _ in range(4000):
    ii = stationary_bootstrap_idx(len(ref), 6, rng)
    seg = ((ref.values[ii][:k] - mu) / sd).cumsum()
    mins.append(seg.min())
mins = np.array(mins)
print(f"с 2025-01 (k={k} мес): CUSUM конечное {cus.iloc[-1]:+.2f}σ, минимум {cus.min():+.2f}σ в {cus.idxmin().date()}; "
      f"бутстреп-квантили минимума при «модель как раньше»: 5% {np.percentile(mins,5):+.2f}, 10% {np.percentile(mins,10):+.2f}, 50% {np.percentile(mins,50):+.2f}")
print(f"избыточная доходность 2025–26: среднее {ex.loc['2025-01-01':].mean()*100:+.2f}%/мес против {mu*100:+.2f}% в 2010–24; t≈{(ex.loc['2025-01-01':].mean()-mu)/(sd/np.sqrt(k)):+.2f}")
cus.to_csv(f"{RES}/C_core_6_cusum.csv", float_format="%.4f")

print("\n=== пробит по знаку: доля месяцев, где знак композита совпал со знаком fwd1m ===")
s = np.sign(comp); fs = np.sign(fwd)
hitm = (s == fs).where(s.notna() & fs.notna())
for lab, (a, b) in {"2010-24": ("2010-01-01", "2024-12-31"), "2025-26": ("2025-01-01", "2026-07-31"), "посл. 12": ("2025-08-01", "2026-07-31"), "посл. 24": ("2024-08-01", "2026-07-31")}.items():
    hh = hitm.loc[a:b].dropna()
    p_half = stats.binomtest(int(hh.sum()), len(hh), 0.5, alternative="less").pvalue
    p_hist = stats.binomtest(int(hh.sum()), len(hh), 0.57, alternative="less").pvalue
    print(f"{lab:9s} hit={hh.mean():.2f} (n={len(hh)}) | p(hit<0.5)={p_half:.2f} | p(hit<0.57 истор.)={p_hist:.2f}")
print("вывод: при n=19–24 биномиальный тест различает hit 0,57 от 0,5 только при отклонениях >0,2")

# ---------------------------------------------------------------- 6е. что именно сломалось: вклад ног в IC-24
print("\n=== разложение: IC-24 композита, если заменить одну ногу на её среднее (убрать её вариацию) ===")
last24 = comp.loc[:"2026-07-31"].dropna().index[-24:]
base = stats.spearmanr(comp[last24], fwd[last24])[0]
print(f"IC-24 полного композита ({last24[0].date()}..{last24[-1].date()}): {base:+.3f}")
for k in ["usd", "slope", "urals"]:
    others = [c for c in ["usd", "slope", "urals"] if c != k]
    alt = Z[others].mean(axis=1)
    print(f"  без {k:6s}: IC-24 = {stats.spearmanr(alt[last24], fwd[last24])[0]:+.3f};  соло {k}: {stats.spearmanr(Z[k][last24], fwd[last24])[0]:+.3f}")
