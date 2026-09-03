"""Дробления акций в корзине ширины рынка.

Дефект, закрытый здесь навсегда (найден 03.09.2026 на сторе 10.08.2026): ISS отдаёт
TQBR СЫРЫМИ ценами, и в день дробления цена меняется в разы. MA200 после этого ещё
двести торговых дней сравнивает рубли после дробления с рублями до него, поэтому бит
«выше своей MA200» залипает на одном значении: у GMKN 199 дней подряд, у PLZL 195,
у TRNFP 260, у VTBR (обратное дробление) 185 дней на противоположном значении.
Всего 919 бумаго-дней, каждый из которых стоил ширине 1/45 = 2,2 п.п.

Вторая половина защиты — от «лечения» здоровых данных: поправка обязана быть ЯВНЫМ
списком, а не порогом по величине скачка. При пороге 1,4x в поправку попали бы
24.02.2022 у всех бумаг сразу, арест Евтушенкова (AFKS 17.09.2014) и реопен VKCO
(29.03.2022) — настоящие движения цены.
"""

import os
import unittest
from datetime import date, timedelta
from unittest import mock

from tests import need
from tests.test_fetch_window import IssCase, _history


class TestApplyCorporateActions(unittest.TestCase):
    """Чистая функция пересчёта: что умножается, что нет."""

    def setUp(self):
        self.iss = need(self, "pipeline.fetch.iss",
                        "CORPORATE_ACTIONS", "_apply_corporate_actions",
                        "BREADTH_TICKERS", "_pct_above_ma")

    def test_only_prices_before_the_event_are_restated(self):
        pts = {"2024-04-04": 15000.0, "2024-04-05": 15054.0,
               "2024-04-08": 152.96, "2024-04-09": 154.0}
        out = self.iss._apply_corporate_actions("GMKN", pts)
        self.assertAlmostEqual(out["2024-04-04"], 150.0)
        self.assertAlmostEqual(out["2024-04-05"], 150.54)
        # День события и всё после — уже в новом масштабе, трогать нельзя.
        self.assertAlmostEqual(out["2024-04-08"], 152.96)
        self.assertAlmostEqual(out["2024-04-09"], 154.0)

    def test_reverse_split_multiplies_up(self):
        # VTBR 5000:1 — единственное событие списка, где цену надо поднимать.
        out = self.iss._apply_corporate_actions(
            "VTBR", {"2024-07-12": 0.01992, "2024-07-15": 92.95})
        self.assertAlmostEqual(out["2024-07-12"], 99.6)
        self.assertAlmostEqual(out["2024-07-15"], 92.95)

    def test_two_events_accumulate(self):
        # Множители обязаны перемножаться, а не затирать друг друга: точка старше
        # обоих дроблений проходит через оба.
        table = {"TEST": [("2020-01-01", 0.1), ("2022-01-01", 0.5)]}
        with mock.patch.dict(self.iss.CORPORATE_ACTIONS, table, clear=False):
            out = self.iss._apply_corporate_actions(
                "TEST", {"2019-06-01": 1000.0, "2021-06-01": 100.0, "2023-06-01": 50.0})
        self.assertAlmostEqual(out["2019-06-01"], 50.0)   # x0,1 и x0,5
        self.assertAlmostEqual(out["2021-06-01"], 50.0)   # только x0,5
        self.assertAlmostEqual(out["2023-06-01"], 50.0)   # не трогаем

    def test_ticker_without_events_is_untouched(self):
        pts = {"2024-04-08": 100.0}
        self.assertIs(self.iss._apply_corporate_actions("SBER", pts), pts)

    def test_real_moves_are_not_in_the_table(self):
        # Список — не эвристика по скачку. Эти три бумаги ходили на новостях,
        # и «поправка» их движения была бы порчей данных.
        for ticker in ("AFKS", "VKCO", "SBER"):
            self.assertNotIn(ticker, self.iss.CORPORATE_ACTIONS)

    def test_table_is_well_formed_and_covers_the_basket(self):
        basket = set(self.iss.BREADTH_TICKERS)
        for ticker, events in self.iss.CORPORATE_ACTIONS.items():
            self.assertIn(ticker, basket,
                          f"{ticker} не в корзине ширины — поправка ни на что не влияет")
            self.assertTrue(events, f"{ticker}: пустой список событий")
            for day, factor in events:
                date.fromisoformat(day)  # бросит при опечатке в дате
                self.assertGreater(factor, 0, f"{ticker} {day}: множитель обязан быть > 0")


