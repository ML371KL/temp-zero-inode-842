"""D2_03: инкремент ставочных ворот/фильтров к правилу панели.
Позиция строится ДНЕВНО как машина выхода/входа: long→flat при E(t)&¬N(t) или при знаке композита
ПОСЛЕДНЕГО ЗАКРЫТОГО месяца ≤0; flat→long при N(t) & композит>0. Две каденции: месячная (позиция
фиксируется на последний торговый день месяца, как в панели) и дневная.
Условие с NaN (нет данных) = поведение эталона. Сравнение с эталоном на том же окне: метрики брифа,
бутстреп разности Шарпов, плацебо (циркулярный сдвиг маски и случайные блочные маски), своевременность."""
import numpy as np
import pandas as pd
from D2_lib import *

d = derive_rates(load_daily())
m = monthly_frame(d)
idx = d.index
me = month_ends(idx)
me_dates = idx[me]
is_me = np.zeros(len(idx), dtype=bool); is_me[me] = True

comp_ok_d = monthly_to_daily_pos((m["comp_sign"] > 0).astype(float), idx)
toxic_d = d["toxic"].fillna(0.0)
bond_d = d["st_bond"].fillna(0.0)
trend_d = d["st_trend"].fillna(0.0)
vol_d = d["st_vol"].fillna(0.0)
r_long_d = np.log(d["mcftr_ffill"]).diff()
r_cash_d = d["mm_rate"] / 100 / 252
base_exit = toxic_d == 1
base_entry = toxic_d == 0


def build_pos(exit_cond, entry_cond, cadence="monthly"):
    ex = exit_cond.reindex(idx).fillna(False).values.astype(bool)
    en = entry_cond.reindex(idx).fillna(False).values.astype(bool)
    co = comp_ok_d.values > 0
    pos = np.zeros(len(idx)); cur = 0.0
    for i in range(len(idx)):
        if cadence == "daily" or is_me[i]:
            if cur == 1.0 and ((ex[i] and not en[i]) or not co[i]):
                cur = 0.0
            elif cur == 0.0 and en[i] and co[i]:
                cur = 1.0
        pos[i] = cur
    return pd.Series(pos, index=idx)


def bt_daily(pos, start, end, cost=COST):
    df = pd.DataFrame({"pos": pos.shift(1), "r_long": r_long_d, "r_cash": r_cash_d}).dropna()
    df = df[(df.index >= start) & (df.index <= end)]
    df["trade"] = df["pos"].diff().abs().fillna(0.0)
    df["ret"] = df["pos"] * df["r_long"] + (1 - df["pos"]) * df["r_cash"] - df["trade"] * cost
    return df


def bt_monthly(pos, start, end, cost=COST):
    pm = pos.iloc[me]; pm.index = me_dates
    return run_monthly(pm, m.reindex(me_dates), cost=cost, start=start, end=end)


VARIANTS = {}
def X(col, op, thr):
    s = d[col]
    return {">": s > thr, "<": s < thr}[op]

def add_exit(fam, name, col, op, thr, note):
    """доп. ВЫХОД по условию (NaN → нет условия)."""
    x = X(col, op, thr)
    VARIANTS[name] = dict(fam=fam, exit=base_exit | x, entry=base_entry & ~x, note=note, col=col)

def add_entry(fam, name, col, op, thr, note):
    """вход ТОЛЬКО при условии (NaN → как эталон); выход как в эталоне."""
    y = X(col, op, thr) | d[col].isna()
    VARIANTS[name] = dict(fam=fam, exit=base_exit, entry=base_entry & y, note=note, col=col)

def add_reentry(fam, name, col, op, thr, note):
    """ранний вход: условие ПЕРЕОПРЕДЕЛЯЕТ токсичную ячейку (и удерживает лонг, пока оно верно)."""
    x = X(col, op, thr)
    VARIANTS[name] = dict(fam=fam, exit=base_exit, entry=base_entry | x, note=note, col=col)

