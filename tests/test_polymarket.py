"""Polymarket: смена контракта не переписывает прошлое, ряд — только суточные срезы.

Аудит 20.11.2026. Около 2–3 декабря 2026, когда до расчёта декабрьского контракта
останется меньше 30 суток, выбор сам переедет на мартовский 2027 (цена ~0,285 против
~0,095). Прежний код при этом доливал в ряд всю историю нового контракта и
переписывал прошлое с 19.08: уровень прыгал задним числом, изменения «за неделю» и
«за месяц» у стыка становились ложными. Вторая находка: последняя точка дня была
ценой в момент прогона, а назавтра переписывалась суточным срезом (19.11.2026:
записано 0,095, срез — 0,115).

Фикстура tests/fixtures/polymarket_series_2026-11-20.json — живые ответы API
20.11.2026 05:31 UTC: серия контрактов и истории цен декабрьского и мартовского.
Часы заморожены (`now=`), в сеть не ходим: подменяется `polymarket.get_json`.
"""

import copy
import os
import unittest
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from unittest import mock

from tests import fixture_json, need

UTC = timezone.utc
DEC = "russia-x-ukraine-ceasefire-agreement-by-december-31-2026"
MAR = "russia-x-ukraine-ceasefire-agreement-by-march-31-2027"


def _stamp(day, hour=0, minute=0):
    y, m, d = (int(x) for x in day.split("-"))
    return int(datetime(y, m, d, hour, minute, tzinfo=UTC).timestamp())


class PolyCase(unittest.TestCase):
    def setUp(self):
        self.pm = need(self, "pipeline.fetch.polymarket", "ceasefire", "_history",
                       "_pick_primary")
        self.store = need(self, "pipeline.lib.store", "upsert_points", "load_series")
        self.fx = copy.deepcopy(fixture_json("polymarket_series_2026-11-20.json"))
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        prev = os.environ.get("STATE_DIR")
        os.environ["STATE_DIR"] = self.tmp.name
        self.addCleanup(lambda: os.environ.pop("STATE_DIR", None) if prev is None
                        else os.environ.__setitem__("STATE_DIR", prev))
        self.tokens = {}
        for market in self.fx["events"][0]["markets"]:
            for name in self.fx["history"]:
                if market["slug"].endswith(name):
                    self.tokens[__import__("json").loads(market["clobTokenIds"])[0]] = name
        patcher = mock.patch.object(self.pm, "get_json", side_effect=self.respond)
        patcher.start()
        self.addCleanup(patcher.stop)

    def respond(self, url, **_kw):
        if "/events?slug=russia-x-ukraine-ceasefire-agreement-by" in url:
            return self.fx["events"]
        if "/events?slug=" in url:
            return []                                   # старая серия: рынков нет
        if "prices-history" in url:
            token = url.split("market=")[1].split("&")[0]
            return self.fx["history"][self.tokens[token]]
        raise AssertionError("незамоканный URL: %s" % url)

    def run_at(self, now):
        """Прогон фетчера и запись в стор — так, как это делает run.py."""
        sid, points, meta = self.pm.ceasefire(now=now)
        saved = self.store.upsert_points(sid, points, dict(meta))
        return points, meta, saved

    def add_rows(self, name, rows):
        self.fx["history"][name]["history"] += [{"t": t, "p": p} for t, p in rows]


