"""Защёлки публикации: что нельзя затереть и что нельзя выпустить в бакет.

Схема data.json проверяется в test_schema.py — здесь только предохранители, каждый
из которых стоил находки аудита:

* history/daily.json собирался ПОСЛЕ обрезки, и «полная история» молча становилась
  такой же прореженной, как витрина (лестница режет payload на месте);
* одна неудачная GET-попытка стирала накопленную history/monitors.json целиком:
  «объекта нет» и «прочитать не удалось» обрабатывались одинаково;
* пустое ядро/вердикт (неполный стор) публиковались поверх рабочей витрины при
  нулевом коде возврата и молчащем мониторинге.

Сеть не трогаем: r2 подменяется словарём-бакетом.
"""

import json
import os
import unittest
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from tests import need

ASOF = "2026-08-11"


def pairs(n, start="1990-01-01"):
    d0 = date.fromisoformat(start)
    return [[(d0 + timedelta(days=i)).isoformat(), round(0.001 * i, 4)] for i in range(n)]


class PublishCase(unittest.TestCase):
    def setUp(self):
        self.publish = need(self, "pipeline.publish", "publish", "build_payload",
                            "check_payload", "fit_size")
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.prev = os.environ.get("STATE_DIR")
        os.environ["STATE_DIR"] = self.tmp.name
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self.prev is None:
            os.environ.pop("STATE_DIR", None)
        else:
            os.environ["STATE_DIR"] = self.prev

    def payload(self, core=0.68, states=None, monitors=None, **kw):
        return self.publish.build_payload(
            core={"value": core, "sign": 1, "components": [], **kw.pop("core_extra", {})},
            states=states if states is not None else {"current": {"trend": 0, "vol": 1,
                                                                  "bond": 1}},
            monitors=monitors or [], sources={}, mode="daily", asof=ASOF, **kw)

    def local(self, name):
        path = self.root / "out" / name
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


class FakeBucket:
    """Бакет в памяти: GET/PUT ходят сюда, отказ чтения включается флагом."""

    def __init__(self, publish_mod, objects=None, read_fails=False):
        self.pub = publish_mod
        self.objects = dict(objects or {})
        self.read_fails = read_fails

    def install(self, case):
        r2 = self.pub.r2
        patchers = [
            mock.patch.object(r2, "configured", lambda: True),
            mock.patch.object(r2, "put", self._put),
            mock.patch.object(r2, "put_json", self._put_json),
            mock.patch.object(r2, "get_json", self._get_json),
            mock.patch.object(self.pub.lease, "can_write", lambda: (True, "vps")),
            mock.patch.object(self.pub.lease, "refresh_heartbeat", lambda **kw: None),
        ]
        for p in patchers:
            p.start()
            case.addCleanup(p.stop)
        return self

    def _put(self, key, data, content_type="", cache_control=None, verify=True):
        self.objects[key] = json.loads(bytes(data).decode("utf-8"))
        return {"key": key}

    def _put_json(self, key, obj, cache_control=None, verify=True):
        self.objects[key] = obj
        return {"key": key}

    def _get_json(self, key):
        if self.read_fails:
            raise self.pub.r2.R2Error(f"GET {key}: не удалось за 3 попыток (HTTP 503)")
        return self.objects.get(key)


class TestHistoryIsBuiltBeforeTrim(PublishCase):
    def test_full_history_survives_the_trim_ladder(self):
        # мутация: собрать историю после fit_size -> в history/daily.json уезжает
        # прореженный ряд, а панель называет этот объект «полной историей».
        payload = self.payload(core_extra={"series": pairs(15000)},
                               states={"current": {"trend": 0, "vol": 1, "bond": 1},
                                       "series": pairs(15000)})
        res = self.publish.publish(payload, "daily", store=None, dry_run=True)
        self.assertIn("core_series", res["trimmed"])
        shop = self.local("data.json")
        hist = self.local("history/daily.json")
        self.assertLess(len(shop["core"]["series"]), 1000)
        self.assertEqual(len(hist["core"]), 15000)
        self.assertEqual(len(hist["states"]), 15000)


