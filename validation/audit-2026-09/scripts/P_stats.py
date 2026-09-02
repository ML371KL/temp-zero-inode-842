"""P_package, прогон 2 (после P_run.py): бутстреп ΔШарпа (к P0 и к предыдущей ступени), своевременность
по зигзагу 15 % IMOEX, годовые доходности и годовые вклады ступеней, таблица «что бы сказал каждый Pk сегодня»,
переключения 2024–2026, плацебо (случайные биты-выходы той же доли и длины серий, 500 повторов) для ES (P3)
и для вето (P7), walk-forward порогов (ES и вето) с расширяющимся окном, диагностика ES-эпизодов, счётчик перебора.
Выход: results/P_bootstrap.csv, P_timeliness.csv, P_timeliness_summary.csv, P_yearly.csv, P_yearly_increments.csv,
P_today.csv, P_switches_2024_2026.csv, P_placebo.csv, P_placebo_reps.csv, P_walkforward.csv, P_es_episodes.csv,
P_stats_log.txt. Запуск: python scripts/P_stats.py (из audit/)."""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import pandas as pd
from P_lib import *

t0 = time.time()
mk = Market()
CF = pd.read_csv(os.path.join(RES, "P_configs.csv"))
CFG = {r.config: json.loads(r.cfg) for r in CF.itertuples()}
GROUP = dict(zip(CF.config, CF.group))
POS = pd.read_csv(os.path.join(RES, "P_positions.csv"), parse_dates=["date"]).set_index("date")
best = open(os.path.join(RES, "P_best.txt"), encoding="utf-8").read().strip()
LADDER = ["P0", "P1", "P2", "P3", "P3m", "P4", "P5", "P6a", "P6b", "P7"]
ABL = [c for c in CF.config if GROUP[c] in ("абляция", "LOO от P3")]
KEYSENS = ["S2_B_thr3", "S2_vol_only", "S3_0.35_10_eff", "S3_0.1_21_eff", "S7_0.2_dec", "S7_onP3", "S1_entry_dec", "S3m_W", "S4m_mcftr", "S3m_onP1", "S3m_0.35_10", "S3m_0.1_21"]
SEL = LADDER + ABL + KEYSENS
log = open(os.path.join(RES, "P_stats_log.txt"), "w", encoding="utf-8")
def P(*a):
    s = " ".join(str(x) for x in a); print(s); log.write(s + "\n")

BT = {nm: {lag: backtest(POS[nm], mk, COST, lag) for lag in (0, 1)} for nm in SEL}
BT["bh"] = {lag: backtest(pd.Series(1.0, index=mk.idx), mk, COST, lag) for lag in (0, 1)}
BT["mm"] = {lag: backtest(pd.Series(0.0, index=mk.idx), mk, COST, lag) for lag in (0, 1)}


def mret(nm, wn, lag):
    s, e = WINDOWS[wn]
    nav = slice_nav(BT[nm][lag]["nav"], s, e)
    r = monthly_logret(nav)
    if wn.startswith("ex2022"):
        r = r[r.index.year != 2022]
    return r


# ============================================================ 1. бутстреп ΔШарпа
P("=== 1. бутстреп ΔШарпа (стационарный, блок 12 мес, 2000) ===")
rows = []
PAIRS = [(nm, "P0") for nm in SEL if nm != "P0"]
PAIRS += [("P1", "P0"), ("P2", "P1"), ("P3", "P2"), ("P4", "P3"), ("P5", "P4"), ("P7", "P2"), ("P6a", "P3"), ("P6b", "P3"),
          ("S7_onP3", "P3"), ("S2_B_thr3", "P2"), ("S3_0.35_10_eff", "P3"), ("S7_0.2_dec", "P2"), ("P3", "P1"), ("P2", "P0"),
          ("P3m", "P2"), ("P3m", "P3"), ("A_esm", "P0"), ("S3m_W", "P2"), ("S4m_mcftr", "P3m"), ("S3m_onP1", "P1"), ("S3m_0.35_10", "P3m"), ("S3m_0.1_21", "P3m")]
