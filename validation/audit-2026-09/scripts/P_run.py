"""P_package, прогон 1: лестница ступеней P0…P7 + абляции + leave-one-out + чувствительности
на одном движке (P_lib.run_engine / run_cfg). Все конфигурации: исполнение t и t+1, издержки 0,1/0,2/0,3 %,
окна по правилу 4 брифа + split A/B + ex-2022.
Выход: results/P_positions.csv (дневные позиции всех конфигураций), results/P_metrics.csv,
results/P_configs.csv (перечень конфигураций = плата за перебор), results/P_switches_all.csv,
results/P_states_2024_2026.csv, results/P_best.txt.
Запуск: python scripts/P_run.py (из audit/)."""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import pandas as pd
from P_lib import *

t0 = time.time()
mk = Market()
print(f"данные: {mk.idx[0].date()}…{mk.idx[-1].date()}, n={mk.n}; закрытых месяцев {len(mk.Mc)}; "
      f"дневной композит на концах месяцев сверен; dist (оборот) с {mk.dist.first_valid_index().date()}, "
      f"доля дней dist>0,10: {(mk.dist > 0.1).mean():.3f}")

# ------------------------------------------------------------------ конфигурации
BITS_PROD = {}
BITS_HYST = dict(trend="hyst", vol="hyst", bond="hyst")
BITS_HYST_B = dict(trend="hyst", vol="hyst", bond="thr3")
BITS_HYST_TR = dict(trend="mcftr_hyst", vol="hyst", bond="hyst")
ES = dict(thr=0.25, win=21, key="eff")
STAB = dict(dd=-0.15, on=0.0, off=-0.02, need_comp=True)

P0 = dict(comp="closed", comp_thr=0.10, comp_dec="daily", bits=BITS_PROD, gate_entry="any")
P1 = dict(comp="live", comp_thr=0.20, comp_dec="W-FRI", bits=BITS_PROD, gate_entry="any")
P2 = dict(P1, bits=BITS_HYST)
P3 = dict(P2, es=ES, es_reentry="dec")
P4 = dict(P3, bits=BITS_HYST_TR)
P5 = dict(P4, stab=STAB)

CONFIGS = {}
def add(name, group, cfg, desc):
    CONFIGS[name] = (group, cfg, desc)

add("P0", "лестница", P0, "прод: ворота ежедневно (биты прода), знак закрытого месяца, гистерезис ±0,1")
add("P1", "лестница", P1, "P0 + композит: дневное значение, решение по пятницам, гистерезис ±0,2; ворота вход/выход любым днём")
add("P2", "лестница", P2, "P1 + гистерезис битов (bond −4/−3 %, тренд ±2 %, вола p80/p60)")
add("P3", "лестница", P3, "P2 + бит-выход «репрайсинг ожиданий» (y1−ключ +0,25 п.п./21 дн), возврат в день решения")
add("P4", "лестница", P4, "P3 + тренд по MCFTR (MA200 полной доходности, ±2 %)")
add("P5", "лестница", P5, "P4 + вход «стабилизация» в токсичной ячейке (dd252<−15 % и ret21>0, снятие при ret21<−2 %), композит нужен")
add("P6a", "диагностика", dict(P3, overlay="staged"), "P3 + ступенчатый вход 50 % первые 21 день")
add("P6b", "диагностика", dict(P3, overlay="trail"), "P3 + трейлинг-стоп 8 % от максимума MCFTR, пауза 21 день")
# абляции: одно изменение поверх P0
add("A_weekly", "абляция", P1, "P0 + только недельное решение по дневному композиту (= P1)")
add("A_hyst", "абляция", dict(P0, bits=BITS_HYST), "P0 + только гистерезис битов")
add("A_es", "абляция", dict(P0, es=ES, es_reentry="dec"), "P0 + только бит репрайсинга (возврат сразу — решение ежедневное)")
add("A_mcftr", "абляция", dict(P0, bits=dict(trend="mcftr")), "P0 + только тренд по MCFTR (без гистерезиса)")
add("A_stab", "абляция", dict(P0, stab=STAB), "P0 + только стабилизация (оценка ежедневно)")
add("A_veto", "абляция", dict(P0, veto=dict(thr=0.10, lag=0, mode="any")), "P0 + только вето по обороту (ежедневно)")
add("A_veto_m", "абляция", dict(P0, veto=dict(thr=0.10, lag=0, mode="dec_monthly")), "P0 + вето по обороту, читаемое на конце месяца (как у D1)")
# leave-one-out от P3
add("L3_noweekly", "LOO от P3", dict(P0, bits=BITS_HYST, es=ES, es_reentry="dec"), "P3 без недельного композита (закрытый месяц, ежедневно)")
add("L3_nohyst", "LOO от P3", dict(P1, es=ES, es_reentry="dec"), "P3 без гистерезиса битов")
add("L3_noes", "LOO от P3", P2, "P3 без репрайсинга (= P2)")
# чувствительность P1
for wd in ["MON", "TUE", "WED", "THU", "FRI"]:
    for thr in (0.1, 0.2, 0.3):
        add(f"S1_{wd}_{thr}", "чувств. P1", dict(P1, comp_dec=f"W-{wd}", comp_thr=thr), f"P1: день недели {wd}, порог {thr}")
