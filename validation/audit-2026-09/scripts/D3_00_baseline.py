"""D3_00 — эталоны: b&h MCFTR, 100% деньги, правило панели (месячное и с дневными воротами).
Сверка с числами брифа (+7,3%/0,48/−19,9% на IMOEX без денег и издержек) и таблица своевременности."""
import numpy as np
import pandas as pd
from D3_lib import *

df = load_daily()
M = monthly_frame(df)

# --- сверка с recalibrate.py: IMOEX price, fwd1m_log, без денег/издержек, знак без гистерезиса
mp = load_monthly_panel()
mp = mp[mp.index <= "2026-08-31"]
u = mp.dropna(subset=["fwd1m_log", "composite", "cell"])
u = u[u.index >= "2004-01-01"]


def ann_sh_dd(r):
    r = np.asarray(r)
    ann = r.mean() * 12
    sh = r.mean() / r.std() * np.sqrt(12)
    cum = np.cumsum(r)
    dd = (cum - np.maximum.accumulate(cum)).min()
    return 100 * (np.exp(ann) - 1), sh, 100 * (np.exp(dd) - 1)


for nm, rule in (("b&h", np.ones(len(u), bool)), ("слой1", u.composite > 0), ("слой2", u.cell != TOXIC),
                 ("оба", (u.composite > 0) & (u.cell != TOXIC))):
    print("сверка IMOEX 2004+ %-6s ann=%+.1f%% Sharpe=%.2f maxDD=%.1f%%" % ((nm,) + ann_sh_dd(np.where(rule, u.fwd1m_log, 0.0))))

# --- эталонные позиции
pos_bh = pd.Series(1.0, index=df.index)
pos_cash = pd.Series(0.0, index=df.index)
pos_panel = panel_positions(df, M, "monthly")
pos_panel_d = panel_positions(df, M, "daily_gate")
pos_gate_only = monthly_to_daily(((M["cell"] != TOXIC) & M["cell"].notna()).astype(float), df)
pos_core_only = monthly_to_daily((M["hyst"] == 1).astype(float), df)

STRATS = {"b&h MCFTR": pos_bh, "100% деньги": pos_cash, "панель (месячно)": pos_panel,
          "панель (ворота ежедневно)": pos_panel_d, "только ворота": pos_gate_only, "только ядро": pos_core_only}

rows = []
for win in WINDOWS:
    for nm, pos in STRATS.items():
        for cost in (0.002,):
            bt = backtest(df, pos, win, cost)
            m = metrics(bt, nm)
            m.update(window=win, cost=cost)
            rows.append(m)
        if win == "main_2010-2026":
            for cost in (0.001, 0.003):
                m = metrics(backtest(df, pos, win, cost), nm)
                m.update(window=win, cost=cost)
                rows.append(m)
base = pd.DataFrame(rows)
cols = ["window", "cost", "name", "cagr_pct", "vol_pct", "sharpe", "sharpe_ex", "maxdd_pct", "time_in_mkt",
        "trades_per_yr", "beat_bh_years", "hit_months", "hit_vs_bh", "n_months"]
base = base[cols]
print()
print(fmt(base[base.cost == 0.002], 2))
save(base, "baseline_metrics")

# ex-2022 (исключаем месяцы 2022 из ряда доходностей)
rows = []
for nm, pos in STRATS.items():
    bt = backtest(df, pos, "full_2004-2026")
    bt = bt[bt.index.year != 2022]
    m = metrics(bt, nm)
    m.update(window="full_ex2022")
    rows.append(m)
print()
print(fmt(pd.DataFrame(rows)[["window"] + cols[2:]], 2))

# --- бутстреп разности Шарпов панель vs b&h (основное окно)
bt_p = backtest(df, pos_panel, "main_2010-2026")
bt_b = backtest(df, pos_bh, "main_2010-2026")
print("\nΔШарп панель−b&h (2010–2026):", sharpe_diff_boot(monthly_returns(bt_p), monthly_returns(bt_b)))

# --- эпизоды и своевременность
eps = episodes(df, "2004-01-01", 0.15)
print("\nЭпизоды просадок MCFTR >15% с 2004:")
print(fmt(eps, 1))
save(eps, "episodes")
tl = []
for nm in ("панель (месячно)", "панель (ворота ежедневно)", "только ворота"):
    t = timeliness(STRATS[nm], df, eps)
    t.insert(0, "rule", nm)
    tl.append(t)
tl = pd.concat(tl)
print("\nСвоевременность правила панели:")
print(fmt(tl[tl.rule == "панель (месячно)"].drop(columns=["rule"]), 2))
save(tl, "baseline_timeliness")
