"""Живой журнал позиции и простая кривая (compute/track.py, аудит 10.11.2026).

Таблица «Что дало правило» показывала пересчёт истории нынешней редакцией, и одна правка
задним числом превращала «хуже вкладов» в «лучше». Журнал «что панель говорила» —
неизменяемый ряд в сторе: прогон только дописывает новый последний день. Здесь —
правила этого ряда (затравка настоящей историей, дописывание, неизменность записанного,
отказ писать поверх нечитаемого), счёт по нему и простая кривая как ориентир.

Даты фиксированы, стор — словарь в памяти.
"""

import unittest
from datetime import date, timedelta

from tests import need


def weekdays(start, end):
    out, d = [], date.fromisoformat(start)
    while d <= date.fromisoformat(end):
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


class MemStore:
    def __init__(self, broken=False):
        self.series, self.broken, self.writes = {}, broken, []

    def load_series(self, sid):
        if self.broken:
            raise OSError("диск отвалился")
        return self.series.get(sid)

    def upsert_points(self, sid, points, meta=None):
        s = self.series.setdefault(sid, {"id": sid, "points": {}, "meta": {}})
        s["points"].update(points)
        s["meta"].update(meta or {})
        self.writes.append((sid, dict(points)))
        return s


class Base(unittest.TestCase):
    def setUp(self):
        self.t = need(self, "pipeline.compute.track", "said_record", "said_load",
                      "live_journal", "curve_positions", "SAID_POS", "SAID_RULE")
        self.K = need(self, "pipeline.lib.constants", "SAID_SEED_CHANGES", "SAID_SEED_TO",
                      "RULE_SINCE", "RULE_VERSION", "PANEL_LAUNCH")
        self.dates = weekdays("2026-07-27", "2026-11-13")
        # Сегодняшний пересчёт: акции с 21.09 (как нынешняя версия на истории) и деньги
        # с 06.11 — пересчёт и сказанное до 21.10 НЕ совпадают, и это нарочно.
        self.pos = [1 if "2026-09-21" <= d < "2026-11-06" else 0 for d in self.dates]


class TestSeedAndAppend(Base):
    def test_затравка_настоящей_историей_а_не_пересчётом(self):
        """Мутация: заводить журнал пересчётом → 21.09–14.10 «акции», тест краснеет."""
        st = MemStore()
        said, rep = self.t.said_record(st, self.dates, self.pos)
        self.assertTrue(rep["seeded"])
        self.assertIsNone(rep["error"])
        self.assertNotIn("2026-08-10", said, "до запуска панель ничего не говорила")
        self.assertEqual(said["2026-08-11"], (0, 1))
        self.assertEqual(said["2026-09-21"], (0, 2), "вживую 21.09 панель стояла в деньгах")
        self.assertEqual(said["2026-10-14"], (0, 3))
        self.assertEqual(said["2026-10-15"], (1, 3), "«акции» решено 15.10")
        self.assertEqual(said["2026-10-21"], (1, 4))
        self.assertEqual(said["2026-11-06"], (0, 4), "«деньги» решено 06.11")
        # После затравки — расчёт нынешней версии (она же публиковалась в эти дни).
        self.assertEqual(said["2026-11-13"], (0, self.K.RULE_VERSION))
        self.assertEqual(sorted(st.series), sorted([self.t.SAID_POS, self.t.SAID_RULE]))

    def test_прогон_дописывает_только_новый_последний_день(self):
        st = MemStore()
        self.t.said_record(st, self.dates[:-3], self.pos[:-3])
        n0 = len(st.writes)
        said, rep = self.t.said_record(st, self.dates, self.pos)
        self.assertEqual(rep["appended"], [self.dates[-1]])
        self.assertTrue(all(list(p) == [self.dates[-1]] for _, p in st.writes[n0:]))
        # Пропущенные дни не досчитываются: панель в них стояла на прежней витрине.
        self.assertNotIn(self.dates[-2], said)

    def test_записанный_день_не_переписывается(self):
        """Мутация: дописывать через пересчёт всего хвоста → 13.11 станет «акции»."""
        st = MemStore()
        self.t.said_record(st, self.dates, self.pos)
        flipped = [1 - p for p in self.pos]
        n0 = len(st.writes)
        said, rep = self.t.said_record(st, self.dates, flipped)
        self.assertEqual(rep["appended"], [])
        self.assertEqual(len(st.writes), n0, "ни одной записи в стор")
        self.assertEqual(said["2026-11-13"][0], 0)

    def test_нечитаемый_журнал_не_затирается(self):
        st = MemStore(broken=True)
        said, rep = self.t.said_record(st, self.dates, self.pos)
        self.assertEqual(said, {})
        self.assertTrue(rep["error"])
        self.assertEqual(st.writes, [])


