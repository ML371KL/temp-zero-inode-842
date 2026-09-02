"""Фетчер HI2 (ALGOPACK datashop): агрегат дня руками, окно ретро, отказы статусом.

Три опасности, ради которых тесты и написаны:

1. **агрегат считается не по тем бумагам.** Точка дня — среднее по бумагам, у
   которых ОБЕ стороны метрики определены; бумага с одним hhi_volume не имеет
   права попасть в нетто-поток, а строка с чужим tradedate — в день вовсе;
2. **ретро без дна.** История у шлюза с 2020 — 1 500 дней × 2 страницы; при
   пустом сторе тянем ровно 260 торговых дней, не больше, и от старых к новым,
   чтобы прерванная загрузка продолжалась, а не оставляла дыру;
3. **нет данных ≠ падение.** Без ключа, при 401 и при мёртвой сети фетчер отдаёт
   статус в meta, а не исключение: у этого ряда бесплатного дублёра нет, и красная
   строка о некупленной подписке каждый день — это шум, а не сигнал.

Сеть подменяется на нижнем слое (http.get_bytes), даты фиксированы литералами.
"""

import os
import unittest
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from unittest import mock

from tests import fake_http, fixture_json, need

TOKEN = "тест-ключ-подписки"
NOW = datetime(2026, 9, 1, 16, 0, 0, tzinfo=timezone.utc)
END = "2026-09-01"
EMPTY_PAGE = {"data": {"columns": ["tradedate", "tradetime", "secid", "metric", "value",
                                   "reference", "SYSTIME"], "data": []},
              "data.cursor": {"columns": ["INDEX", "TOTAL", "PAGESIZE"], "data": [[0, 0, 1000]]}}


