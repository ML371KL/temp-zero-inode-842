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
import re
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
        self.iss = need(self, "pipeline.fetch.iss", "index", "selt", "breadth", "futoi",
                        "index_yield", "YIELD_SANE",
                        "index_yield_estimate", "_bond_yield_cache")
        self.store = need(self, "pipeline.lib.store", "upsert_points")
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prev = os.environ.get("STATE_DIR")
        os.environ["STATE_DIR"] = self.tmp.name
        self.addCleanup(self._restore)
        # Кэш котировок облигаций живёт в модуле (один запрос на прогон, оба
        # индекса). Между тестами он обязан быть пуст: иначе выборка соседнего
        # теста подменяет транспорт и проверка «оценка не строится по огрызку»
        # проходит на чужих данных.
        self.iss._bond_yield_cache.clear()
        self.addCleanup(self.iss._bond_yield_cache.clear)

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
        #
        # Часы ЗАМОРОЖЕНЫ: _front_contract отбирает живые контракты по today_msk(),
        # и фикстура с экспирацией 2026-08-31 была календарной бомбой — с 01.09
        # весь набор краснел бы «не нашёл живых контрактов BR» на любом прогоне.
        # CI-гард часов такое не видит: он ищет вызовы часов В тестах, а не
        # зависимость через прод-код (аудит 18.08.2026).
        def responder(url):
            if "securities.columns" in url:
                return _payload({"securities": {
                    "columns": ["SECID", "ASSETCODE", "LASTTRADEDATE"],
                    "data": [["BRQ6", "BR", "2026-08-31"]]}})
            return _history([["2026-08-10", 70.0, None], ["2099-12-30", 999.0, None]],
                            columns=("TRADEDATE", "CLOSE", "SETTLEPRICE"))

        self.serve(responder)
        with mock.patch.object(self.iss.dates, "today_msk",
                               return_value=date(2026, 8, 11)):
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


class TestYieldSanity(IssCase):
    """Доходность облигационного индекса вне разумного коридора — не число.

    ОПЛАЧЕНО ПРОДОМ 20.08.2026: ISS с 14.08 отдавал в поле YIELD у RUCBHYCP
    мусор (207 -> 14 204 -> 2 850 -> 6 595 при неизменных CLOSE ~78 и DURATION
    ~465), панель записывала его как факт, и витрина печатала читателю «ВДО
    2 850,3% к ОФЗ 2Y» — тайлом с тиром B, то есть «направление подтверждено».
    Отказ источника, выглядящий исправной работой: fetched_at свежий, статус ok.
    """

    def rows(self, yields):
        days = ["2026-08-1%d" % i for i in range(1, 1 + len(yields))]
        return _history([[d, 78.0, 1e9, y] for d, y in zip(days, yields)])

    def test_взрыв_доходности_не_попадает_в_ряд(self):
        self.serve(lambda _u: self.rows([26.5, 27.4, 14204.82]))
        _sid, points, meta = self.iss.index_yield(sec="RUCBHYCP", start="2026-08-11",
                                                  end="2026-08-19")
        self.assertEqual(sorted(points.values()), [26.5, 27.4])
        self.assertEqual(meta["status"], "stale", "мусор источника не назван отказом")
        self.assertIn("коридор", meta["note"])
        self.assertIn("14204.82", meta["note"], "в записке нет самого значения")

    def test_все_значения_мусорные_это_громкий_отказ(self):
        # Ряд не просто отстал — источник сломан целиком. Это FetchError: run.py
        # ловит его, метит ряд error и пишет в журнал, а на панели загорается
        # жёлтая точка. Тихо вернуть пустой ряд нельзя — он выглядел бы «свежим».
        self.serve(lambda _u: self.rows([207.47, 14204.82, 2850.29]))
        with self.assertRaises(self.iss.FetchError) as ctx:
            self.iss.index_yield(sec="RUCBHYCP", start="2026-08-11", end="2026-08-19")
        self.assertIn("коридор", str(ctx.exception))
        self.assertIn("2850.29", str(ctx.exception), "не названо последнее значение")

    def test_нормальные_значения_проходят_без_помех(self):
        self.serve(lambda _u: self.rows([15.7, 15.9, 26.09, 99.9, 1.0]))
        _sid, points, meta = self.iss.index_yield(sec="RUCBCPNS", start="2026-08-11",
                                                  end="2026-08-19")
        self.assertEqual(len(points), 5, "коридор съел здоровые значения")
        self.assertEqual(meta["status"], "ok")
        self.assertIsNone(meta.get("dropped_insane"))

    def test_ноль_по_прежнему_заглушка(self):
        self.serve(lambda _u: self.rows([0.0, 26.5]))
        _sid, points, _meta = self.iss.index_yield(sec="RUCBHYCP", start="2026-08-11",
                                                   end="2026-08-19")
        self.assertEqual(list(points.values()), [26.5])

    def test_цена_индекса_коридором_не_режется(self):
        # Коридор — только для доходности: CLOSE индекса живёт в пунктах и может
        # быть любым (IMOEX ходил от 500 до 4 300).
        self.serve(lambda _u: _history([["2026-08-11", 4300.0, 1e9, None]]))
        _sid, points, meta = self.iss.index(sec="IMOEX", start="2026-08-11",
                                            end="2026-08-19")
        self.assertEqual(points, {"2026-08-11": 4300.0})
        self.assertEqual(meta["status"], "ok")


