"""Ряд доходности ВДО (rucbhycp_yield): медиана состава, которую биржа не переписывает.

Аудит 20.11.2026 нашёл в прежней конструкции четыре ошибки — каждая закрыта здесь
своим классом тестов:

  (а) оценка по составу жила один день: назавтра окно догрузки писало биржевое
      число поверх (журнал 12.11: «беру состав, 35,46 %», в сторе за 11.11 — 59,65);
  (б) правило «спорит с соседями» слепо к плавному уходу 35 -> 41 -> 48 -> 55 -> 70:
      соседи — тот же мусор;
  (в) при отказе оценки (19.11: покрытие 89,0 % при норме 90 %) в ряд уходило
      опровергнутое биржевое число, а поля прошлой оценки оставались в meta;
  (г) сама оценка — средневзвешенная с отсечкой — ползла вместе с проблемными
      бумагами (35,5 -> 40,4 % за 11–17.11).

Фикстуры — настоящие ответы биржи (tests/fixtures/):
  iss_rucbhycp_history_2026-11.json       история индекса 01.10–19.11.2026;
  iss_rucbhycp_analytics_2026-11-19.json  состав индекса, 144 бумаги, восемь страниц;
  iss_bonds_yields_2026-11-19.json        доходности бумаг состава на закрытие 19.11;
  iss_bonds_marketdata_2026-11-20.json    живой ответ 20.11 07:56 МСК с досками и
                                          блоком dataversion.

Даты фиксированные, в сеть не ходим: подменяется http.get_bytes.
"""

import json
import os
import re
import unittest
from tempfile import TemporaryDirectory
from unittest import mock

from tests import FIXTURES, need


def _load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _bytes(obj):
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


HISTORY = _load("iss_rucbhycp_history_2026-11.json")
ANALYTICS = _load("iss_rucbhycp_analytics_2026-11-19.json")["pages"]
YIELDS_1119 = _load("iss_bonds_yields_2026-11-19.json")
MARKET_1120 = _load("iss_bonds_marketdata_2026-11-20.json")

# Числа первоисточника, посчитанные НЕ этим кодом (отчёт аудита 20.11.2026, §8).
MEDIAN_1119 = 35.69          # взвешенная медиана состава на закрытие 19.11
MEAN_1119 = 55.98            # средневзвешенная по тем же бумагам
EXCHANGE_1119 = 85.65        # что публикует биржа за 19.11
EXCHANGE_1118 = 95.99


class HyCase(unittest.TestCase):
    """Пустой стор во временном каталоге + транспорт, отдающий фикстуры."""

    SID = "rucbhycp_yield"

    def setUp(self):
        self.http = need(self, "pipeline.lib.http", "get_bytes")
        self.iss = need(self, "pipeline.fetch.iss", "index_yield", "index_yield_median",
                        "live_bond_snapshot", "YIELD_GUARD_SINCE", "_bond_yield_cache")
        self.store = need(self, "pipeline.lib.store", "upsert_points", "load_series")
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        prev = os.environ.get("STATE_DIR")
        os.environ["STATE_DIR"] = self.tmp.name
        self.addCleanup(lambda: os.environ.pop("STATE_DIR", None) if prev is None
                        else os.environ.__setitem__("STATE_DIR", prev))
        self.iss._bond_yield_cache.clear()
        self.addCleanup(self.iss._bond_yield_cache.clear)
        self.asked = []

    def serve(self, market=YIELDS_1119, history_till=None, history_rows=None,
              history_fails=False, analytics=ANALYTICS):
        """История индекса (обрезанная по дню), состав и витрина облигаций."""
        def responder(url):
            self.asked.append(url)
            if "/analytics/" in url:
                start = re.search(r"[?&]start=(\d+)", url).group(1)
                return _bytes(analytics.get(start) or {"analytics": {"columns": [], "data": []}})
            if "markets/bonds/securities" in url:
                return _bytes(market)
            if history_fails:
                raise self.http.FetchError("HTTP 503 Service Unavailable", url=url)
            rows = history_rows if history_rows is not None else HISTORY["history"]["data"]
            if history_till:
                rows = [r for r in rows if r[0] <= history_till]
            return _bytes({"history": {"columns": HISTORY["history"]["columns"],
                                       "data": rows}})
        patcher = mock.patch.object(self.http, "get_bytes",
                                    side_effect=lambda url, **kw: responder(url))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.iss._bond_yield_cache.clear()

    def run_fetch(self, start="2026-10-15", end="2026-11-20"):
        """Один прогон фетчера и запись в стор — так, как это делает run.py."""
        sid, points, meta = self.iss.index_yield(sec="RUCBHYCP", start=start, end=end)
        saved = self.store.upsert_points(sid, points, dict(meta))
        return points, meta, saved

    def seed(self, points, **meta):
        self.store.upsert_points(self.SID, points, meta)

    # Ряд после разовой чистки: биржевые точки до 20.10 включительно, оценок нет.
    CLEANED = {"2026-10-12": 32.88, "2026-10-13": 33.03, "2026-10-14": 33.27,
               "2026-10-15": 33.05, "2026-10-16": 32.94, "2026-10-19": 32.89,
               "2026-10-20": 32.64}


