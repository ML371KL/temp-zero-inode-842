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


def ru_pct(value, nd, plus=True):
    """Число так, как оно набрано в руководстве: запятая, типографский минус, знак."""
    body = f"{abs(value):.{nd}f}".replace(".", ",")
    sign = "−" if value < 0 else ("+" if plus else "")
    return f"{sign}{body}%"


# Подпись строки: «бык · стресс · облигации ок» -> (тренд, волатильность, облигации).
# Слова взяты из самой вёрстки; третий признак пишется и как «ок», и как «облигации ок».
_BITS = ({"бык": 1, "медведь": 0}, {"стресс": 1, "спокойно": 0}, None)


def cell_table(html):
    """Разметка ИМЕННО таблицы ячеек.

    Искать по всему документу нельзя: таблиц в руководстве пять, и первая же
    `<caption>` принадлежит таблице состава ядра.
    """
    table = re.search(r'<table[^>]*>(?:(?!</table>).)*?<caption>Ячейки отсортированы.*?</table>',
                      html, re.S)
    if table is None:
        raise AssertionError("в руководстве не нашлась таблица ячеек")
    return table.group(0)


def cell_rows(html):
    """Строки таблицы ячеек -> [{key, title, dim, median, mean, worst, hit, n, tone}]."""
    body = re.search(r"<tbody>(.*?)</tbody>", cell_table(html), re.S)
    if body is None:
        raise AssertionError("у таблицы ячеек нет tbody")

    rows = []
    for chunk in re.findall(r"<tr>(.*?)</tr>", body.group(1), re.S):
        head = re.search(r'<th scope="row">(.*?)<br>\s*<span class="guide__dim">(.*?)</span>',
                         chunk, re.S)
        if head is None:
            raise AssertionError(f"строка таблицы без названия и признаков: {chunk[:80]}")
        dim = re.sub(r"\s+", " ", head.group(2)).strip()
        parts = [p.strip() for p in dim.split("·")]
        if len(parts) != 3:
            raise AssertionError(f"в подписи «{dim}» не три признака")
        key = (_BITS[0].get(parts[0]), _BITS[1].get(parts[1]),
               1 if "стресс" in parts[2] else 0)

        cells = re.findall(r'<td(?:\s+class="(tone-\w+)")?>(.*?)</td>', chunk, re.S)
        if len(cells) < 6:
            raise AssertionError(f"в строке «{dim}» {len(cells)} колонок вместо шести")
        vals = [re.sub(r"<[^>]+>", "", v).strip() for _, v in cells]
        rows.append({
            "key": key,
            "title": re.sub(r"<[^>]+>", "", head.group(1)).strip(),
            "dim": dim,
            "median": vals[0], "mean": vals[1], "worst": vals[2],
            "hit": vals[3], "n": vals[4],
            "tone": {"median": cells[0][0], "mean": cells[1][0], "worst": cells[2][0]},
        })
    return rows


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

    def test_cell_table_matches_constants(self):
        """Таблица ячеек разбирается по строкам и сверяется со своей ячейкой.

        До 13.08.2026 сверка шла подстрокой по всему документу: «есть ли где-нибудь
        на странице 2,94». Такая проверка зелена, пока числа просто присутствуют, —
        она не видит ни перепутанных местами строк, ни медианы, уехавшей в колонку
        среднего, ни числа из соседней ячейки. Восемь ячеек с похожими величинами
        (+0,93 / +0,85 / +0,54 / +0,51) — ровно тот случай, где перестановка не
        ловится совпадением набора чисел.

        Разбираем строку целиком: признаки состояния из подписи задают ключ
        CELL_STATS, шесть колонок — шесть величин этой ячейки.
        """
        rows = cell_rows(self.html)
        self.assertEqual(len(rows), len(self.K.CELL_STATS),
                         f"строк в таблице {len(rows)}, ячеек в модели {len(self.K.CELL_STATS)}")
        self.assertEqual(len({r["key"] for r in rows}), len(rows),
                         "две строки таблицы описывают одну и ту же ячейку")

        for row in rows:
            cell = self.K.CELL_STATS.get(row["key"])
            with self.subTest(cell=row["title"], key=row["key"]):
                self.assertIsNotNone(
                    cell, f"признаки «{row['dim']}» не соответствуют ни одной ячейке модели")
                self.assertEqual(row["title"].lower(), cell["label"],
                                 "название строки разошлось с label ячейки")
                for col, field, nd in (("median", "median_fwd1m_pct", 2),
                                       ("mean", "mean_fwd1m_pct", 2),
                                       ("worst", "worst_pct", 1)):
                    self.assertEqual(row[col], ru_pct(cell[field], nd),
                                     f"колонка «{col}»: в руководстве {row[col]}, "
                                     f"в константах {ru_pct(cell[field], nd)}")
                self.assertEqual(row["hit"], f"{round(cell['hit'] * 100)}%",
                                 "доля плюсовых месяцев разошлась с hit")
                self.assertEqual(row["n"], str(cell["n"]), "число наблюдений разошлось с n")

    def test_cell_table_tone_matches_sign(self):
        """Минус, покрашенный зелёным, читается как плюс — и наоборот.

        Знак в таблице несёт весь смысл (среднее токсичной ячейки −2,94% против
        медианы +0,64%), а цвет читается раньше цифры.
        """
        for row in cell_rows(self.html):
            for col in ("median", "mean", "worst"):
                want = "tone-neg" if row[col].startswith("−") else "tone-pos"
                with self.subTest(cell=row["title"], col=col):
                    self.assertEqual(row["tone"][col], want,
                                     f"{row[col]} покрашено как {row['tone'][col]}")

    def test_cell_caption_quotes_toxic_cell(self):
        """Подпись к таблице объясняет разрыв среднего и медианы конкретными числами."""
        toxic = self.K.CELL_STATS[(0, 1, 1)]
        caption = re.search(r"<caption>(.*?)</caption>", cell_table(self.html), re.S)
        self.assertIsNotNone(caption, "у таблицы ячеек пропала подпись")
        text = caption.group(1)
        for field, nd in (("mean_fwd1m_pct", 2), ("median_fwd1m_pct", 2)):
            self.assertIn(ru_pct(toxic[field], nd), text,
                          f"подпись рассказывает про токсичную ячейку не её числами ({field})")

    def test_monitor_tiles_counted_correctly(self):
        """Сколько тайлов на панели, столько же обещано словами и описано в списке.

        Руководство говорило «Пятнадцать тайлов», а панель показывала шестнадцать
        и описывала шестнадцать: счёт словами отстал на один тайл и никем не
        проверялся — числительное прописью не совпадает ни с какой константой, так
        что разъехаться оно может только молча. Считаем от кода: сколько вызовов
        `_tile("id"` в конвейере, столько и должно быть.
        """
        source = (ROOT / "pipeline" / "compute" / "monitors.py").read_text(encoding="utf-8")
        built = set(re.findall(r'_tile\(\s*"([a-z_0-9]+)"', source))
        self.assertGreater(len(built), 5, "тайлы в конвейере не нашлись — сменился вызов?")

        section = re.search(r'<section class="guide__sec" id="monitors">(.*?)</section>',
                            self.html, re.S)
        self.assertIsNotNone(section, "в руководстве нет раздела мониторов")
        described = re.findall(r"<dt>(.*?)</dt>", section.group(1), re.S)
        self.assertEqual(len(described), len(built),
                         f"описано тайлов {len(described)}, конвейер собирает {len(built)}")

        words = {12: "Двенадцать", 13: "Тринадцать", 14: "Четырнадцать", 15: "Пятнадцать",
                 16: "Шестнадцать", 17: "Семнадцать", 18: "Восемнадцать",
                 19: "Девятнадцать", 20: "Двадцать"}
        word = words.get(len(built))
        self.assertIsNotNone(word, f"числительное для {len(built)} не описано в тесте — допишите")
        self.assertIn(f"{word} тайлов", section.group(1),
                      f"вступление раздела обещает не {len(built)} тайлов")

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
