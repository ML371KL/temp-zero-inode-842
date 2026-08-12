"""Дивидендный календарь: три источника по приоритету и защита от подмены величины.

Календарь был последним рядом, который заполнялся руками, — и ручной файл разошёлся
с фактом (SBER 35,0 ₽ против 37,64; у LKOH запись с прошлогодней суммой и придуманной
датой). Теперь порядок такой: T-Invest → smart-lab → inputs/dividends.yml.

Что здесь проверяется — ровно то, что уже ломалось:

1. **адрес gRPC-шлюза.** Сервис — часть ПОЛНОГО ИМЕНИ пакета
   (`…contract.v1.InstrumentsService`), а не отдельный сегмент пути. Со слэшем шлюз
   отдаёт 404, и это читается как «метода нет», хотя дело в адресе;
2. **шапка smart-lab со случайными пробелами.** «Див.Дох.» приходит вёрсткой как
   «Див.<br>Дох.» и после снятия тегов становится «див. дох.» — наивный поиск
   подстроки терял колонку доходности, и календарь выходил без единого процента;
3. **тикер из одного знака.** «T» (Т-Технологии) и «X5» отсекались требованием трёх
   букв, то есть из календаря выпадали самые крупные бумаги;
4. **вес индекса.** Просадка индекса = вес × доходность. Без веса тайл показывал бы
   13,5% Сбербанка как «падение рынка на 13,5%», хотя индекс просядет на 1,7%.

В сеть не ходим: подменяется `http.get_json` / `http.get_text`.
"""

import json
import unittest
from datetime import date
from unittest import mock

from tests import fixture_text, need

WEIGHTS = {"analytics": {"columns": ["ticker", "weight"],
                         "data": [["SBER", 13.9], ["YDEX", 6.89], ["T", 5.17],
                                  ["RAGR", 0.29], ["VTBR", 2.53]]}}
SHARES = {"instruments": [
    {"ticker": "SBER", "uid": "u-sber", "classCode": "TQBR", "divYieldFlag": True},
    {"ticker": "YDEX", "uid": "u-ydex", "classCode": "TQBR", "divYieldFlag": True},
    {"ticker": "VTBR", "uid": "u-vtbr", "classCode": "TQBR", "divYieldFlag": False},
    {"ticker": "SBER", "uid": "u-other", "classCode": "SPBXM", "divYieldFlag": True},
]}
DIVS = {"u-sber": {"dividends": [
            {"recordDate": "2026-07-20T00:00:00Z", "lastBuyDate": "2026-07-17T00:00:00Z",
             "dividendNet": {"units": "37", "nano": 640000000},
             "yieldValue": {"units": "13", "nano": 490000000},
             "closePrice": {"units": "277", "nano": 230000000}}]},
        "u-ydex": {"dividends": [
            {"recordDate": "2026-09-21T00:00:00Z", "lastBuyDate": "2026-09-18T00:00:00Z",
             "dividendNet": {"units": "110", "nano": 0},
             "yieldValue": {"units": "2", "nano": 740000000},
             "closePrice": {"units": "4017", "nano": 0}}]}}


class TinvestCase(unittest.TestCase):
    def setUp(self):
        self.t = need(self, "pipeline.fetch.tinvest", "call", "quotation", "shares",
                      "dividends", "ready")

    def test_адрес_собирается_через_точку(self):
        # мутация: слэш вместо точки -> шлюз отдаёт 404 на любой метод.
        seen = {}

        def fake(url, **kw):
            seen["url"] = url
            return {}
        with mock.patch.object(self.t.http, "auth_token", return_value="x"), \
             mock.patch.object(self.t.http, "get_json", side_effect=fake):
            self.t.call("InstrumentsService", "Shares")
        self.assertIn("contract.v1.InstrumentsService/Shares", seen["url"])
        self.assertNotIn("contract.v1/InstrumentsService", seen["url"])

    def test_без_токена_запрос_не_уходит(self):
        with mock.patch.object(self.t.http, "auth_token", return_value=None), \
             mock.patch.object(self.t.http, "get_json") as never:
            with self.assertRaises(self.t.FetchError):
                self.t.call("InstrumentsService", "Shares")
        never.assert_not_called()

    def test_разбор_units_и_nano(self):
        self.assertEqual(self.t.quotation({"units": "37", "nano": 640000000}), 37.64)
        self.assertEqual(self.t.quotation({"units": 110, "nano": 0}), 110.0)
        self.assertEqual(self.t.quotation({"units": "-1", "nano": -500000000}), -1.5)
        for junk in (None, "37.64", {}, {"units": "abc"}):
            self.assertIn(self.t.quotation(junk), (None, 0.0))

    def test_справочник_фильтрует_чужой_режим(self):
        with mock.patch.object(self.t, "call", return_value=SHARES):
            book = self.t.shares()
        self.assertEqual(sorted(book), ["SBER", "VTBR", "YDEX"])
        self.assertEqual(book["SBER"]["uid"], "u-sber", "взят uid чужого режима торгов")
        self.assertFalse(book["VTBR"]["pays_dividends"])

    def test_даты_обрезаются_до_дня(self):
        with mock.patch.object(self.t, "call", return_value=DIVS["u-sber"]):
            got = self.t.dividends("u-sber", "2026-01-01", "2027-01-01")
        self.assertEqual(got[0]["record_date"], "2026-07-20")
        self.assertEqual(got[0]["last_buy_date"], "2026-07-17")
        self.assertEqual(got[0]["amount_rub"], 37.64)


