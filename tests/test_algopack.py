"""Подписка ALGOPACK: ключ подключается сам, а его отсутствие — законный режим.

Платный шлюз добавляет ровно одно: открытые позиции по группам клиентов без
четырнадцатидневного запрета. Опасностей здесь три, и все проверяются ниже:

1. **ключ уехал не туда.** `Authorization: Bearer` положен ТОЛЬКО хосту подписки;
   тот же заголовок на iss.moex.com или cbr.ru — это утечка платного ключа в
   сторонний лог;
2. **ключ протух, а ряд умер.** Истёкшая подписка не должна превращать futoi в
   пустоту: окно обязано переспрашиваться по бесплатному пути, пусть и с задержкой;
3. **записка врёт про свежесть.** `states.py` подписывает сигналу возраст по данным,
   и meta с «задержка 14 дней» при живой подписке (или наоборот) искажает то, что
   читатель видит на панели.
"""

import os
import unittest
from datetime import datetime, timezone
from unittest import mock

from tests import need

TOKEN = "тест-ключ-подписки"
# Фиксированный «сейчас»: правило набора №1 — никаких «сегодня минус N».
NOW = datetime(2026, 8, 12, 9, 0, 0, tzinfo=timezone.utc)


class AuthHeaderCase(unittest.TestCase):
    def setUp(self):
        self.http = need(self, "pipeline.lib.http", "auth_token", "HOST_AUTH_ENV")
        self.prev = os.environ.get("MOEX_ALGOPACK_TOKEN")
        self.addCleanup(self._restore)

    def _restore(self):
        if self.prev is None:
            os.environ.pop("MOEX_ALGOPACK_TOKEN", None)
        else:
            os.environ["MOEX_ALGOPACK_TOKEN"] = self.prev

    def test_ключ_только_для_своего_хоста(self):
        os.environ["MOEX_ALGOPACK_TOKEN"] = TOKEN
        self.assertEqual(self.http.auth_token("apim.moex.com"), TOKEN)
        for host in ("iss.moex.com", "www.cbr.ru", "www.consultant.ru", "t.me",
                     "apim.moex.com.evil.example"):
            self.assertIsNone(self.http.auth_token(host), f"ключ утёк на {host}")

    def test_пустая_переменная_это_отсутствие_подписки(self):
        for value in ("", "   "):
            os.environ["MOEX_ALGOPACK_TOKEN"] = value
            self.assertIsNone(self.http.auth_token("apim.moex.com"))
        os.environ.pop("MOEX_ALGOPACK_TOKEN", None)
        self.assertIsNone(self.http.auth_token("apim.moex.com"))

    def test_заголовок_доезжает_до_запроса(self):
        os.environ["MOEX_ALGOPACK_TOKEN"] = TOKEN
        seen = {}

        class FakeResp:
            headers = {}

            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_open(req, **_kw):
            seen["url"] = req.full_url
            seen["auth"] = req.get_header("Authorization")
            return FakeResp()

        with mock.patch("urllib.request.urlopen", side_effect=fake_open):
            self.http.get_bytes("https://apim.moex.com/iss/x.json")
            self.assertEqual(seen["auth"], "Bearer " + TOKEN)
            self.http.get_bytes("https://iss.moex.com/iss/x.json")
            self.assertIsNone(seen["auth"], "бесплатный ISS получил платный ключ")