seen = set()
for a, b in PAIRS:
    for wn in ["main_2010_2026", "full_2004_2026", "ex2022_2010_2026", "split_A_2004_2017", "split_B_2018_2026"]:
        for lag in ((1, 0) if wn == "main_2010_2026" else (1,)):
            key = (a, b, wn, lag)
            if key in seen:
                continue
            seen.add(key)
            r = sharpe_diff_boot(mret(a, wn, lag), mret(b, wn, lag), mret("mm", wn, lag))
            rows.append(dict(a=a, b=b, window=wn, exec_lag=lag, **r))
BOOT = pd.DataFrame(rows)
BOOT.to_csv(os.path.join(RES, "P_bootstrap.csv"), index=False, float_format="%.4f")
for wn in ["main_2010_2026", "full_2004_2026", "ex2022_2010_2026", "split_A_2004_2017", "split_B_2018_2026"]:
    sub = BOOT[(BOOT.window == wn) & (BOOT.exec_lag == 1)]
    P(f"\n--- {wn}, t+1 ---")
    P(sub[["a", "b", "delta", "ci05", "ci95", "p_le0", "delta_ex", "p_le0_ex"]].to_string(index=False, float_format=lambda x: f"{x:+.3f}"))
sub = BOOT[(BOOT.window == "main_2010_2026") & (BOOT.exec_lag == 0)]
P("\n--- main, exec t ---")
P(sub[["a", "b", "delta", "ci05", "ci95", "p_le0", "delta_ex", "p_le0_ex"]].to_string(index=False, float_format=lambda x: f"{x:+.3f}"))

# ============================================================ 2. своевременность
P("\n=== 2. своевременность: зигзаг 15 % по IMOEX с 2004-01-06, exec t+1 ===")
start_i = int(np.searchsorted(mk.idx.values, BT_START.to_datetime64()))
px = mk.imoex.values
eps_rel = zigzag_episodes(px[start_i:], 0.15)
eps = [[a + start_i, b + start_i, c + start_i] for a, b, c in eps_rel]
P(f"эпизодов: {len(eps)}")
TL = []; SUMM = []
for nm in SEL:
    E = timeliness(POS[nm], mk, eps, 1); E.insert(0, "config", nm); TL.append(E)
    SUMM.append(dict(config=nm, **timeliness_summary(E)))
TL = pd.concat(TL, ignore_index=True); TL.to_csv(os.path.join(RES, "P_timeliness.csv"), index=False, float_format="%.4f")
SM = pd.DataFrame(SUMM); SM.to_csv(os.path.join(RES, "P_timeliness_summary.csv"), index=False, float_format="%.3f")
P(SM.set_index("config").to_string(float_format=lambda x: f"{x:.2f}"))
# крупные эпизоды подробно для лестницы
big = TL[(TL.depth < -0.25) & (TL.config.isin(["P0", "P1", "P2", "P3", "P7"]))]
P("\nэпизоды глубже 25 %, лестница:")
P(big[["config", "peak", "trough", "depth", "pos_at_peak", "exit_lag_days", "exit_note", "avoided_share", "entry_lag_days", "entry_note", "missed_63", "missed_to_next_peak"]]
  .to_string(index=False, float_format=lambda x: f"{x:.2f}"))

# ============================================================ 3. годовые доходности и вклады ступеней
P("\n=== 3. годовые доходности (t+1, 0,2 %) ===")
Y = {}
for nm in LADDER + ["bh", "mm"]:
    nav = BT[nm][1]["nav"].loc[BT_START:BT_END]
    base = BT[nm][1]["nav"].loc[:BT_START].iloc[-2] if (mk.idx < BT_START).sum() else 1.0
    yr = nav.groupby(nav.index.year).last()
    prev = yr.shift(1); prev.iloc[0] = base
    Y[nm] = yr / prev - 1