add("S1_entry_dec", "чувств. P1", dict(P1, gate_entry="dec"), "P1: вход по воротам только в день решения")
add("S1_hyst_daily", "чувств. P1", dict(P1, comp_hyst="daily"), "P1: гистерезис бежит по дневному ряду, читается по пятницам")
add("S1_closed_W", "чувств. P1", dict(P1, comp="closed", comp_thr=0.1), "P1: закрытый месяц, но решение по пятницам (M_closed/W)")
add("S1_live_D", "чувств. P1", dict(P1, comp_dec="daily"), "P1: дневное значение, решение ежедневно, ±0,2")
# чувствительность P2
add("S2_B_thr3", "чувств. P2", dict(P1, bits=BITS_HYST_B), "P2 вариант B: RGBI −3 % без гистерезиса (тренд ±2 %, вола p80/p60)")
add("S2_bond_only", "чувств. P2", dict(P1, bits=dict(bond="hyst")), "P1 + гистерезис только bond")
add("S2_trend_only", "чувств. P2", dict(P1, bits=dict(trend="hyst")), "P1 + гистерезис только trend")
add("S2_vol_only", "чувств. P2", dict(P1, bits=dict(vol="hyst")), "P1 + гистерезис только vol")
# чувствительность P3
for thr in (0.10, 0.15, 0.25, 0.35):
    for win in (10, 21):
        for key in ("eff", "dec"):
            if (thr, win, key) == (0.25, 21, "eff"):
                continue
            add(f"S3_{thr}_{win}_{key}", "чувств. P3", dict(P2, es=dict(thr=thr, win=win, key=key), es_reentry="dec"),
                f"P3: порог {thr}, окно {win}, ключ по дате {'вступления' if key == 'eff' else 'решения'}")
add("S3_reentry_any", "чувств. P3", dict(P3, es_reentry="any"), "P3: возврат сразу после снятия бита (не ждать дня решения)")
# P3m: бит репрайсинга читается на КОНЦЕ МЕСЯЦА и держится месяц (каденция D2), поверх P2
P3M = dict(P2, es=ES, es_reentry="any", es_sample="monthly")
add("P3m", "лестница", P3M, "P2 + бит репрайсинга, читаемый на конце месяца и удерживаемый месяц (каденция D2)")
add("A_esm", "абляция", dict(P0, es=ES, es_reentry="any", es_sample="monthly"), "P0 + бит репрайсинга на конце месяца (конструкция D2 поверх дневных ворот)")
for thr in (0.10, 0.15, 0.25, 0.35):
    for win in (10, 21):
        if (thr, win) == (0.25, 21):
            continue
        add(f"S3m_{thr}_{win}", "чувств. P3m", dict(P2, es=dict(thr=thr, win=win, key="eff"), es_reentry="any", es_sample="monthly"), f"P3m: порог {thr}, окно {win}")
