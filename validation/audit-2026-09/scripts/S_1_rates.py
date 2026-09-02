"""S_1 — атака на находку 1 (D2): доп. выход при Δ21(y1 − ключ) > +0,25 п.п.

(а) заглядывание: ключ по дате вступления vs по дате решения; что реально триггерит бит;
(б) вола-прокси: конкуренты (st_vol, RVI, вола RGBI, RGBI Δ21, y1 Δ21 без ключа, двусторонний бит);
(в) концентрация: leave-one-episode-out, вклад по годам;
(г) дневная каденция;
(д) чувствительность порог × окно, издержки;
(е) плацебо: марковские маски внутри вола-состояний;
(ж) после снятия бита: цена позднего входа.
"""
import sys
sys.path.insert(0, "scripts")
import numpy as np
import pandas as pd
from S_lib import *

d = load_daily()
m = load_monthly()
idx = d.index
me = month_end_mask(idx)
me_dates = idx[me]

# ---------------------------------------------------------------- база
dec_base = panel_decision_monthly(d, m)                   # решение на срезах
pos_base = decision_to_daily(dec_base, idx)
W = ("2015-01-01", "2026-08-31")
bt_base = run_daily(pos_base, d, start=W[0], end=W[1])
mt_base = metrics(bt_base, "эталон 2015-26")
mr_base = monthly_returns(bt_base)
print("ЭТАЛОН 2015-26:", fmt_row(mt_base))

# ключ по дате решения
cb = pd.read_csv("data/cb_decisions.csv", parse_dates=["date"]).set_index("date")["new_rate"]
key_eff = d["key_rate"]
key_dec = cb.reindex(idx.union(cb.index)).ffill().reindex(idx)
key_dec[idx < cb.index[0]] = key_eff[idx < cb.index[0]]
key_dec = key_dec.where(key_eff.notna())
d["key_dec"] = key_dec
d["spread_eff"] = d["y1"] - key_eff
d["spread_dec"] = d["y1"] - key_dec


def bit_from(spread, L=21, thr=0.25, two_sided=False, neg=False):
    dv = spread - spread.shift(L)
    if two_sided:
        b = dv.abs() > thr
    elif neg:
        b = dv < -thr
    else:
        b = dv > thr
    b = b.astype(float)
    b[dv.isna()] = np.nan
    return b


def apply_exit_monthly(bit):
    """Доп. выход поверх эталона на месячной каденции: бит без данных = 0."""
    b = bit.reindex(me_dates).fillna(0)
    dec = dec_base.copy()
    dec = dec.where(~(b == 1), 0.0)
    dec[dec_base.isna()] = np.nan
    return decision_to_daily(dec, idx)


def apply_exit_daily(bit, reentry="daily"):
    """Дневная каденция: выход в любой день, когда бит горит; вход — когда снялся
    (reentry='daily') или только на срезе месяца ('monthly')."""
    b = bit.reindex(idx).fillna(0).values
    base = pos_base.values
    out = np.full(len(idx), np.nan)
    cur = np.nan
    is_me = me.values
    for i in range(len(idx)):
        if np.isnan(base[i]):
            out[i] = np.nan
            continue
        want = base[i] * (1 - b[i])
        if np.isnan(cur):
            cur = want
        elif want < cur:           # выход — любым днём
            cur = want
        elif want > cur:           # вход
            if reentry == "daily" or is_me[i]:
                cur = want
        out[i] = cur
    return pd.Series(out, index=idx)


def evalpos(pos, name, start=W[0], end=W[1], cost=COST):
    bt = run_daily(pos, d, start=start, end=end, cost=cost)
    mt = metrics(bt, name)
    mr = monthly_returns(bt)
    return bt, mt, mr


def line(mt, extra=""):
    return "%-58s CAGR %5.1f%% Sh %4.2f ex %5.2f MDDm %6.1f%% MDDd %6.1f%% in %5.1f%% tr/y %4.2f beat %3.0f%% %s" % (
        mt["name"], mt["cagr"] * 100, mt["sharpe"], mt["sharpe_ex"], mt["mdd_monthly"] * 100,
        mt["mdd_daily"] * 100, mt["in_market"] * 100, mt["trades_py"], mt["years_beat_bh"] * 100, extra)


results = []