class TestMedianEstimate(HyCase):
    """(г) Оценка — взвешенная медиана, а не средневзвешенная с отсечкой."""

    def test_медиана_настоящего_состава_19_11(self):
        # мутация: вернуть средневзвешенную -> 55,98 (по всем бумагам) либо ~40
        # (с отсечкой Тьюки), а не 35,69.
        self.serve()
        est = self.iss.index_yield_median("RUCBHYCP")
        self.assertEqual(est["value"], MEDIAN_1119)
        self.assertLess(est["value"], MEAN_1119 - 15)
        self.assertLess(est["value"], EXCHANGE_1119 - 40)

    def test_медиана_посчитана_руками(self):
        # Веса 40/35/25, доходности 30/36/5000. Накопленный вес: 40 (< 50) на 30 %,
        # 75 (> 50) на 36 % — медиана 36. Средневзвешенная была бы 1274,6.
        # мутация: среднее вместо медианы -> 1274,6; мутация: невзвешенная медиана
        # при весах 60/20/20 и доходностях 30/36/5000 дала бы 36 вместо 30.
        self.assertEqual(self.iss._weighted_median([(40, 30.0), (35, 36.0), (25, 5000.0)]),
                         36.0)
        self.assertEqual(self.iss._weighted_median([(60, 30.0), (20, 36.0), (20, 5000.0)]),
                         30.0)
        # Половина веса ровно на границе двух бумаг — середина между ними.
        self.assertEqual(self.iss._weighted_median([(50, 30.0), (50, 40.0)]), 35.0)
        self.assertIsNone(self.iss._weighted_median([]))

    def test_бумага_на_грани_дефолта_медиану_не_двигает(self):
        # Десять бумаг по 10 % веса с доходностью 30…39; у самой доходной поле
        # уходит с 39 на 5329 % (как RU000A108FG3 19.11.2026). Медиана та же.
        # мутация: средневзвешенная -> 34,5 превращается в 563,5.
        def market(top):
            ys = [30.0 + i for i in range(9)] + [top]
            return {"marketdata": {"columns": ["SECID", "YIELD"],
                                   "data": [[f"B{i}", y] for i, y in enumerate(ys)]}}
        pages = {"0": {"analytics": {
            "columns": ["indexid", "tradedate", "ticker", "shortnames", "secids", "weight"],
            "data": [["RUCBHYCP", "2026-11-19", f"B{i}", "", f"B{i}", 10.0]
                     for i in range(10)]}}}
        self.serve(market=market(39.0), analytics=pages)
        calm = self.iss.index_yield_median("RUCBHYCP")["value"]
        self.serve(market=market(5329.22), analytics=pages)
        stressed = self.iss.index_yield_median("RUCBHYCP")
        self.assertEqual(calm, 34.5)
        self.assertEqual(stressed["value"], 34.5)
        # Покрытие считается ДО отсечений: бумага с 5329 % — участник индекса.
        # мутация: считать покрытие после отсева «битых» -> 90,0 вместо 100,0.
        self.assertEqual(stressed["cover_pct"], 100.0)
        self.assertEqual(stressed["count"], 10)

    def test_покрытие_настоящего_состава_считается_до_отсечений(self):
        # 19.11 котировки есть у 138 бумаг из 144, вес 99,76 %. Прежняя оценка
        # называла «покрытием» остаток после отсева (89,0 % вечером 19.11) и из-за
        # этого не посчиталась.
        self.serve()
        est = self.iss.index_yield_median("RUCBHYCP")
        self.assertEqual(est["count"], 138)
        self.assertEqual(est["no_quote"], 6)
        self.assertEqual(est["cover_pct"], 99.8)

    def test_бумаги_без_котировки_не_участвуют_и_огрызок_не_считается(self):
        # Котировки только у половины состава — оценки нет вовсе: выпавшие бумаги
        # обычно самые неликвидные, то есть самые доходные.
        # мутация: снять порог покрытия -> правдоподобное число по огрызку.
        rows = YIELDS_1119["marketdata"]["data"]
        half = {"marketdata": {"columns": ["SECID", "YIELD"], "data": rows[:60]}}
        self.serve(market=half)
        with self.assertRaises(self.iss.FetchError) as ctx:
            self.iss.index_yield_median("RUCBHYCP")
        self.assertIn("покрывают лишь", str(ctx.exception))

    def test_день_оценки_день_котировок_а_не_дата_состава(self):
        # Состав датирован 19.11 (биржа публикует его ночью вместе со строкой
        # истории), котировки — 20.11. Оценка, подписанная датой состава, ложилась
        # бы на день раньше, чем торговались бумаги.
        # мутация: day = дата состава -> 2026-11-19.
        self.serve(market=MARKET_1120)
        est = self.iss.index_yield_median("RUCBHYCP")
        self.assertEqual(est["day"], "2026-11-20")
        self.assertEqual(est["composition_day"], "2026-11-19")

    def test_берётся_основной_режим_торгов_а_не_первая_доска(self):
        # У ОФЗ 26207 первой строкой идёт доска SPOB с 54,79 % при 14,18 % на TQOB.
        # мутация: брать первую попавшуюся строку -> 54,79.
        self.serve(market=MARKET_1120)
        yields, day = self.iss.live_bond_snapshot()
        self.assertEqual(yields["SU26207RMFS9"], 14.18)
        self.assertEqual(day, "2026-11-20")


