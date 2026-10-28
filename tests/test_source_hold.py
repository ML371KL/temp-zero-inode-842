"""Выдержка санитарной тревоги об источнике на интрадей-такте (29.10.2026).

С 02.10 по 28.10.2026 почти половина санитарных сообщений 842 — пары интрадей-такта
«протух → снова отвечает» через 2–10 минут (13.10, 20.10, 21.10). Короткий сбой
теперь молчит, выдержанный — одна тревога с исходным временем начала и одна
отбивка. Выдержка только у интрадей: у суточного, месячного и недельного тактов
следующий прогон через часы, там тревога сразу.

Ловушка, которую закрывают эти проверки: выдержка по lag_min. fetched_at пишется и
при ошибке опроса, у источника в ошибке lag_min ≈ 0 на каждом такте — поэтому все
фикстуры ниже держат lag_min=0, и долгий отказ обязан всё равно дойти.
"""

import importlib
import json
import unittest
from datetime import datetime, timedelta, timezone

from tests.test_alerts import AlertsCase, payload

UTC = timezone.utc
# 21.10.2026 15:30:16 UTC — настоящий такт, на котором Polymarket ответил 404.
T0 = datetime(2026, 10, 21, 15, 30, 16, tzinfo=UTC)
TICK = timedelta(minutes=5)
DAY = "2026-10-20"


def sources(status="ok", lag_min=0):
    meta = {"status": status, "asof": DAY, "lag_min": lag_min, "series": "polymarket_ceasefire"}
    if status == "error":
        meta["lag_min"] = 0   # fetched_at свежий и при отказе — как на бою
    return {"iss": {"status": "ok", "asof": DAY}, "polymarket": meta}


def snap(status="ok", mode="intraday"):
    out = payload(asof=DAY, sources=sources(status))
    out["run_mode"] = mode
    return out


class HoldCase(AlertsCase):

    def setUp(self):
        super().setUp()
        self.seed(snap("ok"), now=T0 - TICK)

    def tick(self, status, at, mode="intraday"):
        return self.alerts.run(snap(status, mode), dry_run=False, now=at)

    def ops(self):
        return list(self.by_channel.get("ops") or [])

    def kinds(self, events):
        return [e["kind"] for e in events if e["kind"] in ("source_stale", "source_ok")]


class TestShortOutage(HoldCase):

    def test_сбой_в_три_такта_молчит(self):
        for i in range(3):
            self.assertEqual(self.kinds(self.tick("error", T0 + i * TICK)), [])
        self.assertEqual(self.kinds(self.tick("ok", T0 + 3 * TICK)), [])
        self.assertEqual(self.ops(), [], "короткий сбой дошёл до ops-канала")
        self.assertEqual(self.state().get("source_down"), {},
                         "запись о сбое пережила выздоровление")

    def test_пара_21_10_из_журнала_молчит(self):
        # 15:30 и 15:35 — 404, 15:40 — снова ок: на бою это были два сообщения.
        self.tick("error", T0)
        self.tick("error", T0 + TICK)
        self.tick("ok", T0 + 2 * TICK)
        self.assertEqual(self.ops(), [])

    def test_выдерживаемый_источник_не_попадает_в_снимок(self):
        """Снимок держит прежний «ok»: иначе следующий такт счёл бы «уже сообщали»."""
        self.tick("error", T0)
        self.assertEqual(self.state()["last"]["sources"]["polymarket"], "ok")
        rec = self.state()["source_down"]["polymarket"]
        self.assertTrue(rec["held"])
        self.assertEqual(rec["since"], "2026-10-21T15:30:16Z")

    def test_мигающий_источник_молчит(self):
        # Такт через такт: каждый сбой короче выдержки, отсчёт начинается заново.
        for i in range(8):
            self.tick("error" if i % 2 == 0 else "ok", T0 + i * TICK)
        self.assertEqual(self.ops(), [])


class TestLongOutage(HoldCase):

    def test_одна_тревога_и_одна_отбивка(self):
        stale_at = []
        for i in range(8):                       # восемь тактов подряд в ошибке
            evs = self.tick("error", T0 + i * TICK)
            if "source_stale" in self.kinds(evs):
                stale_at.append(i)
        self.assertEqual(stale_at, [3], "тревога — на четвёртом неудачном такте, одна")
        ok = self.tick("ok", T0 + 8 * TICK)
        self.assertEqual(self.kinds(ok), ["source_ok"])
        self.assertEqual(len(self.ops()), 2, "ровно тревога и отбивка")
        self.assertIn("Сбой длился 40 мин", next(e for e in ok if e["kind"] == "source_ok")["fact"])

    def test_тревога_несёт_время_начала(self):
        evs = []
        for i in range(4):
            evs = self.tick("error", T0 + i * TICK)
        ev = next(e for e in evs if e["kind"] == "source_stale")
        self.assertEqual(ev["ts"], "2026-10-21T15:30:16Z", "время события — начало сбоя")
        self.assertIn("Сбой с 18:30 МСК", ev["fact"])
        self.assertIn("длится 15 мин", ev["fact"])
        # lag_min=0 у ошибки — не «последний удачный опрос 0 мин назад».
        self.assertNotIn("Последний удачный опрос", ev["fact"])

    def test_нулевой_lag_min_не_глушит_долгий_отказ(self):
        """Выдержка по lag_min заглушила бы отказ навсегда: у ошибки он ≈ 0."""
        sent = 0
        for i in range(24):                       # два часа в ошибке
            sent += self.kinds(self.tick("error", T0 + i * TICK)).count("source_stale")
        self.assertEqual(sent, 1)

    def test_недоставленная_тревога_уходит_следующим_тактом(self):
        for i in range(3):
            self.tick("error", T0 + i * TICK)
        self.online = False
        evs = self.tick("error", T0 + 3 * TICK)
        self.assertIn("source_stale", self.kinds(evs))
        self.assertEqual(self.ops(), [])
        self.online = True
        again = self.tick("error", T0 + 4 * TICK)
        ev = next(e for e in again if e["kind"] == "source_stale")
        self.assertEqual(ev["ts"], "2026-10-21T15:30:16Z", "повтор потерял начало сбоя")
        self.assertEqual(len(self.ops()), 1)
        self.tick("error", T0 + 5 * TICK)
        self.assertEqual(len(self.ops()), 1, "доставленная тревога повторилась")


