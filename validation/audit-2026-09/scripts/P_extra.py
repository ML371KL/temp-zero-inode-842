"""P_package, прогон 3: дополнения к лестнице.
(а) сверка с рамкой D2: месячный эталон (всё на конце месяца) и ES, читаемый на конце месяца / любым днём —
    окно 2015–2026 и основное, исполнение t и t+1;
(б) эффект «дня после сигнала»: доходность MCFTR в день t+1 и t+2 после сигналов выхода по причинам
    (ворота / ES / композит / вето) — почему ES живёт при t и умирает при t+1;
(в) P5: «ложные входы» по стабилизации (вход stab_on → результат до выхода против кэша) по годам 2022/2024/2026;
(г) даты MDD ступеней; (д) средний по дням недели выигрыш P1 (порог 0,1/0,2/0,3) при t и t+1.
Выход: results/P_extra_d2frame.csv, P_extra_dayafter.csv, P_extra_stab_entries.csv, P_extra_mdd_dates.csv,
P_extra_weekday.csv, P_extra_log.txt. Запуск: python scripts/P_extra.py (из audit/)."""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import pandas as pd
from P_lib import *

mk = Market()
CF = pd.read_csv(os.path.join(RES, "P_configs.csv")); CFG = {r.config: json.loads(r.cfg) for r in CF.itertuples()}
POS = pd.read_csv(os.path.join(RES, "P_positions.csv"), parse_dates=["date"]).set_index("date")
MET = pd.read_csv(os.path.join(RES, "P_metrics.csv"))
SW = pd.read_csv(os.path.join(RES, "P_switches_all.csv"))
log = open(os.path.join(RES, "P_extra_log.txt"), "w", encoding="utf-8")
def P(*a):
    s = " ".join(str(x) for x in a); print(s); log.write(s + "\n")
W2 = dict(WINDOWS); W2["rates_2015_2026"] = ("2015-01-01", "2026-08-31")
BH = {lag: backtest(pd.Series(1.0, index=mk.idx), mk, COST, lag) for lag in (0, 1)}
MM = {lag: backtest(pd.Series(0.0, index=mk.idx), mk, COST, lag) for lag in (0, 1)}

# ------------------------------------------------------------------ (а) рамка D2
P("=== (а) рамка D2: месячный эталон и ES на конце месяца / любым днём ===")
ES = dict(thr=0.25, win=21, key="eff")
R_M = dict(comp="closed", comp_thr=0.10, comp_dec="monthly", bits={}, gate_entry="dec", gate_exit="dec")
es_real = mk.es_bit(0.25, 21, "eff")
es_month = pd.Series(np.where(mk.decision_mask("monthly"), es_real, np.nan), index=mk.idx).ffill().fillna(0).astype(int).values
EXTRA = {
    "R_m: всё на конце месяца (= вариант e у A)": R_M,
    "R_m + ES на конце месяца (рамка D2, месячная каденция)": dict(R_M, es=ES, es_exit="dec", es_reentry="dec"),
    "R_m + ES любым днём (выход), возврат на конце месяца": dict(R_M, es=ES, es_exit="any", es_reentry="dec"),
    "P0 + ES, бит читается на конце месяца и держится": dict(CFG["P0"], es=ES, es_bit=es_month, es_exit="any", es_reentry="any"),
    "P0 + ES любым днём (= A_es)": dict(CFG["P0"], es=ES, es_exit="any", es_reentry="any"),
    "P2 + ES, бит читается на конце месяца и держится": dict(CFG["P2"], es=ES, es_bit=es_month, es_exit="any", es_reentry="any"),
    "P2 + ES по пятницам (выход и возврат)": dict(CFG["P2"], es=ES, es_exit="dec", es_reentry="dec"),
    "P3 (= P2 + ES любым днём, возврат по пятницам)": CFG["P3"],
}
rows = []
POSX = {}
ENGX = {}
for nm, cfg in EXTRA.items():
    cfg2 = json.loads(json.dumps({k: v for k, v in cfg.items() if k != "es_bit"}))
    if "es_bit" in cfg:
        cfg2["es_bit"] = cfg["es_bit"]
    eng = run_cfg(mk, cfg2); POSX[nm] = eng["pos"]; ENGX[nm] = eng
    for lag in (1, 0):
        bt = backtest(eng["pos"], mk, COST, lag)
        for wn in ["rates_2015_2026", "main_2010_2026", "full_2004_2026", "split_B_2018_2026"]:
            s, e = W2[wn]
            m = metrics(bt, BH[lag], MM[lag], s, e)
            rows.append(dict(strategy=nm, window=wn, exec_lag=lag, **{k: m[k] for k in ("cagr", "sharpe", "sharpe_ex_mm", "mdd", "mdd_date", "time_in_mkt", "trades_per_year")}))
