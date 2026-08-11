"""Разбор публикаций: пресс-центр Минфина и имена выпусков ОРФР.

Аудит 11.08.2026 показал, что два ряда не собирались НИ РАЗУ, а строка прогона при
этом выглядела успехом:
  * budget_deficit — Минфин пишет «бюджет сложился с дефицитом в размере 6 455 млрд
    рублей», а шаблон требовал слова «составил»;
  * fnb — ключ заголовка искал «фондЕ национального благосостояния» (в живом
    заголовке «фондА»), а число напечатано в МИЛЛИОНАХ, тогда как шаблоны требовали
    «млрд». Разница ровно в 1000 раз, и без нормировки ряд был бы хуже пустого.
Фикстуры — замороженные фрагменты живых релизов (11.08.2026 и 10.08.2026) вместе с
ловушками: рядом стоят расходы 28 567 млрд и десятки строк «… млн рублей».

В сеть не ходим: press_items/_press_article подменяются целиком.
"""

import unittest
from unittest import mock

from tests import fixture_text, need


class MinfinPressCase(unittest.TestCase):
    def setUp(self):
        self.minfin = need(self, "pipeline.fetch.minfin", "budget", "fnb")

    def run_on(self, fixture, published):
        text = fixture_text(fixture)
        title = text.splitlines()[0]
        return mock.patch.object(self.minfin, "press_items",
                                 return_value=[("https://minfin.gov.ru/ru/press-center/"
                                                "?id_4=00000", title)]), \
            mock.patch.object(self.minfin, "_press_article",
                              return_value=(text, published))


class TestBudgetDeficit(MinfinPressCase):
    def test_deficit_without_the_word_sostavil(self):
        # мутация: оставить только шаблон «дефицит … составил» -> ряд пуст навсегда,
        # а в журнале «budget_deficit ok точек=0».
        p1, p2 = self.run_on("minfin_budget.txt", "2026-08-11")
        with p1, p2:
            sid, points, meta = self.minfin.budget()
        self.assertEqual(sid, "budget_deficit")
        self.assertEqual(meta["status"], "ok")
        # знак: дефицит — минус (docs/SOURCES.md), месяц берётся из «января-июля 2026»
        self.assertEqual(points, {"2026-07-31": -6455.0})

    def test_expenses_are_not_the_deficit(self):
        # В том же тексте стоят расходы 28 567 и доходы 22 112 млрд руб.
        p1, p2 = self.run_on("minfin_budget.txt", "2026-08-11")
        with p1, p2:
            _sid, points, _meta = self.minfin.budget()
        self.assertNotIn(-28567.0, points.values())
        self.assertNotIn(-22112.0, points.values())


class TestFnbLiquidPart(MinfinPressCase):
    def test_millions_are_normalised_to_billions(self):
        # мутация: взять число как есть -> 3 692 785,4 вместо 3 692,8 млрд руб.
        p1, p2 = self.run_on("minfin_fnb.txt", "2026-08-10")
        with p1, p2:
            sid, points, meta = self.minfin.fnb()
        self.assertEqual(sid, "fnb")
        self.assertEqual(meta["status"], "ok")
        self.assertEqual(points, {"2026-07-31": 3692.7854})

    def test_dollar_amount_is_not_the_answer(self):
        # В той же фразе следом идёт «46 242,3 млн долл. США».
        p1, p2 = self.run_on("minfin_fnb.txt", "2026-08-10")
        with p1, p2:
            _sid, points, _meta = self.minfin.fnb()
        self.assertNotIn(46.2423, points.values())

    def test_balance_on_the_first_day_belongs_to_the_previous_month(self):
        # «по состоянию на 1 августа» — это остаток на конец ИЮЛЯ. Мутация: брать месяц
        # публикации -> точка 2026-08-31, то есть дата из будущего в живом сторе.
        p1, p2 = self.run_on("minfin_fnb.txt", "2026-08-10")
        with p1, p2:
            _sid, points, _meta = self.minfin.fnb()
        self.assertEqual(max(points), "2026-07-31")


class TestOrfrReleaseName(unittest.TestCase):
    """Период выпуска ОРФР берётся из имени PDF — и оно бывает не тем, чем кажется."""

    def setUp(self):
        self.orfr = need(self, "pipeline.fetch.orfr", "_period_from_name", "latest_pdf")

    def test_year_range_is_not_a_month(self):
        # На сайте ЦБ реально висит ORFR_2024-25-1.pdf. мутация: принять «25» за месяц
        # -> _month_end падает ValueError, run.py красит всю семью orfr_flows в error,
        # и ручной фолбэк inputs/orfr.yml уже не отрабатывает.
        self.assertIsNone(self.orfr._period_from_name("/File/55089/ORFR_2024-25-1.pdf"))
        self.assertEqual(self.orfr.latest_pdf(
            '<a href="/File/55089/ORFR_2024-25-1.pdf">обзор</a>'), (None, None))

    def test_normal_and_glued_names_still_work(self):
        self.assertEqual(self.orfr._period_from_name("/File/59728/ORFR_2026-2.pdf"), "2026-02")
        # склейку «декабрь-январь» относим к последнему месяцу периода
        self.assertEqual(self.orfr._period_from_name("/File/1/ORFR_2025-12_2026-01.pdf"),
                         "2026-01")


if __name__ == "__main__":
    unittest.main()
