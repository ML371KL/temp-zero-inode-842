"""D2_04: первичный рынок ОФЗ (2021-01…2026-08, 246 аукционных дней) как индикатор фискального стресса:
провалы, спрос/размещение, доля флоатеров. Event study с плацебо (дедуп эпизодов), месячные IC,
пересечение с бонд-битом, ворота поверх правила панели на окне 2021+ (честно: n мал)."""
import numpy as np
import pandas as pd
from D2_lib import *

d = derive_rates(load_daily())
m = monthly_frame(d)
idx = d.index
au = pd.read_csv(DATA / "auctions.csv", parse_dates=["date"]).sort_values("date")
au["failed"] = au["failed"].astype(str).str.lower().isin(["true", "1"])
au["btc"] = au["demand_bln"] / au["placed_bln"].replace(0, np.nan)
au["floater"] = au["floater_share"].fillna(0.0)
# недельные признаки → на дневную сетку (доступно с даты аукциона, среда)
w = au.set_index("date")
feat = pd.DataFrame(index=idx)
for c in ["failed", "placed_bln", "demand_bln", "btc", "floater"]:
    feat[c] = w[c].reindex(idx.union(w.index)).ffill(limit=10).reindex(idx)
# скользящие 4 недели (по аукционным датам)
w4 = w[["failed", "placed_bln", "floater"]].rolling(4, min_periods=2).agg({"failed": "sum", "placed_bln": "sum", "floater": "mean"})
w4.columns = ["fails_4w", "placed_4w", "floater_4w"]
w4["fails_8w"] = w["failed"].rolling(8, min_periods=4).sum()
for c in w4.columns:
    feat[c] = w4[c].reindex(idx.union(w4.index)).ffill(limit=10).reindex(idx)
d = d.join(feat)

# --- 1. event study: провал аукциона (дедуп: первый провал после ≥4 недель без провалов)
ret = np.log(d["imoex"]).diff().dropna(); ridx = ret.index; pm = {t: i for i, t in enumerate(ridx)}
fails = au[au.failed]["date"].tolist()
dedup = []
for t in fails:
    if not dedup or (t - dedup[-1]).days >= 28: dedup.append(t)
print(f"провалов всего {len(fails)}, эпизодов (дедуп ≥28 дн) {len(dedup)}")
print("эпизоды:", [str(t.date()) for t in dedup])


def car_p(evs, a, b, era=("2021-01-01", "2026-08-31"), n=4000, seed=3):
    ra = ret.values; cs = np.cumsum(np.insert(ra, 0, 0.0)); out = []
    for t in evs:
        later = ridx[ridx >= t]
        if len(later) == 0: continue
        e = pm[later[0]]
        if e + a < 0 or e + b + 1 > len(ra): continue
        out.append(cs[e + b + 1] - cs[e + a])
    out = np.array(out)
    if len(out) < 3: return np.nan, np.nan, len(out)
    rng = np.random.default_rng(seed)
    pool = np.where((ridx >= era[0]) & (ridx <= era[1]))[0]; pool = pool[(pool + a >= 0) & (pool + b + 1 <= len(ra))]
    pl = np.array([(cs[e + b + 1] - cs[e + a]).mean() for e in (rng.choice(pool, size=len(out)) for _ in range(n))])
    p = (np.abs(pl - pl.mean()) >= abs(out.mean() - pl.mean())).mean()
    return out.mean(), p, len(out)


rows = []
groups = {"fail_all": fails, "fail_dedup": dedup, "fail_dedup_ex2022": [t for t in dedup if t.year != 2022],
          "fail_dedup_2025_26": [t for t in dedup if t.year >= 2025],
          "floater_gt50": au[(au.floater > 0.5)]["date"].tolist(),
          "big_placement_gt300": au[au.placed_bln > 300]["date"].tolist(),
          "low_btc_lt1.5": au[(au.btc < 1.5) & au.btc.notna()]["date"].tolist()}
for g, evs in groups.items():
    for (a, b) in [(-5, -1), (0, 0), (0, 5), (0, 21), (0, 42)]:
        c, p, n = car_p(evs, a, b)
        rows.append(dict(group=g, window=f"[{a},{b}]", n=n, car_pct=round(c * 100, 2) if c == c else np.nan, p=round(p, 3) if p == p else np.nan))
ev = pd.DataFrame(rows); ev.to_csv(RES / "D2_04_auction_events.csv", index=False)
print(ev.pivot_table(index="group", columns="window", values="car_pct").to_string())
print(ev.pivot_table(index="group", columns="window", values="p").to_string())
print(ev.groupby("group")["n"].first().to_string())

