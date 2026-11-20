"""Аукционы ОФЗ с биржевой доски: что считать аукционом и как не удвоить их число.

Ряд переехал с недоступного minfin.gov.ru на доску PACT МосБиржи. Биржа даёт размещение
и — единственная из всех проверенных источников — позволяет отличить «аукцион провалился»
от «аукцион не проводился». Но она же расставляет две ловушки, и обе тихие:

1. **эхо на доске.** День ПОСЛЕ успешного размещения выпуск ещё висит строкой с нулевым
   объёмом. По объёму, числу сделок и ценам эта строка НЕОТЛИЧИМА от провалившегося
   аукциона — у обоих нули и None. Без фильтра панель показывала бы вдвое больше
   аукционов, чем было, и половину объявляла провалом. Различает только LASTTRADEDATE:
   у эха последняя сделка накануне, у настоящего провала 15.07.2026 — 2025-11-12;
2. **номинал.** Размещение считается по номиналу из СТРОКИ, а не константой 1000 ₽:
   у ОФЗ-ИН он индексируется, у амортизируемых уменьшается.

Плюс методологическая честность: биржевое число НЕ равно минфиновскому в дни с ДРПА
(20.05.2026: 160,30 против 174,44), спроса биржа не раскрывает вовсе. Оба факта обязаны
быть в meta и на тайле, а не подразумеваться.

С 20.11.2026 число дня — официальный итог Минфина из его телеграм-канала (аукционы
плюс доразмещение), доска — сверка и резерв: аудит показал занижение на 4–29 %
(11.11.2026: 88,87 против 62,70). Чужое число в ряд попасть не должно — на это
классы TestMinfinPostParsing и TestOfficialIsPrimary на настоящих постах.

В сеть не ходим: подменяются `auctions.http.get_json` (ответы доски) и канал Минфина
(`auctions.minfin_results` либо `tg.get_text` с замороженной страницей поиска).
"""

import unittest
from datetime import date
from unittest import mock

from tests import fixture_json, fixture_text, need


class AuctionsCase(unittest.TestCase):
    def setUp(self):
        self.a = need(self, "pipeline.fetch.auctions", "auctions", "day_summary",
                      "is_echo", "next_auction")
        self.days = fixture_json("iss_pact.json")

    def serve(self, announce=None, official=None):
        """Транспорт: доска отвечает из фикстуры, лента новостей — по требованию.

        Канал Минфина подменён ВСЕГДА (по умолчанию — «постов нет»): фетчер ходит
        в него на каждом прогоне, и непатченный вызов увёл бы набор в сеть.
        `official` — словарь итогов, исключение (отказ канала) или None.
        """
        def fake(url, **_kw):
            for day, block in self.days.items():
                if "date=%s" % day in url:
                    return {"history": {"columns": block["columns"], "data": block["data"]}}
            return {"history": {"columns": self.days["2026-07-22"]["columns"], "data": []}}
        patches = [mock.patch.object(self.a.http, "get_json", side_effect=fake)]
        if isinstance(official, Exception):
            patches.append(mock.patch.object(self.a, "minfin_results", side_effect=official))
        else:
            patches.append(mock.patch.object(self.a, "minfin_results",
                                             return_value=dict(official or {})))
        if announce is not None:
            patches.append(mock.patch.object(self.a, "next_auction",
                                             return_value=announce))
        return patches

    def run_range(self, start, end, announce=(None, None, None), official=None):
        patches = self.serve(announce, official)
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return self.a.auctions(start=start, end=end)


