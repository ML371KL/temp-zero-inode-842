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

import json
import re
import unittest
from datetime import date, timedelta
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


class TestTelegramFallback(unittest.TestCase):
    """Зеркало включается вместо недоступного сайта — и не подменяет величину.

    С прод-машины minfin.gov.ru отвечает 503 (docs/LATENCY.md §3.3), поэтому
    `press_items()` там возвращает пустой список — ровно это и подменяется.
    """

    def setUp(self):
        self.minfin = need(self, "pipeline.fetch.minfin", "budget", "fnb", "ngd",
                           "_candidates")
        self.tg = need(self, "pipeline.fetch.tg", "find")
        self.page = fixture_text("tg_minfin.html")

    def offline(self):
        """Сайт Минфина недоступен, лента телеграма заморожена."""
        return mock.patch.object(self.minfin, "press_items", return_value=[]), \
            mock.patch.object(self.tg, "get_text", return_value=self.page)

    def test_ngd_берётся_из_зеркала_когда_сайт_молчит(self):
        p1, p2 = self.offline()
        with p1, p2:
            sid, points, meta = self.minfin.ngd()
        self.assertEqual(sid, "ngd")
        self.assertEqual(points, {"2026-08-31": 136.17})
        self.assertTrue(meta["mirror"])
        self.assertEqual(meta["source"], "minfin_tg")
        self.assertIn("t.me/minfin/9004", meta["url"])

    def test_дефицит_считается_из_доходов_и_расходов(self):
        # В телеграм-версии релиза слова «дефицит» нет вовсе: есть доходы 22 112 и
        # расходы 28 567. Мутация «взять первое число с „млрд“» дала бы +22112.
        p1, p2 = self.offline()
        with p1, p2:
            _sid, points, meta = self.minfin.budget()
        self.assertEqual(points, {"2026-07-31": -6455.0})
        self.assertIn("посчитано как доходы", meta["note"])

    def test_фнб_из_зеркала_НЕ_берётся(self):
        # В канале печатают ОБЩИЙ объём фонда (12 720 845,3 млн руб), а ряд хранит
        # ликвидную часть (3 692,8 млрд). Ошибиться тут — завысить ряд в 3,4 раза,
        # и заметить это по графику невозможно.
        p1, p2 = self.offline()
        with p1, p2:
            _sid, points, meta = self.minfin.fnb()
        self.assertEqual(points, {})
        self.assertEqual(meta["status"], "error")

    def test_сайт_имеет_приоритет_над_зеркалом(self):
        text = fixture_text("minfin_budget.txt")
        with mock.patch.object(self.minfin, "press_items",
                               return_value=[("https://minfin.gov.ru/x", text.splitlines()[0])]), \
             mock.patch.object(self.minfin, "_press_article", return_value=(text, "2026-08-11")), \
             mock.patch.object(self.tg, "messages") as never:
            _sid, points, meta = self.minfin.budget()
        never.assert_not_called()
        self.assertEqual(points, {"2026-07-31": -6455.0})
        self.assertFalse(meta["mirror"])

    def test_отказ_зеркала_не_роняет_фетчер(self):
        boom = self.tg.FetchError("t.me недоступен")
        with mock.patch.object(self.minfin, "press_items", return_value=[]), \
             mock.patch.object(self.tg, "get_text", side_effect=boom):
            _sid, points, meta = self.minfin.ngd()
        self.assertEqual(points, {})
        self.assertEqual(meta["status"], "error")
        self.assertIn("telegram", meta["note"])


