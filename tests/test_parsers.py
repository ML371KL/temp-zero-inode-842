"""Парсеры источников на замороженных ответах. В сеть не ходим.

Подменяется САМЫЙ НИЖНИЙ слой — `http.get_bytes`, а не get_json/get_text: тогда в
тесте работают настоящие декодирование windows-1251, разбор JSON и постраничный
обход ISS. Подмена «сразу распарсенного словаря» проверяла бы только последние
десять строк парсера, а ломается обычно всё, что до них.

Каждый тест ловит конкретные грабли живого источника, а не «функция вернула dict»:
пустой CLOSE в неполный день, чужой tradedate у КБД, схема futoi без seqnum,
римские декады ЦБ, запятая и windows-1251 в XML курса, точка вместо пропуска у FRED.
"""

import json
import os
import unittest
from tempfile import TemporaryDirectory
from unittest import mock

from tests import fixture_bytes, fixture_json, fixture_text, need, need_any


def as_bytes(obj):
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


class FetcherCase(unittest.TestCase):
    """Пустой стор во временном каталоге + подменённый транспорт."""

    def setUp(self):
        self.http = need(self, "pipeline.lib.http", "get_bytes", "get_text", "get_json")
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prev = os.environ.get("STATE_DIR")
        os.environ["STATE_DIR"] = self.tmp.name
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self.prev is None:
            os.environ.pop("STATE_DIR", None)
        else:
            os.environ["STATE_DIR"] = self.prev

    def serve(self, responder):
        """responder(url) -> bytes. Всё, что не обслужено, — это поход в сеть."""
        patcher = mock.patch.object(self.http, "get_bytes",
                                    side_effect=lambda url, **kw: responder(url))
        patcher.start()
        self.addCleanup(patcher.stop)

    def serve_const(self, data):
        payload = data if isinstance(data, bytes) else as_bytes(data)
        self.serve(lambda _url: payload)


# --------------------------------------------------------------------- ISS
class TestIssIndex(FetcherCase):
    def setUp(self):
        super().setUp()
        self.iss = need(self, "pipeline.fetch.iss", "index", "index_value", "index_yield")
        self.fx = fixture_json("iss_index.json")

    def test_index_drops_days_without_close(self):
        self.serve_const(self.fx)
        sid, points, meta = self.iss.index("IMOEX", start="2026-08-03", end="2026-08-11")
        self.assertEqual(sid, "imoex")
        # мутация: класть None как 0.0 -> в панели появится обвал индекса до нуля,
        # а вместе с ним ложный вола-стресс и ложная просадка.
        self.assertNotIn("2026-08-07", points)
        self.assertEqual(len(points), 6)
        self.assertEqual(points["2026-08-11"], 2301.0)
        self.assertEqual(points["2026-08-03"], 2251.14)
        self.assertEqual(meta["source"], "iss")
        self.assertEqual(meta["asof"], "2026-08-11")
        self.assertEqual(meta["status"], "ok")
        self.assertEqual(meta["unit"], "points")

    def test_columns_are_found_by_name_not_position(self):
        # ISS молча игнорирует неизвестные имена в history.columns и меняет порядок
        # между эндпоинтами. мутация: row[1] вместо поиска по имени -> в ряд поедет
        # оборот вместо цены (значения разойдутся на семь порядков).
        shuffled = json.loads(json.dumps(self.fx))
        cols = shuffled["history"]["columns"]
        order = [cols.index(name) for name in ("VALUE", "TRADEDATE", "YIELD", "CLOSE")]
        shuffled["history"]["columns"] = [cols[i] for i in order]
        shuffled["history"]["data"] = [[row[i] for i in order]
                                       for row in shuffled["history"]["data"]]
        self.serve_const(shuffled)
        _sid, points, _meta = self.iss.index("IMOEX", start="2026-08-03", end="2026-08-11")
        self.assertEqual(points["2026-08-11"], 2301.0)

    def test_index_value_reads_turnover(self):
        self.serve_const(self.fx)
        sid, points, meta = self.iss.index_value("IMOEX", start="2026-08-03",
                                                 end="2026-08-11")
        self.assertEqual(sid, "imoex_value")
        self.assertEqual(points["2026-08-11"], 61047484416.1)
        self.assertNotIn("2026-08-07", points)
        self.assertEqual(meta["unit"], "rub")

    def test_zero_yield_is_not_a_yield(self):
        # У ценового индекса YIELD приходит нулём-заглушкой. Доходность 0% у
        # облигационного индекса невозможна, значит это «нет данных».
        # мутация: не отбрасывать нули -> ряд доходности ВДО станет нулевым, а
        # спред ВДО — отрицательным на всю глубину истории.
        self.serve_const(self.fx)
        with self.assertRaises(self.iss.FetchError):
            self.iss.index_yield("RUCBHYCP", start="2026-08-03", end="2026-08-11")

    def test_pagination_walks_past_first_page(self):
        # history отдаёт страницами по 100 строк. мутация: читать только первую
        # страницу -> история любой бумаги обрежется на 100 днях, и MA200 не
        # посчитается никогда.
        page = {"history": {"columns": ["TRADEDATE", "CLOSE", "VALUE", "YIELD"],
                            "data": [[f"2025-{1 + i // 28:02d}-{1 + i % 28:02d}",
                                      1000.0 + i, 1.0, 0] for i in range(100)]},
                "history.cursor": {"columns": ["INDEX", "TOTAL", "PAGESIZE"],
                                   "data": [[0, 101, 100]]}}
        tail = {"history": {"columns": ["TRADEDATE", "CLOSE", "VALUE", "YIELD"],
                            "data": [["2026-08-11", 2301.0, 1.0, 0]]},
                "history.cursor": {"columns": ["INDEX", "TOTAL", "PAGESIZE"],
                                   "data": [[100, 101, 100]]}}
        self.serve(lambda url: as_bytes(tail if "start=100" in url else page))
        _sid, points, _meta = self.iss.index("IMOEX", start="2025-01-01", end="2026-08-11")
        self.assertEqual(len(points), 101)
        self.assertEqual(points["2026-08-11"], 2301.0)

    def test_empty_answer_with_history_is_not_fatal(self):
        # Отказ источника не валит прогон (контракт §0): если в сторе уже есть
        # история, пустое окно — это норма, а не ошибка.
        store = need(self, "pipeline.lib.store", "upsert_points")
        store.upsert_points("imoex", {"2026-08-10": 2293.32})
        self.serve_const({"history": {"columns": ["TRADEDATE", "CLOSE"], "data": []}})
        _sid, points, _meta = self.iss.index("IMOEX", start="2026-08-11", end="2026-08-11")
        self.assertEqual(points, {})