Y = pd.DataFrame(Y); Y.index.name = "year"
Y.to_csv(os.path.join(RES, "P_yearly.csv"), float_format="%.4f")
P((Y * 100).round(1).to_string())
INC = pd.DataFrame({"P1-P0": Y["P1"] - Y["P0"], "P2-P1": Y["P2"] - Y["P1"], "P3-P2": Y["P3"] - Y["P2"], "P3m-P2": Y["P3m"] - Y["P2"], "P4-P3": Y["P4"] - Y["P3"],
                    "P5-P4": Y["P5"] - Y["P4"], "P7-P2": Y["P7"] - Y["P2"], "P6a-P3": Y["P6a"] - Y["P3"], "P6b-P3": Y["P6b"] - Y["P3"],
                    "P2-P0": Y["P2"] - Y["P0"], "P3-P0": Y["P3"] - Y["P0"], "P3m-P0": Y["P3m"] - Y["P0"]})
INC.to_csv(os.path.join(RES, "P_yearly_increments.csv"), float_format="%.4f")
P("\nгодовые вклады ступеней (п.п. простой доходности):")
P((INC * 100).round(1).to_string())
P("\nсумма вкладов по годам, всего / без 2020 / без 2022 / без 2020 и 2022 (п.п.):")
for c in INC.columns:
    s = INC[c] * 100
    P(f"  {c:7s} всего {s.sum():+6.1f} | ex2020 {s.drop(2020).sum():+6.1f} | ex2022 {s.drop(2022).sum():+6.1f} | ex-both {s.drop([2020, 2022]).sum():+6.1f} | "
      f"лет > 0: {(s > 0).sum()} / < 0: {(s < 0).sum()} / = 0: {(s.abs() < 0.05).sum()}")

# ============================================================ 4. сегодня и переключения 2024–2026
P("\n=== 4. что бы сказал каждый Pk на 2026-09-01 ===")
SWALL = pd.read_csv(os.path.join(RES, "P_switches_all.csv"))
today = mk.idx[-1]
rows = []
for nm in LADDER + ["A_veto_m", "S7_onP3", "A_esm"]:
    p = POS[nm].iloc[-1]
    s = SWALL[SWALL.config == nm]
    last = s.iloc[-1] if len(s) else None
    n_2024 = int((s.signal_date >= "2024-01-01").sum())
    rows.append(dict(config=nm, position_2026_09_01=("ЛОНГ" if p >= 1 else ("50 %" if p > 0 else "ФЛЭТ")), last_change=last.signal_date if last is not None else "",
                     last_exec=last.exec_date if last is not None else "", last_reason=REASON_RU.get(last.reason, last.reason) if last is not None else "",
                     switches_2024_2026=n_2024, days_in_state=int((mk.idx > pd.Timestamp(last.signal_date)).sum()) if last is not None else None))
TODAY = pd.DataFrame(rows); TODAY.to_csv(os.path.join(RES, "P_today.csv"), index=False)
P(TODAY.to_string(index=False))
S24 = SWALL[(SWALL.signal_date >= "2024-01-01") & (SWALL.config.isin(["P0", "P2", "P3", "P3m", "P7", "A_esm"]))].copy()
S24["reason_ru"] = S24.reason.map(lambda r: REASON_RU.get(r, r))
S24.to_csv(os.path.join(RES, "P_switches_2024_2026.csv"), index=False)
for nm in ["P0", "P2", "P3", "P3m"]:
    P(f"\nпереключения {nm} 2024-01…2026-09:")
    P(S24[S24.config == nm][["signal_date", "exec_date", "to", "reason_ru"]].to_string(index=False))

# ============================================================ 5. плацебо: случайные биты той же доли и длины серий
P("\n=== 5. плацебо (500 повторов) ===")
N_REP = 500
def sharpe_of(pos, wn, lag):
    bt = backtest(pos, mk, COST, lag)
    s, e = WINDOWS[wn]
    return metrics(bt, BT["bh"][lag], BT["mm"][lag], s, e)

