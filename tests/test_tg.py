"""Телеграм-зеркало как транспорт: разбор превью канала и защита от подмены числа.

Зеркало появилось потому, что minfin.gov.ru отвечает 503 с прод-машины и три ряда не
собрались ни разу (docs/LATENCY.md §3.3). Опасность зеркала ровно одна и она тихая:
взять ЧУЖОЕ число вместо релиза ведомства. Поэтому здесь проверяются не «функция
вернула список», а конкретные ловушки живой ленты, каждая из которых уже стояла в
замороженной странице 12.08.2026:

  * дайджест «📰 ТАСС: … 📰 Российская газета: Объем ФНБ … 12,72 трлн» — заголовки те
    же, числа чужие;
  * релиз, начинающийся строкой из одних эмодзи («📈 📈»), — наивный `first line`
    ищет ключевые слова в двух картинках;
  * вечерняя публикация: превью отдаёт UTC, а период считается по московскому дню.

В сеть не ходим: подменяется `tg.get_text`.
"""

import unittest
from unittest import mock

from tests import fixture_text, need


class TgPreviewCase(unittest.TestCase):
    def setUp(self):
        self.tg = need(self, "pipeline.fetch.tg", "parse_page", "messages", "find",
                       "is_reprint", "headline")
        self.page = fixture_text("tg_minfin.html")

    def msgs(self):
        return self.tg.parse_page(self.page, "minfin")

    def test_разбираются_все_сообщения_с_текстом(self):
        got = self.msgs()
        self.assertEqual([m["id"] for m in got], [9004, 9014, 9016, 9017])
        for m in got:
            self.assertTrue(m["text"], f"пустой текст у {m['id']}")
            self.assertTrue(m["url"].startswith("https://t.me/minfin/"))

    def test_текст_не_обрывается_на_вложенном_блоке(self):
        # Наивное «(.*?)</div>» режет сообщение на первом вложенном div, и число из
        # середины релиза (136,17 млрд) в текст уже не попадает.
        ngd = next(m for m in self.msgs() if m["id"] == 9004)
        self.assertIn("136,17", ngd["text"])
        self.assertIn("21,94", ngd["text"])

    def test_заголовок_пропускает_строку_из_эмодзи(self):
        fnb = next(m for m in self.msgs() if m["id"] == 9014)
        self.assertEqual(fnb["head"],
                         "О результатах размещения средств Фонда национального благосостояния")
        self.assertEqual(self.tg.headline("📈 📈\nО результатах\nдальше"), "О результатах")

    def test_дайджест_перепечаток_отбрасывается(self):
        digest = next(m for m in self.msgs() if m["id"] == 9016)
        self.assertTrue(self.tg.is_reprint(digest["text"]))
        # и он реально содержит ловушку: чужое число про ФНБ
        self.assertIn("12,72 трлн", digest["text"])
        own = next(m for m in self.msgs() if m["id"] == 9017)
        self.assertFalse(self.tg.is_reprint(own["text"]))

    def test_find_ищет_по_заголовку_и_не_берёт_перепечатки(self):
        with mock.patch.object(self.tg, "get_text", return_value=self.page):
            found = self.tg.find("minfin", ("национального благосостояния",))
        self.assertEqual([m["id"] for m in found], [9014],
                         "дайджест с тем же словосочетанием попал в кандидаты")

    def test_время_переводится_в_московское(self):
        # 21:30 UTC — это 00:30 следующего дня в Москве; период релиза считается по
        # московской дате, иначе вечерняя публикация уезжает на сутки назад.
        _at, day = self.tg._when("2026-08-11T21:30:00+00:00")
        self.assertEqual(day, "2026-08-12")
        _at, day = self.tg._when("2026-08-11T16:15:12+00:00")
        self.assertEqual(day, "2026-08-11")
        self.assertEqual(self.tg._when("не дата"), (None, None))

    def test_страница_без_сообщений_не_падает(self):
        self.assertEqual(self.tg.parse_page("<html><body>ничего</body></html>"), [])

    def test_листание_останавливается_на_повторах(self):
        # Одна и та же страница на каждый запрос: цикл обязан прекратиться, а не
        # копить дубли до потолка MAX_PAGES.
        with mock.patch.object(self.tg, "get_text", return_value=self.page) as fake:
            got = self.tg.messages("minfin", pages=5)
        self.assertEqual(len(got), 4)
        self.assertEqual(fake.call_count, 2, "вторая страница должна быть последней")


if __name__ == "__main__":
    unittest.main()
