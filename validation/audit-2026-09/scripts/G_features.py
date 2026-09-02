"""G_decision, шаг 1: признаки панели на каждый день -> results/G_features.csv + проверки инвариантов."""
import os
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from G_lib import *  # noqa

d, c, m = load_raw()
F = build_features(d, c, m)

# --- инвариант 1: дневной композит на последний торговый день месяца == месячный композит панели
me = F.index.to_series().groupby(F.index.to_period("M")).last().values
chk = pd.DataFrame({"daily": F.loc[me, "comp_daily"].values, "monthly": m["composite"].reindex(me).values},
                   index=me).dropna()
diff = (chk["daily"] - chk["monthly"]).abs()
print(f"инвариант дневной композит vs месячный: n={len(chk)} max|diff|={diff.max():.3e} at {diff.idxmax().date()}")

# --- инвариант 2: знак с гистерезисом на 2026-09-01 и закрытый месяц
last = F.iloc[-1]
print("2026-09-01:", "cell", last.cell, "gate", bool(last.gate), "comp_closed", round(last.comp_closed, 3),
      "closed_date", pd.Timestamp(last.comp_closed_date).date(), "comp_daily", round(last.comp_daily, 3),
      "sign_closed", last.sign_closed)
print("активные сигналы 2026-09-01:",
      {s: (round(last[f"{s}_z"], 2), bool(last[f"{s}_active"])) for s in SIG_IDS if last[f"{s}_active"]})

# --- сводка по признакам
sub = F.loc["2004":]
print("дни 2004+:", len(sub), "gate open доля:", round(sub.gate.mean(), 3), "toxic доля:", round(sub.toxic.mean(), 3),
      "window доля:", round(sub.window.mean(), 3))
print("sign_closed>0 доля:", round((sub.sign_closed > 0).mean(), 3), "смен знака (мес):",
      int((F.sign_closed.resample("ME").last().diff().abs() > 0).sum()))
print("корреляция comp_daily vs comp_closed (дни):", round(sub[["comp_daily", "comp_closed"]].corr().iloc[0, 1], 3))
print("n_active распределение:", sub.n_active.value_counts().sort_index().to_dict())
print("n_against распределение:", sub.n_against.value_counts().sort_index().to_dict())

cols = ["imoex", "mcftr", "mm_rate", "st_trend", "st_vol", "st_bond", "st_rate", "cell", "toxic", "gate", "window",
        "n_ok", "comp_closed", "comp_closed_date", "sign_closed", "comp_daily", "trend_gap", "vol_gap", "bond_gap",
        "n_active", "n_for", "n_against", "vote_sum"] + \
       [f"{s}_{k}" for s in SIG_IDS for k in ("val", "z", "active", "contrib")]
F[cols].to_csv(os.path.join(RES, "G_features.csv"), float_format="%.6g")
print("saved results/G_features.csv", F.shape)
