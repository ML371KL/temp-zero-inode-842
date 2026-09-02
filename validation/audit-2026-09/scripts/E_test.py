"""E_algopack шаг 4a: IC-батарея признаков ALGOPACK к форвардным доходностям IMOEX (5/21/63 дн):
дневная выборка с Ньюи-Уэст (лаг = горизонт), месячная невырожденная (конец месяца -> след. месяц),
разрезы по битам панели и по половинам истории. Выход: results/E_ic.csv, results/E_ic_states.csv."""
import sys, os
import numpy as np, pandas as pd
from scipy import stats
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from testlib import nw_tstat, spearman_ic

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
D = os.path.join(ROOT, "data"); A = os.path.join(D, "algopack"); R = os.path.join(ROOT, "results")
os.makedirs(R, exist_ok=True)

feat = pd.read_csv(os.path.join(A, "E_features_daily.csv"), parse_dates=["date"]).set_index("date")
panel = pd.read_csv(os.path.join(D, "panel_prod_daily.csv"), parse_dates=["date"]).set_index("date")
panel = panel.reindex(feat.index)
lp = np.log(panel.imoex)
fwd = {h: (lp.shift(-h) - lp) for h in (5, 21, 63)}
toxic = (panel.cell == "bear|stress|stress")

# --- какие признаки тестируем (id, гипотеза знака: + = растёт -> индекс растёт)
SIGNALS = [
    # FUTOI: уровень нетто-доли физлиц (контрариан по панели: sign -1)
    ("fz_MX", -1, "FUTOI MX физлица, z120 нетто/брутто (как в панели)"),
    ("fz_RI", -1, "FUTOI RI физлица, z120"),
    ("fz_idx", -1, "FUTOI среднее z120 RI+MX"),
    ("fz252_idx", -1, "FUTOI среднее z252 RI+MX"),
    ("fz_IMOEXF", -1, "FUTOI IMOEXF физлица z120 (с 2023-11)"),
    ("fshare_d5_MX", -1, "FUTOI MX изменение нетто-доли за 5 дн"),
    ("fshare_d21_idx", -1, "FUTOI RI+MX изменение нетто-доли за 21 дн"),
    ("fz_Si", -1, "FUTOI Si физлица z120 (лонг доллара)"),
    ("fz_CR", -1, "FUTOI CR физлица z120 (лонг юаня)"),
    ("fz_BR", -1, "FUTOI BR физлица z120"),
    ("fhedge", -1, "хедж рубля минус риск: z(Si) - z(RI+MX)"),
    ("fhedge_cr", -1, "хедж: mean z(Si,CR) - z(RI+MX)"),
    ("fhold_g21_idx", -1, "рост числа физлиц с позицией RI+MX за 21 дн"),
    ("fhold_g21_MX", -1, "рост числа физлиц MX за 21 дн"),
    ("fgross_g21_MX", -1, "рост брутто-позиции физлиц MX за 21 дн"),
    ("fretail_z_idx", -1, "доля розницы в ОИ RI+MX, z252"),
    ("fretail_z_MX", -1, "доля розницы в ОИ MX, z252"),
    ("fintra5_idx", -1, "внутридневное изменение нетто физлиц RI+MX, сумма 5 дн"),
    ("fintra5_MX", -1, "внутридневное изменение нетто физлиц MX, сумма 5 дн"),
    ("fnlong_z_idx", -1, "доля лонгистов по числу лиц RI+MX, z120"),
    ("fwhale_z_idx", +1, "кит: ср. лонг/ср. шорт на лицо RI+MX, z120"),
    ("fwhale_z_MX", +1, "кит MX, z120"),
    ("foi_g21_MX", -1, "рост общего ОИ MX за 21 дн"),
    ("foi_g21_idx", -1, "рост общего ОИ RI+MX за 21 дн"),
    ("fyur_g21_MX", -1, "рост брутто-позиции юрлиц (хеджеров) MX за 21 дн"),
    ("fyur_g21_idx", -1, "рост брутто юрлиц RI+MX за 21 дн"),
    ("fz_oi_MX", -1, "нетто физлиц к общему ОИ MX, z120"),
    ("fz_oi_idx", -1, "нетто физлиц к общему ОИ RI+MX, z120"),
    # tradestats
    ("imb5_c3", +1, "дисбаланс агрессивных покупок-продаж, 3 тяжеловеса (SBER,LKOH,GAZP), 5 дн"),
    ("imb21_c3", +1, "дисбаланс покупок-продаж, 3 тяжеловеса (SBER,LKOH,GAZP), 21 дн"),
    ("imb21z_c3", +1, "то же, z252"),
    ("imbew21_c3", +1, "равновзвешенный дисбаланс, 3 тяжеловеса (SBER,LKOH,GAZP), 21 дн"),
    ("imbtr21_c3", +1, "дисбаланс по числу сделок, 3 тяжеловеса (SBER,LKOH,GAZP), 21 дн"),
    ("trades_g_c3", -1, "рост числа сделок 5дн/63дн, тяжеловесы"),
    ("avgtr_c3", +1, "средний размер сделки 5дн/126дн (крупные), тяжеловесы"),
    ("avgtr_z_c3", +1, "средний размер сделки z252, тяжеловесы"),
    ("val_g_c3", -1, "рост оборота 5дн/63дн, тяжеловесы"),
    ("obimb5_c3", +1, "дисбаланс стакана (obstats imbalance_vol), тяжеловесы, 5 дн"),
    ("obimb21_c3", +1, "дисбаланс стакана, тяжеловесы, 21 дн"),
    ("obimb5_sber", +1, "дисбаланс стакана SBER, 5 дн (непрерывно 2020-2026)"),
    ("obimb21_sber", +1, "дисбаланс стакана SBER, 21 дн"),
    ("ntr_z_c3", -1, "число сделок 3 тяжеловеса, 5д z252 (драйвер среднего размера сделки)"),
    ("imb5_all", +1, "дисбаланс покупок-продаж, 46 бумаг, 5 дн (2024+)"),
    ("imb21_all", +1, "дисбаланс покупок-продаж, 46 бумаг, 21 дн (2024+)"),
    ("imbew21_all", +1, "равновзвешенный дисбаланс, 46 бумаг, 21 дн"),
    ("imbtr21_all", +1, "дисбаланс по числу сделок, 46 бумаг, 21 дн"),
    ("trades_g_all", -1, "рост числа сделок 5/63, 46 бумаг"),
    ("avgtr_all", +1, "средний размер сделки 5/126, 46 бумаг"),
    ("obimb5_all", +1, "дисбаланс стакана, 46 бумаг, 5 дн"),
    ("obimb21_all", +1, "дисбаланс стакана, 46 бумаг, 21 дн"),
    # hi2
    ("hi_bs5", +1, "HI2: концентрация агрессивных покупок минус продаж, 5 дн"),
    ("hi_bs21", +1, "HI2: то же, 21 дн"),
    ("hi_lbs21", +1, "HI2: лог-отношение концентраций покупок/продаж, 21 дн"),
    ("hi_nf21", +1, "HI2: концентрация нетто-покупателей минус нетто-продавцов, 21 дн"),
    ("hi_lnf21", +1, "HI2: лог-отношение нетто-концентраций, 21 дн"),
    ("hi_ap21", -1, "HI2: log(агрессивная/пассивная концентрация), 21 дн"),
    ("hi_pbs21", +1, "HI2: концентрация пассивных покупок минус продаж, 21 дн"),
    ("hi_vol_z", -1, "HI2: концентрация оборота (hhi_volume), z252"),
    ("hi_agr_z", -1, "HI2: концентрация агрессивного оборота, z252"),
    ("hi_bs21z", +1, "HI2: bs21 z252"),
    ("hi_nf21z", +1, "HI2: nf21 z252"),
]
SIGNALS = [s for s in SIGNALS if s[0] in feat.columns]