class TestDayClassification(AuctionsCase):
    def test_обычный_аукцион_считается_по_номиналу(self):
        _sid, pts, _meta = self.run_range("2026-07-01", "2026-07-01")
        # 10 364 891 бумаги × 1000 ₽ = 10,36 млрд — сходится с файлом Минфина в копейку
        self.assertEqual(sorted(pts), ["2026-07-01"])
        self.assertAlmostEqual(pts["2026-07-01"], 10.3649, places=3)

    def test_эхо_на_следующий_день_не_становится_аукционом(self):
        # Мутация «считать аукционом любую непустую строку» -> 02.07 попадает в ряд
        # нулём и помечается провалом, которого не было.
        _sid, pts, _meta = self.run_range("2026-07-01", "2026-07-02")
        self.assertEqual(sorted(pts), ["2026-07-01"], "эхо доски попало в ряд")

    def test_провал_отличается_от_эха(self):
        _sid, pts, meta = self.run_range("2026-07-15", "2026-07-15")
        self.assertEqual(pts, {"2026-07-15": 0.0})
        self.assertTrue(meta["last"]["failed"])
        self.assertEqual(meta["last"]["issue"], "SU29028RMFS6")

    def test_день_без_аукциона_не_создаёт_точку(self):
        # Нулей на плановые среды не выдумываем: «не было» и «провалился» — разные
        # состояния рынка, и склейка их одним нулём стирает смысл ряда.
        _sid, pts, _meta = self.run_range("2026-07-22", "2026-07-22")
        self.assertEqual(pts, {})

    def test_день_из_двух_выпусков_суммируется(self):
        _sid, pts, _meta = self.run_range("2026-05-20", "2026-05-20")
        self.assertAlmostEqual(pts["2026-05-20"], 160.2961, places=3)

    def test_эхо_только_в_своём_окне(self):
        rows = [{"volume": 0, "last_trade": "2026-07-01"}]
        self.assertTrue(self.a.is_echo(rows, "2026-07-02"))
        # 245 дней назад — это не эхо, а выпуск, который сегодня не разместили
        self.assertFalse(self.a.is_echo([{"volume": 0, "last_trade": "2025-11-12"}],
                                        "2026-07-15"))
        # Размещение было — эхо невозможно по определению
        self.assertFalse(self.a.is_echo([{"volume": 5, "last_trade": "2026-07-01"}],
                                        "2026-07-02"))
        # Даты нет — не выдумываем эхо, день берём
        self.assertFalse(self.a.is_echo([{"volume": 0, "last_trade": None}], "2026-07-02"))


class TestSummary(AuctionsCase):
    def test_номинал_берётся_из_строки(self):
        # ОФЗ-ИН: номинал проиндексирован. Константа 1000 занизила бы день на 40%.
        s = self.a.day_summary([{"secid": "SU52005RMFS4", "volume": 1_000_000,
                                 "face": 1400.0, "trades": 10, "yield": 3.1}])
        self.assertAlmostEqual(s["placed_bln"], 1.4, places=6)

    def test_чужие_выпуски_на_доске_игнорируются(self):
        # На PACT размещаются и корпораты: без фильтра SU* в «аукцион ОФЗ» уедет чужой.
        rows = [{"volume": 100, "last_trade": "2026-07-01"}]
        self.assertFalse(self.a.is_echo(rows, "2026-07-01"))
        block = {"history": {"columns": ["TRADEDATE", "SECID", "VOLUME", "FACEVALUE"],
                             "data": [["2026-07-01", "RU000A1080N9", 5_000_000, 1000],
                                      ["2026-07-01", "SU26251RMFS7", 1_000_000, 1000]]}}
        with mock.patch.object(self.a.http, "get_json", return_value=block):
            rows, _url = self.a._day_rows("2026-07-01")
        self.assertEqual([r["secid"] for r in rows], ["SU26251RMFS7"])