class FutoiRoutingCase(unittest.TestCase):
    def setUp(self):
        self.iss = need(self, "pipeline.fetch.iss", "_futoi_window", "algopack_ready",
                        "ALGOPACK", "ISS")
        self.prev = os.environ.get("MOEX_ALGOPACK_TOKEN")
        self.addCleanup(self._restore)

    def _restore(self):
        if self.prev is None:
            os.environ.pop("MOEX_ALGOPACK_TOKEN", None)
        else:
            os.environ["MOEX_ALGOPACK_TOKEN"] = self.prev

    def rows(self):
        return {"futoi": {"columns": ["tradedate", "clgroup", "pos"],
                          "data": [["2026-08-11", "FIZ", 100]]}}

    def test_без_ключа_идём_в_бесплатный_iss(self):
        os.environ.pop("MOEX_ALGOPACK_TOKEN", None)
        self.assertFalse(self.iss.algopack_ready())
        with mock.patch.object(self.iss.http, "get_json", return_value=self.rows()) as g:
            _rows, _cols, url, bad = self.iss._futoi_window("MX", "2026-08-01", "2026-08-11")
        self.assertEqual(bad, 0)
        self.assertIn(self.iss.ISS, url)
        self.assertEqual(g.call_count, 1)

    def test_с_ключом_идём_в_платный_шлюз(self):
        os.environ["MOEX_ALGOPACK_TOKEN"] = TOKEN
        self.assertTrue(self.iss.algopack_ready())
        with mock.patch.object(self.iss.http, "get_json", return_value=self.rows()):
            _rows, _cols, url, bad = self.iss._futoi_window("MX", "2026-08-01", "2026-08-11")
        self.assertEqual(bad, 0)
        self.assertIn("apim.moex.com", url)

    def test_протухший_ключ_откатывает_на_бесплатный(self):
        # Мутация «убрать откат»: истёкшая подписка обнуляет ряд, а тайл при этом
        # зелёный — SLA меряет свежесть выкачки, а не наличие данных.
        os.environ["MOEX_ALGOPACK_TOKEN"] = TOKEN
        calls = []

        def flaky(url, **_kw):
            calls.append(url)
            if "apim.moex.com" in url:
                raise self.iss.FetchError("HTTP 401 на " + url, url=url, status=401)
            return self.rows()

        with mock.patch.object(self.iss.http, "get_json", side_effect=flaky):
            rows, _cols, url, bad = self.iss._futoi_window("MX", "2026-08-01", "2026-08-11")
        self.assertEqual(bad, 0, "откат должен считаться успехом, а не отказом окна")
        self.assertEqual(len(rows), 1)
        self.assertIn(self.iss.ISS, url)
        self.assertEqual(len(calls), 2)

    def test_отказ_обоих_путей_это_отказ_окна(self):
        os.environ["MOEX_ALGOPACK_TOKEN"] = TOKEN
        boom = self.iss.FetchError("сеть легла")
        with mock.patch.object(self.iss.http, "get_json", side_effect=boom):
            rows, _cols, _url, bad = self.iss._futoi_window("MX", "2026-08-01", "2026-08-11")
        self.assertEqual(rows, [])
        self.assertEqual(bad, 1)