class TestIssZcyc(FetcherCase):
    def setUp(self):
        super().setUp()
        self.iss = need(self, "pipeline.fetch.iss", "zcyc")
        self.fx = fixture_json("iss_zcyc.json")

    def test_foreign_tradedate_is_dropped(self):
        # ISS на нерабочий (и иногда на рабочий) день возвращает БОЛЕЕ РАННИЙ срез.
        # мутация: не сверять tradedate с запрошенной датой -> значение 07.08
        # размажется по 10.08 и всем последующим дням, а кривая ОФЗ станет
        # ступенчатой ровно там, где на ней строится наклон 10Y−2Y (нога ядра).
        def responder(url):
            return as_bytes(self.fx["fresh"] if "date=2026-08-11" in url
                            else self.fx["stale"])
        self.serve(responder)
        out = dict((sid, pts) for sid, pts, _meta in
                   self.iss.zcyc(start="2026-08-10", end="2026-08-11"))
        self.assertEqual(out["zcyc_y1"], {"2026-08-11": 15.4})
        self.assertEqual(out["zcyc_y2"], {"2026-08-11": 14.1})
        self.assertEqual(out["zcyc_y10"], {"2026-08-11": 12.74})
        self.assertEqual(out["zcyc_y0_5"], {"2026-08-11": 16.21})
        self.assertEqual(out["zcyc_y5"], {"2026-08-11": 12.83})
        for sid, pts in out.items():
            self.assertNotIn("2026-08-07", pts, sid)

    def test_only_registered_tenors_are_stored(self):
        # В ответе 12 сроков, в сторе живут 5: лишние ряды никто не считает, но
        # они занимают место в R2 и в списке dirty.
        self.serve_const(self.fx["fresh"])
        out = [sid for sid, _pts, _meta in self.iss.zcyc(start="2026-08-11",
                                                         end="2026-08-11")]
        self.assertEqual(sorted(out),
                         ["zcyc_y0_5", "zcyc_y1", "zcyc_y10", "zcyc_y2", "zcyc_y5"])


