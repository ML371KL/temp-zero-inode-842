"""ops/backfill_hy_median.py: досчёт ряда ВДО медианой состава по истории биржи.

С 20.11.2026 значение дня у ВДО — взвешенная медиана доходностей бумаг индекса, но
живой расчёт видит только сегодняшние котировки. После чистки ряда в нём месячная
дыра, а до неё — числа прежних методов; тайл из-за этого не показывает перцентиль за
год и изменение за 21 день. Здесь закреплено, что досчёт:
* берёт торговые дни из истории самого индекса (рабочие субботы бывают),
  листает ответы ISS по курсору, предпочитает основной режим TQ*, ноль считает
  «сделок не было»;
* не считает день, где котировки закрытия покрывают меньше 90 % веса;
* пишет только свой период, убирает там числа другого метода, вносит дни в
  meta.estimated_days, кладёт копию, при повторе ничего не меняет и отказывается
  писать, если сеть отдала меньше 90 % дней.

В сеть не ходим: подменяется http.get_bytes.
"""

import contextlib
import importlib.util
import io
import json
import os
import re
import unittest
from tempfile import TemporaryDirectory
from unittest import mock
from urllib.parse import parse_qs, urlparse

from tests import ROOT, need


def load_module():
    path = ROOT / "ops" / "backfill_hy_median.py"
    spec = importlib.util.spec_from_file_location("backfill_hy_median", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


DAYS = ["2025-10-31", "2025-11-01", "2025-11-05"]       # 01.11.2025 — рабочая суббота
WEIGHTS = {"A": 40.0, "B": 35.0, "C": 25.0}
# Доходности закрытия по бумагам: у B первой строкой — внебиржевой режим с мусором,
# основной режим TQCB ниже; у C 05.11 ноль — «сделок не было».
CLOSES = {
    "A": [["2025-10-31", "TQCB", 20.0], ["2025-11-01", "TQCB", 21.0],
          ["2025-11-05", "TQCB", 22.0]],
    "B": [["2025-10-31", "PSAU", 99.0], ["2025-10-31", "TQCB", 30.0],
          ["2025-11-01", "TQIR", 31.0]],
    "C": [["2025-10-31", "TQCB", 40.0], ["2025-11-01", "TQCB", 41.0],
          ["2025-11-05", "TQCB", 0]],
}


def _page(block, columns, rows, query, size=2):
    """Ответ ISS страницами по size строк с блоком курсора, как у настоящей биржи."""
    start = int((query.get("start") or ["0"])[0])
    return {block: {"columns": columns, "data": rows[start:start + size]},
            block + ".cursor": {"columns": ["INDEX", "TOTAL", "PAGESIZE"],
                                "data": [[start, len(rows), size]]}}


class BackfillCase(unittest.TestCase):
    def setUp(self):
        self.http = need(self, "pipeline.lib.http", "get_bytes", "FetchError")
        need(self, "pipeline.fetch.iss", "_weighted_median", "_url")
        self.store = need(self, "pipeline.lib.store", "upsert_points", "load_series")
        self.mod = load_module()
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        prev = os.environ.get("STATE_DIR")
        os.environ["STATE_DIR"] = self.tmp.name
        self.addCleanup(lambda: os.environ.pop("STATE_DIR", None) if prev is None
                        else os.environ.__setitem__("STATE_DIR", prev))
        self.asked = []

    def serve(self, broken=()):
        def responder(url):
            self.asked.append(url)
            parsed = urlparse(url)
            query = parse_qs(parsed.query)
            path = parsed.path
            if "/analytics/" in path:
                day = query["date"][0]
                if day in broken:
                    raise self.http.FetchError("HTTP 503", url=url)
                rows = [["RUCBHYCP", day, s, s, w] for s, w in WEIGHTS.items()]
                body = _page("analytics", ["indexid", "tradedate", "ticker", "secids",
                                           "weight"], rows, query)
            elif "/markets/index/securities/" in path:
                body = _page("history", ["TRADEDATE"], [[d] for d in DAYS], query)
            elif "/markets/bonds/securities/" in path:
                sec = path.rsplit("/", 1)[-1].split(".")[0]
                body = _page("history", ["TRADEDATE", "BOARDID", "YIELDCLOSE"],
                             CLOSES[sec], query)
            else:
                raise AssertionError("неожиданный запрос %s" % url)
            return json.dumps(body).encode("utf-8")
        patcher = mock.patch.object(self.http, "get_bytes",
                                    side_effect=lambda url, **kw: responder(url))
        patcher.start()
        self.addCleanup(patcher.stop)

    def compute(self, **kw):
        return self.mod.compute("2025-10-31", "2025-11-05", workers=1,
                                say=lambda *_a: None, **kw)


class ComputeCase(BackfillCase):
    def test_медиана_по_закрытию_и_торговому_календарю(self):
        self.serve()
        result = self.compute()
        self.assertEqual(result["trading_days"], DAYS, "суббота 01.11 — торговый день")
        # 31.10: 20 (40 %), 30 (35 %), 40 (25 %) — половина веса набирается на 30.
        # мутация: брать первую строку бумаги -> у B 99 из внебиржевого режима, медиана 40.
        self.assertEqual(result["days"]["2025-10-31"]["value"], 30.0)
        self.assertEqual(result["days"]["2025-10-31"]["cover_pct"], 100.0)
        self.assertEqual(result["days"]["2025-11-01"]["value"], 31.0)

    def test_день_с_малым_покрытием_не_считается(self):
        self.serve()
        result = self.compute()
        # 05.11: у B строки нет, у C ноль — котировки есть у 40 % веса.
        # мутация: считать ноль доходностью -> день посчитан с покрытием 65 %.
        self.assertNotIn("2025-11-05", result["days"])
        self.assertIn("40.0 % веса", result["skipped"]["2025-11-05"])

    def test_ответы_листаются_по_курсору(self):
        self.serve()
        self.compute()
        # Состав из трёх бумаг отдаётся страницами по две: на каждый день два запроса.
        # мутация: брать одну страницу -> бумага C пропадает, покрытие 75 %.
        comp = [u for u in self.asked if "/analytics/" in u and "date=2025-10-31" in u]
        self.assertEqual(len(comp), 2)
        self.assertTrue(any(re.search(r"[?&]start=2\b", u) for u in comp))

    def test_отказ_состава_за_день_не_роняет_расчёт(self):
        self.serve(broken=("2025-11-01",))
        result = self.compute()
        self.assertIn("2025-10-31", result["days"])
        self.assertIn("состав не отдан", result["skipped"]["2025-11-01"])


class ApplyCase(BackfillCase):
    SID = "rucbhycp_yield"

    PERIOD = ["2025-10-31", "2025-11-01", "2025-11-05", "2025-11-06", "2025-11-07",
              "2025-11-10", "2025-11-11", "2025-11-12", "2025-11-13", "2025-11-14"]
    MEDIAN = {"2025-10-31": 30.0, "2025-11-01": 31.0, "2025-11-06": 30.5,
              "2025-11-07": 30.6, "2025-11-10": 30.7, "2025-11-11": 30.8,
              "2025-11-12": 30.9, "2025-11-13": 31.1, "2025-11-14": 31.2}

    def setUp(self):
        super().setUp()
        # Результат расчёта: 9 дней из 10, 05.11 не посчитан (мало котировок).
        self.result = {"sec": "RUCBHYCP", "since": "2025-10-31", "until": "2025-11-14",
                       "trading_days": self.PERIOD,
                       "days": {d: {"value": v, "cover_pct": 99.0, "count": 140,
                                    "no_quote": 4, "composition_day": d}
                                for d, v in self.MEDIAN.items()},
                       "skipped": {"2025-11-05": "котировки закрытия покрывают 40.0 % веса"}}
        self.result_path = os.path.join(self.tmp.name, "hy.json")
        with open(self.result_path, "w", encoding="utf-8") as fh:
            json.dump(self.result, fh)
        # Ряд до досчёта: точка до периода, биржевые числа внутри (31.10 — мусор,
        # 05.11 — число, за которое медиана не посчиталась), живая оценка после.
        self.store.upsert_points(self.SID, {"2025-10-30": 33.0, "2025-10-31": 86.51,
                                            "2025-11-05": 45.0, "2026-11-19": 35.69},
                                 {"estimated_days": ["2026-11-19"],
                                  "fetched_at": "2026-11-20T20:55:40+00:00"})

    def run_script(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = self.mod.main(["apply", self.result_path] + list(argv))
        return code, out.getvalue()

    def series(self):
        return self.store.load_series(self.SID)

    def test_без_apply_ничего_не_пишется(self):
        code, out = self.run_script()
        self.assertEqual(code, 0)
        self.assertEqual(self.series()["points"]["2025-10-31"], 86.51)
        self.assertIn("заменить 1, добавить 8, убрать 1", out)

    def test_период_переписан_медианой_вне_периода_ничего(self):
        self.run_script("--apply")
        s = self.series()
        self.assertEqual(s["points"]["2025-10-31"], 30.0)
        self.assertEqual(s["points"]["2025-11-01"], 31.0)
        # мутация: оставлять в периоде непосчитанные дни -> 45,0 другого метода в ряду.
        self.assertNotIn("2025-11-05", s["points"])
        self.assertEqual(s["points"]["2025-10-30"], 33.0, "до периода не трогается")
        self.assertEqual(s["points"]["2026-11-19"], 35.69, "живая оценка не трогается")
        # мутация: не вносить дни в перечень -> окно догрузки перепишет их биржей.
        self.assertEqual(s["meta"]["estimated_days"], sorted(self.MEDIAN) + ["2026-11-19"])
        self.assertEqual(s["meta"]["fetched_at"], "2026-11-20T20:55:40+00:00",
                         "досчёт — не опрос источника")
        self.assertTrue((self.store.raw_dir()
                         / "rucbhycp_yield.json.before-hy-backfill").exists())

    def test_повтор_ничего_не_меняет(self):
        self.run_script("--apply")
        _code, out = self.run_script("--apply")
        self.assertIn("заменить 0, добавить 0, убрать 0", out)
        self.assertIn("менять нечего", out)

    def test_мало_посчитанных_дней_это_отказ(self):
        # Посчитано 9 дней из 12 (75 %) — сеть подвела, ряд не трогаем.
        self.result["trading_days"] = self.PERIOD + ["2025-11-17", "2025-11-18"]
        with open(self.result_path, "w", encoding="utf-8") as fh:
            json.dump(self.result, fh)
        code, out = self.run_script("--apply")
        self.assertEqual(code, 2)
        self.assertIn("не пишу", out)
        self.assertEqual(self.series()["points"]["2025-10-31"], 86.51)


if __name__ == "__main__":
    unittest.main()