VARIANTS["baseline"] = dict(fam="0", exit=base_exit, entry=base_entry, note="правило панели", col="toxic")
# ---- D: RGBI / бонд-бит
for thr in (-0.02, -0.03, -0.05, -0.06):
    bd = d["rgbi_dd"] < thr
    tox = (trend_d == 0) & (vol_d == 1) & bd
    VARIANTS[f"toxic_bondthr{int(thr*100)}"] = dict(fam="D_bondthr", exit=tox, entry=~tox, note=f"порог бонд-бита {thr}", col="rgbi_dd")
VARIANTS["gate_bond_only"] = dict(fam="D_bondonly", exit=bond_d == 1, entry=bond_d == 0, note="ворота = только бонд-бит (без тренда/волы)", col="rgbi_dd")
VARIANTS["gate_bond_or_toxic"] = dict(fam="D_bondonly", exit=(bond_d == 1) | base_exit, entry=(bond_d == 0) & base_entry, note="выход при бонд-стрессе ИЛИ токсичной", col="rgbi_dd")
add_reentry("D_reentry", "reentry_bond_clear", "st_bond", "<", 0.5, "ранний вход: RGBI-бит очистился (тренд/вола игнор)")
add_reentry("D_reentry", "reentry_rgbi_mom21_pos", "rgbi_mom21", ">", 0.0, "ранний вход: RGBI +за 21д")
add_reentry("D_reentry", "reentry_rgbi_mom21_gt1", "rgbi_mom21", ">", 0.01, "ранний вход: RGBI +1% за 21д")
add_reentry("D_reentry", "reentry_rgbi_mom63_pos", "rgbi_mom63", ">", 0.0, "ранний вход: RGBI +за 63д")
add_reentry("D_reentry", "reentry_rgbi_above_ma50", "rgbi_vs_ma50", ">", 0.0, "ранний вход: RGBI>MA50")
add_reentry("D_reentry", "reentry_rgbi_above_ma100", "rgbi_vs_ma100", ">", 0.0, "ранний вход: RGBI>MA100")
add_reentry("D_reentry", "reentry_rgbi_dd126_clear", "rgbi_dd126", ">", -0.02, "ранний вход: RGBI в −2% от 126д-макс")
add_exit("D_bondonly", "exit_rgbi_below_ma200", "rgbi_vs_ma200", "<", 0.0, "доп. выход RGBI<MA200")
add_exit("D_bondonly", "exit_rgbi_mom63_lt_m3", "rgbi_mom63", "<", -0.03, "доп. выход RGBI −3% за 63д")
for thr in (1.0, 2.0, 3.0):
    add_exit("D_credit", f"exit_hy_d21_gt{thr}", "hy_spread_d21", ">", thr, f"доп. выход: ВДО-спред +{thr} п.п. за 21д")
add_exit("D_credit", "exit_ig_d21_gt0.5", "ig_spread_d21", ">", 0.5, "доп. выход: IG-спред +0,5 за 21д")
add_exit("D_credit", "exit_ig_d63_gt1", "ig_spread_d63", ">", 1.0, "доп. выход: IG-спред +1 за 63д")
add_exit("D_credit", "exit_hy_pct252_gt0.9", "hy_spread_pct252", ">", 0.9, "доп. выход: ВДО-спред >90 перц.")
# ---- A: цена ожиданий (чувствительность порогов и горизонта)
for thr in (0.10, 0.15, 0.25, 0.35, 0.50, 0.75, 1.0):
    add_exit("A_exp", f"exit_y1key_d21_gt{thr}", "y1_key_d21", ">", thr, f"доп. выход: y1−key вырос >{thr} за 21д")