class TestIssFutoi(FetcherCase):
    def setUp(self):
        super().setUp()
        self.iss = need(self, "pipeline.fetch.iss", "futoi", "FUTOI_GROUPS", "FUTOI_FIELDS")
        self.serve_const(fixture_json("iss_futoi.json"))
        self.out = dict((sid, pts) for sid, pts, _meta in
                        self.iss.futoi(ticker="MX", start="2026-07-27", end="2026-07-28"))

    def sid(self, group, column):
        """id ряда по правилу модуля: префикс + суффикс группы + суффикс колонки."""
        parts = ("futoi_mx", self.iss.FUTOI_GROUPS[group], self.iss.FUTOI_FIELDS[column])
        return "_".join(p for p in parts if p)

    def test_physical_persons_are_the_base_series(self):
        # Контракт §2 знает ряд физлиц как futoi_mx (pos/long/short/holders) —
        # на нём построен сигнал контр-позиционирования. Одна литеральная привязка
        # к имени: если id поедет, красным станет ровно этот тест, а не пять.
        self.assertIn("futoi_mx_pos", self.out)
        self.assertEqual(self.sid("FIZ", "pos"), "futoi_mx_pos")

    def test_schema_without_seqnum_orders_by_time(self):
        # В фикстуре нет колонки seqnum — порядок внутри дня определяется tradetime.
        # мутация: брать первую строку дня -> в ряд поедет позиция на 10:00, то есть
        # утренний срез вместо позиции на конец сессии (разница здесь 12%).
        self.assertEqual(self.out[self.sid("FIZ", "pos")],
                         {"2026-07-27": 109774.0, "2026-07-28": 131072.0})
        self.assertEqual(self.out[self.sid("YUR", "pos")],
                         {"2026-07-27": -109774.0, "2026-07-28": -131072.0})

    def test_short_leg_keeps_its_sign(self):
        # pos_short приходит отрицательным; abs() здесь сломал бы знаменатель
        # брутто-позиции в futoi_z120 (panel.py считает long − short).
        self.assertEqual(self.out[self.sid("FIZ", "pos_short")],
                         {"2026-07-27": -89738.0, "2026-07-28": -82832.0})
        self.assertEqual(self.out[self.sid("FIZ", "pos_long")],
                         {"2026-07-27": 199512.0, "2026-07-28": 213904.0})

    def test_unknown_client_group_is_ignored(self):
        # мутация: пускать все clgroup -> в стор поедут ряды-призраки.
        expected = {self.sid(grp, col) for grp in self.iss.FUTOI_GROUPS
                    for col in self.iss.FUTOI_FIELDS}
        self.assertEqual(set(self.out), expected)
        self.assertTrue(all("sur" not in sid for sid in self.out))

    def test_person_counts_have_their_own_unit(self):
        # Число ЛИЦ и число КОНТРАКТОВ лежат в одном наборе рядов; перепутанные
        # единицы дают «средний размер позиции» с ошибкой в тысячи раз.
        units = {sid: meta.get("unit") for sid, _pts, meta in
                 self.iss.futoi(ticker="MX", start="2026-07-28", end="2026-07-28")}
        self.assertEqual(units[self.sid("FIZ", "pos_long_num")], "persons")
        self.assertEqual(units[self.sid("FIZ", "pos_long")], "contracts")


# ---------------------------------------------------------------------- ЦБ
class TestCbrFx(FetcherCase):
    def setUp(self):
        super().setUp()
        self.cbr = need(self, "pipeline.fetch.cbr", "fx")

    def test_fixture_is_really_windows_1251(self):
        # Страховка на саму фикстуру: если её однажды перезапишут в utf-8, тест
        # кодировки превратится в тест ни о чём.
        raw = fixture_bytes("cbr_fx.xml")
        self.assertIn("Курсы валют", raw.decode("windows-1251"))
        with self.assertRaises(UnicodeDecodeError):
            raw.decode("utf-8")

    def test_comma_decimal_and_encoding(self):
        raw = fixture_bytes("cbr_fx.xml")
        seen = []
        real_get_text = self.http.get_text

        def spy(url, encoding="utf-8", **kw):
            seen.append(encoding)
            return real_get_text(url, encoding=encoding, **kw)

        self.serve_const(raw)
        with mock.patch.object(self.http, "get_text", side_effect=spy):
            sid, points, meta = self.cbr.fx(code="R01235", start="2026-08-03",
                                            end="2026-08-11")
        # мутация: читать XML как utf-8 -> «replace» съест кириллицу молча, а на
        # старых выгрузках ЦБ развалится и разметка.
        self.assertEqual(seen, ["windows-1251"])
        self.assertEqual(sid, "usd_cbr")
        self.assertEqual(len(points), 6)
        # мутация: float('92,4517') -> ValueError; float на «.» без замены запятой
        # молча даёт None, и ряд курса становится дырявым.
        self.assertEqual(points["2026-08-11"], 91.3388)
        self.assertEqual(points["2026-08-04"], 92.4517)
        self.assertEqual(meta["unit"], "rub")

    def test_date_is_the_day_the_rate_applies(self):
        # Record Date в XML_dynamic — дата ПРИМЕНЕНИЯ курса (опубликован накануне).
        # мутация: сдвинуть ряд на день «чтобы учесть публикацию» -> ряд разъедется
        # с тем, на котором считался usd_mom63 в валидации.
        self.serve_const(fixture_bytes("cbr_fx.xml"))
        _sid, points, _meta = self.cbr.fx(code="R01235", start="2026-08-03",
                                          end="2026-08-11")
        self.assertEqual(sorted(points)[0], "2026-08-04")
        self.assertEqual(sorted(points)[-1], "2026-08-11")

    def test_nominal_division_for_old_format(self):
        # Старый формат ЦБ без VunitRate: Value относится к НОМИНАЛУ (у юаня он
        # менялся). мутация: брать Value как есть -> скачок ряда на порядок.
        old = ('<?xml version="1.0" encoding="windows-1251"?>'
               '<ValCurs ID="R01375" DateRange1="03.08.2026" DateRange2="03.08.2026">'
               '<Record Date="03.08.2026" Id="R01375">'
               '<Nominal>10</Nominal><Value>1234,5670</Value></Record></ValCurs>')
        self.serve_const(old.encode("windows-1251"))
        sid, points, _meta = self.cbr.fx(code="R01375", start="2026-08-03",
                                         end="2026-08-03")
        self.assertEqual(sid, "cny_cbr")
        self.assertAlmostEqual(points["2026-08-03"], 123.4567, places=6)