class TestSplitBreaksTheMaBit(unittest.TestCase):
    """Тот самый дефект: без поправки бит «выше MA200» залипает на 200 дней."""

    def setUp(self):
        self.iss = need(self, "pipeline.fetch.iss",
                        "_apply_corporate_actions", "_pct_above_ma")

    def _series(self):
        """Ровно растущая бумага, которую 2024-04-08 дробят 1:100.

        Экономически цена растёт КАЖДЫЙ день, то есть честный бит «выше своей
        MA200» обязан быть единицей и до, и после дробления.
        """
        pts, day, price = {}, date(2023, 6, 1), 15000.0
        while day < date(2024, 4, 8):
            pts[day.isoformat()] = price
            price *= 1.001
            day += timedelta(days=1)
        while day < date(2024, 7, 1):
            pts[day.isoformat()] = price / 100.0
            price *= 1.001
            day += timedelta(days=1)
        return pts

    def test_raw_prices_flip_the_bit_and_the_fix_restores_it(self):
        raw = self._series()
        after = [d for d in sorted(raw) if d >= "2024-04-08"]

        broken = self.iss._pct_above_ma({"GMKN": raw}, min_tickers=1)
        got = [broken[d] for d in after if d in broken]
        self.assertTrue(got, "проверка бессмысленна: MA200 не посчиталась ни на одном дне")
        self.assertEqual(set(got), {0.0},
                         "без поправки растущая бумага обязана читаться как «ниже MA200»")

        fixed = self.iss._pct_above_ma(
            {"GMKN": self.iss._apply_corporate_actions("GMKN", raw)}, min_tickers=1)
        got = [fixed[d] for d in after if d in fixed]
        self.assertEqual(set(got), {1.0},
                         "с поправкой растущая бумага обязана читаться как «выше MA200»")


class TestStoreKeepsRawPrices(IssCase):
    """Поправка живёт только в счётной копии: ряд px_<тикер> в сторе остаётся сырым."""

    START, END = "2024-04-01", "2024-04-08"
    FRESH = "2024-04-08"

    def test_px_series_is_stored_unadjusted(self):
        names = self.iss.BREADTH_TICKERS[:20]
        self.assertIn("GMKN", names, "тест опирается на GMKN в первой двадцатке корзины")
        for n, ticker in enumerate(names):
            base = 15000.0 if ticker == "GMKN" else 100.0 + n
            pts = {(date(2023, 10, 1) + timedelta(days=i)).isoformat(): base + i * 0.1
                   for i in range(170)}
            self.store.upsert_points(f"px_{ticker.lower()}", pts)

        def responder(url):
            ticker = url.split("/securities/")[1].split(".json")[0].upper()
            close = 152.96 if ticker == "GMKN" else 500.0
            return _history([[self.FRESH, close, None]],
                            columns=("TRADEDATE", "CLOSE", "LEGALCLOSEPRICE"))

        self.serve(responder)
        out = self.iss.breadth(tickers=names, start=self.START, end=self.END)
        px = next(p for sid, p, _m in out if sid == "px_gmkn")
        # Мутация, от которой защищаемся: применить поправку к history и вернуть
        # её же в стор — тогда цена до дробления навсегда уедет в 150,00, и
        # отличить «поправлено» от «источник так отдал» станет нельзя.
        self.assertAlmostEqual(px[self.FRESH], 152.96)
        self.assertNotIn("2023-10-01", px, "в стор пишутся только свежие точки окна")

        meta = next(m for sid, _p, m in out if sid == "px_gmkn")
        self.assertIn("СЫРЫЕ", meta["note"])


if __name__ == "__main__":
    unittest.main()