for L in (10, 42, 63):
    d[f"y1_key_d{L}"] = d["y1_key"] - d["y1_key"].shift(L)
    add_exit("A_exp", f"exit_y1key_d{L}_gt0.25", f"y1_key_d{L}", ">", 0.25, f"доп. выход: y1−key вырос >0.25 за {L}д")
    add_exit("A_exp", f"exit_y1key_d{L}_gt0.5", f"y1_key_d{L}", ">", 0.5, f"доп. выход: y1−key вырос >0.5 за {L}д")
add_exit("A_exp", "exit_y05key_d21_gt0.25", "y05_key_d21", ">", 0.25, "доп. выход: y0.5−key вырос >0.25 за 21д")
add_exit("A_exp", "exit_y2key_d21_gt0.25", "y2_key_d21", ">", 0.25, "доп. выход: y2−key вырос >0.25 за 21д")
add_exit("A_exp", "exit_d21_y1_gt0.25", "d21_y1", ">", 0.25, "доп. выход: сама y1 выросла >0.25 за 21д (без ключа)")
add_exit("A_exp", "exit_d21_y2_gt0.25", "d21_y2", ">", 0.25, "доп. выход: y2 выросла >0.25 за 21д")
add_exit("A_exp", "exit_rusfar_key_d21_gt0.25", "rusfar_key_d21", ">", 0.25, "доп. выход: RUSFAR−key вырос >0.25 за 21д")
add_exit("A_exp", "exit_y1key_pos", "y1_key", ">", 0.0, "доп. выход: y1 выше ключа")
add_exit("A_exp", "exit_rusfar_key_pos", "rusfar_key", ">", 0.1, "доп. выход: RUSFAR3M > ключ+10бп")
add_entry("A_exp", "entry_y1key_neg", "y1_key", "<", 0.0, "вход только при прайсинге смягчения")
add_reentry("A_exp", "reentry_y1key_d21_lt_m0.5", "y1_key_d21", "<", -0.5, "ранний вход: ожидания смягчились на 50бп за 21д")
# ---- B: кривая
bad = (d["d21_y10"] > 0.5) & (d["d21_slope"] > 0)
VARIANTS["exit_bad_steep"] = dict(fam="B_curve", exit=base_exit | bad, entry=base_entry & ~bad, note="доп. выход: плохое крутизнение (10Y +50бп за 21д при росте наклона)", col="d21_y10")
add_exit("B_curve", "exit_y10_up_50", "d21_y10", ">", 0.5, "доп. выход: 10Y +50бп за 21д")
add_exit("B_curve", "exit_y10_up_100", "d21_y10", ">", 1.0, "доп. выход: 10Y +100бп за 21д")
add_exit("B_curve", "exit_y10key_gt1", "y10_key", ">", 1.0, "доп. выход: 10Y выше ключа на >1пп")
add_exit("B_curve", "exit_y10key_gt2", "y10_key", ">", 2.0, "доп. выход: 10Y выше ключа на >2пп")
add_exit("B_curve", "exit_erp_lt_m8", "erp", "<", -8.0, "доп. выход: дивдоходность ниже 10Y на >8пп")
add_exit("B_curve", "exit_slope_neg", "slope_10_2", "<", 0.0, "доп. выход: инверсия 10−2")
add_exit("B_curve", "exit_slope_10_1_neg", "slope_10_1", "<", 0.0, "доп. выход: инверсия 10−1")
add_exit("B_curve", "exit_slope_5_1_neg", "slope_5_1", "<", 0.0, "доп. выход: инверсия 5−1")
# ---- C: реальная ставка и фаза
add_exit("C_real", "exit_tightening_phase", "phase_dec", ">", 0.5, "флэт в фазе ужесточения")
add_entry("C_real", "entry_easing_only", "phase_dec", "<", -0.5, "вход только в фазе смягчения")
add_exit("C_real", "exit_key_d126_pos", "key_d126", ">", 0.0, "флэт если ключ вырос за 126д")
add_exit("C_real", "exit_key_d63_pos", "key_d63", ">", 0.0, "флэт если ключ вырос за 63д")
for thr in (0.5, 1.0, 1.5, 2.0, 3.0):
    add_exit("C_real", f"exit_real_saar_d63_gt{thr}", "real_saar_d63", ">", thr, f"флэт если реальная (SAAR3) выросла >{thr}пп за 63д")
