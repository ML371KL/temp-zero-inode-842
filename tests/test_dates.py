"""Даты, торговый календарь и — главное — ЛАГИ ДОСТУПНОСТИ.

Заглядывание в будущее это единственная ошибка, которая делает панель не просто
неточной, а вредной: она рисует «сигнал работал» там, где сигнала в тот день ещё
не существовало. Валидация прошла аудит первых появлений с нулём нарушений именно
потому, что лаг применялся ДО выравнивания на календарь (VALIDATION.md §1). Здесь
это зафиксировано тестом: значение периода становится видимым РОВНО с даты
period_end + pub_lag_days и ни днём раньше.

Все даты в файле фиксированные — «сегодня минус N» запрещено (см. tests/__init__.py).
"""

import unittest
from datetime import date, datetime, timedelta, timezone

from tests import need

UTC = timezone.utc


class TestPublicationLag(unittest.TestCase):
    """period_end + pub_lag_days = дата доступности. Ни днём раньше."""

    def setUp(self):
        self.dates = need(self, "pipeline.lib.dates", "apply_pub_lag")

    def test_apply_pub_lag_exact_day(self):
        # Лаги из registry: ОРФР +15 дней после месяца, ИПЦ +13, Urals +5, декады +4.
        # мутация: лаг в рабочих днях вместо календарных или «+1 месяц» —
        # даты доступности разъедутся с теми, на которых считалась валидация.
        self.assertEqual(self.dates.apply_pub_lag("2026-07-31", 15), date(2026, 8, 15))
        self.assertEqual(self.dates.apply_pub_lag("2026-07-31", 13), date(2026, 8, 13))
        self.assertEqual(self.dates.apply_pub_lag("2026-07-31", 5), date(2026, 8, 5))
        self.assertEqual(self.dates.apply_pub_lag("2026-07-20", 4), date(2026, 7, 24))

    def test_zero_lag_is_identity(self):
        # Дневные рыночные ряды известны на закрытии — сдвигать их нельзя.
        self.assertEqual(self.dates.apply_pub_lag("2026-08-11", 0), date(2026, 8, 11))
        self.assertEqual(self.dates.apply_pub_lag("2026-08-11", None), date(2026, 8, 11))


class TestPanelDoesNotLookAhead(unittest.TestCase):
    """Тот же лаг, но уже на слое расчёта: колонка панели обязана молчать до даты
    доступности. Это интеграционная проверка §1 контракта, а не свойств dates.py."""

    STORE = {
        "imoex": {"id": "imoex", "unit": "points", "cadence": "daily", "points": {
            "2026-07-16": 2210.0, "2026-07-17": 2218.4, "2026-07-20": 2224.9,
            "2026-07-21": 2231.7, "2026-07-22": 2229.0, "2026-07-23": 2240.6,
            "2026-07-24": 2262.8, "2026-07-27": 2255.1, "2026-07-28": 2248.3,
            "2026-07-29": 2251.9, "2026-07-30": 2244.0, "2026-07-31": 2247.5,
            "2026-08-03": 2251.14, "2026-08-04": 2268.9, "2026-08-05": 2274.06,
        }, "meta": {"source": "iss", "status": "ok"}},
        # Декады ЦБ: ключ — КОНЕЦ ДЕКАДЫ (дата периода), pub_lag_days = 4.
        "deposit_decade": {"id": "deposit_decade", "unit": "pct", "cadence": "decade",
                           "points": {"2026-07-10": 16.10, "2026-07-20": 15.94,
                                      "2026-07-31": 15.86},
                           "meta": {"source": "cbr", "status": "ok"}},
    }

    def setUp(self):
        self.panel_mod = need(self, "pipeline.compute.panel", "build_panel")
        self.panel = self.panel_mod.build_panel(self.STORE)
        self.at = {d: i for i, d in enumerate(self.panel["dates"])}

    def value_on(self, day):
        return self.panel["cols"]["deposit"][self.at[day]]

    def test_value_appears_exactly_on_availability_date(self):
        # Декада «III.07» закончилась 31.07, доступна с 04.08 (31.07 + 4 дня).
        # мутация: применить лаг после выравнивания (или забыть его) -> 15.86
        # появится 31.07, и панель «узнает» ставку за четыре дня до публикации.
        self.assertEqual(self.value_on("2026-07-31"), 15.94)
        self.assertEqual(self.value_on("2026-08-03"), 15.94)
        self.assertEqual(self.value_on("2026-08-04"), 15.86)
        self.assertEqual(self.value_on("2026-08-05"), 15.86)

    def test_previous_decade_switches_on_its_own_date(self):
        # II декада (период до 20.07) доступна с 24.07 — до этого держится I декада.
        self.assertEqual(self.value_on("2026-07-23"), 16.10)
        self.assertEqual(self.value_on("2026-07-24"), 15.94)

    def test_no_column_leaks_future_dates(self):
        # Общая страховка: календарь панели не выходит за последний день imoex.
        self.assertEqual(self.panel["dates"][-1], "2026-08-05")
        self.assertEqual(len(self.panel["dates"]), 15)

    def test_panel_without_base_series_fails_loudly(self):
        # мутация: тихо вернуть пустую панель -> дашборд опубликует пустоту как норму.
        with self.assertRaises(self.panel_mod.PanelError):
            self.panel_mod.build_panel({"rgbi": {"points": {"2026-08-11": 100.0}}})


