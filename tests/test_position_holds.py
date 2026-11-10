"""Что держит позицию сейчас (аудит 10.11.2026, §3.8).

09.11.2026 под позицией «деньги» стояло «Правило дня: медведь с долговой тенью —
приоритет ОФЗ-флаг», хотя ворота были открыты и деньги держала оценка рынка (−0,28).
Строка обязана называть то, что держит позицию; текст сочетания ворот — только когда
держат ворота. Поле verdict.position.held_by — общий источник для строки и плашек.
"""

import unittest

from tests import need


def position(state, gate_open, comp_state, comp=-0.28):
    return {"state": state, "gate_open": gate_open, "comp_state": comp_state,
            "comp_daily": comp, "comp_state_since": "2026-11-06", "comp_threshold": 0.2}


class TestHeldBy(unittest.TestCase):
    def setUp(self):
        self.dec = need(self, "pipeline.compute.decision", "_held_by")

    def test_четыре_случая(self):
        f = self.dec._held_by
        self.assertEqual(f("long", True, 1), {"by": "open"})
        self.assertEqual(f("flat", True, -1), {"by": "comp"})
        self.assertEqual(f("flat", True, 0), {"by": "comp"})
        self.assertEqual(f("flat", False, 1), {"by": "gate"})
        self.assertEqual(f("flat", False, -1), {"by": "both"})


class TestHoldsText(unittest.TestCase):
    CELL = (0, 0, 1)   # медведь · спокойно · стресс — ворота 09.11.2026
    GATE_CLOSED = (0, 1, 1)

    def setUp(self):
        self.pub = need(self, "pipeline.publish", "build_verdict")
        self.dec = need(self, "pipeline.compute.decision", "_held_by")
        self.K = need(self, "pipeline.lib.constants", "CELL_RULES")

    def verdict(self, state, gate_open, cs, gate):
        p = position(state, gate_open, cs)
        p["held_by"] = self.dec._held_by(state, gate_open, cs)
        states = {"current": dict(zip(("trend", "vol", "bond"), gate)),
                  "gate": dict(zip(("trend", "vol", "bond"), gate))}
        return self.pub.build_verdict({"value": -0.28}, states, {"position": p})

    def test_держит_оценка_строка_не_про_сочетание(self):
        """Мутация: оставить текст сочетания → «приоритет — ОФЗ-флаг» при открытых воротах."""
        v = self.verdict("flat", True, -1, self.CELL)
        self.assertIn("оценка рынка", v["rule"])
        self.assertIn("−0,28", v["rule"])
        self.assertNotIn("ОФЗ-флаг", v["rule"])
        self.assertEqual(v["cell_rule"], self.K.CELL_RULES[self.CELL])

    def test_держат_ворота_строка_сочетания(self):
        v = self.verdict("flat", False, 1, self.GATE_CLOSED)
        self.assertEqual(v["rule"], self.K.CELL_RULES[self.GATE_CLOSED])

    def test_держат_оба_называет_оба(self):
        v = self.verdict("flat", False, -1, self.GATE_CLOSED)
        self.assertIn("и ворота, и оценка", v["rule"])
        self.assertIn(self.K.CELL_RULES[self.GATE_CLOSED], v["rule"])

    def test_акции_называют_условие_выхода(self):
        v = self.verdict("long", True, 1, (1, 0, 0))
        self.assertTrue(v["rule"].startswith("Акции"))
        self.assertIn("−0,2", v["rule"])

    def test_без_позиции_прежнее_правило_дня(self):
        v = self.pub.build_verdict({"value": 0.5}, {"current": {"trend": 1, "vol": 0, "bond": 0}})
        self.assertEqual(v["rule"], self.K.CELL_RULES[(1, 0, 0)])
        self.assertNotIn("cell_rule", v)


if __name__ == "__main__":
    unittest.main()
