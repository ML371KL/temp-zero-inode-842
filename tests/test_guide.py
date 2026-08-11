"""Целостность руководства (web/guide.html) и его связи с панелью.

ПОЧЕМУ это тест, а не «посмотрим глазами». Руководство — единственное место, где
панель объясняет свои числа, и гниёт оно тихо: ссылка в оглавлении переживает
переименование раздела, кнопка с панели переживает переезд файла, а числа в
таблицах переживают реколибровку. Ни одно из этих расхождений не видно на экране
— страница выглядит целой.

Что здесь проверяется:
  1. каждая внутренняя ссылка оглавления ведёт в существующий раздел;
  2. панель ссылается на руководство, а руководство — обратно на панель;
  3. ключевые числа руководства совпадают с константами конвейера (иначе текст
     начнёт рассказывать про модель, которой уже нет).
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "web" / "guide.html"
INDEX = ROOT / "web" / "index.html"


def read(p):
    return p.read_text(encoding="utf-8")


class TestGuideStructure(unittest.TestCase):
    def setUp(self):
        if not GUIDE.exists():
            self.skipTest("нет web/guide.html")
        self.html = read(GUIDE)

    def test_toc_anchors_resolve(self):
        """Мутация: переименовать id раздела, забыв про оглавление."""
        anchors = set(re.findall(r'id="([\w-]+)"', self.html))
        links = re.findall(r'<a href="#([\w-]+)"', self.html)
        self.assertTrue(links, "в оглавлении не нашлось ни одной ссылки")
        missing = [a for a in links if a not in anchors]
        self.assertEqual(missing, [], f"ссылки оглавления ведут в никуда: {missing}")

    def test_every_section_is_in_toc(self):
        """Раздел, которого нет в оглавлении, читатель не найдёт."""
        sections = re.findall(r'<section class="guide__sec" id="([\w-]+)"', self.html)
        toc = set(re.findall(r'<a href="#([\w-]+)"', self.html))
        orphans = [s for s in sections if s not in toc]
        self.assertEqual(orphans, [], f"разделы вне оглавления: {orphans}")

    def test_links_back_to_panel(self):
        self.assertIn('href="./"', self.html, "из руководства нет пути обратно на панель")

    def test_no_placeholder_text(self):
        """Мутация: оставить рыбу в тексте."""
        for bad in ("TODO", "TBD", "Lorem", "XXX"):
            self.assertNotIn(bad, self.html, f"в руководстве осталась заглушка {bad}")


class TestPanelLinksGuide(unittest.TestCase):
    def setUp(self):
        if not INDEX.exists():
            self.skipTest("нет web/index.html")
        self.html = read(INDEX)

    def test_panel_links_to_guide(self):
        """Мутация: переименовать guide.html и забыть про кнопку на панели."""
        self.assertIn('href="guide.html"', self.html,
                      "на панели нет ссылки на руководство")
        self.assertTrue(GUIDE.exists(), "ссылка есть, а файла руководства нет")


class TestGuideMatchesConstants(unittest.TestCase):
    """Числа руководства обязаны совпадать с тем, что реально считает конвейер."""

    def setUp(self):
        if not GUIDE.exists():
            self.skipTest("нет web/guide.html")
        try:
            import sys
            sys.path.insert(0, str(ROOT))
            from pipeline.lib import constants
        except ImportError as exc:  # pragma: no cover
            self.skipTest(f"нет constants ({exc})")
        self.K = constants
        self.html = read(GUIDE)

    def test_cell_means_present(self):
        """Каждая ячейка описана в руководстве своей средней доходностью."""
        for key, cell in self.K.CELL_STATS.items():
            with self.subTest(cell=cell["label"]):
                # В тексте числа набраны по-русски: запятая и типографский минус.
                shown = f"{abs(cell['mean_fwd1m_pct']):.2f}".replace(".", ",")
                self.assertIn(shown, self.html,
                              f"средняя ячейки «{cell['label']}» ({shown}%) не найдена в руководстве")

    def test_cell_counts_present(self):
        for key, cell in self.K.CELL_STATS.items():
            with self.subTest(cell=cell["label"]):
                self.assertRegex(self.html, r">\s*%d\s*<" % cell["n"],
                                 f"число наблюдений ячейки «{cell['label']}» не найдено")

    def test_core_windows_match(self):
        """Окно z и обрезка описаны словами — они не должны разъехаться с кодом."""
        self.assertIn(str(self.K.Z_WINDOW_MONTHS), self.html)
        self.assertIn(str(self.K.Z_MIN_MONTHS), self.html)

    def test_all_second_layer_signals_documented(self):
        """Мутация: добавить сигнал в constants и забыть про руководство."""
        labels = {
            "mom63": "Трендследование",
            "dd252": "Покупка просадки",
            "switch_spread": "Спред дивдоходность",
            "rb_gap": "Рублёвая бочка",
            "futoi_z120": "Контр-позиционирование",
            "dy_trail": "Дивидендная доходность",
            "rgbi_mom21": "Моментум RGBI",
        }
        for sig in self.K.SECOND_LAYER:
            with self.subTest(signal=sig["id"]):
                needle = labels.get(sig["id"])
                self.assertIsNotNone(needle, f"сигнал {sig['id']} не описан в тесте — допишите")
                self.assertIn(needle, self.html,
                              f"сигнал {sig['id']} не описан в руководстве")


if __name__ == "__main__":
    unittest.main()
