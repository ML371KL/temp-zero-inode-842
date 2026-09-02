"""C_core задача 7 — альтернативные конструкции тех же экономических ног, split-sample.
usd: mom21/63/126, курс vs MA100/MA200; ставки: slope 10−2/10−1/5−1, y1−key, y2−key, вместе с наклоном;
нефть: urals_rub_gap (месячный, налоговый) vs дневной rb_gap (Brent×курс к 504-дн среднему) vs brent_mom63.
Оценка: соло IC на общей доступности; композит с подменой ноги — IC и Шарп панели на A/B (два разреза:
2018 и 2021) и MAIN; критерий честности — выигрыш в ОБЕИХ половинах. Перебор: ~14 вариантов."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from C_core_lib import *

D, M, C = load()
me = month_end_idx(D)
mk = monthly_market(D, C, me)
Mm = M.reindex(me)
fwd = mk["fwd_imoex"]
gate = (Mm["cell"] != TOXIC).astype(float)
usd = D["usd"]
brent = D["brent"]

# кандидаты (дневные ряды → срез на конец месяца), знак — экономический
cand = {
    # usd
    "usd_mom21 (+)": (np.log(usd / usd.shift(21)), +1), "usd_mom63 (+) [прод]": (D["usd_mom63"], +1), "usd_mom126 (+)": (np.log(usd / usd.shift(126)), +1),
    "usd/MA100 (+)": (np.log(usd / usd.rolling(100).mean()), +1), "usd/MA200 (+)": (np.log(usd / usd.rolling(200).mean()), +1),
    # ставки
    "slope_10_2 (+) [прод]": (D["slope_10_2"], +1), "slope_10_1 (+)": (D["y10"] - D["y1"], +1), "slope_5_1 (+)": (D["zcyc_y5"] if "zcyc_y5" in D else (D["y10"] * 0 + np.nan), +1),
    "y1−key (+: ключ выше кривой=зажим)": (D["y1"] - D["key_rate"], +1), "y2−key (+)": (D["y2"] - D["key_rate"], +1), "y10−key (+)": (D["y10"] - D["key_rate"], +1),
    "Δy2 63д (−)": (D["y2"] - D["y2"].shift(63), -1),
    # нефть
    "urals_rub_gap (−) [прод]": (D["urals_rub_gap"], -1), "rb_gap дневной 504д (−)": (D["rb_gap"], -1), "brent_mom63 (−)": (D["brent_mom63"], -1),
    "rb×usd gap 504д пересчёт (−)": (np.log((brent * usd) / (brent * usd).rolling(504, min_periods=252).mean()), -1),
}
# y5 из raw_long (в дневной панели нет)
raw = pd.read_csv(f"{DATA}/raw_long.csv")
y5 = raw[raw.series == "zcyc_y5"].assign(date=lambda d: pd.to_datetime(d.date)).set_index("date")["value"].reindex(D.index).ffill(limit=5)
y1 = D["y1"]
cand["slope_5_1 (+)"] = (y5 - y1, +1)
cand["slope_10_5 (+)"] = (D["y10"] - y5, +1)

RAW = pd.DataFrame({k: v[0].reindex(me) for k, v in cand.items()})
SGN = {k: v[1] for k, v in cand.items()}
Zc = pd.DataFrame({k: SGN[k] * zroll(RAW[k]) for k in cand})

# ---------------------------------------------------------------- 7а. соло IC
print("=== соло IC (знаковый z60) на общей доступности семейства ===")
fam = {"usd": [k for k in cand if k.startswith("usd")], "rates": [k for k in cand if k.startswith(("slope", "y1", "y2", "y10", "Δy2"))], "oil": [k for k in cand if k.startswith(("urals", "rb", "brent"))]}
rows = []
for fname, ks in fam.items():
    avail = Zc[ks].notna().all(axis=1) & (Zc.index <= MAIN[1]) & (Zc.index >= "2010-01-01")
    a = Zc.index[avail].min()
    print(f"\n{fname}: общая выборка с {a.date()}, n={int(avail.sum())}")
    for k in ks:
        r = ic_stats(Zc[k][avail], fwd[avail], n_boot=500)
        # половины
        mid = Zc.index[avail][int(avail.sum() // 2)]
        r1 = ic_stats(Zc[k][avail & (Zc.index < mid)], fwd[avail & (Zc.index < mid)], n_boot=300)
        r2 = ic_stats(Zc[k][avail & (Zc.index >= mid)], fwd[avail & (Zc.index >= mid)], n_boot=300)
        rr = ic_stats(SGN[k] * RAW[k][avail], fwd[avail], n_boot=300)
        rows.append(dict(family=fname, candidate=k, n=r["n"], ic=r["ic"], p_boot=r["p_boot"], nw_t=r["nw_t"], ic_half1=r1["ic"], ic_half2=r2["ic"], ic_raw=rr["ic"], split_date=str(mid.date())))
        print(f"  {k:30s} IC={r['ic']:+.3f} (p={r['p_boot']:.3f}, t={r['nw_t']:+.2f}) | половины: {r1['ic']:+.3f} / {r2['ic']:+.3f} (разрез {mid.date()}) | raw IC {rr['ic']:+.3f}")
pd.DataFrame(rows).to_csv(f"{RES}/C_core_7_solo.csv", index=False, float_format="%.4f")

# ---------------------------------------------------------------- 7б. подмена ноги в композите
print("\n=== композит с подменой одной ноги (остальные — прод): IC и панель (ворота + гист 0,1) на разрезах ===")
base_legs = {"usd": "usd_mom63 (+) [прод]", "rates": "slope_10_2 (+) [прод]", "oil": "urals_rub_gap (−) [прод]"}
splits = {"A ..2017": ("2010-01-01", "2017-12-31"), "B 2018+": ("2018-01-01", "2026-08-31"), "A' 2016-02..2020": ("2016-02-01", "2020-12-31"), "B' 2021+": ("2021-01-01", "2026-08-31"), "MAIN": MAIN}
rows = []
rets = {}


def eval_comp(comp, name):
    hs = hysteresis_sign(comp, 0.1)
    pos = (hs > 0).astype(float) * gate
    out = {}
    for sname, (a, b) in splits.items():
        m = (comp.index >= a) & (comp.index <= b)
        r = ic_stats(comp[m], fwd[m], n_boot=300)
        bt = backtest(pos, mk, start=a, end=b)
        mt = metrics(bt)
        rets[(name, sname)] = bt["ret"]
        out[sname] = (r["ic"], mt.get("sharpe", np.nan), mt.get("maxdd", np.nan), mt.get("trades_yr", np.nan))
        rows.append(dict(variant=name, split=sname, n=r["n"], ic=r["ic"], p_boot=r["p_boot"], sharpe_panel=mt.get("sharpe"), cagr_panel=mt.get("cagr"), mdd_panel=mt.get("maxdd"), trades_yr=mt.get("trades_yr"), time_in=mt.get("time_in")))
    return out


def show(name, out):
    print(f"{name:44s}" + " | ".join(f"{s}: IC {v[0]:+.3f} Sh {v[1]:.2f} MDD {v[2]*100:.0f}%" for s, v in out.items()))


base = pd.DataFrame({f: Zc[k] for f, k in base_legs.items()}).mean(axis=1)
show("прод (usd_mom63 + slope_10_2 + urals_gap)", eval_comp(base, "прод"))
for fname, ks in fam.items():
    print(f"--- семейство {fname} ---")
    for k in ks:
        if k == base_legs[fname]:
            continue
        legs = dict(base_legs); legs[fname] = k
        comp = pd.DataFrame({f: Zc[kk] for f, kk in legs.items()}).mean(axis=1)
        show(f"{fname} → {k}", eval_comp(comp, f"{fname} → {k}"))
# добавление четвёртой ноги
print("--- добавление ноги к трём продовым ---")
for k in ["y1−key (+: ключ выше кривой=зажим)", "y2−key (+)", "Δy2 63д (−)", "usd_mom21 (+)", "rb_gap дневной 504д (−)", "brent_mom63 (−)"]:
    comp = pd.DataFrame({**{f: Zc[kk] for f, kk in base_legs.items()}, "extra": Zc[k]}).mean(axis=1)
    show(f"прод + {k}", eval_comp(comp, f"прод + {k}"))
# замена наклона на пару (slope + y2−key) — «фискальная премия» раздельно
comp = pd.DataFrame({"usd": Zc[base_legs["usd"]], "oil": Zc[base_legs["oil"]], "slope": Zc["slope_10_2 (+) [прод]"], "y2k": Zc["y2−key (+)"]}).mean(axis=1)
show("прод + y2−key (4 ноги)", eval_comp(comp, "прод + y2−key (4 ноги)"))
comp = pd.DataFrame({"usd": Zc[base_legs["usd"]], "oil": Zc[base_legs["oil"]], "rates": (Zc["slope_10_2 (+) [прод]"] + Zc["y2−key (+)"]) / 2}).mean(axis=1)
show("прод, ставочная нога = ½(slope + y2−key)", eval_comp(comp, "ставочная нога = ½(slope + y2−key)"))
T = pd.DataFrame(rows)
T.to_csv(f"{RES}/C_core_7_substitutions.csv", index=False, float_format="%.4f")

# ---------------------------------------------------------------- 7в. честный итог: побеждает в обеих половинах?
print("\n=== кто побеждает прод в ОБЕИХ половинах (по IC и по Шарпу панели), оба разреза ===")
P = T.pivot(index="variant", columns="split", values=["ic", "sharpe_panel"])
b = P.loc["прод"]
rows = []
for v in P.index:
    if v == "прод":
        continue
    win_ic_2018 = (P.loc[v, ("ic", "A ..2017")] > b[("ic", "A ..2017")]) and (P.loc[v, ("ic", "B 2018+")] > b[("ic", "B 2018+")])
    win_ic_2021 = (P.loc[v, ("ic", "A' 2016-02..2020")] > b[("ic", "A' 2016-02..2020")]) and (P.loc[v, ("ic", "B' 2021+")] > b[("ic", "B' 2021+")])
    win_sh_2018 = (P.loc[v, ("sharpe_panel", "A ..2017")] > b[("sharpe_panel", "A ..2017")]) and (P.loc[v, ("sharpe_panel", "B 2018+")] > b[("sharpe_panel", "B 2018+")])
    win_sh_2021 = (P.loc[v, ("sharpe_panel", "A' 2016-02..2020")] > b[("sharpe_panel", "A' 2016-02..2020")]) and (P.loc[v, ("sharpe_panel", "B' 2021+")] > b[("sharpe_panel", "B' 2021+")])
    d, p, ci = sharpe_diff_boot(rets[(v, "MAIN")], rets[("прод", "MAIN")])
    rows.append(dict(variant=v, win_ic_split2018=win_ic_2018, win_ic_split2021=win_ic_2021, win_sh_split2018=win_sh_2018, win_sh_split2021=win_sh_2021,
                     d_ic_main=P.loc[v, ("ic", "MAIN")] - b[("ic", "MAIN")], d_sh_main=d, p_sh=p, p_bonf=min(1, p * 14)))
W = pd.DataFrame(rows).sort_values("d_sh_main", ascending=False)
print(W.to_string(index=False, float_format=lambda x: f"{x:+.3f}"))
W.to_csv(f"{RES}/C_core_7_winners.csv", index=False, float_format="%.4f")
print(f"\nвариантов перебрано: {len(W)}; ожидаемое число «побед в обеих половинах» по IC при нулевом эффекте ≈ {len(W)*0.25:.1f} (0,25 на вариант)")
