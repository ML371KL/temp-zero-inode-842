"""S2 — находка 1, дополнение: тонкая сетка порога 0,30…0,50 (обрыв на 0,376/0,383?), устойчивость к
дрожанию композита, LOYO с ограниченной сеткой. Запуск из audit/: python scripts/S2_1b_hyst_fine.py
"""
import sys
sys.path.insert(0, "scripts")
import numpy as np
import pandas as pd
from S2_lib import *  # noqa

D, C, M = load_all()
cuts = month_end_cuts(D.index)
raw_m = pd.DataFrame({k: D[k].reindex(cuts) for k, _ in LEGS_PROD})
Zm = pd.DataFrame({k: sgn * zroll(raw_m[k]) for k, sgn in LEGS_PROD})
comp_m, _ = composite_mean(Zm)
gate_m = (D["cell"].reindex(cuts) != TOXIC).astype(float)
tr = C["mcftr_ffill"]
mmc = (1 + C["mm_rate"] / 100.0 / 252.0).cumprod()
t_ = tr.reindex(cuts)
m_ = mmc.reindex(cuts)
FM = pd.DataFrame({"fwd_tr": t_.shift(-1) / t_ - 1, "fwd_mm": m_.shift(-1) / m_ - 1})
log = open(f"{RES}/S2_1b_log.txt", "w", encoding="utf-8")


