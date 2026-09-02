"""A_baseline: воспроизведение заявлений исследования (месячный шаг) — пункт 6 задания.
Заявления: (1) «оба слоя с 2004: +7,3%/год, Шарп 0,48, просадка −19,9%; слой 1 без ворот 6,4/0,33/−57,9»
(ценовой IMOEX, ноль во флэте, месячный шаг); (2) «полная доходность 2010–2026: +14,1%/год, Шарп 0,97
против 8,7%/0,43» (MCFTR + ключевая во флэте − 0,2%).
Выход: results/A_baseline_repro.csv
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import pandas as pd
from A_baseline_lib import *

D, M, C = load()
me = closed_month_ends(D.index)
Mc = M.loc[M.index.isin(me)].copy()
Mc["fwd"] = np.log(Mc["imoex"].shift(-1) / Mc["imoex"])          # = fwd1m_log
_m = Mc["fwd"].notna() & Mc["fwd1m_log"].notna()
assert np.allclose(Mc.loc[_m, "fwd"], Mc.loc[_m, "fwd1m_log"])
mc = C["mcftr_ffill"].reindex(Mc.index)
Mc["fwd_tr"] = np.log(mc.shift(-1) / mc)
mm = C["mm_rate"].reindex(Mc.index); key = C["key_rate"].reindex(Mc.index)
Mc["mm_fwd"] = mm / 100 / 12                                          # ставка на конце месяца → за следующий месяц
Mc["key_fwd"] = key.fillna(mm) / 100 / 12
Mc["gate"] = Mc["cell"].notna() & (Mc["cell"] != TOXIC)
Mc["sig_plain"] = Mc["composite"] > 0
Mc["sig_hyst"] = hyst_states(Mc["composite"].values) == 1


def stats(r, label):
    r = r.dropna()
    cum = np.exp(r.cumsum()); mdd = (cum / cum.cummax() - 1).min()
    return dict(label=label, n=len(r), ann_log=r.mean() * 12, cagr=np.exp(r.mean() * 12) - 1, sharpe=sharpe_m(r), mdd=mdd)


rows = []
for wname, lo in [("2004+", "2004-01-31"), ("2010+", "2010-01-31")]:
    sub = Mc.loc[lo:].copy()
    sub = sub[sub["fwd"].notna()]
    for signame in ["sig_plain", "sig_hyst"]:
        for gate in [False, True]:
            sig = sub[signame] & (sub["gate"] if gate else True)
            # ценовой, ноль во флэте, без издержек (как исследование)
            r = sub["fwd"].where(sig, 0.0)
            rows.append(dict(window=wname, kind="price_zero_flat", signal=signame, gate=gate, cost=0, **stats(r, "")))
            # полная доходность: MCFTR + ставка во флэте − издержки (лог) за смену позиции
            sw = sig.astype(int).diff().abs().fillna(0)
            for rate, rname in [("key_fwd", "key"), ("mm_fwd", "mm")]:
                r2 = sub["fwd_tr"].where(sig, sub[rate]) + sw * np.log(1 - 0.002)
                rows.append(dict(window=wname, kind=f"total_return_{rname}", signal=signame, gate=gate, cost=0.002, **stats(r2, "")))
    rows.append(dict(window=wname, kind="price_bh", signal="-", gate=False, cost=0, **stats(sub["fwd"], "")))
    rows.append(dict(window=wname, kind="total_return_bh", signal="-", gate=False, cost=0, **stats(sub["fwd_tr"], "")))
    rows.append(dict(window=wname, kind="mm_only", signal="-", gate=False, cost=0, **stats(sub["mm_fwd"], "")))
    rows.append(dict(window=wname, kind="key_only", signal="-", gate=False, cost=0, **stats(sub["key_fwd"], "")))
R = pd.DataFrame(rows).drop(columns=["label"])
R.to_csv(f"{RES}/A_baseline_repro.csv", index=False, float_format="%.4f")
pd.set_option("display.width", 200)
print("=== воспроизведение (месячный шаг, срез = последний торговый день месяца) ===")
print(R.to_string(index=False, float_format=lambda x: f"{x:.3f}"))

# где ядро стояло в 2008 и 2009
print("\n=== композит и ячейка вокруг 2008–2009 ===")
print(Mc.loc["2008-05-31":"2009-12-31", ["composite", "n_used", "cell", "fwd"]].to_string(float_format=lambda x: f"{x:.3f}"))

# помесячно: сколько месяцев решение (c, дневное) отличалось от месячного (e)
Pp = pd.read_csv(f"{RES}/A_positions.csv", parse_dates=["date"]).set_index("date").loc[BT_START:BT_END]
diff_days = (Pp["pos_c"] != Pp["pos_e"])
print(f"\nдней, когда дневное правило (c) ≠ месячному (e): {int(diff_days.sum())} из {len(Pp)} "
      f"({diff_days.mean()*100:.1f}%); 2010+: {int(diff_days.loc['2010':].sum())} из {len(Pp.loc['2010':])}")
# вклад разницы: доходность на днях расхождения
r_ex = Pp["logret_mcftr"].fillna(0) - np.log1p(Pp["ret_mm"].fillna(0))
c_eff = Pp["pos_c"].shift(2).fillna(0); e_eff = Pp["pos_e"].shift(2).fillna(0)
contrib = ((c_eff - e_eff) * r_ex)
print("суммарный лог-вклад дневного наблюдения против месячного (exec t+1), 2004+: %.3f; 2010+: %.3f; по годам:" % (contrib.sum(), contrib.loc["2010":].sum()))
print((contrib.groupby(contrib.index.year).sum() * 100).round(1).to_string())
