"""Фетчеры ISS: окно запроса, отказы источника и покрытие корзины.

Три класса дефектов, найденных аудитом 11.08.2026 и закрытых здесь навсегда:

1. ОКНО. Фетчеры клали в стор ЛЮБУЮ дату из ответа. Одна битая строка (опечатка
   года в TRADEDATE) уводит last_date в 2099-й, следующее окно оказывается
   перевёрнутым, ISS отвечает пустым списком, empty_is_fatal при наполненном сторе
   молчит — и ряд стоит НАВСЕГДА со status=ok. Лечится только ручным bootstrap.
2. ТИХИЙ ОТКАЗ. futoi глотал все ошибки HTTP и рапортовал ok с запиской про
   «нормальный лаг источника»; breadth считал ширину рынка по уцелевшим бумагам,
   а в meta писал полный размер списка.
3. ПОРЯДОК СРЕЗОВ. _cmp_key делал любую строку времени старше любого seqnum.

Даты фиксированные (правило tests/__init__.py), в сеть не ходим: подменяется
самый нижний слой http.get_bytes.
"""

import json
import os
import unittest
from datetime import date, timedelta
from tempfile import TemporaryDirectory
from unittest import mock

from tests import need

FUTURE = "2099-12-31"   # заведомо вне любого окна и в любом календаре


def _payload(obj):
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def _history(rows, columns=("TRADEDATE", "CLOSE", "VALUE", "YIELD")):
    return _payload({"history": {"columns": list(columns), "data": rows}})


class IssCase(unittest.TestCase):
    """Пустой стор во временном каталоге + подменённый транспорт."""

    def setUp(self):
        self.http = need(self, "pipeline.lib.http", "get_bytes")
        self.iss = need(self, "pipeline.fetch.iss", "index", "selt", "breadth", "futoi")
        self.store = need(self, "pipeline.lib.store", "upsert_points")
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prev = os.environ.get("STATE_DIR")
        os.environ["STATE_DIR"] = self.tmp.name
        self.addCleanup(self._restore)

    def _restore(self):
        if self.prev is None:
            os.environ.pop("STATE_DIR", None)
        else:
            os.environ["STATE_DIR"] = self.prev

    def serve(self, responder):
        patcher = mock.patch.object(self.http, "get_bytes",
                                    side_effect=lambda url, **kw: responder(url))
        patcher.start()
        self.addCleanup(patcher.stop)


class TestRequestWindow(IssCase):
    """Точка вне запрошенного окна в ряд не попадает — ни в одном фетчере."""

    def test_index_drops_row_outside_window(self):
        # мутация: класть в ряд любую дату из ответа -> last_date=2099-12-31,
        # следующее окно from=2099-12-26&till=сегодня -> 0 точек и вечный ok.
        self.serve(lambda _u: _history([["2026-08-10", 2293.32, 1, 0],
                                        [FUTURE, 9999.0, 1, 0]]))
        _sid, points, meta = self.iss.index("IMOEX", start="2026-08-03", end="2026-08-11")
        self.assertEqual(points, {"2026-08-10": 2293.32})
        self.assertEqual(meta["asof"], "2026-08-10")

    def test_selt_drops_row_outside_window_and_knows_gold_unit(self):
        # мутация: unit="rub" для всех инструментов -> золото объявлено рублями за
        # штуку; store.upsert_points единицу уже не перезапишет (закрепится навсегда).
        self.serve(lambda _u: _history([["2026-08-10", 11.11, None],
                                        [FUTURE, 99.9, None]],
                                       columns=("TRADEDATE", "CLOSE", "WAPRICE")))
        _sid, points, meta = self.iss.selt("GLDRUB_TOM", start="2026-08-03",
                                           end="2026-08-11")
        self.assertEqual(points, {"2026-08-10": 11.11})
        self.assertEqual(meta["unit"], "rub_g")

    def test_futures_window_compares_dates_as_strings(self):
        # У futures_br frm/till — datetime.date, а дата из ответа строка: наивное
        # `frm <= day <= till` роняет фетчер TypeError на КАЖДОМ прогоне.
        def responder(url):
            if "securities.columns" in url:
                return _payload({"securities": {
                    "columns": ["SECID", "ASSETCODE", "LASTTRADEDATE"],
                    "data": [["BRQ6", "BR", "2026-08-31"]]}})
            return _history([["2026-08-10", 70.0, None], ["2099-12-30", 999.0, None]],
                            columns=("TRADEDATE", "CLOSE", "SETTLEPRICE"))

        self.serve(responder)
        _sid, points, _meta = self.iss.futures_br(start="2026-08-03", end="2026-08-11")
        self.assertEqual(points, {"2026-08-10": 70.0})

    def test_futoi_drops_row_outside_window(self):
        self.serve(lambda _u: _payload({"futoi": {
            "columns": ["tradedate", "clgroup", "pos", "pos_long", "pos_short",
                        "pos_long_num", "pos_short_num", "seqnum"],
            "data": [["2026-08-10", "FIZ", 1.0, 2.0, -1.0, 5.0, 5.0, 1],
                     [FUTURE, "FIZ", 9.0, 9.0, -9.0, 9.0, 9.0, 2]]}}))
        out = dict((sid, pts) for sid, pts, _m in
                   self.iss.futoi(ticker="MX", start="2026-08-10", end="2026-08-11"))
        self.assertEqual(out["futoi_mx_pos"], {"2026-08-10": 1.0})

    def test_poisoned_series_heals_itself(self):
        # Даже если дата из будущего попала в стор мимо фетчеров (ручной ввод,
        # чужой писатель), окно не должно оставаться перевёрнутым навсегда.
        fetch = need(self, "pipeline.fetch", "incremental_start")
        self.store.upsert_points("imoex", {"2026-08-10": 2293.32, FUTURE: 9999.0})
        self.assertEqual(fetch.incremental_start("imoex", 5, "1997-01-01"), "1997-01-01")


