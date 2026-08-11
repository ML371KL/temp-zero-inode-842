"""Стор рядов: атомарность, инкрементальный upsert, dirty-множество, битый JSON.

Почему это важнее, чем кажется: прогон убивают по таймауту регулярно (systemd на
VPS, отмена job в GHA), и полуфайл ряда потом читается как «источник пуст» — панель
молча теряет историю, а причина не видна ни в одном логе. Поэтому запись только
через временный файл + os.replace, а чтение битого файла обязано уводить его в .bad
и возвращать None, а не бросать исключение посреди прогона.

Стор проверяется на временном каталоге через STATE_DIR: тест, пишущий в рабочий
стор, однажды сотрёт настоящие данные.
"""

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from tests import need


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.store = need(self, "pipeline.lib.store", "load_series", "save_series",
                          "upsert_points", "list_dirty")
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prev = os.environ.get("STATE_DIR")
        os.environ["STATE_DIR"] = self.tmp.name
        self.addCleanup(self._restore_env)
        self.root = Path(self.tmp.name)

    def _restore_env(self):
        if self.prev is None:
            os.environ.pop("STATE_DIR", None)
        else:
            os.environ["STATE_DIR"] = self.prev


class TestWriteAndRead(StoreCase):
    def test_round_trip(self):
        self.store.save_series("imoex", {"unit": "points", "cadence": "daily",
                                         "points": {"2026-08-11": 2301.0},
                                         "meta": {"source": "iss", "status": "ok"}})
        got = self.store.load_series("imoex")
        self.assertEqual(got["id"], "imoex")            # id проставляется сам
        self.assertEqual(got["points"], {"2026-08-11": 2301.0})
        self.assertEqual(got["meta"]["source"], "iss")

    def test_file_is_utf8_json_without_bom(self):
        self.store.save_series("rgbi", {"points": {"2026-08-11": 104.12},
                                        "meta": {"note": "индекс гособлигаций"}})
        raw = (self.root / "raw" / "rgbi.json").read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        # ensure_ascii=False: кириллица в note остаётся читаемой, а объект в R2
        # не раздувается в 6 раз escape-последовательностями.
        self.assertIn("индекс".encode("utf-8"), raw)
        json.loads(raw.decode("utf-8"))

    def test_no_temp_files_left_behind(self):
        # мутация: писать прямо в целевой файл -> при падении на диске остаётся
        # обрезанный JSON, и ряд «исчезает» на следующем прогоне.
        self.store.save_series("brent", {"points": {"2026-08-07": 70.31}})
        leftovers = [p.name for p in (self.root / "raw").iterdir() if ".tmp" in p.name]
        self.assertEqual(leftovers, [])

    def test_failed_write_keeps_previous_version(self):
        self.store.save_series("brent", {"points": {"2026-08-07": 70.31}})
        path = self.root / "raw" / "brent.json"
        before = path.read_bytes()
        # Обрыв ровно в момент сериализации: целевой файл трогать нельзя.
        with mock.patch.object(self.store.json, "dump", side_effect=OSError("диск кончился")):
            with self.assertRaises(OSError):
                self.store.save_series("brent", {"points": {"2026-08-10": 999.0}})
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.store.load_series("brent")["points"], {"2026-08-07": 70.31})

    def test_missing_series_is_none_not_error(self):
        self.assertIsNone(self.store.load_series("nope"))
        self.assertIsNone(self.store.last_date("nope"))
        self.assertEqual(self.store.describe("nope")["status"], "missing")