class TestTelegramSearch(unittest.TestCase):
    """Бюджет ищется в канале поиском, а не только лентой.

    Оплачено 09.11.2026: лента в 4 страницы кончалась 19.10, сентябрьский релиз
    (09.10) уже ушёл за горизонт, октябрьский ещё не вышел. Фетчер не находил
    ничего, писал error, и владелец получил «источник minfin не отвечает» о
    канале, который отвечал исправно. Сайт в тестах недоступен, как и с VPS.
    """

    EMPTY_FEED = "<html><body>лента без релизов о бюджете</body></html>"

    def setUp(self):
        self.minfin = need(self, "pipeline.fetch.minfin", "budget", "ngd",
                           "BUDGET_TG_QUERY")
        self.tg = need(self, "pipeline.fetch.tg", "find")
        self.page = fixture_text("tg_minfin.html")
        self.urls = []

    def serve(self, feed, search):
        def fake(url, **_kw):
            self.urls.append(url)
            if "?q=" in url:
                return search(url) if callable(search) else search
            return feed(url) if callable(feed) else feed
        return mock.patch.object(self.minfin, "press_items", return_value=[]), \
            mock.patch.object(self.tg, "get_text", side_effect=fake)

    def test_прошлый_релиз_находится_поиском_когда_лента_его_потеряла(self):
        # мутация: убрать поиск из _candidates -> points == {} и status error,
        # то есть ровно ложная тревога 09.11.2026
        p1, p2 = self.serve(self.EMPTY_FEED, self.page)
        with p1, p2:
            _sid, points, meta = self.minfin.budget()
        self.assertEqual(meta["status"], "ok")
        self.assertEqual(points, {"2026-07-31": -6455.0})
        self.assertTrue(meta["mirror"])
        searched = [u for u in self.urls if "?q=" in u]
        self.assertTrue(searched, "поиск по каналу не вызывался")
        self.assertIn("q=", searched[0])

    def test_поиск_ищет_по_заголовку_релиза(self):
        from urllib.parse import unquote
        p1, p2 = self.serve(self.EMPTY_FEED, self.page)
        with p1, p2:
            self.minfin.budget()
        query = unquote([u for u in self.urls if "?q=" in u][0].split("q=", 1)[1])
        self.assertIn(self.minfin.BUDGET_TG_QUERY, query.replace("+", " "))

    def test_нет_ни_в_ленте_ни_в_поиске__ошибка_с_понятной_заметкой(self):
        p1, p2 = self.serve(self.EMPTY_FEED, self.EMPTY_FEED)
        with p1, p2:
            _sid, points, meta = self.minfin.budget()
        self.assertEqual(points, {})
        self.assertEqual(meta["status"], "error")
        self.assertIn("нет сообщения", meta["note"])

    def test_отказ_поиска_не_мешает_ленте(self):
        def down(url):
            raise self.tg.FetchError("поиск t.me недоступен")
        p1, p2 = self.serve(self.page, down)
        with p1, p2:
            _sid, points, meta = self.minfin.budget()
        self.assertEqual(points, {"2026-07-31": -6455.0})
        self.assertEqual(meta["status"], "ok")

    def test_свежий_релиз_из_ленты_важнее_старого_из_поиска(self):
        # Лента и поиск пересекаются; первым в разбор должен идти самый свежий
        # релиз. Подменяем дату в выдаче поиска на прошлый год: если бы порядок
        # был «сначала поиск», в ряд лёг бы он.
        old = re.sub(r'datetime="2026-', 'datetime="2025-', self.page)
        old = old.replace("2026 года", "2025 года").replace("2026 г", "2025 г")
        old = re.sub(r'data-post="minfin/(\d+)"', lambda m: 'data-post="minfin/1%s"'
                     % m.group(1)[1:], old)
        p1, p2 = self.serve(self.page, old)
        with p1, p2:
            _sid, points, _meta = self.minfin.budget()
        self.assertEqual(list(points), ["2026-07-31"])

    def test_нгд_поиском_не_пользуется(self):
        # Поиск включён только там, где задан запрос; остальные ряды — как были.
        p1, p2 = self.serve(self.page, self.EMPTY_FEED)
        with p1, p2:
            self.minfin.ngd()
        self.assertFalse([u for u in self.urls if "?q=" in u])


