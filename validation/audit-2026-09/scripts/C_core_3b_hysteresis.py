"""C_core задача 3b — гистерезис отдельно: сетка порогов 0..1,0; walk-forward выбора порога по прошлому;
плацебо (тот же порог на сигнале без информации — AR(1)-двойник); альтернативы с тем же эффектом
«реже дёргаться»: минимальное удержание, знак средней за k мес; асимметричный порог.
Где именно расходятся позиции при 0,1 и 0,5 и что это дало."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from C_core_lib import *

D, M, C = load()
me = month_end_idx(D)
mk = monthly_market(D, C, me)
Mm = M.reindex(me)
comp = Mm["composite"]
fwd = mk["fwd_imoex"]
gate = (Mm["cell"] != TOXIC).astype(float)
splits = {"A ..2017": ("2004-01-01", "2017-12-31"), "B 2018+": ("2018-01-01", "2026-08-31"), "MAIN": MAIN, "2016-02+": ("2016-02-01", "2026-08-31")}


def run(pos, a, b, cost=0.002):
    return backtest(pos, mk, cost=cost, start=a, end=b)


rows = []
rets = {}
grid = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 1.0]
print("=== сетка гистерезиса (панель = ворота + гист. знак; Шарп / CAGR / MDD / сделок в год / доля времени) ===")
for sname, (a, b) in splits.items():
    line = f"{sname:9s}"
    for h in grid:
        pos = (hysteresis_sign(comp, h) > 0).astype(float) * gate
        df = run(pos, a, b)
        m = metrics(df)
        rets[(sname, h)] = df["ret"]
        rows.append(dict(split=sname, hyst=h, **m))
        line += f" | {h:.1f}: {m['sharpe']:.2f}/{m['cagr']*100:+.0f}%/{m['maxdd']*100:.0f}%/{m['trades_yr']:.1f}/{m['time_in']:.0%}"
    print(line)
pd.DataFrame(rows).to_csv(f"{RES}/C_core_3b_hyst_grid.csv", index=False, float_format="%.4f")

print("\n=== то же без ворот (только ядро) ===")
rows2 = []
for sname, (a, b) in splits.items():
    line = f"{sname:9s}"
    for h in grid:
        pos = (hysteresis_sign(comp, h) > 0).astype(float)
        m = metrics(run(pos, a, b))
        rows2.append(dict(split=sname, hyst=h, **m))
        line += f" | {h:.1f}: {m['sharpe']:.2f}/{m['maxdd']*100:.0f}%"
    print(line)
pd.DataFrame(rows2).to_csv(f"{RES}/C_core_3b_hyst_grid_core.csv", index=False, float_format="%.4f")

print("\n=== бутстреп разности Шарпов: гист h против 0,1 (панель) ===")
for sname in ["MAIN", "B 2018+", "A ..2017"]:
    for h in (0.3, 0.5, 0.7):
        d, p, ci = sharpe_diff_boot(rets[(sname, h)], rets[(sname, 0.1)])
        print(f"{sname:9s} h={h}: ΔSh={d:+.2f} p={p:.2f} ДИ90 [{ci[0]:+.2f},{ci[1]:+.2f}]  (Бонферрони×9: {min(1, p*9):.2f})")

# ---------------------------------------------------------------- walk-forward выбора порога
print("\n=== walk-forward: порог выбирается по max Шарпа панели на расширяющемся прошлом (min 60 мес), применяется на следующий год ===")
years = range(2010, 2027)
wf_ret = []
chosen = {}
for y in years:
    a_hist, b_hist = "2004-01-01", f"{y-1}-12-31"
    best, best_sh = 0.1, -9
    for h in grid:
        pos = (hysteresis_sign(comp, h) > 0).astype(float) * gate
        df = run(pos, a_hist, b_hist)
        if len(df) < 60:
            continue
        sh = metrics(df)["sharpe"]
        if sh > best_sh + 1e-9:
            best, best_sh = h, sh
    chosen[y] = best
    pos = (hysteresis_sign(comp, best) > 0).astype(float) * gate
    df = run(pos, f"{y}-01-01", f"{y}-12-31")
    wf_ret.append(df)
WF = pd.concat(wf_ret)
# сделки на стыках лет считаем заново
WF["trade"] = (WF["pos"] != WF["pos"].shift(1).fillna(0)).astype(int)
WF["ret"] = WF["pos"] * WF["fwd_tr"] + (1 - WF["pos"]) * WF["fwd_mm"] - 0.002 * WF["trade"]
mwf = metrics(WF)
print("выбранные пороги по годам:", ", ".join(f"{y}:{h}" for y, h in chosen.items()))
print(f"WF-результат 2010–2026: {fmt_metrics(mwf)}")
for h in (0.1, 0.5):
    m = metrics(run((hysteresis_sign(comp, h) > 0).astype(float) * gate, MAIN[0], MAIN[1]))
    print(f"фиксированный {h}: Sh={m['sharpe']:.2f} CAGR={m['cagr']*100:+.1f}% MDD={m['maxdd']*100:.1f}%")
d, p, ci = sharpe_diff_boot(WF["ret"], rets[("MAIN", 0.1)])
print(f"ΔSh WF против фикс. 0,1 = {d:+.2f} (p={p:.2f})")

# ---------------------------------------------------------------- плацебо: AR(1)-двойник без информации
print("\n=== плацебо: 300 AR(1)-двойников композита (та же автокорреляция и σ, без связи с рынком): прирост Шарпа от гист 0,5 против 0,1 ===")
rng = np.random.default_rng(7)
m = (comp.index >= MAIN[0]) & (comp.index <= MAIN[1])
c = comp[m]
phi = c.autocorr(1)
sd = c.std()
gains = []
for k in range(300):
    e = rng.normal(0, sd * np.sqrt(1 - phi ** 2), len(c))
    x = np.zeros(len(c))
    x[0] = rng.normal(0, sd)
    for i in range(1, len(c)):
        x[i] = phi * x[i - 1] + e[i]
    fake = pd.Series(x, index=c.index)
    s01 = metrics(run((hysteresis_sign(fake, 0.1) > 0).astype(float) * gate, MAIN[0], MAIN[1]))["sharpe"]
    s05 = metrics(run((hysteresis_sign(fake, 0.5) > 0).astype(float) * gate, MAIN[0], MAIN[1]))["sharpe"]
    gains.append(s05 - s01)
gains = np.array(gains)
obs = metrics(run((hysteresis_sign(comp, 0.5) > 0).astype(float) * gate, MAIN[0], MAIN[1]))["sharpe"] - metrics(run((hysteresis_sign(comp, 0.1) > 0).astype(float) * gate, MAIN[0], MAIN[1]))["sharpe"]
print(f"наблюдаемый прирост ΔSh(0,5−0,1) = {obs:+.2f}; плацебо: среднее {gains.mean():+.2f}, σ {gains.std():.2f}, доля плацебо ≥ наблюдаемого = {(gains >= obs).mean():.3f}")

# ---------------------------------------------------------------- альтернативы «реже дёргаться»
print("\n=== альтернативы: минимальное удержание k мес (знак без гист.), знак скользящей средней композита за k мес, асимметричный порог ===")
rows = []


def min_hold_pos(sig_pos, k):
    """Позиция меняется не чаще, чем раз в k месяцев (после смены — держим k месяцев)."""
    out = sig_pos.copy()
    vals = sig_pos.values.copy()
    last_change = -10 ** 6
    cur = 0.0
    for i in range(len(vals)):
        if np.isnan(vals[i]):
            vals[i] = cur
            continue
        if vals[i] != cur and i - last_change >= k:
            cur = vals[i]
            last_change = i
        vals[i] = cur
    return pd.Series(vals, index=sig_pos.index)


raw_pos = (hysteresis_sign(comp, 0.0) > 0).astype(float)
variants = {}
for k in (2, 3, 4, 6):
    variants[f"мин. удержание {k} мес"] = min_hold_pos(raw_pos, k)
for k in (2, 3, 6):
    variants[f"знак MA{k} композита (гист 0,1)"] = (hysteresis_sign(comp.rolling(k, min_periods=1).mean(), 0.1) > 0).astype(float)
# асимметричные пороги
def asym(enter, exit_):
    out = np.full(len(comp), np.nan)
    s = 0
    for i, v in enumerate(comp.values):
        if np.isfinite(v):
            if s <= 0 and v > enter:
                s = 1
            elif s >= 0 and v < exit_:
                s = -1
        out[i] = s if s else np.nan
    return (pd.Series(out, index=comp.index) > 0).astype(float)
variants["вход >+0,3, выход <−0,1"] = asym(0.3, -0.1)
variants["вход >+0,1, выход <−0,3"] = asym(0.1, -0.3)
variants["вход >+0,5, выход <0"] = asym(0.5, 0.0)
variants["вход >0, выход <−0,5"] = asym(0.0, -0.5)
variants["гист 0,1 (прод)"] = (hysteresis_sign(comp, 0.1) > 0).astype(float)
variants["гист 0,5"] = (hysteresis_sign(comp, 0.5) > 0).astype(float)
for name, pc in variants.items():
    line = f"{name:34s}"
    for sname in ["A ..2017", "B 2018+", "MAIN"]:
        a, b = splits[sname]
        m = metrics(run(pc * gate, a, b))
        rows.append(dict(variant=name, split=sname, **m))
        line += f" | {sname}: Sh={m['sharpe']:.2f} CAGR={m['cagr']*100:+.1f}% MDD={m['maxdd']*100:.0f}% tr/y={m['trades_yr']:.1f}"
    print(line)
pd.DataFrame(rows).to_csv(f"{RES}/C_core_3b_alternatives.csv", index=False, float_format="%.4f")

# ---------------------------------------------------------------- где расходятся 0,1 и 0,5
print("\n=== месяцы MAIN, где позиции панели при гист 0,1 и 0,5 различаются: композит, позиция, доходность MCFTR − mm ===")
p01 = (hysteresis_sign(comp, 0.1) > 0).astype(float) * gate
p05 = (hysteresis_sign(comp, 0.5) > 0).astype(float) * gate
m = (comp.index >= MAIN[0]) & (comp.index <= MAIN[1])
diff = (p01 != p05) & m
tab = pd.DataFrame({"composite": comp, "pos_h01": p01, "pos_h05": p05, "excess_tr": (mk["fwd_tr"] - mk["fwd_mm"])})[diff]
tab["gain_h05"] = (tab["pos_h05"] - tab["pos_h01"]) * tab["excess_tr"]
print(tab.round(3).to_string())
print(f"месяцев с расхождением: {len(tab)}; суммарный выигрыш 0,5 над 0,1 (сумма избыточных доходностей): {tab['gain_h05'].sum()*100:+.1f} п.п.; из них в {tab['gain_h05'].abs().idxmax().date()} {tab['gain_h05'].max()*100:+.1f} п.п.")
tab.to_csv(f"{RES}/C_core_3b_hyst_diff_months.csv", float_format="%.4f")
