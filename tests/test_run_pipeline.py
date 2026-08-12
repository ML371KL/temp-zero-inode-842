"""Прогон как целое: правдивость журнала и живая цена внутри дня.

Две защёлки, каждая — из аудита:

* fetch_all писал «ok» о фетчерах, которые вернули отказ полем meta.status (по
  контракту §0 половина источников наружу не кидает). Три ряда не собрались ни
  разу, а оператор видел зелёный прогон;
* режим intraday опрашивал history тех же бумаг — а history внутри дня текущего
  дня ещё не содержит, — и весь торговый день переиздавал вчерашнее закрытие с
  новым generated_at. iss.intraday_quote существовал, но не вызывался ниоткуда.

В сеть не ходим: фетчер подменяется на уровне run._resolve и iss.intraday_quote.
"""

import os
import unittest
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from unittest import mock

from tests import need

UTC = timezone.utc
NOW = datetime(2026, 8, 11, 12, 30, 0, tzinfo=UTC)
FETCHED = "2026-08-11T12:29:00Z"


class RunCase(unittest.TestCase):
    def setUp(self):
        self.run = need(self, "pipeline.run", "fetch_all", "fetch_live_quotes", "_quotes")
        self.store = need(self, "pipeline.lib.store", "upsert_points")
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prev = os.environ.get("STATE_DIR")
        os.environ["STATE_DIR"] = self.tmp.name
        self.addCleanup(self._restore_env)
        self.journal = self.run.Journal()

    def _restore_env(self):
        if self.prev is None:
            os.environ.pop("STATE_DIR", None)
        else:
            os.environ["STATE_DIR"] = self.prev

    def with_fetcher(self, fn):
        patcher = mock.patch.object(self.run, "_resolve", lambda fetcher: fn)
        patcher.start()
        self.addCleanup(patcher.stop)


class TestSourceFamilyRanking(RunCase):
    """Статус семьи источников: «не собирали» не должно быть хуже «отказал».

    Аудит 12.08.2026: вся семья Минфина светилась `missing` из-за одного `ngd`,
    который в тот день просто не попадал в окно опроса, — при том что
    `budget_deficit` в ней собирался, а `fnb` и `ofz_auctions` честно падали.
    Читатель видел «у Минфина не собрано ничего», то есть ровно наоборот. Заодно
    строка теперь называет РЯД, который решил статус: без этого догадаться,
    кто именно тянет семью вниз, нельзя.
    """

    def sources(self, states):
        """states: {series_id: статус|None}. None = ряда в сторе нет вовсе."""
        fake_registry = {sid: {"fetcher": "demo.%s" % sid, "args": {}}
                         for sid in states}

        def points(_store, cand):
            status = states.get(cand)
            if status is None:
                return [], {}
            return ([("2026-08-11", 1.0)] if status != "missing" else [],
                    {"fetched_at": FETCHED, "asof": "2026-08-11", "status": status})

        with mock.patch.object(self.run.registry, "SERIES", fake_registry), \
             mock.patch.object(self.run.store, "list_series", return_value=list(states)), \
             mock.patch.object(self.run.monitors_mod, "series_points", points), \
             mock.patch.object(self.run.monitors_mod, "series_status",
                               lambda cand, pts, meta, now: states.get(cand) or "missing"):
            return self.run._sources_fallback(NOW)

    def test_отказ_перевешивает_несобранное(self):
        got = self.sources({"budget": "ok", "ngd": None, "fnb": "error"})
        self.assertEqual(got["demo"]["status"], "error")
        self.assertEqual(got["demo"]["series"], "fnb")

    def test_несобранное_не_топит_живую_семью(self):
        # мутация: вернуть прежний порядок -> «missing» поверх исправного ряда.
        got = self.sources({"budget": "ok", "ngd": None})
        self.assertEqual(got["demo"]["status"], "ok")
        self.assertEqual(got["demo"]["series"], "budget")

    def test_семья_без_единого_ряда_остаётся_missing(self):
        got = self.sources({"ngd": None, "fnb": None})
        self.assertEqual(got["demo"]["status"], "missing")

    def test_порядок_худшести_целиком(self):
        self.assertEqual(self.sources({"a": "ok", "b": "stale"})["demo"]["status"], "stale")
        self.assertEqual(self.sources({"a": "stale", "b": "missing"})["demo"]["status"],
                         "missing")
        self.assertEqual(self.sources({"a": "missing", "b": "error"})["demo"]["status"],
                         "error")

    def test_неизвестный_статус_не_выдаётся_за_дефект(self):
        # Незнакомое слово приравнивается к missing: выдумывать отказ хуже, чем
        # промолчать. Проверяем через сам компаратор — он и есть правило.
        self.assertFalse(self.run._worse("что-то новое", "error"))
        self.assertTrue(self.run._worse("error", "что-то новое"))


