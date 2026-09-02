"""D2_06: механизм двух главных кандидатов — эпизоды срабатывания, что было избегнуто, пересечение с
токсичной ячейкой; версия y1−ключ по ДАТЕ РЕШЕНИЯ (key_dec) против даты вступления в силу."""
import numpy as np
import pandas as pd
from D2_lib import *

d = derive_rates(load_daily())
m = monthly_frame(d)
idx = d.index
d["y1_keydec"] = d["y1"] - d["key_dec"]
d["y1_keydec_d21"] = d["y1_keydec"] - d["y1_keydec"].shift(21)
lp = np.log(d["mcftr_ffill"])
pos_m = baseline_position(m); base_d = monthly_to_daily_pos(pos_m, idx)


def runs(mask, start="2015-01-01"):
    mask = mask.fillna(False) & (idx >= start)
    v = mask.values.astype(int); edges = np.diff(np.r_[0, v, 0])
    s = np.flatnonzero(edges == 1); e = np.flatnonzero(edges == -1) - 1
    out = []
    for a, b in zip(s, e):
        t0, t1 = idx[a], idx[b]
        r = lp.loc[t1] - lp.loc[max(idx[a - 1], idx[0]) if a > 0 else t0]
        after = lp.iloc[min(b + 21, len(idx) - 1)] - lp.loc[t1]
        out.append(dict(start=t0.date(), end=t1.date(), days=b - a + 1, mcftr_in_run_pct=round((np.exp(r) - 1) * 100, 1),
                        mcftr_next21_pct=round((np.exp(after) - 1) * 100, 1),
                        base_long_share=round(base_d.loc[t0:t1].mean(), 2), toxic_share=round(d["toxic"].loc[t0:t1].mean(), 2),
                        rgbi_dd_start=round((np.exp(d["rgbi_dd"].loc[t0]) - 1) * 100, 1), bond_bit_start=int(d["st_bond"].loc[t0]) if pd.notna(d["st_bond"].loc[t0]) else None))
    return pd.DataFrame(out)


for name, mask in [("y1_key_d21>0.25", d["y1_key_d21"] > 0.25), ("y1_keydec_d21>0.25", d["y1_keydec_d21"] > 0.25),
                   ("real_saar_d63>1", d["real_saar_d63"] > 1.0)]:
    r = runs(mask)
    r.to_csv(RES / f"D2_06_runs_{name.replace('>', '_gt').replace('.', '')}.csv", index=False)
    print(f"\n=== {name}: эпизодов {len(r)}, дней в сумме {r.days.sum()}, средняя длина {r.days.mean():.0f} ===")
    long_runs = r[r.base_long_share > 0.5]
    print(f"  эпизоды, когда эталон был в лонге (реально избегнутые): {len(long_runs)}; "
          f"MCFTR в них: среднее {long_runs.mcftr_in_run_pct.mean():+.1f}%, медиана {long_runs.mcftr_in_run_pct.median():+.1f}%, "
          f"доля отрицательных {(long_runs.mcftr_in_run_pct < 0).mean()*100:.0f}%; следующие 21 дн: {long_runs.mcftr_next21_pct.mean():+.1f}%")
    print(f"  все эпизоды: MCFTR среднее {r.mcftr_in_run_pct.mean():+.1f}%, доля отрицательных {(r.mcftr_in_run_pct < 0).mean()*100:.0f}%; "
          f"бонд-бит уже включён на старте в {r.bond_bit_start.mean()*100:.0f}% эпизодов, токсичная доля {r.toxic_share.mean():.2f}")
    print(r[r.days >= 5].to_string(index=False))

# совпадение масок y1_key vs y1_keydec
a = (d["y1_key_d21"] > 0.25); b = (d["y1_keydec_d21"] > 0.25)
sub = idx >= "2015-01-01"
print("\nсовпадение масок key_eff vs key_dec: %.1f%% дней; on-доля %.1f%% / %.1f%%" % ((a[sub] == b[sub]).mean() * 100, a[sub].mean() * 100, b[sub].mean() * 100))

# что сигнал ловит: корреляция дневных Δ(y1−key) с Δrgbi и ret
dd = pd.DataFrame({"d_y1key": d["y1_key"].diff(), "d_rgbi": np.log(d["rgbi"]).diff(), "ret": np.log(d["imoex"]).diff()}).dropna()
dd = dd[dd.index >= "2015-01-01"]
print("корреляция дневных изменений (2015+): Δ(y1−key) vs ΔRGBI %.2f; Δ(y1−key) vs ret IMOEX %.2f" % (dd.corr().iloc[0, 1], dd.corr().iloc[0, 2]))
# IC дневной сигнал → fwd21 (для справки, перекрытие) и месячный (из D2_01)
mm = m.copy(); mm["y1_keydec_d21"] = d["y1_keydec_d21"].iloc[month_ends(idx)].values
for c in ["y1_key_d21", "y1_keydec_d21", "real_saar_d63"]:
    r = ic_stats(mm.loc["2015":, c], mm.loc["2015":, "fwd1m_tr"], n_boot=1000)
    print(f"IC месячный {c}: n={r['n']} IC={r['ic']:+.3f} p={r['p_boot']:.3f} NW t={r['nw_t']:+.2f}")
# текущее состояние
last = d.iloc[-1]
print("\nСЕЙЧАС (%s): y1−key=%.2f, Δ21=%.2f; y0.5−key=%.2f; real_saar=%.2f, Δ63=%.2f; RUSFAR−key=%.2f; y10−key=%.2f; наклон 10−1=%.2f" %
      (idx[-1].date(), last.y1_key, last.y1_key_d21, last.y05_key, last.real_saar, last.real_saar_d63, last.rusfar_key, last.y10_key, last.slope_10_1))
print(d[["y1_key", "y1_key_d21", "real_saar", "real_saar_d63", "rusfar_key", "y10_key"]].loc["2026-05-01":].resample("2W").last().round(2).to_string())
