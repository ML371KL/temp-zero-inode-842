"""G_decision, шаг 3: плата за перебор. Сетка параметров на TRAIN 2004-01..2017-12, проверка на TEST
2018-01..2026-08; подсчёт вариантов; плацебо трёх видов; чувствительность к издержкам/лагу/лагу futoi.
Выход: results/G_search.csv, G_placebo.csv, G_sensitivity.csv
"""
import os
import sys
import math
import itertools
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from G_lib import *  # noqa
from G_rules import rule_pos, APRIORI  # noqa

INF = float("inf")
TRAIN = ("2004-01-01", "2017-12-31")
TEST = ("2018-01-01", "2026-08-31")
MAIN = ("2010-01-01", "2026-08-31")


class FastEval:
    """Быстрые метрики по окнам для массива позиций (месячная выборка, как в metrics())."""

    def __init__(self, F, cost=0.002, lag=1):
        self.F = F
        self.cost, self.lag = cost, lag
        self.r_eq = np.nan_to_num(F["mcftr"].pct_change().values)
        self.r_cash = np.nan_to_num((F["mm_rate"].shift(1).values / 100.0) / 252.0)
        per = F.index.to_period("M").asi8
        self.mstart = np.r_[0, np.where(np.diff(per) != 0)[0] + 1]
        mdates = F.index[self.mstart]
        self.win = {}
        self.dwin = {}
        for name, (a, b) in {"train": TRAIN, "test": TEST, "main": MAIN}.items():
            self.win[name] = (mdates >= pd.Timestamp(a)) & (mdates <= pd.Timestamp(b))
            self.dwin[name] = (F.index >= pd.Timestamp(a)) & (F.index <= pd.Timestamp(b))

    def returns(self, pos):
        held = np.zeros(len(pos))
        k = 1 + self.lag
        held[k:] = pos[:-k]
        dpos = np.abs(np.diff(held, prepend=0.0))
        return held * self.r_eq + (1 - held) * self.r_cash - self.cost * dpos, held

    def eval(self, pos):
        ret, held = self.returns(pos)
        lr = np.log1p(ret)
        m = np.exp(np.add.reduceat(lr, self.mstart)) - 1
        out = {}
        for name, mask in self.win.items():
            x = m[mask]
            s = x.std(ddof=1)
            out[f"sharpe_{name}"] = x.mean() / s * math.sqrt(12) if s > 0 else np.nan
            dm = self.dwin[name]
            eq = np.cumprod(1 + ret[dm])
            out[f"maxdd_{name}"] = (eq / np.maximum.accumulate(eq) - 1).min()
            yrs = dm.sum() / 252.0
            out[f"cagr_{name}"] = eq[-1] ** (1 / yrs) - 1
            out[f"tim_{name}"] = held[dm].mean()
            out[f"sw_{name}"] = (np.abs(np.diff(held[dm])) > 1e-9).sum() / yrs
        return out


def grid_specs():
    """Сетка семейства; каждая запись = (класс, параметры). Пороги композита — с шагом 0.15."""
    specs = []
    ths = [-0.6, -0.45, -0.3, -0.15, 0.0, 0.15, 0.3, 0.45, 0.6]
    for mh in (0, 5, 21):
        specs.append(("R0", {"min_hold": mh}))
    specs.append(("R0", {"monthly": True}))
    for t_in in ths:
        for t_out in ths:
            if t_out > t_in:
                continue
            for mh in (0, 5, 21):
                for de in (False, True):
                    th, h = (t_in + t_out) / 2, (t_in - t_out) / 2
                    specs.append(("R1", {"th": th, "h": h, "min_hold": mh, "daily_exit": de,
                                         "t_in": t_in, "t_out": t_out}))
    for lam in (0.25, 0.5, 1.0, 2.0):
        for ti in (-0.3, 0.0, 0.3):
            for gap in (0.2, 0.4):
                for mh in (0, 21):
                    specs.append(("R2sum", {"lam": lam, "t_in": ti, "t_out": ti - gap, "min_hold": mh}))
    for k in (1, 2, 3):
        for ex in (False, True):
            for mh in (0, 5, 21):
                specs.append(("R2veto", {"k": k, "exit_on_veto": ex, "min_hold": mh}))
    for exits in ((), ("bond",), ("vol",), ("bond", "vol"), ("bear",)):
        for k_ok in (2, 3):
            for ti in (0.0, 0.3):
                for t_alt in (0.3, 0.6, INF):
                    for t_exit in (-0.3, -0.6, -INF):
                        for mh in (0, 21):
                            specs.append(("R3", {"exits": exits, "k_ok": k_ok, "t_in": ti, "t_alt": t_alt,
                                                 "t_exit": t_exit, "min_hold": mh}))
    for th in (-0.3, 0.0, 0.3):
        for h in (0.0, 0.1, 0.2):
            for mh in (0, 5, 21):
                specs.append(("R4", {"th": th, "h": h, "min_hold": mh}))
    for tf in (0.0, 0.3, 0.45):
        for th_ in (-0.3, -0.6, 0.0):
            for mh in (0, 21):
                specs.append(("R5", {"t_full": tf, "t_half": th_, "min_hold": mh}))
    return specs


