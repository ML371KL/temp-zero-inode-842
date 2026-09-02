# -*- coding: utf-8 -*-
"""Задача 2: альтернативные конструкции ворот — запаздывание на эпизодах + Шарп/просадка стратегий
«только ворота» (go_) и «ворота + знак закрытого месяца композита» (gs_), число переключений в год,
доля времени в рынке. Выход: results/B_timeliness_alternatives.csv (ядро, с 2004) и
results/B_timeliness_alternatives_late.csv (RVI/ширина, с 2015)."""
import sys, time
sys.path.insert(0, "scripts")
import numpy as np
import pandas as pd
import B_timeliness_lib as L

df = L.load()
px = df["imoex"]
B = L.bits(df)
bh = df["r_long"]
eps15 = pd.read_csv("results/B_timeliness_episodes.csv", parse_dates=["peak", "trough", "next_peak"])
eps25 = L.episodes(px, 0.25)
sign = df["comp_sign"]

WIN_CORE = {"FULL": L.WINDOWS["FULL_2004+"], "MAIN": L.WINDOWS["MAIN_2010+"], "A": L.WINDOWS["A_2004-2017"],
            "B": L.WINDOWS["B_2018-2026"], "E1": L.WINDOWS["2010-2021"], "E2": L.WINDOWS["2022-03..2024"],
            "E3": L.WINDOWS["2025-2026"]}
WIN_LATE = {"L15": ("2015-01-01", "2026-08-31"), "LA": ("2015-01-01", "2020-12-31"), "LB": ("2021-01-01", "2026-08-31")}


def evaluate(G, windows, eps_sets, tag):
    rows = []
    t0 = time.time()
    for i, (name, spec) in enumerate(G.items()):
        g = spec["gate"]
        row = dict(gate=name, family=spec["family"])
        for k in ("trend", "vol", "bond", "k", "comp", "exit", "entry", "n_on", "n_off"):
            if k in spec:
                row[k] = spec[k]
        for wn, (a, b) in windows.items():
            gs = L.gate_stats(g, a, b)
            row[f"off_{wn}"] = gs["off_share"]; row[f"sw_{wn}"] = gs["switches_yr"]
        for en, eps in eps_sets.items():
            s = L.summarize_timeliness(L.timeliness(g, px, eps))
            for k in ("n_caught", "already_off_share", "exit_lag_med", "exit_lag_med_late", "fall_before_exit_med",
                      "fall_before_exit_mean", "fall_taken_mean", "entry_lag_med", "rise_missed_med",
                      "rise_missed_share_mean"):
                row[f"{en}_{k}"] = s[k]
        for sn, pos in (("go", L.positions_from_gate(g)), ("gs", L.positions_from_gate(g, sign))):
            r = L.strat_returns(pos, df)
            for wn, (a, b) in windows.items():
                m = L.metrics(r, df, pos, a, b, bh=bh)
                for k in ("cagr", "sharpe", "sharpe_ex", "maxdd", "in_mkt", "trades_yr", "hit_m"):
                    row[f"{sn}_{wn}_{k}"] = m.get(k, np.nan)
            if "FULL" in windows:
                m = L.metrics(r, df, pos, *windows["FULL"], excl=L.EXCL["ex2022"], bh=bh)
                row[f"{sn}_ex2022_sharpe"] = m.get("sharpe", np.nan); row[f"{sn}_ex2022_maxdd"] = m.get("maxdd", np.nan)
                m = L.metrics(r, df, pos, *windows["FULL"], excl=L.EXCL["ex2008crash"], bh=bh)
                row[f"{sn}_ex2008_sharpe"] = m.get("sharpe", np.nan); row[f"{sn}_ex2008_maxdd"] = m.get("maxdd", np.nan)
        rows.append(row)
        if (i + 1) % 200 == 0:
            print(f"  {tag}: {i+1}/{len(G)} ({time.time()-t0:.0f}s)", flush=True)
    return pd.DataFrame(rows)


# --- ядро (с 2004, без RVI/ширины)
G = L.build_gates(df, B, "core")
print("ворот в ядре:", len(G))
R = evaluate(G, WIN_CORE, {"e15": eps15, "e25": eps25}, "core")
R.to_csv("results/B_timeliness_alternatives.csv", index=False)

