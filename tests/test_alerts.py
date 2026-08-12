"""События в телеграм (контракт §6): переходы, доставка, очередь повторов.

ПОЧЕМУ этот файл появился поздно: alerts.py — единственный модуль пайплайна, который
сам инициирует внешнее действие, и до аудита он не был покрыт ничем. Все проверки
ниже — это зафиксированные дефекты, а не «покрытие ради покрытия»:

* дубль уведомления считался НЕдоставленным и забивал очередь повторов, вытесняя
  из неё настоящие события (56 интрадей-тактов накануне заседания ЦБ);
* очередь резалась с головы ([:40]), то есть выбрасывалось самое свежее;
* «было X» в сообщении о развороте ядра бралось из вчерашнего снимка, а не из
  зафиксированного знака, — оба числа выходили одного знака;
* окно входа объявлялось после дня, когда состояния не посчитались (vol/bond=None);
* мусор в alerts_state.json ронял прогон ДО публикации;
* lease_lost при неудачной доставке терялся навсегда;
* пустое ядро/вердикт не порождали ни одного события.

Время везде фиксированное: тест про «повторим в течение суток» не имеет права
зависеть от часов машины.
"""

import json
import os
import re
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from tests import need

UTC = timezone.utc
NOW = datetime(2026, 8, 11, 16, 5, 0, tzinfo=UTC)
ASOF = "2026-08-11"
PREV_ASOF = "2026-08-10"


def payload(core=0.68, trend=0, vol=1, bond=1, asof=ASOF, health="ok",
            cell="bear|stress|stress", monitors=None, sources=None):
    """Витрина в объёме, который читают правила алертов."""
    return {
        "asof_trading_day": asof,
        "core": {"value": core, "sign": (0 if core is None else (1 if core > 0 else -1)),
                 "health": {"status": health, "n": 24, "ic_24m": -0.02}},
        "states": {"current": {"trend": trend, "vol": vol, "bond": bond},
                   "distances": [{"id": "bond", "text": "просадка RGBI −1.2% от максимума"}]},
        "verdict": {"cell_code": cell, "cell_label": "токсичная",
                    "core_label": "умеренный лонг",
                    "cell_stats": {"mean_fwd1m_pct": -0.55, "hit": 0.4, "n": 12}},
        "monitors": monitors if monitors is not None else [tile_cb()],
        "sources": sources if sources is not None else {"iss": {"status": "ok"}},
    }


def tile_cb(days_left=9, key_rate=14.0, consensus=None):
    return {"id": "cb_meeting", "status": "ok", "asof": ASOF,
            "headline": "До заседания 31 дн.",
            "payload": {"days_left": days_left, "next_meeting": "2026-09-11",
                        "key_rate": key_rate, "consensus": consensus,
                        "priced_text": "рынок закладывает −100 б.п."}}


class AlertsCase(unittest.TestCase):
    """Общая обвязка: временный STATE_DIR и телеграм, который можно «уронить»."""

    def setUp(self):
        self.alerts = need(self, "pipeline.alerts", "run", "detect", "after_publish")
        self.telegram = need(self, "pipeline.lib.telegram", "deliver", "SENT", "DUP", "FAIL")
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.prev_env = {k: os.environ.get(k) for k in
                         ("STATE_DIR", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
                          "NEXUS_EVENTS_URL", "NEXUS_INGEST_TOKEN")}
        os.environ.update(STATE_DIR=self.tmp.name, TELEGRAM_BOT_TOKEN="тест",
                          TELEGRAM_CHAT_ID="-100")
        # Зеркало NEXUS гасим явно (правило 2 набора: в сеть не ходим). На VPS эти
        # переменные заданы, и без снятия набор, запущенный там, постучался бы в хаб
        # настоящим POST на каждое событие.
        os.environ.pop("NEXUS_EVENTS_URL", None)
        os.environ.pop("NEXUS_INGEST_TOKEN", None)
        self.addCleanup(self._restore_env)
        self.sent = []
        self.online = True
        patcher = mock.patch.object(self.telegram, "send", self._send)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _send(self, text, silent=False, retries=2):
        if not self.online:
            return False, "телеграм лежит"
        self.sent.append(text)
        return True, None

    def _restore_env(self):
        for key, val in self.prev_env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    def state(self):
        return self.alerts.load_state()

    def pending_keys(self):
        return [e["key"] for e in self.state().get("pending") or []]

    def markers(self):
        return sorted(p.name for p in (self.root / "notify").glob("*.json"))

    def seed(self, snapshot_payload, now=None):
        """Запомнить мир молча — как штатный первый прогон на машине."""
        self.alerts.run(snapshot_payload, dry_run=False, now=now or NOW - timedelta(days=1))
        self.sent.clear()


