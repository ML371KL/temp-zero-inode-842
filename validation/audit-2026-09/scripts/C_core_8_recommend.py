"""C_core задача 8 (итог) — кандидаты «рекомендуемого ядра», собранные из находок 1–7, на одной сетке:
P0 прод; P1 прод + гистерезис 0,4; P2 прод + знак MA3; P3 usd/MA200 вместо usd_mom63; P4 прод + y2−key (4 ноги);
P5 = P3+P4; P6 = P5 + гист 0,4; P7 = P3 + гист 0,4. Все — ворота панели + long/flat MCFTR/mm, 0,2%.
Окна: MAIN, FULL, A/B, эры, ex-2022; бутстреп ΔШарп к P0 и к b&h; своевременность; издержки 0,1/0,3;
текущее показание 2026-08-31 / 2026-09-01. ЧЕСТНО: всё выбрано на полной выборке (см. счётчик перебора)."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from C_core_lib import *

D, M, C = load()
me = month_end_idx(D)               # без незавершённого сентября
me_all = month_end_idx(D, drop_unfinished=False)  # с «сегодня» — для текущего показания
mk = monthly_market(D, C, me)
Mm = M.reindex(me)
fwd = mk["fwd_imoex"]
gate = (Mm["cell"] != TOXIC).astype(float)
usd = D["usd"]

# сырые ряды ног (дневные) → месячные срезы (с «сегодня» для показания)
RAWD = {"usd_mom63": D["usd_mom63"], "slope_10_2": D["slope_10_2"], "urals_rub_gap": D["urals_rub_gap"],
        "usd_ma200": np.log(usd / usd.rolling(200).mean()), "y2_key": D["y2"] - D["key_rate"]}
SGN = {"usd_mom63": +1, "slope_10_2": +1, "urals_rub_gap": -1, "usd_ma200": +1, "y2_key": +1}
RAWM = pd.DataFrame({k: v.reindex(me_all) for k, v in RAWD.items()})
Zall = pd.DataFrame({k: SGN[k] * zroll(RAWM[k]) for k in RAWD})
# проверка: прод-композит совпадает
chk = (Zall[["usd_mom63", "slope_10_2", "urals_rub_gap"]].mean(axis=1).reindex(me) - Mm["composite"]).abs().max()
print(f"проверка воспроизведения прод-композита: max|diff|={chk:.1e}")

variants = {
    "P0 прод": (["usd_mom63", "slope_10_2", "urals_rub_gap"], 0.1, 1),
    "P1 прод + гист 0,4": (["usd_mom63", "slope_10_2", "urals_rub_gap"], 0.4, 1),
    "P2 прод + знак MA3": (["usd_mom63", "slope_10_2", "urals_rub_gap"], 0.1, 3),
    "P3 usd/MA200": (["usd_ma200", "slope_10_2", "urals_rub_gap"], 0.1, 1),
    "P4 прод + y2−key": (["usd_mom63", "slope_10_2", "urals_rub_gap", "y2_key"], 0.1, 1),
    "P5 usd/MA200 + y2−key": (["usd_ma200", "slope_10_2", "urals_rub_gap", "y2_key"], 0.1, 1),
    "P6 P5 + гист 0,4": (["usd_ma200", "slope_10_2", "urals_rub_gap", "y2_key"], 0.4, 1),
    "P7 P3 + гист 0,4": (["usd_ma200", "slope_10_2", "urals_rub_gap"], 0.4, 1),
    "P8 P5 + знак MA3": (["usd_ma200", "slope_10_2", "urals_rub_gap", "y2_key"], 0.1, 3),
}
comps, poss = {}, {}
for name, (legs, hyst, ma) in variants.items():
    c = Zall[legs].mean(axis=1)
    sig = c.rolling(ma, min_periods=1).mean() if ma > 1 else c
    hs = hysteresis_sign(sig.reindex(me), hyst)
    comps[name] = (c, sig, hyst)
    poss[name] = (hs > 0).astype(float) * gate

windows = {"MAIN 2010-26": MAIN, "FULL 2004-26": FULL, "A 2004-17": ("2004-01-01", "2017-12-31"), "B 2018-26": ("2018-01-01", "2026-08-31"), **ERAS}
rows = []
rets = {}
print("\n=== метрики (панель = ворота + знак), полная доходность MCFTR / mm, издержки 0,2% ===")
for wname, (a, b) in windows.items():
    print(f"\n--- {wname} ---")
    for name in variants:
        c, sig, hyst = comps[name]
        m = (me >= a) & (me <= b)
        r = ic_stats(sig.reindex(me)[m], fwd[m], n_boot=400)
        bt = backtest(poss[name], mk, start=a, end=b)
        mt = metrics(bt)
        rets[(name, wname)] = bt["ret"]
        rows.append(dict(window=wname, variant=name, ic=r["ic"], p_boot=r["p_boot"], **mt))
        print(f"{name:24s} IC={r['ic']:+.3f} | {fmt_metrics(mt)}")
# ex-2022
print("\n--- ex-2022 (MAIN без 2022) ---")
for name in variants:
    bt = backtest(poss[name], mk, start=MAIN[0], end=MAIN[1])
    bt = bt[bt.index.year != 2022]
    mt = metrics(bt)
    rows.append(dict(window="ex-2022", variant=name, ic=np.nan, p_boot=np.nan, **mt))
    print(f"{name:24s} {fmt_metrics(mt)}")
T = pd.DataFrame(rows)
T.to_csv(f"{RES}/C_core_8_candidates.csv", index=False, float_format="%.4f")

print("\n=== бутстреп ΔШарп (MAIN): кандидат − P0 и кандидат − b&h MCFTR ===")
bh = backtest(pd.Series(1.0, index=me), mk, start=MAIN[0], end=MAIN[1])["ret"]
rows = []
for name in variants:
    d0, p0, ci0 = sharpe_diff_boot(rets[(name, "MAIN 2010-26")], rets[("P0 прод", "MAIN 2010-26")])
    d1, p1, ci1 = sharpe_diff_boot(rets[(name, "MAIN 2010-26")], bh)
    dB, pB, _ = sharpe_diff_boot(rets[(name, "B 2018-26")], rets[("P0 прод", "B 2018-26")])
    dA, pA, _ = sharpe_diff_boot(rets[(name, "A 2004-17")], rets[("P0 прод", "A 2004-17")])
    rows.append(dict(variant=name, d_sh_vs_p0=d0, p_vs_p0=p0, ci_lo=ci0[0], ci_hi=ci0[1], d_sh_vs_bh=d1, p_vs_bh=p1, d_sh_A=dA, p_A=pA, d_sh_B=dB, p_B=pB))
    print(f"{name:24s} vs P0: {d0:+.2f} (p={p0:.2f}, ДИ90 {ci0[0]:+.2f}..{ci0[1]:+.2f}) | A: {dA:+.2f} (p={pA:.2f}) B: {dB:+.2f} (p={pB:.2f}) | vs b&h: {d1:+.2f} (p={p1:.3f})")
pd.DataFrame(rows).to_csv(f"{RES}/C_core_8_sharpe_diff.csv", index=False, float_format="%.4f")

print("\n=== чувствительность к издержкам (MAIN): 0,1 / 0,2 / 0,3 % за смену ===")
for name in ["P0 прод", "P1 прод + гист 0,4", "P3 usd/MA200", "P5 usd/MA200 + y2−key", "P6 P5 + гист 0,4"]:
    line = f"{name:24s}"
    for cost in (0.001, 0.002, 0.003):
        mt = metrics(backtest(poss[name], mk, cost=cost, start=MAIN[0], end=MAIN[1]))
        line += f" | {cost*100:.1f}%: Sh={mt['sharpe']:.2f} CAGR={mt['cagr']*100:+.1f}%"
    print(line)

print("\n=== своевременность (MCFTR, просадки >15%, 2010+) ===")
tr = C["mcftr_ffill"].reindex(D.index).ffill()
frames = []
for name in ["P0 прод", "P1 прод + гист 0,4", "P3 usd/MA200", "P5 usd/MA200 + y2−key", "P6 P5 + гист 0,4"]:
    t = timeliness(poss[name], tr, me, thr=0.15, start="2010-01-01")
    t.insert(0, "variant", name)
    frames.append(t)
    print(name)
    print(t[["peak", "trough", "depth", "days_to_flat", "avoided", "days_to_long", "missed"]].to_string(index=False))
pd.concat(frames).to_csv(f"{RES}/C_core_8_timeliness.csv", index=False, float_format="%.3f")

# годовая таблица: кандидат против b&h и P0
print("\n=== доходность по годам (MAIN): b&h / P0 / P1 / P3 / P5 / P6 ===")
yr = {}
yr["b&h"] = bh.groupby(bh.index.year).apply(lambda s: (1 + s).prod() - 1)
for name in ["P0 прод", "P1 прод + гист 0,4", "P3 usd/MA200", "P5 usd/MA200 + y2−key", "P6 P5 + гист 0,4"]:
    r = rets[(name, "MAIN 2010-26")]
    yr[name] = r.groupby(r.index.year).apply(lambda s: (1 + s).prod() - 1)
Y = pd.DataFrame(yr)
print((Y * 100).round(1).to_string())
Y.to_csv(f"{RES}/C_core_8_yearly.csv", float_format="%.4f")

# текущее показание
print("\n=== текущее показание: значение композита на 2026-07-31 / 2026-08-31 (закрытые) и 2026-09-01 (дневное), знак с гистерезисом, позиция с воротами ===")
rows = []
for name in variants:
    c, sig, hyst = comps[name]
    hs_all = hysteresis_sign(sig, hyst)
    vals = {d: (sig.loc[d], hs_all.loc[d]) for d in [pd.Timestamp("2026-07-31"), pd.Timestamp("2026-08-31"), pd.Timestamp("2026-09-01")]}
    line = f"{name:24s}" + " | ".join(f"{d.date()}: {v:+.2f} знак {int(s) if np.isfinite(s) else 'n/a':>2}" for d, (v, s) in vals.items())
    print(line + f" | ворота 2026-09-01: {'закрыты' if D['cell'].iloc[-1] == TOXIC else 'открыты'}")
    rows.append(dict(variant=name, **{f"val_{d.date()}": v for d, (v, s) in vals.items()}, **{f"sign_{d.date()}": s for d, (v, s) in vals.items()}))
pd.DataFrame(rows).to_csv(f"{RES}/C_core_8_current.csv", index=False, float_format="%.4f")
print("z ног на 2026-09-01:", ", ".join(f"{k} {Zall[k].iloc[-1]:+.2f}" for k in Zall))

print("\n=== счётчик перебора в аудите ядра ===")
print("веса/агрегация 7 + окно z 6 + дневной z 3 + центрирование 4 + обрезка 3 + гистерезис 10 + альтернативы «реже дёргаться» 11 + подмены ног 22 + кандидаты 8 ≈ 74 спецификации.")
print("Бонферрони при 74: нужен p<0,0007; ни одна разница Шарпов до такого не дотягивает. Плацебо-p гистерезиса 0,027 — тоже нет.")