add("S3m_key_dec", "чувств. P3m", dict(P2, es=dict(thr=0.25, win=21, key="dec"), es_reentry="any", es_sample="monthly"), "P3m: ключ по дате решения ЦБ")
add("S3m_W", "чувств. P3m", dict(P2, es=ES, es_reentry="any", es_sample="W-FRI"), "P2 + бит репрайсинга по пятницам (держится неделю)")
add("S4m_mcftr", "чувств. P3m", dict(P3M, bits=BITS_HYST_TR), "P3m + тренд по MCFTR")
add("S3m_onP1", "чувств. P3m", dict(P1, es=ES, es_reentry="any", es_sample="monthly"), "P1 + бит репрайсинга на конце месяца (без гистерезиса битов)")
# чувствительность P4/P5
add("S4_plain", "чувств. P4", dict(P3, bits=dict(trend="mcftr", vol="hyst", bond="hyst")), "P4: тренд MCFTR без гистерезиса (> MA200)")
add("S5_nocomp", "чувств. P5", dict(P4, stab=dict(STAB, need_comp=False)), "P5: стабилизация не требует знака композита")
add("S5_dd20", "чувств. P5", dict(P4, stab=dict(STAB, dd=-0.20)), "P5: стабилизация при dd252<−20 %")
add("S5_onP3", "чувств. P5", dict(P3, stab=STAB), "P3 + стабилизация (без MCFTR-тренда)")

# ------------------------------------------------------------------ прогон
bh_pos = pd.Series(1.0, index=mk.idx); mm_pos = pd.Series(0.0, index=mk.idx)
BT = {}
def bts(pos):
    return {(lag, c): backtest(pos, mk, c, lag) for lag in (0, 1) for c in (0.001, 0.002, 0.003)}
BT["bh"] = bts(bh_pos); BT["mm"] = bts(mm_pos)
ENG = {}
def run_and_store(name):
    eng = run_cfg(mk, json.loads(json.dumps(CONFIGS[name][1])))
    ENG[name] = eng; BT[name] = bts(eng["pos"])
    return eng

for nm in ["P0", "P1", "P2", "P3", "P4", "P5", "P6a", "P6b", "P3m"]:
    run_and_store(nm)

# контроль движка: P0 == правило панели из A_baseline (pos_c)
A = pd.read_csv(os.path.join(RES, "A_positions.csv"), parse_dates=["date"]).set_index("date")
diff = (ENG["P0"]["pos"].reindex(A.index).fillna(0).astype(int) != A["pos_c"].astype(int)).sum()
print(f"КОНТРОЛЬ: P0 против A_positions.pos_c — расхождений {diff} из {len(A)}")
assert diff == 0

def m_main(name, lag=1, cost=0.002):
    return metrics(BT[name][(lag, cost)], BT["bh"][(lag, cost)], BT["mm"][(lag, cost)], *WINDOWS["main_2010_2026"])

print("\nлестница (main 2010–2026, t+1, 0,2 %):")
for nm in ["P0", "P1", "P2", "P3", "P3m", "P4", "P5", "P6a", "P6b"]:
    m = m_main(nm); m0 = m_main(nm, 0)
    print(f"  {nm:4s} CAGR {m['cagr']*100:5.1f}%  Sh {m['sharpe']:.2f} (t: {m0['sharpe']:.2f})  ShEx {m['sharpe_ex_mm']:.2f}  "
          f"MDD {m['mdd']*100:6.1f}%  in {m['time_in_mkt']:.2f}  tr/y {m['trades_per_year']:.1f}")

best = max(["P1", "P2", "P3", "P4", "P5"], key=lambda k: m_main(k)["sharpe"])
print(f"\nлучшая ступень по Шарпу main t+1: {best}")
BEST_CFG = CONFIGS[best][1]
add("P7", "лестница", dict(BEST_CFG, veto=dict(thr=0.10, lag=0, mode="dec")), f"{best} + вето по обороту (dist21>0,10) в дни решения")
for thr in (0.05, 0.10, 0.15, 0.20):
    for mode in ("dec", "any"):
        if (thr, mode) == (0.10, "dec"):
            continue
        add(f"S7_{thr}_{mode}", "чувств. P7", dict(BEST_CFG, veto=dict(thr=thr, lag=0, mode=mode)),
            f"P7: порог {thr}, чтение {'в день решения' if mode == 'dec' else 'ежедневно'}")
add("S7_lag5", "чувств. P7", dict(BEST_CFG, veto=dict(thr=0.10, lag=5, mode="dec")), "P7: признак с лагом 5 торговых дней")
add("S7_lag10", "чувств. P7", dict(BEST_CFG, veto=dict(thr=0.10, lag=10, mode="dec")), "P7: признак с лагом 10 торговых дней")
add("S7_onP3", "чувств. P7", dict(P3, veto=dict(thr=0.10, lag=0, mode="dec")), "P3 + вето по обороту (dist21>0,10) в дни решения")
add("S7_onP3_0.2", "чувств. P7", dict(P3, veto=dict(thr=0.20, lag=0, mode="dec")), "P3 + вето по обороту (dist21>0,20)")