def record(pos, name, fam, start=W[0], end=W[1], cost=COST, boot=True):
    bt, mt, mr = evalpos(pos, name, start, end, cost)
    base_bt = run_daily(pos_base, d, start=start, end=end, cost=cost)
    mrb = monthly_returns(base_bt)
    j = mr.join(mrb, lsuffix="_x", rsuffix="_b").dropna()
    dsh, p, lo, hi = sharpe_diff_boot(j["strat_x"], j["strat_b"]) if boot else (np.nan,) * 4
    row = dict(fam=fam, **mt, d_sharpe=dsh, p_le0=p, ci90_lo=lo, ci90_hi=hi, window=f"{start}..{end}", cost=cost)
    results.append(row)
    print(line(mt, "ΔSh %+.2f p %.3f" % (dsh, p) if boot else ""))
    return bt, mt, mr


# ================================================================ (а) ключ и триггер
print("\n=== (а) ДАТИРОВКА КЛЮЧА И ЧТО ТРИГГЕРИТ БИТ ===")
bit_eff = bit_from(d["spread_eff"])
bit_dec = bit_from(d["spread_dec"])
diff_days = int(((bit_eff != bit_dec) & bit_eff.notna() & bit_dec.notna()).sum())
diff_me = int(((bit_eff != bit_dec) & bit_eff.notna() & bit_dec.notna())[me].sum())
print("бит(ключ по вступлению) ≠ бит(ключ по решению): дней %d из %d; на срезах месяца %d из %d" % (
    diff_days, int(bit_eff.notna().sum()), diff_me, int(bit_eff[me].notna().sum())))
pos_eff = apply_exit_monthly(bit_eff)
pos_dec = apply_exit_monthly(bit_dec)
record(pos_base, "эталон", "0")
bt_eff, mt_eff, mr_eff = record(pos_eff, "Δ21(y1−ключ по вступлению) > 0,25 [D2]", "A")
bt_dec, mt_dec, mr_dec = record(pos_dec, "Δ21(y1−ключ по дате решения) > 0,25", "A")

# разложение срабатываний на срезах: Δy1 и Δключ в окне
sp = d["spread_dec"]
dy1 = d["y1"] - d["y1"].shift(21)
dk = key_dec - key_dec.shift(21)
dsp = sp - sp.shift(21)
fire = (dsp > 0.25)
tbl = pd.DataFrame({"fire": fire[me], "dy1": dy1[me], "dkey": dk[me], "dspread": dsp[me],
                    "base_long": dec_base.reindex(me_dates)}).dropna()
tbl["key_move"] = np.select([tbl["dkey"] > 0, tbl["dkey"] < 0], ["повышение", "снижение"], "без изменений")
act = tbl[(tbl["fire"]) & (tbl["base_long"] == 1)]
print("срезов с горящим битом:", int(tbl["fire"].sum()), "из", len(tbl), "; из них эталон был в лонге (бит менял позицию):", len(act))
print("состав активных срабатываний по движению ключа в окне 21 дн:")
print(act.groupby("key_move").agg(n=("fire", "size"), dy1_mean=("dy1", "mean"), dkey_mean=("dkey", "mean")).to_string())
print("все срабатывания (включая эталон во флэте):")
print(tbl[tbl["fire"]].groupby("key_move").agg(n=("fire", "size"), dy1_mean=("dy1", "mean"), dkey_mean=("dkey", "mean")).to_string())
act.to_csv("results/S_1_fires_monthend.csv")

# гипотеза «это просто выход после решения ЦБ»
key_chg21 = ((key_dec != key_dec.shift(21)) & key_dec.notna()).astype(float)
hike21 = ((key_dec > key_dec.shift(21))).astype(float)
cut21 = ((key_dec < key_dec.shift(21))).astype(float)
record(apply_exit_monthly(hike21), "выход: ключ ПОВЫШЕН за 21 дн", "A_alt")
record(apply_exit_monthly(cut21), "выход: ключ СНИЖЕН за 21 дн", "A_alt")
record(apply_exit_monthly(key_chg21), "выход: ключ ИЗМЕНЁН за 21 дн", "A_alt")
# бит без ключа
bit_y1 = bit_from(d["y1"])
record(apply_exit_monthly(bit_y1), "выход: Δ21 y1 > 0,25 (без ключа)", "A_alt")
# бит «y1 упала меньше, чем снижен ключ» = срабатывания при снижении
bit_cutlag = ((cut21 == 1) & (dsp > 0.25)).astype(float)
record(apply_exit_monthly(bit_cutlag), "выход: бит И ключ снижен (y1 отстаёт от снижения)", "A_alt")
bit_nokey = ((dk == 0) & (dsp > 0.25)).astype(float)
record(apply_exit_monthly(bit_nokey), "выход: бит И ключ не менялся (чистый рост y1)", "A_alt")

