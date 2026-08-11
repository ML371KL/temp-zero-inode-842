"""Панель: свежесть ног и лаг доступности.

Что здесь закреплено (аудит 11.08.2026):

1. РАЗНОСТЬ ДВУХ РЯДОВ СЧИТАЕТСЯ ТОЛЬКО ТАМ, ГДЕ ЕСТЬ ОБА. Если MCFTR отстал на
   день, а IMOEX уже закрылся, протянутый MCFTR даёт не дивдоходность, а
   дивдоходность МИНУС сегодняшнюю доходность индекса: в проде 11.08.2026 dy_trail
   уехал с 8.49 на 7.14 (ровно +1.32% индекса), z −1.77 вместо +0.25 — вердикт
   сигнала переворачивался на пустом месте.
2. МЁРТВЫЙ ИСТОЧНИК НЕ ИЗОБРАЖАЕТ СВЕЖИЕ ДАННЫЕ. Ставка по вкладам тянулась вперёд
   без ограничения: обрыв ряда ЦБ на 18 месяцев давал switch_spread −12.98 вместо
   −4.35, и сигнал так же уверенно выдавал вердикт.
3. ЛАГ СЧИТАЕТСЯ ТЕМ ЖЕ КОДОМ, ЧТО ПРОВЕРЯЮТ ТЕСТЫ. panel._shift на неразобранном
   ключе МОЛЧА возвращал дату без лага — то есть показывал значение раньше, чем
   оно появилось у источника.

Даты фиксированные (правило tests/__init__.py).
"""

import math
import unittest
from datetime import date, timedelta

from tests import need


def _series(points, unit="points"):
    return {"unit": unit, "cadence": "daily", "points": points, "meta": {"status": "ok"}}


def _calendar(n, start=date(2024, 1, 1)):
    return [(start + timedelta(days=i)).isoformat() for i in range(n)]


class TestDyTrailNeedsBothLegs(unittest.TestCase):
    """dy_trail = 252-дневная доходность MCFTR минус та же у IMOEX."""

    N = 300

    def setUp(self):
        self.panel = need(self, "pipeline.compute.panel", "build_panel")
        self.days = _calendar(self.N)
        # Индекс растёт на 0,02% в день, полная доходность — на 0,05%: разница
        # (дивдоходность) на любом окне 252 дня постоянна и считается руками.
        self.px = {d: 2000.0 * math.exp(0.0002 * i) for i, d in enumerate(self.days)}
        self.tr = {d: 5000.0 * math.exp(0.0005 * i) for i, d in enumerate(self.days)}

    def _build(self, mcftr_points):
        return self.panel.build_panel({"imoex": _series(self.px),
                                       "mcftr": _series(mcftr_points)})

    def test_value_is_exact_when_both_legs_are_native(self):
        out = self._build(self.tr)
        i = len(self.days) - 1
        self.assertAlmostEqual(out["cols"]["dy_trail"][i], 0.0003 * 252 * 100.0, places=6)

    def test_stale_mcftr_leaves_a_hole_instead_of_a_shifted_number(self):
        # мутация: считать разность на протянутом MCFTR -> последний день уезжает
        # ровно на дневную доходность индекса, а витрина показывает это как сигнал.
        cut = dict(self.tr)
        del cut[self.days[-1]]
        out = self._build(cut)
        col = out["cols"]["dy_trail"]
        self.assertIsNone(col[-1], "день без собственной точки MCFTR обязан быть пустым")
        self.assertAlmostEqual(col[-2], 0.0003 * 252 * 100.0, places=6)
        # switch_spread наследует пустоту, а не уезжает вместе с dy_trail
        self.assertIsNone(out["cols"]["switch_spread"][-1])


class TestDepositDoesNotOutliveItsSource(unittest.TestCase):
    """Декада ЦБ выходит раз в ~7 торговых дней; протяжка ограничена."""

    def setUp(self):
        self.panel = need(self, "pipeline.compute.panel", "build_panel", "DEPOSIT_FFILL_LIMIT")
        self.days = _calendar(120)
        self.px = {d: 2000.0 + i for i, d in enumerate(self.days)}

    def test_normal_cadence_loses_nothing(self):
        # Декады идут раз в 10 календарных дней — при лимите 15 строк календаря
        # колонка обязана быть непрерывной после первой же даты доступности.
        pts = {self.days[i]: 16.0 - i * 0.01 for i in range(0, len(self.days), 10)}
        col = self.panel.build_panel({"imoex": _series(self.px),
                                      "deposit_decade": _series(pts, "pct")})["cols"]["deposit"]
        tail = col[-10:]
        self.assertTrue(all(v is not None for v in tail), f"дыры в хвосте: {tail}")

    def test_dead_source_stops_feeding_the_signal(self):
        # мутация: тянуть без лимита -> ставка полуторагодовой давности продолжает
        # кормить switch_spread, и вердикт «против лонга» держится на мертвечине.
        pts = {self.days[0]: 21.47}
        col = self.panel.build_panel({"imoex": _series(self.px),
                                      "deposit_decade": _series(pts, "pct")})["cols"]["deposit"]
        self.assertIsNotNone(col[5])
        self.assertIsNone(col[-1])
        alive = sum(1 for v in col if v is not None)
        self.assertLessEqual(alive, self.panel.DEPOSIT_FFILL_LIMIT + 5)


class TestPublicationLagPath(unittest.TestCase):
    """Лаг применяет ровно та функция, которую проверяет tests/test_dates.py."""

    def setUp(self):
        self.panel = need(self, "pipeline.compute.panel", "_shift", "_align")

    def test_lag_is_calendar_days_from_period_end(self):
        self.assertEqual(self.panel._shift("2026-07-31", 4), "2026-08-04")
        self.assertEqual(self.panel._shift("2026-07-31", 0), "2026-07-31")

    def test_month_label_is_normalised_to_month_end(self):
        # Минфин исторически отдавал '2015-01' без дня: лаг от первого числа сделал бы
        # месячное значение видимым на месяц раньше.
        self.assertEqual(self.panel._shift("2026-07", 5), "2026-08-05")

    def test_unparsable_key_never_appears_early(self):
        # мутация: вернуть ключ как есть -> значение видно БЕЗ лага, то есть на 4–15
        # дней раньше публикации; ни один тест этого раньше не ловил.
        with self.assertRaises(ValueError):
            self.panel._shift("июль 2026", 5)
        dates = ["2026-08-03", "2026-08-04", "2026-08-05"]
        col = self.panel._align({"июль 2026": 99.0, "2026-07-31": 5.0}, dates, lag=4)
        self.assertEqual(col, [None, 5.0, 5.0])


if __name__ == "__main__":
    unittest.main()
