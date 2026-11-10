"""«Что в цене» (compute/priced.py, аудит 10.11.2026, этап 3).

Приёмка задания: (а) вклады и остаток в сумме дают ход индекса ТОЧНО; (б) заглядывания
вперёд нет — разложение на усечённой истории совпадает с полным на всех общих днях, как
у ядра; плюс скользящая регрессия равна прямой МНК на своём окне и склейка нефти. Пункты
(в) и (г) меряются на копии боевого стора (audit-842-2026-11-10/work-exec/eval_priced.py):
разрядка их не прошла и выключена (priced.DETENTE), её расчёт проверяется здесь же —
на будущее включение.

Синтетика — детерминированный генератор, даты фиксированы.
"""

import unittest
from datetime import date, timedelta

from tests import need


class Lcg:
    def __init__(self, seed):
        self.s = seed

    def u(self):
        self.s = (self.s * 6364136223846793005 + 1442695040888963407) % (1 << 64)
        return (self.s >> 11) / float(1 << 53)

    def n(self):
        return sum(self.u() for _ in range(12)) - 6.0


def weekdays(start, n):
    out, d = [], date.fromisoformat(start)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def synthetic(n=520, seed=7):
    rnd = Lcg(seed)
    dates = weekdays("2024-01-08", n)
    y10, oil, idx = [10.0], [5000.0], [2500.0]
    stocks = {f"S{k:02d}": [100.0] for k in range(15)}
    sens = {k: (i - 7) / 7.0 for i, k in enumerate(sorted(stocks))}
    events = [(dates[t], s) for t, s in ((300, -1), (330, 1), (360, -1), (400, 1), (440, -1))]
    ev_idx = {300: -1, 330: 1, 360: -1, 400: 1, 440: -1}
    for t in range(1, n):
        dy = 0.05 * rnd.n()
        do = 0.015 * rnd.n()
        news = 0.02 * ev_idx.get(t, 0) + 0.004 * rnd.n()
        r = -0.05 * dy + 0.05 * do + news + 0.008 * rnd.n()
        y10.append(y10[-1] + dy)
        oil.append(oil[-1] * (1 + do))
        idx.append(idx[-1] * (1 + r))
        for k, px in stocks.items():
            px.append(px[-1] * (1 + r + sens[k] * news + 0.01 * rnd.n()))
    stocks["S03"][200] = None   # пропуск у бумаги
    oil[100] = None             # пропуск у нефти
    return dates, idx, y10, oil, stocks, events


class Base(unittest.TestCase):
    def setUp(self):
        self.Q = need(self, "pipeline.compute.priced", "decompose", "summary", "rolling_ols",
                      "oil_rub_series", "DETENTE")
        self.data = synthetic()


