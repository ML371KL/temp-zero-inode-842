"""Что панель умеет и ориентиры (compute/skills.py, аудит 10.11.2026, этап 2).

Волны рынка, зоны «дивиденды минус вклад» и «рынок растёт вопреки оценке» — справка
рядом с позицией, в решение не входят. Здесь: разметка волн и доля взятого на синтетике
с ответом, посчитанным руками; замороженная таблица зон (как CELL_STATS); эпизоды и
событие «одно на эпизод». Регрессия по копии боевого стора — отдельным классом, который
объявляется только при STATE_DIR с копией (без него его нет вовсе, а не пропуск).
"""

import math
import os
import re
import unittest
from datetime import date, datetime, timedelta, timezone

from tests import need

STATE_DIR = (os.environ.get("STATE_DIR") or "").strip()
HAS_STORE = bool(STATE_DIR) and os.path.exists(os.path.join(STATE_DIR, "raw", "mcftr.json"))


def weekdays(start, n):
    out, d = [], date.fromisoformat(start)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def path(*moves, start=100.0, step=10):
    """Ряд из отрезков: каждый ход — доля за отрезок, равными шагами по step дней."""
    out = [start]
    for m in moves:
        k = (1.0 + m) ** (1.0 / step)
        for _ in range(step):
            out.append(out[-1] * k)
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.S = need(self, "pipeline.compute.skills", "zigzag", "capture", "waves",
                      "div_gap", "against", "against_episodes")
        self.K = need(self, "pipeline.lib.constants", "DIV_GAP_ZONES")


class TestWaves(Base):
    def test_зигзаг_от_двадцати_процентов(self):
        """Мутация: порог 0,15 → ход −18 % становится волной падения."""
        tr = path(0.30, -0.25, 0.40)
        legs = self.S.zigzag(tr, 0)
        self.assertEqual([k for k, _a, _b in legs], ["up", "down", "up"])
        self.assertEqual(legs[0][2], 10)   # вершина после +30 %
        self.assertEqual(legs[1][2], 20)   # дно после −25 %
        self.assertEqual(legs[-1][2], len(tr) - 1, "последняя волна не закрыта")
        # −18 % — не разворот: одна волна роста целиком.
        legs = self.S.zigzag(path(0.30, -0.18, 0.40), 0)
        self.assertEqual([k for k, _a, _b in legs], ["up"])

    def test_доля_волны_и_день_входа(self):
        tr = path(0.50, step=10)
        held = [0] * 6 + [1] * 5          # в акциях с шестого дня
        share, wait = self.S.capture(tr, held, 0, 10)
        self.assertEqual(wait, 6)
        self.assertAlmostEqual(share, 5 / 10)

    def test_итоги_и_последние_волны_роста(self):
        dates = weekdays("2009-08-03", 81)
        tr = path(0.40, -0.30, 0.50, -0.25, 0.30, -0.22, 0.35, -0.21, 0.25, step=9)[:81]
        pos = [1 if 10 <= i < 25 or 45 <= i < 60 else 0 for i in range(81)]
        w = self.S.waves(dates, tr, pos)
        self.assertEqual(w["rises"]["n"], 5)
        self.assertEqual(w["falls"]["n"], 4)
        self.assertEqual(len(w["last_rises"]), 4)
        self.assertTrue(w["last_rises"][-1]["open"])
        for r in w["rows"]:
            self.assertGreaterEqual(r["taken_pct"], 0)
            self.assertLessEqual(r["taken_pct"], 100)


class TestDivGap(Base):
    def test_замороженная_таблица_согласована(self):
        z = self.K.DIV_GAP_ZONES
        lo, hi = z["cuts"]
        self.assertLess(lo, hi)
        self.assertEqual(sum(x["months"] for x in z["zones"]), z["months"])
        self.assertEqual([x["id"] for x in z["zones"]], ["low", "mid", "high"])
        for x in z["zones"]:
            # Доля плюсовых — число вида k/n, округлённое до сотых.
            k = round(x["hit"] * x["months"])
            self.assertAlmostEqual(k / x["months"], x["hit"], delta=0.005)
        self.assertTrue(re.match(r"^\d{4}-\d{2}-\d{2}$", z["asof"]))

    def test_зона_по_замороженным_границам(self):
        """Мутация: зона по живым терциям → граница плывёт с каждым новым месяцем."""
        lo, hi = self.K.DIV_GAP_ZONES["cuts"]
        dates = weekdays("2026-01-05", 3)
        for v, zone in ((lo - 0.01, "low"), (lo, "mid"), (hi - 0.01, "mid"), (hi, "high")):
            g = self.S.div_gap(dates, {"switch_spread": [None, v, None]})
            self.assertEqual(g["zone"], zone, v)
            self.assertEqual(g["asof"], dates[1])
            self.assertEqual(g["zones"], self.K.DIV_GAP_ZONES["zones"])


