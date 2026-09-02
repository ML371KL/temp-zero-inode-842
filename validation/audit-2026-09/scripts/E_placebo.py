"""E_algopack шаг 4c: плата за перебор. Семейный плацебо-тест сетки оверлеев: все сигналы одновременно
сдвигаются по кругу на случайный лаг (>=126 дн), сетка (сигнал x порог x тип x частота, lag1) пересчитывается,
берётся МАКСИМУМ прироста избыточного Шарпа. p_fw = доля сдвигов, где макс. плацебо >= наблюдаемого макс.
Плюс индивидуальные плацебо-p для лучших вариантов. Выход: results/E_placebo.csv."""
import sys, os
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
R = os.path.join(ROOT, "results")
import importlib.util
spec = importlib.util.spec_from_file_location("E_strategy", os.path.join(ROOT, "scripts", "E_strategy.py"))
# импортируем окружение стратегии без повторной печати (перенаправим stdout)
import io, contextlib
mod = importlib.util.module_from_spec(spec)
with contextlib.redirect_stdout(io.StringIO()):
    spec.loader.exec_module(mod)

idx, r_long, r_flat = mod.idx, mod.r_long, mod.r_flat
pos_panel_d, pos_panel_m = mod.pos_panel_d, mod.pos_panel_m
CANDS, oriented, month_end_hold = mod.CANDS, mod.oriented, mod.month_end_hold
COST, START, END = mod.COST, mod.START, mod.END
LAG = 1
rl = r_long.values; rf = r_flat.values
inwin = ((idx >= START) & (idx <= END))


def sharpe_ex_fast(pos_arr, a_mask):
    pos = np.nan_to_num(pos_arr)
    pos = np.concatenate([[0.0] * LAG, pos[:-LAG]]) if LAG else pos
    prev = np.concatenate([[0.0], pos[:-1]])
    ret = prev * rl + (1 - prev) * rf - COST * np.abs(pos - prev)
    x = (ret - rf)[a_mask]
    x = x[~np.isnan(x)]
    return x.mean() / x.std() * np.sqrt(252) if len(x) > 120 and x.std() > 0 else np.nan


def grid_for(S_by_sig):
    """S_by_sig: dict sid -> ориентированный сигнал (Series). Возвращает dict variant -> d_sharpe_ex."""
    out = {}
    for sid, S in S_by_sig.items():
        first = S.dropna().index[0]
        a_mask = inwin & (idx >= max(pd.Timestamp(START), first))
        for freq in ("daily", "monthly"):
            Sf = S if freq == "daily" else month_end_hold(S)
            base = pos_panel_d if freq == "daily" else pos_panel_m
            bsh = sharpe_ex_fast(base.values, a_mask)
            sv = Sf.values; bv = base.values
            for k in (0.5, 1.0, 1.5):
                for kind in ("veto", "entry", "both", "solo"):
                    if kind == "solo" and k != 0.5:
                        continue
                    if kind == "veto":
                        pos = bv * (sv > -k)
                    elif kind == "entry":
                        pos = ((bv > 0) | (sv > k)).astype(float)
                    elif kind == "both":
                        pos = (((bv > 0) | (sv > k)) & (sv > -k)).astype(float)
                    else:
                        pos = (sv > 0).astype(float)
                    pos = np.where(np.isnan(sv), bv, pos)
                    out[(sid, kind, k, freq)] = sharpe_ex_fast(pos, a_mask) - bsh
    return out


S0 = {sid: oriented(sid, sgn, is_z) for sid, sgn, is_z in CANDS}
obs = grid_for(S0)
obs_s = pd.Series(obs)
obs_max = obs_s.max()
print(f"вариантов в сетке: {len(obs)}; наблюдаемый макс. прирост изб. Шарпа (lag1): {obs_max:.3f} у {obs_s.idxmax()}")

rng = np.random.default_rng(7)
N = int(sys.argv[1]) if len(sys.argv) > 1 else 300
maxes = np.empty(N)
per_var = {k: 0 for k in obs}
for i in range(N):
    Sp = {}
    for sid, S in S0.items():
        v = S.values.copy(); ok = ~np.isnan(v)
        sub = v[ok]
        shift = rng.integers(126, len(sub) - 126)
        v[ok] = np.roll(sub, shift)          # сдвиг только внутри доступной истории сигнала
        Sp[sid] = pd.Series(v, index=S.index)
    g = grid_for(Sp)
    gs = pd.Series(g)
    maxes[i] = gs.max()
    for k in obs:
        if g.get(k, np.nan) >= obs[k]:
            per_var[k] += 1
    if (i + 1) % 50 == 0:
        print(f"  плацебо {i+1}/{N}: медиана макс {np.nanmedian(maxes[:i+1]):.3f}, 95% {np.nanpercentile(maxes[:i+1], 95):.3f}", flush=True)

p_fw = (maxes >= obs_max).mean()
res = pd.DataFrame([dict(signal=k[0], kind=k[1], k=k[2], freq=k[3], d_sharpe_ex=round(v, 3),
                         p_placebo=round(per_var[k] / N, 3)) for k, v in obs.items()])
res["p_fw_max"] = np.nan
res.loc[res.d_sharpe_ex.idxmax(), "p_fw_max"] = p_fw
res = res.sort_values("d_sharpe_ex", ascending=False)
res.to_csv(os.path.join(R, "E_placebo.csv"), index=False)
print(f"\nСемейный плацебо: наблюдаемый макс {obs_max:.3f}; распределение макс. по {N} сдвигам: "
      f"медиана {np.median(maxes):.3f}, 90% {np.percentile(maxes, 90):.3f}, 95% {np.percentile(maxes, 95):.3f}; p_fw = {p_fw:.3f}")
print("Лучшие 15 вариантов с индивидуальным плацебо-p:")
print(res.head(15).to_string(index=False))
print("\nПо сигналам: доля вариантов с плацебо-p < 0.05:")
print(res.groupby("signal").apply(lambda x: pd.Series(dict(n_sig=(x.p_placebo < 0.05).sum(), n=len(x), best=x.d_sharpe_ex.max(),
                                                          best_p=x.loc[x.d_sharpe_ex.idxmax(), "p_placebo"]))).sort_values("best", ascending=False).to_string())
