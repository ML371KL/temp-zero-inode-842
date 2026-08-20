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

В сеть не ходим: подменяется `auctions.http.get_json` замороженными ответами доски.
"""

import unittest
from datetime import date
from unittest import mock

from tests import fixture_json, need


class AuctionsCase(unittest.TestCase):
    def setUp(self):
        self.a = need(self, "pipeline.fetch.auctions", "auctions", "day_summary",
                      "is_echo", "next_auction")
        self.days = fixture_json("iss_pact.json")

    def serve(self, announce=None):
        """Транспорт: доска отвечает из фикстуры, лента новостей — по требованию."""
        def fake(url, **_kw):
            for day, block in self.days.items():
                if "date=%s" % day in url:
                    return {"history": {"columns": block["columns"], "data": block["data"]}}
            return {"history": {"columns": self.days["2026-07-22"]["columns"], "data": []}}
        patches = [mock.patch.object(self.a.http, "get_json", side_effect=fake)]
        if announce is not None:
            patches.append(mock.patch.object(self.a, "next_auction",
                                             return_value=announce))
        return patches

    def run_range(self, start, end, announce=(None, None, None)):
        patches = self.serve(announce)
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
        _sid, _pts, meta = self.run_range("2026-07-01", "2026-07-01")
        self.assertEqual(meta["method"], "moex_pact")
        self.assertIn("ДРПА", meta["splice"])

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
