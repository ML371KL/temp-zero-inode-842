"""G_decision, шаг 2: семейство правил позиции R0–R5 с АПРИОРНЫМИ параметрами (без подбора),
метрики по окнам брифа, своевременность (правило 7), бутстреп разности Шарпа с R0.
Выход: results/G_rules_metrics.csv, G_timeliness.csv, G_bootstrap.csv, G_positions.csv
"""
import os
import sys
import json
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from G_lib import *  # noqa

INF = float("inf")


def rule_pos(F, name, p, votes=None):
    """Позиция автомата для правила name с параметрами p. votes — переопределение голосов (плацебо/лаг)."""
    gate = F["gate"].values.astype(bool)
    toxic = F["toxic"].values.astype(bool)
    comp = F["comp_closed"].values
    sign = F["sign_closed"].values
    compd = F["comp_daily"].values
    window = F["window"].values.astype(bool)
    n_ok = F["n_ok"].values
    bond = F["st_bond"].values == 1
    vol = F["st_vol"].values == 1
    if votes is None:
        n_against, vote_avg = F["n_against"].values, F["vote_avg"].values
    else:
        n_against, vote_avg = votes["n_against"], votes["vote_avg"]
    mh = p.get("min_hold", 0)
    with np.errstate(invalid="ignore"):
        if name == "R0":
            buy = gate & (sign > 0)
            sell = ~buy
        elif name == "R1":
            th, h = p["th"], p.get("h", 0.0)
            src = compd if p.get("daily_exit") else comp
            buy = gate & (comp > th + h)
            sell = (~gate) | (src < th - h)
        elif name == "R2sum":
            lam, ti, to = p["lam"], p["t_in"], p["t_out"]
            score = comp + lam * vote_avg
            buy = gate & (score > ti)
            sell = (~gate) | (score < to)
        elif name == "R2veto":
            k = p.get("k", 2)
            buy = gate & (sign > 0) & (n_against < k)
            sell = (~gate) | ~(sign > 0)
            if p.get("exit_on_veto"):
                sell = sell | (n_against >= k)
        elif name == "R3":
            exits = set(p.get("exits", ()))
            k_ok, ti, t_alt, t_exit = p.get("k_ok", 2), p.get("t_in", 0.0), p.get("t_alt", INF), p.get("t_exit", -INF)
            buy = (~toxic) & (((n_ok >= k_ok) & (comp > ti)) | (comp > t_alt))
            sell = toxic | (comp < t_exit)
            if "bond" in exits:
                sell = sell | bond
            if "vol" in exits:
                sell = sell | vol
            if "bear" in exits:
                sell = sell | (F["st_trend"].values == 0)
        elif name == "R4":
            th, h = p.get("th", 0.0), p.get("h", 0.0)
            buy = window | (gate & ~window & (comp > th + h))
            sell = toxic | (~window & (comp < th - h))
        elif name == "R5":
            tf, th_ = p.get("t_full", 0.3), p.get("t_half", -0.3)
            full = gate & (comp > tf) & (n_against < 2)
            half = gate & ~full & ((comp > th_) | ((comp > tf) & (n_against >= 2)))
            target = np.where(full, 1.0, np.where(half, 0.5, 0.0))
            return automaton_frac(target, mh)
        else:
            raise ValueError(name)
    buy = buy & ~sell
    if p.get("monthly"):
        me = month_end_mask(F)
        buy, sell = buy & me, sell & me
    return automaton(buy, sell, mh)


# --- априорный набор (параметры заданы ДО просмотра результатов; сетка подбора — в G_search.py)
APRIORI = [
    ("R0_h0", "R0", {"min_hold": 0}),
    ("R0_h5", "R0", {"min_hold": 5}),
    ("R0_h21", "R0", {"min_hold": 21}),
    ("R0_monthly", "R0", {"monthly": True}),
    ("R1_th-0.3", "R1", {"th": -0.3, "h": 0.1, "min_hold": 21}),
    ("R1_th0", "R1", {"th": 0.0, "h": 0.1, "min_hold": 21}),
    ("R1_th+0.3", "R1", {"th": 0.3, "h": 0.1, "min_hold": 21}),
    ("R1_th0_dailyexit", "R1", {"th": 0.0, "h": 0.1, "min_hold": 21, "daily_exit": True}),
    ("R2sum_lam0.5", "R2sum", {"lam": 0.5, "t_in": 0.1, "t_out": -0.1, "min_hold": 21}),
    ("R2sum_lam1", "R2sum", {"lam": 1.0, "t_in": 0.1, "t_out": -0.1, "min_hold": 21}),
    ("R2veto_k2", "R2veto", {"k": 2, "min_hold": 21}),
    ("R2veto_k2_exit", "R2veto", {"k": 2, "exit_on_veto": True, "min_hold": 21}),
    ("R3_bond", "R3", {"exits": ("bond",), "k_ok": 2, "t_in": 0.0, "t_alt": 0.3, "t_exit": -0.3, "min_hold": 21}),
    ("R3_vol", "R3", {"exits": ("vol",), "k_ok": 2, "t_in": 0.0, "t_alt": 0.3, "t_exit": -0.3, "min_hold": 21}),
    ("R3_bond_vol", "R3", {"exits": ("bond", "vol"), "k_ok": 2, "t_in": 0.0, "t_alt": 0.3, "t_exit": -0.3, "min_hold": 21}),
    ("R3_none", "R3", {"exits": (), "k_ok": 2, "t_in": 0.0, "t_alt": 0.3, "t_exit": -0.3, "min_hold": 21}),
    ("R4_h0", "R4", {"th": 0.0, "h": 0.1, "min_hold": 0}),
    ("R4_h21", "R4", {"th": 0.0, "h": 0.1, "min_hold": 21}),
    ("R5_frac", "R5", {"t_full": 0.3, "t_half": -0.3, "min_hold": 21}),
]


