"""P_package: сборка markdown-таблиц отчёта из CSV (чтобы числа в отчёте не переписывались руками).
Выход: results/P_tables.md. Запуск: python scripts/P_tables.py (из audit/) — после P_run/P_stats/P_extra."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
RES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "results")
MET = pd.read_csv(os.path.join(RES, "P_metrics.csv"))
BOOT = pd.read_csv(os.path.join(RES, "P_bootstrap.csv"))
TS = pd.read_csv(os.path.join(RES, "P_timeliness_summary.csv"))
Y = pd.read_csv(os.path.join(RES, "P_yearly.csv")).set_index("year")
INC = pd.read_csv(os.path.join(RES, "P_yearly_increments.csv")).set_index("year")
TODAY = pd.read_csv(os.path.join(RES, "P_today.csv"))
S24 = pd.read_csv(os.path.join(RES, "P_switches_2024_2026.csv"))
PL = pd.read_csv(os.path.join(RES, "P_placebo.csv"))
WF = pd.read_csv(os.path.join(RES, "P_walkforward.csv"))
ESE = pd.read_csv(os.path.join(RES, "P_es_episodes.csv"))
D2F = pd.read_csv(os.path.join(RES, "P_extra_d2frame.csv"))
DA = pd.read_csv(os.path.join(RES, "P_extra_dayafter.csv"))
WD = pd.read_csv(os.path.join(RES, "P_extra_weekday.csv"))
HY = pd.read_csv(os.path.join(RES, "P_extra_hybrid.csv"))
CF = pd.read_csv(os.path.join(RES, "P_configs.csv"))
DESC = dict(zip(CF.config, CF.description))
out = []
def H(s): out.append("\n" + s + "\n")
def pct(x, d=1): return "—" if pd.isna(x) else f"{x*100:.{d}f} %".replace(".", ",")
def f2(x): return "—" if pd.isna(x) else f"{x:.2f}".replace(".", ",")
def f3(x): return "—" if pd.isna(x) else f"{x:+.3f}".replace(".", ",")
def f1(x): return "—" if pd.isna(x) else f"{x:.1f}".replace(".", ",")
def table(header, rows):
    out.append("| " + " | ".join(header) + " |")
    out.append("|" + "---|" * len(header))
    for r in rows:
        out.append("| " + " | ".join(str(x) for x in r) + " |")
def M(cfg, wn, lag, cost=0.002):
    r = MET[(MET.config == cfg) & (MET.window == wn) & (MET.exec_lag == lag) & (MET.cost == cost)]
    return r.iloc[0] if len(r) else None

LADDER = ["P0", "P1", "P2", "P3", "P3m", "P4", "P5", "P7", "P6a", "P6b", "bh", "mm"]
NAMES = {"P0": "P0 прод (ворота ежедневно, знак закрытого месяца ±0,1)", "P1": "P1 = P0 + композит дневной, решение по пятницам, ±0,2",
         "P2": "P2 = P1 + гистерезис битов", "P3": "P3 = P2 + бит-выход «репрайсинг» (y1−ключ +25 б.п./21 дн), читается ежедневно",
         "P3m": "P3m = P2 + тот же бит, читается на конце месяца и держится месяц", "P4": "P4 = P3 + тренд по MCFTR",
         "P5": "P5 = P4 + вход «стабилизация»", "P7": "P7 = P2 + вето по обороту (dist21>0,10, по пятницам)", "P6a": "P6a = P3 + ступенчатый вход 50 %/21 дн",
         "P6b": "P6b = P3 + трейлинг-стоп 8 %", "bh": "b&h MCFTR", "mm": "100 % денежный рынок"}

# ---------------- T1/T2: лестница main и full
for wn, title in [("main_2010_2026", "T1. Лестница, основное окно 2010-01…2026-08 (200 мес), издержки 0,2 %; в скобках — исполнение t"),
                  ("full_2004_2026", "T2. Лестница, полное окно 2004-01…2026-08 (272 мес), издержки 0,2 %; в скобках — исполнение t")]:
    H(f"### {title}")
    rows = []
    for c in LADDER:
        a = M(c, wn, 1); b = M(c, wn, 0)
        if c in ("bh", "mm"):
            rows.append([NAMES[c], pct(a.cagr), f2(a.sharpe) if c == "bh" else "—", f2(a.sharpe_ex_mm) if c == "bh" else "—", pct(a.mdd), pct(a.time_in_mkt, 0), "0", "—", f2(a.hit_month)])
        else:
            rows.append([NAMES[c], f"{pct(a.cagr)} ({pct(b.cagr)})", f"{f2(a.sharpe)} ({f2(b.sharpe)})", f"{f2(a.sharpe_ex_mm)} ({f2(b.sharpe_ex_mm)})",
                         f"{pct(a.mdd)} ({pct(b.mdd)})", pct(a.time_in_mkt, 0), f1(a.trades_per_year), pct(a.years_beat_bh, 0), f2(a.hit_month)])
    table(["Ступень", "CAGR t+1 (t)", "Шарп t+1 (t)", "Шарп над ММ t+1 (t)", "MDD t+1 (t)", "В рынке", "Сделок/год", "Лет > b&h", "hit мес"], rows)

# ---------------- T3: окна
H("### T3. Шарп / MDD по окнам (исполнение t+1, 0,2 %)")
WN = [("era_2010_2021", "2010–2021"), ("era_2022_2024", "2022-03…2024"), ("era_2025_2026", "2025–2026-08"), ("ex2022_2010_2026", "2010–26 ex-2022"), ("split_A_2004_2017", "A 2004–2017"), ("split_B_2018_2026", "B 2018–2026")]
rows = []
for c in ["P0", "P1", "P2", "P3", "P3m", "P4", "P5", "P7", "P6a", "P6b", "bh"]:
    r = [c]
    for wn, _ in WN:
        a = M(c, wn, 1); r.append(f"{f2(a.sharpe)} / {pct(a.mdd, 0)}")
    rows.append(r)
table(["Ступень"] + [t for _, t in WN], rows)
H("Шарп над ММ по тем же окнам (t+1):")
rows = []
for c in ["P0", "P1", "P2", "P3", "P3m", "P4", "P5", "P7"]:
    rows.append([c] + [f2(M(c, wn, 1).sharpe_ex_mm) for wn, _ in WN])
table(["Ступень"] + [t for _, t in WN], rows)

# ---------------- T4: инкременты (бутстреп)
H("### T4. Инкремент каждой ступени к предыдущей: ΔШарп (p одностор. Δ≤0), стационарный бутстреп блок 12 мес, 2000 повторов")
PAIRS = [("P1", "P0"), ("P2", "P1"), ("P3", "P2"), ("P3m", "P2"), ("P3m", "P3"), ("P4", "P3"), ("S4m_mcftr", "P3m"), ("P5", "P4"), ("P7", "P2"), ("P6a", "P3"), ("P6b", "P3"), ("A_esm", "P0"), ("S3m_onP1", "P1"), ("S3m_W", "P2")]
WB = [("main_2010_2026", 1, "main t+1"), ("main_2010_2026", 0, "main t"), ("full_2004_2026", 1, "full"), ("ex2022_2010_2026", 1, "ex-2022"), ("split_A_2004_2017", 1, "A 2004–17"), ("split_B_2018_2026", 1, "B 2018–26")]
def B(a, b, wn, lag):
    r = BOOT[(BOOT.a == a) & (BOOT.b == b) & (BOOT.window == wn) & (BOOT.exec_lag == lag)]
    return r.iloc[0] if len(r) else None
rows = []
for a, b in PAIRS:
    r = [f"{a} − {b}"]
    for wn, lag, _ in WB:
        x = B(a, b, wn, lag); r.append(f"{f3(x.delta)} ({f2(x.p_le0)})" if x is not None else "—")
    x = B(a, b, "main_2010_2026", 1); r.append(f"{f3(x.delta_ex)} ({f2(x.p_le0_ex)})")
    rows.append(r)
table(["Пара"] + [t for _, _, t in WB] + ["над ММ, main t+1"], rows)
H("### T4b. Ступени и абляции против P0: ΔШарп (p), t+1 и t")
rows = []
for c in ["P1", "P2", "P3", "P3m", "P4", "P5", "P7", "P6a", "P6b", "A_hyst", "A_es", "A_esm", "A_mcftr", "A_stab", "A_veto", "A_veto_m", "L3_noweekly", "L3_nohyst", "S2_vol_only", "S2_B_thr3", "S3_0.35_10_eff", "S3m_W", "S4m_mcftr", "S3m_onP1", "S7_0.2_dec", "S7_onP3", "S1_entry_dec"]:
    r = [c, DESC.get(c, NAMES.get(c, ""))]
    for wn, lag, _ in WB:
        x = B(c, "P0", wn, lag); r.append(f"{f3(x.delta)} ({f2(x.p_le0)})" if x is not None else "—")
    x = B(c, "P0", "main_2010_2026", 1); r.append(f"{f3(x.delta_ex)} ({f2(x.p_le0_ex)})")
    rows.append(r)
table(["Код", "Что это"] + [t for _, _, t in WB] + ["над ММ, main t+1"], rows)

# ---------------- T5: абляции и LOO, метрики
H("### T5. Абляции («одно изменение поверх P0») и leave-one-out от P3, основное окно, t+1 (в скобках t)")
rows = []
for c in ["P0", "A_weekly", "A_hyst", "A_es", "A_esm", "A_mcftr", "A_stab", "A_veto", "A_veto_m", "L3_noweekly", "L3_nohyst", "L3_noes", "P3", "P3m"]:
    a = M(c, "main_2010_2026", 1); b = M(c, "main_2010_2026", 0)
    rows.append([c, DESC.get(c, NAMES.get(c, "")), f"{pct(a.cagr)} ({pct(b.cagr)})", f"{f2(a.sharpe)} ({f2(b.sharpe)})", f2(a.sharpe_ex_mm), f"{pct(a.mdd)}", pct(a.time_in_mkt, 0), f1(a.trades_per_year)])
table(["Код", "Что это", "CAGR", "Шарп", "Шарп над ММ", "MDD", "В рынке", "Сделок/год"], rows)
H("### T5b. P3m (бит репрайсинга на конце месяца): чувствительность порог × окно, ключ, каденция чтения (main t+1 / t; над ММ; MDD; сделок/год; full; B; ex-2022)")
rows = []
for c in ["P2", "P3m"] + [c for c in CF.config if c.startswith("S3m_") or c.startswith("S4m_")]:
    a = M(c, "main_2010_2026", 1); b = M(c, "main_2010_2026", 0); fa = M(c, "full_2004_2026", 1); sb = M(c, "split_B_2018_2026", 1); ex = M(c, "ex2022_2010_2026", 1)
    rows.append([c, DESC.get(c, NAMES.get(c, "")), f"{f2(a.sharpe)} / {f2(b.sharpe)}", f2(a.sharpe_ex_mm), pct(a.mdd), pct(a.cagr), f1(a.trades_per_year), f2(fa.sharpe), f2(sb.sharpe), f2(ex.sharpe)])
table(["Код", "Что это", "Шарп main t+1 / t", "над ММ", "MDD", "CAGR", "Сделок/год", "full", "B 2018–26", "ex-2022"], rows)

# ---------------- T6: чувствительности
H("### T6a. P1: день недели × порог гистерезиса, Шарп (P0 для сравнения; mean5 — среднее по пяти дням; n≥P0 — сколько дней не хуже P0)")
rows = []
for r in WD.itertuples():
    rows.append([r.window, f"t+{r.exec_lag}", f2(r.thr), f2(r.P0), f2(r.MON), f2(r.TUE), f2(r.WED), f2(r.THU), f2(r.FRI), f2(r.mean5), f"{r.min5:.2f}…{r.max5:.2f}".replace(".", ","), int(r.n_ge_P0)])
table(["Окно", "Исп.", "Порог", "P0", "Пн", "Вт", "Ср", "Чт", "Пт", "mean5", "min…max", "n ≥ P0"], rows)
H("### T6b. Прочие чувствительности P1/P2, основное окно t+1 (t): Шарп, Шарп над ММ, MDD, сделок/год")
rows = []
for c in ["P1", "S1_entry_dec", "S1_hyst_daily", "S1_closed_W", "S1_live_D", "P2", "S2_B_thr3", "S2_bond_only", "S2_trend_only", "S2_vol_only"]:
    a = M(c, "main_2010_2026", 1); b = M(c, "main_2010_2026", 0); fa = M(c, "full_2004_2026", 1)
    rows.append([c, DESC.get(c, NAMES.get(c, "")), f"{f2(a.sharpe)} ({f2(b.sharpe)})", f2(a.sharpe_ex_mm), pct(a.mdd), f1(a.trades_per_year), f2(fa.sharpe)])
table(["Код", "Что это", "Шарп main", "над ММ", "MDD", "Сделок/год", "Шарп full"], rows)
H("### T6c. P3: порог × окно × дата ключа (main t+1 / t; над ММ; MDD; сделок/год; Шарп full t+1)")
rows = []
for c in [c for c in CF.config if c.startswith("S3_") or c == "P3"]:
    a = M(c, "main_2010_2026", 1); b = M(c, "main_2010_2026", 0); fa = M(c, "full_2004_2026", 1); sb = M(c, "split_B_2018_2026", 1)
    rows.append([c, DESC.get(c, NAMES.get(c, "")), f"{f2(a.sharpe)} / {f2(b.sharpe)}", f2(a.sharpe_ex_mm), pct(a.mdd), f1(a.trades_per_year), f2(fa.sharpe), f2(sb.sharpe)])
table(["Код", "Что это", "Шарп main t+1 / t", "над ММ", "MDD", "Сделок/год", "Шарп full", "Шарп B"], rows)
H("### T6d. P4/P5/P7: чувствительности (main t+1 / t; над ММ; MDD; сделок/год; full; B)")
rows = []
for c in ["P4", "S4_plain", "P5", "S5_nocomp", "S5_dd20", "S5_onP3", "P7"] + [c for c in CF.config if c.startswith("S7_")]:
    a = M(c, "main_2010_2026", 1); b = M(c, "main_2010_2026", 0); fa = M(c, "full_2004_2026", 1); sb = M(c, "split_B_2018_2026", 1)
    rows.append([c, DESC.get(c, NAMES.get(c, "")), f"{f2(a.sharpe)} / {f2(b.sharpe)}", f2(a.sharpe_ex_mm), pct(a.mdd), f1(a.trades_per_year), f2(fa.sharpe), f2(sb.sharpe)])
table(["Код", "Что это", "Шарп main t+1 / t", "над ММ", "MDD", "Сделок/год", "Шарп full", "Шарп B"], rows)
H("### T6e. Издержки 0,1 / 0,2 / 0,3 %: Шарп main, t+1 (t)")
rows = []
for c in ["P0", "P1", "P2", "P3", "P4", "P5", "P7", "P6a", "P6b"]:
    r = [c]
    for cost in (0.001, 0.002, 0.003):
        a = M(c, "main_2010_2026", 1, cost); b = M(c, "main_2010_2026", 0, cost); r.append(f"{f2(a.sharpe)} ({f2(b.sharpe)})")
    rows.append(r)
table(["Ступень", "0,1 %", "0,2 %", "0,3 %"], rows)

# ---------------- T7: своевременность
H("### T7. Своевременность (правило 7): 26 просадок IMOEX ≥15 % (зигзаг) с 2004, исполнение t+1")
rows = []
for c in ["P0", "P1", "P2", "P3", "P3m", "P4", "P5", "P7", "P6a", "P6b", "A_hyst", "A_es", "A_esm", "A_stab", "A_veto_m", "L3_noweekly", "L3_nohyst"]:
    r = TS[TS.config == c].iloc[0]
    rows.append([c, f"{int(r.n_long_at_peak)}/26", f"{int(r.n_exited_before_trough)}", f"{f1(r.exit_lag_mean)} / {f1(r.exit_lag_median)}", f"{pct(r.avoided_mean, 0)} / {pct(r.avoided_median, 0)}",
                 f"{int(r.n_flat_at_trough)}/26", f"{f1(r.entry_lag_mean)} / {f1(r.entry_lag_median)}", f"{pct(r.missed63_mean, 0)} / {pct(r.missed63_median, 0)}", pct(r.missed_to_next_peak_mean, 0)])
table(["Ступень", "В лонге на пике", "Вышло до дна", "Лаг выхода, дн (ср/мед)", "Избегнуто (ср/мед)", "Во флэте на дне", "Лаг входа, дн (ср/мед)", "Пропущено 63 дн (ср/мед)", "Пропущено до след. пика"], rows)

# ---------------- T8: годовые
H("### T8. Годовые доходности (t+1, 0,2 %), %")
cols = ["P0", "P1", "P2", "P3", "P3m", "P4", "P5", "P7", "bh", "mm"]
rows = [[int(y)] + [f"{Y.loc[y, c]*100:.1f}".replace(".", ",") for c in cols] for y in Y.index]
table(["Год"] + cols, rows)
H("### T8b. Годовые вклады ступеней (п.п. простой доходности) и их сумма без 2020 / без 2022")
cols = ["P1-P0", "P2-P1", "P3-P2", "P3m-P2", "P4-P3", "P5-P4", "P7-P2", "P6a-P3", "P6b-P3"]
rows = [[int(y)] + [f"{INC.loc[y, c]*100:+.1f}".replace(".", ",") for c in cols] for y in INC.index]
s = INC * 100
rows.append(["**сумма**"] + [f"{s[c].sum():+.1f}".replace(".", ",") for c in cols])
rows.append(["без 2020"] + [f"{s[c].drop(2020).sum():+.1f}".replace(".", ",") for c in cols])
rows.append(["без 2022"] + [f"{s[c].drop(2022).sum():+.1f}".replace(".", ",") for c in cols])
rows.append(["без 2020 и 2022"] + [f"{s[c].drop([2020, 2022]).sum():+.1f}".replace(".", ",") for c in cols])
rows.append(["лет >0 / <0"] + [f"{int((s[c] > 0.05).sum())} / {int((s[c] < -0.05).sum())}" for c in cols])
table(["Год"] + cols, rows)

# ---------------- T9: сегодня
H("### T9. «Что бы сказал каждый Pk» на закрытии 01.09.2026")
rows = [[r.config, r.position_2026_09_01, r.last_change, r.last_exec, r.last_reason, int(r.switches_2024_2026), int(r.days_in_state)] for r in TODAY.itertuples()]
table(["Ступень", "Позиция", "Последняя смена (сигнал)", "Исполнение", "Причина", "Переключений 2024-01…2026-09", "Дней в состоянии"], rows)
for c in ["P0", "P2", "P3", "P3m"]:
    H(f"Переключения {c}, 2024-01…2026-09 (сигнал → исполнение t+1):")
    rows = [[r.signal_date, r.exec_date, r.to, r.reason_ru] for r in S24[S24.config == c].itertuples()]
    table(["Сигнал", "Исполнение", "Позиция", "Причина"], rows)

# ---------------- T10: плацебо
H("### T10. Плацебо: случайные биты той же доли и длин серий (500 повторов), реальное значение против распределения плацебо")
rows = []
for r in PL.itertuples():
    rows.append([r.name, f"{f2(r.real_sh_main_t1)} — {f2(r.placebo_mean_sh_main_t1)} / {f2(r.placebo_p95_sh_main_t1)} — p={f2(r.p_ge_real_sh_main_t1)}",
                 f"{f2(r.real_sh_main_t0)} — {f2(r.placebo_mean_sh_main_t0)} / {f2(r.placebo_p95_sh_main_t0)} — p={f2(r.p_ge_real_sh_main_t0)}",
                 f"{f2(r.real_shex_main_t1)} — {f2(r.placebo_mean_shex_main_t1)} — p={f2(r.p_ge_real_shex_main_t1)}",
                 f"{f2(r.real_sh_full_t1)} — {f2(r.placebo_mean_sh_full_t1)} — p={f2(r.p_ge_real_sh_full_t1)}",
                 f"{pct(r.real_mdd_main_t1)} — {pct(r.placebo_mean_mdd_main_t1)} / {pct(r.placebo_p05_mdd_main_t1)} — доля плацебо не лучше: {f2(r.p_le_real_mdd_main_t1)}"])
table(["Ступень", "Шарп main t+1: реальный — плацебо mean / p95 — p(плацебо ≥ реал.)", "Шарп main t", "над ММ main t+1", "Шарп full t+1", "MDD main t+1: реальный — плацебо mean / p05"], rows)

# ---------------- T11: walk-forward
H("### T11. Walk-forward порогов (расширяющееся окно с 2004, выбор по Шарпу прошлого, применение в следующем году), t+1")
rows = []
for r in WF[WF.exec_lag == 1].itertuples():
    base_sh = [getattr(r, k) for k in r._fields if k.startswith("sharpe_") and k not in ("sharpe_ex_mm",) and not pd.isna(getattr(r, k))]
    rows.append([r.strategy, r.window, f2(r.sharpe), f2(r.sharpe_ex_mm), pct(r.mdd), pct(r.cagr), f1(r.trades_per_year), f"{f3(r.dsharpe_vs_base)} ({f2(r.p_le0)})" if not pd.isna(r.dsharpe_vs_base) else "—"])
table(["Стратегия", "Окно", "Шарп", "над ММ", "MDD", "CAGR", "Сделок/год", "ΔШарп к базе (p)"], rows)

# ---------------- T12: ES-эпизоды
H("### T12. Флэт-отрезки, созданные битом репрайсинга (P3 против P2): что было избегнуто")
g = ESE.groupby("year").agg(n=("days", "size"), days=("days", "sum"), avoided_pp=("avoided", lambda s: s.sum() * 100), hit=("avoided", lambda s: (s > 0).mean()))
rows = [[int(y), int(r.n), int(r.days), f"{r.avoided_pp:+.1f}".replace(".", ","), f2(r.hit)] for y, r in g.iterrows()]
rows.append(["**итого**", int(ESE.shape[0]), int(ESE.days.sum()), f"{ESE.avoided.sum()*100:+.1f}".replace(".", ","), f2((ESE.avoided > 0).mean())])
table(["Год", "Отрезков", "Дней", "Избегнуто, п.п. лог (+ = польза)", "Доля полезных"], rows)
ESEm = pd.read_csv(os.path.join(RES, "P_es_episodes_m.csv"))
H("### T12b. То же для P3m (бит читается на конце месяца) против P2")
g = ESEm.groupby("year").agg(n=("days", "size"), days=("days", "sum"), avoided_pp=("avoided", lambda s: s.sum() * 100), hit=("avoided", lambda s: (s > 0).mean()))
rows = [[int(y), int(r.n), int(r.days), f"{r.avoided_pp:+.1f}".replace(".", ","), f2(r.hit)] for y, r in g.iterrows()]
rows.append(["**итого**", int(ESEm.shape[0]), int(ESEm.days.sum()), f"{ESEm.avoided.sum()*100:+.1f}".replace(".", ","), f2((ESEm.avoided > 0).mean())])
table(["Год", "Отрезков", "Дней", "Избегнуто, п.п. лог (+ = польза)", "Доля полезных"], rows)
H("Крупнейшие отрезки P3m по |избегнуто|:")
top = ESEm.reindex(ESEm.avoided.abs().sort_values(ascending=False).index).head(12)
table(["Начало", "Конец", "Дней", "MCFTR за отрезок", "Кэш", "Избегнуто, п.п."], [[r.start, r.end, int(r.days), pct(np.exp(r.mcftr_logret) - 1), pct(np.exp(r.cash_logret) - 1), f"{r.avoided*100:+.1f}".replace(".", ",")] for r in top.itertuples()])
DOM = pd.read_csv(os.path.join(RES, "P_extra_dom_placebo.csv"))
H("### T13d. Плацебо «день месяца»: бит репрайсинга читается в k-й торговый день месяца и держится месяц (Шарп t+1; last = последний день = P3m / A_esm)")
for base in ["P2", "P0"]:
    x = DOM[DOM.base == base]
    H(f"База {base}:")
    rows = [[r.k, f2(r.sh_main_t1), f2(r.sh_main_t0), f2(r.shex_main), pct(r.mdd_main_t1), f2(r.sh_rate_t1), pct(r.mdd_rate_t1)] for r in x.itertuples()]
    table(["k", "Шарп main t+1", "Шарп main t", "над ММ main", "MDD main", "Шарп 2015+ t+1", "MDD 2015+"], rows)

# ---------------- T13: рамка D2, день после, гибрид
H("### T13a. Сверка с рамкой D2: месячный эталон и ES, окно 2015–2026 (Шарп t+1 / t; над ММ t+1; MDD t+1; сделок/год)")
rows = []
for nm in D2F.strategy.unique():
    a = D2F[(D2F.strategy == nm) & (D2F.window == "rates_2015_2026") & (D2F.exec_lag == 1)].iloc[0]; b = D2F[(D2F.strategy == nm) & (D2F.window == "rates_2015_2026") & (D2F.exec_lag == 0)].iloc[0]
    m = D2F[(D2F.strategy == nm) & (D2F.window == "main_2010_2026") & (D2F.exec_lag == 1)]
    rows.append([nm, f"{f2(a.sharpe)} / {f2(b.sharpe)}", f2(a.sharpe_ex_mm), pct(a.mdd), f1(a.trades_per_year), f"{f2(m.iloc[0].sharpe)} / {pct(m.iloc[0].mdd)}" if len(m) else "—"])
table(["Стратегия", "Шарп 2015+ t+1 / t", "над ММ", "MDD", "Сделок/год", "main: Шарп / MDD (t+1)"], rows)
H("### T13b. Доходность MCFTR после сигнала ВЫХОДА по причинам: день t+1 (её получает исполнение t и теряет t+1) и день t+2")
rows = [[r.config, r.reason, int(r.n), f"{r.ret_t1_mean:+.2f} %".replace(".", ","), f2(r.ret_t1_hit_neg), f"{r.ret_t2_mean:+.2f} %".replace(".", ","), f"{r.ret_t2_t5_mean:+.2f} %".replace(".", ",")] for r in DA.itertuples()]
table(["Конфигурация", "Причина выхода", "n", "t+1 средн.", "доля t+1 < 0", "t+2 средн.", "t+2…t+5 сумм."], rows)
H("### T13c. Гибридное исполнение P3 (ES-выход на закрытии дня сигнала, остальное — на следующем закрытии)")
rows = [[r.window, f2(r.hybrid_sharpe), f2(r.hybrid_shex), pct(r.hybrid_mdd), pct(r.hybrid_cagr), f"{f2(r.P2_t1_sharpe)} / {pct(r.P2_t1_mdd)}", f"{f2(r.P3_t1_sharpe)} / {pct(r.P3_t1_mdd)}"] for r in HY.itertuples()]
table(["Окно", "Гибрид Шарп", "над ММ", "MDD", "CAGR", "P2 (t+1): Шарп / MDD", "P3 (t+1): Шарп / MDD"], rows)

open(os.path.join(RES, "P_tables.md"), "w", encoding="utf-8").write("\n".join(out))
print("готово: results/P_tables.md,", len(out), "строк")