class TestEstimateIsNotOverwritten(HyCase):
    """(а) День, за который записана оценка, биржа перезаписать не может."""

    def test_два_прогона_подряд_биржа_не_пишет_поверх_оценки(self):
        self.seed(self.CLEANED)
        # Вечер 19.11: история биржи кончается 18.11, котировки — закрытие 19.11.
        self.serve(history_till="2026-11-18")
        points, meta, _ = self.run_fetch(end="2026-11-19")
        self.assertEqual(points, {"2026-11-19": MEDIAN_1119})
        self.assertEqual(meta["estimated_days"], ["2026-11-19"])
        # Утро 20.11: биржа досчитала 19.11 = 85,65; котировки уже 20.11.
        self.serve(market=MARKET_1120)
        points, meta, saved = self.run_fetch()
        # мутация: окно догрузки пишет все дни в коридоре -> за 19.11 ляжет 85,65.
        self.assertEqual(saved["points"]["2026-11-19"], MEDIAN_1119)
        self.assertNotIn(EXCHANGE_1119, saved["points"].values())
        self.assertEqual(meta["estimated_days"], ["2026-11-19", "2026-11-20"])
        self.assertEqual(meta["status"], "ok")

    def test_оценённый_день_защищён_даже_от_близкого_биржевого_числа(self):
        # Защита — по перечню дней, а не по расхождению: биржевые 36,9 % рядом с
        # оценкой 35,69 % прошли бы сверку в 3 п.п., но день уже оценён.
        # мутация: защищать только расходящиеся дни -> 36,9 ляжет поверх 35,69.
        self.seed(dict(self.CLEANED, **{"2026-11-19": MEDIAN_1119}),
                  estimated_days=["2026-11-19"])
        rows = [["2026-11-19", 59.92, 1.0, 36.9]]
        self.serve(market=MARKET_1120, history_rows=rows)
        points, _meta, saved = self.run_fetch(start="2026-11-14")
        self.assertNotIn("2026-11-19", points)
        self.assertEqual(saved["points"]["2026-11-19"], MEDIAN_1119)

    def test_оценённый_день_до_даты_охраны_тоже_защищён(self):
        # С 21.11.2026 медиана досчитана по истории и за дни до YIELD_GUARD_SINCE
        # (ops/backfill_hy_median.py). Окно, достающее туда (пересборка ряда с нуля),
        # не должно вернуть поверх неё биржевое число: 02.10 у биржи 81,81 — внутри
        # коридора, и правило соседей его пропускает (соседей в ряду рядом нет).
        # мутация: пускать биржевые числа до даты охраны без оглядки на перечень ->
        # за 02.10 ляжет 81,81 (проверено: без правки так и выходит).
        self.seed(dict(self.CLEANED, **{"2026-10-02": 29.9}), estimated_days=["2026-10-02"])
        self.serve(market=MARKET_1120)
        _points, meta, saved = self.run_fetch(start="2026-10-01")
        self.assertEqual(saved["points"]["2026-10-02"], 29.9)
        self.assertIn("2026-10-02", meta["estimated_days"])
        self.assertEqual(saved["points"]["2026-10-14"], 33.27, "неоценённые дни — как были")

    def test_перечень_оценённых_дней_переживает_прогон_без_оценки(self):
        # Оценка не посчиталась — перечень обязан остаться: иначе на следующем
        # прогоне защита снята и биржа перепишет старые оценки.
        # мутация: писать в meta только день этого прогона -> перечень пуст.
        self.seed(dict(self.CLEANED, **{"2026-11-18": 36.4, "2026-11-19": MEDIAN_1119}),
                  estimated_days=["2026-11-18", "2026-11-19"])
        self.serve(market={"marketdata": {"columns": ["SECID", "YIELD"], "data": []}})
        _points, meta, saved = self.run_fetch()
        self.assertEqual(meta["estimated_days"], ["2026-11-18", "2026-11-19"])
        self.assertEqual(saved["points"]["2026-11-18"], 36.4)


