"""F2_pm §6/§7: числа для решения «сегодня» — условная статистика текущего состояния,
что бывало после снятия облигационного флага / выхода выше MA200 из токсичной ячейки,
длительности токсичных эпизодов, уровни переключения, реакция на решения ЦБ.
Запуск: python scripts/F2_today.py (из audit/)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from F2_lib import *

d = load_daily()
m = load_monthly()
px = d["imoex"]
tr = d["mcftr_ffill"].where(d["mcftr_ffill"].notna(), d["imoex"])
dd = d[d.index >= "2004-01-01"].copy()

# ---------------------------------------------------------------- 1. токсичные эпизоды (дневные)
tox = (dd["cell"] == TOXIC)
eps = []
i = 0; v = tox.values; idx = dd.index
while i < len(v):
    if v[i]:
        j = i
        while j < len(v) and v[j]: j += 1
        eps.append((idx[i], idx[j - 1], j - i, j))
        i = j
    else:
        i += 1
rows = []
for a, b, L, jend in eps:
    ia = idx.get_loc(a); ib = idx.get_loc(b)
    r_in = (tr.loc[b] / tr.loc[a] - 1) * 100
    # что было в 21/63 дня ПОСЛЕ выхода из ячейки (если есть)
    r21 = (tr.iloc[min(len(tr) - 1, ib + 21)] / tr.loc[b] - 1) * 100 if ib + 1 < len(tr) else np.nan
    r63 = (tr.iloc[min(len(tr) - 1, ib + 63)] / tr.loc[b] - 1) * 100 if ib + 1 < len(tr) else np.nan
    # минимум внутри эпизода и сколько от минимума прошло до выхода
    seg = px.loc[a:b]; lo = seg.idxmin()
    rows.append({"start": a.date(), "end": b.date(), "days": L, "MCFTR_in%": round(r_in, 1),
                 "trough": lo.date(), "trough_to_exit%": round((px.loc[b] / px.loc[lo] - 1) * 100, 1),
                 "after21%": round(r21, 1), "after63%": round(r63, 1),
                 "next_cell": dd["cell"].iloc[jend] if jend < len(dd) else "(идёт)"})
E = pd.DataFrame(rows)
pd.set_option("display.width", 250)
print("=== токсичные эпизоды с 2004 (дневная лента; последний — идёт) ===")
print(E.to_string(index=False))
E.to_csv(os.path.join(RES, "F2_today_toxic_episodes.csv"), index=False)
# эпизоды короче 10 дней — дребезг
print(f"эпизодов: {len(E)}, медиана длительности {E.days.median():.0f} дн, короче 10 дн: {(E.days<10).sum()}, длиннее 60: {(E.days>60).sum()}")
big = E[E.days >= 20]
print(f"по эпизодам >=20 дн: медиана 'от дна до выхода из ячейки' {big['trough_to_exit%'].median():+.1f}%, "
      f"after21 медиана {big['after21%'].median():+.1f}%, after63 медиана {big['after63%'].median():+.1f}% (n={len(big)})")

# ---------------------------------------------------------------- 2. как открывались ворота: какой бит снялся первым
print("\n=== выход из токсичной ячейки: какой бит снялся (по эпизодам >=20 дн) ===")
print(big["next_cell"].value_counts().to_string())

# доходность после открытия ворот в зависимости от того, какой бит снялся
rows = []
for _, e in big.iterrows():
    if e.next_cell == "(идёт)": continue
    b = pd.Timestamp(e.end); ib = idx.get_loc(b)
    for h in (21, 63, 126):
        if ib + h < len(tr):
            rows.append({"exit": e.end, "via": e.next_cell, "h": h, "fwd%": round((tr.iloc[ib + h] / tr.iloc[ib] - 1) * 100, 1)})
X = pd.DataFrame(rows)
if len(X):
    print(X.pivot_table(index="via", columns="h", values="fwd%", aggfunc=["count", "median", "mean"]).round(1).to_string())

# ---------------------------------------------------------------- 3. месячная условная статистика текущего состояния
mm = m[(m.index >= "2004-01-01") & m["closed"] & m["cell"].notna()].copy()
mm["fwd"] = mm["fwd1m_log"] * 100
# fwd3m по месячным ценам
mm["fwd3"] = (np.log(mm["imoex"].shift(-3) / mm["imoex"])) * 100
cur = mm[(mm.cell == TOXIC) & (mm.core_sign == 1)]
print(f"\n=== сегодня: токсичная & композит>0 — fwd1m n={len(cur)} mean={cur.fwd.mean():+.2f} median={cur.fwd.median():+.2f} hit={(cur.fwd>0).mean():.2f}; "
      f"fwd3m n={cur.fwd3.notna().sum()} mean={cur.fwd3.mean():+.2f} median={cur.fwd3.median():+.2f}")
print(cur[["imoex", "composite", "fwd", "fwd3"]].round(2).to_string())
# токсичная & композит>0 & сильный usd_mom (как сейчас, z>1)
c2 = cur[cur.z_usd_mom63 > 1]
print(f"...и z(usd_mom63)>1: n={len(c2)} mean={c2.fwd.mean():+.2f} median={c2.fwd.median():+.2f}")

# ---------------------------------------------------------------- 4. уровни переключения сегодня
last = d.iloc[-1]
rg_max = d["rgbi"].rolling(252, min_periods=200).max().iloc[-1]
lvl_bond = rg_max * np.exp(-0.04)
print(f"\n=== уровни переключения на {d.index[-1].date()} ===")
print(f"RGBI {last.rgbi:.2f}; 252-дн максимум {rg_max:.2f}; флаг снимется выше {lvl_bond:.2f} (+{(lvl_bond/last.rgbi-1)*100:.1f}%)")
print(f"IMOEX {last.imoex:.0f}; MA200 {last.ma200:.0f}; нужно +{(last.ma200/last.imoex-1)*100:.1f}% (MA200 снижается, точка встречи ближе)")
print(f"вола 21д {last.realized_vol_21*100:.1f}% против порога {last.vol_thresh80*100:.1f}%")
# сколько дней MA200 будет падать: средняя цена 200 дней назад
print(f"через 21 торг. день из MA200 выпадут дни с ценами ~{d['imoex'].iloc[-200:-179].mean():.0f} (сейчас {last.imoex:.0f}) — MA200 продолжит снижаться")

# ---------------------------------------------------------------- 5. решения ЦБ: реакция индекса по типу сюрприза
cb = pd.read_csv(os.path.join(DATA, "cb_decisions.csv"), parse_dates=["date"])
rows = []
for _, r in cb.iterrows():
    t = r.date
    if t not in px.index:
        # ближайший торговый день на дату или после
        nxt = px.index[px.index >= t]
        if not len(nxt): continue
        t = nxt[0]
    i = px.index.get_loc(t)
    if i < 1 or i + 21 >= len(px): continue
    rows.append({"date": r.date.date(), "surprise": r.surprise, "d1%": (px.iloc[i] / px.iloc[i - 1] - 1) * 100,
                 "d5%": (px.iloc[i + 5] / px.iloc[i - 1] - 1) * 100, "d21%": (px.iloc[i + 21] / px.iloc[i - 1] - 1) * 100})
CB = pd.DataFrame(rows)
print("\n=== реакция IMOEX на решения ЦБ (с дня до решения), по типу сюрприза, n=%d ===" % len(CB))
print(CB.groupby("surprise")[["d1%", "d5%", "d21%"]].agg(["count", "mean", "median"]).round(2).to_string())
CB.to_csv(os.path.join(RES, "F2_today_cb_reactions.csv"), index=False)
print("\nготово: results/F2_today_*.csv")