class TestMetaHonesty(AuctionsCase):
    def test_спрос_пуст_а_не_ноль(self):
        _sid, _pts, meta = self.run_range("2026-07-15", "2026-07-15")
        self.assertIsNone(meta["last"]["demand_bn"],
                          "пустой спрос обязан быть None, иначе тайл покажет ноль")

    def test_шов_методик_объявлен(self):
        # Без поста Минфина день биржевой, и это названо: и методом, и швом.
        _sid, _pts, meta = self.run_range("2026-07-01", "2026-07-01")
        self.assertEqual(meta["method"], "moex_pact")
        self.assertEqual(meta["last"]["source"], "moex_pact")
        self.assertIn("без доразмещения", meta["splice"])

    def test_пауза_считается_неделями(self):
        _sid, _pts, meta = self.run_range("2026-07-01", "2026-07-22")
        self.assertEqual(meta["last"]["date"], "2026-07-15")
        self.assertIsInstance(meta["weeks_since"], int)

    def test_пустое_окно_не_объявляет_аукционом_старую_точку(self):
        # В затравке дни ОТМЕНЁННЫХ аукционов записаны нулями. Если взять последнюю
        # точку ряда за «последний аукцион», панель во время паузы напишет «Аукцион
        # 05.08 не состоялся» про день, когда аукциона не назначали. Помним свой
        # прошлый разбор, а к ряду не обращаемся.
        prev = {"date": "2026-07-15", "issue": "SU29028RMFS6", "placed_bn": 0.0,
                "failed": True, "demand_bn": None}
        with mock.patch.object(self.a.store, "load_series",
                               return_value={"meta": {"last": prev}}), \
             mock.patch.object(self.a.store, "last_date", return_value="2026-08-05"):
            _sid, pts, meta = self.run_range("2026-07-22", "2026-07-22")
        self.assertEqual(pts, {})
        self.assertEqual(meta["last"]["date"], "2026-07-15")
        self.assertEqual(meta["asof"], "2026-07-15")

    def test_анонс_попадает_в_meta(self):
        _sid, _pts, meta = self.run_range(
            "2026-07-22", "2026-07-22",
            announce=("2026-08-19", "О проведении 19 августа 2026 года аукциона по "
                                    "размещению ОФЗ выпусков № 26252RMFS", "https://x"))
        self.assertEqual(meta["next_auction"], "2026-08-19")
        self.assertIn("19 августа", meta["next_auction_title"])


class OfficialCase(AuctionsCase):
    """Канал Минфина из фикстуры: настоящие посты об итогах аукционов.

    tests/fixtures/tg_minfin_auctions.html — живая страница поиска по каналу
    (20.11.2026), прорежённая до постов 01.07, 02.09, 07.10, 14.10, 21.10, 11.11,
    18.11 плюс перепечатка ТАСС и пост про закупки. Блоки сообщений — как есть.
    """

    def setUp(self):
        super().setUp()
        self.tg = need(self, "pipeline.fetch.tg", "parse_page", "find")
        self.page = fixture_text("tg_minfin_auctions.html")
        self.posts = {m["published"]: m for m in self.tg.parse_page(self.page, "minfin")}

    def results(self, page=None):
        with mock.patch.object(self.tg, "get_text", return_value=page or self.page):
            return self.a.minfin_results()

    def parsed(self, day):
        return self.a.parse_result(self.posts[day]["text"], day)


