"""Единственный писатель в R2 (контракт §5): кто и когда имеет право на data.json.

Правило простое и несимметричное: VPS пишет ВСЕГДА и обновляет heartbeat; фолбэк
в GitHub Actions публикует, только если heartbeat VPS протух или лиз уже его.
Несимметричность намеренная — в соседнем проекте (839) два писателя по cron
перетирали друг друга, и на панели неделю прыгали данные разной свежести.

Все моменты времени фиксированные: тест «протухло ли за 90 минут» не имеет права
зависеть от часов машины.
"""

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from tests import need

UTC = timezone.utc
NOW = datetime(2026, 8, 11, 16, 5, 0, tzinfo=UTC)


def stamp(minutes_ago):
    """ISO-метка heartbeat, отстоящая от NOW на заданное число минут."""
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


class LeaseCase(unittest.TestCase):
    def setUp(self):
        self.lease = need(self, "pipeline.lib.lease", "can_write", "claim_lease",
                          "read_lease", "DEFAULT_TTL")

    def with_lease(self, obj):
        patcher = mock.patch.object(self.lease.r2, "get_json", return_value=obj)
        patcher.start()
        self.addCleanup(patcher.stop)

    def can(self, role, wid="gha:runner-1"):
        return self.lease.can_write(wid=wid, role=role, now=NOW)