class TestMoexFeedDepth(unittest.TestCase):
    """Глубина обхода ленты ISS: месячный релиз в трёх страницах не живёт.

    Раньше здесь стояло `scan_pages=3` — 300 новостей. МосБиржа публикует около сотни
    новостей в СУТКИ, то есть окно поиска было трёхдневным, а релиз про частных
    инвесторов выходит раз в месяц: ряд `moex_retail` не собрался ни разу, и в журнале
    это выглядело как «релизов не нашлось» (замер 12.08.2026 — релиз лежал на 15-й
    странице). Даты в фикстуре фиксированные: отсчёт возраста идёт от ВЕРХА ленты,
    а не от «сегодня» (правило набора №1).
    """

    PER_PAGE = 50  # столько строк sitenews.json отдаёт НА САМОМ ДЕЛЕ (замер 13.08)

    def setUp(self):
        self.press = need(self, "pipeline.fetch.moex_press", "_candidates",
                          "MAX_AGE_DAYS", "MAX_PAGES")
        self.http = need(self, "pipeline.lib.http", "get_bytes")

    def feed(self, hit_row, total=2000, days_per_page=1, top="2026-08-11"):
        """Лента как у настоящего ISS: ПЛОСКИЙ список, из которого запрос со
        start=s получает срез [s, s+50) — ровно 50 строк, что бы ни думал клиент.

        До 18.08.2026 фиктивный сервер строился из клиентского же PAGE_SIZE=100 и
        по построению не мог поймать главный дефект: обход шагал по 100 при
        страницах в 50, и строки 50–99, 150–199, … не читались НИКОГДА. Релиз в
        такой слепой зоне (в проде — позиция 75) не находился при зелёном тесте.
        """
        top_day = date.fromisoformat(top)

        def row(i):
            day = (top_day - timedelta(days=(i // self.PER_PAGE) * days_per_page)).isoformat()
            if i == hit_row:
                return [777, "", "Частные инвесторы вложили в июле 1,5 трлн рублей",
                        day + " 11:30:00", day + " 11:30:00"]
            return [900000 + i, "", "Об установлении риск-параметров",
                    day + " 18:30:00", day + " 18:30:00"]

        def responder(url, **_kw):
            start = int(re.search(r"start=(\d+)", url).group(1))
            rows = [row(i) for i in range(start, min(start + self.PER_PAGE, total))]
            body = {"sitenews": {"columns": ["id", "tag", "title", "published_at",
                                             "modified_at"], "data": rows}}
            return json.dumps(body, ensure_ascii=False).encode("utf-8")

        return mock.patch.object(self.http, "get_bytes", side_effect=responder)

    def test_релиз_в_бывшей_слепой_зоне_находится(self):
        # Позиция 75 — строки 50–99, которые обход с шагом 100 не читал никогда.
        # Ровно там лежал июльский релиз в проде, пока тайл стоял в error.
        with self.feed(hit_row=75):
            ids, errors = self.press._candidates()
        self.assertEqual([i[0] for i in ids], [777], f"слепая зона вернулась: {errors}")

    def test_релиз_месячной_давности_находится(self):
        # 15 настоящих страниц вглубь (позиция 750) — замер 12.08.2026.
        with self.feed(hit_row=750):
            ids, errors = self.press._candidates()
        self.assertEqual([i[0] for i in ids], [777], f"не дошли до релиза: {errors}")

    def test_обход_прекращается_по_возрасту_ленты(self):
        # Шаг 5 суток на страницу: окно в 45 суток кончается на десятой странице,
        # и лежащий за ним релиз уже не ищется — это граница, а не поломка.
        with self.feed(hit_row=750, days_per_page=5) as fake:
            ids, _errors = self.press._candidates()
        self.assertEqual(ids, [])
        self.assertLess(fake.call_count, self.press.MAX_PAGES,
                        "обход должен останавливаться по датам, а не упираться в потолок")

    def test_retail_вызывается_целиком(self):
        """Регрессия 12.08.2026: сигнатуру `_candidates` расширили ПЕРВЫМ параметром.

        `retail()` зовёт её позиционно (`_candidates(scan_pages)`), и число 20 встало
        на место регулярки: прогон падал с «'int' object has no attribute 'search'»,
        ряд простоял `error` до следующего месячного такта, а тесты этого не увидели —
        они дёргали `_candidates()` без аргументов. Теперь проверяется ПУТЬ ЦЕЛИКОМ.
        """
        body = ("Количество частных инвесторов на Московской бирже в июле 2026 года "
                "составило 41,9 млн человек (+62 тыс. за месяц). Сделки в июле "
                "заключали 3,0 млн человек. В народный портфель вошли акции "
                "Сбербанка (31,8% и 7,3% соответственно), ЛУКОЙЛа (11,6%).")

        def feed(url, **_kw):
            if "/sitenews/" in url:
                return {"content": {"columns": ["id", "body"], "data": [[777, body]]}}
            start = int(re.search(r"start=(\d+)", url).group(1))
            rows = [] if start else [[777, "", "Частные инвесторы в июле нарастили вложения",
                                      "2026-08-05 11:30:00", "2026-08-05 11:30:00"]]
            return {"sitenews": {"columns": ["id", "tag", "title", "published_at",
                                             "modified_at"], "data": rows}}

        with mock.patch.object(self.press, "get_json", side_effect=feed):
            sid, points, meta = self.press.retail()
        self.assertEqual(sid, "moex_retail")
        self.assertEqual(meta["status"], "ok", meta.get("note"))
        self.assertEqual(points, {"2026-07-31": 3.0})
        self.assertEqual(meta["payload"]["clients_total_mln"], 41.9)

    def test_новые_параметры_добавляются_в_конец(self):
        # Прямая защита от повторения: первым позиционным аргументом обязана
        # оставаться глубина обхода, а не что-то новое.
        import inspect
        names = list(inspect.signature(self.press._candidates).parameters)
        self.assertEqual(names[0], "max_pages")

    def test_потолок_страниц_держит_ленту_без_дат(self):
        def undated(url, **_kw):
            body = {"sitenews": {"columns": ["id", "tag", "title", "published_at",
                                             "modified_at"],
                                 "data": [[1, "", "новость", None, None]]}}
            return json.dumps(body).encode("utf-8")
        with mock.patch.object(self.http, "get_bytes", side_effect=undated) as fake:
            self.press._candidates()
        self.assertEqual(fake.call_count, self.press.MAX_PAGES)


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