def month_ends(idx):
    s = pd.Series(idx, index=idx)
    return s.groupby([idx.year, idx.month]).last().values


ME = pd.DatetimeIndex(month_ends(feat.index))
rows, rows_st = [], []
for sid, sign, label in SIGNALS:
    s = feat[sid]
    ok = s.notna()
    if ok.sum() < 120:
        continue
    first, last = s[ok].index[0], s[ok].index[-1]
    rec = dict(signal=sid, label=label, hyp_sign=sign, start=first.date(), end=last.date(), n_days=int(ok.sum()))
    for h in (5, 21, 63):
        x, y = s.values, fwd[h].values
        ic, p, n = spearman_ic(x, y)
        _, t, _ = nw_tstat(x, y, h)
        rec[f"ic{h}_d"] = round(ic, 3); rec[f"nw_t{h}"] = round(t, 2); rec[f"n{h}_d"] = n
    # месячная невырожденная: значение на конец месяца -> следующие 21 дн (~месяц)
    sm = s.reindex(ME); fm = fwd[21].reindex(ME)
    ic, p, n = spearman_ic(sm.values, fm.values)
    rec.update(ic21_m=round(ic, 3), p21_m=round(p, 3), n_m=n)
    fm63 = fwd[63].reindex(ME)
    ic, p, n = spearman_ic(sm.values, fm63.values)
    rec.update(ic63_m=round(ic, 3), p63_m=round(p, 3))
    # половины истории (месячная выборка)
    mid = sm.dropna().index[len(sm.dropna()) // 2]
    for tag, mask in (("h1", ME < mid), ("h2", ME >= mid)):
        ic, p, n = spearman_ic(sm[mask].values, fm[mask].values)
        rec[f"ic21_m_{tag}"] = round(ic, 3); rec[f"n_{tag}"] = n
    # без 2022 (месячная)
    m22 = (ME.year != 2022)
    ic, p, n = spearman_ic(sm[m22].values, fm[m22].values)
    rec["ic21_m_ex22"] = round(ic, 3)
    # терцили по дневной выборке fwd21
    df = pd.DataFrame({"s": s, "f": fwd[21]}).dropna()
    q1, q2 = df.s.quantile([1 / 3, 2 / 3])
    rec["terc_hi_lo_21"] = round((df.f[df.s >= q2].mean() - df.f[df.s <= q1].mean()) * 100, 2)
    rows.append(rec)
    # по состояниям панели (дневная выборка, NW t, fwd21)
    for st_name, mask in (("trend1", panel.st_trend == 1), ("trend0", panel.st_trend == 0),
                          ("vol1", panel.st_vol == 1), ("vol0", panel.st_vol == 0),
                          ("bond1", panel.st_bond == 1), ("bond0", panel.st_bond == 0),
                          ("toxic", toxic), ("nontoxic", ~toxic)):
        x = s.where(mask).values; y = fwd[21].values
        ic, p, n = spearman_ic(x, y)
        _, t, _ = nw_tstat(x, y, 21)
        rows_st.append(dict(signal=sid, state=st_name, n=n, ic21=round(ic, 3) if ic == ic else np.nan,
                            nw_t=round(t, 2) if t == t else np.nan))

res = pd.DataFrame(rows)
from testlib import fdr_bh
res["q21_m"] = fdr_bh(res.p21_m.values).round(3)
res["q63_m"] = fdr_bh(res.p63_m.values).round(3)
res.to_csv(os.path.join(R, "E_ic.csv"), index=False)
print("FDR (BH, q<0.10) по месячной выборке fwd21:", list(res[res.q21_m < 0.10].signal),
      "; fwd63:", list(res[res.q63_m < 0.10].signal))
st = pd.DataFrame(rows_st)
st.to_csv(os.path.join(R, "E_ic_states.csv"), index=False)

pd.set_option("display.width", 250); pd.set_option("display.max_columns", 40)
cols = ["signal", "hyp_sign", "start", "n_m", "ic5_d", "nw_t5", "ic21_d", "nw_t21", "ic63_d", "nw_t63",
        "ic21_m", "p21_m", "ic63_m", "ic21_m_h1", "ic21_m_h2", "ic21_m_ex22", "terc_hi_lo_21"]
print(res[cols].to_string(index=False))
print("\nПо состояниям (fwd21, дневная выборка, NW t):")
piv = st.pivot(index="signal", columns="state", values="nw_t")
print(piv.reindex([s[0] for s in SIGNALS if s[0] in piv.index]).to_string())