for L in (42, 126):
    d[f"real_saar_d{L}"] = d["real_saar"] - d["real_saar"].shift(L)
    add_exit("C_real", f"exit_real_saar_d{L}_gt1", f"real_saar_d{L}", ">", 1.0, f"флэт если реальная выросла >1пп за {L}д")
add_exit("C_real", "exit_real_yoy_d63_gt1", "real_yoy_d63", ">", 1.0, "флэт если реальная (yoy) выросла >1пп за 63д")
add_exit("C_real", "exit_real_saar_gt8", "real_saar", ">", 8.0, "флэт если реальная ставка >8пп")
add_exit("C_real", "exit_real_saar_gt10", "real_saar", ">", 10.0, "флэт если реальная ставка >10пп")
add_entry("C_real", "entry_real_saar_falling", "real_saar_d63", "<", 0.0, "вход только когда реальная ставка падает")
# ---- F: депозитная альтернатива
add_exit("F_dep", "exit_switch_lt_m8", "switch_spread", "<", -8.0, "флэт если вклад бьёт дивиденды на >8пп")
add_exit("F_dep", "exit_switch_lt_m10", "switch_spread", "<", -10.0, "флэт если вклад бьёт дивиденды на >10пп")
add_entry("F_dep", "entry_deposit_falling", "deposit_d63", "<", 0.0, "вход только когда ставки вкладов падают")
add_reentry("F_dep", "reentry_deposit_falling", "deposit_d63", "<", -0.5, "ранний вход: вклады −50бп за 63д")
add_exit("F_dep", "exit_deposit_rising", "deposit_d63", ">", 0.5, "флэт если вклады +50бп за 63д")
add_reentry("F_dep", "reentry_switch_d63_pos", "switch_d63", ">", 1.0, "ранний вход: спред переключения +1пп за 63д")

WINDOWS = {
    "main_2010_2026": ("2010-01-01", "2026-08-31"), "full_2004_2026": ("2004-01-01", "2026-08-31"),
    "rates_2015_2026": ("2015-01-01", "2026-08-31"), "era_2010_2021": ("2010-01-01", "2021-12-31"),
    "era_2015_2021": ("2015-01-01", "2021-12-31"), "era_2022_2024": ("2022-03-01", "2024-12-31"),
    "era_2025_2026": ("2025-01-01", "2026-08-31"), "split_2015_2019": ("2015-01-01", "2019-12-31"),
    "split_2020_2026": ("2020-01-01", "2026-08-31"),
}
rows_m, rows_d, tl_rows = [], [], []
price = d["mcftr_ffill"].dropna(); price = price[price.index >= "2004-01-01"]
eps = drawdown_episodes(price, 0.15)
cash_daily = r_cash_d.reindex(price.index).fillna(0)
base_m_ret, base_d_ret = {}, {}
POS = {}
for name, v in VARIANTS.items():
    avail = d[v["col"]].first_valid_index()
    for cad in ("monthly", "daily"):
        pos = build_pos(v["exit"], v["entry"], cad)
        POS[(name, cad)] = pos
        for wn, (a, b) in WINDOWS.items():
            if cad == "monthly":
                df = bt_monthly(pos, a, b); mt = metrics(df, freq=12, name=name)
                if name == "baseline": base_m_ret[wn] = df["ret"]
                else:
                    br = base_m_ret[wn]; common = df.index.intersection(br.index)
                    dsh, p, ci = sharpe_diff_bootstrap(df.loc[common, "ret"], br.loc[common], n_boot=1000)
                    mt["d_sharpe"] = dsh; mt["p_dsharpe"] = p
                    mt["d_maxdd"] = mt["maxdd"] - metrics(pd.DataFrame({"ret": br, "r_long": df["r_long"], "r_cash": df["r_cash"], "pos": df["pos"], "trade": df["trade"]}), freq=12)["maxdd"]
                df_ex = df[df.index.year != 2022]; mt_ex = metrics(df_ex, freq=12)
                mt["sharpe_ex2022"] = mt_ex.get("sharpe"); mt["maxdd_ex2022"] = mt_ex.get("maxdd")
                mt.update(fam=v["fam"], cadence=cad, window=wn, note=v["note"], avail_from=str(avail.date()) if avail is not None else ""); rows_m.append(mt)
            else:
                df = bt_daily(pos, a, b); mt = metrics(df, freq=252, name=name)
                df_ex = df[df.index.year != 2022]; mt_ex = metrics(df_ex, freq=252)
                mt["sharpe_ex2022"] = mt_ex.get("sharpe"); mt["maxdd_ex2022"] = mt_ex.get("maxdd")
                mt.update(fam=v["fam"], cadence=cad, window=wn, note=v["note"], avail_from=str(avail.date()) if avail is not None else ""); rows_d.append(mt)
        tl = timeliness(pos.reindex(price.index).fillna(0), price, cash_daily, eps)
        tl.insert(0, "cadence", cad); tl.insert(0, "variant", name); tl_rows.append(tl)