class Hi2Case(unittest.TestCase):
    def setUp(self):
        self.ap = need(self, "pipeline.fetch.algopack", "hi2_market", "fetch_day",
                       "aggregate", "series_ids", "RETRO_TRADING_DAYS")
        self.http = need(self, "pipeline.lib.http", "get_bytes")
        self.store = need(self, "pipeline.lib.store", "save_series", "last_date")
        self.fixture = fixture_json("algopack_hi2.json")
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prev_state = os.environ.get("STATE_DIR")
        os.environ["STATE_DIR"] = self.tmp.name
        self.prev_key = os.environ.get("MOEX_ALGOPACK_TOKEN")
        os.environ["MOEX_ALGOPACK_TOKEN"] = TOKEN
        self.addCleanup(self._restore)

    def _restore(self):
        for name, prev in (("STATE_DIR", self.prev_state),
                           ("MOEX_ALGOPACK_TOKEN", self.prev_key)):
            if prev is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = prev

    def calls(self):
        """Обёртка fake_http, запоминающая URL — по ним проверяется окно."""
        seen = []

        def _get(mapping, default=None):
            inner = fake_http(mapping, default)

            def _get_bytes(url, **kw):
                seen.append(url)
                return inner(url, **kw)
            return _get_bytes
        return seen, _get

    @staticmethod
    def dates_of(urls):
        out = []
        for u in urls:
            q = u.split("?", 1)[1]
            params = dict(p.split("=", 1) for p in q.split("&"))
            out.append(params["date"])
        return out

    # ------------------------------------------------------------ агрегат
    def test_агрегат_дня_руками(self):
        """AAA: nf 915−554=361, bs 1270−518=752; BBB: 210−70=140, 208−190=18.
        CCC без netflow — только в hhi_volume; DDD с чужой датой — никуда.
        nf = (361+140)/2 = 250.5; bs = (752+18)/2 = 385; n = 2;
        hhi_volume_mean = (457+187+207)/3 = 283.667."""
        with mock.patch.object(self.http, "get_bytes", fake_http({"date=2026-08-12": self.fixture})):
            by_ticker, pages = self.ap.fetch_day("2026-08-12")
        self.assertEqual(pages, 1)
        self.assertEqual(sorted(by_ticker), ["AAA", "BBB", "CCC"])  # DDD отброшен по дате
        agg = self.ap.aggregate(by_ticker)
        self.assertEqual(agg["nf"], 250.5)
        self.assertEqual(agg["bs"], 385.0)
        self.assertEqual(agg["n"], 2.0)
        self.assertEqual(agg["hhi_volume_mean"], 283.667)

    def test_без_нетто_потока_точки_нет(self):
        # Мутация «усреднять по всем, подставляя ноль» дала бы точку из одной
        # hhi_volume — день без нетто-потока не имеет права стать числом.
        self.assertIsNone(self.ap.aggregate({"CCC": {"hhi_volume": 207.0}}))
        self.assertIsNone(self.ap.aggregate({}))

    def test_страницы_склеиваются_в_один_день(self):
        rows = self.fixture["data"]["data"]
        cols = self.fixture["data"]["columns"]
        cursor = {"columns": ["INDEX", "TOTAL", "PAGESIZE"]}
        page1 = {"data": {"columns": cols, "data": rows[:13]},
                 "data.cursor": dict(cursor, data=[[0, 26, 13]])}
        page2 = {"data": {"columns": cols, "data": rows[13:]},
                 "data.cursor": dict(cursor, data=[[13, 26, 13]])}
        with mock.patch.object(self.ap, "PAGE", 13), \
             mock.patch.object(self.http, "get_bytes",
                               fake_http({"start=13": page2, "date=2026-08-12": page1})):
            by_ticker, pages = self.ap.fetch_day("2026-08-12")
        self.assertEqual(pages, 2)
        self.assertEqual(self.ap.aggregate(by_ticker)["nf"], 250.5)

    # --------------------------------------------------------------- окно
    def test_ретро_при_пустом_сторе_ровно_260_торговых_дней_от_старых_к_новым(self):
        seen, get = self.calls()
        with mock.patch.object(self.http, "get_bytes", get({}, default=EMPTY_PAGE)):
            out = self.ap.hi2_market(end=END)
        days = self.dates_of(seen)
        self.assertEqual(len(days), self.ap.RETRO_TRADING_DAYS)
        self.assertEqual(len(days), 260)
        self.assertEqual(days, sorted(days), "ретро обязан идти от старых дней к новым")
        self.assertEqual(days[-1], END)
        self.assertLessEqual("2025-08-01", days[0])   # год торговых дней ≈ 13 месяцев
        # Ключ живой, отказов нет, а строк за 260 дней ни одной — это «стучимся не
        # туда», и молчать со status=ok нельзя.
        metas = {sid: m for sid, _pts, m in out}
        self.assertEqual(sorted(metas), sorted(self.ap.series_ids()))
        self.assertEqual(metas["hi2_market_nf"]["status"], "error")
        self.assertIn("ни одной строки", metas["hi2_market_nf"]["note"])

    def test_инкремент_с_последней_даты_минус_запас(self):
        for sid in self.ap.series_ids():
            self.store.save_series(sid, {"id": sid, "points": {"2026-08-25": 1.0},
                                         "meta": {"status": "ok"}})
        seen, get = self.calls()
        with mock.patch.object(self.http, "get_bytes",
                               get({"date=2026-08-31": self.fixture}, default=EMPTY_PAGE)):
            out = self.ap.hi2_market(end=END)
        days = self.dates_of(seen)
        self.assertEqual(days[0], "2026-08-20")   # 25.08 − RETRO_DAYS(5)
        self.assertEqual(days[-1], END)
        self.assertLess(len(days), 15, "инкремент не должен перечитывать историю")
        points = {sid: p for sid, p, _m in out}
        # Фикстура датирована 12.08, а запросили 31.08: строки с чужим tradedate
        # отбрасываются, и точки за 31.08 быть не должно.
        self.assertEqual(points["hi2_market_nf"], {})
        self.assertEqual({sid: m["status"] for sid, _p, m in out},
                         {sid: "ok" for sid in self.ap.series_ids()})

    def test_единицы_подрядов(self):
        seen, get = self.calls()
        with mock.patch.object(self.http, "get_bytes", get({}, default=EMPTY_PAGE)):
            out = self.ap.hi2_market(start="2026-08-31", end=END)
        units = {sid: m["unit"] for sid, _p, m in out}
        self.assertEqual(units["hi2_market_n"], "count")
        self.assertEqual(units["hi2_market_nf"], "hhi_pts")

    # ------------------------------------------------------------- отказы
    def test_без_ключа_статус_а_не_исключение_и_ни_одного_запроса(self):
        os.environ.pop("MOEX_ALGOPACK_TOKEN", None)
        with mock.patch.object(self.http, "get_bytes", fake_http({})):  # любой запрос = AssertionError
            out = self.ap.hi2_market(end=END)
        self.assertEqual(len(out), 4)
        for sid, pts, meta in out:
            self.assertEqual(pts, {})
            self.assertIn(meta["status"], ("stale", "missing"))
            self.assertIn("ключа ALGOPACK нет", meta["note"])
            self.assertFalse(meta["algopack"])

    def test_отказ_ключа_останавливает_загрузку_и_говорит_про_подписку(self):
        FetchError = self.http.FetchError
        calls = []

        def denied(url, **_kw):
            calls.append(url)
            raise FetchError("HTTP 401 на " + url, url=url, status=401)

        with mock.patch.object(self.http, "get_bytes", denied):
            out = self.ap.hi2_market(end=END)
        # Мутация «продолжать после 401»: 260 бесполезных запросов к платному хосту.
        self.assertEqual(len(calls), 1)
        meta = out[0][2]
        self.assertEqual(meta["status"], "error")
        self.assertIn("проверьте подписку", meta["note"])

    def test_сетевой_отказ_не_винит_подписку_и_останавливается(self):
        FetchError = self.http.FetchError
        calls = []

        def dead(url, **_kw):
            calls.append(url)
            raise FetchError("сеть легла", url=url)

        with mock.patch.object(self.http, "get_bytes", dead):
            out = self.ap.hi2_market(end=END)
        self.assertEqual(len(calls), self.ap.MAX_NET_FAILURES)
        meta = out[0][2]
        self.assertEqual(meta["status"], "error")
        self.assertIn("подписка ни при чём", meta["note"])
        self.assertNotIn("проверьте подписку", meta["note"])

    def test_ключ_только_платному_хосту(self):
        # Тот же инвариант, что у futoi (test_algopack): заголовок Authorization
        # обязан уехать ТОЛЬКО на apim.moex.com.
        seen = {}

        class FakeResp:
            headers = {}

            def read(self):
                return b'{"data": {"columns": [], "data": []}}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_open(req, **_kw):
            seen[req.full_url] = req.get_header("Authorization")
            return FakeResp()

        with mock.patch("urllib.request.urlopen", side_effect=fake_open):
            self.ap.fetch_day("2026-08-12")
        self.assertEqual(len(seen), 1)
        url, auth = next(iter(seen.items()))
        self.assertIn("apim.moex.com/iss/datashop/algopack/eq/hi2.json", url)
        self.assertIn("date=2026-08-12", url)
        self.assertEqual(auth, "Bearer " + TOKEN)