class TestExchangeNeedsConfirmation(HyCase):
    """(б) Биржевое число идёт в ряд только с подтверждением соседней оценкой."""

    def test_плавный_уход_биржи_в_ряд_не_возвращается(self):
        # Ряд после чистки, первый прогон с оценкой. Биржа за 21.10–19.11 отдаёт
        # 35,49 -> 41,02 -> 48,22 -> 55,53 -> 70,34 -> 95,99 -> 85,65: всё в коридоре,
        # соседи согласны друг с другом. В ряд идёт только оценка.
        # мутация: дни без оценки рядом проверять правилом соседей -> в ряд
        # возвращаются 35,49…61,39 (правило соседей их пропускает).
        self.seed(self.CLEANED)
        self.serve(market=MARKET_1120)
        points, meta, saved = self.run_fetch()
        self.assertEqual(list(points), ["2026-11-20"])
        after = {d: v for d, v in saved["points"].items() if d >= "2026-10-21"}
        self.assertEqual(list(after), ["2026-11-20"])
        skipped = meta["exchange_skipped"]
        # 16–19.11 — рядом с оценкой 20.11 и опровергнуты ею; 17.11 (121,28) вне
        # коридора и сюда не доходит; всё остальное — без оценки рядом.
        self.assertEqual([r[0] for r in skipped["refuted"]],
                         ["2026-11-16", "2026-11-18", "2026-11-19"])
        self.assertEqual(skipped["unverified"], 17)

    def test_подтверждённое_биржевое_число_закрывает_день_без_оценки(self):
        # Оценка есть за 22.10 (35,0). Биржа: 21.10 — 35,49, 23.10 — 35,22,
        # 26.10 — 35,47, 27.10 — 36,23: в пяти днях от оценки и не дальше 3 п.п. —
        # идут в ряд. 28.10 (шесть дней) подтвердить нечем.
        # мутация: не пускать биржевые числа вовсе -> дни без оценки навсегда пусты.
        self.seed(dict(self.CLEANED, **{"2026-10-22": 35.0}),
                  estimated_days=["2026-10-22"])
        self.serve(market={"marketdata": {"columns": ["SECID", "YIELD"], "data": []}},
                   history_till="2026-10-28")
        points, _meta, saved = self.run_fetch(end="2026-10-28")
        self.assertEqual(points, {"2026-10-21": 35.49, "2026-10-23": 35.22,
                                  "2026-10-26": 35.47, "2026-10-27": 36.23})
        self.assertEqual(saved["points"]["2026-10-22"], 35.0, "оценку переписала биржа")

    def test_расхождение_с_соседней_оценкой_больше_допуска_не_проходит(self):
        # Оценки за 12.11 и 16.11 — 36,2 и 36,9; биржа за 13.11 отдаёт 61,39.
        # мутация: ослабить YIELD_XCHECK_PP до 30 -> 61,39 в ряду.
        self.seed(dict(self.CLEANED, **{"2026-11-12": 36.2, "2026-11-16": 36.9}),
                  estimated_days=["2026-11-12", "2026-11-16"])
        self.serve(market={"marketdata": {"columns": ["SECID", "YIELD"], "data": []}},
                   history_till="2026-11-16")
        points, meta, _ = self.run_fetch(start="2026-11-09", end="2026-11-16")
        self.assertEqual(points, {})
        self.assertIn(["2026-11-13", 61.39, "2026-11-12", 36.2],
                      meta["exchange_skipped"]["refuted"])

    def test_история_до_даты_охраны_собирается_прежними_фильтрами(self):
        # Пересборка с нуля: до 21.10.2026 оценок нет, требовать подтверждения там
        # значило бы потерять весь ряд. Работает коридор (всё, что выше 100 %).
        # мутация: требовать подтверждения с начала истории -> ряд пуст.
        self.serve(market=MARKET_1120)
        points, _meta, _ = self.run_fetch(start="2026-10-01")
        early = {d: v for d, v in points.items() if d < "2026-10-21"}
        self.assertEqual(early, {"2026-10-02": 81.81, "2026-10-05": 74.94,
                                 "2026-10-06": 98.57, "2026-10-07": 86.51})