for nm in ["P0", "P1", "P2", "P3"]:
    for lag in (1, 0):
        bt = backtest(POS[nm], mk, COST, lag)
        s, e = W2["rates_2015_2026"]; m = metrics(bt, BH[lag], MM[lag], s, e)
        rows.append(dict(strategy=nm, window="rates_2015_2026", exec_lag=lag, **{k: m[k] for k in ("cagr", "sharpe", "sharpe_ex_mm", "mdd", "mdd_date", "time_in_mkt", "trades_per_year")}))
D2F = pd.DataFrame(rows); D2F.to_csv(os.path.join(RES, "P_extra_d2frame.csv"), index=False, float_format="%.4f")
pd.set_option("display.width", 250); pd.set_option("display.max_colwidth", 60)
for wn in ["rates_2015_2026", "main_2010_2026"]:
    P(f"\n--- {wn} ---")
    P(D2F[D2F.window == wn].pivot(index="strategy", columns="exec_lag", values=["sharpe", "sharpe_ex_mm", "mdd", "cagr", "trades_per_year"]).to_string(float_format=lambda x: f"{x:.3f}"))
# контроль: R_m должен совпасть с A вариантом e (14,1 % / 0,99 / −34,3 % при t)
A = pd.read_csv(os.path.join(RES, "A_positions.csv"), parse_dates=["date"]).set_index("date")
diff_e = (POSX["R_m: всё на конце месяца (= вариант e у A)"].reindex(A.index).fillna(0).astype(int) != A["pos_e"].astype(int)).sum()
P(f"\nКОНТРОЛЬ: R_m против A_positions.pos_e — расхождений {diff_e} из {len(A)}")

# ------------------------------------------------------------------ (а2) гибридное исполнение: ES-выход в t, всё остальное в t+1
P("\n=== (а2) гибридное исполнение P3: выход по ES исполняется на закрытии дня сигнала, остальные смены — на следующем закрытии ===")
eng3 = run_cfg(mk, json.loads(json.dumps(CFG["P3"])))
p_exec = eng3["pos"].shift(1).fillna(0.0)                      # всё в t+1
p_exec[eng3["reason"] == "es_exit"] = 0.0                        # ES-выход — уже в t
rows = []
for wn in ["main_2010_2026", "full_2004_2026", "rates_2015_2026", "ex2022_2010_2026", "split_A_2004_2017", "split_B_2018_2026"]:
    s, e = W2[wn]
    bt = backtest(p_exec, mk, COST, 0)
    m = metrics(bt, BH[0], MM[0], s, e, ex2022=wn.startswith("ex2022"))
    m2 = metrics(backtest(POS["P2"], mk, COST, 1), BH[1], MM[1], s, e, ex2022=wn.startswith("ex2022"))
    m3 = metrics(backtest(POS["P3"], mk, COST, 1), BH[1], MM[1], s, e, ex2022=wn.startswith("ex2022"))
    rows.append(dict(window=wn, hybrid_sharpe=m["sharpe"], hybrid_shex=m["sharpe_ex_mm"], hybrid_cagr=m["cagr"], hybrid_mdd=m["mdd"],
                     P2_t1_sharpe=m2["sharpe"], P2_t1_mdd=m2["mdd"], P3_t1_sharpe=m3["sharpe"], P3_t1_mdd=m3["mdd"]))
HY = pd.DataFrame(rows); HY.to_csv(os.path.join(RES, "P_extra_hybrid.csv"), index=False, float_format="%.4f")
P(HY.to_string(index=False, float_format=lambda x: f"{x:.3f}"))
nav_h = backtest(p_exec, mk, COST, 0)["nav"]
bb = sharpe_diff_boot(monthly_logret(slice_nav(nav_h, *WINDOWS["main_2010_2026"])), monthly_logret(slice_nav(backtest(POS["P2"], mk, COST, 1)["nav"], *WINDOWS["main_2010_2026"])),
                      monthly_logret(slice_nav(MM[1]["nav"], *WINDOWS["main_2010_2026"])))
P(f"гибрид − P2(t+1), main: ΔШарп {bb['delta']:+.3f} [{bb['ci05']:+.2f}; {bb['ci95']:+.2f}] p={bb['p_le0']:.3f}; над ММ {bb['delta_ex']:+.3f} p={bb['p_le0_ex']:.3f}")