class TestOnlyTransitions(AlertsCase):
    def test_second_identical_run_is_silent(self):
        # мутация: слать состояние, а не переход -> ежедневное «сегодня та же ячейка»,
        # и читатель выключает канал вместе с настоящими алертами.
        self.seed(payload())
        events = self.alerts.run(payload(), dry_run=False, now=NOW)
        self.assertEqual(events, [])
        self.assertEqual(self.sent, [])

    def test_first_run_on_clean_machine_is_silent(self):
        events = self.alerts.run(payload(bond=0, health="dead"), dry_run=False, now=NOW)
        self.assertEqual([e["kind"] for e in events], [])


class TestCoreFlip(AlertsCase):
    def flips(self, values):
        out = []
        self.seed(payload(core=values[0]))
        for i, val in enumerate(values[1:], start=1):
            evs = self.alerts.run(payload(core=val), dry_run=False,
                                  now=NOW + timedelta(days=i))
            out += [e for e in evs if e["kind"] == "core_flip"]
        return out

    def test_noise_around_zero_is_silent(self):
        # CORE_FLIP_HYSTERESIS = 0.1: болтанка ±0.05 знака не фиксирует, иначе
        # композит около нуля шлёт по алерту в день.
        self.assertEqual(self.flips([0.05, -0.05, 0.05]), [])

    def test_real_flip_reported_once(self):
        got = self.flips([0.66, -0.35, -0.40])
        self.assertEqual(len(got), 1)
        self.assertIn("-0.35", got[0]["text"])

    def test_was_value_is_the_alerted_one_not_yesterday(self):
        # мутация: печатать prev['core_value'] -> «развернулось: -0.66, было -0.02»,
        # оба числа одного знака (путь через мёртвую зону гистерезиса).
        got = self.flips([0.66, 0.02, -0.02, -0.66])
        self.assertEqual(len(got), 1)
        self.assertIn("+0.66", got[0]["text"])
        self.assertNotIn("-0.02", got[0]["text"])


class TestBuyWindow(AlertsCase):
    def fire(self, prev_state, cur_state):
        self.seed(payload(vol=prev_state[0], bond=prev_state[1], cell="bear|calm|stress"))
        evs = self.alerts.run(payload(vol=cur_state[0], bond=cur_state[1],
                                      cell="bear|stress|ok"), dry_run=False, now=NOW)
        return [e for e in evs if e["kind"] == "buy_window_open"]

    def test_opens_on_vol_spike_with_calm_bonds(self):
        self.assertEqual(len(self.fire((0, 0), (1, 0))), 1)

    def test_silent_while_bond_flag_on(self):
        self.assertEqual(self.fire((0, 1), (1, 1)), [])

    def test_silent_when_previous_state_unknown(self):
        # мутация: убрать защиту от None -> после дня без RGBI приходит «Окно входа»
        # как о новом переходе, хотя ячейка не менялась неделями.
        self.alerts.save_state({"last": {"cell": "bear|calm|ok", "core_value": 0.68,
                                         "core_sign": 1, "trend": 0, "vol": None,
                                         "bond": None, "health": "ok"},
                                "core_sign_alerted": 1})
        evs = self.alerts.detect(payload(vol=1, bond=0, cell="bear|stress|ok"),
                                 self.state(), NOW)
        self.assertNotIn("buy_window_open", [e["kind"] for e in evs])


