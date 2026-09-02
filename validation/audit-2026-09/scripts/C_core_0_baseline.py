"""C_core шаг 0 — калибровка движка: b&h MCFTR, деньги, правило панели, ядро без ворот.
Сверка с числами исследования (REGIME §4 / бриф)."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from C_core_lib import *

D, M, C = load()
me = month_end_idx(D)
mk = monthly_market(D, C, me)
Mm = M.reindex(me)

# позиции
pos_panel = panel_positions(Mm)                      # ворота + знак закрытого месяца (гистерезис 0,1)
pos_core = panel_positions(Mm, gate=False)           # только знак ядра
pos_core_raw = (Mm["composite"] > 0).astype(float)   # знак без гистерезиса
pos_gate = (Mm["cell"] != TOXIC).astype(float)       # только ворота
pos_bh = pd.Series(1.0, index=Mm.index)
pos_mm = pd.Series(0.0, index=Mm.index)

rows = []
for win, (a, b) in {"MAIN 2010-26": MAIN, "FULL 2004-26": FULL, **ERAS}.items():
    for name, pos in [("b&h MCFTR", pos_bh), ("100% деньги", pos_mm), ("панель: ворота+знак", pos_panel),
                      ("ядро без ворот (гист.)", pos_core), ("ядро без ворот (знак)", pos_core_raw),
                      ("только ворота", pos_gate)]:
        for cost in (0.002,):
            df = backtest(pos, mk, cost=cost, start=a, end=b)
            m = metrics(df)
            rows.append(dict(window=win, rule=name, cost=cost, **m))
            print(f"{win:13s} {name:26s} {fmt_metrics(m)}")
    print()
# ex-2022
mask = era_slices(Mm.index)["ex-2022"]
for name, pos in [("b&h MCFTR", pos_bh), ("панель: ворота+знак", pos_panel), ("ядро без ворот (гист.)", pos_core)]:
    df = backtest(pos, mk, start=MAIN[0], end=MAIN[1])
    df = df[df.index.year != 2022]
    m = metrics(df)
    rows.append(dict(window="ex-2022", rule=name, cost=0.002, **m))
    print(f"{'ex-2022':13s} {name:26s} {fmt_metrics(m)}")

# ценовая версия (IMOEX лог, флэт=0) для сверки с REGIME §4
print("\n--- ценовая сверка с исследованием (IMOEX log, флэт 0, без издержек) ---")
for win, (a, b) in {"MAIN 2010-26": MAIN, "FULL 2004-26": FULL}.items():
    sub = Mm[(Mm.index >= a) & (Mm.index <= b)]
    f = mk["fwd_imoex"].reindex(sub.index)
    for name, pos in [("панель", pos_panel), ("ядро", pos_core_raw)]:
        p = pos.reindex(sub.index).fillna(0)
        r = (f * p).dropna()
        cum = np.exp(r.cumsum())
        dd = (cum / cum.cummax() - 1).min()
        print(f"{win} {name}: ann={r.mean()*12*100:+.1f}% Sh={r.mean()/r.std()*np.sqrt(12):.2f} MDD={dd*100:.1f}% n={len(r)}")

# чувствительность к издержкам
print("\n--- издержки 0,1 / 0,2 / 0,3 % (MAIN, панель) ---")
for cost in (0.001, 0.002, 0.003):
    m = metrics(backtest(pos_panel, mk, cost=cost, start=MAIN[0], end=MAIN[1]))
    print(f"cost={cost*100:.1f}%: {fmt_metrics(m)}")
    rows.append(dict(window="MAIN 2010-26", rule="панель: ворота+знак", cost=cost, **m))

pd.DataFrame(rows).to_csv(f"{RES}/C_core_0_baseline.csv", index=False, float_format="%.4f")

# своевременность
print("\n--- своевременность по просадкам MCFTR >15% (с 2004) ---")
tr = C["mcftr_ffill"].reindex(D.index).ffill()
for name, pos in [("панель", pos_panel), ("ядро", pos_core), ("ворота", pos_gate)]:
    t = timeliness(pos, tr, me, thr=0.15, start="2004-01-01")
    t["rule"] = name
    print(name)
    print(t.to_string())
    rows_t = t
    t.to_csv(f"{RES}/C_core_0_timeliness_{name}.csv", index=False, float_format="%.3f")

# IC композита
print("\n--- IC композита к fwd1m (IMOEX log) ---")
for win, mask in era_slices(Mm.index).items():
    r = ic_stats(Mm["composite"][mask], mk["fwd_imoex"][mask])
    print(f"{win:13s} IC={r['ic']:+.3f} n={r['n']} p_sp={r['p_sp']:.3f} NW_t={r['nw_t']:+.2f} p_boot={r['p_boot']:.3f} CI90=[{r['ci_lo']:+.2f},{r['ci_hi']:+.2f}]")