class TestAgainst(Base):
    def setUp(self):
        super().setUp()
        self.dates = weekdays("2010-01-04", 200)
        tr = [100.0] * 200
        # Два рывка: +12 % за 20 сессий к 60-му дню и к 70-му (один эпизод: ближе 30
        # дней), и третий к 150-му — отдельный эпизод.
        for i in range(40, 60):
            tr[i] = tr[i - 1] * 1.0058
        for i in range(60, 200):
            tr[i] = tr[i - 1]
        for i in range(130, 150):
            tr[i] = tr[i - 1] * 1.006
        for i in range(150, 200):
            tr[i] = tr[i - 1]
        self.tr = tr

    def test_эпизоды_и_деньги(self):
        """Мутация: убрать условие «деньги» → при акциях тоже эпизоды."""
        flat = [0] * 200
        eps, st = self.S.against_episodes(self.dates, self.tr, flat)
        self.assertEqual(len(eps), 2)
        self.assertEqual(st["episodes"], 2)
        long_ = [1] * 200
        self.assertEqual(self.S.against_episodes(self.dates, self.tr, long_)[0], [])

    def test_последний_день_начинает_или_продолжает_эпизод(self):
        flat = [0] * 200
        a = self.S.against(self.dates[:150], self.tr[:150], flat[:150])
        self.assertTrue(a["active"])
        self.assertEqual(a["rise_20_pct"], round((1.006 ** 20 - 1) * 100, 1))
        start = a["episode_start"]
        b = self.S.against(self.dates[:152], self.tr[:152], flat[:152])
        self.assertEqual(b["episode_start"], start, "продолжение того же эпизода")
        c = self.S.against(self.dates[:200], self.tr, flat)
        self.assertFalse(c["active"])
        self.assertIsNone(c["episode_start"])


class TestAgainstEvent(unittest.TestCase):
    NOW = datetime(2026, 11, 10, 16, 5, tzinfo=timezone.utc)

    def setUp(self):
        self.alerts = need(self, "pipeline.alerts", "_against")

    def payload(self, active, start, rise=12.3):
        return {"verdict": {"against": {
            "active": active, "episode_start": start, "rise_20_pct": rise, "window": 20,
            "fwd_sessions": 40, "since": "2010-01-01",
            "stats": {"episodes": 12, "with_fwd": 11, "higher": 8, "fwd_avg_pct": 2.3,
                      "flat_fwd_avg_pct": 0.1}}}}

    def test_одно_событие_на_эпизод(self):
        """Мутация: дедуп по дню, а не по началу эпизода → событие каждый день подряд."""
        state = {}
        ev = self.alerts._against(self.payload(True, "2026-11-10"), {}, state, self.NOW)
        self.assertEqual(len(ev), 1)
        e = ev[0]
        self.assertEqual(e["kind"], "against_rally")
        self.assertIn("+12,3%", e["text"])
        self.assertIn("8 из 11", e["text"])
        self.assertIsNone(re.search(r"\d\.\d", re.sub(r"\b\d{2}\.\d{2}(?:\.\d{4})?\b", " ", e["text"])))
        self.assertIsNone(re.search(r"(?<![\w])-\d", e["text"]))
        again = self.alerts._against(self.payload(True, "2026-11-10", 14.0), {}, state, self.NOW)
        self.assertEqual(again, [])
        pause = self.alerts._against(self.payload(False, None), {}, state, self.NOW)
        self.assertEqual(pause, [])
        new = self.alerts._against(self.payload(True, "2026-12-20"), {}, state, self.NOW)
        self.assertEqual(len(new), 1)

    def test_вид_события_в_словаре_внешних_имён(self):
        wording = need(self, "pipeline.lib.wording", "KIND")
        self.assertIn("against_rally", wording.KIND)


if HAS_STORE:
    class TestStoreRegression(unittest.TestCase):
        """Копия боевого стора 09.11.2026: числа совпадают с эталоном аудита 10.11.2026
        (results/5-turns-and-leading.txt, results/6-deeper.txt, блок 4)."""

        @classmethod
        def setUpClass(cls):
            from pipeline.lib import store
            from pipeline.compute import panel as P, core as C, decision as D
            cls.panel = P.build_panel(store)
            cls.dec = D.compute_decision(cls.panel, C.monthly_frame(cls.panel))

        def test_зоны_совпадают_с_замороженными(self):
            from pipeline.compute import skills
            from pipeline.lib import constants
            t = skills.div_gap_table(self.panel["dates"], self.panel["cols"])
            z = constants.DIV_GAP_ZONES
            if t["until"] != z["until"]:
                self.skipTest("стор новее заморозки — сверка только на копии 09.11.2026")
            self.assertEqual(t["cuts"], z["cuts"])
            self.assertEqual(t["zones"], z["zones"])

        def test_волны_как_у_аудита(self):
            w = self.dec["skills"]["waves"]
            self.assertEqual((w["falls"]["n"], w["falls"]["lived_pct"]), (10, 21))
            self.assertEqual((w["rises"]["n"], w["rises"]["entry_days_avg"],
                              w["rises"]["taken_pct_avg"]), (11, 19, 44))
            got = [(round(r["move_pct"]), r["entry_days"], r["taken_pct"]) for r in w["last_rises"]]
            self.assertEqual(got, [(113, 23, 62), (41, None, 0), (20, 46, 70), (31, 44, 29)])


if __name__ == "__main__":
    unittest.main()