class TestDelivery(AlertsCase):
    def test_undelivered_event_waits_in_queue_without_marker(self):
        self.seed(payload(bond=1))
        self.online = False
        evs = self.alerts.run(payload(bond=0, cell="bear|stress|ok"), dry_run=False, now=NOW)
        self.assertTrue(evs)
        self.assertFalse(any(e["delivered"] for e in evs))
        self.assertEqual(self.markers(), [], "маркер дедупа не имеет права опережать доставку")
        self.assertIn("bond_off:" + ASOF, self.pending_keys())

    def test_repaired_telegram_delivers_pending_exactly_once(self):
        self.seed(payload(bond=1))
        self.online = False
        self.alerts.run(payload(bond=0, cell="bear|stress|ok"), dry_run=False, now=NOW)
        self.online = True
        self.alerts.run(payload(bond=0, cell="bear|stress|ok"), dry_run=False,
                        now=NOW + timedelta(hours=1))
        self.assertEqual(len([t for t in self.sent if "Облигационный флаг снят" in t]), 1)
        self.assertEqual(self.pending_keys(), [])
        # третий прогон ничего не повторяет
        self.alerts.run(payload(bond=0, cell="bear|stress|ok"), dry_run=False,
                        now=NOW + timedelta(hours=2))
        self.assertEqual(len([t for t in self.sent if "Облигационный флаг снят" in t]), 1)

    def test_stale_pending_is_dropped_after_a_day(self):
        self.seed(payload(bond=1))
        self.online = False
        self.alerts.run(payload(bond=0, cell="bear|stress|ok"), dry_run=False, now=NOW)
        self.online = True
        self.alerts.run(payload(bond=0, cell="bear|stress|ok"), dry_run=False,
                        now=NOW + timedelta(hours=25))
        self.assertEqual(self.sent, [], "протухшую новость не повторяем")

    def test_duplicate_reminder_never_piles_up_in_queue(self):
        # Корень pending-eviction: notify() возвращал False и на «уже отправлено».
        # 56 интрадей-тактов накануне заседания забивали очередь одним cb_reminder.
        self.seed(payload(monitors=[tile_cb(days_left=1)]))
        for i in range(56):
            self.alerts.run(payload(monitors=[tile_cb(days_left=1)]), dry_run=False,
                            now=NOW + timedelta(minutes=15 * i))
        self.assertEqual(len([t for t in self.sent if "Завтра заседание" in t]), 1)
        self.assertEqual(self.pending_keys(), [])

    def test_fresh_events_survive_a_full_queue(self):
        # мутация: срез [:40] вместо [-40:] -> в очередь не попадает именно свежее,
        # то есть теряется ровно то, ради чего канал заведён.
        old = [{"key": f"info:{i}", "ts": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "kind": "info", "severity": "info", "text": f"старое {i}"}
               for i in range(self.alerts.PENDING_MAX)]
        self.alerts.save_state({"last": {"cell": "bear|stress|stress", "core_value": 0.68,
                                         "core_sign": 1, "trend": 0, "vol": 1, "bond": 1,
                                         "health": "ok"},
                                "core_sign_alerted": 1, "pending": old})
        self.online = False
        self.alerts.run(payload(bond=0, cell="bear|stress|ok"), dry_run=False, now=NOW)
        keys = self.pending_keys()
        self.assertLessEqual(len(keys), self.alerts.PENDING_MAX)
        self.assertIn("bond_off:" + ASOF, keys)
        self.assertIn("buy_window:" + ASOF, keys)

    def test_dry_run_does_not_eat_the_transition(self):
        self.seed(payload(bond=1))
        self.alerts.run(payload(bond=0, cell="bear|stress|ok"), dry_run=True, now=NOW)
        self.assertEqual(self.sent, [])
        evs = self.alerts.run(payload(bond=0, cell="bear|stress|ok"), dry_run=False, now=NOW)
        self.assertIn("bond_flag_off", [e["kind"] for e in evs])