class TestFutoiReportsFailures(IssCase):
    """«Точек нет» из-за 14-дневного лага и из-за 403 — разные состояния."""

    ROWS = {"futoi": {"columns": ["tradedate", "clgroup", "pos", "pos_long", "pos_short",
                                  "pos_long_num", "pos_short_num", "seqnum"],
                      "data": [["2026-08-10", "FIZ", 1.0, 2.0, -1.0, 5.0, 5.0, 1]]}}

    def _seed(self):
        """Ряды futoi уже собраны — иначе пустой ответ и так фатален (empty_is_fatal)."""
        for grp, gsuf in self.iss.FUTOI_GROUPS.items():
            for field, fsuf in self.iss.FUTOI_FIELDS.items():
                sid = "_".join(p for p in ("futoi_mx", gsuf, fsuf) if p)
                self.store.upsert_points(sid, {"2026-07-27": 1.0})

    def test_total_http_failure_is_an_error_not_ok(self):
        # мутация: глотать FetchError и отдавать ok -> отказ источника неотличим от
        # его нормального режима, alerts._source_stale молчит (CONTRACT §7).
        self._seed()

        def boom(url):
            raise self.http.FetchError("HTTP 403 Forbidden", url=url)

        self.serve(boom)
        metas = [m for _sid, _p, m in self.iss.futoi(ticker="MX", start="2026-08-05",
                                                     end="2026-08-11")]
        self.assertTrue(metas)
        for meta in metas:
            self.assertEqual(meta["status"], "error")
            self.assertIn("отказов", meta["note"])
            self.assertGreater(meta["fetch_failed"], 0)

    def test_healthy_run_stays_ok(self):
        self.serve(lambda _u: _payload(self.ROWS))
        metas = [m for _sid, _p, m in self.iss.futoi(ticker="MX", start="2026-08-10",
                                                     end="2026-08-11")]
        for meta in metas:
            self.assertEqual(meta["status"], "ok")
            self.assertEqual(meta["fetch_failed"], 0)


class TestFutoiSliceOrder(unittest.TestCase):
    """Позиция дня — ПОСЛЕДНИЙ срез сессии, а не первый попавшийся."""

    def setUp(self):
        self.iss = need(self, "pipeline.fetch.iss", "_cmp_key")

    def test_seqnum_beats_time_string(self):
        # мутация: ключ, переключающий тип ((1,0.0,'10:00') > (0,999999,'')) —
        # при смешанной схеме одного дня в ряд уезжает утренний срез.
        self.assertGreater(self.iss._cmp_key(999999.0), self.iss._cmp_key("10:00:00"))
        self.assertGreater(self.iss._cmp_key(2.0), self.iss._cmp_key(1.0))
        self.assertGreater(self.iss._cmp_key("18:45:00"), self.iss._cmp_key("10:00:00"))


