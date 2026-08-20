"""Нога ядра: налоговая цена Юралс. Три вопроса — откуда, то ли число, что если нет.

Ряд `urals_tax` — треть композита по весу, и мера ошибки посчитана: замороженный на
месяц ряд сдвигает композит в медиане на 0,096 и переносит его через порог алерта в
47% месяцев, выпавший совсем — на 0,266 и меняет ЗНАК в 15% (замер 12.08.2026 на 105
месяцах). Поэтому здесь проверяется не «парсер вернул словарь», а ровно те три
способа испортить эту ногу, которые уже случались или почти случились:

1. **взять не ту величину.** Ленты печатают РЫНОЧНУЮ цену Юралс, справочник НДПИ —
   НАЛОГОВУЮ, и они расходятся (январь-2026: 40,95 против ~45 в пересказах). Ряд
   калиброван на налоговой. Число из ленты не должно попадать в точки НИКОГДА;
2. **взять не ту колонку.** В строке рядом с ценой стоят курс, Кц и ставка НДПИ
   (919 руб./т). Сдвиг на колонку — и в ногу ядра уезжает ставка. Ловится
   тождеством Кц = (Ц − 15) × Р / 261;
3. **молча остаться без месяца.** Если справочник не открылся, должен подхватиться
   inputs/urals.yml, а если и он пуст — статус error, а не тихий «ok» с пустым рядом.

В сеть не ходим: подменяется `consultant.get_text`.
"""

import unittest
from unittest import mock

from tests import fixture_text, need


class ConsultantCase(unittest.TestCase):
    def setUp(self):
        self.cons = need(self, "pipeline.fetch.consultant", "sections", "parse_table",
                         "ndpi_prices", "month_end")
        self.card = fixture_text("cons_ndpi_card.html")
        self.t13 = fixture_text("cons_ndpi_13.html")
        self.t22 = fixture_text("cons_ndpi_22.html")

    def serve(self, card=None, t13=None, t22=None):
        card = self.card if card is None else card
        t13 = self.t13 if t13 is None else t13
        t22 = self.t22 if t22 is None else t22
        seen = []

        def fake(url, **_kw):
            seen.append(url)
            if url.rstrip("/").endswith("cons_doc_LAW_50642"):
                return card
            return t13 if len(seen) == 2 else t22
        return mock.patch.object(self.cons, "get_text", side_effect=fake), seen


class TestSections(ConsultantCase):
    def test_берутся_только_разделы_с_живой_таблицей(self):
        got = self.cons.sections(self.card)
        titles = [t for _u, t in got]
        self.assertEqual(len(got), 2, f"ожидались 1.3 и 2.2, получено: {titles}")
        self.assertTrue(titles[0].startswith("1.3."))
        self.assertTrue(titles[1].startswith("2.2."))

    def test_архивы_и_оглавления_не_берутся(self):
        titles = " | ".join(t for _u, t in self.cons.sections(self.card))
        self.assertNotIn("Архив", titles)
        # «1.» и «2.» — заголовки того же вида, но таблицы с месяцами в них нет:
        # взять их значит получить пустой разбор и решить, что источник упал.
        for bad in ("1. Данные", "2. Данные"):
            self.assertNotIn("| " + bad, " | " + titles)

    def test_адрес_раздела_не_захардкожен(self):
        # Хэш меняется при каждом обновлении документа; раздел ищется по заголовку.
        urls = [u for u, _t in self.cons.sections(self.card)]
        for url in urls:
            self.assertIn("/document/cons_doc_LAW_50642/", url)
        self.assertEqual(len(set(urls)), 2)


