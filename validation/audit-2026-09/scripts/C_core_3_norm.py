"""C_core задача 3 — нормировка: окно z 36/48/60/84/120 мес; z по дневным данным (252/504 дн) vs месячным;
без вычитания среднего (raw/σ) и без нормировки вовсе; обрезка ±2/±3/нет; гистерезис 0/0,1/0,2/0,3.
Влияние на IC, Шарп, просадку, число разворотов/год. Split A/B и MAIN."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from C_core_lib import *

D, M, C = load()
me = month_end_idx(D)
mk = monthly_market(D, C, me)
Mm = M.reindex(me)
fwd = mk["fwd_imoex"]
gate = (Mm["cell"] != TOXIC).astype(float)
RAWm = pd.DataFrame({k: Mm["raw_" + k] for k, _ in LEGS})
RAWd = pd.DataFrame({k: D[k] for k, _ in LEGS})
splits = {"A ..2017": ("2004-01-01", "2017-12-31"), "B 2018+": ("2018-01-01", "2026-08-31"), "MAIN": MAIN}


def evaluate(comp, name, group, hyst=0.1, extra=None):
    hs = hysteresis_sign(comp, hyst)
    pos_core = (hs > 0).astype(float)
    pos_panel = pos_core * gate
    out = []
    for sname, (a, b) in splits.items():
        m = (comp.index >= a) & (comp.index <= b)
        r = ic_stats(comp[m], fwd[m], n_boot=600)
        bp = metrics(backtest(pos_panel, mk, start=a, end=b))
        bc = metrics(backtest(pos_core, mk, start=a, end=b))
        h = hs[m]
        flips = (h.diff().abs() > 0).sum() / max(1, h.notna().sum() / 12)
        out.append(dict(group=group, variant=name, split=sname, n=r["n"], ic=r["ic"], p_boot=r["p_boot"], nw_t=r["nw_t"],
                        sharpe_panel=bp.get("sharpe"), shex_panel=bp.get("sharpe_ex"), cagr_panel=bp.get("cagr"), mdd_panel=bp.get("maxdd"),
                        time_in=bp.get("time_in"), trades_yr=bp.get("trades_yr"), hit=bp.get("hit"),
                        sharpe_core=bc.get("sharpe"), mdd_core=bc.get("maxdd"), flips_yr=flips, **(extra or {})))
    return out


def comp_from(z_dict):
    return pd.DataFrame({k: sgn * z_dict[k] for k, sgn in LEGS}).mean(axis=1)


rows = []
# ---------------------------------------------------------- 3а. окно z (месячное)
for w in (36, 48, 60, 84, 120):
    z = {k: zroll(RAWm[k], w, 24, 3.0) for k, _ in LEGS}
    rows += evaluate(comp_from(z), f"z месячный, окно {w}м (min 24)", "окно z")
# расширяющееся окно (min 24)
z = {k: zroll(RAWm[k], 10000, 24, 3.0) for k, _ in LEGS}
rows += evaluate(comp_from(z), "z расширяющееся окно (min 24)", "окно z")

# ---------------------------------------------------------- 3б. z по дневным данным
for wd, mpd in ((252, 126), (504, 252), (1260, 504)):
    zd = {}
    for k, _ in LEGS:
        x = RAWd[k]
        zz = (x - x.rolling(wd, min_periods=mpd).mean()) / x.rolling(wd, min_periods=mpd).std()
        zd[k] = zz.clip(-3, 3).reindex(me)
    rows += evaluate(comp_from(zd), f"z дневной, окно {wd}д (min {mpd})", "дневной z")

# ---------------------------------------------------------- 3в. без вычитания среднего / без нормировки
z_scale = {k: (RAWm[k] / RAWm[k].rolling(60, min_periods=24).std()).clip(-3, 3) for k, _ in LEGS}
rows += evaluate(comp_from(z_scale), "raw/σ60 (без вычитания среднего), clip 3", "центрирование")
z_scale_e = {k: (RAWm[k] / RAWm[k].expanding(24).std()).clip(-3, 3) for k, _ in LEGS}
rows += evaluate(comp_from(z_scale_e), "raw/σ расширяющ. (без вычитания среднего)", "центрирование")
# raw/σ с полной выборкой (in-sample масштаб; только знак — IC от масштаба не зависит между ногами лишь при равных σ)
z_full = {k: (RAWm[k] / RAWm[k].std()).clip(-3, 3) for k, _ in LEGS}
rows += evaluate(comp_from(z_full), "raw/σ полной выборки (in-sample масштаб)", "центрирование")
# смешанный: usd raw/σ (без центрирования), остальные z60
mix = {"usd_mom63": z_scale["usd_mom63"], "slope_10_2": zroll(RAWm["slope_10_2"]), "urals_rub_gap": zroll(RAWm["urals_rub_gap"])}
rows += evaluate(comp_from(mix), "usd raw/σ60 + slope z60 + urals z60", "центрирование")
# демонстрация: только вычитание среднего, без масштаба (среднее ног в разных единицах — некорректно, для справки не делаем)

# ---------------------------------------------------------- 3г. обрезка
for clip in (2.0, 3.0, None):
    z = {k: zroll(RAWm[k], 60, 24, clip) for k, _ in LEGS}
    rows += evaluate(comp_from(z), f"clip ±{clip if clip else 'нет'}", "обрезка")

# ---------------------------------------------------------- 3д. гистерезис
base = Mm["composite"]
for h in (0.0, 0.1, 0.2, 0.3, 0.5):
    rows += evaluate(base, f"гистерезис {h}", "гистерезис", hyst=h)

T = pd.DataFrame(rows)
T.to_csv(f"{RES}/C_core_3_norm.csv", index=False, float_format="%.4f")
for g in T.group.unique():
    print(f"\n=== {g} ===")
    print(f"{'вариант':46s} {'split':8s} {'n':>4s} {'IC':>7s} {'p':>6s} | панель {'Sh':>5s} {'Shex':>5s} {'CAGR':>6s} {'MDD':>6s} {'in':>4s} {'tr/y':>4s} {'hit':>5s} | ядро {'Sh':>5s} {'MDD':>6s} | {'flips/y':>7s}")
    for _, x in T[T.group == g].iterrows():
        print(f"{x.variant:46s} {x.split:8s} {x.n:4d} {x.ic:+.3f} {x.p_boot:6.3f} |        {x.sharpe_panel:5.2f} {x.shex_panel:5.2f} {x.cagr_panel*100:+5.1f}% {x.mdd_panel*100:5.1f}% {x.time_in:4.0%} {x.trades_yr:4.1f} {x.hit:5.2f} |      {x.sharpe_core:5.2f} {x.mdd_core*100:5.1f}% | {x.flips_yr:7.2f}")

# ---------------------------------------------------------- 3е. задержка гистерезиса
print("\n=== гистерезис: задержка разворота относительно чистого знака (месяцы), MAIN ===")
raw_sign = hysteresis_sign(base, 0.0)
for h in (0.1, 0.2, 0.3):
    hs = hysteresis_sign(base, h)
    m = (base.index >= MAIN[0]) & (base.index <= MAIN[1])
    rs, hh = raw_sign[m], hs[m]
    # для каждого разворота чистого знака — через сколько месяцев гистерезисный знак его повторил (если повторил до следующего разворота)
    ch = rs.index[(rs.diff().abs() > 0)]
    lags, missed = [], 0
    for i, d in enumerate(ch):
        nxt = ch[i + 1] if i + 1 < len(ch) else rs.index[-1]
        seg = hh.loc[d:nxt]
        hit = seg[seg == rs.loc[d]]
        if len(hit):
            lags.append(len(hh.loc[d:hit.index[0]]) - 1)
        else:
            missed += 1
    print(f"гист {h}: разворотов чистого знака {len(ch)}, повторено {len(lags)} (ср. задержка {np.mean(lags):.2f} мес, макс {max(lags)}), пропущено (дребезг) {missed}")