class TestDailySnapshots(PolyCase):
    def test_живая_цена_не_становится_точкой_дня(self):
        """История: … (20.11 00:00 — 0,095) и последней строкой цена «сейчас».

        В фикстуре живая строка стоит в 05:28 и равна срезу, поэтому подменяем её
        ценой 0,14: в ряд обязан пойти срез 0,095.
        мутация: писать все строки подряд -> за 20.11 ляжет 0,14, а завтра это
        значение перепишется срезом — вчерашний день изменится задним числом.
        """
        rows = self.fx["history"]["december-31-2026"]["history"]
        gap = rows[-1]["t"] - rows[-2]["t"]
        self.assertTrue(3600 < gap < 23 * 3600, "в фикстуре нет живой строки вне сетки")
        rows[-1]["p"] = 0.14
        points, meta, _ = self.run_at(datetime(2026, 11, 20, 5, 31, tzinfo=UTC))
        self.assertEqual(points["2026-11-20"], 0.095)
        # Срез 19.11 — 0,115; вечерний прогон 19.11 записал в стор «сейчас» 0,095.
        self.assertEqual(points["2026-11-19"], 0.115)
        self.assertNotIn(0.14, points.values())

    def test_день_без_среза_живой_ценой_не_заполняется(self):
        # Суточного среза за сегодня ещё нет (в истории бывают пропуски), есть
        # только живая строка. Точки дня из неё не получается: завтра её пришлось
        # бы переписывать срезом — либо она осталась бы в ряду навсегда.
        # мутация: оставлять живую строку среди срезов -> за 20.11 ляжет 0,14.
        rows = self.fx["history"]["december-31-2026"]["history"]
        del rows[-2]                                  # срез 20.11 00:00
        rows[-1]["p"] = 0.14                          # живая строка 20.11 05:28
        points, meta, _ = self.run_at(datetime(2026, 11, 20, 5, 31, tzinfo=UTC))
        self.assertNotIn("2026-11-20", points)
        self.assertEqual(max(points), "2026-11-19")
        self.assertEqual(meta["price_now"], 0.095)    # цена рынка из ответа серии

    def test_цена_сейчас_лежит_в_meta(self):
        _points, meta, saved = self.run_at(datetime(2026, 11, 20, 5, 31, tzinfo=UTC))
        self.assertEqual(meta["price_now"], 0.095)
        self.assertEqual(meta["price_now_at"], "2026-11-20T05:31:00Z")
        self.assertEqual(saved["meta"]["asof"], "2026-11-20")

    def test_строка_на_суточной_сетке_живой_не_считается(self):
        # Живой строки нет (прогон ровно в момент среза): последняя строка — срез.
        # мутация: всегда отбрасывать последнюю строку -> пропадает срез дня.
        rows = self.fx["history"]["december-31-2026"]["history"]
        rows.pop()
        points, _meta, _ = self.run_at(datetime(2026, 11, 20, 0, 1, tzinfo=UTC))
        self.assertEqual(points["2026-11-20"], 0.095)


class TestContractSwitch(PolyCase):
    def first_run(self):
        return self.run_at(datetime(2026, 11, 20, 5, 31, tzinfo=UTC))

    def test_до_смены_идёт_декабрьский_контракт_целиком(self):
        points, meta, _ = self.first_run()
        self.assertEqual(meta["slug"], DEC)
        self.assertIsNone(meta["contract_since"])
        self.assertEqual(min(points), "2026-05-14")
        self.assertEqual(meta["horizon_days"], 42)

    def test_смена_контракта_прошлое_не_переписывает(self):
        """03.12.2026 до расчёта декабрьского 29 суток — выбор уходит на мартовский.

        У мартовского своя история с 19.08.2026 (23.10 — 0,375 против 0,205 у
        декабрьского). В ряду обязано остаться декабрьское прошлое.
        мутация: доливать всю историю нового контракта -> 23.10 в ряду 0,375.
        """
        self.first_run()
        before = dict(self.store.load_series("polymarket_ceasefire")["points"])
        self.add_rows("march-31-2027", [(_stamp("2026-12-02"), 0.29),
                                        (_stamp("2026-12-03"), 0.30),
                                        (_stamp("2026-12-03", 20, 55), 0.31)])
        points, meta, saved = self.run_at(datetime(2026, 12, 3, 20, 55, tzinfo=UTC))
        self.assertEqual(meta["slug"], MAR)
        self.assertEqual(meta["contract_since"], "2026-12-03")
        # Новый контракт пишется только с даты смены: 02.12 и раньше — нет.
        self.assertEqual(points, {"2026-12-03": 0.30})
        for day, value in before.items():
            self.assertEqual(saved["points"][day], value, day)
        self.assertEqual(saved["points"]["2026-10-23"], 0.205)
        prev = meta["contract_prev"]
        self.assertEqual(prev["slug"], DEC)
        self.assertEqual(prev["resolves_on"], "2026-12-31")
        self.assertEqual(prev["until"], "2026-11-20")
        self.assertEqual(meta["resolves_on"], "2027-03-31")
        self.assertEqual(meta["price_now"], 0.285)      # цена рынка из ответа серии

    def test_дата_смены_помнится_на_следующих_прогонах(self):
        # мутация: вычислять дату смены заново -> на следующий день её нет, и
        # история нового контракта с 19.08 заливается в ряд.
        self.first_run()
        self.add_rows("march-31-2027", [(_stamp("2026-12-03"), 0.30)])
        self.run_at(datetime(2026, 12, 3, 20, 55, tzinfo=UTC))
        self.add_rows("march-31-2027", [(_stamp("2026-12-04"), 0.32),
                                        (_stamp("2026-12-05"), 0.33)])
        points, meta, saved = self.run_at(datetime(2026, 12, 5, 20, 55, tzinfo=UTC))
        self.assertEqual(meta["contract_since"], "2026-12-03")
        self.assertEqual(meta["contract_prev"]["slug"], DEC)
        self.assertEqual(sorted(points), ["2026-12-03", "2026-12-04", "2026-12-05"])
        self.assertEqual(saved["points"]["2026-10-23"], 0.205)

    def test_пустой_стор_сменой_не_считается(self):
        # Первый прогон сразу на мартовском: прежнего контракта не было.
        points, meta, _ = self.run_at(datetime(2026, 12, 3, 20, 55, tzinfo=UTC))
        self.assertEqual(meta["slug"], MAR)
        self.assertIsNone(meta["contract_since"])
        self.assertIsNone(meta["contract_prev"])
        self.assertEqual(min(points), "2026-08-19")