for nm in CONFIGS:
    if nm not in ENG:
        run_and_store(nm)
print(f"\nконфигураций всего: {len(CONFIGS)} (прогонов исполнение×издержки: {len(CONFIGS)*6}); время {time.time()-t0:.0f} с")

# ------------------------------------------------------------------ метрики по окнам
rows = []
for nm in list(CONFIGS) + ["bh", "mm"]:
    for (lag, cost), bt in BT[nm].items():
        if nm in ("bh", "mm") and (lag, cost) != (1, 0.002):
            continue
        for wn, (s, e) in WINDOWS.items():
            m = metrics(bt, BT["bh"][(lag, cost)], BT["mm"][(lag, cost)], s, e, ex2022=wn.startswith("ex2022"))
            rows.append(dict(config=nm, group=CONFIGS[nm][0] if nm in CONFIGS else "эталон", window=wn, exec_lag=lag, cost=cost, **m))
MET = pd.DataFrame(rows)
MET.to_csv(os.path.join(RES, "P_metrics.csv"), index=False, float_format="%.5f")

# ------------------------------------------------------------------ позиции, конфигурации, переключения, состояния
POS = pd.DataFrame({nm: ENG[nm]["pos"] for nm in CONFIGS}, index=mk.idx); POS.index.name = "date"
POS.to_csv(os.path.join(RES, "P_positions.csv"), float_format="%.2f")
CF = pd.DataFrame([dict(config=k, group=v[0], description=v[2], cfg=json.dumps(v[1], ensure_ascii=False)) for k, v in CONFIGS.items()])
CF.to_csv(os.path.join(RES, "P_configs.csv"), index=False)
sw = []
for nm in CONFIGS:
    s = switches(ENG[nm], 1); s.insert(0, "config", nm); sw.append(s)
pd.concat(sw, ignore_index=True).to_csv(os.path.join(RES, "P_switches_all.csv"), index=False)
ST = pd.concat({nm: ENG[nm][["pos", "comp_state", "gate", "es", "stab", "trend", "vol", "bond"] + (["veto"] if "veto" in ENG[nm] else [])]
                for nm in ["P0", "P1", "P2", "P3", "P3m", "P4", "P5", "P7"]}, axis=1)
ST.columns = [f"{a}.{b}" for a, b in ST.columns]
ST["comp_closed"] = mk.comp_closed; ST["comp_live"] = mk.comp_live; ST["y1_key_d21"] = (mk.y1 - mk.key_eff) - (mk.y1 - mk.key_eff).shift(21)
ST["dist21"] = mk.dist; ST["imoex"] = mk.imoex; ST["rgbi_dd"] = mk.rgbi_dd; ST["rv21"] = mk.rv; ST["q80"] = mk.q80; ST["q60"] = mk.q60
ST["ratio_ma200_imoex"] = mk.ratio_imoex; ST["ratio_ma200_mcftr"] = mk.ratio_mcftr
ST.loc["2023-12-01":].to_csv(os.path.join(RES, "P_states_2024_2026.csv"), float_format="%.4f")
with open(os.path.join(RES, "P_best.txt"), "w", encoding="utf-8") as f:
    f.write(best)

pd.set_option("display.width", 250)
cols = ["cagr", "sharpe", "sharpe_ex_mm", "mdd", "time_in_mkt", "trades_per_year", "years_beat_bh", "hit_month"]
for wn in ["main_2010_2026", "full_2004_2026"]:
    sub = MET[(MET.window == wn) & (MET.exec_lag == 1) & (MET.cost == 0.002) & (MET.group.isin(["лестница", "диагностика", "абляция", "LOO от P3", "эталон"]))]
    print(f"\n=== {wn}, t+1, 0,2 % ===")
    print(sub.set_index("config")[cols].to_string(float_format=lambda x: f"{x:.3f}"))
print("\nготово: P_positions.csv, P_metrics.csv, P_configs.csv, P_switches_all.csv, P_states_2024_2026.csv, P_best.txt")