# --- 2. пересечение с бонд-битом и токсичной ячейкой: провал аукциона — новость или уже отражено?
fd = d.loc[[t for t in fails if t in d.index]]
print("\nв дни провалов: бонд-бит уже включён %.0f%%, токсичная ячейка %.0f%%, RGBI-dd медиана %.1f%%" %
      (fd["st_bond"].mean() * 100, fd["toxic"].mean() * 100, np.exp(fd["rgbi_dd"].median()) * 100 - 100))
print("на всём окне 2021+: бонд-бит %.0f%%, токсичная %.0f%%" % (d.loc["2021":, "st_bond"].mean() * 100, d.loc["2021":, "toxic"].mean() * 100))

# --- 3. месячные IC (2021-01…2026-08)
mm = monthly_frame(d)
print("\nIC месячный (fwd1m MCFTR), 2021+:")
icr = []
for c in ["fails_4w", "fails_8w", "placed_4w", "floater_4w", "btc", "floater"]:
    r = ic_stats(mm.loc["2021":, c], mm.loc["2021":, "fwd1m_tr"], n_boot=1000); r["signal"] = c; icr.append(r)
    print(f"  {c:12s} n={r['n']} IC={r['ic']:+.3f} p={r['p_boot']:.3f} NW t={r['nw_t']:+.2f}")
pd.DataFrame(icr).to_csv(RES / "D2_04_auction_ic.csv", index=False)

# --- 4. ворота поверх правила панели, окно 2021-01…2026-08, месячная и дневная каденции
me = month_ends(idx); me_dates = idx[me]; is_me = np.zeros(len(idx), dtype=bool); is_me[me] = True
comp_ok_d = monthly_to_daily_pos((m["comp_sign"] > 0).astype(float), idx)
toxic_d = d["toxic"].fillna(0.0); base_exit = toxic_d == 1; base_entry = toxic_d == 0
r_long_d = np.log(d["mcftr_ffill"]).diff(); r_cash_d = d["mm_rate"] / 100 / 252


def build_pos(exit_cond, entry_cond, cadence):
    ex = exit_cond.reindex(idx).fillna(False).values.astype(bool); en = entry_cond.reindex(idx).fillna(False).values.astype(bool)
    co = comp_ok_d.values > 0; pos = np.zeros(len(idx)); cur = 0.0
    for i in range(len(idx)):
        if cadence == "daily" or is_me[i]:
            if cur == 1.0 and ((ex[i] and not en[i]) or not co[i]): cur = 0.0
            elif cur == 0.0 and en[i] and co[i]: cur = 1.0
        pos[i] = cur
    return pd.Series(pos, index=idx)


def bt_daily(pos, a, b, cost=COST):
    df = pd.DataFrame({"pos": pos.shift(1), "r_long": r_long_d, "r_cash": r_cash_d}).dropna()
    df = df[(df.index >= a) & (df.index <= b)]; df["trade"] = df["pos"].diff().abs().fillna(0.0)
    df["ret"] = df["pos"] * df["r_long"] + (1 - df["pos"]) * df["r_cash"] - df["trade"] * cost; return df


def bt_monthly(pos, a, b):
    pmm = pos.iloc[me]; pmm.index = me_dates; return run_monthly(pmm, m.reindex(me_dates), cost=COST, start=a, end=b)


V = {"baseline": (base_exit, base_entry)}
for c, thr, lbl in [("fails_4w", 0.5, "провал за 4 нед"), ("fails_4w", 1.5, "≥2 провала за 4 нед"), ("fails_8w", 1.5, "≥2 провала за 8 нед"),
                    ("floater_4w", 0.5, "флоатеры >50% за 4 нед"), ("floater", 0.99, "аукцион только флоатер")]:
    x = d[c] > thr
    V[f"exit_{c}_gt{thr}"] = (base_exit | x, base_entry & ~x)
rows = []
for a, b, wn in [("2021-01-01", "2026-08-31", "2021_2026"), ("2021-01-01", "2024-12-31", "2021_2024"), ("2025-01-01", "2026-08-31", "2025_2026")]:
    for nm, (ex, en) in V.items():
        for cad in ("monthly", "daily"):
            pos = build_pos(ex, en, cad)
            df = bt_monthly(pos, a, b) if cad == "monthly" else bt_daily(pos, a, b)
            mt = metrics(df, freq=12 if cad == "monthly" else 252, name=nm); mt.update(window=wn, cadence=cad)
            dfx = df[df.index.year != 2022]; mt["sharpe_ex2022"] = metrics(dfx, freq=12 if cad == "monthly" else 252).get("sharpe")
            rows.append(mt)
tab = fmt_metrics_table(rows); tab["sharpe_ex2022"] = tab["sharpe_ex2022"].round(2)
tab.to_csv(RES / "D2_04_auction_gates.csv", index=False)
print("\nворота по аукционам поверх правила панели:")
print(tab[["window", "cadence", "name", "cagr", "sharpe", "maxdd", "time_in_mkt", "trades_per_yr", "sharpe_ex2022"]].to_string(index=False))