class TestMinfinPostParsing(OfficialCase):
    def test_итог_дня_сумма_аукционов_и_доразмещения(self):
        # Руками по тексту поста 18.11.2026: 27,918 + 42,947 = 70,865 на аукционах,
        # ДРПА 2 877,000 млн = 2,877; всего 73,742 — как в заголовке.
        # мутация: брать только аукционы -> 70,865 (то самое занижение на ДРПА).
        got = self.parsed("2026-11-18")
        self.assertEqual(got["total_bn"], 73.742)
        self.assertEqual(got["auction_bn"], 70.865)
        self.assertEqual(got["drpa_bn"], 2.877)
        self.assertEqual(got["issues"], ["26233RMFS", "26253RMFS"])

    def test_округлённый_заголовок_уступает_сумме_частей(self):
        # 11.11: заголовок «около 88,87», части 34,060 + 31,630 + 23,183584 = 88,874.
        # 14.10: заголовок «около 140», части 41,771 + 73,479 + 24,743391 = 139,993.
        # мутация: брать число заголовка -> 140,0 вместо 139,993.
        self.assertEqual(self.parsed("2026-11-11")["total_bn"], 88.874)
        self.assertEqual(self.parsed("2026-11-11")["drpa_bn"], 23.184)
        self.assertEqual(self.parsed("2026-10-14")["total_bn"], 139.993)

    def test_триллион_и_один_выпуск_без_доразмещения(self):
        # «на сумму 1 трлн рублей» — другая единица и нет слова «общую».
        # мутация: понимать только «млрд» -> день 02.09 остаётся биржевым (987).
        self.assertEqual(self.parsed("2026-09-02")["total_bn"], 1000.0)
        got = self.parsed("2026-07-01")
        self.assertEqual((got["total_bn"], got["drpa_bn"]), (10.365, 0.0))

    def test_доразмещение_больше_самого_аукциона(self):
        # 07.10: аукцион 39,129, ДРПА 41,867 — итог 80,996 при 38,07 на доске.
        got = self.parsed("2026-10-07")
        self.assertEqual((got["auction_bn"], got["drpa_bn"], got["total_bn"]),
                         (39.129, 41.867, 80.996))

    def test_чужие_посты_итогом_не_считаются(self):
        # Пост про закупки («подвел итоги… контрактной системы») и перепечатка ТАСС.
        for day in ("2026-04-30", "2026-11-09"):
            self.assertIsNone(self.parsed(day), day)
        # Свои формулировки, чужая тема: сумма «по номиналу» про ОФЗ обязательна.
        for text in ("Правительство списало две трети задолженности по бюджетным "
                     "кредитам ещё 23 регионам на общую сумму 59,6 млрд рублей",
                     "Минфин России на аукционе разместил средства ФНБ на общую сумму "
                     "12,5 млрд рублей",
                     "О проведении 25 ноября 2026 года аукциона по размещению ОФЗ "
                     "выпусков № 26253RMFS на общую сумму 50 млрд рублей по номиналу"):
            self.assertIsNone(self.a.parse_result(text, "2026-11-18"), text)

    def test_перепечатка_с_теми_же_словами_отсекается(self):
        # «📰 Интерфакс: Минфин России на аукционе разместил…» — пересказ с чужим
        # округлением. мутация: не отсекать перепечатки -> в ряд идёт число дайджеста.
        # В разметке заголовок разбит тегами подсветки поиска, поэтому маркер
        # вставляется перед началом заголовка именно поста 18.11 (он последний).
        at = self.page.rindex("<b>Минфин России </b>", 0, self.page.index("73,742"))
        page = self.page[:at] + "📰 Интерфакс: " + self.page[at:]
        self.assertNotIn("2026-11-18", self.results(page))
        self.assertIn("2026-11-18", self.results())

    def test_заголовок_без_состава_не_принимается(self):
        # Вёрстка поста сменилась, частей не видно — одному заголовку не верим.
        text = ("Минфин России на аукционе разместил два выпуска ОФЗ на общую сумму "
                "73,742 млрд рублей по номиналу\nПодробности по ссылке")
        self.assertIsNone(self.a.parse_result(text, "2026-11-18"))

    def test_части_не_сходятся_с_заголовком_пост_отклонён(self):
        # мутация: не сверять части с заголовком -> опечатка в посте уходит в ряд.
        text = self.posts["2026-11-18"]["text"].replace("73,742", "93,742")
        self.assertIsNone(self.a.parse_result(text, "2026-11-18"))

    def test_пост_привязан_к_своему_дню(self):
        # В посте 18.11 дата доразмещения названа словами: «18 ноября 2026 г.».
        # Тот же текст, «опубликованный» другим днём, итогом этого дня не станет.
        # мутация: верить дате публикации -> итог 18.11 записан за 19.11.
        text = self.posts["2026-11-18"]["text"]
        self.assertIsNone(self.a.parse_result(text, "2026-11-19"))
        self.assertIsNotNone(self.a.parse_result(text, "2026-11-18"))

    def test_все_посты_страницы_разобраны_по_дням(self):
        got = self.results()
        self.assertEqual(sorted(got), ["2026-07-01", "2026-09-02", "2026-10-07",
                                       "2026-10-14", "2026-10-21", "2026-11-11",
                                       "2026-11-18"])
        self.assertEqual(got["2026-10-21"]["total_bn"], 177.128)
        self.assertEqual(got["2026-11-18"]["url"], "https://t.me/minfin/9501")