def placebo(name, base_cfg, real_bit, avail, kind):
    rng = np.random.default_rng(2026)
    real = {("main", 1): sharpe_of(POS[name], "main_2010_2026", 1), ("main", 0): sharpe_of(POS[name], "main_2010_2026", 0),
            ("full", 1): sharpe_of(POS[name], "full_2004_2026", 1)}
    reps = []
    for k in range(N_REP):
        fb = random_runs_bit(real_bit, avail, rng)
        cfg = dict(base_cfg)
        if kind == "es":
            cfg["es_bit"] = fb; eng = run_engine(mk, {kk: vv for kk, vv in cfg.items() if kk not in ("veto", "overlay", "es_sample")})
        elif kind == "es_m":   # случайный дневной бит той же структуры, затем чтение на конце месяца и удержание
            cfg["es_bit"] = sample_hold(mk, fb, "monthly"); eng = run_engine(mk, {kk: vv for kk, vv in cfg.items() if kk not in ("veto", "overlay", "es_sample")})
        else:
            eng = run_engine_veto(mk, dict({kk: vv for kk, vv in cfg.items() if kk not in ("veto", "overlay")}, veto_bit=fb, veto_mode="dec"))
        m1 = sharpe_of(eng["pos"], "main_2010_2026", 1); m0 = sharpe_of(eng["pos"], "main_2010_2026", 0); mf = sharpe_of(eng["pos"], "full_2004_2026", 1)
        reps.append(dict(name=name, rep=k, sh_main_t1=m1["sharpe"], shex_main_t1=m1["sharpe_ex_mm"], mdd_main_t1=m1["mdd"], sh_main_t0=m0["sharpe"],
                         sh_full_t1=mf["sharpe"], mdd_full_t1=mf["mdd"], cagr_main_t1=m1["cagr"]))
    R = pd.DataFrame(reps)
    out = dict(name=name, n_rep=N_REP)
    for col, rv in [("sh_main_t1", real[("main", 1)]["sharpe"]), ("shex_main_t1", real[("main", 1)]["sharpe_ex_mm"]), ("sh_main_t0", real[("main", 0)]["sharpe"]),
                    ("sh_full_t1", real[("full", 1)]["sharpe"]), ("cagr_main_t1", real[("main", 1)]["cagr"])]:
        out[f"real_{col}"] = rv; out[f"placebo_mean_{col}"] = R[col].mean(); out[f"placebo_p95_{col}"] = R[col].quantile(0.95)
        out[f"p_ge_real_{col}"] = float((R[col] >= rv).mean())
    for col, rv in [("mdd_main_t1", real[("main", 1)]["mdd"]), ("mdd_full_t1", real[("full", 1)]["mdd"])]:
        out[f"real_{col}"] = rv; out[f"placebo_mean_{col}"] = R[col].mean(); out[f"placebo_p05_{col}"] = R[col].quantile(0.05)
        out[f"p_le_real_{col}"] = float((R[col] <= rv).mean())   # доля плацебо с просадкой не хуже реальной
    return out, R