class TestRegistryLags(unittest.TestCase):
    """Реестр — часть контракта: лаги и окна опроса обязаны быть осмысленными."""

    def setUp(self):
        self.reg = need(self, "pipeline.lib.registry", "SERIES", "poll_due")

    def test_lags_are_non_negative_whole_days(self):
        for sid, spec in self.reg.SERIES.items():
            lag = spec.get("pub_lag_days")
            self.assertIsInstance(lag, int, f"{sid}: pub_lag_days не целое")
            # мутация: отрицательный лаг = сигнал доступен ДО конца периода.
            self.assertGreaterEqual(lag, 0, f"{sid}: отрицательный лаг")
            self.assertLessEqual(lag, 45, f"{sid}: лаг больше полутора месяцев")

    def test_daily_market_series_have_no_lag(self):
        # Закрытие известно в тот же день; лаг у дневного ряда сдвинул бы всю панель.
        for sid in ("imoex", "rgbi", "mcftr", "rtsi", "usd_cbr"):
            self.assertEqual(self.reg.SERIES[sid]["pub_lag_days"], 0, sid)

    def test_monthly_sources_have_sane_poll_window(self):
        for sid, spec in self.reg.SERIES.items():
            win = spec.get("poll_window")
            if win is None:
                continue
            lo, hi = win
            self.assertTrue(1 <= lo <= hi <= 31, f"{sid}: окно опроса {win}")

    def test_poll_due_respects_window(self):
        spec = self.reg.SERIES["orfr_flows"]      # poll_window (5, 17)
        self.assertFalse(self.reg.poll_due(spec, 4))
        self.assertTrue(self.reg.poll_due(spec, 5))
        self.assertTrue(self.reg.poll_due(spec, 17))
        self.assertFalse(self.reg.poll_due(spec, 18))
        # Ряд без окна опрашивается всегда.
        self.assertTrue(self.reg.poll_due(self.reg.SERIES["imoex"], 22))

    def test_core_and_state_series_are_registered(self):
        # Минимум из контракта §2: без этих рядов ядро и машина состояний не считаются.
        for sid in ("imoex", "imoex_value", "rgbi", "rvi", "mcftr", "mcxsm", "rtsi",
                    "cny_tom", "gld_tom", "usd_cbr", "key_rate", "zcyc", "rusfar3m",
                    "rucbhycp_yield", "rucbcpns_yield", "brent", "deposit_decade",
                    "urals_tax", "futoi_mx", "breadth"):
            self.assertIn(sid, self.reg.SERIES)

    def test_modes_reference_known_series(self):
        for mode, ids in self.reg.MODES.items():
            for sid in ids:
                self.assertIn(sid, self.reg.SERIES, f"режим {mode}: неизвестный ряд {sid}")


