"""F2_pm §5: здоровье модели — IC-24 против операционных критериев (скользящая доходность
стратегии к деньгам/b&h за 12–24 мес, CUSUM, доля верных месяцев). Когда бы срабатывали и
предсказывают ли они дальнейший провал. Запуск: python scripts/F2_health.py (после F2_switching.py)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from F2_lib import *
from scipy import stats

d = load_daily()
m = load_monthly()
POS = pd.read_csv(os.path.join(RES, "F2_rules_positions.csv"), index_col=0, parse_dates=True)
sig = POS["PANEL_M: ворота+ядро, месячная оценка"]
bt = backtest(d, sig, 0.002)
bh, mm = bench(d)
MO = pd.DataFrame({"strat": bt["r"].resample("ME").sum(), "bh": bh["r"].resample("ME").sum(),
                   "mm": mm["r"].resample("ME").sum(), "pos": bt["pos"].resample("ME").mean()})
MO = MO[(MO.index >= "2004-01-31") & (MO.index <= "2026-08-31")]
MO["ex_mm"] = MO["strat"] - MO["mm"]
MO["ex_bh"] = MO["strat"] - MO["bh"]
# верное решение месяца: лонг и рынок обогнал деньги, либо флэт и рынок отстал от денег
MO["correct"] = ((MO["pos"] > 0.5) & (MO["bh"] > MO["mm"])) | ((MO["pos"] <= 0.5) & (MO["bh"] <= MO["mm"]))

# IC-24 как в health.py: Спирмен композита к fwd1m по последним 24 закрытым парам
mc = m[m["closed"] & m["composite"].notna() & m["fwd1m_log"].notna()]
ic = []
for i in range(24, len(mc) + 1):
    w = mc.iloc[i - 24:i]
    ic.append((w.index[-1], stats.spearmanr(w["composite"], w["fwd1m_log"])[0]))
IC = pd.Series(dict(ic))
IC = IC[IC.index >= "2004-01-31"]
# сверка с витриной
import json
dj = json.load(open(os.path.join(os.path.dirname(AUDIT), "data.json"), encoding="utf-8"))
hs = pd.Series({pd.Timestamp(a): b for a, b in dj["core"]["health"]["series"]})
j = pd.concat([IC.rename("mine"), hs.rename("site")], axis=1, sort=True).dropna()
print(f"IC-24: сверка с витриной — {len(j)} точек, max|diff|={ (j['mine']-j['site']).abs().max():.4f}; последнее {IC.index[-1].date()} {IC.iloc[-1]:+.3f}")

H = pd.DataFrame(index=MO.index)
H["ic24"] = IC.reindex(MO.index)
H["ex_mm_12"] = MO["ex_mm"].rolling(12).sum() * 100
H["ex_mm_24"] = MO["ex_mm"].rolling(24).sum() * 100
H["ex_bh_24"] = MO["ex_bh"].rolling(24).sum() * 100
H["hit_24"] = MO["correct"].rolling(24).mean()
H["strat_12"] = MO["strat"].rolling(12).sum() * 100
# CUSUM (односторонний вниз) на избытке над деньгами: S = max(0, S - (x - k)), k = допуск
def cusum(x, k, h):
    S = 0.0; out = []; alarms = []
    for t, v in x.items():
        if v != v:
            out.append(np.nan); continue
        S = max(0.0, S - (v - k))
        out.append(S)
        if S > h:
            alarms.append(t); S = 0.0
    return pd.Series(out, index=x.index), alarms
H["cusum_mm"], al_cusum = cusum(MO["ex_mm"] * 100, k=0.0, h=10.0)
H["cusum_bh"], al_cusum_bh = cusum(MO["ex_bh"] * 100, k=-0.5, h=15.0)
H.to_csv(os.path.join(RES, "F2_health_series.csv"))

# алармы (фронт срабатывания)
def edges(cond):
    c = cond.fillna(False).astype(bool)
    return list(c.index[c & ~c.shift(1).fillna(False).astype(bool)])
crit = {
    "IC-24 < 0 (текущий 'dead')": edges(H["ic24"] < 0),
    "IC-24 < 0 шесть мес подряд (регламент)": edges((H["ic24"] < 0).rolling(6).sum() >= 6),
    "избыток над деньгами 12м < 0": edges(H["ex_mm_12"] < 0),
    "избыток над деньгами 24м < 0": edges(H["ex_mm_24"] < 0),
    "избыток над деньгами 24м < −10%": edges(H["ex_mm_24"] < -10),
    "доля верных месяцев 24м < 50%": edges(H["hit_24"] < 0.5),
    "CUSUM над деньгами (k=0, h=10%)": al_cusum,
    "CUSUM над b&h (k=−0.5%/мес, h=15%)": al_cusum_bh,
}
print("\n=== когда срабатывали критерии (фронты), и что было потом: избыток стратегии над деньгами за 12 мес после ===")
rows = []
for nm, dates in crit.items():
    fw = []
    for t in dates:
        i = MO.index.get_loc(t)
        if i + 12 < len(MO):
            fw.append(MO["ex_mm"].iloc[i + 1:i + 13].sum() * 100)
    unc = MO["ex_mm"].rolling(12).sum().shift(-12).dropna() * 100
    rows.append({"criterion": nm, "n_alarms": len(dates), "alarms": ", ".join(str(t.date())[:7] for t in dates),
                 "fwd12_ex_mm_after_alarm_mean%": round(np.mean(fw), 1) if fw else np.nan,
                 "fwd12_ex_mm_unconditional_mean%": round(unc.mean(), 1),
                 "share_alarms_followed_by_neg_12m%": round(np.mean(np.array(fw) < 0) * 100) if fw else np.nan,
                 "months_in_alarm%": round((H[nm.split(" ")[0] if False else "ic24"] < 0).mean() * 100, 1) if nm.startswith("IC-24 < 0 (") else np.nan})
A = pd.DataFrame(rows)
pd.set_option("display.width", 250); pd.set_option("display.max_colwidth", 120)
print(A.to_string(index=False))
A.to_csv(os.path.join(RES, "F2_health_alarms.csv"), index=False)
print(f"\nдоля месяцев со статусом IC-24<0 (2004+): {(H.ic24<0).mean()*100:.0f}%; "
      f"с 12м-избытком<0: {(H.ex_mm_12<0).mean()*100:.0f}%; с 24м-избытком<0: {(H.ex_mm_24<0).mean()*100:.0f}%")
print("\nтекущие значения:")
print(H.tail(3).round(3).to_string())

# связь IC-24 с будущей результативностью: предсказывает ли IC-24 следующий 12м избыток?
j2 = pd.concat([H["ic24"], MO["ex_mm"].rolling(12).sum().shift(-12) * 100, H["hit_24"], H["ex_mm_24"]], axis=1).dropna()
j2.columns = ["ic24", "fwd12", "hit24", "ex24"]
print(f"\nСпирмен(IC-24, избыток стратегии над деньгами в следующие 12м) = {stats.spearmanr(j2.ic24, j2.fwd12)[0]:+.3f} (n={len(j2)}, перекрывающиеся окна — только ориентир)")
print(f"Спирмен(hit-24, следующие 12м) = {stats.spearmanr(j2.hit24, j2.fwd12)[0]:+.3f}; Спирмен(ex24, следующие 12м) = {stats.spearmanr(j2.ex24, j2.fwd12)[0]:+.3f}")

# «что делать при срабатывании»: оверлей — при 24м-избытке<0 экспозиция 0.5 вместо 1
print("\n=== оверлей «здоровье»: при избытке над деньгами за 24 мес < 0 — половина позиции (2010-2026.08) ===")
flag = (H["ex_mm_24"] < 0).astype(float).reindex(d.index, method="ffill").fillna(0.0) > 0.5
flag_ic = (H["ic24"] < 0).astype(float).reindex(d.index, method="ffill").fillna(0.0) > 0.5
for nm, s in [("PANEL_M база", sig), ("PANEL_M × 0.5 при 24м-избытке<0", sig.where(~flag, sig * 0.5)),
              ("PANEL_M × 0.5 при IC-24<0", sig.where(~flag_ic, sig * 0.5)),
              ("PANEL_M × 0 при IC-24<0 (выключать модель)", sig.where(~flag_ic, 0.0))]:
    for a, b in [("2010-01-01", "2026-08-31"), ("2004-01-01", "2026-08-31")]:
        bhw, _ = bench(d, a, b)
        mt = metrics(backtest(d, s, 0.002, a, b), d, bhw)
        print(f"  {nm:45s} {a[:4]}-{b[:4]}: CAGR {mt['CAGR%']:+.2f}% Sharpe {mt['Sharpe']:.2f} MDD {mt['MDD%']:.1f}% in_mkt {mt['in_mkt%']:.0f}%")
print("\nготово: results/F2_health_series.csv, F2_health_alarms.csv")
