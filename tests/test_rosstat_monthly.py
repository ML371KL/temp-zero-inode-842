"""Месячный ИПЦ Росстата из релиза — и честный статус между релизами.

Оплачено 09.11.2026: все xlsx в `/storage/mediabank/` начали отдавать 404 (с VPS и
с ноутбука, PDF при этом отдаются), страница «Цены» застыла на 13.03.2026, а свежие
релизы Росстат вешает в ленту новостей файлом PDF. Фетчер шёл по ссылке
`ipc_mes_02-2026.xlsx`, получал 404 и писал error. Теперь число берётся из релиза.

Фикстуры:
  * rosstat_cpi_release_2026-01.txt / _2025-12.txt — текст настоящих месячных
    релизов (копии веб-архива, сайт Росстата отдаёт на них 404). Числа совпадают с
    рядом в сторе: январь 2026 = 1,62, декабрь 2025 = 0,32;
  * rosstat_weekly_pdf_2026-10-28.txt — текст живого недельного PDF, извлечённый
    `orfr.pdf_text`: так рвёт строки и числа настоящий PDF Росстата;
  * rosstat_news_cards.html — разметка живой ленты; две карточки месячных релизов
    собраны по её образцу.
В сеть не ходим.
"""

import unittest
from datetime import date
from unittest import mock

from tests import fixture_text, need


class TestReleaseParse(unittest.TestCase):
    def setUp(self):
        self.r = need(self, "pipeline.fetch.rosstat", "parse_cpi_release")

    def test_январь_2026_из_настоящего_релиза(self):
        self.assertEqual(self.r.parse_cpi_release(fixture_text("rosstat_cpi_release_2026-01.txt")),
                         ("2026-01-31", 1.62))

    def test_декабрь__к_ноябрю_а_не_к_декабрю_прошлого_года(self):
        # В том же лиде стоят 105,59 (к декабрю 2024) и 109,52 (справочно). Мутация
        # «первое число после „составил“ без проверки базы» на другом порядке слов
        # дала бы годовую инфляцию вместо месячной.
        self.assertEqual(self.r.parse_cpi_release(fixture_text("rosstat_cpi_release_2025-12.txt")),
                         ("2025-12-31", 0.32))

    def test_база_не_прошлый_месяц__не_берётся(self):
        text = ("В январе 2026 г. по сравнению с январем 2025 г. индекс потребительских "
                "цен составил 106,00%.")
        self.assertIsNone(self.r.parse_cpi_release(text))

    def test_недельный_PDF_не_месячный_релиз(self):
        # «За период с 20 по 26 октября … составил 100,08%» — недельная оценка.
        self.assertIsNone(self.r.parse_cpi_release(
            fixture_text("rosstat_weekly_pdf_2026-10-28.txt")))

    def test_текст_из_PDF_с_разорванными_словами_и_числами(self):
        # Так извлекается настоящий PDF Росстата (см. недельную фикстуру): слова и
        # числа разорваны переводами строк, после «цен» стоит сноска.
        text = ("Срочная информация\n \n1\n3\n ноября 2026 года\n№ \n170\nОБ ИНДЕКСЕ\n \n"
                "ПОТРЕБИТЕЛЬСКИХ ЦЕН \nВ ОКТЯБРЕ 2026 ГОДА\n1\n \nВ \nок\nтябре 2026\n \n"
                "г. по сравнению с \nсентябр\nем 2026 г. индекс потребительских цен\n2\n"
                ", составил 10\n0,52\n%, (в октябре 2025 г. \n–\n 100,50%).")
        self.assertEqual(self.r.parse_cpi_release(text), ("2026-10-31", 0.52))

    def test_буква_в_из_соседнего_слова_не_съедает_совпадение(self):
        # Текст разбирается без пробелов: «Здравоохранение в январе» -> «…ениевянваре».
        # С шаблоном «в[а-я]+» совпадение начиналось с «в» внутри «здраВоохранение»,
        # месяц «оохранениевянваре» не узнавался, а верное совпадение перекрывалось.
        text = ("Здравоохранение в январе 2026 г. по сравнению с декабрем 2025 г. индекс "
                "потребительских цен составил 101,62%.")
        self.assertEqual(self.r.parse_cpi_release(text), ("2026-01-31", 1.62))

    def test_май_и_март_не_путаются(self):
        text = ("В мае 2026 г. по сравнению с апрелем 2026 г. индекс потребительских цен "
                "составил 100,43%.")
        self.assertEqual(self.r.parse_cpi_release(text), ("2026-05-31", 0.43))
        text = ("В марте 2026 г. по сравнению с февралем 2026 г. индекс потребительских "
                "цен составил 100,39%.")
        self.assertEqual(self.r.parse_cpi_release(text), ("2026-03-31", 0.39))


class TestReleaseLinks(unittest.TestCase):
    def setUp(self):
        self.r = need(self, "pipeline.fetch.rosstat", "monthly_releases")

    def test_в_ленте_находятся_только_месячные_релизы(self):
        got = self.r.monthly_releases(fixture_text("rosstat_news_cards.html"))
        self.assertEqual(sorted((y, m) for _u, y, m in got), [(2026, 9), (2026, 10)])
        urls = [u for u, _y, _m in got]
        self.assertIn("https://rosstat.gov.ru/storage/mediabank/ipc_m10.pdf", urls)
        # недельная «Об оценке индекса…» и «динамика цен на бензин» — мимо
        self.assertFalse([u for u in urls if "ipc11_02" in u or "ben" in u])

    def test_относительная_ссылка_становится_полной(self):
        html = '<a href="/central-news/document/1">Об индексе потребительских цен в мае 2026 года</a>'
        self.assertEqual(self.r.monthly_releases(html),
                         [("https://rosstat.gov.ru/central-news/document/1", 2026, 5)])