PL = []; PR = []
# ES: реальный бит P3 (0,25/21) поверх P2; окно доступности — где d21 определён
es_real = mk.es_bit(0.25, 21, "eff")
sp = (mk.y1 - mk.key_eff); d21 = (sp - sp.shift(21)).to_numpy()
avail_es = np.isfinite(d21)
o, R = placebo("P3", CFG["P2"], es_real, avail_es, "es"); PL.append(o); PR.append(R)
P(f"ES: реальных дней бита {es_real.sum()} из {avail_es.sum()} доступных ({es_real.sum()/avail_es.sum():.3f}); плацебо-повторов {N_REP}")
o, R = placebo("P3m", dict(CFG["P2"], es_reentry="any"), es_real, avail_es, "es_m"); PL.append(o); PR.append(R)
o, R = placebo("A_esm", dict(CFG["P0"], es_reentry="any"), es_real, avail_es, "es_m"); PL.append(o); PR.append(R)
# вето: реальный бит P7 (dist>0,10) поверх лучшей ступени, окно — где dist определён
vb_real = mk.veto_bit(0.10, 0); avail_v = np.isfinite(mk.dist.to_numpy())
o, R = placebo("P7", CFG[best], vb_real, avail_v, "veto"); PL.append(o); PR.append(R)
# вето на P0 помесячно (A_veto_m) — конструкция D1
vb_m = pd.Series(np.where(mk.decision_mask("monthly"), vb_real, np.nan), index=mk.idx).ffill().fillna(0).astype(int).values
def placebo_vm(name):
    rng = np.random.default_rng(7); reps = []
    real = sharpe_of(POS[name], "main_2010_2026", 1); realf = sharpe_of(POS[name], "full_2004_2026", 1); real0 = sharpe_of(POS[name], "main_2010_2026", 0)
    for k in range(N_REP):
        fb = random_runs_bit(vb_m, avail_v, rng)
        eng = run_engine_veto(mk, dict(CFG["P0"], veto_bit=fb, veto_mode="any"))
        m1 = sharpe_of(eng["pos"], "main_2010_2026", 1); m0 = sharpe_of(eng["pos"], "main_2010_2026", 0); mf = sharpe_of(eng["pos"], "full_2004_2026", 1)
        reps.append(dict(name=name, rep=k, sh_main_t1=m1["sharpe"], shex_main_t1=m1["sharpe_ex_mm"], mdd_main_t1=m1["mdd"], sh_main_t0=m0["sharpe"],
                         sh_full_t1=mf["sharpe"], mdd_full_t1=mf["mdd"], cagr_main_t1=m1["cagr"]))
    R = pd.DataFrame(reps)
    out = dict(name=name, n_rep=N_REP)
    for col, rv in [("sh_main_t1", real["sharpe"]), ("shex_main_t1", real["sharpe_ex_mm"]), ("sh_main_t0", real0["sharpe"]), ("sh_full_t1", realf["sharpe"]), ("cagr_main_t1", real["cagr"])]:
        out[f"real_{col}"] = rv; out[f"placebo_mean_{col}"] = R[col].mean(); out[f"placebo_p95_{col}"] = R[col].quantile(0.95); out[f"p_ge_real_{col}"] = float((R[col] >= rv).mean())
    for col, rv in [("mdd_main_t1", real["mdd"]), ("mdd_full_t1", realf["mdd"])]:
        out[f"real_{col}"] = rv; out[f"placebo_mean_{col}"] = R[col].mean(); out[f"placebo_p05_{col}"] = R[col].quantile(0.05); out[f"p_le_real_{col}"] = float((R[col] <= rv).mean())
    return out, R
o, R = placebo_vm("A_veto_m"); PL.append(o); PR.append(R)
PLD = pd.DataFrame(PL); PLD.to_csv(os.path.join(RES, "P_placebo.csv"), index=False, float_format="%.4f")
pd.concat(PR).to_csv(os.path.join(RES, "P_placebo_reps.csv"), index=False, float_format="%.4f")
P(PLD.T.to_string(float_format=lambda x: f"{x:.3f}"))