# --- поздние (RVI, ширина): с 2015
Gl = L.build_gates(df, B, "late")
print("ворот поздних:", len(Gl))
eps15_late = eps15[eps15.peak >= "2015-01-01"]
Rl = evaluate(Gl, WIN_LATE, {"e15": eps15_late}, "late")
Rl.to_csv("results/B_timeliness_alternatives_late.csv", index=False)

pd.set_option("display.width", 250); pd.set_option("display.max_columns", 40)
base = R[R.gate == "prod_toxic"].iloc[0]
print("\n=== БАЗА prod_toxic ===")
print(base[["e15_n_caught", "e15_exit_lag_med", "e15_fall_before_exit_mean", "e15_fall_taken_mean", "e15_entry_lag_med",
            "e15_rise_missed_share_mean", "sw_FULL", "off_FULL", "gs_FULL_sharpe", "gs_FULL_maxdd", "gs_MAIN_sharpe",
            "gs_MAIN_maxdd", "gs_A_sharpe", "gs_B_sharpe", "go_FULL_sharpe", "go_FULL_maxdd", "go_MAIN_sharpe", "go_MAIN_maxdd"]].round(3))

cols = ["gate", "e15_n_caught", "e15_exit_lag_med", "e15_fall_taken_mean", "e15_entry_lag_med", "e15_rise_missed_share_mean",
        "sw_FULL", "off_FULL", "gs_FULL_sharpe", "gs_FULL_maxdd", "gs_MAIN_sharpe", "gs_MAIN_maxdd", "gs_A_sharpe", "gs_B_sharpe",
        "go_MAIN_sharpe", "go_MAIN_maxdd"]
print("\n=== Одиночные компоненты ===")
print(R[R.family == "single"][cols].round(3).to_string(index=False))
print("\n=== Один параметр за раз от базы (trend x vol_w21_p80 x bond_dd4, k=3) ===")
oat = R[(R.family == "grid_k3") & (((R.vol == "vol_w21_p80") & (R.bond == "bond_dd4")) |
                                    ((R.trend == "trend_ma200") & (R.bond == "bond_dd4")) |
                                    ((R.trend == "trend_ma200") & (R.vol == "vol_w21_p80")))]
print(oat[cols].round(3).to_string(index=False))
print("\n=== k=2 при базовых компонентах, один параметр за раз ===")
oat2 = R[(R.family == "grid_k2") & (((R.vol == "vol_w21_p80") & (R.bond == "bond_dd4")) |
                                     ((R.trend == "trend_ma200") & (R.bond == "bond_dd4")) |
                                     ((R.trend == "trend_ma200") & (R.vol == "vol_w21_p80")))]
print(oat2[cols].round(3).to_string(index=False))
print("\n=== Ранние триггеры как добавка (OR) ===")
print(R[R.family.str.startswith("addon")][cols].round(3).to_string(index=False))
print("\n=== Асимметрия выход/вход ===")
print(R[R.family == "asym"][cols].round(3).to_string(index=False))
print("\n=== Подтверждение N дней ===")
print(R[R.family == "confirm"][cols].round(3).to_string(index=False))
print("\n=== ТОП-15 grid по gs_MAIN_sharpe ===")
print(R[R.family.str.startswith("grid")].sort_values("gs_MAIN_sharpe", ascending=False).head(15)[cols].round(3).to_string(index=False))
print("\n=== ТОП-15 grid по go_MAIN_sharpe (только ворота) ===")
print(R[R.family.str.startswith("grid")].sort_values("go_MAIN_sharpe", ascending=False).head(15)[cols].round(3).to_string(index=False))
print("\n=== ПОЗДНИЕ (RVI/ширина), окно 2015+ ===")
colsl = ["gate", "e15_n_caught", "e15_exit_lag_med", "e15_fall_taken_mean", "e15_entry_lag_med", "e15_rise_missed_share_mean",
         "sw_L15", "off_L15", "gs_L15_sharpe", "gs_L15_maxdd", "gs_LA_sharpe", "gs_LB_sharpe", "go_L15_sharpe", "go_L15_maxdd"]
print(Rl[~Rl.family.str.startswith("grid")][colsl].round(3).to_string(index=False))
print("\n=== ТОП-10 grid_rvi по gs_L15_sharpe ===")
print(Rl[Rl.family.str.startswith("grid")].sort_values("gs_L15_sharpe", ascending=False).head(10)[colsl].round(3).to_string(index=False))
