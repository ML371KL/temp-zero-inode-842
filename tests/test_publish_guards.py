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

    def _get(self, key):
        """Сырое тело объекта: манифест зеркала читается именно так, а не get_json."""
        if self.read_fails:
            raise self.pub.r2.R2Error(f"GET {key}: не удалось за 3 попыток (HTTP 503)")
        obj = self.objects.get(key)
        return None if obj is None else json.dumps(obj, ensure_ascii=False).encode("utf-8")


class FakeStore:
    """Стор с двумя рядами: столько, сколько нужно манифесту зеркала."""

    def __init__(self, series):
        self.series = list(series)
        self.cleared = []

    def list_series(self):
        return list(self.series)

    def list_dirty(self):
        return []

    def load_series(self, sid):
        return {"id": sid, "points": {}}

    def clear_dirty(self, ids):
        self.cleared.extend(ids)


class TestMirrorIndex(PublishCase):
    """raw/_index.json — единственный способ перечислить зеркало.

    Подпись S3 в lib/r2.py не умеет ListObjects, поэтому без манифеста стор из
    бакета не восстановить: надо заранее знать все имена. Отсюда и цена ошибки —
    реколибровка на чистом раннере (ops/recalibrate.py) собрала бы композит из
    того, что попало в манифест, и молча получила бы другую модель.
    """

    def install(self, bucket):
        p = mock.patch.object(self.publish.r2, "get", bucket._get)
        p.start()
        self.addCleanup(p.stop)
        return bucket

    def test_манифест_объединяет_а_не_перезаписывает(self):
        # ОПЛАЧЕНО ПРОДОМ: писателей двое, и у запасного (GitHub Actions) стор пустой
        # — он собирает только ряды суточного режима. Перезапись вычёркивала 69 рядов
        # из 105, включая ногу ядра urals_tax: следующее восстановление дало бы
        # композит из двух ног вместо трёх, то есть ложный «разошёлся с эталоном».
        bucket = self.install(FakeBucket(
            self.publish, {"raw/_index.json": {"series": ["urals_tax", "imoex", "cbr_key"]}}
        ).install(self))
        store = FakeStore(["imoex", "rgbi"])
        res = self.publish.publish(self.payload(), "daily", store=store)
        got = bucket.objects["raw/_index.json"]
        self.assertEqual(got["series"], ["cbr_key", "imoex", "rgbi", "urals_tax"])
        self.assertEqual(got["written_by_local"], 2,
                         "манифест обязан признаваться, сколько рядов знает сам писатель")
        self.assertEqual(res["raw_indexed"], 4)

    def test_битый_манифест_не_роняет_публикацию(self):
        bucket = FakeBucket(self.publish).install(self)
        p = mock.patch.object(self.publish.r2, "get", lambda key: "{ это не json".encode("utf-8"))
        p.start()
        self.addCleanup(p.stop)
        res = self.publish.publish(self.payload(), "daily", store=FakeStore(["imoex"]))
        self.assertEqual(bucket.objects["raw/_index.json"]["series"], ["imoex"])
        self.assertTrue(res["ok"])

    def test_отказ_чтения_манифеста_не_перезаписывает_его(self):
        # «Манифеста нет» и «манифест не прочитался» — разные исходы. У запасного
        # писателя local — 23 суточных ряда: перезапись при временной 503 вычеркнула
        # бы 80+ имён, включая ногу ядра. При отказе чтения манифест не трогаем.
        bucket = self.install(FakeBucket(
            self.publish, {"raw/_index.json": {"series": ["urals_tax", "imoex"]}},
            read_fails=True).install(self))
        res = self.publish.publish(self.payload(), "daily", store=FakeStore(["imoex", "rgbi"]))
        self.assertEqual(bucket.objects["raw/_index.json"]["series"],
                         ["urals_tax", "imoex"], "манифест перезаписан вслепую")
        self.assertIsNone(res["raw_indexed"])
        self.assertTrue(any("манифест не прочитался" in e for e in res["errors"]))
        self.assertTrue(res["ok"], "витрину это ронять не должно")

    def test_без_стора_манифест_не_трогаем(self):
        # Прогон без стора ничего о зеркале не знает: пустой манифест был бы враньём.
        bucket = self.install(FakeBucket(
            self.publish, {"raw/_index.json": {"series": ["imoex"]}}).install(self))
        res = self.publish.publish(self.payload(), "daily", store=None)
        self.assertIsNone(res["raw_indexed"])
        self.assertEqual(bucket.objects["raw/_index.json"]["series"], ["imoex"])


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