# ================================================================ (б) вола-прокси
print("\n=== (б) ВОЛА-ПРОКСИ И КОНКУРЕНТЫ ===")
rg = d["rgbi"]
rg_ret = np.log(rg).diff()
d["rgbi_vol21"] = rg_ret.rolling(21).std() * np.sqrt(252)
d["rgbi_vol_thr80"] = d["rgbi_vol21"].rolling(756, min_periods=252).quantile(0.8)
d["rgbi_d21"] = np.log(rg / rg.shift(21))
d["rvi_d21"] = d["rvi"] - d["rvi"].shift(21)
d["y2_d21"] = d["y2"] - d["y2"].shift(21)
d["y10_d21"] = d["y10"] - d["y10"].shift(21)
comp_list = [
    ("выход: st_vol = 1 (бит панели)", d["st_vol"]),
    ("выход: st_bond = 1 (бит панели)", d["st_bond"]),
    ("выход: RVI > 30", (d["rvi"] > 30).astype(float)),
    ("выход: RVI > 35", (d["rvi"] > 35).astype(float)),
    ("выход: RVI Δ21 > +5", (d["rvi_d21"] > 5).astype(float)),
    ("выход: вола RGBI 21д > 80-й перц. 756д", (d["rgbi_vol21"] > d["rgbi_vol_thr80"]).astype(float)),
    ("выход: RGBI Δ21 < −1%", (d["rgbi_d21"] < -0.01).astype(float)),
    ("выход: RGBI Δ21 < −2%", (d["rgbi_d21"] < -0.02).astype(float)),
    ("выход: RGBI Δ21 < −3%", (d["rgbi_d21"] < -0.03).astype(float)),
    ("выход: Δ21 y2 > 0,25", (d["y2_d21"] > 0.25).astype(float)),
    ("выход: Δ21 y10 > 0,25", (d["y10_d21"] > 0.25).astype(float)),
    ("выход: |Δ21(y1−ключ)| > 0,25 (ДВУСТОРОННИЙ)", bit_from(d["spread_dec"], two_sided=True)),
    ("выход: Δ21(y1−ключ) < −0,25 (ОБРАТНЫЙ знак)", bit_from(d["spread_dec"], neg=True)),
    ("выход: бит И st_vol=0 (бит вне вола-стресса)", ((bit_dec == 1) & (d["st_vol"] == 0)).astype(float)),
    ("выход: бит И st_vol=1", ((bit_dec == 1) & (d["st_vol"] == 1)).astype(float)),
    ("выход: бит И RGBI Δ21 ≥ −1% (бит без распродажи ОФЗ)", ((bit_dec == 1) & (d["rgbi_d21"] >= -0.01)).astype(float)),
    ("выход: бит И RGBI Δ21 < −1%", ((bit_dec == 1) & (d["rgbi_d21"] < -0.01)).astype(float)),
]
for name, b in comp_list:
    record(apply_exit_monthly(b), name, "B")

# что происходит в дни/срезы срабатывания: вола, RVI, RGBI
st = pd.DataFrame({"bit": bit_dec, "rv": d["realized_vol_21"], "rvi": d["rvi"], "rgbi_d21": d["rgbi_d21"],
                   "st_vol": d["st_vol"], "st_bond": d["st_bond"], "rgbi_vol": d["rgbi_vol21"]}).dropna()
st = st.loc["2015":]
print("условные средние на ДНЯХ 2015+: бит=1 против бит=0")
print(st.groupby("bit").agg(n=("rv", "size"), rv=("rv", "mean"), rvi=("rvi", "mean"), rgbi_d21=("rgbi_d21", "mean"),
                            p_stvol=("st_vol", "mean"), p_bond=("st_bond", "mean"), rgbi_vol=("rgbi_vol", "mean")).to_string())
print("корреляции дневных Δ21(y1−ключ) с RGBI Δ21: %.2f; с Δ21 RVI: %.2f; с ret21 MCFTR: %.2f" % (
    dsp.corr(d["rgbi_d21"]), dsp.corr(d["rvi_d21"]), dsp.corr(np.log(d["mcftr"] / d["mcftr"].shift(21)))))