class TestTileAcrossSwitch(unittest.TestCase):
    """Тайл: изменения по календарным датам и только внутри текущего контракта."""

    NOW = datetime(2026, 12, 6, 9, 0, tzinfo=UTC)

    def setUp(self):
        self.monitors = need(self, "pipeline.compute.monitors", "BUILDERS")
        self.store = need(self, "pipeline.lib.store", "save_series")
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        prev = os.environ.get("STATE_DIR")
        os.environ["STATE_DIR"] = self.tmp.name
        self.addCleanup(lambda: os.environ.pop("STATE_DIR", None) if prev is None
                        else os.environ.__setitem__("STATE_DIR", prev))

    def tile(self, points, now=None, **meta):
        base = {"status": "ok", "fetched_at": (now or self.NOW).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "question": "Russia x Ukraine ceasefire agreement by March 31, 2027?",
                "slug": MAR, "end_date": "2027-04-01T03:59:00Z",
                "resolves_on": "2027-03-31"}
        base.update(meta)
        self.store.save_series("polymarket_ceasefire",
                               {"id": "polymarket_ceasefire", "points": points, "meta": base})
        return dict(self.monitors.BUILDERS)["polymarket"](self.store, now or self.NOW)

    # Декабрьский контракт до 02.12 (уровень ~10 %), мартовский с 03.12 (~30 %).
    SWITCHED = {"2026-11-05": 0.125, "2026-11-06": 0.145, "2026-11-26": 0.10,
                "2026-11-27": 0.10, "2026-11-28": 0.10, "2026-11-29": 0.095,
                "2026-11-30": 0.095, "2026-12-01": 0.09, "2026-12-02": 0.09,
                "2026-12-03": 0.30, "2026-12-04": 0.32, "2026-12-05": 0.33,
                "2026-12-06": 0.31}
    PREV = {"slug": DEC, "resolves_on": "2026-12-31", "until": "2026-12-02"}

    def test_через_стык_изменения_не_считаются(self):
        """06.12: неделю назад (29.11) в ряду ещё декабрьский контракт — 9,5 %.

        мутация: считать «7 точек назад» или не смотреть на дату смены ->
        «+21,5 п.п. за неделю» из смены контракта, а не из новостей.
        """
        t = self.tile(self.SWITCHED, contract_since="2026-12-03", contract_prev=self.PREV)
        p = t["payload"]
        self.assertEqual(p["prob_pct"], 31.0)
        self.assertIsNone(p["chg_7d_pp"])
        self.assertIsNone(p["chg_30d_pp"])
        self.assertEqual(p["contract_since"], "2026-12-03")
        self.assertEqual(p["contract_prev_end_date"], "2026-12-31")
        self.assertEqual(t["headline"], "Соглашение до 31.03.2027: 31%")
        self.assertIn("С 03.12.2026 показан контракт до 31.03.2027 вместо контракта "
                      "до 31.12.2026", t["note"])

    def test_через_неделю_после_смены_изменение_возвращается(self):
        pts = dict(self.SWITCHED, **{"2026-12-08": 0.34, "2026-12-10": 0.36})
        t = self.tile(pts, now=datetime(2026, 12, 10, 9, 0, tzinfo=UTC),
                      contract_since="2026-12-03", contract_prev=self.PREV)
        # 10.12 против 03.12 — оба дня на мартовском контракте: 36 − 30 = +6,0.
        self.assertEqual(t["payload"]["chg_7d_pp"], 6.0)
        self.assertIsNone(t["payload"]["chg_30d_pp"])
        self.assertIn("(+6,0 п.п. за неделю)", t["headline"])

    def test_изменение_считается_по_календарю_а_не_по_точкам(self):
        """В ряду пропуски, поэтому «7 точек назад» — не «неделю назад».

        Точки: 28.10, 29.10, 30.10, 31.10, 02.11, 03.11, 05.11, 06.11 (без 01.11 и
        04.11). Семь календарных дней от 06.11 — 30.10 (0,155); «семь точек назад»
        — 28.10 (0,165).
        мутация: считать по точкам -> −2,0 вместо −1,0.
        """
        pts = {"2026-10-28": 0.165, "2026-10-29": 0.155, "2026-10-30": 0.155,
               "2026-10-31": 0.145, "2026-11-02": 0.135, "2026-11-03": 0.16,
               "2026-11-05": 0.125, "2026-11-06": 0.145}
        t = self.tile(pts, now=datetime(2026, 11, 6, 21, 0, tzinfo=UTC), slug=DEC,
                      question="Russia x Ukraine ceasefire agreement by December 31, 2026?",
                      resolves_on="2026-12-31", end_date="2027-01-01T04:59:00Z")
        self.assertEqual(t["payload"]["chg_7d_pp"], -1.0)
        self.assertIn("(−1,0 п.п. за неделю)", t["headline"])

    def test_точка_неделю_назад_пропущена_берётся_ближайшая_раньше(self):
        # 07 дней назад от 08.11 — 01.11, точки нет; ближайшая раньше — 31.10.
        pts = {"2026-10-31": 0.145, "2026-11-02": 0.135, "2026-11-08": 0.155}
        t = self.tile(pts, now=datetime(2026, 11, 8, 21, 0, tzinfo=UTC), slug=DEC)
        self.assertEqual(t["payload"]["chg_7d_pp"], 1.0)
        # А вот точка двухнедельной давности «неделей назад» не считается.
        pts = {"2026-10-20": 0.165, "2026-11-08": 0.155}
        t = self.tile(pts, now=datetime(2026, 11, 8, 21, 0, tzinfo=UTC), slug=DEC)
        self.assertIsNone(t["payload"]["chg_7d_pp"])

    def test_крупное_число_цена_сейчас_и_это_сказано(self):
        # Срез 06.12 — 31 %, цена в момент прогона — 33,5 %.
        # мутация: показывать срез -> число на тайле отстаёт от рынка на часы
        # (а ряд обновляется внутри дня ради скорости события).
        t = self.tile(self.SWITCHED, contract_since="2026-12-03", contract_prev=self.PREV,
                      price_now=0.335, price_now_at="2026-12-06T08:55:00Z")
        self.assertEqual(t["payload"]["prob_pct"], 33.5)
        self.assertEqual(t["payload"]["prob_kind"], "now")
        self.assertIn("34% сейчас", t["headline"])
        self.assertIn("Крупное число — цена сейчас", t["note"])
        # График — только срезы: последняя точка 31 %, а не 33,5 %.
        self.assertEqual(t["payload"]["series"][-1], ["2026-12-06", 31.0])

    def test_устаревшая_цена_сейчас_не_показывается(self):
        # В meta цена позавчерашнего прогона, а срез уже свежее — берётся срез.
        t = self.tile(self.SWITCHED, contract_since="2026-12-03", contract_prev=self.PREV,
                      price_now=0.20, price_now_at="2026-12-04T20:55:00Z")
        self.assertEqual(t["payload"]["prob_pct"], 31.0)
        self.assertEqual(t["payload"]["prob_kind"], "snapshot")

    def test_в_день_смены_без_среза_нового_контракта_показана_цена_сейчас(self):
        # Смена 03.12, среза нового контракта за 03.12 ещё нет: показывать последнюю
        # точку ряда значило бы подписать декабрьские 9 % мартовским вопросом.
        pts = {d: v for d, v in self.SWITCHED.items() if d < "2026-12-03"}
        t = self.tile(pts, now=datetime(2026, 12, 3, 9, 0, tzinfo=UTC),
                      contract_since="2026-12-03", contract_prev=self.PREV,
                      price_now=0.285, price_now_at="2026-12-03T08:55:00Z")
        self.assertEqual(t["payload"]["prob_pct"], 28.5)
        self.assertEqual(t["asof"], "2026-12-03")
        self.assertIsNone(t["payload"]["chg_7d_pp"])


if __name__ == "__main__":
    unittest.main()