class TestBreadthCoverage(IssCase):
    """Ширина рынка обязана рассказывать, по скольким бумагам она посчитана."""

    START, END = "2025-06-16", "2025-06-20"
    FRESH = "2025-06-20"

    def setUp(self):
        super().setUp()
        self.names = self.iss.BREADTH_TICKERS[:20]
        # История в сторе: 170 дней — больше BREADTH_MIN_OBS, иначе MA200 не считается
        # ни по одной бумаге и агрегата не будет вовсе.
        for n, ticker in enumerate(self.names):
            pts = {(date(2025, 1, 1) + timedelta(days=i)).isoformat(): 100.0 + n + i * 0.1
                   for i in range(170)}
            self.store.upsert_points(f"px_{ticker.lower()}", pts)

    def _serve(self, live):
        alive = {t.upper() for t in self.names[:live]}

        def responder(url):
            ticker = url.split("/securities/")[1].split(".json")[0].upper()
            if ticker not in alive:
                raise self.http.FetchError("HTTP 403 Forbidden", url=url)
            return _history([[self.FRESH, 500.0, None]],
                            columns=("TRADEDATE", "CLOSE", "LEGALCLOSEPRICE"))

        self.serve(responder)
        out = self.iss.breadth(tickers=self.names, start=self.START, end=self.END)
        return next((p, m) for sid, p, m in out if sid == "breadth")

    def test_full_run_publishes_actual_coverage(self):
        # мутация: писать tickers=len(merged) -> в проде стояло «45» при живых 42.
        points, meta = self._serve(len(self.names))
        self.assertEqual(max(points), self.FRESH)
        self.assertEqual(meta["status"], "ok")
        self.assertEqual(meta["tickers"], len(self.names))
        self.assertEqual(meta["fetch_failed"], 0)

    def test_partial_failure_does_not_publish_a_thin_day(self):
        # 16 живых из 20 проходят порог BREADTH_MIN_TICKERS=15 и раньше считались
        # МОЛЧА — корзина другая, значение уезжает на единицы-десятки п.п.
        points, meta = self._serve(16)
        self.assertNotIn(self.FRESH, points)
        self.assertIn("HTTP-отказов 4", meta["note"])
        self.assertEqual(meta["fetch_failed"], 4)
        self.assertTrue(points, "прошлые дни агрегата терять нельзя")

    def test_total_failure_marks_the_series(self):
        # мутация: отдавать ok при 403 по ВСЕМ бумагам -> ряд из кэша с зелёным тайлом.
        points, meta = self._serve(0)
        self.assertEqual(meta["status"], "error")
        self.assertEqual(meta["fetch_failed"], len(self.names))
        self.assertTrue(points, "агрегат по истории считать всё равно надо")


class TestZcycCalendar(IssCase):
    """КБД спрашиваем за дни, когда биржа РЕАЛЬНО торговала."""

    def test_working_saturday_is_asked(self):
        # мутация: строить календарь только эвристикой «будни минус праздники» ->
        # рабочие субботы (МосБиржа торгует по ним с 2025) в КБД не попадают
        # никогда, и панель показывает на них пятничную кривую.
        self.store.upsert_points("imoex", {"2024-12-26": 2700.0, "2024-12-27": 2710.0,
                                           "2024-12-28": 2720.0})
        for _tenor, sid in self.iss.ZCYC_SERIES:   # ряды уже есть: пустой ответ не фатален
            self.store.upsert_points(sid, {"2024-12-25": 15.0})
        asked = []

        def responder(url):
            asked.append(url.split("date=")[1].split("&")[0])
            return _payload({"yearyields": {"columns": ["tradedate", "period", "value"],
                                            "data": []}})

        self.serve(responder)
        self.iss.zcyc(start="2024-12-26", end="2024-12-29")
        self.assertIn("2024-12-28", asked)          # рабочая суббота
        self.assertNotIn("2024-12-29", asked)       # обычное воскресенье — не спрашиваем


if __name__ == "__main__":
    unittest.main()