# IC бита/Δспреда к fwd 1м на месячной выборке
fwd1 = np.log(d["mcftr"].reindex(me_dates).shift(-1) / d["mcftr"].reindex(me_dates))
for nm, s in [("Δ21(y1−ключ)", dsp[me]), ("−Δ21 RGBI", -d["rgbi_d21"][me]), ("RVI", d["rvi"][me])]:
    rho, t, n = spearman_nw(-s.loc["2015":], fwd1.loc["2015":], lag=1)
    print("IC (−%s → fwd1m MCFTR) 2015+: %.3f (NW t %.2f, n %d)" % (nm, rho, t, n))

# ================================================================ (в) концентрация
print("\n=== (в) КОНЦЕНТРАЦИЯ: LEAVE-ONE-EPISODE-OUT И ВКЛАД ПО ГОДАМ ===")
b_me = bit_dec.reindex(me_dates).fillna(0)
changed = ((dec_base == 1) & (b_me == 1)).astype(int)   # срезы, где бит перевёл во флэт
# эпизоды = максимальные серии подряд идущих таких срезов
ep_id = (changed.diff().fillna(changed) == 1).cumsum() * changed
episodes = [g.index for k, g in changed[changed == 1].groupby(ep_id[changed == 1])]
print("эпизодов вмешательства бита (месячная каденция):", len(episodes))
rows = []
j_full = mr_dec.join(mr_base, lsuffix="_x", rsuffix="_b").dropna()
sh_full = sharpe(j_full["strat_x"])
for k, ep in enumerate(episodes):
    dec_k = dec_base.copy()
    keep = b_me.copy()
    keep.loc[ep] = 0                                  # восстанавливаем эталон в этом эпизоде
    dec_k = dec_k.where(~(keep == 1), 0.0)
    dec_k[dec_base.isna()] = np.nan
    pos_k = decision_to_daily(dec_k, idx)
    bt_k = run_daily(pos_k, d, start=W[0], end=W[1])
    mt_k = metrics(bt_k)
    # что дал этот эпизод: доходность MCFTR минус кэш за месяцы, где бит держал флэт
    months = [pd.Period(x, "M") + 1 for x in ep]     # позиция действует в следующем месяце
    mrb = mr_base.reindex(months).dropna()
    avoided = float(((1 + mrb["bh"]).prod() - (1 + mrb["cash"]).prod()) * 100)
    rows.append(dict(episode=k + 1, first=ep[0].date(), last=ep[-1].date(), n_months=len(ep),
                     mcftr_minus_cash_pct=avoided, sharpe_without=mt_k["sharpe"], mdd_m_without=mt_k["mdd_monthly"],
                     mdd_d_without=mt_k["mdd_daily"]))
loeo = pd.DataFrame(rows)
loeo.to_csv("results/S_1_loeo.csv", index=False)
print("Шарп полного правила %.2f; эталон %.2f" % (sh_full, mt_base["sharpe"]))
print(loeo.to_string(index=False, float_format=lambda v: f"{v:.2f}"))
print("LOEO: min Шарп без одного эпизода %.2f, медиана %.2f; эпизодов, без которых Шарп < 1,5: %d" % (
    loeo["sharpe_without"].min(), loeo["sharpe_without"].median(), int((loeo["sharpe_without"] < 1.5).sum())))
# вклад по годам: разность месячных доходностей правило − эталон
diffm = (j_full["strat_x"] - j_full["strat_b"]) * 100
by_year = diffm.groupby(diffm.index.year).agg(["sum", "count"])
by_year["n_active"] = changed.groupby(changed.index.year).sum().reindex(by_year.index).fillna(0).astype(int)
print("вклад по годам (п.п. к эталону, число месяцев, где бит менял позицию):")
print(by_year.to_string(float_format=lambda v: f"{v:.1f}"))
by_year.to_csv("results/S_1_by_year.csv")
# без 2020, без 2022, без 2025-26
for lab, yrs in [("без 2020", [2020]), ("без 2022", [2022]), ("без 2025-26", [2025, 2026]), ("без 2020 и 2022", [2020, 2022]),
                 ("без 2020, 2022, 2025-26", [2020, 2022, 2025, 2026])]:
    keepm = ~j_full.index.year.isin(yrs)
    print("  %-25s Шарп правило %.2f / эталон %.2f (n=%d)" % (lab, sharpe(j_full.loc[keepm, "strat_x"]), sharpe(j_full.loc[keepm, "strat_b"]), keepm.sum()))
