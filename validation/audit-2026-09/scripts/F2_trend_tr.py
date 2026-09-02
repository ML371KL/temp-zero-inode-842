"""F2_pm §6 (гипотеза): тренд-бит по ценовому IMOEX смещён к «медведю» дивидендными гэпами
(~8%/год). Проверка: тот же бит по MCFTR (полная доходность) — как меняются доля «медведя»,
ячейки и результат правила панели. Запуск: python scripts/F2_trend_tr.py (из audit/)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from F2_lib import *

d = load_daily()
m = load_monthly()
core = core_sign_daily(d, m)
have = d["cell"].notna() & core.notna()
tr = d["mcftr_ffill"]
ma_tr = tr.rolling(200, min_periods=200).mean()
bull_px = (d["imoex"] > d["ma200"]).astype(int)
bull_tr = (tr > ma_tr).astype(int)
ok = d["ma200"].notna() & ma_tr.notna() & (d.index >= "2004-01-01")
print(f"доля дней «бык» 2004+: по IMOEX {bull_px[ok].mean()*100:.1f}%, по MCFTR {bull_tr[ok].mean()*100:.1f}%; расходятся в {(bull_px[ok]!=bull_tr[ok]).mean()*100:.1f}% дней")
print(f"сегодня: IMOEX/MA200 {d['imoex'].iloc[-1]/d['ma200'].iloc[-1]-1:+.1%}, MCFTR/MA200(TR) {tr.iloc[-1]/ma_tr.iloc[-1]-1:+.1%}")
toxic_px = (bull_px == 0) & (d["st_vol"] == 1) & (d["st_bond"] == 1)
toxic_tr = (bull_tr == 0) & (d["st_vol"] == 1) & (d["st_bond"] == 1)
print(f"дней в токсичной ячейке 2004+: по IMOEX {int(toxic_px[ok].sum())}, по MCFTR {int(toxic_tr[ok].sum())}")


def sample_hold(sig, freq):
    s = sig.astype(float)
    if freq == "W":
        marks = s.groupby([s.index.isocalendar().year, s.index.isocalendar().week]).apply(lambda g: g.index[-1])
    else:
        marks = s.groupby(s.index.to_period("M")).apply(lambda g: g.index[-1])
    o = pd.Series(np.nan, index=s.index); o.loc[marks.values] = s.loc[marks.values]
    return o.ffill().fillna(0.0)


rules = {
    "PANEL_M (тренд по IMOEX)": sample_hold(((~toxic_px) & (core == 1)).where(have, False), "M"),
    "PANEL_M (тренд по MCFTR)": sample_hold(((~toxic_tr) & (core == 1)).where(have, False), "M"),
    "PANEL_W (тренд по IMOEX)": sample_hold(((~toxic_px) & (core == 1)).where(have, False), "W"),
    "PANEL_W (тренд по MCFTR)": sample_hold(((~toxic_tr) & (core == 1)).where(have, False), "W"),
}
T = pd.concat([run_windows(d, s, nm, with_bench=False) for nm, s in rules.items()], ignore_index=True)
T = T[["rule", "window", "CAGR%", "Sharpe", "MDD%", "in_mkt%", "trades/yr", "beat_bh_years%"]]
pd.set_option("display.width", 220)
for w in ["2010-2026.08 (осн.)", "2004+ (полная)", "2025-2026.08"]:
    print(f"\n=== {w} ==="); print(T[T.window == w].drop(columns=["window"]).to_string(index=False))
T.to_csv(os.path.join(RES, "F2_trend_tr.csv"), index=False)
print("готово: results/F2_trend_tr.csv")