class TestBrokenState(AlertsCase):
    def test_garbage_in_state_file_does_not_crash_the_run(self):
        # Раньше строка вместо списка в pending роняла alerts.run, а с ним и весь
        # прогон ДО публикации: панель не обновлялась из-за уведомления.
        (self.root / "alerts_state.json").write_text(json.dumps(
            {"last": ["не", "словарь"], "pending": "мусор", "feed": {"a": 1},
             "core_sign_alerted": 1}, ensure_ascii=False), encoding="utf-8")
        events = self.alerts.run(payload(), dry_run=False, now=NOW)
        self.assertIsInstance(events, list)
        self.assertIsInstance(self.state().get("pending"), list)

    def test_unreadable_state_file_is_empty_state(self):
        (self.root / "alerts_state.json").write_text("{битый", encoding="utf-8")
        self.assertEqual(self.alerts.load_state(), {})


class TestSeedFromPublishedPayload(AlertsCase):
    def test_clean_runner_restores_snapshot_from_data_json(self):
        # Фолбэк GHA поднимается с пустым STATE_DIR каждый раз: без снимка detect
        # молчит по определению, и ровно в аварии VPS канал был глухим.
        published = payload(core=0.68, bond=1)
        evs = self.alerts.run(payload(core=0.61, bond=0, cell="bear|stress|ok",
                                      health="dead",
                                      sources={"iss": {"status": "error", "lag_min": 4300,
                                                       "asof": "2026-08-05"}}),
                              dry_run=False, now=NOW, seed_payload=published)
        kinds = [e["kind"] for e in evs]
        for kind in ("bond_flag_off", "buy_window_open", "source_stale", "health_dead"):
            self.assertIn(kind, kinds)

    def test_seeding_does_not_invent_a_core_flip(self):
        evs = self.alerts.run(payload(core=0.68), dry_run=False, now=NOW,
                              seed_payload=payload(core=0.68))
        self.assertNotIn("core_flip", [e["kind"] for e in evs])

    def test_empty_published_payload_is_not_a_snapshot(self):
        empty = payload(core=None, cell=None)
        self.assertFalse(self.alerts.seed_from_payload({}, empty, NOW))

    def test_existing_state_wins_over_snapshot(self):
        self.seed(payload(bond=1))
        state = self.state()
        self.assertFalse(self.alerts.seed_from_payload(state, payload(bond=0), NOW))


class TestCoreMissing(AlertsCase):
    def test_lost_verdict_is_an_event(self):
        # Неполный стор давал панель «нет данных» при зелёном прогоне: источники ok,
        # health смотрит только на 'dead', сторож видит свежий Last-Modified.
        self.seed(payload())
        evs = self.alerts.run(payload(core=None, cell=None), dry_run=False, now=NOW)
        kinds = [e["kind"] for e in evs]
        self.assertIn("core_missing", kinds)
        self.assertEqual([e["severity"] for e in evs if e["kind"] == "core_missing"], ["warn"])

    def test_no_event_when_core_was_empty_before(self):
        self.seed(payload(core=None, cell=None))
        evs = self.alerts.run(payload(core=None, cell=None), dry_run=False, now=NOW)
        self.assertNotIn("core_missing", [e["kind"] for e in evs])


class TestAfterPublish(AlertsCase):
    def result(self, **kw):
        base = {"ok": True, "published": True, "lease_ok": True, "lease_reason": "vps",
                "bytes": 1024, "limit": 250 * 1024, "trimmed": [], "oversize": False}
        base.update(kw)
        return base

    def test_lease_lost_is_queued_when_telegram_is_down(self):
        self.online = False
        evs = self.alerts.after_publish(self.result(lease_ok=False,
                                                    lease_reason="перо у GHA"), now=NOW)
        self.assertEqual([e["kind"] for e in evs], ["lease_lost"])
        self.assertIn("lease_lost:2026-08-11", self.pending_keys())

    def test_lease_flag_moves_only_after_delivery(self):
        self.online = False
        self.alerts.after_publish(self.result(lease_ok=False), now=NOW)
        self.assertNotEqual(self.state().get("lease_ok"), False)
        self.online = True
        evs = self.alerts.after_publish(self.result(lease_ok=False),
                                        now=NOW + timedelta(hours=1))
        self.assertEqual([e["kind"] for e in evs], ["lease_lost"])
        self.assertEqual(self.state().get("lease_ok"), False)
        # перо вернулось — о потере больше не напоминаем
        self.assertEqual(self.alerts.after_publish(self.result(lease_ok=True),
                                                   now=NOW + timedelta(hours=2)), [])

    def test_oversize_payload_is_loud(self):
        evs = self.alerts.after_publish(self.result(oversize=True, bytes=415649,
                                                    trimmed=["spark"]), now=NOW)
        self.assertEqual([e["kind"] for e in evs], ["payload_oversize"])
        self.assertTrue(self.sent)

    def test_quiet_run_produces_nothing(self):
        self.assertEqual(self.alerts.after_publish(self.result(), now=NOW), [])