class TestOfficialIsPrimary(OfficialCase):
    """Число дня — итог Минфина; доска — сверка и резерв; оба числа в meta."""

    def test_итог_минфина_идёт_в_ряд_доска_в_meta(self):
        _sid, pts, meta = self.run_range("2026-11-18", "2026-11-18",
                                         official=self.results())
        # Доска: 27 697 363 + 42 946 854 бумаги по 1000 ₽ = 70,6442 млрд.
        # мутация: писать в ряд биржевое число -> 70,6442 (занижение на 4 %).
        self.assertEqual(pts, {"2026-11-18": 73.742})
        last = meta["last"]
        self.assertEqual(last["source"], "minfin_tg")
        self.assertEqual(last["placed_bn"], 73.742)
        self.assertAlmostEqual(last["placed_board_bn"], 70.6442, places=4)
        self.assertEqual(last["drpa_bn"], 2.877)
        self.assertEqual(last["official_url"], "https://t.me/minfin/9501")
        self.assertEqual(meta["official_days"], ["2026-11-18"])
        self.assertEqual(meta["method"], "minfin_tg")

    def test_нет_поста_день_остаётся_биржевым_и_это_сказано(self):
        _sid, pts, meta = self.run_range("2026-11-18", "2026-11-18", official={})
        self.assertAlmostEqual(pts["2026-11-18"], 70.6442, places=4)
        self.assertEqual(meta["last"]["source"], "moex_pact")
        self.assertIsNone(meta["last"]["placed_official_bn"])
        self.assertIn("без доразмещения", meta["note"])
        self.assertEqual(meta["official_days"], [])

    def test_отказ_канала_не_роняет_ряд(self):
        # мутация: не ловить отказ канала -> при лежащем t.me ряд аукционов встаёт.
        boom = self.a.FetchError("t.me: timed out")
        _sid, pts, meta = self.run_range("2026-11-18", "2026-11-18", official=boom)
        self.assertAlmostEqual(pts["2026-11-18"], 70.6442, places=4)
        self.assertEqual(meta["status"], "ok")
        self.assertIn("канал Минфина не прочитан", meta["note"])
        self.assertEqual(meta["last"]["source"], "moex_pact")

    def test_итог_меньше_биржевого_не_принимается(self):
        # Официальный итог включает всё, что видела доска. Меньше — значит пост не
        # про этот день или разобран неверно. мутация: снять нижнюю границу ->
        # 12,0 млрд за день, когда доска зарегистрировала 70,6.
        bad = {"2026-11-18": {"date": "2026-11-18", "total_bn": 12.0, "auction_bn": 12.0,
                              "drpa_bn": 0.0, "issues": [], "url": "https://t.me/x"}}
        _sid, pts, meta = self.run_range("2026-11-18", "2026-11-18", official=bad)
        self.assertAlmostEqual(pts["2026-11-18"], 70.6442, places=4)
        self.assertEqual(meta["last"]["source"], "moex_pact")
        self.assertEqual(meta["official_rejected"][0][:2], ["2026-11-18", 12.0])

    def test_пост_без_строк_доски_точку_не_создаёт(self):
        # Был ли аукцион, решает доска: за 22.07 строк нет — и итог, приписанный
        # этому дню, точку не создаёт. мутация: создавать точку по посту -> аукцион
        # в день, когда его не проводили.
        ghost = {"2026-07-22": {"date": "2026-07-22", "total_bn": 50.0, "auction_bn": 50.0,
                                "drpa_bn": 0.0, "issues": [], "url": None}}
        _sid, pts, _meta = self.run_range("2026-07-22", "2026-07-22", official=ghost)
        self.assertEqual(pts, {})

    def test_провал_аукциона_остаётся_провалом(self):
        # 30.09.2026: строка доски с нулём, поста об итогах нет.
        _sid, pts, meta = self.run_range("2026-09-30", "2026-09-30",
                                         official=self.results())
        self.assertEqual(pts, {"2026-09-30": 0.0})
        self.assertTrue(meta["last"]["failed"])

    def test_прошлые_биржевые_точки_досчитываются_до_итога(self):
        """В сторе с августа лежат биржевые числа: 11.11 — 62,70, 21.10 — 136,24.

        мутация: не трогать прошлые дни -> в последних двенадцати аукционах тайла
        вперемешку итоги Минфина и биржевые числа, на 4–29 % ниже.
        """
        stored = {"points": {"2026-07-01": 10.3649, "2026-10-21": 136.2381,
                             "2026-11-11": 62.7021, "2026-08-05": 0.0},
                  "meta": {}}
        with mock.patch.object(self.a.store, "load_series", return_value=stored):
            _sid, pts, meta = self.run_range("2026-11-18", "2026-11-18",
                                             official=self.results())
        self.assertEqual(pts["2026-11-11"], 88.874)
        self.assertEqual(pts["2026-10-21"], 177.128)
        # 01.07 сходится с итогом до сотых (10,3649 против 10,365) — не переписываем.
        self.assertNotIn("2026-07-01", pts)
        # 02.09, 07.10, 14.10 в сторе нет — пост точек не создаёт.
        self.assertEqual(sorted(pts), ["2026-10-21", "2026-11-11", "2026-11-18"])
        self.assertEqual(meta["official_days"],
                         ["2026-07-01", "2026-10-21", "2026-11-11", "2026-11-18"])
        self.assertEqual(meta["official_backfilled"][-1], ["2026-11-11", 62.7021, 88.874])