class TestTrimLadder(PublishCase):
    """Лестница обрезки: ПОРЯДОК ступеней и остановка «влезло — не режем дальше».

    До 18.08.2026 ни то, ни другое не закреплялось: переворот TRIM_STEPS (события
    режутся первыми, спарклайны последними) и снятие ранней остановки проходили
    зелёными. Порядок — содержательное решение: сверху то, чего меньше всего жалко.
    """

    def fat(self, n=15000):
        p = self.payload(states={"current": {"trend": 0, "vol": 1, "bond": 1},
                                 "series": pairs(n)},
                         core_extra={"series": pairs(n)},
                         monitors=[{"id": "rvi", "status": "ok", "asof": ASOF,
                                    "headline": "х", "payload": {"series": pairs(800)}}])
        p["events"] = [{"ts": ASOF + "T10:00:00Z", "kind": "state_cell_change",
                        "severity": "info", "text": "событие %d" % i}
                       for i in range(30)]
        p["core"]["components"] = [{"id": "usd_mom63", "spark": pairs(500)}]
        return p

    def test_события_режутся_последними(self):
        # Журнал — самое дорогое: он режется, только когда всё остальное не помогло.
        payload = self.fat()
        data, cut = self.publish.fit_size(payload, limit=200 * 1024)
        self.assertIn("core_series", cut)
        self.assertNotIn("events", cut, "события порезаны раньше дешёвых ступеней")
        self.assertEqual(len(payload["events"]), 30)

    def test_остановка_как_только_влезло(self):
        # Лимит, который закрывается первой же ступенью: остальные не трогаем.
        payload = self.fat(n=600)
        big = len(self.publish.dumps(payload))
        data, cut = self.publish.fit_size(payload, limit=big - 1000)
        self.assertEqual(cut, ["monitor_series"],
                         "лестница продолжила резать после того, как влезла")
        self.assertTrue(payload["core"]["components"][0].get("spark"),
                        "спарклайны срезаны, хотя лимит уже был выдержан")

    def test_порядок_ступеней_заморожен(self):
        self.assertEqual([name for name, _ in self.publish.TRIM_STEPS],
                         ["monitor_series", "spark", "core_series",
                          "states_series", "events"])

    def test_события_режутся_с_хвоста_старого(self):
        # мутация ev[-20:] -> ev[:20]: остаются двадцать СТАРЕЙШИХ, свежее событие
        # (то, ради которого журнал и читают) вылетает первым.
        payload = self.fat()
        self.publish.fit_size(payload, limit=10 * 1024)
        texts = [e["text"] for e in payload["events"]]
        self.assertEqual(len(texts), 20)
        self.assertIn("событие 29", texts, "свежайшее событие срезано")
        self.assertNotIn("событие 0", texts, "старьё пережило обрезку вместо свежего")