# ============================================================ 6. walk-forward порогов (расширяющееся окно, выбор по прошлому)
P("\n=== 6. walk-forward порогов ===")
def wf(grid_cfgs, label, start_year=2010):
    """grid_cfgs: dict(param -> cfg). Для года y выбираем параметр с max Шарпа (t+1) на 2004-01-06…y−1, применяем в году y."""
    posg = {k: run_cfg(mk, json.loads(json.dumps(c)))["pos"] for k, c in grid_cfgs.items()}
    btg = {k: backtest(p, mk, COST, 1) for k, p in posg.items()}
    pos_wf = pd.Series(0.0, index=mk.idx); chosen = []
    for y in range(start_year, 2027):
        e = pd.Timestamp(f"{y-1}-12-31")
        sc = {}
        for k, bt in btg.items():
            r = monthly_logret(slice_nav(bt["nav"], BT_START, e))
            sc[k] = sharpe_m(r)
        kbest = max(sc, key=sc.get)
        m = (mk.idx.year == y)
        pos_wf[m] = posg[kbest][m]
        chosen.append(dict(year=y, chosen=str(kbest), **{f"sh_{k}": round(v, 3) for k, v in sc.items()}))
    return pos_wf, pd.DataFrame(chosen)

WF = []
# ES: порог × окно поверх P2
grid = {f"{thr}/{win}": dict(CFG["P2"], es=dict(thr=thr, win=win, key="eff"), es_reentry="dec") for thr in (0.10, 0.15, 0.25, 0.35) for win in (10, 21)}
pos_wf_es, ch_es = wf(grid, "ES")
ch_es.to_csv(os.path.join(RES, "P_walkforward_es_choices.csv"), index=False)
gridm = {f"{thr}/{win}": dict(CFG["P2"], es=dict(thr=thr, win=win, key="eff"), es_reentry="any", es_sample="monthly") for thr in (0.10, 0.15, 0.25, 0.35) for win in (10, 21)}
pos_wf_esm, ch_esm = wf(gridm, "ESm")
ch_esm.to_csv(os.path.join(RES, "P_walkforward_esm_choices.csv"), index=False)
# вето: порог поверх лучшей ступени
gridv = {f"{thr}": dict(CFG[best], veto=dict(thr=thr, lag=0, mode="dec")) for thr in (0.05, 0.10, 0.15, 0.20)}
pos_wf_v, ch_v = wf(gridv, "veto")
ch_v.to_csv(os.path.join(RES, "P_walkforward_veto_choices.csv"), index=False)
# вето на P0 помесячно (конструкция D1)
gridvm = {f"{thr}": dict(CFG["P0"], veto=dict(thr=thr, lag=0, mode="dec_monthly")) for thr in (0.05, 0.10, 0.15, 0.20)}
pos_wf_vm, ch_vm = wf(gridvm, "veto_m")
ch_vm.to_csv(os.path.join(RES, "P_walkforward_vetom_choices.csv"), index=False)
for label, pos_wf, ref in [("ES walk-forward (порог×окно) на P2", pos_wf_es, ["P2", "P3"]), ("ES месячный walk-forward (порог×окно) на P2", pos_wf_esm, ["P2", "P3m"]),
                           ("вето walk-forward (порог) на " + best, pos_wf_v, [best, "P7"]), ("вето помесячно walk-forward на P0", pos_wf_vm, ["P0", "A_veto_m"])]:
    for lag in (1, 0):
        bt = backtest(pos_wf, mk, COST, lag)
        for wn in ["main_2010_2026", "split_B_2018_2026", "ex2022_2010_2026"]:
            s, e = WINDOWS[wn]
            m = metrics(bt, BT["bh"][lag], BT["mm"][lag], s, e, ex2022=wn.startswith("ex2022"))
            row = dict(strategy=label, window=wn, exec_lag=lag, **{k: m[k] for k in ("cagr", "sharpe", "sharpe_ex_mm", "mdd", "time_in_mkt", "trades_per_year")})
            for r in ref:
                mr = metrics(BT[r][lag], BT["bh"][lag], BT["mm"][lag], s, e, ex2022=wn.startswith("ex2022"))
                row[f"sharpe_{r}"] = mr["sharpe"]; row[f"mdd_{r}"] = mr["mdd"]
            if lag == 1:
                bb = sharpe_diff_boot(monthly_logret(slice_nav(bt["nav"], s, e)) if not wn.startswith("ex2022") else monthly_logret(slice_nav(bt["nav"], s, e)).pipe(lambda r: r[r.index.year != 2022]),
                                      mret(ref[0], wn, 1), mret("mm", wn, 1), n_boot=1000)
                row["dsharpe_vs_base"] = bb["delta"]; row["p_le0"] = bb["p_le0"]
            WF.append(row)