class TestHoldBoundary(HoldCase):

    def test_граница_ровно_на_допуске(self):
        edge = self.alerts.SOURCE_HOLD - self.alerts.SOURCE_HOLD_SLACK
        self.tick("error", T0)
        self.assertEqual(self.kinds(self.tick("error", T0 + edge - timedelta(seconds=1))), [])
        self.assertEqual(self.kinds(self.tick("error", T0 + edge)), ["source_stale"])

    def test_четвёртый_такт_раньше_срока_на_дрожь_таймера(self):
        # RandomizedDelaySec=20: первый такт опоздал на 20 с, четвёртый пришёл вовремя.
        self.tick("error", T0)
        self.tick("error", T0 + TICK)
        self.tick("error", T0 + 2 * TICK)
        evs = self.tick("error", T0 + 3 * TICK - timedelta(seconds=20))
        self.assertEqual(self.kinds(evs), ["source_stale"])

    def test_третий_такт_с_опозданием_ещё_молчит(self):
        self.tick("error", T0)
        self.tick("error", T0 + TICK)
        evs = self.tick("error", T0 + 2 * TICK + timedelta(seconds=20))
        self.assertEqual(self.kinds(evs), [])


class TestSlowModesAlertAtOnce(HoldCase):

    def test_суточный_такт_без_выдержки(self):
        evs = self.tick("error", T0, mode="daily")
        self.assertEqual(self.kinds(evs), ["source_stale"])
        self.assertEqual(len(self.ops()), 1)

    def test_месячный_и_недельный_без_выдержки(self):
        for mode in ("monthly", "weekly"):
            with self.subTest(mode=mode):
                self.seed(snap("ok"), now=T0 - TICK)
                self.assertEqual(self.kinds(self.tick("error", T0, mode=mode)), ["source_stale"])

    def test_суточный_после_интрадей_выдержки_берёт_начало_сбоя(self):
        self.tick("error", T0)
        evs = self.tick("error", T0 + TICK, mode="daily")
        ev = next(e for e in evs if e["kind"] == "source_stale")
        self.assertEqual(ev["ts"], "2026-10-21T15:30:16Z")
        self.assertIn("Сбой с 18:30 МСК, длится 5 мин", ev["fact"])

    def test_payload_без_режима_тревожит_сразу(self):
        # Старый payload без run_mode — как раньше: тревога сразу.
        out = payload(asof=DAY, sources=sources("error"))
        evs = self.alerts.run(out, dry_run=False, now=T0)
        self.assertEqual(self.kinds(evs), ["source_stale"])


class TestRestartBetweenTicks(HoldCase):

    def test_начало_сбоя_живёт_на_диске(self):
        self.tick("error", T0)
        on_disk = json.loads((self.root / self.alerts.STATE_NAME).read_text(encoding="utf-8"))
        self.assertEqual(on_disk["source_down"]["polymarket"]["since"], "2026-10-21T15:30:16Z")
        # Новый процесс: модуль загружается заново, память прошлого такта — только файл.
        self.alerts = importlib.reload(self.alerts)
        self.tick("error", T0 + TICK)
        self.alerts = importlib.reload(self.alerts)
        self.tick("error", T0 + 2 * TICK)
        self.alerts = importlib.reload(self.alerts)
        evs = self.tick("error", T0 + 3 * TICK)
        ev = next(e for e in evs if e["kind"] == "source_stale")
        self.assertEqual(ev["ts"], "2026-10-21T15:30:16Z")

    def test_битая_запись_о_сбое_не_роняет_прогон(self):
        path = self.root / self.alerts.STATE_NAME
        data = json.loads(path.read_text(encoding="utf-8"))
        data["source_down"] = {"polymarket": {"since": "мусор"}, "iss": "строка"}
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(self.kinds(self.tick("error", T0)), [])
        self.assertEqual(self.state()["source_down"]["polymarket"]["since"],
                         "2026-10-21T15:30:16Z", "мусор вместо начала сбоя не заменён")
        data["source_down"] = ["не словарь"]
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        self.tick("error", T0 + TICK)


if __name__ == "__main__":
    unittest.main()