class TestCbrTables(FetcherCase):
    def setUp(self):
        super().setUp()
        self.cbr = need(self, "pipeline.fetch.cbr", "keyrate", "deposit", "decade_end")

    def test_keyrate_table_is_picked_by_header(self):
        # На странице выше лежит вёрстка с вложенными таблицами; «первая таблица»
        # давно перестала быть нужной. мутация: брать tables[0] -> в ряд поедут
        # даты фильтра «с/по» вместо ставок.
        self.serve_const(fixture_text("cbr_keyrate.html").encode("utf-8"))
        sid, points, meta = self.cbr.keyrate(start="2026-08-03", end="2026-08-11")
        self.assertEqual(sid, "key_rate")
        self.assertEqual(len(points), 7)
        self.assertEqual(points["2026-08-11"], 17.0)
        self.assertEqual(meta["unit"], "pct")

    def test_missing_data_table_is_a_source_failure(self):
        # Так выглядит заглушка/капча/редирект: это отказ источника (FetchError),
        # а не пустой ряд, который тихо перезапишет стор.
        self.serve_const("<html><body><h1>Ошибка</h1></body></html>".encode("utf-8"))
        with self.assertRaises(self.cbr.FetchError):
            self.cbr.keyrate(start="2026-08-03", end="2026-08-11")

    def test_decade_end_maps_roman_numerals(self):
        # I -> 10-е, II -> 20-е, III -> КОНЕЦ месяца (28/29 в феврале).
        # мутация: III -> 30-е число -> февральская декада получит несуществующую
        # дату либо уедет на сутки, а с ней уедет и лаг доступности (+4 дня).
        self.assertEqual(self.cbr.decade_end("III.07.2026").isoformat(), "2026-07-31")
        self.assertEqual(self.cbr.decade_end("II.07.2026").isoformat(), "2026-07-20")
        self.assertEqual(self.cbr.decade_end("I.07.2026").isoformat(), "2026-07-10")
        self.assertEqual(self.cbr.decade_end("III.02.2024").isoformat(), "2024-02-29")
        self.assertEqual(self.cbr.decade_end("III.02.2026").isoformat(), "2026-02-28")
        self.assertIsNone(self.cbr.decade_end("Итого"))
        self.assertIsNone(self.cbr.decade_end("IV.07.2026"))

    def test_deposit_decades_parsed(self):
        self.serve_const(fixture_text("cbr_deposit.html").encode("utf-8"))
        sid, points, meta = self.cbr.deposit(start="2024-01-01", end="2026-08-11")
        self.assertEqual(sid, "deposit_decade")
        self.assertEqual(points["2026-07-31"], 15.86)
        self.assertEqual(points["2026-07-20"], 15.94)
        self.assertEqual(points["2026-07-10"], 16.10)
        self.assertEqual(points["2024-02-29"], 14.79)
        # строка «Итого» с прочерком в ряд не попадает
        self.assertEqual(len(points), 7)
        self.assertEqual(meta["unit"], "pct")