class TestUpsert(StoreCase):
    def test_incremental_merge_and_sorting(self):
        self.store.upsert_points("imoex", {"2026-08-11": 2301.0, "2026-08-10": 2293.32},
                                 unit="points", cadence="daily")
        self.store.upsert_points("imoex", {"2026-08-12": 2310.5})
        got = self.store.load_series("imoex")
        # мутация: заменять points целиком вместо долива -> история обнуляется
        # на каждом инкрементальном прогоне.
        self.assertEqual(list(got["points"]), ["2026-08-10", "2026-08-11", "2026-08-12"])
        self.assertEqual(got["unit"], "points")
        self.assertEqual(got["cadence"], "daily")

    def test_retro_correction_overwrites(self):
        # ISS пересчитывает обороты и значения задним числом — новое значение по
        # существующей дате обязано победить.
        self.store.upsert_points("imoex_value", {"2026-08-11": 100.0})
        self.store.upsert_points("imoex_value", {"2026-08-11": 142.0})
        self.assertEqual(self.store.load_series("imoex_value")["points"],
                         {"2026-08-11": 142.0})

    def test_asof_is_recomputed_on_every_upsert(self):
        # мутация: сохранять asof из старой meta -> ряд выглядит протухшим при
        # живых данных (и наоборот — свежим при пустом доливе).
        self.store.upsert_points("rgbi", {"2026-08-10": 104.0},
                                 meta_patch={"source": "iss"})
        self.store.upsert_points("rgbi", {"2026-08-11": 104.4})
        meta = self.store.load_series("rgbi")["meta"]
        self.assertEqual(meta["asof"], "2026-08-11")
        self.assertEqual(meta["status"], "ok")
        self.assertEqual(meta["source"], "iss")

    def test_last_date_ignores_empty_points(self):
        self.store.upsert_points("cpi_weekly", {"2026-08-03": 100.07, "2026-08-10": None})
        self.assertEqual(self.store.last_date("cpi_weekly"), "2026-08-03")

    def test_bad_values_are_refused(self):
        # Строка '15,86' один раз пропущенная в стор всплывает через месяц как NaN
        # в z-скоре, и найти источник уже невозможно.
        with self.assertRaises(ValueError):
            self.store.upsert_points("deposit_decade", {"2026-07-31": "15,86"})
        with self.assertRaises(ValueError):
            self.store.upsert_points("deposit_decade", {"2026-07-31": True})
        with self.assertRaises(ValueError):
            self.store.upsert_points("deposit_decade", {"31.07.2026": 15.86})
        with self.assertRaises(ValueError):
            self.store.upsert_points("../../etc/passwd", {"2026-07-31": 1.0})

    def test_none_value_is_allowed(self):
        # None — законное «день был, значения нет»; запрет сломал бы календарные ряды.
        self.store.upsert_points("cpi_weekly", {"2026-08-10": None})
        self.assertEqual(self.store.load_series("cpi_weekly")["points"],
                         {"2026-08-10": None})


class TestDirtySet(StoreCase):
    def test_upsert_marks_dirty_and_clean_clears(self):
        self.store.upsert_points("imoex", {"2026-08-11": 2301.0})
        self.store.upsert_points("rgbi", {"2026-08-11": 104.4})
        self.assertEqual(self.store.list_dirty(), ["imoex", "rgbi"])
        self.store.mark_clean("imoex")
        self.assertEqual(self.store.list_dirty(), ["rgbi"])
        self.store.mark_clean()
        self.assertEqual(self.store.list_dirty(), [])

    def test_unchanged_upsert_does_not_redirty(self):
        # мутация: помечать dirty всегда -> каждый прогон гонит в R2 десятки
        # одинаковых объектов, и журнал выгрузки перестаёт что-либо значить.
        self.store.upsert_points("imoex", {"2026-08-11": 2301.0})
        self.store.mark_clean()
        self.store.upsert_points("imoex", {"2026-08-11": 2301.0})
        self.assertEqual(self.store.list_dirty(), [])

    def test_changed_value_redirties(self):
        self.store.upsert_points("imoex", {"2026-08-11": 2301.0})
        self.store.mark_clean()
        self.store.upsert_points("imoex", {"2026-08-11": 2302.0})
        self.assertEqual(self.store.list_dirty(), ["imoex"])

    def test_status_change_redirties(self):
        # Даже без новых точек смена статуса обязана уехать в R2: фронт рисует
        # по ней жёлтый бейдж.
        self.store.upsert_points("brent", {"2026-08-07": 70.31})
        self.store.mark_clean()
        self.store.upsert_points("brent", {"2026-08-07": 70.31},
                                 meta_patch={"status": "stale"})
        self.assertEqual(self.store.list_dirty(), ["brent"])


class TestCorruptedDisk(StoreCase):
    def test_broken_json_is_quarantined(self):
        path = self.root / "raw" / "imoex.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"id": "imoex", "points": {"2026-08-11": 23', encoding="utf-8")
        # мутация: пробросить JSONDecodeError наружу -> один обрезанный файл
        # роняет весь прогон вместо одного ряда.
        self.assertIsNone(self.store.load_series("imoex"))
        self.assertTrue((self.root / "raw" / "imoex.json.bad").exists())
        self.assertFalse(path.exists())
        # После карантина ряд собирается заново, а не остаётся мёртвым.
        self.store.upsert_points("imoex", {"2026-08-11": 2301.0})
        self.assertEqual(self.store.load_series("imoex")["points"], {"2026-08-11": 2301.0})

    def test_broken_meta_does_not_lose_dirty_mechanism(self):
        (self.root).mkdir(parents=True, exist_ok=True)
        (self.root / "_meta.json").write_text("не json вовсе", encoding="utf-8")
        self.assertEqual(self.store.list_dirty(), [])
        self.store.upsert_points("rgbi", {"2026-08-11": 104.4})
        self.assertEqual(self.store.list_dirty(), ["rgbi"])

    def test_series_without_points_is_not_a_series(self):
        path = self.root / "raw" / "rvi.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"id": "rvi", "meta": {}}', encoding="utf-8")
        self.assertIsNone(self.store.load_series("rvi"))


if __name__ == "__main__":
    unittest.main()