# сплиты
for lab, (a, b) in [("2015-2019", ("2015-01-01", "2019-12-31")), ("2020-2026", ("2020-01-01", "2026-08-31")),
                    ("2018-2026", ("2018-01-01", "2026-08-31")), ("2022-03-2024", WINDOWS["2022-03-2024"]),
                    ("2025-2026", WINDOWS["2025-2026"]), ("2015-2021", ("2015-01-01", "2021-12-31"))]:
    record(pos_dec, f"бит [сплит {lab}]", "C_split", start=a, end=b)
    record(pos_base, f"эталон [сплит {lab}]", "C_split", start=a, end=b, boot=False)

# ================================================================ (г) дневная каденция
print("\n=== (г) ДНЕВНАЯ КАДЕНЦИЯ ===")
record(apply_exit_daily(bit_dec, "daily"), "бит: выход любым днём, вход при снятии любым днём", "D")
record(apply_exit_daily(bit_dec, "monthly"), "бит: выход любым днём, вход только на срезе", "D")
# эталон с дневными воротами (для честного сравнения)
dec_gate_d = ((d["toxic"] == 0) & (hysteresis_sign(m["composite"], 0.1).reindex(idx).ffill().shift(0) > 0))
# знак закрытого месяца: сдвиг на день после среза
sgn_m = hysteresis_sign(m["composite"], 0.1)
sgn_daily = sgn_m.reindex(idx).ffill()
pos_gate_daily = ((d["toxic"] == 0) & (sgn_daily > 0)).astype(float)
pos_gate_daily[d["toxic"].isna()] = np.nan
record(pos_gate_daily, "эталон, ворота ежедневно (знак — закрытый месяц)", "D", boot=True)
pos_gd_bit = pos_gate_daily.where(~(bit_dec.reindex(idx).fillna(0) == 1), 0.0)
record(pos_gd_bit, "ворота ежедневно + бит ежедневно", "D")

# ================================================================ (д) чувствительность
print("\n=== (д) ЧУВСТВИТЕЛЬНОСТЬ: ПОРОГ × ОКНО, ИЗДЕРЖКИ ===")
grid = []
for L in [5, 10, 15, 21, 30, 42, 63]:
    for thr in [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.50, 0.75, 1.0]:
        b = bit_from(d["spread_dec"], L, thr)
        pos = apply_exit_monthly(b)
        bt = run_daily(pos, d, start=W[0], end=W[1])
        mt = metrics(bt)
        mr = monthly_returns(bt)
        # ex-2022 и сплиты
        j = mr.join(mr_base, lsuffix="_x", rsuffix="_b").dropna()
        k22 = j.index.year != 2022
        grid.append(dict(L=L, thr=thr, sharpe=mt["sharpe"], mdd_m=mt["mdd_monthly"], mdd_d=mt["mdd_daily"],
                         in_market=mt["in_market"], trades=mt["trades_py"], cagr=mt["cagr"],
                         sharpe_ex2022=sharpe(j.loc[k22, "strat_x"]),
                         sharpe_2015_19=sharpe(j.loc[j.index.year <= 2019, "strat_x"]),
                         sharpe_2020_26=sharpe(j.loc[j.index.year >= 2020, "strat_x"])))
grid = pd.DataFrame(grid)
grid.to_csv("results/S_1_grid.csv", index=False)
print("Шарп по сетке (строки — окно L, столбцы — порог):")
print(grid.pivot(index="L", columns="thr", values="sharpe").to_string(float_format=lambda v: f"{v:.2f}"))
print("MDD мес. по сетке:")
print(grid.pivot(index="L", columns="thr", values="mdd_m").to_string(float_format=lambda v: f"{v * 100:.1f}"))
print("доля в рынке по сетке:")
print(grid.pivot(index="L", columns="thr", values="in_market").to_string(float_format=lambda v: f"{v * 100:.0f}"))
print("ex-2022 Шарп по сетке (эталон ex-2022 = %.2f):" % sharpe(j_full.loc[j_full.index.year != 2022, "strat_b"]))
print(grid.pivot(index="L", columns="thr", values="sharpe_ex2022").to_string(float_format=lambda v: f"{v:.2f}"))
for c in [0.001, 0.002, 0.003, 0.005]:
    record(pos_dec, f"бит, издержки {c * 100:.1f}%", "E_cost", cost=c, boot=False)
    record(pos_base, f"эталон, издержки {c * 100:.1f}%", "E_cost", cost=c, boot=False)