class TestEstimateFailure(HyCase):
    """(в) Оценка не посчиталась — биржевое число в ряд не идёт, ряд честно стареет."""

    STALE_META = {"method": "constituents", "estimate_asof": "2026-11-17",
                  "estimate_cover_pct": 91.0,
                  "estimate_dropped": {"count": 14, "weight_pct": 8.68, "fence_pct": 92.85},
                  "xcheck_constituents": 33.32, "xcheck_diff_pp": 2.91,
                  "dropped_outliers": [["2026-11-16", 70.34, 56.19]],
                  "estimated_days": ["2026-11-17"]}

    def fail_estimate(self):
        """Котировки есть у 60 бумаг из 144 — как вечером 19.11, только заметнее."""
        rows = YIELDS_1119["marketdata"]["data"][:60]
        self.serve(market={"marketdata": {"columns": ["SECID", "YIELD"], "data": rows}})

    def test_опровергнутое_биржевое_число_в_ряд_не_пишется(self):
        self.seed(dict(self.CLEANED, **{"2026-11-17": 36.9}), **self.STALE_META)
        self.fail_estimate()
        points, meta, saved = self.run_fetch(start="2026-11-12")
        # мутация: при отказе оценки вернуть биржевые точки окна -> 95,99 и 85,65.
        self.assertEqual(points, {})
        self.assertEqual(max(saved["points"]), "2026-11-17")
        self.assertNotIn(EXCHANGE_1118, saved["points"].values())
        # Ряд стареет вслух: статус и записка называют день и число биржи.
        self.assertEqual(meta["status"], "stale")
        self.assertIn("85,65", meta["note"])
        self.assertIn("2026-11-19", meta["note"])
        self.assertIn("2026-11-17", meta["note"])
        self.assertEqual(saved["meta"]["asof"], "2026-11-17")

    def test_поля_прошлой_оценки_из_meta_убираются(self):
        self.seed(dict(self.CLEANED, **{"2026-11-17": 36.9}), **self.STALE_META)
        self.fail_estimate()
        _points, _meta, saved = self.run_fetch(start="2026-11-12")
        meta = saved["meta"]
        # мутация: не называть поля оценки при отказе -> «покрытие 91 %» и
        # «отброшено 14 бумаг» остаются в meta от прошлого прогона.
        for key in ("estimate_cover_pct", "estimate_dropped", "xcheck_constituents",
                    "xcheck_diff_pp", "dropped_outliers"):
            self.assertIsNone(meta.get(key), f"{key} остался от прошлого прогона")
        self.assertIn("покрывают лишь", meta["estimate_error"])
        # Последняя точка ряда по-прежнему оценка, и meta говорит именно это.
        self.assertEqual(meta["method"], "constituents")
        self.assertEqual(meta["estimate_asof"], "2026-11-17")

    def test_нечего_писать_и_нечего_бояться_статус_ok(self):
        # Утро субботы: история кончается пятницей, пятница уже оценена, оценка
        # не посчиталась. Ничего нового у биржи нет — тревожить нечем.
        # мутация: stale при любом отказе оценки -> ложная тревога каждое утро.
        self.seed(dict(self.CLEANED, **{"2026-11-19": MEDIAN_1119}),
                  estimated_days=["2026-11-19"])
        self.fail_estimate()
        points, meta, _ = self.run_fetch(start="2026-11-14")
        self.assertEqual(points, {})
        self.assertEqual(meta["status"], "ok")

    def test_значение_вне_коридора_тоже_считается_неполученным_днём(self):
        # Последний день биржи — 121,28 % (17.11): коридор его срезал, оценки нет.
        self.seed(dict(self.CLEANED, **{"2026-11-16": 36.9}),
                  estimated_days=["2026-11-16"])
        # мутация: считать неполученными только дни внутри коридора -> статус ok
        # при том, что биржа отдала день, которого в ряду нет.
        self.serve(market={"marketdata": {"columns": ["SECID", "YIELD"], "data": []}},
                   history_till="2026-11-17")
        _points, meta, _ = self.run_fetch(start="2026-11-12", end="2026-11-17")
        self.assertEqual(meta["status"], "stale")
        self.assertIn("121,28", meta["note"])

    def test_котировки_выходного_дня_в_ряд_не_идут(self):
        # Утро субботы: блок dataversion называет 21.11 (суббота), котировки —
        # пятничные. Точка выходного дня в ряду — фантом.
        # мутация: писать оценку любым днём -> в ряду суббота 2026-11-21.
        market = dict(MARKET_1120, dataversion={
            "columns": ["data_version", "seqnum", "trade_date", "trade_session_date"],
            "data": [[9021, 1, "2026-11-21", "2026-11-21"]]})
        self.seed(dict(self.CLEANED, **{"2026-11-19": MEDIAN_1119}),
                  estimated_days=["2026-11-19"])
        self.serve(market=market)
        points, meta, _ = self.run_fetch(start="2026-11-14")
        self.assertNotIn("2026-11-21", points)
        self.assertIn("выходным", meta["estimate_error"])

    def test_история_упала_а_оценка_есть_ряд_пополняется(self):
        # Ряд теперь стоит на оценке; отказ ИСТОРИИ индекса его не останавливает.
        # мутация: поднимать отказ истории всегда -> ряд стоит, пока лежит история.
        self.seed(self.CLEANED)
        self.serve(history_fails=True)
        points, meta, _ = self.run_fetch()
        self.assertEqual(points, {"2026-11-19": MEDIAN_1119})
        self.assertEqual(meta["status"], "ok")
        self.assertIn("история индекса не получена", meta["note"])

    def test_ни_истории_ни_оценки_это_отказ(self):
        self.seed(self.CLEANED)
        self.serve(history_fails=True,
                   market={"marketdata": {"columns": ["SECID", "YIELD"], "data": []}})
        with self.assertRaises(self.iss.FetchError):
            self.iss.index_yield(sec="RUCBHYCP", start="2026-11-14", end="2026-11-20")