WFD = pd.DataFrame(WF); WFD.to_csv(os.path.join(RES, "P_walkforward.csv"), index=False, float_format="%.4f")
P(WFD.to_string(index=False, float_format=lambda x: f"{x:.3f}"))
P("\nвыбор порога ES по годам:"); P(ch_es.to_string(index=False))
P("\nвыбор порога ES (месячное чтение) по годам:"); P(ch_esm.to_string(index=False))
P("\nвыбор порога вето по годам:"); P(ch_v.to_string(index=False))
P("\nвыбор порога вето (помесячно, P0) по годам:"); P(ch_vm.to_string(index=False))

# ============================================================ 7. ES-эпизоды: что именно избегли
P("\n=== 7. ES-эпизоды: флэт-отрезки, вызванные битом (P3 против P2; P3m против P2) ===")
lr = mk.logret_eq.fillna(0).values; rmm = np.log1p(mk.ret_mm.fillna(0).values)
def es_segments(pa, pb, fname):
    eff2 = np.roll(pa, 2); eff2[:2] = 0; eff3 = np.roll(pb, 2); eff3[:2] = 0
    diffdays = (eff2 == 1) & (eff3 == 0)
    rows = []; i = 0; n = mk.n
    while i < n:
        if diffdays[i]:
            j = i
            while j < n and diffdays[j]:
                j += 1
            seg = slice(i, j)
            rows.append(dict(start=str(mk.idx[i].date()), end=str(mk.idx[j - 1].date()), days=j - i, year=mk.idx[i].year,
                             mcftr_logret=lr[seg].sum(), cash_logret=rmm[seg].sum(), avoided=(rmm[seg].sum() - lr[seg].sum())))
            i = j
        else:
            i += 1
    E = pd.DataFrame(rows); E.to_csv(os.path.join(RES, fname), index=False, float_format="%.4f")
    if len(E):
        P(f"отрезков {len(E)}, медиана длины {E.days.median():.0f} дн, средняя {E.days.mean():.1f}; доля отрезков с avoided>0: {(E.avoided > 0).mean():.2f}; "
          f"сумма avoided {E.avoided.sum()*100:+.1f} п.п. лог; медиана MCFTR за отрезок {E.mcftr_logret.median()*100:+.2f} %")
        g = E.groupby("year").agg(n=("days", "size"), days=("days", "sum"), avoided_pp=("avoided", lambda s: s.sum() * 100), hit=("avoided", lambda s: (s > 0).mean()))
        P(g.round(2).to_string())
        P("крупнейшие отрезки по |avoided|:"); P(E.reindex(E.avoided.abs().sort_values(ascending=False).index).head(12).to_string(index=False, float_format=lambda x: f"{x:.3f}"))
    return E
P("--- P3 (дневное чтение) против P2 ---"); ESE = es_segments(POS["P2"].values, POS["P3"].values, "P_es_episodes.csv")
P("\n--- P3m (чтение на конце месяца) против P2 ---"); ESEm = es_segments(POS["P2"].values, POS["P3m"].values, "P_es_episodes_m.csv")

# ============================================================ 8. счётчик перебора
n_cfg = len(CF)
P(f"\n=== 8. плата за перебор === конфигураций слоя решения: {n_cfg} (по группам: {CF.group.value_counts().to_dict()}); "
  f"× исполнение 2 × издержки 3 = {n_cfg*6} прогонов; плацебо 3×{N_REP}; walk-forward сеток: 8 (ES) + 4 (вето) + 4 (вето P0)")
P(f"время {time.time()-t0:.0f} с")
log.close()