class TestTable(ConsultantCase):
    def test_месяцы_и_цены_разбираются(self):
        points, problems = self.cons.parse_table(self.t13)
        self.assertEqual(problems, [])
        self.assertEqual(points["2026-01-31"], 40.95)
        self.assertEqual(points["2026-07-31"], 59.02)
        self.assertEqual(len(points), 7)

    def test_свежий_месяц_без_курса_и_кц_принимается(self):
        # У июля Минэк уже дал цену, а ФНС ещё не досчитала Кц — колонки пустые.
        # Требовать тождество там значит терять самый нужный, свежий месяц.
        points, _ = self.cons.parse_table(self.t13)
        self.assertIn("2026-07-31", points)

    def test_сдвиг_колонок_ловится_тождеством_кц(self):
        # Мутация «взять курс вместо цены»: 73,5447 лежит внутри санитарного коридора
        # $5…$250 и на глаз выглядит правдоподобной ценой барреля — поймать её может
        # только тождество Кц.
        shifted = self.t13.replace('<p class="align_left">63,52</p>',
                                   '<p class="align_left">73,5447</p>')
        points, problems = self.cons.parse_table(shifted)
        self.assertNotIn("2026-06-30", points, "строка со сломанным Кц принята")
        self.assertTrue(any("Кц не сходится" in p for p in problems), problems)

    def test_ставка_ндпи_вместо_цены_ловится_коридором(self):
        # Вторая линия обороны: 919 руб./т — это соседняя колонка, и она вне
        # коридора цен барреля. Тождество тут даже не понадобится.
        shifted = self.t13.replace('<p class="align_left">63,52</p>',
                                   '<p class="align_left">919</p>')
        points, problems = self.cons.parse_table(shifted)
        self.assertNotIn("2026-06-30", points)
        self.assertTrue(any("вне коридора" in p for p in problems), problems)

    def test_у_конденсата_тождество_не_применяется(self):
        # Раздел 2.2 печатает ТУ ЖЕ цену, но свой Кц (16,2032 против 13,6720 за июнь):
        # нефтяная формула его забракует, и перекрёстная сверка исчезнет.
        strict, _ = self.cons.parse_table(self.t22, check_kc=True)
        loose, problems = self.cons.parse_table(self.t22, check_kc=False)
        self.assertEqual(problems, [])
        self.assertGreater(len(loose), len(strict))
        self.assertEqual(loose["2026-06-30"], 63.52)

    def test_шапка_и_мусор_не_становятся_точками(self):
        self.assertIsNone(self.cons.month_end("Период"))
        self.assertIsNone(self.cons.month_end("Январь - Март 2015"))
        self.assertEqual(self.cons.month_end("Июль 2026"), "2026-07-31")
        self.assertEqual(self.cons.month_end("Декабрь 2025"), "2025-12-31")


class TestNdpiPrices(ConsultantCase):
    def test_два_раздела_подтверждают_друг_друга(self):
        patch, _seen = self.serve()
        with patch:
            points, meta = self.cons.ndpi_prices()
        self.assertEqual(len(points), 7)
        self.assertEqual(meta["conflicts"], [])
        self.assertEqual(len(meta["sections"]), 2)

    def test_расхождение_разделов_не_усредняется(self):
        broken = self.t22.replace("63,52", "70,00")
        patch, _seen = self.serve(t22=broken)
        with patch:
            points, meta = self.cons.ndpi_prices()
        self.assertEqual(points["2026-06-30"], 63.52, "значение первого раздела перетёрто")
        self.assertTrue(any("2026-06-30" in c for c in meta["conflicts"]))

    def test_пустая_карточка_это_отказ_источника(self):
        patch, _seen = self.serve(card="<html>ничего</html>")
        with patch, self.assertRaises(self.cons.FetchError):
            self.cons.ndpi_prices()