def run_all(F, specs, cost=0.002, lag=1):
    bh, cash = bh_frame(F)
    out = {}
    for label, name, p in specs:
        pos = rule_pos(F, name, p)
        out[label] = backtest(pos, F, cost=cost, lag=lag)
    return out, bh, cash


if __name__ == "__main__":
    d, c, m = load_raw()
    F = build_features(d, c, m)
    F = F.loc["2003-06-01":]  # MCFTR и mm_rate есть с середины 2003; биты ячейки — с 2004-01
    bts, bh, cash = run_all(F, APRIORI)
    bts_all = {"BH_MCFTR": bh, "CASH_100": cash, **bts}

    # --- метрики по окнам
    rows = []
    for label, bt in bts_all.items():
        for wname, (a, b) in WINDOWS_BT.items():
            mt = metrics(bt, bh, cash, a, b)
            if mt:
                rows.append({"rule": label, "window": wname, **mt})
        # ex-2022: выкинуть календарный 2022 из основного окна
        sub = bt[(bt.index >= "2010-01-01") & (bt.index <= "2026-08-31") & (bt.index.year != 2022)]
        mt = metrics(sub, bh, cash, "2010-01-01", "2026-08-31")
        if mt:
            rows.append({"rule": label, "window": "main_ex2022", **mt})
    M = pd.DataFrame(rows)
    M.to_csv(os.path.join(RES, "G_rules_metrics.csv"), index=False, float_format="%.4f")
    cols = ["rule", "cagr", "vol", "sharpe", "sharpe_ex_cash", "maxdd", "time_in_mkt", "switches_per_year",
            "beat_bh_years", "hit_months_vs_bh", "n_months"]
    for w in ("main_2010_2026", "full_2004_2026", "main_ex2022", "era_2010_2021", "era_2022_2024", "era_2025_2026"):
        print(f"\n=== {w} ===")
        print(M[M.window == w][cols].to_string(index=False, float_format=lambda x: f"{x:.3f}"))

    # --- своевременность: эпизоды просадок MCFTR >15% с 2004
    eps = find_episodes(F.loc["2004":, "mcftr"], 0.15)
    print("\nэпизоды:", [(p.date().isoformat(), t.date().isoformat(), r.date().isoformat() if r is not None else None)
                         for p, t, r in eps])
    trows = []
    for label, bt in bts.items():
        for r in timeliness(bt, F, eps):
            trows.append({"rule": label, **r})
    T = pd.DataFrame(trows)
    T.to_csv(os.path.join(RES, "G_timeliness.csv"), index=False)
    for label in ("R0_h21", "R1_th0", "R3_bond", "R4_h21"):
        print(f"\n--- своевременность {label} ---")
        print(T[T.rule == label].drop(columns=["rule"]).to_string(index=False))

    # --- бутстреп разности Шарпа с R0_h21 (месячные пары, стационарный бутстреп, блок 8 мес)
    brows = []
    for wname in ("main_2010_2026", "full_2004_2026", "test_2018_2026"):
        a, b = WINDOWS_BT[wname]
        ref = monthly_returns(bts["R0_h21"].loc[a:b])
        refbh = monthly_returns(bh.loc[a:b])
        for label, bt in bts_all.items():
            mr = monthly_returns(bt.loc[a:b])
            r1 = sharpe_diff_bootstrap(mr, ref)
            r2 = sharpe_diff_bootstrap(mr, refbh)
            brows.append({"rule": label, "window": wname, "vs": "R0_h21", **r1})
            brows.append({"rule": label, "window": wname, "vs": "BH", **r2})
    B = pd.DataFrame(brows)
    B.to_csv(os.path.join(RES, "G_bootstrap.csv"), index=False, float_format="%.4f")
    print("\n=== бутстреп ΔSharpe vs R0_h21, main_2010_2026 ===")
    print(B[(B.window == "main_2010_2026") & (B.vs == "R0_h21")].to_string(index=False, float_format=lambda x: f"{x:.3f}"))
    print("\n=== бутстреп ΔSharpe vs BH, main_2010_2026 ===")
    print(B[(B.window == "main_2010_2026") & (B.vs == "BH")].to_string(index=False, float_format=lambda x: f"{x:.3f}"))

    # --- позиции для сводки
    P = pd.DataFrame({k: v["held"] for k, v in bts.items()}, index=F.index)
    P.to_csv(os.path.join(RES, "G_positions.csv"), float_format="%.1f")
    print("\nsaved results/G_rules_metrics.csv, G_timeliness.csv, G_bootstrap.csv, G_positions.csv")