class TestGhaFallback(LeaseCase):
    def test_gha_stays_silent_while_vps_heartbeat_is_fresh(self):
        # мутация: разрешить GHA писать всегда -> два писателя, и на панели
        # чередуются свежий и вчерашний data.json.
        self.with_lease({"writer": "vps", "holder_id": "vps:radar",
                         "heartbeat": stamp(30), "ttl_seconds": 5400})
        allowed, why = self.can("gha")
        self.assertFalse(allowed)
        self.assertIn("VPS", why)

    def test_gha_takes_over_when_heartbeat_is_stale(self):
        self.with_lease({"writer": "vps", "holder_id": "vps:radar",
                         "heartbeat": stamp(120), "ttl_seconds": 5400})
        allowed, _why = self.can("gha")
        self.assertTrue(allowed)

    def test_ttl_boundary_is_strict(self):
        # ttl 5400 с = 90 мин. Ровно на границе лиз ещё ЖИВ (условие age > ttl).
        # мутация: `>=` вместо `>` -> оба писателя просыпаются в одну и ту же
        # минуту раз в полтора часа; мутация ttl на 60 мин -> GHA перехватывает
        # лиз между обычными прогонами VPS.
        fresh = {"writer": "vps", "holder_id": "vps:radar",
                 "heartbeat": stamp(90), "ttl_seconds": 5400}
        self.with_lease(fresh)
        self.assertFalse(self.can("gha")[0])

    def test_one_second_past_ttl_opens_the_door(self):
        past = (NOW - timedelta(seconds=5401)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.with_lease({"writer": "vps", "holder_id": "vps:radar",
                         "heartbeat": past, "ttl_seconds": 5400})
        self.assertTrue(self.can("gha")[0])

    def test_gha_keeps_writing_while_it_holds_the_lease(self):
        # Пока лиз у GHA, свежесть heartbeat не важна: иначе фолбэк отключит
        # сам себя через полтора часа непрерывной работы.
        self.with_lease({"writer": "gha", "holder_id": "gha:runner-1",
                         "heartbeat": stamp(600), "ttl_seconds": 5400})
        self.assertTrue(self.can("gha")[0])

    def test_missing_lease_object_is_free_to_claim(self):
        self.with_lease(None)
        self.assertTrue(self.can("gha")[0])
        self.assertTrue(self.can("vps", wid="vps:radar")[0])

    def test_lease_without_heartbeat_is_treated_as_stale(self):
        # мутация: считать лиз без heartbeat живым -> битый объект навсегда
        # блокирует фолбэк, и панель молча замирает.
        self.with_lease({"writer": "vps", "holder_id": "vps:radar"})
        allowed, why = self.can("gha")
        self.assertTrue(allowed)
        self.assertIn("heartbeat", why)

    def test_broken_ttl_falls_back_to_default(self):
        # True — не «единичка»: isinstance(True, int) истинно, True > 0 тоже, и
        # ttl_seconds:true из чужого сериализатора давал TTL длиной РОВНО СЕКУНДУ —
        # фолбэк начинал писать параллельно с живым VPS (нарушение §5).
        for bad in (0, -1, True, "полтора часа", None):
            with self.subTest(ttl=bad):
                self.with_lease({"writer": "vps", "holder_id": "vps:radar",
                                 "heartbeat": stamp(30), "ttl_seconds": bad})
                self.assertFalse(self.can("gha")[0])   # 30 мин < DEFAULT_TTL
        self.with_lease({"writer": "vps", "holder_id": "vps:radar",
                         "heartbeat": stamp(120), "ttl_seconds": "полтора часа"})
        self.assertTrue(self.can("gha")[0])            # 120 мин > DEFAULT_TTL

    def test_unreadable_bucket_does_not_block_the_run(self):
        # Недоступный бакет — не повод не публиковать: лиз читается как «нет».
        patcher = mock.patch.object(self.lease.r2, "get_json",
                                    side_effect=self.lease.r2.R2Error("нет сети"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assertIsNone(self.lease.read_lease())
        self.assertTrue(self.can("gha")[0])


class TestVpsPriority(LeaseCase):
    def test_vps_writes_even_over_a_fresh_gha_lease(self):
        # Конфликт разрешается приоритетом, а не блокировкой (контракт §5).
        # мутация: симметричное правило -> VPS не сможет вернуть себе панель
        # после единственного фолбэк-прогона.
        self.with_lease({"writer": "gha", "holder_id": "gha:runner-1",
                         "heartbeat": stamp(1), "ttl_seconds": 5400})
        allowed, why = self.can("vps", wid="vps:radar")
        self.assertTrue(allowed)
        self.assertIn("перехват", why)

    def test_vps_recognises_its_own_lease(self):
        self.with_lease({"writer": "vps", "holder_id": "vps:radar",
                         "heartbeat": stamp(200), "ttl_seconds": 5400})
        allowed, why = self.can("vps", wid="vps:radar")
        self.assertTrue(allowed)
        self.assertIn("наш", why)


class TestClaim(LeaseCase):
    def test_claim_writes_the_contract_object(self):
        put = mock.patch.object(self.lease.r2, "put_json").start()
        self.addCleanup(mock.patch.stopall)
        got = self.lease.claim_lease(wid="vps:radar", role="vps", mode="daily",
                                     ttl=5400, now=NOW)
        self.assertEqual(got, {"writer": "vps", "holder_id": "vps:radar",
                               "heartbeat": "2026-08-11T16:05:00Z",
                               "ttl_seconds": 5400, "mode": "daily"})
        key, payload = put.call_args[0]
        self.assertEqual(key, self.lease.LEASE_KEY)
        self.assertEqual(payload["writer"], "vps")
        # Лиз обязан быть некэшируемым: закэшированный на минуту heartbeat —
        # это ровно та минута, в которую просыпается второй писатель.
        self.assertEqual(put.call_args[1].get("cache_control"), "no-store")

    def test_refresh_is_the_same_put(self):
        put = mock.patch.object(self.lease.r2, "put_json").start()
        self.addCleanup(mock.patch.stopall)
        self.lease.refresh_heartbeat(wid="vps:radar", role="vps", mode="intraday",
                                     now=NOW)
        self.assertEqual(put.call_count, 1)

    def test_age_seconds_uses_fixed_now(self):
        self.assertAlmostEqual(
            self.lease.age_seconds({"heartbeat": stamp(45)}, now=NOW), 2700.0, places=3)
        self.assertIsNone(self.lease.age_seconds({}, now=NOW))
        self.assertIsNone(self.lease.age_seconds({"heartbeat": "вчера"}, now=NOW))


class TestWriterRole(LeaseCase):
    def setUp(self):
        super().setUp()
        self.saved = {k: os.environ.get(k) for k in
                      ("RADAR_WRITER", "GITHUB_ACTIONS", "RADAR_WRITER_ID")}
        self.addCleanup(self._restore)
        for key in self.saved:
            os.environ.pop(key, None)

    def _restore(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_role_is_gha_inside_actions(self):
        os.environ["GITHUB_ACTIONS"] = "true"
        self.assertEqual(self.lease.writer_role(), "gha")

    def test_explicit_env_wins(self):
        # На VPS иногда нужно запустить прогон «как фолбэк» — явная переменная
        # обязана побеждать эвристику.
        os.environ["GITHUB_ACTIONS"] = "true"
        os.environ["RADAR_WRITER"] = "vps"
        self.assertEqual(self.lease.writer_role(), "vps")

    def test_default_is_vps(self):
        self.assertEqual(self.lease.writer_role(), "vps")

    def test_writer_id_is_stable_between_runs(self):
        # Без pid: иначе VPS вечно «перехватывает» лиз сам у себя, и в журнале
        # это неотличимо от настоящего конфликта писателей.
        os.environ["RADAR_WRITER"] = "vps"
        self.assertEqual(self.lease.writer_id(), self.lease.writer_id())


if __name__ == "__main__":
    unittest.main()
