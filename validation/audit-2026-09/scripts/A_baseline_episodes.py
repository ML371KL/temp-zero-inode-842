"""A_baseline: своевременность (правило 7 брифа).
Просадки IMOEX глубже 15% (зигзаг пик→дно, дно подтверждается отскоком ≥15%) с 2004;
для каждого варианта: лаг выхода после пика, доля избегнутого падения, лаг входа после дна,
доля пропущенного восстановления (63 дня / до следующего пика / до нового максимума). Ложные выходы.
Выход: results/A_baseline_episodes.csv, results/A_baseline_episodes_summary.csv, results/A_baseline_false_exits.csv
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import pandas as pd
from A_baseline_lib import *

P = pd.read_csv(f"{RES}/A_positions.csv", parse_dates=["date"]).set_index("date")
P = P.loc[:BT_END]
idx = P.index; n = len(idx)
px = P["imoex"].values
rl = P["logret_mcftr"].fillna(0).values
rmm = np.log1p(P["ret_mm"].fillna(0)).values
THR = 0.15
VARS = ["c", "a", "b", "d", "e", "e2", "h", "i"]

# ------------------------------------------------------------- зигзаг
eps = []; mode = "up"; peak = 0; trough = None
for i in range(n):
    if mode == "up":
        if px[i] > px[peak]:
            peak = i
        elif px[i] <= px[peak] * (1 - THR):
            mode = "down"; trough = i
    else:
        if px[i] < px[trough]:
            trough = i
        elif px[i] >= px[trough] * (1 + THR):
            eps.append([peak, trough]); mode = "up"; peak = i
open_episode = (mode == "down")
if open_episode:
    eps.append([peak, trough])
for k, e in enumerate(eps):
    nxt = eps[k + 1][0] if k + 1 < len(eps) else (n - 1 if (open_episode and k == len(eps) - 1) else peak)
    e.append(nxt)
    rec = None
    for j in range(e[1] + 1, n):
        if px[j] >= px[e[0]]:
            rec = j; break
    e.append(rec)
print(f"эпизодов: {len(eps)}")


def run_start(pos, j):
    v = pos[j]; k = j
    while k - 1 >= 0 and pos[k - 1] == v:
        k -= 1
    return k


def share(pos_eff, a, b, arr):
    """1 − (понесено стратегией)/(всего) на (a,b]; pos_eff[t] — позиция, действующая в течение дня t."""
    if b <= a:
        return np.nan
    tot = arr[a + 1:b + 1].sum(); borne = (pos_eff[a + 1:b + 1] * arr[a + 1:b + 1]).sum()
    return 1 - borne / tot if abs(tot) > 1e-12 else np.nan


rows = []
for v in VARS:
    pos = P[f"pos_{v}"].values
    bt1 = backtest(P[f"pos_{v}"], P["ret_mcftr"], P["ret_mm"], COST, 1)
    bt0 = backtest(P[f"pos_{v}"], P["ret_mcftr"], P["ret_mm"], COST, 0)
    nav1 = bt1["nav"].values; nav0 = bt0["nav"].values
    navb = backtest(P["pos_f"], P["ret_mcftr"], P["ret_mm"], COST, 1)["nav"].values
    eff1 = np.roll(pos, 2); eff1[:2] = 0          # exec t+1: в день t действует позиция, решённая в t−2
    eff0 = np.roll(pos, 1); eff0[:1] = 0          # exec t: решённая в t−1
    for k, (Pk, Tk, Rk, Rec) in enumerate(eps):
        depth = px[Tk] / px[Pk] - 1
        # выход
        if pos[Pk] == 1:
            ex = next((j for j in range(Pk + 1, Rk + 1) if pos[j] == 0), None)
            exit_lag = (ex - Pk) if ex is not None else np.nan
            exit_note = "не выходила" if ex is None else ("выход после дна" if ex > Tk else "выход до дна")
            exit_date = str(idx[ex].date()) if ex is not None else ""
        else:
            j0 = run_start(pos, Pk)
            exit_lag = -(Pk - j0); exit_note = "флэт ещё до пика"; exit_date = str(idx[j0].date())
        # вход
        if pos[Tk] == 0:
            en = next((j for j in range(Tk + 1, n) if pos[j] == 1), None)
            entry_lag = (en - Tk) if en is not None else np.nan
            entry_note = "не вошла" if en is None else ("вход после след. пика" if en > Rk else "вход до след. пика")
            entry_date = str(idx[en].date()) if en is not None else ""
        else:
            j1 = run_start(pos, Tk)
            entry_lag = -(Tk - j1); entry_note = ("не выходила" if j1 <= Pk else "лонг ещё до дна"); entry_date = str(idx[j1].date())
        e63 = min(Tk + 63, n - 1)
        rows.append(dict(
            variant=v, ep=k + 1, peak=str(idx[Pk].date()), trough=str(idx[Tk].date()), next_peak=str(idx[Rk].date()),
            recovered=str(idx[Rec].date()) if Rec is not None else "", depth=depth,
            fall_days=Tk - Pk, days_to_next_peak=Rk - Tk, days_to_recovery=(Rec - Tk) if Rec is not None else np.nan,
            pos_at_peak=int(pos[Pk]), exit_lag_days=exit_lag, exit_date=exit_date, exit_note=exit_note,
            avoided_share_t1=share(eff1, Pk, Tk, rl), avoided_share_t=share(eff0, Pk, Tk, rl),
            bh_fall=np.exp(rl[Pk + 1:Tk + 1].sum()) - 1, strat_fall_t1=nav1[Tk] / nav1[Pk] - 1,
            pos_at_trough=int(pos[Tk]), entry_lag_days=entry_lag, entry_date=entry_date, entry_note=entry_note,
            missed_63_t1=share(eff1, Tk, e63, rl), missed_63_t=share(eff0, Tk, e63, rl),
            missed_to_next_peak_t1=share(eff1, Tk, Rk, rl),
            missed_to_recovery_t1=share(eff1, Tk, Rec, rl) if Rec is not None else np.nan,
            bh_63=np.exp(rl[Tk + 1:e63 + 1].sum()) - 1, strat_63_t1=nav1[e63] / nav1[Tk] - 1,
            bh_to_next_peak=np.exp(rl[Tk + 1:Rk + 1].sum()) - 1, strat_to_next_peak_t1=nav1[Rk] / nav1[Tk] - 1,
            bh_cycle=navb[Rk] / navb[Pk] - 1, strat_cycle_t1=nav1[Rk] / nav1[Pk] - 1, strat_cycle_t=nav0[Rk] / nav0[Pk] - 1,
            tim_fall=eff1[Pk + 1:Tk + 1].mean(), tim_63=eff1[Tk + 1:e63 + 1].mean(),
        ))
E = pd.DataFrame(rows)
E.to_csv(f"{RES}/A_baseline_episodes.csv", index=False, float_format="%.4f")

pd.set_option("display.width", 250)
cols = ["ep", "peak", "trough", "depth", "fall_days", "pos_at_peak", "exit_lag_days", "exit_note", "avoided_share_t1", "bh_fall",
        "strat_fall_t1", "pos_at_trough", "entry_lag_days", "entry_note", "missed_63_t1", "bh_63", "strat_63_t1",
        "missed_to_next_peak_t1", "bh_cycle", "strat_cycle_t1"]
for v in ["c", "a", "b"]:
    print(f"\n=== вариант {v}: {VARIANTS[v]} ===")
    print(E[E.variant == v][cols].to_string(index=False, float_format=lambda x: f"{x:.2f}"))

# ------------------------------------------------------------- сводка по вариантам
summ = []
for v in VARS:
    s = E[E.variant == v]
    long_at_peak = s[s.pos_at_peak == 1]
    exited = long_at_peak[long_at_peak.exit_lag_days.notna()]
    summ.append(dict(
        variant=v, n_episodes=len(s), n_long_at_peak=len(long_at_peak), n_exited_before_trough=int((long_at_peak.exit_note == "выход до дна").sum()),
        n_never_exited=int((long_at_peak.exit_note == "не выходила").sum()), n_flat_before_peak=int((s.pos_at_peak == 0).sum()),
        exit_lag_mean=exited.exit_lag_days.mean(), exit_lag_median=exited.exit_lag_days.median(),
        exit_lag_mean_all=s.exit_lag_days.mean(),
        avoided_mean=s.avoided_share_t1.mean(), avoided_median=s.avoided_share_t1.median(), avoided_mean_t=s.avoided_share_t.mean(),
        n_flat_at_trough=int((s.pos_at_trough == 0).sum()),
        entry_lag_mean=s[s.pos_at_trough == 0].entry_lag_days.mean(), entry_lag_median=s[s.pos_at_trough == 0].entry_lag_days.median(),
        entry_lag_mean_all=s.entry_lag_days.mean(),
        missed63_mean=s.missed_63_t1.mean(), missed63_median=s.missed_63_t1.median(),
        missed_to_next_peak_mean=s.missed_to_next_peak_t1.mean(), missed_to_recovery_mean=s.missed_to_recovery_t1.mean(),
        cycle_strat_minus_bh_mean=(s.strat_cycle_t1 - s.bh_cycle).mean(), n_cycles_beat_bh=int((s.strat_cycle_t1 > s.bh_cycle).sum()),
    ))
S = pd.DataFrame(summ)
S.to_csv(f"{RES}/A_baseline_episodes_summary.csv", index=False, float_format="%.3f")
print("\n=== сводка своевременности (лаги в торговых днях от решения; доли — exec t+1) ===")
print(S.to_string(index=False, float_format=lambda x: f"{x:.2f}"))

# ------------------------------------------------------------- ложные выходы (флэт-отрезки), exec t+1
fx_rows, fx_detail = [], []
for v in VARS:
    pos = P[f"pos_{v}"]
    eff = pos.shift(2).fillna(0).astype(int)   # позиция, действующая в течение дня t (exec t+1)
    eff = eff.loc[BT_START:]
    runs = []; cur = None
    for d, val in eff.items():
        if val == 0 and cur is None:
            cur = d
        elif val == 1 and cur is not None:
            runs.append((cur, d)); cur = None
    if cur is not None:
        runs.append((cur, eff.index[-1]))
    det = []
    for a, b in runs:
        ia, ib = idx.get_loc(a), idx.get_loc(b)
        seg = slice(ia, ib) if ib > ia else slice(ia, ia + 1)   # дни, когда позиция флэт (доход дня t при eff[t]==0)
        mk = rl[ia:ib].sum() if ib > ia else rl[ia]
        mm_ = rmm[ia:ib].sum() if ib > ia else rmm[ia]
        gain = mm_ - mk - 2 * COST
        sig_day = idx[max(ia - 2, 0)]
        cause = ""
        if v in ("c", "e", "e2", "d", "h", "i"):
            g_ok = P["cell"].get(sig_day) != TOXIC
            s_ok = P["sign_closed"].get(sig_day) == 1
            cause = ("gate" if not g_ok else "") + ("+" if (not g_ok and not s_ok) else "") + ("sign" if not s_ok else "")
        det.append(dict(variant=v, flat_from=str(a.date()), long_again=str(b.date()), days=ib - ia, mcftr_logret=mk, mm_logret=mm_,
                        gain_vs_bh=gain, false_exit=gain < 0, market_rose=mk > 0, cause_at_signal=cause, cell_at_signal=P["cell"].get(sig_day)))
    d = pd.DataFrame(det)
    fx_detail.append(d)
    fx_rows.append(dict(variant=v, n_flat_spells=len(d), n_false=int(d.false_exit.sum()), share_false=d.false_exit.mean(),
                        n_market_rose=int(d.market_rose.sum()), median_days=d.days.median(), mean_days=d.days.mean(),
                        total_gain_true=d.loc[~d.false_exit, "gain_vs_bh"].sum(), total_loss_false=d.loc[d.false_exit, "gain_vs_bh"].sum(),
                        net_gain=d.gain_vs_bh.sum(), worst_false=d.gain_vs_bh.min(), best_true=d.gain_vs_bh.max(),
                        n_false_2010=int(d[(d.flat_from >= "2010") & d.false_exit].shape[0]),
                        n_spells_2010=int(d[d.flat_from >= "2010"].shape[0]),
                        net_gain_2010=d[d.flat_from >= "2010"].gain_vs_bh.sum(),
                        loss_false_2010=d[(d.flat_from >= "2010") & d.false_exit].gain_vs_bh.sum()))
FX = pd.DataFrame(fx_rows)
FXD = pd.concat(fx_detail)
FX.to_csv(f"{RES}/A_baseline_false_exits.csv", index=False, float_format="%.4f")
FXD.to_csv(f"{RES}/A_baseline_flat_spells.csv", index=False, float_format="%.4f")
print("\n=== ложные выходы (флэт-отрезок, где ММ − MCFTR − 2×издержки < 0), лог-доходности, exec t+1 ===")
print(FX.to_string(index=False, float_format=lambda x: f"{x:.3f}"))
print("\n=== c: все флэт-отрезки (лог-доходности) ===")
print(FXD[FXD.variant == "c"].drop(columns=["variant"]).to_string(index=False, float_format=lambda x: f"{x:.3f}"))
c = FXD[FXD.variant == "c"]
print("\nc: по причине НАЧАЛА отрезка:", c.groupby("cause_at_signal").agg(n=("days", "size"), false=("false_exit", "sum"), gain=("gain_vs_bh", "sum"), days=("days", "median")).to_string())

# ------------------------------------------------------------- подневная атрибуция флэта (c): кто именно держал вне рынка
pos_c = P["pos_c"]; eff = pos_c.shift(2).fillna(0).astype(int)
g_ok = (P["cell"] != TOXIC).shift(2); s_ok = (P["sign_closed"] == 1).shift(2)
excess_flat = (np.log1p(P["ret_mm"].fillna(0)) - P["logret_mcftr"].fillna(0))   # выигрыш дня во флэте против лонга
cls = pd.Series(np.where(eff == 1, "лонг", np.where(~g_ok.astype(bool) & ~s_ok.astype(bool), "оба закрыты",
                np.where(~g_ok.astype(bool), "только ворота", "только знак"))), index=P.index)
att = []
for w, (s, e) in [("2004+", (BT_START, BT_END)), ("2010+", ("2010-01-01", BT_END))]:
    m = (P.index >= s) & (P.index <= e) & (eff.values == 0)
    grp = pd.DataFrame({"cls": cls[m], "x": excess_flat[m]}).groupby("cls")["x"].agg(["size", "sum"])
    for k, r in grp.iterrows():
        att.append(dict(window=w, blocker=k, days=int(r["size"]), gain_log=r["sum"], gain_per_year_pp=r["sum"] / (r["size"] / 252) * 100))
ATT = pd.DataFrame(att)
ATT.to_csv(f"{RES}/A_baseline_flat_attribution.csv", index=False, float_format="%.4f")
print("\n=== c: подневная атрибуция флэта — суммарный лог-выигрыш против лонга по блокирующему условию (exec t+1) ===")
print(ATT.to_string(index=False, float_format=lambda x: f"{x:.3f}"))