class TestUralsFetcher(unittest.TestCase):
    """minfin.urals(): что попадает в ряд, а что только в meta."""

    def setUp(self):
        self.minfin = need(self, "pipeline.fetch.minfin", "urals", "market_prices",
                           "URALS_SELF_CHECK", "_stored_urals")
        self.cons = need(self, "pipeline.fetch.consultant", "ndpi_prices")
        # По умолчанию считаем ряд пустым: иначе ранний выход «месяц уже собран»
        # сделает исход теста зависимым от того, что лежит в сторе на машине.
        empty = mock.patch.object(self.minfin, "_stored_urals", return_value=None)
        empty.start()
        self.addCleanup(empty.stop)

    def test_рыночная_цена_из_ленты_в_ряд_не_попадает(self):
        # Ключевая проверка всей затеи: ряд калиброван на НАЛОГОВОЙ цене.
        with mock.patch.object(self.minfin.consultant, "ndpi_prices",
                               return_value=({"2026-01-31": 40.95}, {"url": "u"})), \
             mock.patch.object(self.minfin, "market_prices",
                               return_value=({"2026-01-31": (45.0, "1prime.ru")}, [])):
            _sid, points, meta = self.minfin.urals()
        self.assertEqual(points, {"2026-01-31": 40.95})
        self.assertNotIn(45.0, points.values())
        self.assertTrue(any("рыночной 45.00" in c for c in meta["conflicts"]),
                        f"расхождение не показано: {meta['conflicts']}")
        self.assertEqual(meta["market"], {"2026-01-31": 45.0})

    def test_репер_января_совпадает_с_налоговой_ценой(self):
        # Со старым репером 45,0 самопроверка кричала бы на ВЕРНОМ числе.
        self.assertEqual(self.minfin.URALS_SELF_CHECK["2026-01"], 40.95)
        with mock.patch.object(self.minfin.consultant, "ndpi_prices",
                               return_value=({"2026-01-31": 40.95}, {"url": "u"})), \
             mock.patch.object(self.minfin, "market_prices", return_value=({}, [])):
            _sid, _points, meta = self.minfin.urals()
        self.assertEqual(meta["selfcheck"], "ok")

    def test_собранный_месяц_не_опрашивается_повторно(self):
        # Окно 1–12 числа при трёх тактах в сутки — до 36 попыток, а нужна одна.
        # Справочник чужой и некоммерческий: ходить к нему за уже полученным
        # числом невежливо и незачем.
        with mock.patch.object(self.minfin, "_stored_urals", return_value="2026-07-31"), \
             mock.patch.object(self.minfin, "_prev_month_end", return_value="2026-07-31"), \
             mock.patch.object(self.minfin.consultant, "ndpi_prices") as never, \
             mock.patch.object(self.minfin, "market_prices") as never_market:
            _sid, points, meta = self.minfin.urals()
        never.assert_not_called()
        never_market.assert_not_called()
        self.assertEqual(points, {})
        self.assertEqual(meta["status"], "ok")
        self.assertTrue(meta["skipped"])

    def test_ранний_выход_не_выдаёт_себя_за_живой_ответ(self):
        # Весь ряд urals_tax до 20.08.2026 пришёл из затравки исследования, а meta
        # подписывала его «consultant_ndpi» со status=ok — тайл и баннер источников
        # показывали подтверждение, которого не было ни разу. Ранняя ветка обязана
        # называть дату последнего ЖИВОГО ответа, а её отсутствие — вслух.
        with mock.patch.object(self.minfin, "_stored_urals", return_value="2026-07-31"), \
             mock.patch.object(self.minfin, "_prev_month_end", return_value="2026-07-31"), \
             mock.patch.object(self.minfin, "_stored_meta", return_value={}), \
             mock.patch.object(self.minfin.consultant, "ndpi_prices"), \
             mock.patch.object(self.minfin, "market_prices"):
            _sid, _points, meta = self.minfin.urals()
        self.assertIsNone(meta["last_live"])
        self.assertIn("ни разу", meta["note"])

    def test_дата_живого_ответа_переживает_ранний_выход(self):
        with mock.patch.object(self.minfin, "_stored_urals", return_value="2026-07-31"), \
             mock.patch.object(self.minfin, "_prev_month_end", return_value="2026-07-31"), \
             mock.patch.object(self.minfin, "_stored_meta",
                               return_value={"last_live": "2026-08-20"}), \
             mock.patch.object(self.minfin.consultant, "ndpi_prices"), \
             mock.patch.object(self.minfin, "market_prices"):
            _sid, _points, meta = self.minfin.urals()
        self.assertEqual(meta["last_live"], "2026-08-20")
        self.assertIn("2026-08-20", meta["note"])
        self.assertNotIn("ни разу", meta["note"])

    def test_живой_ответ_проставляет_дату(self):
        with mock.patch.object(self.minfin.consultant, "ndpi_prices",
                               return_value=({"2026-01-31": 40.95}, {"url": "u"})), \
             mock.patch.object(self.minfin, "market_prices", return_value=({}, [])):
            _sid, _points, meta = self.minfin.urals()
        self.assertRegex(str(meta.get("last_live")), r"^\d{4}-\d{2}-\d{2}$")

    def test_недостающий_месяц_опрашивается(self):
        with mock.patch.object(self.minfin, "_stored_urals", return_value="2026-06-30"), \
             mock.patch.object(self.minfin, "_prev_month_end", return_value="2026-07-31"), \
             mock.patch.object(self.minfin.consultant, "ndpi_prices",
                               return_value=({"2026-07-31": 59.02}, {"url": "u"})) as called, \
             mock.patch.object(self.minfin, "market_prices", return_value=({}, [])):
            _sid, points, _meta = self.minfin.urals()
        called.assert_called_once()
        self.assertEqual(points, {"2026-07-31": 59.02})

    def test_отказ_справочника_уводит_в_ручной_ввод(self):
        boom = self.cons.FetchError("таблица не открылась")
        with mock.patch.object(self.minfin.consultant, "ndpi_prices", side_effect=boom), \
             mock.patch.object(self.minfin, "market_prices", return_value=({}, [])), \
             mock.patch.object(self.minfin, "_urals_manual",
                               return_value=({"2026-08-31": 61.3}, "inputs/urals.yml")):
            _sid, points, meta = self.minfin.urals()
        self.assertEqual(points, {"2026-08-31": 61.3})
        self.assertEqual(meta["status"], "manual_needed")

    def test_без_справочника_и_без_ручного_это_error(self):
        boom = self.cons.FetchError("таблица не открылась")
        with mock.patch.object(self.minfin.consultant, "ndpi_prices", side_effect=boom), \
             mock.patch.object(self.minfin, "market_prices", return_value=({}, [])), \
             mock.patch.object(self.minfin, "_urals_manual", return_value=({}, None)):
            _sid, points, meta = self.minfin.urals()
        self.assertEqual(points, {})
        self.assertEqual(meta["status"], "error")
        self.assertIn("inputs/urals.yml", meta["note"])


