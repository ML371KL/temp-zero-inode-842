"""Бюджет времени фетча: публикация важнее последнего ряда.

Контракт прогона «панель обязана обновляться и с половиной источников» до
18.08.2026 выполнялся только при БЫСТРЫХ отказах: источник, который не отвечает
отказом, а ВИСИТ до сокет-таймаута, стоит ~93 с на ряд (30 с × 3 попытки +
бэкофф), fetch_all строго последователен, и дедлайн юнита (300 с интрадей)
пробивался ДО публикации — timeout убивал прогон, data.json не обновлялся вовсе.
Blackhole одного ISS замораживал панель целиком; сторож заметил бы через 26 ч.

Часы везде виртуальные (подменяется time.monotonic внутри lib/runbudget) —
правило набора №1: тест не смотрит на настоящие часы.
"""

import unittest
from unittest import mock

from tests import need


class Journal:
    def __init__(self):
        self.lines = []

    def line(self, tag, msg):
        self.lines.append(msg)

    def warn(self, tag, msg):
        self.lines.append("WARN " + msg)


class RunBudgetCase(unittest.TestCase):
    def setUp(self):
        self.budget = need(self, "pipeline.lib.runbudget", "arm", "arm_from_env",
                           "disarm", "exhausted", "remaining")
        self.run_mod = need(self, "pipeline.run", "fetch_all")
        self.addCleanup(self.budget.disarm)
        self.clock = {"t": 0.0}
        patcher = mock.patch.object(self.budget.time, "monotonic",
                                    lambda: self.clock["t"])
        patcher.start()
        self.addCleanup(patcher.stop)

    def slow_items(self, n):
        def hang(**kw):
            self.clock["t"] += 93.0
            raise self.run_mod.FetchError("источник висит")

        patches = [mock.patch.object(self.run_mod, "_resolve", return_value=hang),
                   mock.patch.object(self.run_mod, "_mark_error", lambda *a: None)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return [(f"s{i}", {"fetcher": "x.y"}, None) for i in range(n)]

    def test_исчерпанный_бюджет_пропускает_хвост_а_не_убивает_прогон(self):
        # 5 висящих рядов по 93 с при бюджете 180 с: два успевают отказать,
        # остальные пропускаются с внятной пометкой — publish состоится.
        self.budget.arm(180)
        journal = Journal()
        report = self.run_mod.fetch_all(self.slow_items(5), journal)
        statuses = [report[f"s{i}"]["status"] for i in range(5)]
        self.assertEqual(statuses, ["error", "error", "skip", "skip", "skip"])
        for i in (2, 3, 4):
            self.assertIn("бюджет", report[f"s{i}"]["note"])
        self.assertEqual(sum("бюджет" in ln for ln in journal.lines), 1,
                         "об исчерпании говорим один раз, а не на каждый ряд")

    def test_без_бюджета_поведение_прежнее(self):
        self.budget.disarm()
        report = self.run_mod.fetch_all(self.slow_items(4), Journal())
        self.assertEqual([report[f"s{i}"]["status"] for i in range(4)], ["error"] * 4)

    def test_мусор_в_переменной_не_взводит_и_не_роняет(self):
        import os
        prev = os.environ.get("RADAR_FETCH_BUDGET_S")
        self.addCleanup(lambda: os.environ.__setitem__("RADAR_FETCH_BUDGET_S", prev)
                        if prev is not None else os.environ.pop("RADAR_FETCH_BUDGET_S", None))
        for junk in ("скоро", "", "-5", "0"):
            os.environ["RADAR_FETCH_BUDGET_S"] = junk
            self.assertFalse(self.budget.arm_from_env(), junk)
            self.assertFalse(self.budget.exhausted())
        os.environ["RADAR_FETCH_BUDGET_S"] = "300"
        self.assertTrue(self.budget.arm_from_env())
        self.assertEqual(self.budget.remaining(), 300.0)

    def test_ширина_рынка_не_считается_по_огрызку_корзины(self):
        # breadth — 45 последовательных историй; частичная корзина исказила бы
        # состав дня. При исчерпанном бюджете ряд остаётся на кэше громким отказом.
        iss = need(self, "pipeline.fetch.iss", "breadth")
        self.budget.arm(0)
        self.clock["t"] = 1.0
        with self.assertRaises(iss.FetchError) as ctx:
            iss.breadth(tickers=["SBER", "LKOH"])
        self.assertIn("бюджет", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
