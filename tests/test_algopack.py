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
from unittest import mock

from tests import need

TOKEN = "тест-ключ-подписки"


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