class TestManualUrals(unittest.TestCase):
    def setUp(self):
        self.manual = need(self, "pipeline.fetch.manual", "urals_manual", "load_input")

    def test_месяц_разворачивается_в_конец_месяца(self):
        data = {"items": [{"month": "2026-08", "usd": 61.3, "source": "письмо ФНС"}]}
        with mock.patch.object(self.manual, "load_input",
                               return_value=(data, "inputs/urals.yml")):
            points, path = self.manual.urals_manual()
        self.assertEqual(points, {"2026-08-31": 61.3})
        self.assertTrue(path.endswith("urals.yml"))

    def test_опечатка_в_рублях_или_копейках_отбрасывается(self):
        data = {"items": [{"month": "2026-08", "usd": 5400},
                          {"month": "2026-09", "usd": 0.59},
                          {"month": "2026-10", "usd": 61.3}]}
        with mock.patch.object(self.manual, "load_input", return_value=(data, "p")):
            points, _path = self.manual.urals_manual()
        self.assertEqual(points, {"2026-10-31": 61.3})

    def test_пустой_файл_не_падает(self):
        with mock.patch.object(self.manual, "load_input", return_value=({"items": []}, "p")):
            self.assertEqual(self.manual.urals_manual()[0], {})
        with mock.patch.object(self.manual, "load_input", return_value=(None, None)):
            self.assertEqual(self.manual.urals_manual()[0], {})


if __name__ == "__main__":
    unittest.main()