class CalendarCase(unittest.TestCase):
    def setUp(self):
        self.d = need(self, "pipeline.fetch.dividends", "calendar", "parse_calendar",
                      "index_weights", "from_tinvest")
        self.tv = need(self, "pipeline.fetch.tinvest", "shares")

    def serve(self, tinvest_ok=True, smartlab=None):
        """Подменяем оба транспорта: ISS-веса через get_json, smart-lab через get_text."""
        def get_json(url, **kw):
            if "analytics/IMOEX" in url:
                return WEIGHTS
            if "ISSUESIZE" in url or "securities/" in url:
                return {"description": {"columns": ["name", "title", "value"],
                                        "data": [["ISSUESIZE", "Объём", "1000000"]]}}
            raise AssertionError("незамоканный JSON-запрос: %s" % url)
        patches = [mock.patch.object(self.d.http, "get_json", side_effect=get_json),
                   mock.patch.object(self.d.http, "get_text",
                                     return_value=smartlab or fixture_text("smartlab_dividends.html"))]
        if tinvest_ok:
            patches += [mock.patch.object(self.tv, "ready", return_value=True),
                        mock.patch.object(self.tv, "shares", return_value={
                            k: {"uid": v["uid"], "pays_dividends": v["divYieldFlag"]}
                            for k, v in ((i["ticker"], i) for i in SHARES["instruments"]
                                         if i["classCode"] == "TQBR")}),
                        mock.patch.object(self.tv, "dividends",
                                          side_effect=lambda uid, f, t: [
                                              dict(r, record_date=r["recordDate"][:10],
                                                   last_buy_date=r["lastBuyDate"][:10],
                                                   payment_date=None,
                                                   amount_rub=self.tv.quotation(r["dividendNet"]),
                                                   yield_pct=self.tv.quotation(r["yieldValue"]),
                                                   price=self.tv.quotation(r["closePrice"]))
                                              for r in (DIVS.get(uid) or {}).get("dividends", [])])]
        else:
            patches.append(mock.patch.object(self.tv, "ready", return_value=False))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_приоритет_у_tinvest(self):
        self.serve(tinvest_ok=True)
        _sid, _pts, meta = self.d.calendar(with_amounts=False)
        self.assertEqual(meta["origin"], "tinvest")
        tickers = {i["ticker"] for i in meta["items"]}
        self.assertEqual(tickers, {"SBER", "YDEX"}, "VTBR не платит дивиденды — лишний запрос")

    def test_без_токена_падаем_на_smartlab(self):
        self.serve(tinvest_ok=False)
        _sid, _pts, meta = self.d.calendar(with_amounts=False)
        self.assertEqual(meta["origin"], "smartlab")
        self.assertTrue(meta["items"], "скрейп не дал ни одной записи")

    def test_asof_это_день_чтения_а_не_будущая_отсечка(self):
        # ОПЛАЧЕНО ПРОДОМ: make_meta по умолчанию берёт максимальную дату точек, а
        # точки здесь — БУДУЩИЕ отсечки. Витрина от 12.08 получала asof 2026-10-12,
        # фронт дату из будущего в подпись не пускает — и тайл с исправными числами
        # показывал «нет данных». Та же дата уезжала в блок sources.
        #
        # День чтения ЗАМОРОЖЕН, а не взят из тех же часов, что смотрит код: сверка
        # `today_msk()` с `today_msk()` верна при любой поломке — включая ту самую,
        # когда фетчер снова начнёт брать дату из точек, если они окажутся в прошлом.
        # Дата взята заведомо РАНЬШЕ отсечек фикстуры (июль и сентябрь 2026), то есть
        # умолчание make_meta здесь дало бы другой ответ.
        with mock.patch.object(self.d.dates, "today_msk", return_value=date(2026, 6, 30)):
            self.serve(tinvest_ok=True)
            _sid, _pts, meta = self.d.calendar(with_amounts=False)
        self.assertEqual(meta["asof"], "2026-06-30")
        # Ловушка в чистом виде и без зависимости от календаря (правило 1 набора:
        # никаких «сегодня минус N»): умолчание make_meta на ряде из будущих точек
        # отдаёт дату из будущего, и фетчер обязан её перебить.
        self.assertEqual(self.d.make_meta("x", "u", {"2099-01-01": 1.0})["asof"],
                         "2099-01-01")

    def test_гэп_индекса_это_вес_на_доходность(self):
        # мутация: взять доходность бумаги как просадку индекса -> 13,5% вместо 1,7%.
        self.serve(tinvest_ok=True)
        _sid, _pts, meta = self.d.calendar(with_amounts=False)
        sber = next(i for i in meta["items"] if i["ticker"] == "SBER")
        self.assertAlmostEqual(sber["index_drag_pct"], 13.9 * 13.49 / 100, places=3)
        self.assertLess(sber["index_drag_pct"], sber["yield_pct"])

    def test_бумаги_вне_индекса_отбрасываются(self):
        self.serve(tinvest_ok=False)
        _sid, _pts, meta = self.d.calendar(with_amounts=False)
        self.assertIn("DIAS", meta["skipped_non_index"])
        self.assertNotIn("DIAS", {i["ticker"] for i in meta["items"]})

    def test_без_весов_уходим_в_ручной_резерв(self):
        with mock.patch.object(self.d, "index_weights", return_value={}), \
             mock.patch.object(self.d, "_fallback", return_value=("dividends", {}, {})) as fb:
            self.d.calendar()
        fb.assert_called_once()
        self.assertIn("состав индекса", fb.call_args[0][0])