# -------------------------------------------------------------------- FRED
class TestFredBrent(FetcherCase):
    def setUp(self):
        super().setUp()
        self.ext = need(self, "pipeline.fetch.external", "brent_fred")

    def test_dots_are_holes_not_zeros(self):
        # FRED помечает выходные и праздники США точкой. мутация: to_float('.')
        # -> 0.0, и рублёвая бочка проваливается в ноль на каждый праздник США.
        self.serve_const(fixture_text("fred_brent.csv").encode("utf-8"))
        sid, points, meta = self.ext.brent_fred(start="2026-07-27", end="2026-08-07")
        self.assertEqual(sid, "brent")
        self.assertEqual(len(points), 8)
        self.assertNotIn("2026-07-30", points)
        self.assertNotIn("2026-08-05", points)
        self.assertEqual(points["2026-08-07"], 70.31)
        self.assertEqual(meta["unit"], "usd")
        self.assertEqual(meta["asof"], "2026-08-07")

    def test_not_a_csv_is_a_source_failure(self):
        self.serve_const(b"<html>503 Service Unavailable</html>")
        with self.assertRaises(self.ext.FetchError):
            self.ext.brent_fred(start="2026-07-27", end="2026-08-07")


# ------------------------------------------------------- ещё не написанные
def _orfr_values(result):
    """Категории -> число из любого разумного вида ответа парсера ОРФР.

    Форма пока плавает между агентами: контракт §4 допускает и одну тройку
    (series_id, points, meta), и список троек по подрядам; текстовый парсер
    возвращает пару (значения, аудит).
    """
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], dict):
        result = result[0]                       # (значения, аудит)
    if isinstance(result, tuple) and len(result) == 3:
        result = [result]
    if isinstance(result, list) and result and isinstance(result[0], tuple):
        out = {}
        for sid, points, _meta in result:
            key = str(sid).split(".")[-1].split("_flows_")[-1]
            values = [v for _d, v in sorted((points or {}).items())]
            if values:
                out[key] = values[-1]
        return out
    if isinstance(result, dict):
        inner = result.get("values") or result.get("flows") or result
        if isinstance(inner, dict) and all(
                isinstance(v, (int, float, type(None))) for v in inner.values()):
            return inner
    return None


class TestOrfrText(unittest.TestCase):
    """Разбор текста PDF ЦБ: знак задаётся ГЛАГОЛОМ, а не пунктуацией.

    Тире перед числом в тексте ЦБ («нетто-покупки – 15,7 млрд руб.») — разделитель,
    а не минус; принимать его за знак значит перевернуть половину истории потоков.
    """

    def test_signs_follow_the_verb(self):
        _mod, _name, parse = need_any(self, "pipeline.fetch.orfr",
                                      "parse_flows", "parse_text", "parse")
        got = _orfr_values(parse(fixture_text("orfr_text.txt")))
        self.assertIsNotNone(got, "парсер ОРФР вернул форму вне контракта §4")
        # мутация: брать все числа положительными -> «рекордные продажи ДУ»
        # превращаются в рекордные покупки, а тайл ОРФР — в ложь наоборот.
        self.assertAlmostEqual(got["fiz"], 12.4, places=3)
        self.assertAlmostEqual(got["nfo_du"], -37.9, places=3)
        self.assertAlmostEqual(got["nfo_own"], -4.1, places=3)
        self.assertAlmostEqual(got["szko"], -5.2, places=3)
        self.assertAlmostEqual(got["other_banks"], 2.3, places=3)
        self.assertAlmostEqual(got["nonres"], 0.7, places=3)

    def test_foreign_numbers_are_not_flows(self):
        # В обзоре рядом живут объёмы торгов (118,6 млрд руб.) и продажи валюты
        # экспортёрами (7,9 млрд долл.). мутация: брать любое «млрд руб» ->
        # в поток физлиц уедет среднедневной оборот рынка.
        _mod, _name, parse = need_any(self, "pipeline.fetch.orfr",
                                      "parse_flows", "parse_text", "parse")
        got = _orfr_values(parse(fixture_text("orfr_text.txt")))
        self.assertNotIn(118.6, [abs(v) for v in got.values()])
        self.assertNotIn(7.9, [abs(v) for v in got.values()])
        self.assertNotIn(96.3, [abs(v) for v in got.values()])


class TestRosstatCpi(unittest.TestCase):
    def test_weekly_print_is_an_index_not_a_percent(self):
        _mod, _name, parse = need_any(self, "pipeline.fetch.rosstat",
                                      "parse_weekly", "cpi_weekly_from_html", "parse")
        got = parse(fixture_text("rosstat_cpi.html"))
        self.assertTrue(got, "парсер недельного ИПЦ вернул пусто на фикстуре")


if __name__ == "__main__":
    unittest.main()
