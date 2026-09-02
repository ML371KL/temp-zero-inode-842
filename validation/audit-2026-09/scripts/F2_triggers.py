"""F2_pm §6/§7: календарь МЕХАНИЧЕСКИХ триггеров переключения битов при неизменном рынке —
когда 252-дневный максимум RGBI, MA200 и 21-дневная вола сами «доедут» до порога
за счёт выпадения старых наблюдений из окна. Запуск: python scripts/F2_triggers.py (из audit/)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from F2_lib import *

d = load_daily()
last = d.iloc[-1]
H = 130  # горизонт, торговых дней
# --- RGBI: флаг снимается, если log(rgbi / max252) > -0.04 (и -0.03 в варианте с гистерезисом)
rg = d["rgbi"].dropna().values
rows = []
for k in range(0, H + 1):
    window = np.r_[rg[len(rg) - 252 + k:], np.full(k, last.rgbi)]  # старые выпадают, новые = сегодня
    mx = window.max()
    dd = np.log(last.rgbi / mx)
    rows.append({"days_ahead": k, "rgbi_max252_if_flat": round(mx, 2), "dd_if_flat%": round(dd * 100, 2),
                 "rgbi_needed_for_-4%": round(mx * np.exp(-0.04), 2), "rgbi_needed_for_-3%": round(mx * np.exp(-0.03), 2)})
RG = pd.DataFrame(rows)
lift4 = RG[RG["dd_if_flat%"] > -4.0]; lift3 = RG[RG["dd_if_flat%"] > -3.0]
print("=== RGBI: облигационный флаг при НЕИЗМЕННОМ RGBI (%.2f) ===" % last.rgbi)
print(RG[RG.days_ahead.isin([0, 3, 5, 10, 21, 42, 63, 84, 105, 126])].to_string(index=False))
print("флаг −4% снимется сам через", int(lift4.days_ahead.iloc[0]) if len(lift4) else ">H", "торг. дней;",
      "порог −3% (гистерезис) —", int(lift3.days_ahead.iloc[0]) if len(lift3) else ">H")
# --- MA200 при неизменном IMOEX
px = d["imoex"].dropna().values
rows = []
for k in range(0, H + 1):
    w = np.r_[px[len(px) - 200 + k:], np.full(k, last.imoex)]
    ma = w.mean()
    rows.append({"days_ahead": k, "ma200_if_flat": round(ma, 0), "gap%": round((last.imoex / ma - 1) * 100, 1),
                 "imoex_needed_bull": round(ma, 0), "imoex_needed_bull_+2%": round(ma * 1.02, 0)})
MA = pd.DataFrame(rows)
print("\n=== MA200 при НЕИЗМЕННОМ IMOEX (%.0f) ===" % last.imoex)
print(MA[MA.days_ahead.isin([0, 21, 42, 63, 84, 105, 126])].to_string(index=False))
cross = MA[MA["gap%"] >= 0]
print("тренд станет «бык» сам через", int(cross.days_ahead.iloc[0]) if len(cross) else ">H", "торг. дней при плоском рынке")
# --- вола 21д при спокойных днях (|ret| = 0.8%/день, ~12.7% годовых)
r = d["ret1"].dropna().values
rows = []
for k in range(0, 30):
    w = np.r_[r[len(r) - 21 + k:], np.full(k, 0.008) * np.where(np.arange(k) % 2 == 0, 1, -1)]
    rv = w.std(ddof=1) * np.sqrt(252) if k < 21 else 0.008 * np.sqrt(252)
    rows.append({"days_ahead": k, "vol21_if_calm%": round(rv * 100, 1)})
V = pd.DataFrame(rows)
q80 = last.vol_thresh80 * 100; q60 = d["realized_vol_21"].rolling(756, min_periods=252).quantile(0.6).iloc[-1] * 100
print("\n=== вола 21д при спокойных днях (±0.8%% в день): порог q80 %.1f%%, q60 %.1f%% ===" % (q80, q60))
print(V[V.days_ahead.isin([0, 3, 5, 7, 10, 15, 21])].to_string(index=False))
b80 = V[V["vol21_if_calm%"] < q80]; b60 = V[V["vol21_if_calm%"] < q60]
print("ниже q80 через", int(b80.days_ahead.iloc[0]) if len(b80) else ">30", "спокойных дней; ниже q60 через", int(b60.days_ahead.iloc[0]) if len(b60) else ">30")
RG.to_csv(os.path.join(RES, "F2_triggers_rgbi.csv"), index=False)
MA.to_csv(os.path.join(RES, "F2_triggers_ma200.csv"), index=False)
V.to_csv(os.path.join(RES, "F2_triggers_vol.csv"), index=False)
# история: сколько раз облигационный флаг снимался «механически» (RGBI за день не вырос, а флаг снялся)
bond = d["st_bond"]; rgd = d["rgbi"].diff()
lift = (bond.shift(1) == 1) & (bond == 0)
mech = lift & (rgd <= 0)
print(f"\nистория с 2004: снятий облигационного флага {int(lift.sum())}, из них при НЕрастущем RGBI в день снятия — {int(mech.sum())} "
      f"({mech.sum()/max(1,lift.sum())*100:.0f}%) — выпадение максимума из окна, а не разворот облигаций")
print("готово: results/F2_triggers_*.csv")
