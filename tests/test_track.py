"""Журнал позиции (pipeline/compute/track.py): деньги правила в той мере, в какой оно решает.

Мера — та же, что во всех бэктестах аудитов, и каждая её деталь закреплена здесь
отдельной проверкой, потому что каждая молча меняет ответ на вопрос «работает ли
правило»: исполнение на следующем закрытии (решение дня t зарабатывает день t+2),
комиссия за смену, вклады по календарным дням, индекс ПОЛНОЙ доходности.
"""

import math
import unittest
from datetime import date, timedelta

from tests import need, panel_small


def weekdays(start, n):
    out, cur = [], date.fromisoformat(start)
    while len(out) < n:
        if cur.weekday() < 5:
            out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


class TrackCase(unittest.TestCase):
    def setUp(self):
        self.t = need(self, "pipeline.compute.track", "daily_rows", "summarize",
                      "monthly_hits", "wilson", "months_back", "journal", "FREEZE",
                      "SWITCH_COST")


class TestDailyRows(TrackCase):
    def test_решение_дня_t_зарабатывает_день_t_плюс_2(self):
        dates = weekdays("2026-09-07", 6)              # пн..пн
        pos = [0, 0, 1, 1, 1, 1]                        # решено «акции» по закрытию d2
        tr = [100.0, 100.0, 100.0, 110.0, 121.0, 133.1]
        rows = self.t.daily_rows(dates, pos, tr, [0.0] * 6)
        by_day = {r[0]: r for r in rows}
        # d3: исполнение ещё не случилось (закрытие d3 — момент исполнения)
        # мутация: lag=0 -> правило «поймало бы» +10% дня d3
        self.assertEqual(by_day[dates[3]][1], 0)
        self.assertAlmostEqual(by_day[dates[3]][2], 0.0)
        # d4: первый день в акциях, комиссия в день смены
        self.assertEqual(by_day[dates[4]][1], 1)
        self.assertAlmostEqual(by_day[dates[4]][2], 0.10 - self.t.SWITCH_COST)
        self.assertAlmostEqual(by_day[dates[5]][2], 0.10)

    def test_вклады_по_календарным_дням(self):
        dates = ["2026-09-11", "2026-09-14"]            # пятница -> понедельник
        rows = self.t.daily_rows(["2026-09-10"] + dates, [0, 0, 0], [1.0, 1.0, 1.0],
                                 [10.0, 10.0, 10.0])
        mon = [r for r in rows if r[0] == "2026-09-14"][0]
        # мутация: начислять за торговый день, а не за три календарных
        self.assertAlmostEqual(mon[4], 1.10 ** (3 / 365) - 1, places=12)

    def test_пропуск_индекса_протягивается(self):
        dates = weekdays("2026-09-07", 5)
        rows = self.t.daily_rows(dates, [1] * 5, [100.0, 100.0, None, 105.0, 105.0],
                                 [0.0] * 5)
        rets = {r[0]: r[3] for r in rows}
        self.assertAlmostEqual(rets[dates[2]], 0.0)
        self.assertAlmostEqual(rets[dates[3]], 0.05)

    def test_без_ставки_дней_нет(self):
        dates = weekdays("2026-09-07", 5)
        self.assertEqual(self.t.daily_rows(dates, [0] * 5, [1.0] * 5, [None] * 5), [])


class TestSummarize(TrackCase):
    def test_годовой_темп_только_для_окна_от_года(self):
        dates = weekdays("2026-01-05", 30)
        rows = self.t.daily_rows(dates, [1] * 30, [100.0 + i for i in range(30)], [10.0] * 30)
        s = self.t.summarize(rows)
        self.assertNotIn("rule_ann_pct", s)   # мутация: печатать годовой темп за месяц
        self.assertNotIn("ex_sharpe", s)      # Шарп по 30 дням — шум
        long = weekdays("2025-01-06", 300)
        rows = self.t.daily_rows(long, [1] * 300, [100.0 * 1.001 ** i for i in range(300)],
                                 [10.0] * 300)
        s = self.t.summarize(rows)
        self.assertIn("rule_ann_pct", s)
        self.assertIn("ex_sharpe", s)

    def test_итоги_просадка_и_смены(self):
        dates = weekdays("2026-09-07", 6)
        pos = [1, 1, 1, 0, 0, 0]
        tr = [100.0, 100.0, 90.0, 99.0, 99.0, 99.0]
        rows = self.t.daily_rows(dates, pos, tr, [0.0] * 6)
        s = self.t.summarize(rows, lo=dates[0])
        # держали акции d2 (−10%), d3 (+10%), d4 (0%); d5 — деньги и комиссия смены
        want = 0.9 * 1.1 * (1 - self.t.SWITCH_COST) - 1
        self.assertAlmostEqual(s["rule_pct"], round(want * 100, 2))
        self.assertAlmostEqual(s["tr_pct"], -1.0)
        self.assertEqual(s["switches"], 1)
        self.assertEqual(s["mdd_pct"], -10.0)

    def test_окно_исключает_левую_границу(self):
        dates = weekdays("2026-09-07", 5)
        rows = self.t.daily_rows(dates, [0] * 5, [1.0] * 5, [10.0] * 5)
        s = self.t.summarize(rows, lo=dates[2])
        self.assertEqual(s["days"], 2)
        self.assertIsNone(self.t.summarize(rows, lo=dates[-1]))


