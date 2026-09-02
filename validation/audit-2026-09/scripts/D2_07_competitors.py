"""D2_07: конкуренты и разложение эффекта главного кандидата (выход по росту y1−ключ >0.25 за 21д).
(1) Против простого вола-фильтра (st_vol) и бонд-бита: инкремент поверх них; (2) без денежной ноги
(флэт=0) — чистый эффект тайминга; (3) как самостоятельное правило поверх buy&hold; (4) помесячная
история срабатываний 2024–2026 на срезах."""
import numpy as np
import pandas as pd
from D2_lib import *

d = derive_rates(load_daily())
m = monthly_frame(d)
idx = d.index; me = month_ends(idx); me_dates = idx[me]
is_me = np.zeros(len(idx), dtype=bool); is_me[me] = True
comp_ok_d = monthly_to_daily_pos((m["comp_sign"] > 0).astype(float), idx)
toxic_d = d["toxic"].fillna(0.0); base_exit = toxic_d == 1; base_entry = toxic_d == 0
vol_d = d["st_vol"].fillna(0.0) == 1; bond_d = d["st_bond"].fillna(0.0) == 1
x_y1 = (d["y1_key_d21"] > 0.25).fillna(False)
x_rs = (d["real_saar_d63"] > 1.0).fillna(False)


def build_pos(exit_cond, entry_cond, cadence="monthly", use_comp=True):
    ex = exit_cond.reindex(idx).fillna(False).values.astype(bool); en = entry_cond.reindex(idx).fillna(False).values.astype(bool)
    co = (comp_ok_d.values > 0) if use_comp else np.ones(len(idx), dtype=bool); pos = np.zeros(len(idx)); cur = 0.0
    for i in range(len(idx)):
        if cadence == "daily" or is_me[i]:
            if cur == 1.0 and ((ex[i] and not en[i]) or not co[i]): cur = 0.0
            elif cur == 0.0 and en[i] and co[i]: cur = 1.0
        pos[i] = cur
    return pd.Series(pos, index=idx)


def bt_monthly(pos, a, b, cost=COST, cash=True):
    pm = pos.iloc[me]; pm.index = me_dates; mm = m.reindex(me_dates).copy()
    if not cash: mm["fwd1m_cash"] = 0.0
    return run_monthly(pm, mm, cost=cost, start=a, end=b)


A, B = "2015-01-01", "2026-08-31"
rows = []
def rep(name, ex, en, use_comp=True, cash=True, a=A, b=B):
    pos = build_pos(ex, en, "monthly", use_comp); mt = metrics(bt_monthly(pos, a, b, cash=cash), freq=12, name=name)
    mt.update(cash=cash, window=f"{a[:4]}_{b[:4]}"); rows.append(mt); return pos

# (1) конкуренты
rep("baseline", base_exit, base_entry)
rep("baseline+y1key", base_exit | x_y1, base_entry & ~x_y1)
rep("exit_vol_bit_only(+toxic)", base_exit | vol_d, base_entry & ~vol_d)
rep("exit_bond_bit_only(+toxic)", base_exit | bond_d, base_entry & ~bond_d)
rep("exit_vol_or_bond(+toxic)", base_exit | vol_d | bond_d, base_entry & ~(vol_d | bond_d))
rep("exit_vol_bit+y1key", base_exit | vol_d | x_y1, base_entry & ~(vol_d | x_y1))
rep("exit_bond_bit+y1key", base_exit | bond_d | x_y1, base_entry & ~(bond_d | x_y1))
rep("baseline+realsaar", base_exit | x_rs, base_entry & ~x_rs)
rep("exit_vol_bit+realsaar", base_exit | vol_d | x_rs, base_entry & ~(vol_d | x_rs))
# (2) без денежной ноги
rep("baseline_nocash", base_exit, base_entry, cash=False)
rep("baseline+y1key_nocash", base_exit | x_y1, base_entry & ~x_y1, cash=False)
rep("exit_vol_bit_only_nocash", base_exit | vol_d, base_entry & ~vol_d, cash=False)
rep("baseline+realsaar_nocash", base_exit | x_rs, base_entry & ~x_rs, cash=False)
# (3) поверх buy&hold (без ворот и композита)
never = pd.Series(False, index=idx); always = pd.Series(True, index=idx)
rep("bh", never, always, use_comp=False)
rep("bh+y1key_exit", x_y1, ~x_y1, use_comp=False)
rep("bh+vol_bit_exit", vol_d, ~vol_d, use_comp=False)
rep("bh+toxic_exit(no core)", base_exit, base_entry, use_comp=False)
rep("bh+toxic+y1key(no core)", base_exit | x_y1, base_entry & ~x_y1, use_comp=False)
rep("core_only(no gate)", never, always, use_comp=True)
rep("core+y1key(no toxic gate)", x_y1, ~x_y1, use_comp=True)
# по эрам для главных
for a, b in [("2015-01-01", "2021-12-31"), ("2022-03-01", "2024-12-31"), ("2025-01-01", "2026-08-31")]:
    rep("baseline", base_exit, base_entry, a=a, b=b); rep("baseline+y1key", base_exit | x_y1, base_entry & ~x_y1, a=a, b=b)
    rep("exit_vol_bit_only(+toxic)", base_exit | vol_d, base_entry & ~vol_d, a=a, b=b)
    rep("exit_vol_bit+y1key", base_exit | vol_d | x_y1, base_entry & ~(vol_d | x_y1), a=a, b=b)
tab = fmt_metrics_table(rows); tab.to_csv(RES / "D2_07_competitors.csv", index=False)
print(tab[["window", "cash", "name", "cagr", "vol", "sharpe", "sharpe_excess", "maxdd", "time_in_mkt", "trades_per_yr", "hit_rate"]].to_string(index=False))

# перекрытие масок: y1key vs vol-бит vs бонд-бит (2015+)
s = idx >= A
print("\nперекрытие (2015+): доля дней y1key=%.2f, vol=%.2f, bond=%.2f; P(vol|y1key)=%.2f, P(bond|y1key)=%.2f, P(y1key|vol)=%.2f; y1key & ~vol & ~bond = %.2f дней" %
      (x_y1[s].mean(), vol_d[s].mean(), bond_d[s].mean(), vol_d[s & x_y1].mean(), bond_d[s & x_y1].mean(), x_y1[s & vol_d].mean(), (x_y1 & ~vol_d & ~bond_d)[s].mean()))

# (4) история срабатываний на месячных срезах 2024–2026
h = pd.DataFrame({"y1_key": d["y1_key"].iloc[me], "y1_key_d21": d["y1_key_d21"].iloc[me], "real_saar_d63": d["real_saar_d63"].iloc[me],
                  "cell": d["cell"].iloc[me], "composite": m["composite"].values, "fwd1m_tr_pct": (np.exp(m["fwd1m_tr"]) - 1) * 100})
h["y1key_exit"] = h["y1_key_d21"] > 0.25; h["rs_exit"] = h["real_saar_d63"] > 1.0
h["baseline_pos"] = baseline_position(m).values
print("\nсрезы 2024-01…2026-09:")
print(h.loc["2024-01-01":].round(2).to_string())
