"""F2_pm §4: риск-менеджмент поверх long/flat — стоп по просадке позиции, трейлинг-стоп,
лимит времени во флэте, частичный вход/градация экспозиции. Быстрые проверки на истории.
Запуск: python scripts/F2_risk.py (из audit/) — после F2_switching.py (нужен F2_rules_positions.csv)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from F2_lib import *

d = load_daily()
m = load_monthly()
core = core_sign_daily(d, m)
have = d["cell"].notna() & core.notna()
gate = d["gate_open"].astype(bool)
POS = pd.read_csv(os.path.join(RES, "F2_rules_positions.csv"), index_col=0, parse_dates=True)
BASES = {"PANEL_D": POS["PANEL_D: ворота(день)+ядро(закр.мес)"],
         "SM2": POS["SM2: HYST(−3%,±2%,q60) + дебаунс 5д + удерж. 21д"]}
px = d["mcftr_ffill"].where(d["mcftr_ffill"].notna(), d["imoex"])


def overlay(sig, kind, x=0.10, cooldown=21, T=126):
    """kind: 'stop' — стоп от цены входа; 'trail' — от максимума с входа; 'flatlimit' — принудительный
    вход, если базовое правило держит флэт дольше T дней, а индекс выше уровня выхода."""
    s = sig.astype(float).values; p = px.values
    out = np.zeros(len(s)); n_trig = 0; regret = 0
    in_pos = 0.0; entry = np.nan; peak = np.nan; cd = 0; flat_days = 0; exit_lvl = np.nan; forced = False
    for i in range(len(s)):
        want = s[i]
        if kind in ("stop", "trail"):
            if cd > 0:
                cd -= 1; want = 0.0
            if in_pos == 0 and want == 1:
                entry = p[i]; peak = p[i]
            elif in_pos == 1 and want == 1:
                peak = max(peak, p[i])
                ref = entry if kind == "stop" else peak
                if p[i] / ref - 1 < -x:
                    want = 0.0; cd = cooldown; n_trig += 1
                    # сожаление: индекс через 21 день выше уровня стопа?
                    j = min(len(p) - 1, i + 21)
                    if p[j] > p[i]: regret += 1
        elif kind == "flatlimit":
            if in_pos == 1 and want == 0:
                exit_lvl = p[i]; flat_days = 0; forced = False
            if want == 0 and in_pos == 0:
                flat_days += 1
                if flat_days > T and p[i] > exit_lvl:
                    want = 1.0; n_trig += 1; forced = True
            if forced and s[i] == 0 and want == 1:
                pass
            if forced and in_pos == 1 and s[i] == 0:
                want = 1.0  # держим принудительный лонг, пока базовое правило не станет лонгом и снова флэтом
            if forced and s[i] == 1:
                forced = False
        out[i] = want; in_pos = want
    return pd.Series(out, index=sig.index), n_trig, regret


rows = []
win = [("2010-2026.08 (осн.)", "2010-01-01", "2026-08-31"), ("2004+ (полная)", "2004-01-01", "2026-08-31"),
       ("2022.03-2024", "2022-03-24", "2024-12-31"), ("2025-2026.08", "2025-01-01", "2026-08-31")]


def add(nm, sig, trig=None, regret=None):
    for w, a, b in win:
        bh, _ = bench(d, a, b)
        mt = metrics(backtest(d, sig, 0.002, a, b), d, bh)
        mt.update({"rule": nm, "window": w, "n_trig": trig, "regret": regret})
        rows.append(mt)


for bname, bsig in BASES.items():
    add(f"{bname} (база)", bsig)
    for x in (0.05, 0.08, 0.10, 0.15):
        s, t, r = overlay(bsig, "stop", x); add(f"{bname} + стоп от входа {int(x*100)}%", s, t, r)
    for x in (0.08, 0.10, 0.15):
        s, t, r = overlay(bsig, "trail", x); add(f"{bname} + трейлинг {int(x*100)}%", s, t, r)
    for T in (63, 126):
        s, t, r = overlay(bsig, "flatlimit", T=T); add(f"{bname} + лимит флэта {T}д (вход, если индекс выше уровня выхода)", s, t, r)
    # градация экспозиции
    core_pos = (core == 1)
    half = pd.Series(0.0, index=d.index)
    half[(gate & core_pos)] = 1.0
    half[(gate & ~core_pos) | (~gate & core_pos)] = 0.5
    half = half.where(have, 0.0)
    if bname == "PANEL_D":
        add("Градация 0/0.5/1: ворота&ядро=1, одно из двух=0.5", half)
        conf = pd.Series(0.0, index=d.index)
        conf[gate & core_pos] = 1.0
        conf[gate & ~core_pos] = 0.5
        conf = conf.where(have, 0.0)
        add("Градация: ворота открыты → 1 при ядре>0, 0.5 при ядре<0; токсичная → 0", conf)
        # масштабирование входа: первые 21 день после входа — половина
        v = bsig.values; sc = np.zeros(len(v)); age = 99
        for i in range(len(v)):
            if v[i] == 1:
                age = 0 if (i == 0 or v[i - 1] == 0) else age + 1
                sc[i] = 0.5 if age < 21 else 1.0
            else:
                sc[i] = 0.0
        add("Ступенчатый вход: 50% первые 21 день лонга", pd.Series(sc, index=d.index))
        # постоянная половинная экспозиция как ориентир масштаба
        add("Ориентир: 50% MCFTR / 50% деньги постоянно", pd.Series(0.5, index=d.index).where(have, 0.0))

R = pd.DataFrame(rows)[["rule", "window", "CAGR%", "vol%", "Sharpe", "Sharpe_ex_mm", "MDD%", "in_mkt%", "trades/yr",
                        "hit_m_vs_mm%", "beat_bh_years%", "n_trig", "regret"]]
R.to_csv(os.path.join(RES, "F2_risk_overlays.csv"), index=False)
pd.set_option("display.width", 260); pd.set_option("display.max_colwidth", 80)
for w, _, _ in win:
    print(f"\n=== {w} ===")
    print(R[R.window == w].drop(columns=["window"]).to_string(index=False))

# что происходило после стопов: распределение доходности MCFTR за 21/63 дня после срабатывания стопа 10% (PANEL_D)
s, t, r = overlay(BASES["PANEL_D"], "stop", 0.10)
trig_days = d.index[(BASES["PANEL_D"].values == 1) & (s.values == 0) & (np.r_[0, np.diff(s.values)] < 0)]
fw = []
for t0 in trig_days:
    i = d.index.get_loc(t0)
    for h in (21, 63):
        j = min(len(d) - 1, i + h)
        fw.append({"stop_date": t0.date(), "h": h, "fwd%": round((px.iloc[j] / px.iloc[i] - 1) * 100, 1)})
FW = pd.DataFrame(fw)
if len(FW):
    print("\n=== после стопа 10% (PANEL_D): доходность MCFTR вперёд ===")
    print(FW.pivot(index="stop_date", columns="h", values="fwd%").to_string())
    print(FW.groupby("h")["fwd%"].agg(["count", "mean", "median", lambda s: (s > 0).mean()]).round(2).to_string())
print("\nготово: results/F2_risk_overlays.csv")