class TestCleanHySeries(HyCase):
    """ops/clean_hy_series.py: разовая чистка боевого ряда ВДО."""

    # Хвост боевого ряда на 20.11.2026 (копия стора аудита): биржевые числа
    # 21.10–18.11 и оценка прежнего метода 40,40 за 17.11.
    LIVE_TAIL = {"2026-10-19": 32.89, "2026-10-20": 32.64, "2026-10-21": 35.49,
                 "2026-11-02": 41.02, "2026-11-13": 61.39, "2026-11-16": 70.34,
                 "2026-11-17": 40.4, "2026-11-18": 95.99}
    LIVE_META = {"method": "constituents", "estimate_asof": "2026-11-18",
                 "estimate_cover_pct": 91.0, "xcheck_constituents": 33.32,
                 "estimate_dropped": {"count": 14, "weight_pct": 8.68}}

    def setUp(self):
        super().setUp()
        import importlib.util
        from pathlib import Path
        path = Path(__file__).resolve().parents[1] / "ops" / "clean_hy_series.py"
        spec = importlib.util.spec_from_file_location("clean_hy_series", path)
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)

    def script(self, argv):
        # Скрипт печатает, что делает; в выводе набора это шум.
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            return self.mod.main(argv)

    def test_без_apply_ничего_не_меняется(self):
        # мутация: чистить по умолчанию -> показ того, что будет сделано, уже чистит.
        self.seed(self.LIVE_TAIL, **self.LIVE_META)
        before = self.store.load_series(self.SID)["points"]
        self.assertEqual(self.script(["--only", "hy"]), 0)
        self.assertEqual(self.store.load_series(self.SID)["points"], before)
        self.assertFalse(list(self.store.raw_dir().glob("*.before-median*")))

    def test_apply_убирает_биржевые_точки_и_оставляет_копию(self):
        self.seed(self.LIVE_TAIL, **self.LIVE_META)
        self.script(["--apply", "--only", "hy"])
        series = self.store.load_series(self.SID)
        # мутация: чистить весь ряд -> пропадает история до 21.10.
        self.assertEqual(series["points"], {"2026-10-19": 32.89, "2026-10-20": 32.64})
        self.assertEqual(series["meta"]["asof"], "2026-10-20")
        # Поля прошлой оценки описывали бы точки, которых больше нет.
        for key in ("method", "estimate_asof", "estimate_cover_pct", "estimate_dropped",
                    "xcheck_constituents"):
            self.assertIsNone(series["meta"].get(key), key)
        copy = self.store.raw_dir() / "rucbhycp_yield.json.before-median"
        self.assertEqual(json.loads(copy.read_text(encoding="utf-8"))["points"],
                         self.LIVE_TAIL)
        self.assertIn(self.SID, self.store.list_dirty(), "ряд не помечен к выгрузке в зеркало")

    def test_оценки_панели_переживают_чистку(self):
        # Скрипт запускают и ПОСЛЕ первого прогона нового кода: записанную им
        # медиану трогать нельзя.
        # мутация: удалять всё с 21.10 -> пропадает и свежая оценка.
        self.seed(dict(self.LIVE_TAIL, **{"2026-11-20": 36.76}),
                  estimated_days=["2026-11-20"], method="constituents",
                  estimate_asof="2026-11-20", estimate_cover_pct=99.8)
        self.script(["--apply", "--only", "hy"])
        series = self.store.load_series(self.SID)
        self.assertEqual(sorted(series["points"]),
                         ["2026-10-19", "2026-10-20", "2026-11-20"])
        self.assertEqual(series["meta"]["estimated_days"], ["2026-11-20"])
        self.assertEqual(series["meta"]["estimate_cover_pct"], 99.8)

    def test_повторный_запуск_не_затирает_первую_копию(self):
        self.seed(self.LIVE_TAIL, **self.LIVE_META)
        self.script(["--apply", "--only", "hy"])
        self.store.upsert_points(self.SID, {"2026-11-19": 85.65})
        self.script(["--apply", "--only", "hy"])
        first = self.store.raw_dir() / "rucbhycp_yield.json.before-median"
        self.assertEqual(json.loads(first.read_text(encoding="utf-8"))["points"],
                         self.LIVE_TAIL, "первая копия — единственная улика — затёрта")
        self.assertTrue((self.store.raw_dir() / "rucbhycp_yield.json.before-median.2").exists())

    def test_затравочные_нули_аукционов_убираются_а_провалы_остаются(self):
        """24.06 и 08.07.2026: на доске PACT строк нет вовсе (живой запрос 20.11.2026)
        — аукционов не было. 15.07 и 30.09 — строка с нулём: аукцион шёл и провалился.

        мутация: убирать все нули -> пропадают настоящие провалы 15.07 и 30.09.
        """
        self.store.upsert_points("ofz_auctions", {
            "2026-06-17": 120.04, "2026-06-24": 0.0, "2026-07-01": 10.3649,
            "2026-07-08": 0.0, "2026-07-15": 0.0, "2026-09-30": 0.0})
        self.script(["--only", "auctions"])
        self.assertIn("2026-06-24", self.store.load_series("ofz_auctions")["points"])
        self.script(["--apply", "--only", "auctions"])
        self.assertEqual(sorted(self.store.load_series("ofz_auctions")["points"]),
                         ["2026-06-17", "2026-07-01", "2026-07-15", "2026-09-30"])
        self.assertTrue((self.store.raw_dir() / "ofz_auctions.json.before-median").exists())

    def test_день_с_объёмом_скрипт_аукционов_не_трогает(self):
        # Если за «фантомный» день в ряду лежит объём, это уже не затравочный ноль.
        self.store.upsert_points("ofz_auctions", {"2026-06-24": 55.5})
        self.script(["--apply", "--only", "auctions"])
        self.assertEqual(self.store.load_series("ofz_auctions")["points"],
                         {"2026-06-24": 55.5})

    def test_после_чистки_новый_код_биржевые_точки_не_возвращает(self):
        """Чистка и фетчер вместе: то, что убрано, через окно догрузки не приходит.

        мутация: окно догрузки пишет дни без подтверждения -> ряд снова с мусором.
        """
        self.seed(self.LIVE_TAIL, **self.LIVE_META)
        self.script(["--apply", "--only", "hy"])
        self.serve(market=MARKET_1120)
        sid, points, meta = self.iss.index_yield(sec="RUCBHYCP", end="2026-11-20")
        saved = self.store.upsert_points(sid, points, meta)
        self.assertEqual(sorted(d for d in saved["points"] if d >= "2026-10-21"),
                         ["2026-11-20"])
        self.assertEqual(saved["points"]["2026-11-20"], 36.76)


if __name__ == "__main__":
    unittest.main()