# walk-forward порога/окна (расширяющееся окно, выбор по прошлому Шарпу, ежегодно с 2018)
print("без денежной ноги (флэт = 0): проверка, что это тайминг, а не карри")
d0 = d.copy(); d0["mm_rate"] = 0.0
for nm, pp in [("эталон, флэт=0", pos_base), ("бит, флэт=0", pos_dec)]:
    bt0 = run_daily(pp, d0, start=W[0], end=W[1]); mt0 = metrics(bt0, nm); print(line(mt0))
    results.append(dict(fam="E_nocash", **mt0, window=f"{W[0]}..{W[1]}", cost=COST))
print("walk-forward выбора (L, thr) по сетке %d вариантов, OOS 2018–2026:" % len(grid))
cands = [(L, thr) for L in [10, 21, 42] for thr in [0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.50]]
mr_c = {}
for L, thr in cands:
    b = bit_from(d["spread_dec"], L, thr)
    bt = run_daily(apply_exit_monthly(b), d, start=W[0], end=W[1])
    mr_c[(L, thr)] = monthly_returns(bt)["strat"]
oos = []
choices = []
for y in range(2018, 2027):
    best, best_sh = None, -9
    for k, s in mr_c.items():
        past = s[s.index.year < y]
        if len(past) < 24:
            continue
        v = sharpe(past)
        if v > best_sh:
            best, best_sh = k, v
    choices.append((y, best, round(best_sh, 2)))
    oos.append(mr_c[best][mr_c[best].index.year == y])
oos = pd.concat(oos)
base_oos = mr_base["strat"][mr_base.index.year >= 2018]
j = pd.concat([oos, base_oos], axis=1, keys=["wf", "base"]).dropna()
dsh, p, lo, hi = sharpe_diff_boot(j["wf"], j["base"])
eq = (1 + j).cumprod()
print("  выборы по годам:", choices)
print("  OOS 2018-26: Шарп WF %.2f против эталона %.2f; ΔSh %+.2f (p %.3f, ДИ90 %+.2f…%+.2f); MDDm WF %.1f%% / эталон %.1f%%" % (
    sharpe(j["wf"]), sharpe(j["base"]), dsh, p, lo, hi, mdd(eq["wf"]) * 100, mdd(eq["base"]) * 100))

# ================================================================ (е) плацебо
print("\n=== (е) ПЛАЦЕБО: МАРКОВСКИЕ МАСКИ ВНУТРИ ВОЛА-СОСТОЯНИЙ ===")
seg = d.loc["2015-02-01":"2026-08-31"]
b_obs = bit_dec.loc[seg.index].fillna(0).values.astype(int)


def markov_placebo(states, b, n_sim, seed, label):
    """Оценка P(b_t | b_{t-1}, state_t) и симуляция масок той же структуры."""
    S = int(np.nanmax(states)) + 1
    st_ = np.nan_to_num(states, nan=0).astype(int)
    cnt = np.zeros((S, 2, 2))
    for i in range(1, len(b)):
        cnt[st_[i], b[i - 1], b[i]] += 1
    P = (cnt[:, :, 1] + 0.5) / (cnt.sum(axis=2) + 1.0)
    rng = np.random.default_rng(seed)
    out = []
    for s in range(n_sim):
        bb = np.zeros(len(b), dtype=int)
        bb[0] = b[0]
        u = rng.random(len(b))
        for i in range(1, len(b)):
            bb[i] = 1 if u[i] < P[st_[i], bb[i - 1]] else 0
        mask = pd.Series(bb, index=seg.index, dtype=float)
        pos = apply_exit_monthly(mask)
        bt = run_daily(pos, d, start=W[0], end=W[1])
        mt = metrics(bt)
        out.append(dict(sharpe=mt["sharpe"], mdd_m=mt["mdd_monthly"], in_market=mt["in_market"], share_on=bb.mean()))
    out = pd.DataFrame(out)
    obs_sh = mt_dec["sharpe"]
    print("  %-52s плацебо Шарп: медиана %.2f, 95-й перц. %.2f, макс %.2f; p(≥%.2f) = %.3f; MDDm медиана %.1f%%; в рынке медиана %.0f%% (набл. %.0f%%); доля бит=1 %.3f (набл. %.3f)" % (
        label, out["sharpe"].median(), out["sharpe"].quantile(0.95), out["sharpe"].max(), obs_sh,
        (out["sharpe"] >= obs_sh).mean(), out["mdd_m"].median() * 100, out["in_market"].median() * 100,
        mt_dec["in_market"] * 100, out["share_on"].mean(), b.mean()))
    return out