class TestMonthlyStatus(unittest.TestCase):
    """Статус ряда в окне опроса 9–18: «ещё не вышел» — не авария."""

    OCT_LEAD = ("В октябре 2026 г. по сравнению с сентябрем 2026 г. индекс потребительских "
                "цен составил 100,52%.")

    def setUp(self):
        self.r = need(self, "pipeline.fetch.rosstat", "cpi_monthly", "_cpi_monthly_xlsx")
        self.cards = fixture_text("rosstat_news_cards.html")
        self.no_monthly = "<div>" + "".join(
            line for line in self.cards.splitlines() if "индексе" not in line) + "</div>"
        self.xlsx_dead = ("cpi_monthly", {}, {"status": "error",
                                              "note": "xlsx не разобрался: HTTP 404"})

    def run_case(self, today, known, listing, release_text=None, listing_error=None):
        def page(url, **_kw):
            if listing_error:
                raise self.r.FetchError(listing_error)
            return listing
        patches = [
            mock.patch.object(self.r, "_today_msk", return_value=today),
            mock.patch.object(self.r, "_stored_last", return_value=known),
            mock.patch.object(self.r, "get_text", side_effect=page),
            mock.patch.object(self.r, "_release_text", return_value=release_text or ""),
            mock.patch.object(self.r, "_cpi_monthly_xlsx", return_value=self.xlsx_dead),
        ]
        for p in patches:
            p.start()
        self.addCleanup(mock.patch.stopall)
        return self.r.cpi_monthly()

    def test_релиз_ещё_не_вышел__ok_без_новых_точек(self):
        # Ровно 09.11.2026: в ряду сентябрь, октябрьского релиза нет. Мутация «нет
        # релиза = error» возвращает ложную тревогу «источник не отвечает».
        _sid, points, meta = self.run_case(date(2026, 11, 9), "2026-09-30", self.no_monthly)
        self.assertEqual(meta["status"], "ok")
        self.assertEqual(points, {})
        self.assertEqual(meta["asof"], "2026-09-30")
        self.assertIn("ещё не вышел", meta["note"])

    def test_к_17_числу_релиза_нет__ошибка(self):
        _sid, points, meta = self.run_case(date(2026, 11, 17), "2026-09-30", self.no_monthly)
        self.assertEqual(meta["status"], "error")
        self.assertIn("10.2026", meta["note"])

    def test_релиз_вышел__точка_за_октябрь(self):
        _sid, points, meta = self.run_case(date(2026, 11, 13), "2026-09-30", self.cards,
                                           release_text=self.OCT_LEAD)
        self.assertEqual(meta["status"], "ok")
        self.assertEqual(points, {"2026-10-31": 0.52})
        self.assertEqual(meta["asof"], "2026-10-31")
        self.assertIn("ipc_m10.pdf", meta["url"])

    def test_релиз_есть_а_число_не_разобралось__ошибка(self):
        _sid, points, meta = self.run_case(date(2026, 11, 13), "2026-09-30", self.cards,
                                           release_text="Сменился формат релиза.")
        self.assertEqual(meta["status"], "error")
        self.assertIn("сменился формат", meta["note"])

    def test_октябрь_уже_в_ряду__ok_и_после_срока(self):
        _sid, points, meta = self.run_case(date(2026, 11, 18), "2026-10-31", self.no_monthly)
        self.assertEqual(meta["status"], "ok")
        self.assertEqual(meta["asof"], "2026-10-31")

    def test_лента_не_открылась__ошибка(self):
        _sid, _points, meta = self.run_case(date(2026, 11, 9), "2026-09-30", "",
                                            listing_error="HTTP 503")
        self.assertEqual(meta["status"], "error")
        self.assertIn("не открылась", meta["note"])

    def test_в_ленте_только_прошлый_релиз_после_срока__ошибка(self):
        sept_only = "<div>" + "".join(
            line for line in self.cards.splitlines() if "октябре 2026 года" not in line) + "</div>"
        sept_lead = ("В сентябре 2026 г. по сравнению с августом 2026 г. индекс "
                     "потребительских цен составил 100,35%.")
        _sid, _points, meta = self.run_case(date(2026, 11, 17), "2026-09-30", sept_only,
                                            release_text=sept_lead)
        self.assertEqual(meta["status"], "error")

    def test_январь_ждёт_декабрь_прошлого_года(self):
        _sid, _points, meta = self.run_case(date(2027, 1, 12), "2026-11-30", self.no_monthly)
        self.assertEqual(meta["status"], "ok")
        self.assertIn("12.2026", meta["note"])

    def test_вернувшийся_xlsx_подхватывается(self):
        alive = ("cpi_monthly", {"2026-10-31": 0.52},
                 {"status": "ok", "note": "месяцев в ряду: 1", "asof": "2026-10-31"})
        self.run_case(date(2026, 11, 9), "2026-09-30", self.no_monthly)  # ставит заглушки
        with mock.patch.object(self.r, "_cpi_monthly_xlsx", return_value=alive):
            _sid, points, meta = self.r.cpi_monthly()
        self.assertEqual(meta["status"], "ok")
        self.assertEqual(points, {"2026-10-31": 0.52})
        self.assertIn("релиз: нового нет", meta["note"])


if __name__ == "__main__":
    unittest.main()