class TestMirrorShrinkGuard(PublishCase):
    """raw/{sid}.json — единственная восстановимая копия рядов, которых нет в git.

    Сценарий катастрофы (аудит 18.08.2026): карантин битого файла в сторе -> ряд
    строится заново из одной свежей точки -> зеркало затирается огрызком, и
    реколибровка на пустом раннере молча считает по нему. Дорогие ряды (zcyc ~2900
    запросов ISS) штатным прогоном не бэкфиллятся — затирание необратимо.
    """

    def full(self, n=60):
        return {"id": "urals_tax", "points": {f"20{i:02d}-01-31": 40.0 + i for i in range(n)}}

    def dirty_store(self, points):
        store = FakeStore(["urals_tax"])
        store.list_dirty = lambda: ["urals_tax"]
        store.load_series = lambda sid: {"id": sid, "points": points}
        return store

    def install(self, bucket):
        p = mock.patch.object(self.publish.r2, "get", bucket._get)
        p.start()
        self.addCleanup(p.stop)
        return bucket

    def test_огрызок_не_затирает_полное_зеркало(self):
        bucket = self.install(FakeBucket(
            self.publish, {"raw/urals_tax.json": self.full(60)}).install(self))
        res = self.publish.publish(self.payload(), "daily",
                                   store=self.dirty_store({"2026-08-18": 41.0}))
        self.assertEqual(len(bucket.objects["raw/urals_tax.json"]["points"]), 60,
                         "канон затёрт огрызком")
        self.assertTrue(any("сжался 60 -> 1" in e for e in res["errors"]), res["errors"])
        self.assertNotIn("urals_tax", res["raw_mirrored"])

    def test_ретро_правка_и_рост_проходят(self):
        # Штатные случаи: ряд вырос, ряд чуть уточнён — зеркалим как раньше.
        grown = {f"20{i:02d}-01-31": 40.0 + i for i in range(61)}
        bucket = self.install(FakeBucket(
            self.publish, {"raw/urals_tax.json": self.full(60)}).install(self))
        self.publish.publish(self.payload(), "daily", store=self.dirty_store(grown))
        self.assertEqual(len(bucket.objects["raw/urals_tax.json"]["points"]), 61)

    def test_первое_зеркало_пишется_без_вопросов(self):
        bucket = self.install(FakeBucket(self.publish).install(self))
        self.publish.publish(self.payload(), "daily",
                             store=self.dirty_store({"2026-08-18": 41.0}))
        self.assertIn("raw/urals_tax.json", bucket.objects)

    def test_нечитаемый_бакет_не_перезаписывается_вслепую(self):
        # Если старое зеркало не прочиталось, писать поверх нельзя: перезаписывать
        # канон, не увидев его, — исходная ошибка. Ряд останется dirty и приедет
        # следующим прогоном.
        bucket = self.install(FakeBucket(
            self.publish, {"raw/urals_tax.json": self.full(60)}, read_fails=True
        ).install(self))
        store = self.dirty_store({"2026-08-18": 41.0})
        res = self.publish.publish(self.payload(), "daily", store=store)
        self.assertEqual(len(bucket.objects["raw/urals_tax.json"]["points"]), 60)
        self.assertTrue(any("не перезаписываю вслепую" in e for e in res["errors"]))
        self.assertEqual(store.cleared, [], "ряд обязан остаться в очереди зеркала")


class TestNanGate(PublishCase):
    def test_nan_не_уезжает_в_бакет_и_не_роняет_прогон(self):
        # json.dumps по умолчанию пишет NaN литералом, JSON.parse браузера падает
        # на нём первым символом — один NaN убивал всю витрину для всех читателей.
        bucket = FakeBucket(self.publish, {"data.json": self.payload()}).install(self)
        res = self.publish.publish(self.payload(core=float("nan")), "daily", store=None)
        self.assertFalse(res["ok"])
        self.assertIn("не сериализуется", res["reason"])
        self.assertEqual(bucket.objects["data.json"]["core"]["value"], 0.68,
                         "прежняя витрина обязана уцелеть")


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

    def test_нечитаемый_эталон_блокирует_битую_витрину(self):
        # Fail-closed: «эталон не прочитался» ≠ «эталона нет». В бакете может лежать
        # живая витрина, и публиковать поверх неё пустоту вслепую нельзя.
        bucket = FakeBucket(self.publish, {"data.json": self.payload()},
                            read_fails=True).install(self)
        broken = self.publish.build_payload(core={"value": None}, states={"current": {}},
                                            monitors=[], sources={}, mode="daily", asof=ASOF)
        res = self.publish.publish(broken, "daily", store=None)
        self.assertFalse(res["ok"])
        self.assertFalse(res["published"])
        self.assertIn("вслепую", res["reason"])
        self.assertEqual(bucket.objects["data.json"]["core"]["value"], 0.68,
                         "живая витрина затёрта при недоступном эталоне")

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
