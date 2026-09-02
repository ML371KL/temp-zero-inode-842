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
     начнёт рассказывать про модель, которой уже нет): статистика ячеек, окна z,
     состав второго ряда, число тайлов, пороги слоя решения (аудит 02.09.2026),
     порог здоровья ×12, три режима и их состав.

Проверки констант, которых в старом constants.py ещё нет (DECISION, REGIMES),
пропускаются с явным сообщением, а не падают: руководство и константы правятся
разными руками, и красный тест «нет константы» ничего бы не сказал о тексте.
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


def strip_tags(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s)).strip()


# Подпись строки: «бык · стресс · облигации ок» -> (тренд, волатильность, облигации).
# Слова взяты из самой вёрстки; третий признак пишется и как «ок», и как «облигации ок».
_BITS = ({"бык": 1, "медведь": 0}, {"стресс": 1, "спокойно": 0}, None)


def cell_key(dim):
    """«медведь · стресс · стресс» -> (0, 1, 1)."""
    parts = [p.strip() for p in dim.split("·")]
    if len(parts) != 3:
        raise AssertionError(f"в подписи «{dim}» не три признака")
    return (_BITS[0].get(parts[0]), _BITS[1].get(parts[1]), 1 if "стресс" in parts[2] else 0)


def cell_table(html):
    """Разметка ИМЕННО таблицы ячеек.

    Искать по всему документу нельзя: таблиц в руководстве больше пяти, и первая же
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
        key = cell_key(dim)

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


def regime_rows(html):
    """Таблица трёх режимов -> [{label, cells: [(t,v,b)…], n}]."""
    table = re.search(r'<table[^>]*>(?:(?!</table>).)*?<caption>Три режима.*?</table>', html, re.S)
    if table is None:
        raise AssertionError("в руководстве не нашлась таблица режимов")
    body = re.search(r"<tbody>(.*?)</tbody>", table.group(0), re.S)
    rows = []
    for chunk in re.findall(r"<tr>(.*?)</tr>", body.group(1), re.S):
        head = re.search(r'<th scope="row">(.*?)</th>', chunk, re.S)
        tds = re.findall(r"<td[^>]*>(.*?)</td>", chunk, re.S)
        if head is None or len(tds) < 3:
            raise AssertionError(f"строка таблицы режимов без названия или колонок: {chunk[:80]}")
        cells = [cell_key(c) for c in strip_tags(tds[0]).split(";") if c.strip()]
        rows.append({"label": strip_tags(head.group(1)).lower(), "cells": cells,
                     "n": strip_tags(tds[-1])})
    return rows


def section(html, sec_id):
    m = re.search(r'<section class="guide__sec" id="%s">(.*?)</section>' % re.escape(sec_id),
                  html, re.S)
    if m is None:
        raise AssertionError(f"в руководстве нет раздела #{sec_id}")
    return m.group(1)


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

    def test_position_is_first_step(self):
        """Первая строка панели — позиция; руководство обязано начинать с неё.

        Мутация: вернуть старый порядок «сначала ячейка, потом композит» — читатель
        снова соберёт решение сам из двух чисел, чего аудит 02.09.2026 и не велел.
        """
        quick = section(self.html, "quick")
        first = re.search(r"<li>(.*?)</li>", quick, re.S)
        self.assertIsNotNone(first, "в разделе «За 30 секунд» нет шагов")
        self.assertIn("позици", strip_tags(first.group(1)).lower(),
                      "первый шаг руководства — не строка позиции")
        self.assertIn('id="position"', self.html, "нет раздела о позиции")


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

    def test_panel_howto_mentions_position(self):
        """Блок «Как читать» на панели обязан называть позицию и ворота с гистерезисом."""
        block = re.search(r'<section class="section howto".*?</section>', self.html, re.S)
        self.assertIsNotNone(block, "на панели нет блока «Как читать»")
        text = strip_tags(block.group(0)).lower()
        self.assertIn("позиция", text)
        self.assertIn("гистерезис", text)
        self.assertIn("пятниц", text)


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

    def test_regimes_match_constants(self):
        """Три режима руководства — те же, что REGIMES: подписи, состав, n.

        n режима в руководстве — сумма n его ячеек по CELL_STATS (замороженные пары
        исследования). Живая статистика режима на панели считается заново по
        закрытым месяцам и признакам с гистерезисом, и её n другое — руководство
        обязано это оговаривать, а не выдавать одно за другое.
        """
        regimes = getattr(self.K, "REGIMES", None)
        if not regimes:
            self.skipTest("в constants ещё нет REGIMES (правка IMPL-1)")
        rows = regime_rows(self.html)
        self.assertEqual([r["label"] for r in rows], [r["label"] for r in regimes],
                         "подписи режимов или их порядок разошлись с REGIMES")
        for row, reg in zip(rows, regimes):
            with self.subTest(regime=reg["id"]):
                self.assertEqual(sorted(row["cells"]), sorted(tuple(c) for c in reg["cells"]),
                                 "состав ячеек режима разошёлся с REGIMES")
                n = sum(self.K.CELL_STATS[tuple(c)]["n"] for c in reg["cells"])
                self.assertEqual(row["n"], str(n), "n режима не равно сумме n его ячеек")
        covered = [c for r in regimes for c in r["cells"]]
        self.assertEqual(sorted(map(tuple, covered)), sorted(self.K.CELL_STATS),
                         "режимы не покрывают восемь ячеек ровно по разу")

    def test_decision_thresholds_in_guide(self):
        """Пороги слоя решения названы в руководстве теми же числами, что в DECISION.

        Мутация: поменять comp_threshold на 0,3 или vol_off_quantile на 0,5 и не
        тронуть текст — руководство продолжит рассказывать про модель, которой нет.
        """
        dec = getattr(self.K, "DECISION", None)
        if not dec:
            self.skipTest("в constants ещё нет DECISION (правка IMPL-1)")
        text = strip_tags(self.html)
        thr = f"{dec['comp_threshold']:.1f}".replace(".", ",")
        self.assertIn(f"±{thr}", text, "порог решения по наклону не назван")
        q = round(dec["vol_off_quantile"] * 100)
        self.assertRegex(text, rf"{q}-(й|го) перцентил", "порог выключения волатильности не назван")
        bond_off = f"{abs(dec['bond_off']) * 100:.0f}"
        self.assertIn(f"−{bond_off}%", text, "порог снятия облигационного флага не назван")
        band = f"{dec['trend_band'] * 100:.0f}"
        self.assertIn(f"±{band}%", text, "полоса гистерезиса тренда не названа")
        self.assertIn("пятниц", text.lower(), "день решения по наклону не назван")

    def test_health_review_months_in_guide(self):
        """Порог ревалидации (×12) — словами в разделе о здоровье модели."""
        words = {6: "шесть", 12: "двенадцать"}
        word = words.get(self.K.HEALTH_REVIEW_MONTHS)
        self.assertIsNotNone(word, f"числительное для {self.K.HEALTH_REVIEW_MONTHS} не описано — допишите")
        core = strip_tags(section(self.html, "core")).lower()
        self.assertIn(word, core, "порог ревалидации в руководстве не совпадает с HEALTH_REVIEW_MONTHS")
        self.assertIn("не видно", core, "руководство обязано отличать «связи не видно» от «сломана»")

    def test_monitor_tiles_counted_correctly(self):
        """Сколько тайлов собирает конвейер, столько же обещано словами и описано в списке.

        Руководство говорило «Пятнадцать тайлов», а панель показывала шестнадцать
        и описывала шестнадцать: счёт словами отстал на один тайл и никем не
        проверялся — числительное прописью не совпадает ни с какой константой, так
        что разъехаться оно может только молча. Считаем от кода — по списку
        BUILDERS, а не по вызовам `_tile("id"`: функция снятого тайла (cpi_weekly)
        остаётся в исходнике, но не собирается.
        """
        source = (ROOT / "pipeline" / "compute" / "monitors.py").read_text(encoding="utf-8")
        block = re.search(r"\nBUILDERS\s*=\s*\[(.*?)\n\]", source, re.S)
        self.assertIsNotNone(block, "в monitors.py не нашёлся список BUILDERS — сменился сборщик?")
        built = re.findall(r'\(\s*"([a-z_0-9]+)"\s*,\s*_t_', block.group(1))
        self.assertGreater(len(built), 5, "тайлы в конвейере не нашлись — сменился вызов?")
        self.assertEqual(len(set(built)), len(built), "дубликаты в BUILDERS")

        sec = section(self.html, "monitors")
        described = re.findall(r"<dt>(.*?)</dt>", sec, re.S)
        self.assertEqual(len(described), len(built),
                         f"описано тайлов {len(described)}, конвейер собирает {len(built)}")

        words = {12: "Двенадцать", 13: "Тринадцать", 14: "Четырнадцать", 15: "Пятнадцать",
                 16: "Шестнадцать", 17: "Семнадцать", 18: "Восемнадцать",
                 19: "Девятнадцать", 20: "Двадцать"}
        word = words.get(len(built))
        self.assertIsNotNone(word, f"числительное для {len(built)} не описано в тесте — допишите")
        self.assertIn(f"{word} тайлов", sec,
                      f"вступление раздела обещает не {len(built)} тайлов")

        # Заголовки тайлов из конвейера — те же, что <dt> руководства: снятый тайл
        # не должен остаться описанным, новый — не должен остаться безымянным.
        titles = dict(re.findall(r'^\s*"([a-z_0-9]+)":\s*"([^"]+)",', source, re.M))
        dts = [strip_tags(d) for d in described]
        for tid in built:
            with self.subTest(tile=tid):
                self.assertIn(titles.get(tid, tid), dts, f"тайл {tid} не описан в руководстве под своим заголовком")
        for tid, title in titles.items():
            if tid not in built and tid in self.K.MONITOR_TIERS:
                self.fail(f"тир для {tid} есть, тайла нет")
            if tid not in built:
                with self.subTest(removed=tid):
                    self.assertNotIn(title, dts, f"снятый тайл {tid} всё ещё описан как действующий")

    def test_core_windows_match(self):
        """Окно z и обрезка описаны словами — они не должны разъехаться с кодом."""
        self.assertIn(str(self.K.Z_WINDOW_MONTHS), self.html)
        self.assertIn(str(self.K.Z_MIN_MONTHS), self.html)

    def test_all_second_layer_signals_documented(self):
        """Мутация: добавить сигнал в constants и забыть про руководство — и наоборот,
        снять сигнал (rb_gap, futoi_z120 аудитом 02.09.2026) и оставить его строку."""
        labels = {
            "mom63": "Трендследование",
            "dd252": "Покупка просадки",
            "switch_spread": "Спред дивдоходность",
            "rb_gap": "Рублёвая бочка к тренду",
            "futoi_z120": "Контр-позиционирование",
            "dy_trail": "Дивидендная доходность",
            "rgbi_mom21": "Моментум RGBI",
        }
        sec = section(self.html, "second")
        ids = {sig["id"] for sig in self.K.SECOND_LAYER}
        for sig in self.K.SECOND_LAYER:
            with self.subTest(signal=sig["id"]):
                needle = labels.get(sig["id"])
                self.assertIsNotNone(needle, f"сигнал {sig['id']} не описан в тесте — допишите")
                self.assertIn(needle, sec, f"сигнал {sig['id']} не описан в руководстве")
        for sid, needle in labels.items():
            if sid in ids:
                continue
            with self.subTest(removed=sid):
                self.assertNotIn(needle, sec, f"снятый сигнал {sid} всё ещё стоит в таблице второго ряда")
        words = {4: "Четыре", 5: "Пять", 6: "Шесть", 7: "Семь", 8: "Восемь"}
        word = words.get(len(self.K.SECOND_LAYER))
        self.assertIsNotNone(word, f"числительное для {len(self.K.SECOND_LAYER)} сигналов не описано — допишите")
        self.assertIn(f"{word} показател", sec, "вступление второго ряда обещает другое число сигналов")

    def test_second_layer_conditions_match(self):
        """Условие включения каждого сигнала в таблице совпадает с полем when.

        Мутация 02.09.2026: у dd252 условие сменилось с «облигации спокойны» на
        «медвежий тренд»; текст, оставшийся прежним, советовал бы покупать просадку
        не там, где это проверялось.
        """
        words = {("trend", 0): "медвеж", ("trend", 1): "быч", ("vol", 1): "стресс",
                 ("bond", 0): "облигации спокойны", ("rate_phase", -1): "смягчени",
                 ("era", "post22"): "2022"}
        labels = {"mom63": "Трендследование", "dd252": "Покупка просадки",
                  "switch_spread": "Спред дивдоходность", "dy_trail": "Дивидендная доходность",
                  "rgbi_mom21": "Моментум RGBI"}
        sec = section(self.html, "second")
        for sig in self.K.SECOND_LAYER:
            needle = labels.get(sig["id"])
            if needle is None:
                continue
            row = re.search(r'<th scope="row">%s.*?</tr>' % re.escape(needle), sec, re.S)
            with self.subTest(signal=sig["id"]):
                self.assertIsNotNone(row, f"строки сигнала {sig['id']} нет в таблице")
                cond = strip_tags(re.findall(r"<td[^>]*>(.*?)</td>", row.group(0), re.S)[0]).lower()
                for k, v in sig["when"].items():
                    self.assertIn(words[(k, v)], cond,
                                  f"условие «{cond}» не говорит про {k}={v}")


if __name__ == "__main__":
    unittest.main()