class TestMonitorsHistory(PublishCase):
    def prev_history(self, rows=29):
        return {tid: [{"ts": f"2026-06-{i + 1:02d}T00:00:00Z", "asof": f"2026-06-{i + 1:02d}",
                       "headline": f"заголовок {i}", "status": "ok"} for i in range(rows)]
                for tid in ("rvi", "lqdt")}

    def tiles(self):
        return [{"id": "rvi", "status": "ok", "asof": ASOF, "headline": "новый", "payload": {}},
                {"id": "lqdt", "status": "ok", "asof": ASOF, "headline": "новый", "payload": {}}]

    def test_appends_when_bucket_is_readable(self):
        bucket = FakeBucket(self.publish,
                            {"history/monitors.json": self.prev_history()}).install(self)
        self.publish.publish(self.payload(monitors=self.tiles()), "daily", store=None)
        got = bucket.objects["history/monitors.json"]
        self.assertEqual({k: len(v) for k, v in got.items()}, {"rvi": 30, "lqdt": 30})

    def test_read_failure_never_wipes_the_ledger(self):
        # мутация: считать неудачное чтение отсутствием объекта -> одна 503 стирает
        # ленту заголовков (до 200 записей × 14 тайлов) без права восстановления.
        bucket = FakeBucket(self.publish, {"history/monitors.json": self.prev_history()},
                            read_fails=True).install(self)
        res = self.publish.publish(self.payload(monitors=self.tiles()), "daily", store=None)
        got = bucket.objects["history/monitors.json"]
        self.assertEqual({k: len(v) for k, v in got.items()}, {"rvi": 29, "lqdt": 29})
        self.assertTrue(any("history/monitors.json" in e for e in res["errors"]))
        self.assertTrue(res["ok"], "витрину это ронять не должно")

    def test_identical_history_is_not_rewritten(self):
        bucket = FakeBucket(self.publish).install(self)
        first = self.payload(monitors=self.tiles())
        self.publish.publish(first, "daily", store=None)
        before = json.dumps(bucket.objects["history/monitors.json"], ensure_ascii=False)
        res = self.publish.publish(self.payload(monitors=self.tiles()), "daily", store=None)
        self.assertEqual(json.dumps(bucket.objects["history/monitors.json"],
                                    ensure_ascii=False), before)
        self.assertNotIn("history/monitors.json", res["objects"])


class TestIntegrityGate(PublishCase):
    def test_empty_core_is_not_published_over_a_good_panel(self):
        # мутация: публиковать как раньше -> авария конвейера доезжает до читателя
        # словами «нет данных» и неотличима от рыночного состояния.
        self.publish.publish(self.payload(), "daily", store=None, dry_run=True)
        good = self.local("data.json")
        broken = self.publish.build_payload(core={"value": None}, states={"current": {}},
                                            monitors=[], sources={}, mode="daily", asof=ASOF)
        res = self.publish.publish(broken, "daily", store=None, dry_run=True)
        self.assertFalse(res["ok"])
        self.assertTrue(res["integrity"])
        self.assertFalse(res["published"])
        self.assertEqual(self.local("data.json"), good, "рабочая витрина обязана уцелеть")
        self.assertIsNotNone(self.local("data.json.rejected"))

    def test_first_install_still_gets_a_panel_but_the_run_is_red(self):
        broken = self.publish.build_payload(core={"value": None}, states={"current": {}},
                                            monitors=[], sources={}, mode="daily", asof=ASOF)
        res = self.publish.publish(broken, "daily", store=None, dry_run=True)
        self.assertFalse(res["ok"])
        self.assertIsNotNone(self.local("data.json"))

    def test_runner_without_local_copy_asks_the_bucket_first(self):
        # У фолбэка GHA STATE_DIR пустой каждый прогон: без этого вопроса подмена
        # писателя кладёт «нет данных» поверх живой витрины ровно в аварии.
        bucket = FakeBucket(self.publish, {"data.json": self.payload()}).install(self)
        broken = self.publish.build_payload(core={"value": None}, states={"current": {}},
                                            monitors=[], sources={}, mode="daily", asof=ASOF)
        res = self.publish.publish(broken, "daily", store=None)
        self.assertFalse(res["ok"])
        self.assertFalse(res["published"])
        self.assertEqual(bucket.objects["data.json"]["core"]["value"], 0.68)

    def test_good_payload_passes(self):
        self.assertEqual(self.publish.check_payload(self.payload()), [])
        res = self.publish.publish(self.payload(), "daily", store=None, dry_run=True)
        self.assertTrue(res["ok"])
        self.assertEqual(res["integrity"], [])


class TestOversizeIsLoud(PublishCase):
    def test_flag_is_raised_when_the_ladder_cannot_help(self):
        # Раздут блок, которого в лестнице нет (sources): резать нечего, и раньше
        # об этом знал только тот, кто читает journald руками.
        big = {f"src{i}": {"asof": ASOF, "note": "x" * 400, "status": "ok"}
               for i in range(900)}
        payload = self.publish.build_payload(
            core={"value": 0.68, "sign": 1}, states={"current": {"trend": 0, "vol": 1,
                                                                 "bond": 1}},
            monitors=[], sources=big, mode="daily", asof=ASOF)
        res = self.publish.publish(payload, "daily", store=None, dry_run=True)
        self.assertGreater(res["bytes"], self.publish.MAX_BYTES)
        self.assertTrue(res["oversize"])
        self.assertEqual(res["limit"], self.publish.MAX_BYTES)

    def test_normal_payload_is_not_flagged(self):
        res = self.publish.publish(self.payload(), "daily", store=None, dry_run=True)
        self.assertFalse(res["oversize"])


if __name__ == "__main__":
    unittest.main()