class LiveQuotesCase(unittest.TestCase):
    """Витрина котировок: T-Invest вместо ISS и обязательный откат на бесплатный.

    Бесплатный ISS накрывает ход торгов ИНСТРУМЕНТАМИ задержкой ровно 15 минут —
    замерено 12.08.2026 в 11:10 МСК: по юаню UPDATETIME=10:55 при SYSTIME=11:10.
    T-Invest отдаёт ту же бумагу текущей секундой и весь набор одним запросом.
    Но витрина не имеет права зависеть от платного API: отказ обязан уводить на ISS,
    а не оставлять панель без цен.
    """

    def setUp(self):
        self.run = need(self, "pipeline.run", "fetch_live_quotes")
        self.tv = need(self, "pipeline.fetch.tinvest", "live_quotes", "LIVE_UIDS")
        self.tmp_env = os.environ.get("MOEX_ALGOPACK_TOKEN")
        self.journal = self.run.Journal()
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prev = os.environ.get("STATE_DIR")
        os.environ["STATE_DIR"] = self.tmp.name
        self.addCleanup(self._restore)

    def _restore(self):
        if self.prev is None:
            os.environ.pop("STATE_DIR", None)
        else:
            os.environ["STATE_DIR"] = self.prev

    def triple(self, source):
        return [("live_imoex", {"2026-08-12": 2317.13},
                 {"source": source, "asof": "2026-08-12", "delay_min": 0,
                  "intraday": True, "fetched_at": "2026-08-12T08:27:48Z"})]

    def test_с_токеном_идём_в_tinvest(self):
        with mock.patch.object(self.tv, "ready", return_value=True), \
             mock.patch.object(self.tv, "live_quotes",
                               return_value=self.triple("tinvest")) as called, \
             mock.patch("pipeline.fetch.iss.intraday_quote") as never:
            got = self.run.fetch_live_quotes(self.journal)
        called.assert_called_once()
        never.assert_not_called()
        self.assertEqual(got, {"live_imoex": "2026-08-12"})

    def test_без_токена_остаётся_бесплатный_iss(self):
        with mock.patch.object(self.tv, "ready", return_value=False), \
             mock.patch.object(self.tv, "live_quotes") as never, \
             mock.patch("pipeline.fetch.iss.intraday_quote",
                        return_value=self.triple("iss")) as fallback:
            self.run.fetch_live_quotes(self.journal)
        never.assert_not_called()
        fallback.assert_called_once()

    def test_отказ_платного_api_не_оставляет_панель_без_цен(self):
        # мутация: убрать откат -> при любом сбое T-Invest витрина показывает
        # вчерашнее закрытие весь торговый день и молчит об этом.
        with mock.patch.object(self.tv, "ready", return_value=True), \
             mock.patch.object(self.tv, "live_quotes", side_effect=RuntimeError("лёг")), \
             mock.patch("pipeline.fetch.iss.intraday_quote",
                        return_value=self.triple("iss")) as fallback:
            got = self.run.fetch_live_quotes(self.journal)
        fallback.assert_called_once()
        self.assertEqual(got, {"live_imoex": "2026-08-12"})

    def test_нулевая_цена_не_становится_котировкой(self):
        # По инструменту без сделок T-Invest отдаёт ноль. Это не цена.
        data = {"lastPrices": [
            {"instrumentUid": "u1", "price": {"units": "0", "nano": 0}, "time": "2026-08-12T08:00:00Z"},
            {"instrumentUid": "u2", "price": {"units": "12", "nano": 264000000}, "time": "2026-08-12T08:27:31Z"}]}
        with mock.patch.object(self.tv, "call", return_value=data):
            got = self.tv.last_prices(["u1", "u2"])
        self.assertEqual(list(got), ["u2"])
        self.assertEqual(got["u2"][0], 12.264)

    def test_время_показывается_московское(self):
        self.assertEqual(self.tv._msk_time("2026-08-12T08:27:31Z"), "11:27:31")
        self.assertIsNone(self.tv._msk_time("не время"))

    def store_stub(self, daily_secid=None, cached=None):
        class S:
            @staticmethod
            def load_series(sid):
                if sid == "brent_moex":
                    return {"meta": {"secid": daily_secid}} if daily_secid else None
                if sid == "live_brent_moex":
                    return {"meta": cached} if cached else None
                return None
        return S

    def test_контракт_берётся_из_суточного_ряда_без_запросов(self):
        # Передний контракт уже разрешил суточный прогон, uid лежит с прошлого
        # раза — обычный такт не имеет права ходить за справочником фьючерсов.
        st = self.store_stub("BRU6", {"secid": "BRU6", "uid": "u-bru6",
                                      "secid_since": "2026-07-31"})
        with mock.patch.object(self.tv, "futures_uid") as never:
            uid, secid, since = self.tv.front_futures(st, today="2026-08-12")
        never.assert_not_called()
        self.assertEqual((uid, secid, since), ("u-bru6", "BRU6", "2026-07-31"))

    def test_перекат_разрешается_один_раз_и_датируется(self):
        st = self.store_stub("BRV6", {"secid": "BRU6", "uid": "u-bru6"})
        with mock.patch.object(self.tv, "futures_uid", return_value="u-brv6") as once:
            uid, secid, since = self.tv.front_futures(st, today="2026-08-12")
        once.assert_called_once_with("BRV6")
        self.assertEqual((uid, secid, since), ("u-brv6", "BRV6", "2026-08-12"))

    def test_первое_включение_не_считается_перекатом(self):
        # мутация: ставить дату всегда -> в день подключения панель на сутки
        # замолкает об изменении нефти, хотя контракт не менялся.
        st = self.store_stub("BRU6", cached=None)
        with mock.patch.object(self.tv, "futures_uid", return_value="u-bru6"):
            _uid, _secid, since = self.tv.front_futures(st, today="2026-08-12")
        self.assertIsNone(since)

    def test_без_суточного_ряда_фьючерс_не_подключается(self):
        st = self.store_stub(daily_secid=None)
        with mock.patch.object(self.tv, "futures_uid") as never:
            self.assertEqual(self.tv.front_futures(st, today="2026-08-12"),
                             (None, None, None))
        never.assert_not_called()

    def test_изменение_за_день_гасится_на_перекате(self):
        # Живая цена нового контракта против закрытия старого — это контанго,
        # а не движение нефти.
        self.run.store.upsert_points("brent_moex", {"2026-08-11": 84.0}, {})
        self.run.store.upsert_points(
            "live_brent_moex", {"2026-08-12": 89.3},
            {"delay_min": 0, "secid": "BRV6", "secid_since": "2026-08-12"})
        q = self.run._quotes(NOW)["brent_moex"]
        self.assertIsNone(q["chg_pct"], "на перекате показано выдуманное движение")
        self.assertEqual(q["contract"], "BRV6")

    def test_вне_переката_изменение_считается(self):
        self.run.store.upsert_points("brent_moex", {"2026-08-11": 84.0}, {})
        self.run.store.upsert_points(
            "live_brent_moex", {"2026-08-12": 89.3},
            {"delay_min": 0, "secid": "BRU6", "secid_since": "2026-07-31"})
        q = self.run._quotes(NOW)["brent_moex"]
        self.assertAlmostEqual(q["chg_pct"], 6.31, places=2)

    def test_известны_все_инструменты_витрины(self):
        # Витрина показывает шесть строк, пять из них живые (шестая — фьючерс BR
        # из history). Пропущенный uid означал бы тихо застывшую цену.
        self.assertEqual(sorted(self.tv.LIVE_UIDS),
                         ["live_cny_tom", "live_gld_tom", "live_imoex", "live_rgbi",
                          "live_rvi"])