class TestTexts(AlertsCase):
    """Каждое событие обязано нести число и один десятичный разделитель — точку."""

    def all_kinds(self):
        prev = {"cell": "bull|calm|ok", "core_value": 0.66, "core_sign": 1, "trend": 1,
                "vol": 0, "bond": 1, "health": "ok", "key_rate": 15.0, "deposit": 16.0,
                "orfr_asof": "2026-06-30", "auction_date": "2026-07-29",
                "sources": {"iss": "ok"}}
        state = {"last": prev, "core_sign_alerted": 1, "core_value_alerted": 0.66}
        mons = [tile_cb(days_left=1, key_rate=14.0, consensus=14.5),
                {"id": "orfr", "status": "ok", "asof": "2026-07-31",
                 "headline": "физлица купили на 12.3 млрд",
                 "payload": {"seller_exhaustion": {"text": "продавец выдыхается"}}},
                {"id": "ofz_auctions", "status": "ok", "asof": ASOF,
                 "headline": "аукцион провален",
                 "payload": {"date": ASOF, "failed": True, "placed_bn": 12.4,
                             "demand_bn": 88.1}},
                {"id": "deposit_spread", "status": "ok", "asof": ASOF,
                 "headline": "ставка выросла",
                 "payload": {"deposit_pct": 16.4, "deposit_asof": ASOF, "spread_pp": 8.1}}]
        bad = payload(core=-0.66, trend=0, vol=1, bond=0, cell="bear|stress|ok",
                      health="dead", monitors=mons,
                      sources={"iss": {"status": "stale", "lag_min": 4300,
                                       "asof": "2026-08-05"}})
        events = self.alerts.detect(bad, state, NOW)
        events += self.alerts.detect(payload(core=None, cell=None), {"last": prev}, NOW)
        events += self.alerts.detect(payload(bond=1, cell="bear|stress|stress"),
                                     {"last": dict(prev, bond=0)}, NOW)
        self.online = False
        events += self.alerts.after_publish(
            {"lease_ok": False, "lease_reason": "перо у GHA", "oversize": True,
             "bytes": 415649, "limit": 250 * 1024, "trimmed": ["spark"]}, now=NOW)
        return events

    def test_contract_kinds_are_reachable(self):
        # мутация: правило перестало срабатывать -> вид события пропадает из набора,
        # и об этом узнают не по молчанию канала, а здесь.
        kinds = {e["kind"] for e in self.all_kinds()}
        for kind in ("core_flip", "state_cell_change", "bond_flag_off", "bond_flag_on",
                     "buy_window_open", "cb_reminder", "cb_decision", "orfr_published",
                     "auction_failed", "deposit_uptick", "source_stale", "health_dead",
                     "core_missing", "lease_lost", "payload_oversize"):
            self.assertIn(kind, kinds)

    def test_every_event_carries_a_number(self):
        # Событие без числа — это «что-то произошло»: получатель всё равно идёт на
        # панель. Исключение одно: lease_lost сообщает факт («перо у другого
        # раннера»), а число в нём — только то, что пришло из причины лиза.
        for ev in self.all_kinds():
            if ev["kind"] == "lease_lost":
                self.assertIn("публикацию пропустил", ev["text"])
                continue
            self.assertRegex(ev["text"], r"\d", f"{ev['kind']} без единого числа")

    def test_decimal_separator_is_a_dot_everywhere(self):
        # мутация: вернуть запятую в жёсткие строки -> в одном прогоне рядом висят
        # «+1.4%/мес (hit 0.6)» и «+1,4%/мес (hit 0,64)» — читатель считает это опечаткой.
        for ev in self.all_kinds():
            self.assertIsNone(re.search(r"\d,\d", ev["text"]),
                              f"{ev['kind']}: запятая как десятичный разделитель — {ev['text']}")

    def test_severity_is_from_the_contract(self):
        for ev in self.all_kinds():
            self.assertIn(ev["severity"], ("info", "warn"))
            self.assertTrue(ev["key"] and ev["ts"].endswith("Z"))