class TestTradingCalendar(unittest.TestCase):
    def setUp(self):
        self.dates = need(self, "pipeline.lib.dates", "is_trading_day", "prev_trading_day")

    def test_weekend_is_not_trading(self):
        self.assertTrue(self.dates.is_trading_day("2026-08-11"))   # вторник
        self.assertFalse(self.dates.is_trading_day("2026-08-08"))  # суббота
        self.assertFalse(self.dates.is_trading_day("2026-08-09"))  # воскресенье

    def test_fixed_holidays_and_transfers(self):
        # мутация: забыть перенос праздника, попавшего на выходной, -> пайплайн
        # каждый год ходит в ISS за заведомо пустыми днями.
        self.assertFalse(self.dates.is_trading_day("2026-01-07"))  # среда, каникулы
        self.assertFalse(self.dates.is_trading_day("2026-06-12"))  # пятница, 12 июня
        self.assertFalse(self.dates.is_trading_day("2026-03-09"))  # перенос с вс 08.03
        self.assertFalse(self.dates.is_trading_day("2026-05-11"))  # перенос с сб 09.05

    def test_prev_and_next_step_over_holidays(self):
        self.assertEqual(self.dates.prev_trading_day("2026-01-12"), date(2026, 1, 9))
        self.assertEqual(self.dates.next_trading_day("2026-06-11"), date(2026, 6, 15))
        self.assertEqual(self.dates.shift_trading_days("2026-08-11", -2), date(2026, 8, 7))
        self.assertEqual(self.dates.shift_trading_days("2026-08-07", 2), date(2026, 8, 11))

    def test_iter_trading_days(self):
        got = list(self.dates.iter_trading_days("2026-08-03", "2026-08-09"))
        self.assertEqual(len(got), 5)
        self.assertEqual(got[0], date(2026, 8, 3))
        self.assertEqual(got[-1], date(2026, 8, 7))

    def test_last_date_in_points_ignores_empty_values(self):
        # Точка со значением None — это «день был, данных нет», а не свежесть.
        # мутация: max(points) -> ряд выглядит свежим на пустом дне.
        pts = {"2026-08-10": 2293.32, "2026-08-11": None}
        self.assertEqual(self.dates.last_date_in_points(pts), "2026-08-10")
        self.assertIsNone(self.dates.last_date_in_points({}))