# ------------------------------------------------------------------ (а3) плацебо «день месяца» для месячного чтения ES
P("\n=== (а3) ES читается в k-й торговый день месяца и держится месяц (k=1…20 и последний): Шарп main / 2015+ (t+1 и t) ===")
def kth_day_mask(k):
    per = mk.idx.to_period("M"); s = pd.Series(np.arange(mk.n), index=mk.idx)
    rank = s.groupby(per).cumcount() + 1
    cnt = s.groupby(per).transform("size")
    return (rank == np.minimum(k, cnt)).values
rows = []
for base_nm in ["P2", "P0"]:
    for k in list(range(1, 21)) + ["last"]:
        m = mk.decision_mask("monthly") if k == "last" else kth_day_mask(k)
        held = pd.Series(np.where(m, es_real, np.nan), index=mk.idx).ffill().fillna(0).astype(int).values
        cfg = dict(CFG[base_nm], es=ES, es_reentry="any"); cfg["es_bit"] = held
        eng = run_cfg(mk, cfg)
        r = dict(base=base_nm, k=k)
        for lag in (1, 0):
            bt = backtest(eng["pos"], mk, COST, lag)
            for wn in ["main_2010_2026", "rates_2015_2026"]:
                s, e = W2[wn]; mm_ = metrics(bt, BH[lag], MM[lag], s, e)
                r[f"sh_{wn[:4]}_t{lag}"] = mm_["sharpe"]; r[f"mdd_{wn[:4]}_t{lag}"] = mm_["mdd"]
                if lag == 1: r[f"shex_{wn[:4]}"] = mm_["sharpe_ex_mm"]
        rows.append(r)
DOM = pd.DataFrame(rows); DOM.to_csv(os.path.join(RES, "P_extra_dom_placebo.csv"), index=False, float_format="%.4f")
for base_nm in ["P2", "P0"]:
    x = DOM[DOM.base == base_nm]; xl = x[x.k == "last"].iloc[0]; xk = x[x.k != "last"]
    base_sh = MET[(MET.config == base_nm) & (MET.window == "main_2010_2026") & (MET.exec_lag == 1) & (MET.cost == 0.002)].sharpe.iloc[0]
    P(f"\nбаза {base_nm} (Шарп main t+1 {base_sh:.3f}): последний день → main {xl.sh_main_t1:.3f} (t {xl.sh_main_t0:.3f}), 2015+ {xl.sh_rate_t1:.3f}; "
      f"k=1…20: main t+1 медиана {xk.sh_main_t1.median():.3f} [{xk.sh_main_t1.min():.3f}…{xk.sh_main_t1.max():.3f}], ранг последнего дня среди 21: "
      f"{int((xk.sh_main_t1 > xl.sh_main_t1).sum()) + 1}; 2015+ t+1 медиана {xk.sh_rate_t1.median():.3f} [{xk.sh_rate_t1.min():.3f}…{xk.sh_rate_t1.max():.3f}]; "
      f"дней k с main t+1 ≥ базы: {int((xk.sh_main_t1 >= base_sh).sum())}/20")
    P(x[["k", "sh_main_t1", "sh_main_t0", "shex_main", "mdd_main_t1", "sh_rate_t1", "sh_rate_t0", "mdd_rate_t1"]].to_string(index=False, float_format=lambda v: f"{v:.3f}"))

# ------------------------------------------------------------------ (б) день после сигнала
P("\n=== (б) доходность MCFTR после сигналов ВЫХОДА, по причинам (сигнал на закрытии t) ===")
lr = mk.logret_eq.fillna(0)
rows = []
for nm in ["P0", "P2", "P3", "P3m", "P7", "A_es", "A_esm", "A_veto_m"]:
    s = SW[(SW.config == nm) & (SW.to == "FLAT")]
    for reason, g in s.groupby("reason"):
        d1, d2, d5 = [], [], []
        for d in pd.to_datetime(g.signal_date):
            i = mk.idx.get_loc(d)
            d1.append(lr.iloc[i + 1] if i + 1 < mk.n else np.nan)
            d2.append(lr.iloc[i + 2] if i + 2 < mk.n else np.nan)
            d5.append(lr.iloc[i + 2:i + 6].sum() if i + 5 < mk.n else np.nan)
        d1, d2, d5 = np.array(d1), np.array(d2), np.array(d5)
        rows.append(dict(config=nm, reason=reason, n=len(g), ret_t1_mean=np.nanmean(d1) * 100, ret_t1_hit_neg=np.nanmean(d1 < 0),
                         ret_t2_mean=np.nanmean(d2) * 100, ret_t2_hit_neg=np.nanmean(d2 < 0), ret_t2_t5_mean=np.nanmean(d5) * 100,
                         t1_mean_minus_t2=(np.nanmean(d1) - np.nanmean(d2)) * 100))
