"""Инварианты payload (docs/CONTRACT.md §3) — то, на что смотрит фронт.

data.json публикуется без ревью и читается с телефона по мобильной сети. Поэтому
проверяется ровно то, что ломает страницу молча: пропавший обязательный ключ,
неожиданный тип, NaN/Infinity в JSON (спецификация их не знает — JSON.parse падает),
превышение лимита 250 КБ и разъехавшаяся история.

Payload собирается из тех же модулей, что и в проде, на замороженных панелях:
состояния — из tests/fixtures/panel_small.json, ядро — из синтетики test_core.
"""

import json
import math
import os
import re
import unittest
from datetime import datetime, timezone
from tempfile import TemporaryDirectory

from tests import need, panel_small

NOW = datetime(2026, 8, 11, 16, 5, 12, tzinfo=timezone.utc)
GENERATED_AT = "2026-08-11T16:05:12Z"
ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTHS = 60


def month_labels(n=MONTHS, year=2021):
    out = []
    for i in range(n):
        y, m = year + i // 12, i % 12 + 1
        last = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
        if m == 2 and y % 4 == 0:
            last = 29
        out.append(f"{y:04d}-{m:02d}-{last:02d}")
    return out


def alternating(last_positive=True):
    sign = 1.0 if last_positive else -1.0
    return [sign * (1.0 if i % 2 == (MONTHS - 1) % 2 else -1.0) for i in range(MONTHS)]