class TestFetchReportTellsTheTruth(RunCase):
    def test_meta_status_error_is_a_failure(self):
        # мутация: считать провалом только исключение -> «[fetch] fnb ok точек=0»,
        # сводки отказов нет, алерт по источникам не сработает.
        self.with_fetcher(lambda **kw: [("fnb", {}, {"source": "minfin", "status": "error",
                                                     "note": "страница не разобралась",
                                                     "fetched_at": FETCHED})])
        report = self.run.fetch_all([("fnb", {"fetcher": "minfin.fnb", "args": {}}, None)],
                                    self.journal)
        self.assertEqual(report["fnb"]["status"], "error")
        self.assertTrue(self.journal.warns)

    def test_manual_needed_is_a_failure_too(self):
        self.with_fetcher(lambda **kw: [("orfr_flows", {}, {"source": "orfr",
                                                            "status": "manual_needed",
                                                            "fetched_at": FETCHED})])
        report = self.run.fetch_all([("orfr_flows", {"fetcher": "orfr.flows", "args": {}},
                                      None)], self.journal)
        self.assertEqual(report["orfr_flows"]["status"], "error")

    def test_good_fetch_stays_ok(self):
        self.with_fetcher(lambda **kw: [("imoex", {"2026-08-11": 2301.0},
                                         {"source": "iss", "status": "ok", "asof": "2026-08-11",
                                          "fetched_at": FETCHED})])
        report = self.run.fetch_all([("imoex", {"fetcher": "iss.index", "args": {}}, None)],
                                    self.journal)
        self.assertEqual(report["imoex"]["status"], "ok")
        self.assertEqual(report["imoex"]["points"], 1)
        self.assertEqual(self.journal.warns, [])


class TestIntradayQuotes(RunCase):
    def daily_close(self):
        self.store.upsert_points("imoex", {"2026-08-07": 2280.0, "2026-08-10": 2293.32},
                                 {"source": "iss", "status": "ok", "asof": "2026-08-10",
                                  "fetched_at": FETCHED})

    def test_live_point_wins_over_yesterdays_close(self):
        # мутация: читать только дневной ряд -> витрина весь день показывает
        # вчерашнее закрытие и пишет «обновлено только что».
        self.daily_close()
        self.store.upsert_points("live_imoex", {"2026-08-11": 2323.82},
                                 {"source": "iss", "status": "ok", "asof": "2026-08-11",
                                  "fetched_at": FETCHED, "delay_min": 15, "intraday": True,
                                  "updatetime": "12:29:00"})
        quote = self.run._quotes(NOW)["imoex"]
        self.assertEqual(quote["value"], 2323.82)
        self.assertEqual(quote["asof"], "2026-08-11")
        self.assertTrue(quote["intraday"])
        self.assertEqual(quote["delay_min"], 15)
        # изменение считаем к ПОСЛЕДНЕМУ ЗАКРЫТИЮ, а не к самой живой точке
        self.assertAlmostEqual(quote["chg_pct"], 1.33, places=2)

    def test_without_live_series_quote_is_the_close(self):
        self.daily_close()
        quote = self.run._quotes(NOW)["imoex"]
        self.assertEqual(quote["value"], 2293.32)
        self.assertFalse(quote["intraday"])

    def test_stale_live_point_does_not_override_a_newer_close(self):
        # Вечером дневной прогон приносит официальное закрытие: показываем его,
        # а не позавчерашнюю живую котировку.
        self.daily_close()
        self.store.upsert_points("live_imoex", {"2026-08-07": 2270.0},
                                 {"source": "iss", "status": "ok", "asof": "2026-08-07",
                                  "fetched_at": FETCHED, "delay_min": 15})
        quote = self.run._quotes(NOW)["imoex"]
        self.assertEqual(quote["value"], 2293.32)
        self.assertFalse(quote["intraday"])

    def test_live_quotes_land_in_separate_series(self):
        # Живую цену НЕЛЬЗЯ класть в дневной ряд: ядро и состояния считаются на
        # закрытие, и подмена закрытия внутридневным значением ломает валидацию.
        self.daily_close()
        fake = mock.Mock(return_value=[("live_imoex", {"2026-08-11": 2323.82},
                                        {"source": "iss", "status": "ok",
                                         "asof": "2026-08-11", "fetched_at": FETCHED,
                                         "delay_min": 15})])
        with mock.patch("pipeline.fetch.iss.intraday_quote", fake):
            got = self.run.fetch_live_quotes(self.journal)
        self.assertEqual(got, {"live_imoex": "2026-08-11"})
        self.assertEqual(sorted((self.store.load_series("imoex") or {})["points"]),
                         ["2026-08-07", "2026-08-10"])
        self.assertEqual((self.store.load_series("live_imoex") or {})["points"],
                         {"2026-08-11": 2323.82})

    def test_dead_iss_does_not_break_the_run(self):
        self.daily_close()
        with mock.patch("pipeline.fetch.iss.intraday_quote",
                        side_effect=RuntimeError("ISS 503")):
            self.assertEqual(self.run.fetch_live_quotes(self.journal), {})
        self.assertTrue(self.journal.warns)
        self.assertEqual(self.run._quotes(NOW)["imoex"]["value"], 2293.32)


if __name__ == "__main__":
    unittest.main()