DA = pd.DataFrame(rows); DA.to_csv(os.path.join(RES, "P_extra_dayafter.csv"), index=False, float_format="%.3f")
P(DA.to_string(index=False, float_format=lambda x: f"{x:.2f}"))
P("чтение: ret_t1 — доходность дня t+1 (её получает исполнение t, теряет исполнение t+1); ret_t2 — дня t+2.")

# ------------------------------------------------------------------ (в) P5: ложные входы по стабилизации
P("\n=== (в) P5: входы по стабилизации (stab_on) — результат до следующего выхода ===")
rows = []
for nm in ["P5", "A_stab", "S5_nocomp", "S5_onP3"]:
    s = SW[SW.config == nm].reset_index(drop=True)
    nav = backtest(POS[nm], mk, COST, 1)["nav"]; navmm = MM[1]["nav"]
    for i, r in s.iterrows():
        if r.reason != "stab_on":
            continue
        nxt = s.iloc[i + 1] if i + 1 < len(s) else None
        d0 = pd.Timestamp(r.exec_date); d1 = pd.Timestamp(nxt.exec_date) if nxt is not None else mk.idx[-1]
        ret = nav.loc[d1] / nav.loc[d0] - 1; retmm = navmm.loc[d1] / navmm.loc[d0] - 1
        rows.append(dict(config=nm, entry=r.signal_date, exit=(nxt.signal_date if nxt is not None else "открыта"), exit_reason=(nxt.reason if nxt is not None else ""),
                         days=int((mk.idx > d0).sum() - (mk.idx > d1).sum()), ret_pct=ret * 100, cash_pct=retmm * 100, excess_pct=(ret - retmm) * 100, false=(ret < retmm)))
SE = pd.DataFrame(rows); SE.to_csv(os.path.join(RES, "P_extra_stab_entries.csv"), index=False, float_format="%.2f")
for nm in ["P5", "A_stab"]:
    x = SE[SE.config == nm].copy(); x["year"] = x.entry.str[:4].astype(int)
    P(f"\n{nm}: входов по стабилизации {len(x)}, ложных (хуже кэша) {int(x.false.sum())}; по годам:")
    P(x.groupby("year").agg(n=("false", "size"), false=("false", "sum"), excess_pp=("excess_pct", "sum")).to_string())
    P(x[x.year.isin([2008, 2020, 2022, 2024, 2025, 2026])][["entry", "exit", "exit_reason", "days", "ret_pct", "cash_pct", "false"]].to_string(index=False, float_format=lambda v: f"{v:.1f}"))

# ------------------------------------------------------------------ (г) даты MDD
P("\n=== (г) MDD и даты, лестница (t+1 / t) ===")
MD = MET[(MET.cost == 0.002) & (MET.group.isin(["лестница", "диагностика"])) & (MET.window.isin(["main_2010_2026", "full_2004_2026"]))][["config", "window", "exec_lag", "mdd", "mdd_date"]]
MD.to_csv(os.path.join(RES, "P_extra_mdd_dates.csv"), index=False)
P(MD.pivot(index="config", columns=["window", "exec_lag"], values=["mdd", "mdd_date"]).to_string())

# ------------------------------------------------------------------ (д) день недели: среднее и разброс
P("\n=== (д) P1 по дням недели и порогам: Шарп main/full, t+1 и t; среднее по пяти дням против P0 ===")
rows = []
for lag in (1, 0):
    for wn in ["main_2010_2026", "full_2004_2026", "ex2022_2010_2026"]:
        p0 = MET[(MET.config == "P0") & (MET.window == wn) & (MET.exec_lag == lag) & (MET.cost == 0.002)].sharpe.iloc[0]
        for thr in (0.1, 0.2, 0.3):
            vals = {wd: MET[(MET.config == f"S1_{wd}_{thr}") & (MET.window == wn) & (MET.exec_lag == lag) & (MET.cost == 0.002)].sharpe.iloc[0] for wd in ["MON", "TUE", "WED", "THU", "FRI"]}
            rows.append(dict(window=wn, exec_lag=lag, thr=thr, P0=p0, **vals, mean5=np.mean(list(vals.values())), min5=min(vals.values()), max5=max(vals.values()),
                             n_ge_P0=sum(v >= p0 for v in vals.values())))
WD = pd.DataFrame(rows); WD.to_csv(os.path.join(RES, "P_extra_weekday.csv"), index=False, float_format="%.3f")
P(WD.to_string(index=False, float_format=lambda x: f"{x:.3f}"))
log.close()
print("\nготово: P_extra_*.csv, P_extra_log.txt")
