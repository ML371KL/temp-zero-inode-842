"""F2_pm §1/§3: цена «закрытого месяца» у композита. Дневной композит восстанавливается из дневных
ног (z по статистике окна 60 мес, как в ядре: окно = последние 60 месячных меток, включая текущий
день как незакрытый месяц). Сравнение: знак дневного композита против знака закрытого месяца —
сколько дней расходятся, что это стоило, и помогает ли недельная оценка по дневному знаку.
Запуск: python scripts/F2_corelag.py (из audit/)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from F2_lib import *

d = load_daily()
m = load_monthly()
LEGS = [("usd_mom63", +1), ("slope_10_2", +1), ("urals_rub_gap", -1)]
# месячные метки (последний торговый день месяца) и их индексы в дневной панели
midx = m.index
comp_daily = pd.Series(np.nan, index=d.index)
mvals = {leg: m[f"raw_{leg}"] for leg, _ in LEGS}
# для каждого дня: окно = 59 предыдущих закрытых месячных значений + текущее дневное
prev_month_end = pd.Series(midx, index=midx).reindex(d.index).ffill()  # последняя месячная метка <= t (в т.ч. сегодня, если t — метка)
# метка "закрытого" месяца для дня t: если t сама месячная метка — она закрыта; иначе предыдущая
out = np.full(len(d), np.nan)
dates = d.index
mpos = {t: i for i, t in enumerate(midx)}
for i, t in enumerate(dates):
    if t < pd.Timestamp("2004-01-01"):
        continue
    pm = prev_month_end.iloc[i]
    if pm != pm:
        continue
    k = mpos[pm]
    # если t — сама метка, окно = m[k-59..k]; иначе окно = m[k-58..k] + сегодняшнее значение
    s, n = 0.0, 0
    for leg, sgn in LEGS:
        x = d[leg].iloc[i]
        if x != x:
            continue
        if t == pm:
            w = mvals[leg].iloc[max(0, k - 59):k + 1].dropna()
        else:
            w = pd.concat([mvals[leg].iloc[max(0, k - 58):k + 1], pd.Series([x])]).dropna()
        if len(w) < 24:
            continue
        z = (x - w.mean()) / w.std(ddof=1)
        z = max(-3.0, min(3.0, z))
        s += sgn * z; n += 1
    if n:
        out[i] = s / n
comp_daily = pd.Series(out, index=dates)
# сверка на месячных метках с композитом ядра
chk = pd.concat([comp_daily.reindex(midx), m["composite"]], axis=1).dropna()
chk = chk[chk.index >= "2004-01-01"]
print(f"сверка дневного композита с ядром на месячных метках: n={len(chk)}, max|diff|={(chk.iloc[:,0]-chk.iloc[:,1]).abs().max():.2e}")

sign_daily = pd.Series(hysteresis_sign(comp_daily.values, 0.10), index=dates)
sign_closed = core_sign_daily(d, m)
have = d["cell"].notna() & sign_daily.notna() & sign_closed.notna() & (d.index >= "2004-01-01")
dif = (sign_daily != sign_closed) & have
print(f"дней, когда дневной знак отличается от знака закрытого месяца: {int(dif.sum())} из {int(have.sum())} ({dif.sum()/have.sum()*100:.1f}%)")
gate = d["gate_open"].astype(bool)


def sample_hold(sig, freq):
    s = sig.astype(float)
    if freq == "D":
        return s
    if freq == "W":
        marks = s.groupby([s.index.isocalendar().year, s.index.isocalendar().week]).apply(lambda g: g.index[-1])
    else:
        marks = s.groupby(s.index.to_period("M")).apply(lambda g: g.index[-1])
    o = pd.Series(np.nan, index=s.index); o.loc[marks.values] = s.loc[marks.values]
    return o.ffill().fillna(0.0)


rules = {
    "закрытый месяц, месячная оценка (PANEL_M)": sample_hold((gate & (sign_closed == 1)).where(have, False), "M"),
    "закрытый месяц, недельная оценка (PANEL_W)": sample_hold((gate & (sign_closed == 1)).where(have, False), "W"),
    "дневной знак ядра, недельная оценка": sample_hold((gate & (sign_daily == 1)).where(have, False), "W"),
    "дневной знак ядра, дневная оценка": (gate & (sign_daily == 1)).where(have, False).astype(float),
    "дневной знак ядра, месячная оценка": sample_hold((gate & (sign_daily == 1)).where(have, False), "M"),
    "только дневной знак ядра (без ворот), недельно": sample_hold((sign_daily == 1).where(have, False), "W"),
}
tabs = [run_windows(d, s, nm, with_bench=(i == 0)) for i, (nm, s) in enumerate(rules.items())]
T = pd.concat(tabs, ignore_index=True)[["rule", "window", "CAGR%", "vol%", "Sharpe", "MDD%", "in_mkt%", "trades/yr", "beat_bh_years%"]]
pd.set_option("display.width", 250)
for w in ["2010-2026.08 (осн.)", "2004+ (полная)", "2025-2026.08", "ex-2022 (2010-26 без 2022)"]:
    print(f"\n=== {w} ===")
    print(T[T.window == w].drop(columns=["window"]).to_string(index=False))
T.to_csv(os.path.join(RES, "F2_corelag_metrics.csv"), index=False)
# сколько торговых дней в среднем дневной знак опережает месячный на разворотах
sw_d = sign_daily[have].diff().fillna(0) != 0
sw_c = sign_closed[have].diff().fillna(0) != 0
print(f"\nсмен знака 2004+: дневной {int(sw_d.sum())}, закрытый месяц {int(sw_c.sum())}")
comp_daily.to_csv(os.path.join(RES, "F2_corelag_composite_daily.csv"), header=["composite_daily"])
print("текущее: дневной композит", round(comp_daily.iloc[-1], 3), "знак", sign_daily.iloc[-1], "| закрытый", sign_closed.iloc[-1])
print("готово: results/F2_corelag_metrics.csv, F2_corelag_composite_daily.csv")
