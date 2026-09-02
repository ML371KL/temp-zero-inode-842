# -*- coding: utf-8 -*-
"""Задача 4: именованные эпизоды (2008, 2011, 2014-12, 2020-03, 2022-02, 2024-05…12, 2026-03…07) —
где именно текущие ворота опоздали, какая альтернатива закрыла бы раньше, и что это стоит в ложных
срабатываниях в спокойные годы. Выход: results/B_timeliness_named.csv, results/B_timeliness_calm_years.csv"""
import sys
sys.path.insert(0, "scripts")
import numpy as np
import pandas as pd
import B_timeliness_lib as L

df = L.load()
px = df["imoex"]
B = L.bits(df)
sign = df["comp_sign"]
G = L.build_gates(df, B, "core")
Gl = L.build_gates(df, B, "late")
R = pd.read_csv("results/B_timeliness_alternatives.csv")

# именованные эпизоды: пик -> дно -> следующий пик (из зигзага, 2014-12 — вручную по локальному максимуму)
def loc_ext(a, b, fn):
    s = px.loc[a:b]
    return s.idxmax() if fn == "max" else s.idxmin()

NAMED = [
    ("2008", pd.Timestamp("2008-05-19"), pd.Timestamp("2008-10-24"), pd.Timestamp("2009-06-01")),
    ("2011", pd.Timestamp("2011-04-06"), pd.Timestamp("2011-10-05"), pd.Timestamp("2012-03-14")),
    ("2014-12", loc_ext("2014-11-01", "2014-12-10", "max"), loc_ext("2014-12-10", "2014-12-31", "min"), loc_ext("2015-01-01", "2015-03-31", "max")),
    ("2020-03", pd.Timestamp("2020-01-20"), pd.Timestamp("2020-03-18"), pd.Timestamp("2021-10-20")),
    ("2022-02", pd.Timestamp("2021-10-20"), pd.Timestamp("2022-02-24"), pd.Timestamp("2022-04-04")),
    ("2024-05..12", pd.Timestamp("2024-05-17"), pd.Timestamp("2024-12-17"), pd.Timestamp("2025-02-25")),
    ("2026-03..07", pd.Timestamp("2026-03-09"), pd.Timestamp("2026-07-17"), pd.Timestamp("2026-08-11")),
]
eps = pd.DataFrame([dict(name=n, peak=p, trough=t, next_peak=x, peak_px=px[p], trough_px=px[t], next_px=px[x],
                         dd=px[t] / px[p] - 1, rise=px[x] / px[t] - 1) for n, p, t, x in NAMED])
print(eps[["name", "peak", "trough", "next_peak", "dd", "rise"]].round(3).to_string(index=False))

# набор ворот для разбора: продовые биты, правило, 2из3, ранние триггеры, лучшие альтернативы
two3 = L.gate_from_bits(B["prod_trend"], B["prod_vol"], B["prod_bond"], 2)
rule_off = ~(L.positions_from_gate(B["prod_toxic"], sign) > 0)
SET = {
    "bit_trend": B["prod_trend"], "bit_vol": B["prod_vol"], "bit_bond": B["prod_bond"],
    "cell_toxic": B["prod_toxic"], "sign<=0": ~(sign > 0), "RULE(toxic|sign<=0)": rule_off, "two_of_three": two3,
    "mom21<-8%": B["mom21_lt-8"], "mom21<-10%": B["mom21_lt-10"], "dd252<-15%": B["dd252_lt-15"],
    "vol_jump21/63>1.5": B["vol_jump_21_63"], "rvi>40": B["rvi_gt40"], "rvi>45": B["rvi_gt45"],
    "breadth<0.4": B["breadth_lt40"], "trend_ma50": B["trend_ma50"], "trend_ma100": B["trend_ma100"],
    "bond_dd2": B["bond_dd2"], "bond_mom21": B["bond_mom21"], "vol_w10_p80": B["vol_w10_p80"],
}
# лучшие альтернативы по устойчивости: победители A и B (gs) в семействах grid/asym/addon
S = pd.read_csv("results/B_timeliness_split.csv") if __import__("os").path.exists("results/B_timeliness_split.csv") else None
extra = []
if S is not None:
    for _, s in S[S.criterion == "gs"].iterrows():
        if s.best_gate in G and s.best_gate not in extra:
            extra.append(s.best_gate)
for c in ["cell[trend_ma100|vol_w21_p80|bond_dd4]k3", "cell[trend_ma200|vol_w21_p80|bond_dd3]k3",
          "cell[trend_ma200|vol_w21_p80|bond_dd4]k2", "asym[exit=two3|entry=!toxic&px>MA200]",
          "asym[exit=toxic|entry=px>MA200]", "toxic OR mom21_lt-10", "toxic_confirm5"]:
    if c not in extra:
        extra.append(c)
for c in extra:
    SET[c] = G[c]["gate"]

rows = []
for nm, g in SET.items():
    tl = L.timeliness(g, px, eps)
    tl.insert(0, "gate", nm); tl.insert(1, "name", eps.name.values)
    rows.append(tl)
T = pd.concat(rows, ignore_index=True)
T.to_csv("results/B_timeliness_named.csv", index=False)
pd.set_option("display.width", 250); pd.set_option("display.max_columns", 40)
for n in eps.name:
    e = eps[eps.name == n].iloc[0]
    print(f"\n=== {n}: пик {e.peak.date()} ({e.peak_px:.0f}) -> дно {e.trough.date()} ({e.trough_px:.0f}, {e.dd*100:.0f}%) -> "
          f"след. пик {e.next_peak.date()} (+{e.rise*100:.0f}%) ===")
    t = T[T.name == n][["gate", "exit_lag", "exit_date", "fall_before_exit", "fall_taken_share", "entry_lag", "entry_date",
                        "rise_missed", "rise_missed_share"]]
    print(t.round(2).to_string(index=False))

# ------------------------------------------------------------ спокойные годы: ложные срабатывания
yr = px.loc["2004-01-06":"2026-08-31"]
intra_dd = yr.groupby(yr.index.year).apply(lambda s: float((s / s.cummax() - 1).min()))
calm = [y for y, d in intra_dd.items() if d > -0.15]
print("\nВнутригодовая просадка IMOEX по годам:", {int(k): round(v, 3) for k, v in intra_dd.items()})
print("Спокойные годы (dd внутри года > −15%):", calm)
mask_calm = df.index.year.isin(calm) & (df.index >= "2004-01-06") & (df.index <= "2026-08-31")
rows = []
for nm, g in SET.items():
    gg = g.reindex(df.index).fillna(False).astype(bool)
    gc = gg[mask_calm]
    n_on = int(((gc.astype(int).diff() == 1)).sum())
    off_share = float(gc.mean())
    # упущенная доходность: (r_long − r_flat) в дни риск-офф спокойных лет (позиция с закрытия t-1)
    prev = gg.shift(1).fillna(False)
    fore = float(((df["r_long"] - df["r_flat"])[mask_calm & prev.values]).sum())
    per_year = fore / len(calm)
    rows.append(dict(gate=nm, calm_years=len(calm), n_switch_on_calm=n_on, off_share_calm=off_share,
                     foregone_logret_total=fore, foregone_per_calm_year=per_year))
C = pd.DataFrame(rows)
C.to_csv("results/B_timeliness_calm_years.csv", index=False)
print("\n=== Ложные срабатывания в спокойные годы ===")
print(C.round(3).to_string(index=False))