class TestPayloadFeed(AlertsCase):
    def test_feed_keeps_tail_and_shape(self):
        self.seed(payload(bond=1))
        self.alerts.run(payload(bond=0, cell="bear|stress|ok"), dry_run=False, now=NOW)
        feed = self.alerts.payload_events()
        self.assertTrue(feed)
        for row in feed:
            self.assertEqual(set(row), {"ts", "kind", "severity", "text"})
        self.assertLessEqual(len(feed), self.alerts.FEED_LIMIT)


class TestNexusMirror(AlertsCase):
    """Копия события в ленту хаба. Сам POST проверяется в test_nexus.py — здесь
    только сцепка с очередью повторов, которая живёт в alerts.dispatch."""

    def setUp(self):
        super().setUp()
        self.nexus = need(self, "pipeline.lib.nexus", "deliver", "SENT", "OFF", "FAIL")
        self.hub_outcome = self.nexus.SENT
        self.mirrored = []
        patcher = mock.patch.object(self.nexus, "deliver", self._deliver)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _deliver(self, event):
        self.mirrored.append(event["key"])
        return self.hub_outcome

    def seed(self, *args, **kwargs):
        super().seed(*args, **kwargs)
        self.mirrored.clear()

    def bond_off(self, now=NOW):
        return self.alerts.run(payload(bond=0, cell="bear|stress|ok"), dry_run=False, now=now)

    def test_event_reaches_the_hub_under_its_own_key(self):
        self.seed(payload(bond=1))
        events = self.bond_off()
        # Снятие флага тянет за собой смену ячейки и окно входа — в ленту уезжают
        # все три, по одному ключу на событие.
        self.assertEqual(sorted(self.mirrored), sorted(e["key"] for e in events))
        self.assertIn("bond_off:" + ASOF, self.mirrored)
        self.assertEqual(self.pending_keys(), [])

    def test_dead_hub_retries_without_a_second_telegram_message(self):
        # мутация: считать событие доставленным по одному телеграму -> упавший хаб
        # теряет событие навсегда, повторить его уже некому.
        self.seed(payload(bond=1))
        self.hub_outcome = self.nexus.FAIL
        self.bond_off()
        self.assertIn("bond_off:" + ASOF, self.pending_keys())
        self.hub_outcome = self.nexus.SENT
        self.bond_off(NOW + timedelta(hours=1))
        self.assertEqual(self.pending_keys(), [])
        self.assertEqual(self.mirrored.count("bond_off:" + ASOF), 2)
        # Телеграм на повторе отвечает DUP: второго сообщения в канал не уходит.
        self.assertEqual(len([t for t in self.sent if "Облигационный флаг снят" in t]), 1)

    def test_unconfigured_hub_never_blocks_delivery(self):
        # мутация: OFF трактовать как FAIL -> на машине без NEXUS_* очередь повторов
        # растёт вечно, а телеграм на каждом прогоне отвечает DUP впустую.
        self.seed(payload(bond=1))
        self.hub_outcome = self.nexus.OFF
        self.bond_off()
        self.assertEqual(self.pending_keys(), [])

    def test_dry_run_touches_neither_channel(self):
        self.seed(payload(bond=1))
        self.alerts.run(payload(bond=0, cell="bear|stress|ok"), dry_run=True, now=NOW)
        self.assertEqual(self.mirrored, [])
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