class TestMoscowTime(unittest.TestCase):
    """Внутри всё в UTC, наружу — МСК (UTC+3). Зафиксированные моменты, не часы."""

    def setUp(self):
        self.dates = need(self, "pipeline.lib.dates", "iso_utc", "parse_ts", "MSK")

    def test_msk_is_utc_plus_three(self):
        moment = self.dates.parse_ts("2026-08-11T16:05:12Z")
        self.assertEqual(moment.tzinfo, UTC)
        msk = moment.astimezone(self.dates.MSK)
        # мутация: смещение 0 или 4 часа -> подпись «данные на 19:05 МСК» соврёт.
        self.assertEqual((msk.hour, msk.minute), (19, 5))
        self.assertEqual(msk.utcoffset(), timedelta(hours=3))

    def test_iso_utc_round_trip(self):
        moment = datetime(2026, 8, 11, 19, 5, 12, tzinfo=self.dates.MSK)
        self.assertEqual(self.dates.iso_utc(moment), "2026-08-11T16:05:12Z")
        self.assertEqual(self.dates.parse_ts(self.dates.iso_utc(moment)),
                         moment.astimezone(UTC))

    def test_naive_timestamp_treated_as_utc(self):
        # Мы сами пишем метки без зоны только в UTC; трактовка «как местное время»
        # дала бы возраст источника на три часа меньше и спрятала протухание.
        self.assertEqual(self.dates.parse_ts("2026-08-11T16:05:12"),
                         datetime(2026, 8, 11, 16, 5, 12, tzinfo=UTC))

    def test_age_minutes_against_fixed_now(self):
        now = datetime(2026, 8, 11, 16, 5, 0, tzinfo=UTC)
        self.assertAlmostEqual(
            self.dates.age_minutes("2026-08-11T15:05:00Z", now=now), 60.0, places=6)
        self.assertAlmostEqual(
            self.dates.age_minutes("2026-08-11T16:05:00Z", now=now), 0.0, places=6)

    def test_last_trading_day_waits_for_close(self):
        # До 19:00 МСК дневной прогон обязан считать «последним» вчерашний день:
        # иначе промежуточное значение уедет в стор как закрытие.
        # мутация: require_close игнорируется -> в 18:00 запишется незакрытый день.
        midday = datetime(2026, 8, 11, 18, 0, tzinfo=self.dates.MSK)
        evening = datetime(2026, 8, 11, 19, 30, tzinfo=self.dates.MSK)
        self.assertEqual(self.dates.last_trading_day(now=midday), date(2026, 8, 10))
        self.assertEqual(self.dates.last_trading_day(now=evening), date(2026, 8, 11))
        self.assertEqual(self.dates.last_trading_day(now=midday, require_close=False),
                         date(2026, 8, 11))

    def test_last_trading_day_on_weekend(self):
        saturday = datetime(2026, 8, 8, 20, 0, tzinfo=self.dates.MSK)
        self.assertEqual(self.dates.last_trading_day(now=saturday), date(2026, 8, 7))


class TestDateFormats(unittest.TestCase):
    def setUp(self):
        self.dates = need(self, "pipeline.lib.dates", "parse_date", "fmt_ru", "month_end")

    def test_parse_known_forms(self):
        self.assertEqual(self.dates.parse_date("2026-08-11"), date(2026, 8, 11))
        self.assertEqual(self.dates.parse_date("2026-08-11T16:05:12"), date(2026, 8, 11))
        # ЦБ отдаёт dd.mm.yyyy — перепутанные местами день и месяц дают валидную,
        # но чужую дату, поэтому форма проверяется явно.
        self.assertEqual(self.dates.parse_date("11.08.2026"), date(2026, 8, 11))
        self.assertEqual(self.dates.parse_date("01/12/2026"), date(2026, 12, 1))
        with self.assertRaises(ValueError):
            self.dates.parse_date("11 августа 2026")

    def test_fmt_ru_separators(self):
        # XML_dynamic ЦБ требует dd/mm/yyyy, HTML-формы — dd.mm.yyyy.
        self.assertEqual(self.dates.fmt_ru("2026-08-01"), "01.08.2026")
        self.assertEqual(self.dates.fmt_ru("2026-08-01", "/"), "01/08/2026")

    def test_month_end_and_add_months_clamp(self):
        self.assertEqual(self.dates.month_end("2026-02-10"), date(2026, 2, 28))
        self.assertEqual(self.dates.month_end("2024-02-10"), date(2024, 2, 29))
        self.assertEqual(self.dates.month_end("2026-12-01"), date(2026, 12, 31))
        # мутация: сдвиг на 30 дней вместо месяца -> 31.01 + 1 мес даст 02.03.
        self.assertEqual(self.dates.add_months("2026-01-31", 1), date(2026, 2, 28))
        self.assertEqual(self.dates.add_months("2026-03-31", -1), date(2026, 2, 28))

    def test_iter_months_yields_only_completed(self):
        # мутация: отдавать текущий месяц -> месячный ряд получит точку в будущем.
        got = list(self.dates.iter_months("2026-01-01", "2026-03-15"))
        self.assertEqual(got, [date(2026, 1, 31), date(2026, 2, 28)])


if __name__ == "__main__":
    unittest.main()