N_SIM = 300
pl_rows = []
# 1) без состояний (чистый марков той же доли и длины серий)
o = markov_placebo(np.zeros(len(seg)), b_obs, N_SIM, 11, "марков без состояний")
pl_rows.append(dict(placebo="без состояний", **o.describe().loc[["50%", "mean", "max"]]["sharpe"].rename({"50%": "med"}).to_dict(), p=float((o["sharpe"] >= mt_dec["sharpe"]).mean())))
# 2) внутри st_vol
o = markov_placebo(seg["st_vol"].values, b_obs, N_SIM, 12, "марков внутри st_vol (0/1)")
pl_rows.append(dict(placebo="st_vol", med=o["sharpe"].median(), mean=o["sharpe"].mean(), max=o["sharpe"].max(), p=float((o["sharpe"] >= mt_dec["sharpe"]).mean())))
# 3) внутри терцилей реализованной волы (скользящие перцентили 756 дн)
rv = seg["realized_vol_21"]
q33 = d["realized_vol_21"].rolling(756, min_periods=252).quantile(1 / 3).loc[seg.index]
q67 = d["realized_vol_21"].rolling(756, min_periods=252).quantile(2 / 3).loc[seg.index]
ter = np.where(rv > q67, 2, np.where(rv > q33, 1, 0)).astype(float)
o = markov_placebo(ter, b_obs, N_SIM, 13, "марков внутри терцилей волы IMOEX")
pl_rows.append(dict(placebo="терцили волы IMOEX", med=o["sharpe"].median(), mean=o["sharpe"].mean(), max=o["sharpe"].max(), p=float((o["sharpe"] >= mt_dec["sharpe"]).mean())))
# 4) внутри терцилей волы RGBI
rvb = seg["rgbi_vol21"]
q33b = d["rgbi_vol21"].rolling(756, min_periods=252).quantile(1 / 3).loc[seg.index]
q67b = d["rgbi_vol21"].rolling(756, min_periods=252).quantile(2 / 3).loc[seg.index]
terb = np.where(rvb > q67b, 2, np.where(rvb > q33b, 1, 0)).astype(float)
o = markov_placebo(terb, b_obs, N_SIM, 14, "марков внутри терцилей волы RGBI")
pl_rows.append(dict(placebo="терцили волы RGBI", med=o["sharpe"].median(), mean=o["sharpe"].mean(), max=o["sharpe"].max(), p=float((o["sharpe"] >= mt_dec["sharpe"]).mean())))
# 5) внутри 4 состояний st_vol × st_bond
st4 = (seg["st_vol"].fillna(0) * 2 + seg["st_bond"].fillna(0)).values
o = markov_placebo(st4, b_obs, N_SIM, 15, "марков внутри st_vol × st_bond (4 сост.)")
pl_rows.append(dict(placebo="st_vol×st_bond", med=o["sharpe"].median(), mean=o["sharpe"].mean(), max=o["sharpe"].max(), p=float((o["sharpe"] >= mt_dec["sharpe"]).mean())))
# 6) внутри терцилей RGBI Δ21 (состояние распродажи ОФЗ)
rgd = seg["rgbi_d21"]
q33r = d["rgbi_d21"].rolling(756, min_periods=252).quantile(1 / 3).loc[seg.index]
q67r = d["rgbi_d21"].rolling(756, min_periods=252).quantile(2 / 3).loc[seg.index]
terr = np.where(rgd > q67r, 2, np.where(rgd > q33r, 1, 0)).astype(float)
o = markov_placebo(terr, b_obs, N_SIM, 16, "марков внутри терцилей RGBI Δ21")
pl_rows.append(dict(placebo="терцили RGBI Δ21", med=o["sharpe"].median(), mean=o["sharpe"].mean(), max=o["sharpe"].max(), p=float((o["sharpe"] >= mt_dec["sharpe"]).mean())))
# 7) внутри 9 состояний: терцили волы IMOEX × терцили RGBI Δ21
st9 = ter * 3 + terr
o = markov_placebo(st9, b_obs, N_SIM, 17, "марков внутри вола IMOEX × RGBI Δ21 (9 сост.)")
pl_rows.append(dict(placebo="вола×RGBIΔ21 (9)", med=o["sharpe"].median(), mean=o["sharpe"].mean(), max=o["sharpe"].max(), p=float((o["sharpe"] >= mt_dec["sharpe"]).mean())))
pd.DataFrame(pl_rows).to_csv("results/S_1_placebo.csv", index=False)

