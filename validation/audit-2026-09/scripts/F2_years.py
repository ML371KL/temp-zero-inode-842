"""F2_pm: погодовая таблица правил против b&h и денег; помесячный разбор 2024-09…2026-08 —
где именно панель проиграла деньгам. Запуск: python scripts/F2_years.py (после F2_switching.py)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from F2_lib import *

d = load_daily()
m = load_monthly()
POS = pd.read_csv(os.path.join(RES, "F2_rules_positions.csv"), index_col=0, parse_dates=True)
sel = {"PANEL_M": "PANEL_M: ворота+ядро, месячная оценка", "PANEL_W": "PANEL_W: ворота+ядро, недельная оценка",
       "HYST+ядро": "HYST ворота (bond off -3.0%, тренд ±2%, vol off q60) + ядро",
       "SM3": "SM3: HYST(−3%,±2%,q60), недельная оценка"}
bh, mm = bench(d)
Y = pd.DataFrame({"b&h MCFTR": bh["r"], "деньги": mm["r"]})
for k, v in sel.items():
    Y[k] = backtest(d, POS[v], 0.002)["r"]
Y = Y[(Y.index >= "2004-01-01") & (Y.index <= "2026-08-31")]
yr = (np.exp(Y.groupby(Y.index.year).sum()) - 1) * 100
yr["PANEL_M − деньги"] = yr["PANEL_M"] - yr["деньги"]
yr["PANEL_M − b&h"] = yr["PANEL_M"] - yr["b&h MCFTR"]
pos_share = POS[sel["PANEL_M"]].shift(1).groupby(POS.index.year).mean() * 100
yr["PANEL_M in_mkt%"] = pos_share.reindex(yr.index)
pd.set_option("display.width", 250)
print("=== по годам, % (2026 — по август) ===")
print(yr.round(1).to_string())
yr.round(2).to_csv(os.path.join(RES, "F2_years.csv"))
print(f"\nлет, когда PANEL_M проиграл деньгам: {(yr['PANEL_M − деньги']<0).sum()} из {len(yr)}; проиграл b&h: {(yr['PANEL_M − b&h']<0).sum()}")

# помесячно 2024-09 .. 2026-08
MO = pd.DataFrame({"MCFTR%": (np.exp(Y["b&h MCFTR"].resample("ME").sum()) - 1) * 100,
                   "деньги%": (np.exp(Y["деньги"].resample("ME").sum()) - 1) * 100,
                   "PANEL_M%": (np.exp(Y["PANEL_M"].resample("ME").sum()) - 1) * 100,
                   "SM3%": (np.exp(Y["SM3"].resample("ME").sum()) - 1) * 100,
                   "pos_M": POS[sel["PANEL_M"]].shift(1).resample("ME").mean(),
                   "pos_SM3": POS[sel["SM3"]].shift(1).resample("ME").mean()})
mm2 = m[["composite", "core_sign", "cell"]].copy(); mm2.index = mm2.index.to_period("M").to_timestamp("M")
MO = MO.join(mm2, how="left")
MO = MO[(MO.index >= "2024-08-31") & (MO.index <= "2026-08-31")]
print("\n=== помесячно 2024-09…2026-08: доходности, позиция, композит/ячейка на конец месяца (решение на следующий) ===")
print(MO.round(2).to_string())
MO.round(3).to_csv(os.path.join(RES, "F2_months_2024_2026.csv"))
# что дал бы флэт всё время в 2025-26 и где были потери
sub = MO[MO.index >= "2025-01-31"]
print(f"\n2025-01…2026-08: MCFTR {((1+sub['MCFTR%']/100).prod()-1)*100:+.1f}%, деньги {((1+sub['деньги%']/100).prod()-1)*100:+.1f}%, "
      f"PANEL_M {((1+sub['PANEL_M%']/100).prod()-1)*100:+.1f}%, SM3 {((1+sub['SM3%']/100).prod()-1)*100:+.1f}%")
lost = sub[(sub.pos_M > 0.5) & (sub["MCFTR%"] < sub["деньги%"])]
print(f"месяцев в лонге с проигрышем деньгам: {len(lost)} из {int((sub.pos_M>0.5).sum())} лонговых; суммарный проигрыш в них {(lost['MCFTR%']-lost['деньги%']).sum():+.1f} п.п.")
print("готово: results/F2_years.csv, F2_months_2024_2026.csv")
