"""C_core задача 2 — веса и способ агрегации: равные vs обратная вола z, «2 защищённые ноги»,
ранговая сумма, медиана, голосование 2 из 3, трейлинг-IC веса (walk-forward внутри фиксированного набора).
Проверка split-sample 2004–2017 / 2018–2026 + MAIN; плата за перебор — 7 вариантов, Бонферрони и
«выбор лучшего на A → результат на B»."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from C_core_lib import *

D, M, C = load()
me = month_end_idx(D)
mk = monthly_market(D, C, me)
Mm = M.reindex(me)
fwd = mk["fwd_imoex"]
gate = (Mm["cell"] != TOXIC).astype(float)

Z = pd.DataFrame({k: sgn * Mm["z_" + k] for k, sgn in LEGS})
RAW = pd.DataFrame({k: sgn * Mm["raw_" + k] for k, sgn in LEGS})
legs = list(Z.columns)


def pct_rank_roll(x, w=60, mp=24):
    """Перцентиль текущего значения в окне (как calc.percentile_rank_rolling), центрирован −0,5."""
    out = pd.Series(np.nan, index=x.index)
    v = x.values
    for i in range(len(v)):
        if not np.isfinite(v[i]):
            continue
        win = v[max(0, i - w + 1):i + 1]
        win = win[np.isfinite(win)]
        if len(win) < mp:
            continue
        less = (win < v[i]).sum()
        eq = (win == v[i]).sum()
        out.iloc[i] = (less + (eq + 1) / 2.0) / len(win) - 0.5
    return out


constructs = {}
constructs["EW z (прод)"] = Z.mean(axis=1)
constructs["медиана z"] = Z.median(axis=1)
constructs["2 защищённые (usd+slope)"] = Z[["usd_mom63", "slope_10_2"]].mean(axis=1)
# голосование знаков: сумма знаков (+3..−3); при равенстве 0 — держим прошлую позицию (внутри hysteresis 0)
constructs["голосование знаков"] = np.sign(Z).sum(axis=1).where(Z.notna().any(axis=1))
# ранговая сумма
RK = pd.DataFrame({k: pct_rank_roll(RAW[k]) for k in legs})
constructs["ранговая сумма"] = RK.mean(axis=1)
# обратная вола z: веса ∝ 1/std(36 мес) знакового z, только прошлое (окно включает текущую точку)
sd = Z.rolling(36, min_periods=24).std()
w_iv = (1.0 / sd).where(Z.notna())
constructs["обратная вола z(36м)"] = (Z * w_iv).sum(axis=1) / w_iv.sum(axis=1)
# трейлинг-IC веса внутри фиксированного набора (расширяющееся окно, min 36, веса = max(IC,0); все ≤0 → EW)
ic_w = pd.DataFrame(np.nan, index=Z.index, columns=legs)
for i in range(len(Z)):
    hist_s = Z.iloc[:max(0, i - 1)]
    hist_f = fwd.iloc[:max(0, i - 1)]
    for k in legs:
        m = hist_s[k].notna() & hist_f.notna()
        if m.sum() >= 36:
            ic_w.iloc[i, legs.index(k)] = max(stats.spearmanr(hist_s[k][m], hist_f[m])[0], 0.0)
avail = Z.notna()
w_ic = ic_w.where(avail)
row_sum = w_ic.sum(axis=1)
w_ic = w_ic.where(row_sum > 0, avail.astype(float))  # все ≤0 или нет истории → равные среди доступных
constructs["трейлинг-IC веса (WF)"] = (Z * w_ic).sum(axis=1) / w_ic.sum(axis=1)
# in-sample IC-веса (потолок, НЕ рекомендация)
full_ic = {k: max(stats.spearmanr(*pd.concat([Z[k], fwd], axis=1).dropna().values.T)[0], 0) for k in legs}
w_full = pd.DataFrame({k: full_ic[k] for k in legs}, index=Z.index).where(avail)
constructs["in-sample IC-веса (потолок)"] = (Z * w_full).sum(axis=1) / w_full.sum(axis=1)

# гистерезис в масштабе конструкции: 0,1 в единицах σ EW-композита
sig_ew = constructs["EW z (прод)"].loc["2010":"2026-08"].std()
splits = {"A 2004-01..2017-12": ("2004-01-01", "2017-12-31"), "B 2018-01..2026-08": ("2018-01-01", "2026-08-31"),
          "MAIN 2010-01..2026-08": MAIN, "common 2016-02..2026-08": ("2016-02-01", "2026-08-31")}
rows = []
rets = {}
for name, comp in constructs.items():
    thr = 0.1 * comp.loc["2010":"2026-08"].std() / sig_ew if name != "голосование знаков" else 0.0
    hs = hysteresis_sign(comp, thr)
    pos_core = (hs > 0).astype(float)
    pos_panel = pos_core * gate
    flips = (hs.diff().abs() > 0).sum() / (hs.notna().sum() / 12)
    for sname, (a, b) in splits.items():
        m = (comp.index >= a) & (comp.index <= b)
        r = ic_stats(comp[m], fwd[m])
        bp = backtest(pos_panel, mk, start=a, end=b)
        bc = backtest(pos_core, mk, start=a, end=b)
        mp_, mc_ = metrics(bp), metrics(bc)
        rets[(name, sname)] = bp["ret"]
        rows.append(dict(construct=name, split=sname, hyst=thr, n=r["n"], ic=r["ic"], p_boot=r["p_boot"], nw_t=r["nw_t"],
                         sharpe_panel=mp_.get("sharpe"), shex_panel=mp_.get("sharpe_ex"), cagr_panel=mp_.get("cagr"), mdd_panel=mp_.get("maxdd"),
                         time_in=mp_.get("time_in"), trades_yr=mp_.get("trades_yr"), hit=mp_.get("hit"),
                         sharpe_core=mc_.get("sharpe"), cagr_core=mc_.get("cagr"), mdd_core=mc_.get("maxdd"), flips_yr=flips))
T = pd.DataFrame(rows)
T.to_csv(f"{RES}/C_core_2_weights.csv", index=False, float_format="%.4f")
for sname in splits:
    print(f"\n=== {sname} ===")
    sub = T[T.split == sname]
    print(f"{'конструкция':30s} {'IC':>7s} {'p':>6s} {'NWt':>6s} | панель: {'Sh':>5s} {'Shex':>5s} {'CAGR':>6s} {'MDD':>6s} {'in':>4s} {'tr/y':>4s} | ядро: {'Sh':>5s} {'MDD':>6s}")
    for _, x in sub.iterrows():
        print(f"{x.construct:30s} {x.ic:+.3f} {x.p_boot:6.3f} {x.nw_t:+6.2f} |         {x.sharpe_panel:5.2f} {x.shex_panel:5.2f} {x.cagr_panel*100:+5.1f}% {x.mdd_panel*100:5.1f}% {x.time_in:4.0%} {x.trades_yr:4.1f} |       {x.sharpe_core:5.2f} {x.mdd_core*100:5.1f}%")

# бутстреп разности Шарпов против EW на B и MAIN
print("\n=== разность Шарпов (панель) против EW z, стационарный бутстреп блок 8 ===")
rows = []
for sname in ["B 2018-01..2026-08", "MAIN 2010-01..2026-08", "common 2016-02..2026-08"]:
    for name in constructs:
        if name == "EW z (прод)":
            continue
        d, p, ci = sharpe_diff_boot(rets[(name, sname)], rets[("EW z (прод)", sname)])
        rows.append(dict(split=sname, construct=name, d_sharpe=d, p=p, ci_lo=ci[0], ci_hi=ci[1], p_bonf7=min(1, p * 7)))
        print(f"{sname:24s} {name:30s} ΔSh={d:+.2f} p={p:.2f} (Бонферрони×7: {min(1,p*7):.2f}) ДИ90 [{ci[0]:+.2f},{ci[1]:+.2f}]")
pd.DataFrame(rows).to_csv(f"{RES}/C_core_2_sharpe_diff.csv", index=False, float_format="%.4f")

# «выбор на A → результат на B»
A = T[T.split == "A 2004-01..2017-12"].set_index("construct")
B = T[T.split == "B 2018-01..2026-08"].set_index("construct")
cand = [c for c in constructs if "потолок" not in c]
best_ic = A.loc[cand, "ic"].idxmax()
best_sh = A.loc[cand, "sharpe_panel"].idxmax()
print(f"\nЛучший на A по IC: {best_ic} → на B IC={B.loc[best_ic,'ic']:+.3f}, Sh={B.loc[best_ic,'sharpe_panel']:.2f}  (EW на B: IC={B.loc['EW z (прод)','ic']:+.3f}, Sh={B.loc['EW z (прод)','sharpe_panel']:.2f})")
print(f"Лучший на A по Шарпу: {best_sh} → на B IC={B.loc[best_sh,'ic']:+.3f}, Sh={B.loc[best_sh,'sharpe_panel']:.2f}")
print("(На A до 2015 у всех конструкций одна нога usd — различия только с 2015/2016.)")

# согласие знаков конструкций с EW
print("\n=== доля месяцев (2016-02+) с тем же знаком, что у EW z ===")
ew = np.sign(constructs["EW z (прод)"].loc["2016-02":"2026-08"])
for name, comp in constructs.items():
    s = np.sign(comp.loc["2016-02":"2026-08"])
    print(f"{name:30s} {(s == ew).mean():.2f}")