def fmt_params(p):
    return ";".join(f"{k}={v}" for k, v in p.items() if k not in ("th", "h"))


def run_grid(F, ev, specs, votes=None):
    rows = []
    for name, p in specs:
        pos = rule_pos(F, name, p, votes=votes)
        r = ev.eval(pos)
        rows.append({"class": name, "params": fmt_params(p), **r})
    return pd.DataFrame(rows)


def shifted_features(F, k_days, what):
    """Плацебо: циклический сдвиг признаков на k_days торговых дней. what='comp' — только композит
    (ворота настоящие), 'all' — и биты состояний тоже (полностью случайное правило того же класса)."""
    G = F.copy()
    cols = ["comp_closed", "sign_closed", "comp_daily"]
    if what == "all":
        cols += ["gate", "toxic", "window", "n_ok", "st_trend", "st_vol", "st_bond", "n_against", "vote_avg"]
    for ccol in cols:
        G[ccol] = np.roll(F[ccol].values, k_days)
    return G


if __name__ == "__main__":
    d, c, m = load_raw()
    F = build_features(d, c, m).loc["2003-06-01":]
    ev = FastEval(F, cost=0.002, lag=1)
    specs = grid_specs()
    by_class = pd.Series([s[0] for s in specs]).value_counts().to_dict()
    print("вариантов в сетке:", len(specs), by_class)

    S = run_grid(F, ev, specs)
    S.to_csv(os.path.join(RES, "G_search.csv"), index=False, float_format="%.4f")
    base = S[(S["class"] == "R0")]
    print("\nбазовые R0:")
    print(base[["params", "sharpe_train", "sharpe_test", "sharpe_main", "maxdd_test", "tim_test", "sw_test"]]
          .to_string(index=False, float_format=lambda x: f"{x:.3f}"))
    print("\n=== лучший по TRAIN в каждом классе → его TEST (и лучший по TEST для сравнения) ===")
    summ = []
    for cls, g in S.groupby("class"):
        bt = g.loc[g["sharpe_train"].idxmax()]
        bte = g.loc[g["sharpe_test"].idxmax()]
        summ.append({"class": cls, "n_variants": len(g), "best_train_params": bt["params"],
                     "train_sharpe": bt["sharpe_train"], "its_test_sharpe": bt["sharpe_test"],
                     "its_test_maxdd": bt["maxdd_test"], "its_main_sharpe": bt["sharpe_main"],
                     "best_test_params": bte["params"], "best_test_sharpe": bte["sharpe_test"],
                     "median_test_sharpe": g["sharpe_test"].median(),
                     "share_test_beats_R0h0": float((g["sharpe_test"] > base.iloc[0]["sharpe_test"]).mean())})
    SM = pd.DataFrame(summ)
    SM.to_csv(os.path.join(RES, "G_search_summary.csv"), index=False, float_format="%.4f")
    print(SM.to_string(index=False, float_format=lambda x: f"{x:.3f}"))

    # --- R1: карта порогов (train / test) для отчёта
    R1 = S[S["class"] == "R1"].copy()
    R1["t_in"] = R1["params"].str.extract(r"t_in=([-\d.]+)").astype(float)
    R1["t_out"] = R1["params"].str.extract(r"t_out=([-\d.]+)").astype(float)
    R1["mh"] = R1["params"].str.extract(r"min_hold=(\d+)").astype(int)
    R1["de"] = R1["params"].str.contains("daily_exit=True")
    sub = R1[(R1.mh == 0) & (~R1.de)]
    print("\nR1 карта (min_hold=0, выход по закрытому): Sharpe TRAIN (строки t_in, столбцы t_out)")
    print(sub.pivot(index="t_in", columns="t_out", values="sharpe_train").round(2).to_string())
    print("Sharpe TEST:")
    print(sub.pivot(index="t_in", columns="t_out", values="sharpe_test").round(2).to_string())
    print("Sharpe MAIN 2010+:")
    print(sub.pivot(index="t_in", columns="t_out", values="sharpe_main").round(2).to_string())
    print("MaxDD TEST:")
    print(sub.pivot(index="t_in", columns="t_out", values="maxdd_test").round(2).to_string())

    # --- плацебо
    rng = np.random.default_rng(11)
    r1_specs = [s for s in specs if s[0] in ("R1",)]
    prow = []
    N = len(F)
    for kind in ("comp", "all"):
        for it in range(60):
            k = int(rng.integers(252, N - 252))
            G = shifted_features(F, k, kind)
            evG = FastEval(G, cost=0.002, lag=1)
            P = run_grid(G, evG, r1_specs)
            bt = P.loc[P["sharpe_train"].idxmax()]
            r0 = evG.eval(rule_pos(G, "R0", {"min_hold": 0}))
            prow.append({"placebo": f"shift_{kind}", "draw": it, "shift_days": k,
                         "max_train_sharpe": bt["sharpe_train"], "test_of_train_best": bt["sharpe_test"],
                         "max_test_sharpe": P["sharpe_test"].max(), "max_main_sharpe": P["sharpe_main"].max(),
                         "R0_train": r0["sharpe_train"], "R0_test": r0["sharpe_test"], "R0_main": r0["sharpe_main"]})
        print(f"плацебо {kind}: готово")
    # случайные знаки второго ряда: все 128 комбинаций для R2 (sum+veto)
    r2_specs = [s for s in specs if s[0] in ("R2sum", "R2veto")]
    for signs in itertools.product((-1, 1), repeat=7):
        votes = recompute_votes(F, signs)
        P = run_grid(F, ev, r2_specs, votes=votes)
        bt = P.loc[P["sharpe_train"].idxmax()]
        prow.append({"placebo": "signs_R2", "draw": "".join("+" if s > 0 else "-" for s in signs), "shift_days": 0,
                     "max_train_sharpe": bt["sharpe_train"], "test_of_train_best": bt["sharpe_test"],
                     "max_test_sharpe": P["sharpe_test"].max(), "max_main_sharpe": P["sharpe_main"].max(),
                     "R0_train": base.iloc[0]["sharpe_train"], "R0_test": base.iloc[0]["sharpe_test"],
                     "R0_main": base.iloc[0]["sharpe_main"]})
    PL = pd.DataFrame(prow)
    PL.to_csv(os.path.join(RES, "G_placebo.csv"), index=False, float_format="%.4f")
    print("\n=== плацебо: ожидаемый максимум Шарпа случайного правила того же класса ===")
    agg = PL.groupby("placebo").agg(n=("draw", "size"),
                                    mean_max_train=("max_train_sharpe", "mean"), p95_max_train=("max_train_sharpe", lambda x: x.quantile(.95)),
                                    mean_test_of_best=("test_of_train_best", "mean"), p95_test_of_best=("test_of_train_best", lambda x: x.quantile(.95)),
                                    mean_max_test=("max_test_sharpe", "mean"), p95_max_test=("max_test_sharpe", lambda x: x.quantile(.95)),
                                    mean_max_main=("max_main_sharpe", "mean"), p95_max_main=("max_main_sharpe", lambda x: x.quantile(.95)),
                                    mean_R0_test=("R0_test", "mean"))
    print(agg.to_string(float_format=lambda x: f"{x:.3f}"))
    real_signs = tuple(s["sign"] for s in SECOND_LAYER)
    key = "".join("+" if s > 0 else "-" for s in real_signs)
    r = PL[(PL.placebo == "signs_R2") & (PL.draw == key)].iloc[0]
    rank = int((PL[PL.placebo == "signs_R2"]["max_train_sharpe"] > r["max_train_sharpe"]).sum()) + 1
    print(f"настоящие знаки {key}: max_train={r['max_train_sharpe']:.3f} (ранг {rank}/128), test_of_best={r['test_of_train_best']:.3f}")

    # --- чувствительность: издержки, лаг исполнения, лаг futoi (10 торг. дней ≈ 14 календ.)
    srow = []
    sens_specs = [s for s in APRIORI if s[0] in ("R0_h0", "R0_h21", "R1_th+0.3", "R2veto_k2_exit", "R5_frac", "R4_h0")] + \
                 [("R1_in0.45_out0.15", "R1", {"th": 0.3, "h": 0.15, "min_hold": 0})]
    for cost in (0.001, 0.002, 0.003):
        for lag in (0, 1):
            evS = FastEval(F, cost=cost, lag=lag)
            for label, name, p in sens_specs:
                for fsh in (0, 10):
                    if fsh and name not in ("R2veto", "R2sum", "R5"):
                        continue
                    votes = recompute_votes(F, real_signs, futoi_shift=fsh) if fsh else None
                    r = evS.eval(rule_pos(F, name, p, votes=votes))
                    srow.append({"rule": label, "cost": cost, "lag": lag, "futoi_shift": fsh,
                                 **{k: v for k, v in r.items() if k.startswith(("sharpe", "maxdd", "cagr", "sw_"))}})
    SE = pd.DataFrame(srow)
    SE.to_csv(os.path.join(RES, "G_sensitivity.csv"), index=False, float_format="%.4f")
    print("\n=== чувствительность (Sharpe main 2010+) ===")
    print(SE.pivot_table(index="rule", columns=["cost", "lag", "futoi_shift"], values="sharpe_main").round(2).to_string())
    print("\nsaved results/G_search.csv, G_search_summary.csv, G_placebo.csv, G_sensitivity.csv")