class FutoiNoteCase(unittest.TestCase):
    """Записка о задержке обязана следовать режиму, а не быть константой."""

    def setUp(self):
        self.iss = need(self, "pipeline.fetch.iss", "futoi")
        self.prev = os.environ.get("MOEX_ALGOPACK_TOKEN")
        self.addCleanup(self._restore)

    def _restore(self):
        if self.prev is None:
            os.environ.pop("MOEX_ALGOPACK_TOKEN", None)
        else:
            os.environ["MOEX_ALGOPACK_TOKEN"] = self.prev

    def run_futoi(self):
        payload = {"futoi": {"columns": ["tradedate", "clgroup", "pos", "seqnum"],
                             "data": [["2026-08-11", "FIZ", 100, 1]]}}
        with mock.patch.object(self.iss.http, "get_json", return_value=payload), \
             mock.patch.object(self.iss.store, "last_date", return_value="2026-08-10"):
            return self.iss.futoi(ticker="MX", start="2026-08-10", end="2026-08-11")

    def test_без_подписки_записка_про_задержку(self):
        os.environ.pop("MOEX_ALGOPACK_TOKEN", None)
        out = self.run_futoi()
        meta = out[0][2]
        self.assertIn("14 дней", meta["note"])
        self.assertFalse(meta["algopack"])

    def test_с_подпиской_записка_про_отсутствие_задержки(self):
        os.environ["MOEX_ALGOPACK_TOKEN"] = TOKEN
        out = self.run_futoi()
        meta = out[0][2]
        self.assertIn("без задержки", meta["note"])
        self.assertTrue(meta["algopack"])


if __name__ == "__main__":
    unittest.main()
