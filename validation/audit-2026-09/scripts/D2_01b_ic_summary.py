"""D2_01b: сводки из D2_01_ic_all.csv (окно 2025–2026 = 20 месяцев < 24 — IC не считается)."""
import sys; sys.stdout.reconfigure(encoding="utf-8")
import numpy as np, pandas as pd
from pathlib import Path
RES = Path(__file__).resolve().parents[1] / "results"
res = pd.read_csv(RES / "D2_01_ic_all.csv")
pd.set_option("display.width", 250); pd.set_option("display.max_rows", 200)
a = res[(res.cond == "all") & (res.target == "fwd1m_tr")]
piv = a.pivot_table(index=["block_id", "signal"], columns="window", values="ic")
base = a[a.window == "all_avail"].set_index("signal")
piv["n_all"] = base["n"].reindex(piv.index.get_level_values(1)).values
piv["p_all"] = base["p_boot"].reindex(piv.index.get_level_values(1)).values
piv["nw_t"] = base["nw_t"].reindex(piv.index.get_level_values(1)).values
cols = [c for c in ["n_all", "all_avail", "p_all", "nw_t", "2015_2021", "2022_2024", "pre2020", "post2020"] if c in piv]
piv = piv[cols]; piv.to_csv(RES / "D2_01_ic_summary.csv")
print("=== IC fwd1m (Спирмен, месячная выборка; all_avail = всё доступное окно) ===")
print(piv.round(3).to_string())
a3 = res[(res.cond == "all") & (res.target == "fwd3m_tr") & (res.window == "all_avail")].set_index("signal")[["n", "ic", "p_boot", "nw_t"]]
print("\n=== IC fwd3m, all_avail ==="); print(a3.round(3).to_string())
piv2 = res[(res.window == "all_avail") & (res.target == "fwd1m_tr")].pivot_table(index=["block_id", "signal"], columns="cond", values="ic")
piv2 = piv2[[c for c in ["all", "ex2022", "toxic", "nontoxic", "bear", "bull", "easing", "tightening"] if c in piv2]]
piv2.to_csv(RES / "D2_01_ic_by_state.csv")
print("\n=== IC fwd1m по состояниям (all_avail) ==="); print(piv2.round(3).to_string())
nn = res[(res.window == "all_avail") & (res.target == "fwd1m_tr")].pivot_table(index="signal", columns="cond", values="n")
print("\nn по состояниям (медиана по сигналам):", nn.median().round(0).to_dict())
allc = base; ex = res[(res.window == "all_avail") & (res.target == "fwd1m_tr") & (res.cond == "ex2022")].set_index("signal")
surv = allc[(allc.ic.abs() >= 0.10) & (allc.p_boot < 0.10)].copy()
surv["ic_ex2022"] = ex["ic"].reindex(surv.index); surv["p_ex2022"] = ex["p_boot"].reindex(surv.index)
print("\n=== |IC|>=0.10 и p<0.10 (all_avail, fwd1m) ==="); print(surv[["n", "ic", "p_boot", "nw_t", "ic_ex2022", "p_ex2022"]].round(3).to_string())
pv = allc["p_boot"].dropna().sort_values(); k = len(pv)
bh = pv[pv.values <= 0.10 * (np.arange(1, k + 1) / k)]
print(f"\nсигналов {k}; Бонферрони p<{0.05/k:.4f}; BH-FDR 10% открытия: {list(bh.index)}")
