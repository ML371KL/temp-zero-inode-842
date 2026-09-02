"""C_core задача 5 — слепые пятна: 2008, 2022-01, 2024-05, 2026-03. Какая нога держала композит высоко;
нелинейность usd_mom (ускорение девальвации = кризис); разложение наклона на движение 2Y и 10Y —
«наклон работает только когда растёт из-за падения короткого конца» количественно."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from C_core_lib import *
import statsmodels.api as sm

D, M, C = load()
me = month_end_idx(D)
mk = monthly_market(D, C, me)
Mm = M.reindex(me)
fwd = mk["fwd_imoex"]
comp = Mm["composite"]

# дополнительные ряды на срезах
usd = D["usd"]
add = pd.DataFrame({
    "usd_mom21": np.log(usd / usd.shift(21)), "usd_mom63": D["usd_mom63"], "usd_mom126": np.log(usd / usd.shift(126)),
    "usd_accel": np.log(usd / usd.shift(21)) * 3 - D["usd_mom63"],  # темп последних 21 дн против 63-дн темпа
    "usd_vol63": np.log(usd / usd.shift(1)).rolling(63).std() * np.sqrt(252),
    "d_y2_63": D["y2"] - D["y2"].shift(63), "d_y10_63": D["y10"] - D["y10"].shift(63),
    "d_y2_21": D["y2"] - D["y2"].shift(21), "d_y10_21": D["y10"] - D["y10"].shift(21),
    "d_slope_63": D["slope_10_2"] - D["slope_10_2"].shift(63),
    "y2_key": D["y2"] - D["key_rate"], "y1_key": D["y1"] - D["key_rate"], "y10_key": D["y10"] - D["key_rate"],
    "brent_mom63": D["brent_mom63"], "rb_gap": D["rb_gap"], "vol21": D["realized_vol_21"], "rgbi_dd": D["rgbi_dd"],
}).reindex(me)
X = pd.concat([Mm, add], axis=1)

# ---------------------------------------------------------------- 5а. эпизоды
print("=== эпизоды: срезы перед и во время обвала (композит, z ног, сырьё, состояние) ===")
episodes = {"2008": ("2008-04-30", "2008-12-31"), "2022-01": ("2021-09-30", "2022-03-31"), "2024-05": ("2024-02-29", "2024-12-31"),
            "2026-03": ("2025-11-28", "2026-08-31"), "2020-02 (COVID)": ("2019-12-31", "2020-04-30")}
cols = ["composite", "z_usd_mom63", "z_slope_10_2", "z_urals_rub_gap", "raw_usd_mom63", "usd_mom21", "usd_accel", "raw_slope_10_2", "d_y2_63", "d_y10_63", "y2_key", "raw_urals_rub_gap", "cell"]
frames = []
for name, (a, b) in episodes.items():
    sub = X.loc[a:b, cols].copy()
    sub["fwd1m"] = fwd.loc[a:b]
    sub.insert(0, "episode", name)
    frames.append(sub)
    print(f"\n--- {name} ---")
    print(sub.drop(columns="episode").round(3).to_string())
pd.concat(frames).to_csv(f"{RES}/C_core_5_episodes.csv", float_format="%.4f")

# ---------------------------------------------------------------- 5б. нелинейность usd_mom
print("\n=== нелинейность usd_mom63: fwd1m по децилям сырого usd_mom63 (2004–2026, n≈272) и (2010+) ===")
rows = []
for sname, (a, b) in {"FULL": FULL, "MAIN": MAIN}.items():
    m = (X.index >= a) & (X.index <= b)
    df = pd.DataFrame({"u": X["raw_usd_mom63"][m], "f": fwd[m]}).dropna()
    df["dec"] = pd.qcut(df["u"], 10, labels=False, duplicates="drop")
    g = df.groupby("dec").agg(u_lo=("u", "min"), u_hi=("u", "max"), fwd_mean=("f", "mean"), fwd_med=("f", "median"), hit=("f", lambda s: (s > 0).mean()), n=("f", "size"))
    print(f"\n{sname}:")
    print((g.assign(u_lo=g.u_lo * 100, u_hi=g.u_hi * 100, fwd_mean=g.fwd_mean * 100, fwd_med=g.fwd_med * 100)).round(2).to_string())
    g["sample"] = sname
    rows.append(g)
    # квадратичная регрессия по рангам/z: fwd ~ z + z² и fwd ~ z + |z|, NW
    z = X["z_usd_mom63"][m]
    d2 = pd.DataFrame({"f": fwd[m], "z": z, "z2": z ** 2, "az": z.abs()}).dropna()
    for spec in (["z", "z2"], ["z", "az"]):
        mod = sm.OLS(d2["f"], sm.add_constant(d2[spec])).fit(cov_type="HAC", cov_kwds={"maxlags": 3})
        print(f"  fwd ~ {'+'.join(spec)}: " + ", ".join(f"{k}={mod.params[k]*100:+.2f}% (t={mod.tvalues[k]:+.2f})" for k in spec) + f"  R²={mod.rsquared:.3f} n={len(d2)}")
    # ускорение: IC usd_mom63 при usd_accel>0 (ускоряется) и <0
    for lab, cond in (("ускорение (mom21×3 > mom63)", X["usd_accel"][m] > 0), ("замедление", X["usd_accel"][m] <= 0)):
        r = ic_stats(X["z_usd_mom63"][m][cond], fwd[m][cond], n_boot=500)
        print(f"  IC z_usd при {lab}: {r['ic']:+.3f} (n={r['n']}, p={r['p_boot']:.2f})")
    # верхний дециль девальвации: сколько из них — кризисные месяцы (fwd < −10%)
    top = df[df["dec"] == df["dec"].max()]
    print(f"  верхний дециль usd_mom63 (>{top.u.min()*100:.1f}%): fwd среднее {top.f.mean()*100:+.2f}%, доля fwd<−10%: {(top.f < -0.10).mean():.2f}, месяцы: {', '.join(str(d.date())[:7] for d in top.index)}")
    # usd_mom63 в стрессе волы: знак
    for lab, cond in (("vol стресс", X["st_vol"][m] == 1), ("vol спокойно", X["st_vol"][m] == 0), ("usd_vol63 верхний квартиль", X["usd_vol63"][m] > X["usd_vol63"][m].quantile(0.75))):
        r = ic_stats(X["z_usd_mom63"][m][cond], fwd[m][cond], n_boot=500)
        print(f"  IC z_usd при {lab}: {r['ic']:+.3f} (n={r['n']}, p={r['p_boot']:.2f})")
pd.concat(rows).to_csv(f"{RES}/C_core_5_usd_deciles.csv", float_format="%.4f")

# ---------------------------------------------------------------- 5в. разложение наклона
print("\n=== наклон 10−2: разложение движения за 63 дн на короткий и длинный конец (2015+, n≈140) ===")
m = (X.index >= "2015-01-01") & (X.index <= MAIN[1]) & X["raw_slope_10_2"].notna()
zs = X["z_slope_10_2"][m]
f = fwd[m]
rows = []
conds = {
    "все": pd.Series(True, index=zs.index),
    "Δy2(63д) < 0 (короткий конец падает)": X["d_y2_63"][m] < 0, "Δy2(63д) ≥ 0 (короткий конец растёт)": X["d_y2_63"][m] >= 0,
    "Δy10(63д) < 0": X["d_y10_63"][m] < 0, "Δy10(63д) ≥ 0 (длинный конец растёт)": X["d_y10_63"][m] >= 0,
    "бычье укручение: Δslope>0 & Δy2<0": (X["d_slope_63"][m] > 0) & (X["d_y2_63"][m] < 0),
    "медвежье укручение: Δslope>0 & Δy10>0 & Δy2≥0": (X["d_slope_63"][m] > 0) & (X["d_y10_63"][m] > 0) & (X["d_y2_63"][m] >= 0),
    "уплощение: Δslope<0": X["d_slope_63"][m] < 0,
    "y2 < key (рынок ждёт снижения)": X["y2_key"][m] < 0, "y2 ≥ key": X["y2_key"][m] >= 0,
    "slope > 0 (нормальная)": X["raw_slope_10_2"][m] > 0, "slope < 0 (инверсия)": X["raw_slope_10_2"][m] < 0,
}
for cname, cm in conds.items():
    r = ic_stats(zs[cm], f[cm], n_boot=500)
    mean_f = f[cm].mean() * 100
    rows.append(dict(cond=cname, n=r["n"], ic_zslope=r["ic"], p_boot=r["p_boot"], mean_fwd_pct=mean_f))
    print(f"{cname:48s} n={r['n']:3d} IC(z_slope)={r['ic']:+.3f} p={r['p_boot']:.2f} | средний fwd {mean_f:+.2f}%")
pd.DataFrame(rows).to_csv(f"{RES}/C_core_5_slope_decomp.csv", index=False, float_format="%.4f")

# регрессия с взаимодействием: fwd ~ z_slope + z_slope×I(Δy2<0) + I(Δy2<0)
d3 = pd.DataFrame({"f": f, "z": zs, "bull": (X["d_y2_63"][m] < 0).astype(float), "dy2": X["d_y2_63"][m], "dy10": X["d_y10_63"][m]}).dropna()
d3["z_bull"] = d3["z"] * d3["bull"]
mod = sm.OLS(d3["f"], sm.add_constant(d3[["z", "z_bull", "bull"]])).fit(cov_type="HAC", cov_kwds={"maxlags": 3})
print("\nfwd ~ z_slope + z_slope×I(Δy2<0) + I(Δy2<0), NW(3):")
for k in ["z", "z_bull", "bull"]:
    print(f"  {k:7s} {mod.params[k]*100:+.2f}% t={mod.tvalues[k]:+.2f}")
print(f"  наклон при Δy2≥0: {mod.params['z']*100:+.2f}%/z; при Δy2<0: {(mod.params['z']+mod.params['z_bull'])*100:+.2f}%/z; n={len(d3)}")
# прямая проверка: IC самих Δy2 и Δy10 к fwd (знак −: рост ставок = минус)
for k in ["d_y2_63", "d_y10_63", "d_y2_21", "d_y10_21", "y2_key", "y1_key", "y10_key"]:
    r = ic_stats(X[k][m], f, n_boot=500)
    print(f"  IC({k:9s}) = {r['ic']:+.3f} (n={r['n']}, p={r['p_boot']:.2f})")

# «наклон работает только когда растёт из-за падения короткого конца» — знак z_slope в 2025-26
print("\n=== эра 2025–26: что сделал наклон ===")
sub = X.loc["2024-12-31":"2026-08-31", ["composite", "z_slope_10_2", "raw_slope_10_2", "d_y2_63", "d_y10_63", "y2_key", "z_usd_mom63", "z_urals_rub_gap"]].copy()
sub["fwd1m"] = fwd.loc["2024-12-31":"2026-08-31"]
print(sub.round(3).to_string())
mm = (X.index >= "2025-01-01") & (X.index <= MAIN[1])
for lab, cond in (("Δy2<0", X["d_y2_63"][mm] < 0), ("Δy2≥0", X["d_y2_63"][mm] >= 0)):
    r = ic_stats(X["z_slope_10_2"][mm][cond], fwd[mm][cond], n_boot=300)
    print(f"2025–26, IC z_slope при {lab}: {r['ic']:+.3f} (n={r['n']})")

# ---------------------------------------------------------------- 5г. что могло бы поймать 2008 внутри тех же рядов
print("\n=== 2008 внутри тех же трёх рядов: кандидаты-флаги (месячные срезы 2004–2026) ===")
mF = (X.index >= FULL[0]) & (X.index <= FULL[1])
cands = {
    "usd_mom21 > +5%": X["usd_mom21"] > 0.05, "usd_mom63 > +10%": X["raw_usd_mom63"] > 0.10, "usd_accel > +5%": X["usd_accel"] > 0.05,
    "usd_vol63 > 15%": X["usd_vol63"] > 0.15, "brent_mom63 < −20%": X["brent_mom63"] < -0.20, "rb_gap < −15%": X["rb_gap"] < -0.15,
    "usd_mom63>0 & brent_mom63<−15%": (X["raw_usd_mom63"] > 0) & (X["brent_mom63"] < -0.15),
}
rows = []
for name, flag in cands.items():
    flag = flag & mF
    r_on = fwd[flag]
    r_off = fwd[mF & ~flag & X["raw_usd_mom63"].notna()]
    rows.append(dict(flag=name, n_on=len(r_on.dropna()), fwd_on=r_on.mean() * 100, fwd_off=r_off.mean() * 100, hit_on=(r_on > 0).mean(), months=", ".join(str(d.date())[:7] for d in r_on.index[:40])))
    print(f"{name:34s} n={len(r_on.dropna()):3d} fwd_on={r_on.mean()*100:+.2f}% (off {r_off.mean()*100:+.2f}%) hit_on={(r_on>0).mean():.2f}")
pd.DataFrame(rows).to_csv(f"{RES}/C_core_5_crisis_flags.csv", index=False, float_format="%.4f")