# ================================================================ (ж) после снятия бита
print("\n=== (ж) ПОСЛЕ СНЯТИЯ БИТА: ЦЕНА ПОЗДНЕГО ВХОДА ===")
mc = d["mcftr"]
rows = []
for k, ep in enumerate(episodes):
    start_m = pd.Period(ep[0], "M") + 1          # первый месяц во флэте по биту
    end_m = pd.Period(ep[-1], "M") + 1           # последний месяц во флэте
    days = idx[(idx.to_period("M") >= start_m) & (idx.to_period("M") <= end_m)]
    t0, t1 = ep[0], days[-1]                     # от среза-выхода до среза-входа
    r_flat = np.log(mc[t1] / mc[t0])
    # когда бит снялся на дневной ленте после выхода (первый день бит=0 после t0)
    bb = bit_dec.loc[t0:t1]
    clear = bb[bb == 0]
    t_clear = clear.index[0] if len(clear) else None
    r_after_clear = np.log(mc[t1] / mc[t_clear]) if t_clear is not None else np.nan
    # следующие 21/63 дня после входа
    i1 = idx.get_loc(t1)
    r_next21 = np.log(mc.iloc[min(i1 + 21, len(idx) - 1)] / mc[t1])
    r_next63 = np.log(mc.iloc[min(i1 + 63, len(idx) - 1)] / mc[t1])
    # что делал эталон после t1 (вошёл ли вообще)
    rows.append(dict(episode=k + 1, exit_cut=t0.date(), reentry_cut=t1.date(), n_months=len(ep),
                     mcftr_during_flat_pct=r_flat * 100, bit_cleared=None if t_clear is None else t_clear.date(),
                     days_clear_to_reentry=None if t_clear is None else int(idx.get_loc(t1) - idx.get_loc(t_clear)),
                     mcftr_clear_to_reentry_pct=r_after_clear * 100, next21_pct=r_next21 * 100, next63_pct=r_next63 * 100,
                     base_pos_at_reentry=float(dec_base.get(t1, np.nan))))
aft = pd.DataFrame(rows)
aft.to_csv("results/S_1_after_clear.csv", index=False)
print(aft.to_string(index=False, float_format=lambda v: f"{v:.1f}"))
print("итого: MCFTR за флэт-окна (сумма лог, %%): %.1f; из них после снятия бита до входа на срезе: %.1f (доля пропущенного роста внутри окон %.0f%%); медиана дней от снятия до входа %.0f" % (
    aft["mcftr_during_flat_pct"].sum(), aft["mcftr_clear_to_reentry_pct"].sum(),
    100 * aft["mcftr_clear_to_reentry_pct"].sum() / aft["mcftr_during_flat_pct"].abs().sum() if aft["mcftr_during_flat_pct"].abs().sum() else np.nan,
    aft["days_clear_to_reentry"].median()))
print("средняя MCFTR в 21/63 дн после входа: %.1f%% / %.1f%%; доля эпизодов с ростом MCFTR во флэт-окне: %.0f%%" % (
    aft["next21_pct"].mean(), aft["next63_pct"].mean(), 100 * (aft["mcftr_during_flat_pct"] > 0).mean()))

# своевременность (правило 7) для бита против эталона
tl_b = timeliness(pos_base.loc["2015":], d["mcftr"].loc["2015":])
tl_x = timeliness(pos_dec.loc["2015":], d["mcftr"].loc["2015":])
tl = tl_b.merge(tl_x, on=["peak", "trough", "depth"], suffixes=("_base", "_bit"))
tl.to_csv("results/S_1_timeliness.csv", index=False)
print("своевременность по просадкам MCFTR > 15% (2015+):")
print(tl.to_string(index=False, float_format=lambda v: f"{v:.2f}"))

pd.DataFrame(results).to_csv("results/S_1_variants.csv", index=False)
print("\nготово: results/S_1_*.csv")