class TestExactAndNoLookahead(Base):
    def test_а_вклады_и_остаток_дают_ход_точно(self):
        """Мутация: остаток считать от регрессии, а не как ход минус вклады → сумма плывёт."""
        dates, idx, y10, oil, stocks, ev = self.data
        for det in (False, True):
            res = self.Q.decompose(dates, idx, y10, oil, stocks, ev, window=150, with_detente=det)
            for t, r in enumerate(res["r"]):
                if r is None:
                    continue
                total = res["c_rates"][t] + res["c_oil"][t] + res["c_det"][t] + res["other"][t]
                # Точно — до округления двойной точности (один-два ulp).
                self.assertAlmostEqual(r, total, delta=1e-15, msg=f"день {dates[t]}, разрядка={det}")

    def test_б_усечённая_история_совпадает_с_полной(self):
        """Мутация: регрессия или чувствительность бумаг берут данные ПОСЛЕ дня (все
        события реестра, а не прошлые; окно вперёд) — разложение на полной истории
        расходится с усечённой."""
        dates, idx, y10, oil, stocks, ev = self.data
        for det in (False, True):
            full = self.Q.decompose(dates, idx, y10, oil, stocks, ev, window=150, with_detente=det)
            for cut in (320, 380, 450):
                part = self.Q.decompose(dates[:cut], idx[:cut], y10[:cut], oil[:cut],
                                        {k: v[:cut] for k, v in stocks.items()}, ev,
                                        window=150, with_detente=det)
                for key in ("c_rates", "c_oil", "c_det", "other", "basket"):
                    self.assertEqual(full[key][:cut], part[key], f"{key}, срез {cut}, разрядка={det}")

    def test_вклад_дня_по_вчерашнему_коэффициенту(self):
        """Мутация: окно регрессии включает сегодняшний день (t вместо t−1) — изменение
        хода индекса в день t меняет вклады этого же дня. Усечённая история такого не
        ловит (сегодня есть в обеих), поэтому отдельная проверка."""
        dates, idx, y10, oil, stocks, ev = self.data
        base = self.Q.decompose(dates, idx, y10, oil, stocks, ev, window=150, with_detente=True)
        t = 470
        bumped = list(idx)
        for j in range(t, len(bumped)):
            bumped[j] = bumped[j] * 1.03   # +3 % ровно в день t, дальше тот же ход
        alt = self.Q.decompose(dates, bumped, y10, oil, stocks, ev, window=150, with_detente=True)
        for key in ("c_rates", "c_oil", "c_det"):
            self.assertEqual(base[key][:t + 1], alt[key][:t + 1], key)
        self.assertNotEqual(base["other"][t], alt["other"][t])

    def test_разрядка_на_синтетике_ловит_новости(self):
        # В синтетике новости реально двигают чувствительные бумаги: корзина обязана
        # появиться после третьего события и нести положительный коэффициент.
        dates, idx, y10, oil, stocks, ev = self.data
        res = self.Q.decompose(dates, idx, y10, oil, stocks, ev, window=150, with_detente=True)
        first = next(t for t, v in enumerate(res["basket"]) if v is not None)
        self.assertEqual(first, 361, "корзина — со дня после реакции на третье событие")
        beta = next(b for b in reversed(res["beta_det"]) if b is not None)
        self.assertGreater(beta[1], 0)

    def test_по_умолчанию_разрядка_выключена(self):
        # Приёмка 10.11.2026 не пройдена (докстринг модуля): включает только новое решение.
        self.assertFalse(self.Q.DETENTE)
        dates, idx, y10, oil, stocks, ev = self.data
        res = self.Q.decompose(dates, idx, y10, oil, stocks, ev, window=150)
        self.assertTrue(all(c == 0.0 for c in res["c_det"]))


class TestPieces(Base):
    def test_скользящая_регрессия_равна_прямой_на_своём_окне(self):
        dates, idx, y10, oil, stocks, ev = self.data
        r = self.Q.log_returns(idx)
        fr, fo = self.Q.diffs(y10), self.Q.log_returns(oil)
        betas = self.Q.rolling_ols(r, [fr, fo], window=150, min_obs=120)
        t = 400
        rows = [(1.0, fr[j], fo[j], r[j]) for j in range(t - 150, t)
                if None not in (fr[j], fo[j], r[j])]
        xtx = [[sum(a[i] * a[k] for a in rows) for k in range(3)] for i in range(3)]
        xty = [sum(a[i] * a[3] for a in rows) for i in range(3)]
        direct = self.Q._solve(xtx, xty)
        for a, b in zip(betas[t], direct):
            self.assertAlmostEqual(a, b, places=9)
        self.assertIsNone(betas[100], "окно не набралось — коэффициента нет")

    def test_сумма_окна_и_доля_объяснённого(self):
        dates, idx, y10, oil, stocks, ev = self.data
        res = self.Q.decompose(dates, idx, y10, oil, stocks, ev, window=150)
        wins = self.Q.summary(dates, res)
        self.assertEqual([w["sessions"] for w in wins], [5, 21, 63])
        for w in wins:
            parts = w["rates_lp"] + w["oil_lp"] + w["detente_lp"] + w["other_lp"]
            self.assertAlmostEqual(parts, w["index_lp"], delta=0.03)
            self.assertGreaterEqual(w["explained"], 0)
            self.assertLessEqual(w["explained"], 1)

    def test_хвост_нефти_продолжен_фьючерсом(self):
        dates = weekdays("2026-11-02", 5)
        brent = {dates[0]: 100.0, dates[1]: 102.0}           # FRED кончился на втором дне
        fut = {dates[1]: 90.0, dates[2]: 99.0, dates[3]: 90.0}
        cny = [10.0] * 5
        got = self.Q.oil_rub_series(dates, brent, fut, cny)
        self.assertEqual(got[:2], [1000.0, 1020.0])
        self.assertAlmostEqual(got[2], 1020.0 * 99.0 / 90.0)
        self.assertAlmostEqual(got[3], 1020.0)
        self.assertIsNone(got[4], "у фьючерса нет точки — хвост обрывается, а не тянется")


if __name__ == "__main__":
    unittest.main()
