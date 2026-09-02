"""F2_pm §1/§3: правило панели при дневном/недельном/месячном наблюдении, число переключений,
цена издержек и пропущенных движений, дебаунс/мин. удержание/гистерезис битов, автомат состояний,
своевременность по эпизодам просадок. Запуск: python scripts/F2_switching.py (из audit/)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from F2_lib import *

d = load_daily()
m = load_monthly()
core = core_sign_daily(d, m)            # знак закрытого месяца (гистерезис ±0.1), протянут по дням
core_pos = (core == 1)
gate = d["gate_open"].astype(bool)
have = d["cell"].notna() & core.notna()
START_EVAL = "2004-01-01"

# ------------------------------------------------------------------ строительные блоки
def sample_hold(sig, freq):
    """Оценка правила только на метках: 'D' — каждый день, 'W' — последний торговый день недели,
    'M' — последний торговый день месяца; между метками позиция держится."""
    s = sig.astype(float)
    if freq == "D":
        return s
    if freq == "W":
        marks = s.groupby([s.index.isocalendar().year, s.index.isocalendar().week]).apply(lambda g: g.index[-1])
    else:
        marks = s.groupby(s.index.to_period("M")).apply(lambda g: g.index[-1])
    out = pd.Series(np.nan, index=s.index)
    out.loc[marks.values] = s.loc[marks.values]
    return out.ffill().fillna(0.0)


def debounce(b, n):
    """Бит переключается, только если новое значение продержалось n дней подряд."""
    v = b.astype(int).values
    out = np.zeros(len(v), dtype=int)
    cur = v[0]; run = 0
    for i in range(len(v)):
        if v[i] != cur:
            run += 1
            if run >= n:
                cur = v[i]; run = 0
        else:
            run = 0
        out[i] = cur
    return pd.Series(out, index=b.index).astype(bool)


def min_hold(sig, h):
    """После смены позиции держать не меньше h торговых дней."""
    v = sig.astype(float).values
    out = np.zeros(len(v)); cur = v[0]; age = h
    for i in range(len(v)):
        if v[i] != cur and age >= h:
            cur = v[i]; age = 0
        out[i] = cur; age += 1
    return pd.Series(out, index=sig.index)


def hyst_bit(x, on_thr, off_thr, on_if_greater):
    """Бит с двумя порогами: включается при x>on (или <on), выключается при x<off (или >off)."""
    xv = x.values; out = np.zeros(len(xv), dtype=int); cur = 0
    for i in range(len(xv)):
        v = xv[i]
        if v != v:
            out[i] = cur; continue
        if on_if_greater:
            if v > on_thr: cur = 1
            elif v < off_thr: cur = 0
        else:
            if v < on_thr: cur = 1
            elif v > off_thr: cur = 0
        out[i] = cur
    return pd.Series(out, index=x.index)


def hyst_gate(bond_off=-0.03, trend_band=0.02, vol_off_q=0.60):
    """Токсичная ячейка на битах с гистерезисом; ворота = НЕ токсичная."""
    rv = d["realized_vol_21"]
    q_on = d["vol_thresh80"]
    q_off = rv.rolling(756, min_periods=252).quantile(vol_off_q)
    vol = pd.Series(0, index=d.index); cur = 0; out = []
    for a, b, c in zip(rv.values, q_on.values, q_off.values):
        if a == a and b == b and c == c:
            if a > b: cur = 1
            elif a < c: cur = 0
        out.append(cur)
    vol = pd.Series(out, index=d.index)
    ratio = d["imoex"] / d["ma200"] - 1
    bull = hyst_bit(ratio, trend_band, -trend_band, True)   # 1 = бык
    bond = hyst_bit(d["rgbi_dd"], -0.04, bond_off, False)   # 1 = стресс
    toxic = (bull == 0) & (vol == 1) & (bond == 1)
    return (~toxic) & d["cell"].notna(), bull, vol, bond


# ------------------------------------------------------------------ набор правил
rules = {}
base = (gate & core_pos).where(have, False)
rules["PANEL_M: ворота+ядро, месячная оценка"] = sample_hold(base, "M")
rules["PANEL_W: ворота+ядро, недельная оценка"] = sample_hold(base, "W")
rules["PANEL_D: ворота(день)+ядро(закр.мес)"] = sample_hold(base, "D")
rules["GATE_D: только ворота (день)"] = gate.where(have, False).astype(float)
rules["GATE_M: только ворота (месяц)"] = sample_hold(gate.where(have, False), "M")
rules["CORE_M: только знак ядра"] = core_pos.where(have, False).astype(float)
for n in (3, 5, 10):
    rules[f"PANEL_D + дебаунс ворот {n}д"] = (debounce(gate.where(have, False), n) & core_pos).where(have, False).astype(float)
for h in (10, 21, 42):
    rules[f"PANEL_D + мин.удержание {h}д"] = min_hold(base.astype(float), h)
for bo, tb, vq in [(-0.03, 0.02, 0.60), (-0.025, 0.03, 0.60), (-0.03, 0.0, 0.80), (-0.03, 0.02, 0.80), (-0.04, 0.0, 0.60)]:
    g2, _, _, _ = hyst_gate(bo, tb, vq)
    rules[f"HYST ворота (bond off {bo*100:.1f}%, тренд ±{tb*100:.0f}%, vol off q{int(vq*100)}) + ядро"] = (g2 & core_pos).where(have, False).astype(float)
g2, _, _, _ = hyst_gate(-0.03, 0.02, 0.60)
sm1 = (debounce(gate.where(have, False), 5) & core_pos).where(have, False)
rules["SM1: дебаунс 5д + мин.удержание 21д"] = min_hold(sm1.astype(float), 21)
sm2 = (debounce(g2, 5) & core_pos).where(have, False)
rules["SM2: HYST(−3%,±2%,q60) + дебаунс 5д + удерж. 21д"] = min_hold(sm2.astype(float), 21)
rules["SM3: HYST(−3%,±2%,q60), недельная оценка"] = sample_hold((g2 & core_pos).where(have, False), "W")
rules["SM4: HYST + недельно + удерж. 21д"] = min_hold(sample_hold((g2 & core_pos).where(have, False), "W"), 21)
# окна входа: покупать шип волы при спокойных ОФЗ независимо от знака ядра
entry = d["cell"].isin(ENTRY)
rules["PANEL_D + окна входа (лонг при vol&bond_ok независимо от ядра)"] = ((gate & core_pos) | entry).where(have, False).astype(float)
N_VARIANTS = len(rules)
print(f"вариантов правил перебрано: {N_VARIANTS}")

# ------------------------------------------------------------------ метрики по окнам
tabs = []
for i, (nm, sig) in enumerate(rules.items()):
    tabs.append(run_windows(d, sig, nm, with_bench=(i == 0)))
T = pd.concat(tabs, ignore_index=True)
cols = ["rule", "window", "CAGR%", "vol%", "Sharpe", "Sharpe_ex_mm", "MDD%", "in_mkt%", "trades/yr",
        "hit_m_vs_mm%", "beat_bh_years%", "n_years", "CAGR_bh%"]
T = T[cols]
T.to_csv(os.path.join(RES, "F2_rules_metrics.csv"), index=False)
pd.set_option("display.width", 260); pd.set_option("display.max_colwidth", 70)
for w in WINDOWS:
    print(f"\n=== окно {w} ===")
    print(T[T.window == w].drop(columns=["window"]).to_string(index=False))

# чувствительность к издержкам
print("\n=== чувствительность к издержкам (окно 2010-2026.08): CAGR / Sharpe при 0.1 / 0.2 / 0.3% ===")
rows = []
for nm in ["PANEL_M: ворота+ядро, месячная оценка", "PANEL_W: ворота+ядро, недельная оценка",
           "PANEL_D: ворота(день)+ядро(закр.мес)", "GATE_D: только ворота (день)",
           "SM2: HYST(−3%,±2%,q60) + дебаунс 5д + удерж. 21д", "SM3: HYST(−3%,±2%,q60), недельная оценка"]:
    r = {"rule": nm}
    for c in (0.001, 0.002, 0.003):
        mt = metrics(backtest(d, rules[nm], c, "2010-01-01", "2026-08-31"), d)
        r[f"CAGR@{c*100:.1f}%"] = mt["CAGR%"]; r[f"Sharpe@{c*100:.1f}%"] = mt["Sharpe"]
    rows.append(r)
CS = pd.DataFrame(rows); print(CS.to_string(index=False))
CS.to_csv(os.path.join(RES, "F2_rules_cost_sensitivity.csv"), index=False)

# ------------------------------------------------------------------ число переключений ворот и правила
print("\n=== переключения: ворота и полное правило при дневном/недельном/месячном наблюдении ===")
rows = []
for a, b in [("2010-01-01", "2026-08-31"), ("2004-01-01", "2026-08-31")]:
    for nm, s in [("ворота", gate.where(have, False)), ("ворота+ядро", base)]:
        for f in ("D", "W", "M"):
            x = sample_hold(s, f)
            x = x[(x.index >= a) & (x.index <= b)]
            sw = x.diff().abs().fillna(0)
            yrs = len(x) / 252
            # длительности закрытых эпизодов
            runs = []
            cur = x.iloc[0]; L = 0
            for v in x.values:
                if v == cur: L += 1
                else:
                    runs.append((cur, L)); cur = v; L = 1
            runs.append((cur, L))
            closed = [L for v, L in runs if v == 0]
            openr = [L for v, L in runs if v == 1]
            rows.append({"window": f"{a[:4]}-{b[:7]}", "signal": nm, "obs": f, "switches": int(sw.sum()),
                         "switches/yr": round(sw.sum() / yrs, 2), "cost/yr% (0.2%)": round(sw.sum() / yrs * 0.2, 2),
                         "flat_episodes": len(closed), "flat<5d": sum(1 for L in closed if L < 5),
                         "flat<10d": sum(1 for L in closed if L < 10), "flat<21d": sum(1 for L in closed if L < 21),
                         "flat_median_days": int(np.median(closed)) if closed else 0,
                         "long_episodes": len(openr), "long<21d": sum(1 for L in openr if L < 21),
                         "long_median_days": int(np.median(openr)) if openr else 0})
SW = pd.DataFrame(rows); print(SW.to_string(index=False))
SW.to_csv(os.path.join(RES, "F2_switch_counts.csv"), index=False)

# пропущенные движения: где дневное и месячное правила расходятся, что заработал индекс
print("\n=== расхождение PANEL_D и PANEL_M (2010-2026.08): доходность MCFTR в дни расхождения ===")
pd_ = rules["PANEL_D: ворота(день)+ядро(закр.мес)"].shift(1).fillna(0)
pm_ = rules["PANEL_M: ворота+ядро, месячная оценка"].shift(1).fillna(0)
w = (d.index >= "2010-01-01") & (d.index <= "2026-08-31")
rl = d["r_long"].fillna(0); rf = d["r_flat"].fillna(0)
onlyD = (pd_ == 1) & (pm_ == 0) & w; onlyM = (pd_ == 0) & (pm_ == 1) & w
print(f"дней 'D в лонге, M во флэте': {int(onlyD.sum())}, избыток над деньгами за них: {((rl-rf)[onlyD].sum())*100:+.1f}% (лог, сумм.)")
print(f"дней 'D во флэте, M в лонге': {int(onlyM.sum())}, избыток над деньгами за них: {((rl-rf)[onlyM].sum())*100:+.1f}% (лог, сумм.) — "
      f"т.е. D избежал {(-(rl-rf)[onlyM].sum())*100:+.1f}%")
print(f"издержки D минус M за окно: {(pd_.diff().abs()[w].sum()-pm_.diff().abs()[w].sum())*0.2:.1f}% (по 0.2%)")

# ------------------------------------------------------------------ своевременность по эпизодам просадок
eps = drawdown_episodes(d["imoex"][d.index >= "2004-01-01"], 0.15)
print(f"\n=== эпизоды просадок IMOEX >15% с 2004: {len(eps)} ===")
TL = []
for nm in ["PANEL_M: ворота+ядро, месячная оценка", "PANEL_W: ворота+ядро, недельная оценка",
           "PANEL_D: ворота(день)+ядро(закр.мес)", "GATE_D: только ворота (день)", "CORE_M: только знак ядра",
           "SM2: HYST(−3%,±2%,q60) + дебаунс 5д + удерж. 21д", "SM3: HYST(−3%,±2%,q60), недельная оценка"]:
    pos = rules[nm].shift(1).fillna(0)
    t = timeliness(pos, d["imoex"], eps); t.insert(0, "rule", nm)
    TL.append(t)
TL = pd.concat(TL, ignore_index=True)
TL.to_csv(os.path.join(RES, "F2_timeliness.csv"), index=False)
for nm in TL.rule.unique():
    print(f"\n--- {nm}")
    print(TL[TL.rule == nm].drop(columns=["rule"]).to_string(index=False))
print("\nсводка по правилам (медианы по эпизодам):")
print(TL.groupby("rule")[["exit_lag_days", "avoided%", "entry_lag_days", "missed%"]].median().round(0).to_string())

# ------------------------------------------------------------------ бутстреп разности Шарпов (осн. окно)
print("\n=== бутстреп разности Шарпов, 2010-2026.08, блок ~126 дн, 1000 повторов ===")
bh, mm = bench(d, "2010-01-01", "2026-08-31")
ref = backtest(d, rules["PANEL_M: ворота+ядро, месячная оценка"], 0.002, "2010-01-01", "2026-08-31")
for nm in ["PANEL_M: ворота+ядро, месячная оценка", "PANEL_D: ворота(день)+ядро(закр.мес)", "GATE_D: только ворота (день)",
           "SM2: HYST(−3%,±2%,q60) + дебаунс 5д + удерж. 21д", "SM3: HYST(−3%,±2%,q60), недельная оценка"]:
    bt = backtest(d, rules[nm], 0.002, "2010-01-01", "2026-08-31")
    o, lo, hi, p = sharpe_diff_bootstrap(bt["r"].values, bh["r"].values)
    line = f"{nm[:52]:52s} vs b&h: ΔSharpe {o:+.2f} [{lo:+.2f}; {hi:+.2f}] P(Δ<=0)={p:.2f}"
    if nm != "PANEL_M: ворота+ядро, месячная оценка":
        o2, lo2, hi2, p2 = sharpe_diff_bootstrap(bt["r"].values, ref["r"].values)
        line += f" | vs PANEL_M: {o2:+.2f} [{lo2:+.2f}; {hi2:+.2f}] P={p2:.2f}"
    print(line)

# сохранить дневные позиции ключевых правил для других скриптов
POS = pd.DataFrame({k: v for k, v in rules.items()})
POS.to_csv(os.path.join(RES, "F2_rules_positions.csv"))
print("\nготово: results/F2_rules_metrics.csv, F2_switch_counts.csv, F2_timeliness.csv, F2_rules_positions.csv")