class TestHits(TrackCase):
    def test_интервал_уилсона(self):
        self.assertEqual(self.t.wilson(12, 24), (0.314, 0.686))
        self.assertEqual(self.t.wilson(0, 0), (None, None))
        lo, hi = self.t.wilson(0, 5)
        self.assertEqual(lo, 0.0)            # у малых n интервал не уходит ниже нуля

    def test_верный_месяц_против_зеркала(self):
        rows = [("2026-01-15", 1, 0.0, 0.05, 0.01),   # акции и рынок выше вклада — верно
                ("2026-02-16", 1, 0.0, -0.05, 0.01),  # акции, рынок ниже — неверно
                ("2026-03-16", 0, 0.0, -0.05, 0.01),  # деньги, рынок ниже — верно
                ("2026-04-15", 0, 0.0, 0.05, 0.01)]   # вне окна
        hit = self.t.monthly_hits(rows, "2026-01", "2026-03")
        self.assertEqual((hit["months"], hit["right"]), (3, 2))
        self.assertEqual(hit["ci95"], list(self.t.wilson(2, 3)))


class TestMonthsBack(TrackCase):
    def test_конец_месяца_и_високосный_год(self):
        self.assertEqual(self.t.months_back("2026-03-31", 1), "2026-02-28")
        self.assertEqual(self.t.months_back("2024-03-31", 1), "2024-02-29")
        self.assertEqual(self.t.months_back("2026-01-15", 1), "2025-12-15")
        self.assertEqual(self.t.months_back("2026-10-02", 24), "2024-10-02")


class TestJournal(TrackCase):
    def test_окна_и_сделки_с_заморозки(self):
        dates = weekdays("2025-08-01", 320)
        n = len(dates)
        k = dates.index("2026-09-10")
        pos = [0] * k + [1] * (n - k)
        reasons = [""] * n
        reasons[k] = "gate_open"
        reasons[10] = "comp_neg"                       # до заморозки — не сделка журнала
        j = self.t.journal(dates, pos, reasons, [100.0 + i for i in range(n)], [12.0] * n)
        self.assertEqual(j["freeze"], self.t.FREEZE)
        self.assertEqual([w["id"] for w in j["windows"]], ["since_freeze", "m12", "m24"])
        self.assertEqual(j["trades"], [{"decided": "2026-09-10", "executed": "2026-09-11",
                                        "to": "long", "reason": "gate_open"}])
        since = j["windows"][0]
        self.assertEqual(since["from"], self.t.FREEZE)
        self.assertEqual(since["switches"], 1)
        self.assertIn("MCFTR", j["basis"])

    def test_без_индекса_полной_доходности_журнала_нет(self):
        dates = weekdays("2026-09-01", 10)
        self.assertIsNone(self.t.journal(dates, [0] * 10, [""] * 10, None, [12.0] * 10))


class TestDecisionCarriesJournal(unittest.TestCase):
    def test_позиция_несёт_журнал_по_mcftr_и_вкладам(self):
        dec = need(self, "pipeline.compute.decision", "compute_decision")
        core = need(self, "pipeline.compute.core", "monthly_frame")
        p = panel_small()
        n = len(p["dates"])
        cols = dict(p["cols"])
        # Индекс полной доходности растёт, ценовой стоит: журнал обязан брать первый.
        cols["mcftr"] = [1000.0 * 1.0004 ** i for i in range(n)]
        cols["imoex"] = [3000.0] * n
        cols["deposit"] = [15.0] * n
        panel = {"dates": p["dates"], "cols": cols}
        out = dec.compute_decision(panel, core.monthly_frame(panel))
        j = out["position"]["journal"]
        ids = [w["id"] for w in j["windows"]]
        self.assertNotIn("since_freeze", ids)     # панель кончается до заморозки
        self.assertIn("m12", ids)
        m12 = [w for w in j["windows"] if w["id"] == "m12"][0]
        # мутация: журнал по cols["imoex"] -> рынок за год 0%
        self.assertGreater(m12["tr_pct"], 5.0)
        self.assertTrue(math.isclose(m12["cash_ann_pct"], 15.0, abs_tol=0.2))


if __name__ == "__main__":
    unittest.main()