class RegistryCase(unittest.TestCase):
    """Ряд HI2 и теневые ряды в реестре: режимы, нормы возраста, отсутствие фетчера."""

    def setUp(self):
        self.reg = need(self, "pipeline.lib.registry", "SERIES", "MODES", "data_age_norm",
                        "fetchable", "SHADOW_SIGNALS")

    def test_hi2_в_суточном_режиме_с_подключами(self):
        spec = self.reg.SERIES["hi2_market"]
        self.assertEqual(spec["fetcher"], "algopack.hi2_market")
        self.assertEqual(spec["subkeys"], ["nf", "bs", "n", "hhi_volume_mean"])
        self.assertIn("hi2_market", self.reg.MODES["daily"])
        self.assertNotIn("hi2_market", self.reg.MODES["intraday"])
        self.assertEqual(self.reg.data_age_norm("hi2_market_nf"), 9)  # дневная норма по префиксу

    def test_теневые_ряды_без_фетчера_и_без_нормы_возраста(self):
        for sid in self.reg.SHADOW_SIGNALS:
            spec = self.reg.SERIES[f"shadow_{sid}"]
            self.assertIsNone(spec["fetcher"])
            self.assertEqual(spec["role"], "shadow")
            self.assertEqual(spec["cadence"], "derived")
            self.assertIsNone(self.reg.data_age_norm(f"shadow_{sid}"),
                              "возраст теневого ряда — не дефект источника")
            for mode, ids in self.reg.MODES.items():
                self.assertNotIn(f"shadow_{sid}", ids, f"тень попала в режим {mode}")
        # То, что можно опрашивать, — только ряды с фетчером.
        self.assertNotIn("shadow_repricing", self.reg.fetchable())
        self.assertIn("hi2_market", self.reg.fetchable())


if __name__ == "__main__":
    unittest.main()
