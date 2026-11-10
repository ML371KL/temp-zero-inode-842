"""Дата живой котировки T-Invest — день СДЕЛКИ, а не день опроса (аудит 10.11.2026, §3.8).

Ошибка, которую сторожит набор: tinvest.live_quotes датировал точку моментом опроса.
Последний такт дня в 00:00 МСК записывал вчерашнюю цену сегодняшним числом, run._quotes
считал изменение «к вчерашнему закрытию» от неё самой, и с полуночи до 10:00 МСК и все
выходные у индекса, RGBI, RVI, юаня и золота на витрине стояло «0,00 %».

Часы заморожены параметром now= (никаких «сегодня минус N»), сеть подменена на уровне
tinvest.call — разбор цены и времени работает настоящий.
"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

from tests import need

UID = "4821c9aa-5e2a-4e60-8ae0-5b8b3d1d2f8e"


def _answer(price_units, nano, when):
    return {"lastPrices": [{"instrumentUid": UID,
                            "price": {"units": str(price_units), "nano": nano},
                            "time": when}]}


class TestTradeDay(unittest.TestCase):
    def setUp(self):
        self.tv = need(self, "pipeline.fetch.tinvest", "live_quotes", "last_prices")

    def quote(self, now, when, price=(2470, 70000000)):
        with mock.patch.object(self.tv, "call", return_value=_answer(price[0], price[1], when)):
            return self.tv.live_quotes({"live_imoex": UID}, now=now)

    def test_полночь_мск_датируется_днём_сделки(self):
        """Мутация: вернуть дату опроса → точка «2026-11-10» с ценой 09.11, тест краснеет."""
        now = datetime(2026, 11, 9, 21, 0, 7, tzinfo=timezone.utc)      # 00:00:07 МСК 10.11
        out = self.quote(now, "2026-11-09T20:49:59.123Z")               # 23:49:59 МСК 09.11
        (sid, pts, meta), = out
        self.assertEqual(sid, "live_imoex")
        self.assertEqual(list(pts), ["2026-11-09"])
        self.assertEqual(meta["asof"], "2026-11-09")
        self.assertEqual(meta["updatetime"], "23:49:59")
        self.assertEqual(meta["fetched_at"], "2026-11-09T21:00:07Z")

    def test_выходные_датируются_пятницей(self):
        now = datetime(2026, 11, 7, 9, 30, tzinfo=timezone.utc)         # суббота, 12:30 МСК
        (sid, pts, meta), = self.quote(now, "2026-11-06T15:49:58Z")     # пятница, 18:49 МСК
        self.assertEqual(list(pts), ["2026-11-06"])

    def test_вечерняя_сессия_после_полуночи_utc(self):
        # 23:30 МСК — это уже следующий день по UTC минус три часа: день сделки по Москве.
        now = datetime(2026, 11, 9, 20, 40, tzinfo=timezone.utc)
        (sid, pts, meta), = self.quote(now, "2026-11-09T20:30:00Z")
        self.assertEqual(list(pts), ["2026-11-09"])

    def test_без_времени_сделки_точки_нет(self):
        # Подставить день опроса — та самая ошибка; без времени цену не датируем вовсе.
        now = datetime(2026, 11, 9, 21, 0, tzinfo=timezone.utc)
        with mock.patch.object(self.tv, "call", return_value=_answer(2470, 0, "")):
            with self.assertRaises(Exception):
                self.tv.live_quotes({"live_imoex": UID}, now=now)

    def test_время_из_будущего_отбрасывается(self):
        now = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
        with mock.patch.object(self.tv, "call",
                               return_value=_answer(2470, 0, "2026-11-12T10:00:00Z")):
            with self.assertRaises(Exception):
                self.tv.live_quotes({"live_imoex": UID}, now=now)


class TestNightChange(unittest.TestCase):
    """Сквозная проверка строки котировок ночью: изменение — за последнюю сессию."""

    def setUp(self):
        self.tv = need(self, "pipeline.fetch.tinvest", "live_quotes")
        self.run = need(self, "pipeline.run", "_quotes")
        self.store = need(self, "pipeline.lib.store", "upsert_points")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        prev = os.environ.get("STATE_DIR")
        os.environ["STATE_DIR"] = self.tmp.name
        self.addCleanup(lambda: os.environ.__setitem__("STATE_DIR", prev) if prev is not None
                        else os.environ.pop("STATE_DIR", None))
        self.store.upsert_points("imoex", {"2026-11-05": 2458.46, "2026-11-06": 2476.70,
                                           "2026-11-09": 2470.07})

    def tick(self, now, when, price):
        with mock.patch.object(self.tv, "call", return_value=_answer(price[0], price[1], when)):
            for sid, pts, meta in self.tv.live_quotes({"live_imoex": UID}, now=now):
                self.store.upsert_points(sid, pts, meta)

    def test_в_полночь_строка_показывает_изменение_сессии(self):
        """Мутация: дата опроса → живая точка 10.11 = цена 09.11, база — закрытие 09.11,
        изменение 0,00 %; с днём сделки база — закрытие 06.11, изменение −0,27 %."""
        now = datetime(2026, 11, 9, 21, 0, 7, tzinfo=timezone.utc)
        self.tick(now, "2026-11-09T20:50:02Z", (2470, 70000000))
        row = self.run._quotes(now)["imoex"]
        self.assertEqual(row["asof"], "2026-11-09")
        self.assertAlmostEqual(row["chg_pct"], round((2470.07 / 2476.70 - 1) * 100, 2))
        self.assertNotEqual(row["chg_pct"], 0.0)

    def test_в_субботу_строка_показывает_пятницу(self):
        self.store.upsert_points("imoex", {"2026-11-09": None})
        now = datetime(2026, 11, 7, 9, 30, tzinfo=timezone.utc)
        self.tick(now, "2026-11-06T15:49:58Z", (2476, 700000000))
        row = self.run._quotes(now)["imoex"]
        self.assertEqual(row["asof"], "2026-11-06")
        self.assertAlmostEqual(row["chg_pct"], round((2476.70 / 2458.46 - 1) * 100, 2))


class TestPhantomCleanup(unittest.TestCase):
    """ops/clean_live_phantoms.py: что считается фантомом дня опроса."""

    def setUp(self):
        import importlib.util
        from pathlib import Path
        path = Path(__file__).resolve().parents[1] / "ops" / "clean_live_phantoms.py"
        spec = importlib.util.spec_from_file_location("clean_live_phantoms", path)
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)

    def test_суббота_праздник_и_полночная_копия(self):
        closes = {"2026-11-03": 2446.64, "2026-11-05": 2458.46, "2026-11-06": 2476.70,
                  "2026-11-09": 2470.07}
        live = {"2026-11-03": 2446.64, "2026-11-04": 2442.10, "2026-11-05": 2458.46,
                "2026-11-06": 2476.70, "2026-11-07": 2476.70, "2026-11-09": 2470.07,
                "2026-11-10": 2470.07}
        self.assertEqual(self.mod.phantoms(live, closes),
                         ["2026-11-04", "2026-11-07", "2026-11-10"])

    def test_живая_цена_сегодняшней_сессии_не_фантом(self):
        # Сессия идёт, закрытия ещё нет, цена другая — это настоящая точка.
        closes = {"2026-11-09": 2470.07}
        live = {"2026-11-09": 2470.07, "2026-11-10": 2481.30}
        self.assertEqual(self.mod.phantoms(live, closes), [])


if __name__ == "__main__":
    unittest.main()