class SmartlabParsingCase(unittest.TestCase):
    def setUp(self):
        self.d = need(self, "pipeline.fetch.dividends", "parse_calendar")
        self.html = fixture_text("smartlab_dividends.html")

    def test_шапка_с_пробелами_находится(self):
        # «Див.Дох.» приходит как «Див.<br>Дох.» -> «див. дох.». Наивный поиск
        # подстроки «див.дох» её теряет, и весь календарь выходит без процентов.
        rows = self.d.parse_calendar(self.html)
        self.assertTrue(any(r["yield_pct"] is not None for r in rows),
                        "колонка доходности потеряна")

    def test_короткий_тикер_не_выпадает(self):
        # «T» — Т-Технологии, вес в индексе 5%. Требование трёх букв его выкидывало.
        tickers = {r["ticker"] for r in self.d.parse_calendar(self.html)}
        self.assertIn("T", tickers)

    def test_дата_отсечки_это_день_гэпа(self):
        rows = {r["ticker"]: r for r in self.d.parse_calendar(self.html)}
        self.assertEqual(rows["YDEX"]["ex_date"], "2026-09-21")
        self.assertEqual(rows["YDEX"]["buy_until"], "2026-09-18")

    def test_смена_вёрстки_это_отказ_а_не_мусор(self):
        with self.assertRaises(self.d.FetchError):
            self.d.parse_calendar("<table><tr><th>Что-то</th></tr></table>")


class TileAsofCase(unittest.TestCase):
    """Подпись тайла не имеет права уехать в будущее ни по одному из путей."""

    def setUp(self):
        self.m = need(self, "pipeline.compute.monitors", "BUILDERS")

    def tile(self, meta):
        class Store:
            def load_series(self, sid):
                return {"points": {}, "meta": meta} if sid == "dividends" else None

        builder = dict(self.m.BUILDERS)["dividends"]
        # Фиксированный момент: правило 1 набора запрещает считать от «сегодня».
        return builder(Store(), self.m.datetime(2026, 8, 12, 9, 0, tzinfo=self.m.timezone.utc))

    def test_ручной_резерв_без_asof_берёт_день_чтения(self):
        # fetch/manual.py asof не ставит вовсе, а прежний запасной вариант брал
        # БЛИЖАЙШУЮ ОТСЕЧКУ — то есть будущее, которое фронт печатать отказывается.
        tile = self.tile({"status": "ok", "fetched_at": "2026-08-12T07:56:42Z",
                          "items": [{"ticker": "YDEX", "ex_date": "2026-09-21",
                                     "yield_pct": 2.74, "amount_bn": 43.6,
                                     "index_drag_pct": 0.189, "weight_pct": 6.89}]})
        self.assertEqual(tile["asof"], "2026-08-12")
        self.assertIn("YDEX", tile["headline"], "числа тайла при этом не меняются")

    def test_asof_из_meta_уважается(self):
        tile = self.tile({"status": "ok", "asof": "2026-08-11",
                          "fetched_at": "2026-08-12T07:56:42Z",
                          "items": [{"ticker": "T", "ex_date": "2026-10-12",
                                     "yield_pct": 1.66, "amount_bn": 12.6,
                                     "index_drag_pct": 0.086, "weight_pct": 5.17}]})
        self.assertEqual(tile["asof"], "2026-08-11")


if __name__ == "__main__":
    unittest.main()