tm = fmt_metrics_table(rows_m); td = fmt_metrics_table(rows_d)
for t in (tm, td):
    t["sharpe_ex2022"] = t["sharpe_ex2022"].round(2); t["maxdd_ex2022"] = (t["maxdd_ex2022"] * 100).round(1)
tm["d_sharpe"] = tm["d_sharpe"].round(3); tm["p_dsharpe"] = tm["p_dsharpe"].round(3); tm["d_maxdd"] = (tm["d_maxdd"] * 100).round(1)
tm.to_csv(RES / "D2_03_gates_monthly.csv", index=False)
td.to_csv(RES / "D2_03_gates_daily.csv", index=False)
tl_all = pd.concat(tl_rows, ignore_index=True); tl_all.to_csv(RES / "D2_03_timeliness.csv", index=False)

cols = ["fam", "name", "avail_from", "cagr", "sharpe", "sharpe_excess", "maxdd", "time_in_mkt", "trades_per_yr", "d_sharpe", "p_dsharpe", "d_maxdd", "sharpe_ex2022", "maxdd_ex2022"]
for wn in ["rates_2015_2026", "era_2015_2021", "era_2022_2024", "era_2025_2026", "split_2015_2019", "split_2020_2026", "main_2010_2026"]:
    print(f"\n=== МЕСЯЧНАЯ каденция, окно {wn} ===")
    print(tm[tm.window == wn][cols].to_string(index=False))
colsd = ["fam", "name", "cagr", "sharpe", "sharpe_excess", "maxdd", "time_in_mkt", "trades_per_yr", "sharpe_ex2022", "maxdd_ex2022"]
for wn in ["rates_2015_2026", "era_2025_2026", "main_2010_2026"]:
    print(f"\n=== ДНЕВНАЯ каденция, окно {wn} ===")
    print(td[td.window == wn][colsd].to_string(index=False))

# плацебо вынесено в D2_03b_placebo.py

print("\n=== своевременность: главные эпизоды (дневная каденция) ===")
sel = tl_all[(tl_all.cadence == "daily") & (tl_all.peak.astype(str).isin(["2025-02-25", "2021-10-20", "2011-04-06", "2020-01-20", "2017-01-03"]))]
print(sel[["variant", "peak", "depth_pct", "long_at_peak", "days_to_exit", "fall_avoided_pct", "days_to_reentry", "recovery_missed_pct"]].to_string(index=False))