def walk_numbers(node, path="$"):
    """Все числа payload с путями — для проверки конечности."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from walk_numbers(value, f"{path}.{key}")
    elif isinstance(node, (list, tuple)):
        for i, value in enumerate(node):
            yield from walk_numbers(value, f"{path}[{i}]")
    elif isinstance(node, float):
        yield path, node


class PayloadCase(unittest.TestCase):
    def setUp(self):
        self.publish = need(self, "pipeline.publish", "build_payload", "dumps",
                            "fit_size", "MAX_BYTES")
        self.constants = need(self, "pipeline.lib.constants", "SCHEMA_VERSION",
                              "MONITOR_TIERS", "TIER_NOTES", "CELL_STATS")
        core_mod = need(self, "pipeline.compute.core", "compute_core")
        states_mod = need(self, "pipeline.compute.states", "compute_states")
        monitors_mod = need(self, "pipeline.compute.monitors", "build_monitors")
        self.store_mod = need(self, "pipeline.lib.store", "upsert_points")

        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prev = os.environ.get("STATE_DIR")
        os.environ["STATE_DIR"] = self.tmp.name
        self.addCleanup(self._restore_env)

        panel = panel_small()
        self.expect = panel["expect"]
        self.states = states_mod.compute_states({"dates": panel["dates"],
                                                 "cols": panel["cols"]})
        month_panel = {"dates": month_labels(), "cols": {
            "imoex": [3000.0 + 10.0 * i for i in range(MONTHS)],
            "usd_mom63": alternating(True),
            "slope_10_2": alternating(False),
            "urals_rub_gap": alternating(False),
        }}
        self.core = core_mod.compute_core(month_panel)
        self.monitors = monitors_mod.build_monitors({}, now=NOW)

        # Стор для истории: два ряда, которых достаточно для history/daily.json.
        self.store_mod.upsert_points("imoex", {"2026-08-10": 2293.32,
                                               "2026-08-11": 2301.0})
        self.store_mod.upsert_points("rgbi", {"2026-08-10": 104.12,
                                              "2026-08-11": 104.40})

        self.payload = self.publish.build_payload(
            core=self.core, states=self.states, monitors=self.monitors,
            sources={"iss": {"asof": "2026-08-11", "fetched_at": GENERATED_AT,
                             "status": "ok", "lag_min": 12}},
            events=[{"ts": GENERATED_AT, "kind": "state_change", "severity": "info",
                     "text": "облигационный флаг включён"}],
            mode="daily", asof=self.expect["last_date"], generated_at=GENERATED_AT)

    def _restore_env(self):
        if self.prev is None:
            os.environ.pop("STATE_DIR", None)
        else:
            os.environ["STATE_DIR"] = self.prev


class TestRequiredShape(PayloadCase):
    def test_top_level_keys(self):
        # мутация: переименовать любой ключ -> фронт получит undefined и покажет
        # пустую страницу без единого сообщения об ошибке.
        for key in ("schema", "generated_at", "run_mode", "asof_trading_day",
                    "stale_after_minutes", "verdict", "core", "states", "monitors",
                    "sources", "events"):
            self.assertIn(key, self.payload)

    def test_types(self):
        self.assertEqual(self.payload["schema"], self.constants.SCHEMA_VERSION)
        self.assertIsInstance(self.payload["schema"], int)
        self.assertTrue(ISO_Z.match(self.payload["generated_at"]))
        self.assertTrue(DAY.match(self.payload["asof_trading_day"]))
        self.assertIsInstance(self.payload["stale_after_minutes"], int)
        self.assertIsInstance(self.payload["monitors"], list)
        self.assertIsInstance(self.payload["events"], list)
        self.assertIsInstance(self.payload["sources"], dict)
        self.assertIn(self.payload["run_mode"],
                      ("intraday", "daily", "weekly", "monthly", "event", "bootstrap"))

    def test_verdict_block(self):
        verdict = self.payload["verdict"]
        for key in ("cell_code", "cell_label", "cell_stats", "rule", "core_value",
                    "core_label"):
            self.assertIn(key, verdict)
        # Вердикт обязан говорить о ТОЙ ЖЕ ячейке, что и машина состояний.
        # мутация: собрать вердикт по другому порядку бит -> заголовок панели
        # разойдётся с лентой состояний под ним.
        self.assertEqual(verdict["cell_code"], self.expect["cell_code"])
        stats = self.constants.CELL_STATS[tuple(self.expect["cell_key"])]
        # Все ПЯТЬ витринных величин, а не только среднее: витрина показывает медиану,
        # худший месяц и долю плюсовых отдельными тайлами, и до 13.08.2026 контракт
        # не проверял ни одну из них — выпадение поля из publish._verdict читалось бы
        # на панели как прочерк, то есть «данных нет», при исправных константах.
        # Среднее по ячейке — хвостовая статистика, поэтому именно спутники дают смысл.
        for field in ("mean_fwd1m_pct", "median_fwd1m_pct", "worst_pct", "hit", "n"):
            with self.subTest(field=field):
                self.assertIn(field, verdict["cell_stats"])
                self.assertEqual(verdict["cell_stats"][field], stats[field])
        # Лучший месяц ездит в витрине ради симметрии подписи «от … до …».
        self.assertEqual(verdict["cell_stats"].get("best_pct"), stats["best_pct"])
        self.assertEqual(verdict["core_value"], self.core["value"])
        self.assertTrue(verdict["rule"].strip())

    def test_core_block(self):
        core = self.payload["core"]
        for key in ("value", "sign", "sign_since", "components", "series", "health"):
            self.assertIn(key, core)
        self.assertIn(core["sign"], (-1, 0, 1))
        for comp in core["components"]:
            for key in ("id", "label", "z", "raw", "raw_fmt", "tier", "weight",
                        "mechanism"):
                self.assertIn(key, comp, comp.get("id"))
            self.assertIn(comp["tier"], ("A", "B"), comp["id"])
            # Механизм обязателен: число без механизма — это гадание, а панель
            # обещает объяснимость (CONTRACT §3).
            self.assertTrue(comp["mechanism"].strip(), comp["id"])
        self.assertIn(core["health"]["status"], ("ok", "warn", "dead"))

    def test_states_block(self):
        states = self.payload["states"]
        for key in ("current", "distances", "active_signals", "cells", "series"):
            self.assertIn(key, states)
        self.assertIn("since", states["current"])
        for dist in states["distances"]:
            for key in ("id", "text", "value", "threshold", "gap_pct"):
                self.assertIn(key, dist)

    def test_monitor_tiles(self):
        ids = [t["id"] for t in self.payload["monitors"]]
        self.assertEqual(len(ids), len(set(ids)), "дубликаты id тайлов")
        allowed_tiers = set(self.constants.TIER_NOTES)
        for tile in self.payload["monitors"]:
            for key in ("id", "title", "tier", "status", "asof", "headline",
                        "payload", "note"):
                self.assertIn(key, tile, tile.get("id"))
            # мутация: завести тир вне множества (или потерять пометку dead) ->
            # опровергнутый предиктор поедет на панель как обычный сигнал.
            self.assertIn(tile["tier"], allowed_tiers, tile["id"])
            self.assertEqual(tile["tier"], self.constants.MONITOR_TIERS[tile["id"]])
            self.assertIn(tile["status"], ("ok", "stale", "missing", "error"), tile["id"])
            self.assertTrue(tile["note"].strip(), tile["id"])
            if tile["tier"] == "dead":
                self.assertIn("опровергнут", tile["note"].lower(), tile["id"])

    def test_events_shape(self):
        for event in self.payload["events"]:
            self.assertTrue(ISO_Z.match(event["ts"]))
            self.assertIn(event["severity"], ("info", "warn"))
            self.assertTrue(event["text"].strip())


class TestSerialisation(PayloadCase):
    def test_no_nan_or_infinity(self):
        # JSON не знает NaN/Infinity: json.dumps их пишет, а JSON.parse на них
        # падает — страница гаснет целиком.
        # мутация: пустить float('nan') из парсера в payload -> allow_nan=False
        # бросит ValueError прямо здесь.
        json.dumps(self.payload, allow_nan=False)
        for path, value in walk_numbers(self.payload):
            self.assertTrue(math.isfinite(value), f"нечисло в {path}: {value}")
        raw = self.publish.dumps(self.payload).decode("utf-8")
        self.assertNotIn("NaN", raw)
        self.assertNotIn("Infinity", raw)

    def test_the_nan_guard_actually_guards(self):
        # Страховка на сам тест: проверка обязана краснеть на подсунутом NaN.
        poisoned = dict(self.payload)
        poisoned["core"] = dict(poisoned["core"], value=float("nan"))
        with self.assertRaises(ValueError):
            json.dumps(poisoned, allow_nan=False)

    def test_size_limit(self):
        data = self.publish.dumps(self.payload)
        self.assertEqual(self.publish.MAX_BYTES, 250 * 1024)
        # мутация: положить дневную ленту состояний с 2004 года -> +100 КБ и
        # первая отрисовка на мобильной сети уезжает за секунды.
        self.assertLessEqual(len(data), self.publish.MAX_BYTES,
                             f"payload {len(data)} Б больше лимита")

    def test_utf8_without_escapes(self):
        raw = self.publish.dumps(self.payload)
        self.assertIn("умеренный".encode("utf-8"), raw)
        self.assertNotIn(b"\\u0443", raw)   # ensure_ascii=False экономит ~5x

    def test_trim_ladder_cuts_decorations_first(self):
        # Лестница обрезки: сначала украшения (ряды тайлов, спарклайны), потом
        # глубина истории, и только в конце — события.
        # мутация: начать с событий -> панель потеряет журнал раньше, чем
        # необязательные картинки.
        fat = json.loads(json.dumps(self.payload))
        fat["monitors"][0]["payload"]["series"] = [[f"2026-{i % 12 + 1:02d}-01", i]
                                                   for i in range(4000)]
        data, cut = self.publish.fit_size(fat, limit=20 * 1024)
        self.assertIn("monitor_series", cut)
        self.assertLessEqual(len(data), 20 * 1024)
        self.assertEqual(len(fat["events"]), len(self.payload["events"]))


class TestHistoryMonotonic(PayloadCase):
    def series_lists(self, node, path="$"):
        """Все списки пар [дата, значение] в объекте."""
        found = []
        if isinstance(node, dict):
            for key, value in node.items():
                found += self.series_lists(value, f"{path}.{key}")
        elif isinstance(node, list):
            pairs = [row for row in node
                     if isinstance(row, list) and len(row) == 2
                     and isinstance(row[0], str) and DAY.match(row[0])]
            if pairs and len(pairs) == len(node):
                found.append((path, node))
            else:
                for i, value in enumerate(node):
                    found += self.series_lists(value, f"{path}[{i}]")
        return found

    def test_every_dated_series_is_strictly_increasing(self):
        # мутация: собрать ряд из dict без sorted() -> порядок ключей случайный,
        # и график на панели превращается в клубок.
        found = self.series_lists(self.payload)
        self.assertTrue(found, "в payload не нашлось ни одного датированного ряда")
        for path, rows in found:
            days = [row[0] for row in rows]
            self.assertEqual(days, sorted(days), f"ряд не отсортирован: {path}")
            self.assertEqual(len(set(days)), len(days), f"дубликаты дат: {path}")

    def test_history_daily_is_sorted_and_thinned(self):
        history = self.publish.build_history(self.store_mod, self.payload)
        self.assertIn("imoex", history)
        for key in ("imoex", "rgbi"):
            days = [row[0] for row in history[key]]
            self.assertEqual(days, sorted(days), key)
        self.assertEqual(history["asof"], self.payload["asof_trading_day"])

    def test_states_ribbon_dates_are_inside_the_panel(self):
        series = self.payload["states"]["series"]
        self.assertEqual(series[-1][0], self.expect["last_date"])
        self.assertTrue(all(day <= self.expect["last_date"] for day, _code in series))


class TestMonitorRegistry(unittest.TestCase):
    def test_coverage_selfcheck_is_clean(self):
        # Модуль сам сверяет тайлы с реестром тиров (run.py --mode selftest).
        monitors_mod = need(self, "pipeline.compute.monitors", "check_coverage")
        self.assertEqual(monitors_mod.check_coverage(), [])


if __name__ == "__main__":
    unittest.main()