class TestAnnounceParsing(unittest.TestCase):
    def setUp(self):
        self.a = need(self, "pipeline.fetch.auctions", "next_auction", "ANNOUNCE_RE",
                      "ANNOUNCE_DAY")

    def announce(self, title, published, today):
        """Разбор анонса при ЗАМОРОЖЕННОМ сегодня.

        Часы обязательны: next_auction отбрасывает уже прошедшие аукционы, и без
        заморозки тест зелен ровно до даты из фикстуры. Так и вышло — правка от
        18.08.2026 («прошедший аукцион не бывает следующим») сделала этот тест
        календарной бомбой, и 20.08 он покраснел на ровном месте.
        """
        with mock.patch("pipeline.fetch.moex_press.scan_news",
                        return_value=[(101, title, published)]),              mock.patch.object(self.a.dates, "today_msk", return_value=today):
            return self.a.next_auction()

    def test_заголовок_анонса_узнаётся_и_дата_разбирается(self):
        title = ("О проведении 19 августа 2026 года аукциона по размещению ОФЗ "
                 "выпусков № 26252RMFS")
        self.assertTrue(self.a.ANNOUNCE_RE.search(title))
        m = self.a.ANNOUNCE_DAY.search(title)
        self.assertEqual((m.group(1), m.group(3)), ("19", "2026"))
        day, _got, url = self.announce(title, "2026-08-18", date(2026, 8, 18))
        self.assertEqual(day, "2026-08-19")
        self.assertIn("moex.com/n101", url)

    def test_прошедший_аукцион_не_объявляется_следующим(self):
        # Анонс живёт в ленте и ПОСЛЕ аукциона: со среды-вечера до следующего
        # вторника свежайший подходящий анонс — прошлый, и тайл 5 дней из 7 писал
        # «Назначен следующий: 19.08» про уже прошедшее размещение.
        title = ("О проведении 19 августа 2026 года аукциона по размещению ОФЗ "
                 "выпусков № 26252RMFS")
        self.assertEqual(self.announce(title, "2026-08-18", date(2026, 8, 21))[0], None)
        # В САМ день аукциона он ещё «следующий»: размещение идёт днём.
        self.assertEqual(self.announce(title, "2026-08-18", date(2026, 8, 19))[0],
                         "2026-08-19")

    def test_чужая_новость_анонсом_не_считается(self):
        for foreign in ("Об итогах аукциона по размещению ОФЗ",
                        "О проведении аукциона по размещению облигаций Банка России",
                        "Объем вторичных торгов облигациями в июле составил 2,2 трлн"):
            self.assertIsNone(self.a.ANNOUNCE_DAY.search(foreign),
                              f"дата вытащена из чужого заголовка: {foreign}")


if __name__ == "__main__":
    unittest.main()