class TestLiveJournal(Base):
    def setUp(self):
        super().setUp()
        n = len(self.dates)
        # Индекс растёт на 1% в каждый торговый день после 15.10, до того стоит.
        tr, v = [], 100.0
        for d in self.dates:
            if d > "2026-10-15":
                v *= 1.01
            tr.append(v)
        self.tr, self.cash = tr, [10.0] * n
        self.said, _ = self.t.said_record(MemStore(), self.dates, self.pos)

    def test_окна_и_отрезки_по_сказанному(self):
        live = self.t.live_journal(self.dates, self.said, self.tr, self.cash,
                                   curve=[1] * len(self.dates), pos_now=self.pos)
        ids = [w["id"] for w in live["windows"]]
        self.assertEqual(ids, ["launch", "version"])
        w = live["windows"][0]
        self.assertEqual(w["from"], "2026-08-11")
        self.assertEqual(w["to"], "2026-11-13")
        # Простая кривая всё время в акциях — её итог равен индексу минус одна смена
        # на старте журнала нет (до запуска позиция кривой та же).
        self.assertAlmostEqual(w["curve_pct"], w["tr_pct"], places=2)
        self.assertAlmostEqual(w["vs_curve_pp"], round(w["rule_pct"] - w["curve_pct"], 2))
        sp = live["spells"]
        self.assertEqual([(s["decided"], s["to"]) for s in sp],
                         [("2026-08-11", "flat"), ("2026-10-15", "long"), ("2026-11-06", "flat")])
        self.assertEqual(sp[1]["executed"], "2026-10-16")
        self.assertEqual(sp[1]["until"], "2026-11-09")
        # Акции от закрытия 16.10 до закрытия 09.11: 16 торговых дней по +1%.
        days = sum(1 for d in self.dates if "2026-10-16" < d <= "2026-11-09")
        self.assertAlmostEqual(sp[1]["tr_pct"], round((1.01 ** days - 1) * 100, 2))
        self.assertTrue(sp[-1]["open"])
        # Пересчёт расходится со сказанным только до 21.10, а это прошлая версия.
        self.assertEqual(live["differs_days"], 0)

    def test_пропуск_прогона_читается_прежней_позицией(self):
        said = dict(self.said)
        for d in ("2026-11-10", "2026-11-11"):
            said.pop(d)
        live = self.t.live_journal(self.dates, said, self.tr, self.cash)
        self.assertEqual(live["gap_days"], 2)

    def test_без_журнала_блока_нет(self):
        self.assertIsNone(self.t.live_journal(self.dates, {}, self.tr, self.cash))


class TestCurve(Base):
    def test_кривая_читается_только_в_день_решения(self):
        """Мутация: читать наклон каждый день → смена во вторник, тест краснеет."""
        dates = weekdays("2026-11-02", "2026-11-13")
        dec = [date.fromisoformat(d).weekday() == 4 for d in dates]
        slope = [-0.5, -0.4, 0.3, 0.2, -0.1, 0.5, 0.6, 0.7, 0.8, 0.9]
        got = self.t.curve_positions(dates, slope, dec, start="2004-01-06")
        # Пятница 06.11: наклон −0,1 → деньги; пятница 13.11: +0,9 → акции.
        self.assertEqual(got, [0, 0, 0, 0, 0, 0, 0, 0, 0, 1])

    def test_пропуск_наклона_держит_позицию(self):
        dates = weekdays("2026-11-02", "2026-11-20")
        dec = [date.fromisoformat(d).weekday() == 4 for d in dates]
        slope = [1.0] * 5 + [None] * 5 + [-1.0] * 5
        got = self.t.curve_positions(dates, slope, dec)
        self.assertEqual(got[4], 1)
        self.assertEqual(got[9], 1, "в день решения без данных прежняя позиция")
        self.assertEqual(got[14], 0)


if __name__ == "__main__":
    unittest.main()