class TestYieldFromConstituents(IssCase):
    """Резерв доходности индекса: считаем из состава, когда биржа отдаёт мусор.

    Без резерва ряд замирает на последнем здоровом дне, и тайл мертвеет на всё
    время поломки источника — а она уже длится неделю. Метод сверен с биржей на
    здоровых днях (31.07, 05.08, 11.08, 13.08 × два индекса): расхождение
    −0,41…+0,02 п.п. у ВДО и −0,07…−0,01 п.п. у корпоблигаций.
    """

    def serve_all(self, index_yields, weights, bond_yields):
        """История индекса + состав + витрина облигаций на одном транспорте."""
        def responder(url):
            if "analytics" in url:
                start = int(re.search(r"start=(\d+)", url).group(1)) if "start=" in url else 0
                rows = [["RUCBHYCP", "2026-08-19", s, s, s, w, 3, "2026-08-19"]
                        for s, w in list(weights.items())[start:start + 20]]
                return _payload({"analytics": {
                    "columns": ["indexid", "tradedate", "ticker", "shortnames",
                                "secids", "weight", "tradingsession",
                                "trade_session_date"], "data": rows}})
            if "markets/bonds" in url:
                return _payload({"marketdata": {
                    "columns": ["SECID", "YIELD"],
                    "data": [[s, y] for s, y in bond_yields.items()]}})
            days = ["2026-08-1%d" % i for i in range(1, 1 + len(index_yields))]
            return _history([[d, 78.0, 1e9, y] for d, y in zip(days, index_yields)])
        self.serve(responder)

    def test_мусор_биржи_заменяется_оценкой_по_составу(self):
        self.serve_all([26.5, 14204.82],
                       {"BOND-A": 60.0, "BOND-B": 40.0},
                       {"BOND-A": 30.0, "BOND-B": 20.0})
        _sid, points, meta = self.iss.index_yield(sec="RUCBHYCP", start="2026-08-11",
                                                  end="2026-08-19")
        # 0,6*30 + 0,4*20 = 26,0 — считаем руками, а не тем же кодом.
        self.assertEqual(points["2026-08-19"], 26.0)
        self.assertEqual(meta["method"], "constituents")
        self.assertEqual(meta["estimate_cover_pct"], 100.0)
        self.assertIn("оценка", meta["note"])
        self.assertEqual(points["2026-08-11"], 26.5, "здоровая точка потеряна")

    def test_здоровый_источник_резерв_не_трогает(self):
        # мутация «считать всегда» -> лишние 10 запросов на каждый прогон и
        # подмена биржевого числа собственной оценкой без повода.
        asked = []
        def responder(url):
            asked.append(url)
            return _history([["2026-08-11", 78.0, 1e9, 26.5],
                             ["2026-08-12", 78.0, 1e9, 26.9]])
        self.serve(responder)
        _sid, points, meta = self.iss.index_yield(sec="RUCBHYCP", start="2026-08-11",
                                                  end="2026-08-19")
        self.assertEqual(sorted(points.values()), [26.5, 26.9])
        self.assertNotIn("method", meta)
        self.assertFalse([u for u in asked if "analytics" in u or "bonds" in u],
                         "за составом ходили при исправном источнике")

    def test_огрызок_корзины_не_превращается_в_число(self):
        # Выпавшие бумаги — обычно самые неликвидные, то есть самые доходные:
        # оценка по половине веса похожа на правду и потому опасна. Лучше пустой
        # ряд с отказом, чем правдоподобное число.
        self.serve_all([14204.82],
                       {"BOND-A": 60.0, "BOND-B": 40.0},
                       {"BOND-A": 30.0})
        with self.assertRaises(self.iss.FetchError) as ctx:
            self.iss.index_yield(sec="RUCBHYCP", start="2026-08-11", end="2026-08-19")
        # Наружу идёт ИСХОДНАЯ причина — мусор биржи, а не «покрытие мало»:
        # чинить надо источник, а не корзину.
        self.assertIn("коридор", str(ctx.exception))

    def test_оценка_считается_взвешенно_а_не_средним(self):
        # мутация: простое среднее вместо взвешенного -> (30+20)/2 = 25 вместо 29.
        self.serve_all([14204.82],
                       {"BOND-A": 90.0, "BOND-B": 10.0},
                       {"BOND-A": 30.0, "BOND-B": 20.0})
        _sid, points, _meta = self.iss.index_yield(sec="RUCBHYCP", start="2026-08-11",
                                                   end="2026-08-19")
        self.assertEqual(points["2026-08-19"], 29.0)


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
