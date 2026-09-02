"""D2_01: IC ставочных кандидатов на МЕСЯЧНОЙ выборке (срез = последний торговый день месяца,
цель = лог-доходность MCFTR за следующий месяц / 3 месяца). Стационарный бутстреп + Ньюи-Уэст.
Разрезы: окна/эры, ex-2022, split до/после 2020, состояния (токсичная ячейка, тренд, фаза ставки)."""
import numpy as np
import pandas as pd
from D2_lib import *

d = derive_rates(load_daily())
m = monthly_frame(d)

SIGNALS = {
    # блок 1: цена ожиданий
    "y05_key": "1", "y1_key": "1", "y2_key": "1", "rusfar_key": "1",
    "y05_key_d21": "1", "y1_key_d21": "1", "y2_key_d21": "1", "rusfar_key_d21": "1",
    # блок 2: кривая
    "slope_10_2": "2", "slope_10_1": "2", "slope_5_1": "2", "slope_10_5": "2",
    "d21_slope": "2", "d21_y10": "2", "d21_y2": "2", "d21_y1": "2", "steep_good": "2", "steep_bad": "2",
    "y10_key": "2", "y10": "2", "erp": "2",
    # блок 3: реальная ставка
    "real_saar": "3", "real_yoy": "3", "real_saar_d21": "3", "real_saar_d63": "3", "real_saar_dd": "3",
    "real_yoy_d63": "3", "key_d63": "3", "key_d126": "3", "phase_dec": "3", "st_rate": "3", "key_rate": "3",
    # блок 4: кредитный стресс и RGBI
    "hy_spread": "4", "hy_spread_d21": "4", "hy_spread_d63": "4", "hy_spread_pct252": "4",
    "ig_spread": "4", "ig_spread_d21": "4", "ig_spread_d63": "4", "ig_spread_pct252": "4",
    "rgbi_dd": "4", "rgbi_dd126": "4", "rgbi_mom21": "4", "rgbi_mom63": "4",
    "rgbi_vs_ma50": "4", "rgbi_vs_ma100": "4", "rgbi_vs_ma200": "4",
    # блок 6: депозитная альтернатива
    "switch_spread": "6", "switch_d21": "6", "switch_d63": "6", "deposit": "6", "deposit_d21": "6",
    "deposit_d63": "6", "orfr_fiz": "6",
}

WINDOWS = {
    "all_avail": (None, None),
    "2015_2021": ("2015-01-01", "2021-12-31"),
    "2022_2024": ("2022-03-01", "2024-12-31"),
    "2025_2026": ("2025-01-01", "2026-08-31"),
    "pre2020": (None, "2019-12-31"),
    "post2020": ("2020-01-01", "2026-08-31"),
}

rows = []
for sig, blk in SIGNALS.items():
    if sig not in m:
        print("нет", sig); continue
    s = m[sig]
    for tgt, block in [("fwd1m_tr", 6), ("fwd3m_tr", 9)]:
        f = m[tgt]
        for wn, (a, b) in WINDOWS.items():
            mask = pd.Series(True, index=m.index)
            if a: mask &= m.index >= a
            if b: mask &= m.index <= b
            for cond in ["all", "ex2022", "toxic", "nontoxic", "bear", "bull", "easing", "tightening"]:
                cm = mask.copy()
                if cond == "ex2022": cm &= m.index.year != 2022
                elif cond == "toxic": cm &= m["cell"] == TOXIC
                elif cond == "nontoxic": cm &= (m["cell"] != TOXIC) & m["cell"].notna()
                elif cond == "bear": cm &= m["st_trend"] == 0
                elif cond == "bull": cm &= m["st_trend"] == 1
                elif cond == "easing": cm &= m["phase_dec"] == -1
                elif cond == "tightening": cm &= m["phase_dec"] == 1
                if cond != "all" and wn not in ("all_avail", "pre2020", "post2020"):
                    continue
                r = ic_stats(s[cm], f[cm], block=block, n_boot=1000)
                r.update(signal=sig, block_id=blk, target=tgt, window=wn, cond=cond)
                rows.append(r)
res = pd.DataFrame(rows)
res["ic"] = res["ic"].round(3); res["p_boot"] = res["p_boot"].round(3)
res["ci_lo"] = res["ci_lo"].round(3); res["ci_hi"] = res["ci_hi"].round(3); res["nw_t"] = res["nw_t"].round(2)
res = res[["block_id", "signal", "target", "window", "cond", "n", "ic", "ci_lo", "ci_hi", "p_boot", "nw_t"]]
res.to_csv(RES / "D2_01_ic_all.csv", index=False)

# сводка: all_avail/all + эры (fwd1m), с числом тестов для платы за перебор
piv = res[(res.cond == "all") & (res.target == "fwd1m_tr")].pivot_table(
    index=["block_id", "signal"], columns="window", values="ic")
nn = res[(res.cond == "all") & (res.target == "fwd1m_tr") & (res.window == "all_avail")].set_index("signal")["n"]
pp = res[(res.cond == "all") & (res.target == "fwd1m_tr") & (res.window == "all_avail")].set_index("signal")["p_boot"]
piv["n_all"] = nn.reindex(piv.index.get_level_values(1)).values
piv["p_all"] = pp.reindex(piv.index.get_level_values(1)).values
piv = piv[["n_all", "all_avail", "p_all", "2015_2021", "2022_2024", "2025_2026", "pre2020", "post2020"]]
piv.to_csv(RES / "D2_01_ic_summary.csv")
print("=== IC fwd1m (Спирмен, месячная выборка) ===")
print(piv.to_string())

print("\n=== IC fwd1m по состояниям (all_avail) ===")
piv2 = res[(res.window == "all_avail") & (res.target == "fwd1m_tr")].pivot_table(
    index=["block_id", "signal"], columns="cond", values="ic")
piv2 = piv2[["all", "ex2022", "toxic", "nontoxic", "bear", "bull", "easing", "tightening"]]
piv2.to_csv(RES / "D2_01_ic_by_state.csv")
print(piv2.to_string())

print("\n=== выжившие: |IC|>=0.10 и p_boot<0.10 на all_avail, fwd1m, cond=all; и ex2022 того же знака ===")
a = res[(res.window == "all_avail") & (res.target == "fwd1m_tr")]
allc = a[a.cond == "all"].set_index("signal"); ex = a[a.cond == "ex2022"].set_index("signal")
surv = allc[(allc.ic.abs() >= 0.10) & (allc.p_boot < 0.10)].copy()
surv["ic_ex2022"] = ex["ic"].reindex(surv.index); surv["p_ex2022"] = ex["p_boot"].reindex(surv.index)
print(surv[["n", "ic", "p_boot", "nw_t", "ic_ex2022", "p_ex2022"]].to_string())
n_tests = len(SIGNALS)
print(f"\nчисло сигналов в семье: {n_tests}; Бонферрони-порог p<{0.05/n_tests:.4f}; BH-FDR 10% ниже")
from D2_lib import stats as _st
pv = allc["p_boot"].dropna().sort_values()
k = len(pv); q = 0.10
bh = pv[pv.values <= q * (np.arange(1, k + 1) / k)]
print("BH-открытия (10%):", list(bh.index))