def P(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    log.write(s + "\n")


def fast_ret(pos, F, cost=COST):
    p = pos.reindex(F.index).fillna(0).values
    tr_ = np.abs(np.diff(np.r_[p[0], p]))
    r = p * F["fwd_tr"].values + (1 - p) * F["fwd_mm"].values - cost * tr_
    return pd.Series(r, index=F.index)


def win(s, w):
    return s[(s.index >= pd.Timestamp(w[0])) & (s.index <= pd.Timestamp(w[1]))]


def panel_ret(comp, thr):
    sg = hysteresis_sign(comp, thr)
    pos = ((sg > 0) & (gate_m > 0)).astype(float)
    return fast_ret(pos, FM), pos


# ---- тонкая сетка
P("[fine] Шарп панели по порогу 0,30…0,50 шагом 0,01 (MAIN / A / B / ex-2022), и какие месяцы меняют позицию")
fine = np.round(np.arange(0.30, 0.501, 0.01), 2)
rows = []
prev_pos = None
for thr in fine:
    r, pos = panel_ret(comp_m, thr)
    rm = win(r, MAIN)
    row = dict(thr=thr, sharpe_main=sharpe(rm), sharpe_A=sharpe(win(r, WINDOWS["A 2004-17"])), sharpe_B=sharpe(win(r, WINDOWS["B 2018-26"])),
               sharpe_ex2022=sharpe(rm[rm.index.year != 2022]), cagr_main=(1 + rm).prod() ** (12 / len(rm)) - 1)
    if prev_pos is not None:
        ch = win(pos, MAIN)[(win(pos, MAIN) != win(prev_pos, MAIN))]
        row["months_changed"] = ";".join(f"{d.strftime('%Y-%m')}(c={comp_m.loc[d]:.3f},pos={int(v)})" for d, v in ch.items())
    else:
        row["months_changed"] = ""
    rows.append(row)
    prev_pos = pos
fine_df = pd.DataFrame(rows)
fine_df.to_csv(f"{RES}/S2_hyst_fine_grid.csv", index=False, float_format="%.4f")
P(fine_df.round(3).to_string())
sh = fine_df.set_index("thr")["sharpe_main"]
P(f"  скачок Шарпа между 0,37 и 0,39: {sh[0.39]-sh[0.37]:+.2f}; между 0,30 и 0,37: {sh[0.37]-sh[0.30]:+.2f}; между 0,39 и 0,50: {sh[0.50]-sh[0.39]:+.2f}")

# ---- какие значения композита на срезах лежат в (0,3; 0,5] и что было дальше
P("\n[fine] срезы MAIN с |композит| в (0,30; 0,50] — «пограничные» месяцы: знак 0,1 / позиция 0,1 / fwd MCFTR−mm")
cm = win(comp_m, MAIN)
s01 = hysteresis_sign(comp_m, 0.1)
border = cm[(cm.abs() > 0.30) & (cm.abs() <= 0.50)]
bd = pd.DataFrame({"composite": border, "sign01": s01.reindex(border.index), "gate": gate_m.reindex(border.index),
                   "fwd_ex": (FM["fwd_tr"] - FM["fwd_mm"]).reindex(border.index)})
bd["sign_prev"] = s01.shift(1).reindex(border.index)
bd["is_flip01"] = bd.sign01 != bd.sign_prev
bd.to_csv(f"{RES}/S2_hyst_border_months.csv", float_format="%.4f")
P(bd.round(3).to_string())
P(f"  пограничных месяцев: {len(bd)}; из них развороты знака при 0,1: {int(bd.is_flip01.sum())}; "
  f"средний fwd избыток (со знаком композита) у разворотных: {(np.sign(bd[bd.is_flip01].composite)*bd[bd.is_flip01].fwd_ex).mean()*100:+.2f} п.п., "
  f"у неразворотных: {(np.sign(bd[~bd.is_flip01].composite)*bd[~bd.is_flip01].fwd_ex).mean()*100:+.2f} п.п.")

# ---- дрожание композита: N(0, s) поверх композита, s = 0,03 / 0,05 / 0,10 (ошибка масштаба z-нормировки)
P("\n[fine] устойчивость к дрожанию композита (шум N(0,s) на каждом срезе, 500 реплик): Шарп MAIN при порогах 0,1 / 0,4 / 0,5 и разность")
rng = np.random.default_rng(1)
rows = []
for s in [0.02, 0.05, 0.10, 0.20]:
    vals = {0.1: [], 0.4: [], 0.5: [], "ma3": []}
    for k in range(500):
        cj = comp_m + pd.Series(rng.normal(0, s, len(comp_m)), index=comp_m.index).where(comp_m.notna())
        for thr in [0.1, 0.4, 0.5]:
            r, _ = panel_ret(cj, thr)
            vals[thr].append(sharpe(win(r, MAIN)))
        r, _ = panel_ret(cj.rolling(3, min_periods=2).mean(), 0.1)
        vals["ma3"].append(sharpe(win(r, MAIN)))
    d = np.array(vals[0.4]) - np.array(vals[0.1])
    rows.append(dict(noise=s, sh01=np.mean(vals[0.1]), sh04=np.mean(vals[0.4]), sh04_p10=np.percentile(vals[0.4], 10), sh04_p90=np.percentile(vals[0.4], 90),
                     sh05=np.mean(vals[0.5]), sh_ma3=np.mean(vals["ma3"]), d04_mean=d.mean(), d04_p10=np.percentile(d, 10), share_d04_pos=(d > 0).mean()))
jit = pd.DataFrame(rows)
jit.to_csv(f"{RES}/S2_hyst_jitter.csv", index=False, float_format="%.4f")
P(jit.round(3).to_string())

# ---- LOYO с ограниченной сеткой 0…0,6 и «выбор по Шарпу с минимальной долей в рынке ≥ 40 %»
P("\n[fine] leave-one-year-out, кросс-валидация выбора порога, сетка 0…0,6 (без вырожденных 0,7/0,8)")
THR = np.round(np.arange(0.0, 0.61, 0.1), 2)
R = pd.DataFrame({thr: win(panel_ret(comp_m, thr)[0], MAIN) for thr in THR})
years = sorted(set(R.index.year))
cv = pd.Series(index=R.index, dtype=float)
picks = {}
for y in years:
    trn = R[R.index.year != y]
    pick = float(trn.apply(sharpe).idxmax())
    picks[y] = pick
    cv.loc[R.index.year == y] = R.loc[R.index.year == y, pick]
P(f"  пороги по годам: {picks}")
P(f"  CV-Шарп (сетка ≤0,6): {sharpe(cv):.2f}; 0,1: {sharpe(R[0.1]):.2f}; 0,4 in-sample: {sharpe(R[0.4]):.2f}; 0,5: {sharpe(R[0.5]):.2f}")
d, p, lo, hi = boot_sharpe_diff(cv.values, R[0.1].values)
P(f"  бутстреп ΔШарп(CV − 0,1) = {d:+.2f}, p={p:.3f}, ДИ90 [{lo:+.2f}; {hi:+.2f}]")
# LOYO по блокам 2 года (меньше утечки через автокорреляцию)
cv2 = pd.Series(index=R.index, dtype=float)
picks2 = {}
blocks = [(2010, 2011), (2012, 2013), (2014, 2015), (2016, 2017), (2018, 2019), (2020, 2021), (2022, 2023), (2024, 2026)]
for a, b in blocks:
    mask = (R.index.year >= a) & (R.index.year <= b)
    pick = float(R[~mask].apply(sharpe).idxmax())
    picks2[(a, b)] = pick
    cv2.loc[mask] = R.loc[mask, pick]
P(f"  блоки по 2 года: пороги {picks2}; CV-Шарп {sharpe(cv2):.2f}")
# по половинам: выбрать на A применить на B и наоборот
RA = pd.DataFrame({thr: win(panel_ret(comp_m, thr)[0], WINDOWS["A 2004-17"]) for thr in THR})
RB = pd.DataFrame({thr: win(panel_ret(comp_m, thr)[0], WINDOWS["B 2018-26"]) for thr in THR})
pa, pb = float(RA.apply(sharpe).idxmax()), float(RB.apply(sharpe).idxmax())
P(f"  split: лучший на A = {pa} → на B даёт {sharpe(RB[pa]):.2f} (0,1 на B: {sharpe(RB[0.1]):.2f}); лучший на B = {pb} → на A даёт {sharpe(RA[pb]):.2f} (0,1 на A: {sharpe(RA[0.1]):.2f})")

# ---- сравнение концентрации: доля топ-3 месяцев в разности доходностей (0,4 − 0,1) против такой же доли у (прод − b&h) и (прод − деньги)
P("\n[fine] концентрация выигрыша: доля топ-3 месяцев в сумме положительных месяцев разности")
def conc(x):
    pos_ = x[x > 0].sort_values(ascending=False)
    return pos_.head(3).sum() / pos_.sum(), x.sum(), (x > 0).sum(), (x < 0).sum()
d04 = R[0.4] - R[0.1]
bh = win(FM["fwd_tr"], MAIN)
mm = win(FM["fwd_mm"], MAIN)
for name, x in [("0,4 − 0,1", d04), ("прод − b&h", R[0.1] - bh), ("прод − деньги", R[0.1] - mm), ("0,4 − деньги", R[0.4] - mm)]:
    c, s_, npos, nneg = conc(x.dropna())
    P(f"  {name}: топ-3 / сумма плюсов = {c:.0%}; сумма {s_*100:+.1f} п.п.; месяцев + {npos}, − {nneg}")

log.close()
