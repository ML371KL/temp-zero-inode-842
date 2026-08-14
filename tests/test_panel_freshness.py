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
from datetime import date, datetime, timedelta, timezone
from unittest import mock

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


class TestStalenessFollowsTheSchedule(unittest.TestCase):
    """«Протух» — это ПРОПУЩЕННЫЙ ОПРОС, а не просто много часов без записи.

    ОПЛАЧЕНО ПРОДОМ 14.08.2026, 08:25 UTC: в ops-канал ушло «источник auctions
    отдаёт устаревшие данные, данные от 15.07.2026, последний удачный опрос 26 ч
    назад». Источник был исправен, числа верны, аукционов с 20.07 просто нет.
    Ряд `ofz_auctions` опрашивается НЕДЕЛЬНЫМ режимом (пн 07:05, ср 17:20, чт 06:20),
    между четвергом и понедельником — 97 часов, а норма свежести стояла `iss_daily`
    = 26 часов. То есть ряд краснел с утра пятницы до утра понедельника КАЖДУЮ
    НЕДЕЛЮ, и это было заложено в конфигурации.

    Замер тем же кодом на боевом сторе показал, что аукционы — лишь верхушка: в
    ближайшие выходные так протухли бы 23 ряда из 26, то есть пять сообщений в
    ops-канал о том, что биржа закрыта.

    Та же болезнь и то же лекарство, что у баннера «Данные устарели» на витрине:
    сравнивать возраст надо не с плоским числом часов, а с расписанием.
    """

    def setUp(self):
        self.monitors = need(self, "pipeline.compute.monitors", "series_status")
        self.schedule = need(self, "pipeline.lib.schedule", "last_run_at")
        self.pts = {"2026-07-15": 1.0}

    def status(self, sid, fetched, when):
        return self.monitors.series_status(
            sid, self.pts, {"fetched_at": fetched, "status": "ok"},
            datetime.fromisoformat(when).replace(tzinfo=timezone.utc))

    # ------------------------------------------------------------- расписание
    def test_последний_плановый_такт_режима(self):
        got = self.schedule.last_run_at(
            "weekly", datetime(2026, 8, 14, 8, 25, tzinfo=timezone.utc))
        self.assertEqual(got, datetime(2026, 8, 13, 6, 20, tzinfo=timezone.utc))

    def test_такт_ищется_и_через_выходные(self):
        # Суточный режим — только по будням: в воскресенье последний такт пятничный.
        got = self.schedule.last_run_at(
            "daily", datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(got, datetime(2026, 8, 14, 20, 55, tzinfo=timezone.utc))

    def test_незнакомый_режим_не_угадывается(self):
        self.assertIsNone(self.schedule.last_run_at(
            "такого-нет", datetime(2026, 8, 14, tzinfo=timezone.utc)))

    # ------------------------------------------------- боевой случай аукционов
    def test_недельный_ряд_не_краснеет_между_своими_тактами(self):
        """Ровно то сообщение, что пришло владельцу: пятница, 26 часов, всё исправно."""
        fetched = "2026-08-13T06:21:42Z"          # четверговый недельный такт
        for when in ("2026-08-14 08:25",          # пятница, момент ложной тревоги
                     "2026-08-15 12:00",          # суббота
                     "2026-08-16 23:00",          # воскресенье
                     "2026-08-17 06:00"):         # понедельник до такта 07:05
            with self.subTest(when=when):
                self.assertEqual(self.status("ofz_auctions", fetched, when), "ok")

    def test_пропущенный_такт_по_прежнему_ловится(self):
        # Понедельничный такт в 07:05 прошёл, записи нет — вот это уже отказ.
        self.assertEqual(
            self.status("ofz_auctions", "2026-08-13T06:21:42Z", "2026-08-17 12:00"),
            "stale")

    # --------------------------------------------------------- рыночные ряды
    def test_дневные_ряды_переживают_выходные(self):
        # Момент записи берётся СВОЙ для каждого ряда: интрадей-такт закрывает день
        # в 21:00, суточный — в 20:55, и ряд, живущий в обоих режимах, обязан быть
        # свежим по позднему из них.
        cases = {"imoex": "2026-08-14T21:00:05Z", "rgbi": "2026-08-14T21:00:05Z",
                 "usd_cbr": "2026-08-14T20:55:29Z", "zcyc": "2026-08-14T20:55:29Z"}
        for sid, fetched in cases.items():
            for when in ("2026-08-15 23:00", "2026-08-16 23:00"):
                with self.subTest(sid=sid, when=when):
                    self.assertEqual(self.status(sid, fetched, when), "ok")

    def test_будний_пропуск_остаётся_отказом(self):
        # Со среды пропущены такты четверга и пятницы, возраст 49 ч против нормы 26.
        self.assertEqual(self.status("imoex", "2026-08-12T21:00:05Z",
                                     "2026-08-14 22:00"), "stale")

    def test_один_пропущенный_такт_внутри_нормы_молчит(self):
        """Проверка стоит в связке И: пропуск сам по себе тревогой не делает.

        Запись с четверга при пятничном такте — это 25 часов при норме 26. Прежнее
        правило здесь молчало, и новое обязано молчать тоже: связка `И` может только
        УБИРАТЬ тревоги, а не добавлять.
        """
        self.assertEqual(self.status("imoex", "2026-08-13T21:00:05Z",
                                     "2026-08-14 22:00"), "ok")

    def test_правило_только_смягчает(self):
        """Новая проверка НЕ ДОЛЖНА создавать новых отказов.

        Она стоит в связке `И` со старой нормой: ряд считается протухшим, только
        если ПЕРЕЖИЛ пропущенный такт И вышел за свою норму часов. Значит любой
        вердикт «stale» остаётся подмножеством прежних.
        """
        fetched = "2026-08-13T06:21:42Z"
        now = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
        age_min = (now - datetime(2026, 8, 13, 6, 21, 42,
                                  tzinfo=timezone.utc)).total_seconds() / 60
        constants = need(self, "pipeline.lib.constants", "SLA_MINUTES")
        registry = need(self, "pipeline.lib.registry", "SERIES")
        sla = constants.SLA_MINUTES[registry.SERIES["ofz_auctions"]["sla"]]
        self.assertGreater(age_min, sla, "проверка бессмысленна: ряд и по норме свежий")
        self.assertEqual(self.status("ofz_auctions", fetched, "2026-08-17 12:00"),
                         "stale")

    def test_прогону_дают_доехать_до_записи(self):
        """Такт сработал секунду назад — ряд ещё не записан, и это не отказ.

        Без форы правило объявляло бы протухшим КАЖДЫЙ ряд в те полчаса, что прогон
        идёт: таймер уже сработал, а записи в сторе ещё нет. Тревога, загорающаяся
        по собственному расписанию наблюдателя, — это шум, а не наблюдение.
        """
        fetched = "2026-08-13T06:21:42Z"       # четверговый недельный такт
        # Понедельничный такт 07:05 сработал двадцать секунд назад.
        self.assertEqual(self.status("ofz_auctions", fetched, "2026-08-17 07:05:20"),
                         "ok")
        # Полчаса прошло, записи так и нет — теперь это отказ.
        self.assertEqual(self.status("ofz_auctions", fetched, "2026-08-17 07:40"),
                         "stale")

    def test_нечитаемое_расписание_возвращает_прежнее_поведение(self):
        """Ответ «не знаю» трактуется как «пропуск был» — решает старая норма.

        Иначе исчезнувший каталог `ops/` тихо погасил бы всю проверку свежести.
        """
        with mock.patch.object(self.schedule, "last_run_at", return_value=None):
            self.assertEqual(
                self.status("ofz_auctions", "2026-08-13T06:21:42Z", "2026-08-14 08:25"),
                "stale")
