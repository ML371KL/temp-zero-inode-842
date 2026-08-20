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
            cell="bear|stress|stress", monitors=None, sources=None,
            review_due=False, streak=0):
    """Витрина в объёме, который читают правила алертов."""
    return {
        "asof_trading_day": asof,
        "core": {"value": core, "sign": (0 if core is None else (1 if core > 0 else -1)),
                 "health": {"status": health, "n": 24, "ic_24m": -0.02,
                            "review_due": review_due, "below_zero_months": streak,
                            "below_since": "2026-01-30" if streak else None,
                            "review_months": 6}},
        "states": {"current": {"trend": trend, "vol": vol, "bond": bond},
                   "distances": [{"id": "bond", "text": "просадка RGBI −1,2% от максимума"}]},
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
        self.telegram = need(self, "pipeline.lib.telegram", "deliver", "SENT", "DUP",
                             "FAIL", "OFF")
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.prev_env = {k: os.environ.get(k) for k in
                         ("STATE_DIR", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
                          "NEXUS_EVENTS_URL", "NEXUS_INGEST_TOKEN",
                          "ERROR_BOT_TOKEN", "ERROR_CHAT_ID", "OPENROUTER_KEY")}
        os.environ.update(STATE_DIR=self.tmp.name, TELEGRAM_BOT_TOKEN="тест",
                          TELEGRAM_CHAT_ID="-100", ERROR_BOT_TOKEN="тест-ops",
                          ERROR_CHAT_ID="-200")
        # Внешние каналы гасим явно (правило 2 набора: в сеть не ходим). На VPS эти
        # переменные заданы, и без снятия набор, запущенный там, постучался бы в хаб
        # и в OpenRouter настоящими запросами на каждое событие.
        for key in ("NEXUS_EVENTS_URL", "NEXUS_INGEST_TOKEN", "OPENROUTER_KEY"):
            os.environ.pop(key, None)
        self.addCleanup(self._restore_env)
        self.sent = []
        self.by_channel = {}
        self.online = True
        patcher = mock.patch.object(self.telegram, "send", self._send)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _send(self, text, silent=False, retries=2, channel="alerts"):
        if not self.online:
            return False, "телеграм лежит"
        self.sent.append(text)
        self.by_channel.setdefault(channel, []).append(text)
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
        self.assertIn("−0,35", got[0]["text"])

    def test_was_value_is_the_alerted_one_not_yesterday(self):
        # мутация: печатать prev['core_value'] -> «развернулось: -0.66, было -0.02»,
        # оба числа одного знака (путь через мёртвую зону гистерезиса).
        got = self.flips([0.66, 0.02, -0.02, -0.66])
        self.assertEqual(len(got), 1)
        self.assertIn("+0,66", got[0]["text"])
        self.assertNotIn("0,02", got[0]["text"])


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
        self.seed(payload(vol=0, bond=1, cell="bear|calm|stress"))
        self.online = False
        evs = self.alerts.run(payload(vol=0, bond=0, cell="bear|calm|ok"), dry_run=False, now=NOW)
        self.assertTrue(evs)
        self.assertFalse(any(e["delivered"] for e in evs))
        self.assertEqual(self.markers(), [], "маркер дедупа не имеет права опережать доставку")
        self.assertIn("bond_off:" + ASOF, self.pending_keys())

    def test_repaired_telegram_delivers_pending_exactly_once(self):
        self.seed(payload(vol=0, bond=1, cell="bear|calm|stress"))
        self.online = False
        self.alerts.run(payload(vol=0, bond=0, cell="bear|calm|ok"), dry_run=False, now=NOW)
        self.online = True
        self.alerts.run(payload(vol=0, bond=0, cell="bear|calm|ok"), dry_run=False,
                        now=NOW + timedelta(hours=1))
        self.assertEqual(len([t for t in self.sent if "Долговой рынок вышел из стресса" in t]), 1)
        self.assertEqual(self.pending_keys(), [])
        # третий прогон ничего не повторяет
        self.alerts.run(payload(vol=0, bond=0, cell="bear|calm|ok"), dry_run=False,
                        now=NOW + timedelta(hours=2))
        self.assertEqual(len([t for t in self.sent if "Долговой рынок вышел из стресса" in t]), 1)

    def test_stale_pending_is_dropped_after_a_day(self):
        self.seed(payload(vol=0, bond=1, cell="bear|calm|stress"))
        self.online = False
        self.alerts.run(payload(vol=0, bond=0, cell="bear|calm|ok"), dry_run=False, now=NOW)
        self.online = True
        self.alerts.run(payload(vol=0, bond=0, cell="bear|calm|ok"), dry_run=False,
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
        self.alerts.run(payload(vol=0, bond=0, cell="bear|calm|ok"), dry_run=False, now=NOW)
        keys = self.pending_keys()
        self.assertLessEqual(len(keys), self.alerts.PENDING_MAX)
        self.assertIn("bond_off:" + ASOF, keys)
        # Свёрнутые в семейство ключи тоже обязаны пережить срез очереди (дубль
        # первой проверки, стоявший здесь, закреплял присутствие лишь одного
        # события — аудит 18.08.2026): без merged повтор слал бы слитую тройку
        # заново по отдельности.
        queued = next(e for e in self.state()["pending"]
                      if e["key"] == "bond_off:" + ASOF)
        self.assertIn("cell:%s:bear|calm|ok" % ASOF, queued.get("merged") or [])

    def test_dry_run_does_not_eat_the_transition(self):
        self.seed(payload(vol=0, bond=1, cell="bear|calm|stress"))
        self.alerts.run(payload(vol=0, bond=0, cell="bear|calm|ok"), dry_run=True, now=NOW)
        self.assertEqual(self.sent, [])
        evs = self.alerts.run(payload(vol=0, bond=0, cell="bear|calm|ok"), dry_run=False, now=NOW)
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
        for kind in ("buy_window_open", "source_stale", "health_dead"):
            self.assertIn(kind, kinds)
        # Снятый облигационный флаг — часть того же поворота, поэтому он не отдельное
        # сообщение, а подпункт окна входа (см. TestRegimeMerge).
        window = next(e for e in evs if e["kind"] == "buy_window_open")
        self.assertIn("bond_off:" + ASOF, window.get("merged") or [])

    def test_seeding_does_not_invent_a_core_flip(self):
        evs = self.alerts.run(payload(core=0.68), dry_run=False, now=NOW,
                              seed_payload=payload(core=0.68))
        self.assertNotIn("core_flip", [e["kind"] for e in evs])

    def test_empty_published_payload_is_not_a_snapshot(self):
        empty = payload(core=None, cell=None)
        self.assertFalse(self.alerts.seed_from_payload({}, empty, NOW))

    def test_existing_state_wins_over_snapshot(self):
        self.seed(payload(vol=0, bond=1, cell="bear|calm|stress"))
        state = self.state()
        self.assertFalse(self.alerts.seed_from_payload(state, payload(bond=0), NOW))

    def test_журнал_витрины_переживает_подмену_писателя(self):
        # Фолбэк с чистым STATE_DIR публиковал ленту из одних событий своего прогона
        # поверх журнала, который читатели видели минуту назад: авария VPS выглядела
        # как «панель забыла всё, что рассказывала».
        published = payload()
        published["events"] = [
            {"key": "old:1", "ts": "2026-08-10T10:00:00Z", "kind": "state_cell_change",
             "severity": "info", "text": "Смена режима: было и прошло."},
            {"key": "old:2", "ts": "2026-08-11T10:00:00Z", "kind": "core_flip",
             "severity": "warn", "text": "Разворот, о котором рассказывали вчера."},
            # Санитарное в опубликованной ленте не живёт по построению, но витрина —
            # внешний вход: если оно там оказалось, восстанавливать его нельзя.
            {"key": "ops:x", "ts": "2026-08-11T11:00:00Z", "kind": "source_stale",
             "severity": "warn", "text": "источник отстал"},
        ]
        self.alerts.run(payload(), dry_run=False, now=NOW, seed_payload=published)
        feed = self.alerts.payload_events()
        texts = " | ".join(e.get("text") or "" for e in feed)
        self.assertIn("было и прошло", texts,
                      "журнал прошлых событий потерян при подмене писателя")
        self.assertIn("рассказывали вчера", texts)
        self.assertNotIn("источник отстал", texts,
                         "санитарное событие въехало в журнал из витрины")
        # А дедуп по ключу продолжает работать: то же событие не задвоится.
        state = self.state()
        self.assertEqual([e.get("key") for e in state.get("feed") or []],
                         ["old:1", "old:2"])


class TestCbDecision(AlertsCase):
    """Сюрприз решения ЦБ меряется от консенсуса ПРОШЕДШЕГО заседания.

    Ставка попадает в ряд key_rate на 1–3 рабочих дня позже решения (16 из 17 смен
    с 2023 — ровно +3 дня), и к этому моменту consensus тайла смотрит уже на
    СЛЕДУЮЩЕЕ заседание. До 18.08.2026 сюрприз считался от него: в типовом сценарии
    флагманский вердикт («в линию» или «выше/ниже ожиданий») был всегда неверен, а
    поле last_consensus, заведённое тайлом ровно для этого, не читал никто.
    """

    def tile(self, key_rate, consensus=None, last_consensus=None):
        t = tile_cb(days_left=30, key_rate=key_rate, consensus=consensus)
        t["payload"]["last_meeting"] = "2026-09-11"
        t["payload"]["last_consensus"] = last_consensus
        return t

    def decide(self, old, new, consensus=None, last_consensus=None):
        self.seed(payload(monitors=[self.tile(old)]))
        evs = self.alerts.run(payload(monitors=[self.tile(new, consensus, last_consensus)]),
                              dry_run=False, now=NOW)
        return next(e for e in evs if e["kind"] == "cb_decision")

    def test_сюрприз_от_прошедшего_заседания_а_не_будущего(self):
        # Прошедшее: ждали удержания 16,00, ЦБ дал 15,00 (−100 б.п. к ожиданиям).
        # Будущее заседание с консенсусом 14,00 обязано быть проигнорировано:
        # от него «сюрприз» вышел бы +100 б.п. — противоположный знак.
        ev = self.decide(16.0, 15.0, consensus=14.0, last_consensus=16.0)
        self.assertEqual(ev["severity"], "warn")
        self.assertIn("ниже", ev["meaning"])
        self.assertIn("100", ev["meaning"])
        self.assertIn("16,00", ev["meaning"])
        self.assertNotIn("14,00", ev["meaning"])

    def test_без_консенсуса_прошедшего_честное_нечем(self):
        # Фолбэка на консенсус будущего заседания нет НАМЕРЕННО: честное «сказать
        # нечем» лучше сюрприза от чужих ожиданий.
        ev = self.decide(16.0, 15.0, consensus=14.0, last_consensus=None)
        self.assertEqual(ev["severity"], "info")
        self.assertIn("нечем", ev["meaning"])

    def test_совпадение_с_ожиданиями_спокойное(self):
        ev = self.decide(16.0, 15.0, last_consensus=15.0)
        self.assertEqual(ev["severity"], "info")
        self.assertIn("совпало", ev["meaning"])


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


class TestAuctionFreshness(AlertsCase):
    """Тревога об аукционе — про сегодняшний провал, а не про запись в истории.

    Оплачено 12.08.2026: ряд аукционов переехал с недоступного Минфина на биржевую
    доску, «последним» стал реальный аукцион 15.07 вместо нуля затравки от 05.08 —
    дата тайла изменилась, и правило разослало в телеграм «аукцион провален» про
    день ЧЕТЫРЁХНЕДЕЛЬНОЙ давности. Смена записи в истории не событие рынка.
    """

    def tile(self, date_, demand=88.1):
        return [{"id": "ofz_auctions", "status": "ok", "asof": date_,
                 "headline": "аукцион провален",
                 "payload": {"date": date_, "failed": True, "placed_bn": 0.0,
                             "demand_bn": demand}}]

    def fire(self, date_, demand=88.1, prev_date="2026-07-29"):
        state = {"last": {"auction_date": prev_date}}
        events = self.alerts.detect(payload(monitors=self.tile(date_, demand)),
                                    state, NOW)
        return [e for e in events if e["kind"] == "auction_failed"]

    def test_свежий_провал_доходит(self):
        self.assertEqual(len(self.fire(ASOF)), 1)

    def test_провал_месячной_давности_молчит(self):
        # мутация: убрать проверку возраста -> в канал уходит новость про 15.07,
        # как будто аукцион был сегодня.
        self.assertEqual(self.fire("2026-07-15"), [])

    def test_граница_окна(self):
        self.assertEqual(len(self.fire("2026-08-07")), 1)   # 4 суток — ещё событие
        self.assertEqual(self.fire("2026-08-06"), [])       # 5 суток — уже история

    def test_пустой_спрос_не_печатается_единицей(self):
        # «при спросе н/д млрд» — единица, приклеенная к отсутствующему числу.
        # Биржа спрос не раскрывает вовсе, и это надо сказать словами.
        text = self.fire(ASOF, demand=None)[0]["text"]
        self.assertNotIn("н/д млрд", text)
        self.assertIn("спрос биржа не раскрывает", text)

    def test_битая_дата_не_роняет_правило(self):
        self.assertEqual(self.fire("не дата"), [])


class TestDepositAnchor(AlertsCase):
    """Рост ставок меряется от последнего ОБЪЯВЛЕННОГО уровня, не от вчерашнего.

    Якорь, ползущий за снимком, съедал цикл плавных повышений целиком: десять декад
    по +0,04 п.п. (порог шума 0,05) — и события «ротация в акции отдаляется» не было
    ни разу при суммарном сдвиге +0,40. Та же ошибка уже чинилась у _core_flip.
    """

    def tile(self, pct):
        return [{"id": "deposit_spread", "status": "ok", "asof": ASOF,
                 "headline": "ставка", "payload": {"deposit_pct": pct,
                                                   "deposit_asof": ASOF,
                                                   "spread_pp": 1.5}}]

    def test_ползучий_рост_даёт_событие(self):
        self.seed(payload(monitors=self.tile(16.00)))
        got = []
        for i in range(1, 11):
            evs = self.alerts.run(payload(monitors=self.tile(16.00 + 0.04 * i)),
                                  dry_run=False, now=NOW + timedelta(days=i))
            got += [e for e in evs if e["kind"] == "deposit_uptick"]
        self.assertTrue(got, "цикл повышений +0,40 п.п. прошёл без единого события")
        # «Было» — уровень-якорь, о котором сообщали, а не вчерашний снимок.
        self.assertIn("→", got[0]["text"])

    def test_дрожь_ниже_порога_молчит(self):
        self.seed(payload(monitors=self.tile(16.00)))
        evs = self.alerts.run(payload(monitors=self.tile(16.04)),
                              dry_run=False, now=NOW)
        self.assertEqual([e for e in evs if e["kind"] == "deposit_uptick"], [])

    def test_снижение_опускает_якорь(self):
        # После отката вниз следующий цикл меряется от дна, а не от старого пика.
        self.seed(payload(monitors=self.tile(16.00)))
        self.alerts.run(payload(monitors=self.tile(15.00)), dry_run=False,
                        now=NOW + timedelta(days=1))
        evs = self.alerts.run(payload(monitors=self.tile(15.10)), dry_run=False,
                              now=NOW + timedelta(days=2))
        got = [e for e in evs if e["kind"] == "deposit_uptick"]
        self.assertTrue(got, "рост от дна потерялся за старым пиком")
        self.assertIn("15,00", got[0]["text"])


class TestOrfrBackdate(AlertsCase):
    """Смена asof назад — переезд источника, а не публикация.

    Тот же класс, что инцидент аукционов 12.08: восстановление стора сдвинуло дату
    задним числом, и в телеграм ушла «новость» месячной давности как сегодняшняя.
    """

    def tile(self, asof):
        return [{"id": "orfr", "status": "ok", "asof": asof,
                 "headline": "физлица купили на 12.3 млрд", "payload": {}}]

    def test_откат_даты_назад_молчит(self):
        self.seed(payload(monitors=self.tile("2026-07-31")))
        evs = self.alerts.run(payload(monitors=self.tile("2026-05-31")),
                              dry_run=False, now=NOW)
        self.assertEqual([e for e in evs if e["kind"] == "orfr_published"], [])

    def test_древний_релиз_не_подаётся_свежим(self):
        self.seed(payload(monitors=self.tile("2026-01-31")))
        evs = self.alerts.run(payload(monitors=self.tile("2026-02-28")),
                              dry_run=False, now=NOW)
        self.assertEqual([e for e in evs if e["kind"] == "orfr_published"], [])

    def test_настоящая_публикация_проходит(self):
        self.seed(payload(monitors=self.tile("2026-06-30")))
        evs = self.alerts.run(payload(monitors=self.tile("2026-07-31")),
                              dry_run=False, now=NOW)
        self.assertEqual(len([e for e in evs if e["kind"] == "orfr_published"]), 1)


class TestDedupKeepsLast(AlertsCase):
    def test_при_повторе_ключа_остаётся_свежая_копия(self):
        # Документированный контракт _dedup («остаётся ПОСЛЕДНЯЯ копия») не
        # проверялся: мутация keep-first проходила зелёной, а повтор из pending
        # затирал бы свежую версию события старой (аудит 18.08.2026).
        old_ev = {"key": "k", "ts": "2026-08-10T10:00:00Z", "kind": "core_flip",
                  "severity": "info", "text": "старая копия"}
        new_ev = {"key": "k", "ts": "2026-08-11T10:00:00Z", "kind": "core_flip",
                  "severity": "warn", "text": "свежая копия"}
        got = self.alerts._dedup([old_ev, new_ev])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["text"], "свежая копия")


class TestTexts(AlertsCase):
    """Каждое событие обязано нести число и один десятичный разделитель — точку."""

    def all_kinds(self):
        prev = {"cell": "bull|calm|ok", "core_value": 0.66, "core_sign": 1, "trend": 1,
                "vol": 0, "bond": 1, "health": "ok", "key_rate": 15.0, "deposit": 16.0,
                "orfr_asof": "2026-06-30", "auction_date": "2026-07-29",
                "sources": {"iss": "ok"}}
        state = {"last": prev, "core_sign_alerted": 1, "core_value_alerted": 0.66,
                 # Якорь депозитной ставки — уровень, о котором СООБЩАЛИ (не
                 # вчерашний снимок): рост меряется от него.
                 "deposit_alerted": 16.0}
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
        # merge=False — СЫРОЙ выход правил. После слияния семейства режима часть
        # видов в одном прогоне не появляется по построению (заголовком становится
        # один, остальные уходят подпунктами), и «вид недостижим» стало бы
        # неотличимо от «правило сломалось». Слияние проверяется отдельно —
        # TestRegimeMerge.
        events = self.alerts.detect(bad, state, NOW, merge=False)
        events += self.alerts.detect(payload(core=None, cell=None), {"last": prev},
                                     NOW, merge=False)
        events += self.alerts.detect(payload(bond=1, cell="bear|stress|stress"),
                                     {"last": dict(prev, bond=0)}, NOW, merge=False)
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
                self.assertIn("свои числа не опубликовал", ev["text"])
                continue
            self.assertRegex(ev["text"], r"\d", f"{ev['kind']} без единого числа")

    def test_decimal_separator_is_a_comma_everywhere(self):
        """Десятичный разделитель — ЗАПЯТАЯ, как на самой панели и в 837/838.

        Раньше здесь требовалась точка, и требование было по-своему логичным: лишь бы
        в одном сообщении не оказалось «+1.4%» рядом с «+1,4%». Но выбрана была не та
        сторона: панель рисует «+3,78%», соседние панели пишут «312 б.п.» и «1,97 %»,
        а в телеграм из этой же панели уезжало «+0.66» — то есть разнобой был не
        внутри сообщения, а между сообщением и всем остальным, что видит владелец.
        """
        for ev in self.all_kinds():
            # Даты (11.08.2026) из проверки вычёркиваем: точка там разделитель
            # частей даты, а не дробной части, и пишется она так намеренно.
            body = re.sub(r"\b\d{2}\.\d{2}\.\d{4}\b", " ", ev["text"])
            self.assertIsNone(re.search(r"\d\.\d", body),
                              f"{ev['kind']}: точка как десятичный разделитель — {ev['text']}")

    def test_minus_is_typographic(self):
        # Дефис в роли минуса рядом с переносами и тире читается как дефис.
        for ev in self.all_kinds():
            self.assertIsNone(re.search(r"(?<![\w])-\d", ev["text"]),
                              f"{ev['kind']}: минус дефисом — {ev['text']}")

    def test_severity_is_from_the_contract(self):
        for ev in self.all_kinds():
            self.assertIn(ev["severity"], ("info", "warn"))
            self.assertTrue(ev["key"] and ev["ts"].endswith("Z"))


class TestPayloadFeed(AlertsCase):
    def test_feed_keeps_tail_and_shape(self):
        self.seed(payload(vol=0, bond=1, cell="bear|calm|stress"))
        self.alerts.run(payload(vol=0, bond=0, cell="bear|calm|ok"), dry_run=False, now=NOW)
        feed = self.alerts.payload_events()
        self.assertTrue(feed)
        for row in feed:
            self.assertEqual(set(row), {"ts", "kind", "severity", "text", "comment"})
        self.assertLessEqual(len(feed), self.alerts.FEED_LIMIT)


class TestOpsSplit(AlertsCase):
    """Санитарные события живут в ops-канале и в ленту не попадают (контракт §6)."""

    def stale(self, now=NOW):
        """Прогон, который рождает и рыночное событие, и санитарное разом."""
        return self.alerts.run(
            payload(bond=0, cell="bear|stress|ok",
                    sources={"iss": {"status": "ok"}, "moex_press": {"status": "error",
                                                                     "asof": "2026-06-30"}}),
            dry_run=False, now=now)

    def test_ops_event_goes_to_the_ops_channel_only(self):
        # мутация: слать санитарные тем же каналом -> лента рынка тонет в отказах
        # источников, ровно как это выглядело в проде 12 августа.
        self.seed(payload(vol=0, bond=1, cell="bear|calm|stress"))
        events = self.stale()
        kinds = {e["kind"] for e in events}
        self.assertIn("source_stale", kinds, "проверка бессмысленна без санитарного события")
        ops = self.by_channel.get("ops") or []
        self.assertTrue(any("moex_press" in t for t in ops))
        self.assertFalse(any("moex_press" in t for t in self.by_channel.get("alerts") or []))

    def test_ops_event_never_reaches_the_feed(self):
        self.seed(payload(vol=0, bond=1, cell="bear|calm|stress"))
        self.stale()
        feed = self.alerts.payload_events()
        self.assertTrue(feed, "рыночные события в ленте остаться обязаны")
        self.assertFalse([e for e in feed if "moex_press" in (e.get("text") or "")])
        self.assertFalse([e for e in feed if e.get("kind") in self.alerts.OPS_KINDS])

    def test_old_ops_events_are_swept_out_of_a_saved_feed(self):
        # Состояние переживает обновление кода: до разделения санитарные лежали в
        # feed, и без вычистки они висели бы в журнале ещё двадцать событий.
        self.seed(payload(vol=0, bond=1, cell="bear|calm|stress"))
        state = self.state()
        state["feed"] = [{"key": "source_stale:iss:x", "kind": "source_stale",
                          "ts": "2026-08-10T00:00:00Z", "severity": "warn", "text": "старьё"}]
        self.alerts.save_state(state)
        self.assertEqual(self.alerts.payload_events(), [])

    def test_ops_events_are_not_mirrored_to_the_hub(self):
        self.seed(payload(vol=0, bond=1, cell="bear|calm|stress"))
        mirrored = []
        nexus = need(self, "pipeline.lib.nexus", "deliver", "SENT")
        with mock.patch.object(nexus, "deliver",
                               lambda ev: mirrored.append(ev["key"]) or nexus.SENT):
            self.stale()
        self.assertFalse([k for k in mirrored if k.startswith("source_stale")])


class TestComment(AlertsCase):
    """Комментарий модели: одинаковый во всех трёх местах, необязательный везде."""

    def test_comment_reaches_telegram_and_feed(self):
        self.seed(payload(vol=0, bond=1, cell="bear|calm|stress"))
        with mock.patch.object(self.alerts.commentary, "annotate",
                               lambda evs, pl, log=None: [e.update(comment="разбор") for e in evs
                                                          if not self.alerts.is_ops(e)]):
            self.alerts.run(payload(vol=0, bond=0, cell="bear|calm|ok"), dry_run=False, now=NOW)
        self.assertTrue(any("💬 разбор" in t for t in self.sent))
        self.assertTrue(all(e.get("comment") == "разбор" for e in self.alerts.payload_events()))

    def test_event_without_comment_is_a_plain_fact(self):
        # Ключа нет — комментатор возвращает None, и это НЕ отказ доставки.
        self.seed(payload(vol=0, bond=1, cell="bear|calm|stress"))
        self.alerts.run(payload(vol=0, bond=0, cell="bear|calm|ok"), dry_run=False, now=NOW)
        self.assertTrue(self.sent)
        self.assertFalse(any("💬" in t for t in self.sent))
        self.assertEqual(self.pending_keys(), [])

    def test_dry_run_does_not_call_the_model(self):
        # Прогон «на посмотреть» не должен зависеть от чужого провайдера.
        self.seed(payload(vol=0, bond=1, cell="bear|calm|stress"))
        calls = []
        with mock.patch.object(self.alerts.commentary, "annotate",
                               lambda *a, **k: calls.append(1)):
            self.alerts.run(payload(vol=0, bond=0, cell="bear|calm|ok"), dry_run=True, now=NOW)
        self.assertEqual(calls, [])


class TestRequeueKeepsStructure(AlertsCase):
    """Повтор из очереди обязан выглядеть так же, как первая отправка.

    События с 14.08 — структуры (title/fact/meaning/where), а телеграм рендерится
    из СТРУКТУРЫ: render_ops вообще не читает text. EVENT_FIELDS при переходе не
    расширили, и повтор из pending уходил владельцу пустым слэгом
    «842 · source_stale» без факта и «куда смотреть» — ровно в аварии, ради
    которой очередь существует (аудит 18.08.2026).
    """

    def test_повтор_санитарного_несёт_факт_и_адрес(self):
        self.seed(payload())
        self.online = False
        broken = payload(sources={"iss": {"status": "error", "lag_min": 4300,
                                          "asof": "2026-08-05"}})
        self.alerts.run(broken, dry_run=False, now=NOW)
        self.online = True
        # Тот же прогон повторяется через час: событие приходит ИЗ ОЧЕРЕДИ.
        self.alerts.run(broken, dry_run=False, now=NOW + timedelta(hours=1))
        ops = "\n".join(self.by_channel.get("ops") or [])
        self.assertIn("journalctl", ops, "повтор потерял «куда смотреть»")
        self.assertIn("не отвечает", ops, "повтор потерял заголовок события")
        self.assertIn("05.08.2026", ops, "повтор потерял факт с датой данных")

    def test_повтор_рыночного_не_слипается_в_жирный_ком(self):
        self.seed(payload(vol=0, bond=1, cell="bear|calm|stress"))
        self.online = False
        self.alerts.run(payload(vol=1, bond=0, cell="bear|stress|ok"),
                        dry_run=False, now=NOW)
        self.online = True
        self.alerts.run(payload(vol=1, bond=0, cell="bear|stress|ok"),
                        dry_run=False, now=NOW + timedelta(hours=1))
        market = "\n".join(self.by_channel.get("alerts") or [])
        self.assertIn("<b>", market)
        # Заголовок закрывается до подробностей: жирным — только он.
        head = market.split("</b>")[0]
        self.assertLess(len(head), 120,
                        "повтор завернул весь текст события в одну жирную строку")


class TestSourceStaleNamesSeries(AlertsCase):
    """Сообщение об отставшем источнике называет КОНКРЕТНЫЙ ряд.

    Статус семьи — статус худшего ряда в ней, и его имя семья знает (run.py
    кладёт meta.series). Без него владелец получал «источник iss отдаёт
    устаревшие данные» при двадцати живых рядах ISS и одном сломанном — адрес,
    по которому нечего чинить. Оплачено 20.08.2026: биржа сломала расчёт
    доходности ВДО, панель сказала «данные от 13.08», а индекс был свежий.
    """

    def fire(self, **extra):
        self.seed(payload())
        src = {"iss": dict({"status": "stale", "lag_min": 90, "asof": "2026-08-13"},
                           **extra)}
        evs = self.alerts.run(payload(sources=src), dry_run=False, now=NOW)
        return next(e for e in evs if e["kind"] == "source_stale")

    def test_имя_ряда_в_заголовке_и_факте(self):
        ev = self.fire(series="rucbhycp_yield")
        self.assertIn("rucbhycp_yield", ev["title"])
        self.assertIn("rucbhycp_yield", ev["fact"])
        self.assertIn("13.08.2026", ev["fact"])

    def test_без_имени_ряда_текст_прежний(self):
        # Семья может не знать ряда (старое состояние, чужой писатель) — тогда
        # сообщение остаётся про источник целиком, а не про «None».
        ev = self.fire()
        self.assertNotIn("None", ev["title"] + ev["fact"])
        self.assertIn("iss", ev["title"])

    def test_ряд_совпал_с_именем_семьи_не_дублируется(self):
        ev = self.fire(series="iss")
        self.assertNotIn("iss (iss)", ev["title"])


class TestOpsLatchAllKinds(AlertsCase):
    """Защёлка недоставленных санитарных — для ВСЕХ видов, не только health.

    До 18.08.2026 незащищённые source_stale и core_missing при недоставке умирали
    в pending по TTL 24 ч и не рождались больше никогда: снимок уже зафиксировал
    «error»/«None», и правило молчало как «уже сообщали». Стор частично теряется —
    панель публикует «нет данных» поверх рабочего вердикта, watchdog видит свежий
    Last-Modified, владелец не узнаёт об аварии вовсе.
    """

    def test_source_stale_переживает_суточный_обрыв_телеграма(self):
        self.seed(payload())
        self.online = False
        broken = payload(sources={"iss": {"status": "error", "lag_min": 4300,
                                          "asof": "2026-08-05"}})
        first = self.alerts.run(broken, dry_run=False, now=NOW)
        self.assertEqual([e["kind"] for e in first].count("source_stale"), 1)
        # Прошло больше суток: очередь повторов пуста, спасает только защёлка.
        self.online = True
        broken2 = dict(broken, asof_trading_day="2026-08-12")
        again = self.alerts.run(broken2, dry_run=False, now=NOW + timedelta(hours=30))
        self.assertEqual([e["kind"] for e in again].count("source_stale"), 1)
        self.assertIn("iss", " ".join(self.by_channel.get("ops") or []))

    def test_core_missing_переживает_суточный_обрыв(self):
        self.seed(payload(core=0.68))
        self.online = False
        lost = payload(core=None, cell=None)
        first = self.alerts.run(lost, dry_run=False, now=NOW)
        self.assertEqual([e["kind"] for e in first].count("core_missing"), 1)
        self.online = True
        lost2 = payload(core=None, cell=None, asof="2026-08-12")
        again = self.alerts.run(lost2, dry_run=False, now=NOW + timedelta(hours=30))
        self.assertEqual([e["kind"] for e in again].count("core_missing"), 1)

    def test_доставленное_не_повторяется(self):
        # Защёлка не имеет права превратиться в «состояние каждый день».
        self.seed(payload())
        broken = payload(sources={"iss": {"status": "error", "lag_min": 4300,
                                          "asof": "2026-08-05"}})
        self.alerts.run(broken, dry_run=False, now=NOW)
        again = self.alerts.run(dict(broken, asof_trading_day="2026-08-12"),
                                dry_run=False, now=NOW + timedelta(days=1))
        self.assertEqual([e["kind"] for e in again].count("source_stale"), 0)


class TestHealthReviewEvent(AlertsCase):
    """Событие регламента §7: переход, канал, защёлка после недоставки.

    Аудит 13.08.2026: правило не упоминалось в тестах ни разу. Зелёными проходили
    удаление правила, снятие защёлки (сообщение каждый прогон вместо перехода),
    вынос из OPS_KINDS (утечка санитарного события в ленту витрины и в хаб) и
    потеря события навсегда при недоставке.
    """

    def due(self, now=NOW, streak=7):
        return self.alerts.run(payload(review_due=True, streak=streak, health="dead"),
                               dry_run=False, now=now)

    def kinds(self, events):
        return [e["kind"] for e in events]

    def test_переход_даёт_ровно_одно_событие(self):
        self.seed(payload())
        first = self.due()
        self.assertEqual(self.kinds(first).count("health_review_due"), 1)
        second = self.due(NOW + timedelta(days=1))
        self.assertEqual(self.kinds(second).count("health_review_due"), 0)

    def test_уходит_только_в_ops_канал(self):
        # мутация: убрать kind из OPS_KINDS -> санитарное событие уезжает в канал
        # рынка, в журнал витрины и в ленту хаба.
        self.seed(payload())
        self.due()
        ops = " ".join(self.by_channel.get("ops") or [])
        alerts_ch = " ".join(self.by_channel.get("alerts") or [])
        self.assertIn("§7", ops)
        self.assertNotIn("§7", alerts_ch)

    def test_в_ленту_витрины_не_попадает(self):
        self.seed(payload())
        self.due()
        feed = self.alerts.payload_events()
        self.assertFalse([e for e in feed if e.get("kind") == "health_review_due"])
        self.assertFalse([e for e in self.state().get("feed") or []
                          if e.get("kind") == "health_review_due"])

    def test_недоставленное_событие_не_теряется(self):
        # ГЛАВНОЕ: снимок писался безусловно, поэтому недоставленное событие
        # исчезало навсегда — в ленту оно не идёт, очередь повторов живёт сутки,
        # а заново правило его не породит, пока держится та же серия.
        self.seed(payload())
        self.online = False
        self.assertEqual(self.kinds(self.due()).count("health_review_due"), 1)
        self.assertFalse(self.state()["last"].get("health_review_due"),
                         "защёлка не имеет права защёлкнуться без доставки")
        self.online = True
        # Прошли сутки — очередь повторов уже пуста, спасти может только защёлка.
        again = self.due(NOW + timedelta(hours=30))
        self.assertEqual(self.kinds(again).count("health_review_due"), 1)

    def test_доставленное_событие_защёлкивается(self):
        self.seed(payload())
        self.due()
        self.assertTrue(self.state()["last"].get("health_review_due"))

    def test_ненастроенный_ops_канал_не_защёлкивает(self):
        """«Канала нет» — не доставка, а именно ненастроенный ops-канал и есть
        умолчание: `ops/env.example` оставляет `ERROR_*` пустыми.

        Отличать «канал выключен» от «не смогли отправить» модуль доставки обязан
        (telegram.OFF), но для санитарного события оба исхода одинаковы: находка
        никуда не уехала. Защёлка держит её до настоящей отправки — иначе панель
        один раз сообщит в пустоту и замолчит навсегда.
        """
        os.environ.pop("ERROR_BOT_TOKEN")
        self.seed(payload())
        batch = self.due()
        self.assertEqual(self.kinds(batch).count("health_review_due"), 1)
        self.assertEqual([e["outcome"] for e in batch if e["kind"] == "health_review_due"],
                         [self.telegram.OFF])
        self.assertFalse(self.state()["last"].get("health_review_due"),
                         "защёлка сработала, хотя событие никуда не ушло")
        # Канал настроили — находка обязана дойти.
        os.environ["ERROR_BOT_TOKEN"] = "тест-ops"
        again = self.due(NOW + timedelta(hours=30))
        self.assertEqual(self.kinds(again).count("health_review_due"), 1)
        self.assertIn("§7", " ".join(self.by_channel.get("ops") or []))


class TestCommentaryScope(AlertsCase):
    """Комментатор спрашивается только про то, чего ещё нет в ленте.

    Часть правил рождает событие заново на КАЖДОМ такте (cb_reminder — все 169
    тактов в канун заседания), телеграм гасит их дедупом уже ПОСЛЕ похода к модели.
    Модель отвечает 150–200 с, а alerts.run стоит до публикации при дедлайне такта
    300 с: лишний запрос платит не деньгами, а риском не обновить витрину.
    """

    def setUp(self):
        super().setUp()
        self.asked = []
        patcher = mock.patch.object(
            self.alerts.commentary, "annotate",
            lambda evs, pl, log=None: self.asked.append([e["key"] for e in evs]))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_напоминание_цб_идёт_к_модели_один_раз(self):
        # cb_reminder рождается заново на КАЖДОМ такте, пока до заседания сутки, и
        # гасится дедупом телеграма уже после похода к модели. Это и есть боевой
        # случай: 169 тактов в сутки × 8 кануновых дней в году.
        self.seed(payload())
        eve = [tile_cb(days_left=1)]
        first = self.alerts.run(payload(monitors=eve), dry_run=False, now=NOW)
        self.assertIn("cb_reminder", [e["kind"] for e in first])
        self.assertTrue([k for batch in self.asked for k in batch if k.startswith("cb_reminder")],
                        "первый раз спросить обязаны")
        self.asked.clear()

        second = self.alerts.run(payload(monitors=eve), dry_run=False,
                                 now=NOW + timedelta(minutes=5))
        self.assertIn("cb_reminder", [e["kind"] for e in second],
                      "правило по-прежнему рождает событие — проверка о другом")
        self.assertEqual([k for batch in self.asked for k in batch], [],
                         "событие уже в ленте: второй разбор ему не нужен")


class TestRegimeMerge(AlertsCase):
    """Совпавшие переходы машины состояний — одно сообщение, а не три.

    ОПЛАЧЕНО ПРОГОНОМ 14.08.2026: ячейка — это и есть три её признака вместе,
    поэтому снятие облигационного флага НЕИЗБЕЖНО меняет ячейку, а окно входа —
    частный случай той же смены. Переход «ОФЗ вышли из стресса» рождал три
    уведомления подряд про одно движение.

    Приём взят у 837: внутренние переключения не рассылаются отдельно, а собираются
    объяснением к одному внешнему событию — блоком «Что за этим стоит».
    """

    def turn(self, before, after):
        """Один поворот рынка -> список событий этого прогона."""
        self.seed(payload(**before))
        return self.alerts.run(payload(**after), dry_run=False, now=NOW)

    def test_три_перехода_становятся_одним_сообщением(self):
        evs = self.turn(dict(vol=1, bond=1, cell="bear|stress|stress"),
                        dict(vol=1, bond=0, cell="bear|stress|ok"))
        self.assertEqual(len(evs), 1, [e["kind"] for e in evs])
        self.assertEqual(evs[0]["kind"], "buy_window_open")
        self.assertEqual(len(self.sent), 1, "в телеграм ушло больше одного сообщения")

    def test_заголовком_становится_самое_предметное(self):
        """«Долговой рынок вошёл в стресс» конкретнее, чем «Режим рынка сменился».

        Обратный порядок давал заголовки, из которых нельзя понять, ЧТО произошло.
        """
        evs = self.turn(dict(vol=0, bond=0, cell="bull|calm|ok"),
                        dict(vol=0, bond=1, cell="bull|calm|stress"))
        self.assertEqual([e["kind"] for e in evs], ["bond_flag_on"])
        self.assertIn("Режим рынка сменился", " ".join(evs[0]["causes"]))

    def test_свёрнутое_не_теряется_а_становится_подпунктом(self):
        evs = self.turn(dict(vol=1, bond=1, cell="bear|stress|stress"),
                        dict(vol=1, bond=0, cell="bear|stress|ok"))
        message = self.alerts.render(evs[0])
        self.assertIn("<b>Что за этим стоит</b>", message)
        self.assertIn("Долговой рынок вышел из стресса", message)
        # Смысловая часть свёрнутого — самое ценное, что в нём было.
        self.assertIn("Покупка просадок в акциях снова имеет смысл", message)

    def test_ключи_свёрнутых_переезжают_в_главное(self):
        """Дедуп телеграма и лента хаба работают по ключу.

        Без переноса повтор из очереди прислал бы свёрнутые события заново
        по отдельности — тем самым вернув ту же тройку сообщений.
        """
        evs = self.turn(dict(vol=1, bond=1, cell="bear|stress|stress"),
                        dict(vol=1, bond=0, cell="bear|stress|ok"))
        merged = evs[0].get("merged") or []
        self.assertIn("bond_off:" + ASOF, merged)
        self.assertTrue(any(k.startswith("cell:") for k in merged), merged)

    def test_подпункт_не_повторяет_строку_выше(self):
        # Свёрнутое событие описывает то же движение, и его «было → стало» сплошь
        # и рядом совпадает с уже напечатанным. Дубль в блоке объяснений —
        # не объяснение.
        evs = self.turn(dict(vol=1, bond=1, cell="bear|stress|stress"),
                        dict(vol=1, bond=0, cell="bear|stress|ok"))
        move = f"{evs[0]['before']} → {evs[0]['after']}"
        for cause in evs[0]["causes"]:
            self.assertNotIn(move, cause, f"подпункт повторяет строку движения: {cause}")

    def test_одиночный_переход_не_обрастает_блоком(self):
        evs = self.turn(dict(trend=1, vol=0, bond=0, cell="bull|calm|ok"),
                        dict(trend=0, vol=0, bond=0, cell="bear|calm|ok"))
        self.assertEqual([e["kind"] for e in evs], ["state_cell_change"])
        self.assertNotIn("Что за этим стоит", self.alerts.render(evs[0]))

    def test_наклон_ядра_не_сливается_с_воротами(self):
        """Наклон и ворота — разные слои модели, и слияние спрятало бы одно за другим.

        «Сначала ворота, потом наклон» (docs/ARCHITECTURE.md): они меняются
        независимо друг от друга, это два разных факта, а не один поворот.
        """
        self.seed(payload(core=0.66, vol=0, bond=0, cell="bull|calm|ok"))
        evs = self.alerts.run(payload(core=-0.66, vol=0, bond=1, cell="bull|calm|stress"),
                              dry_run=False, now=NOW)
        kinds = [e["kind"] for e in evs]
        self.assertIn("core_flip", kinds)
        self.assertIn("bond_flag_on", kinds)
        self.assertEqual(len(kinds), 2, kinds)

    def test_тревожность_берётся_худшая(self):
        # Спокойная смена режима, свёрнутая с тревожным флагом, обязана остаться
        # тревогой: иначе слияние понижает важность сообщения.
        evs = self.turn(dict(vol=0, bond=0, cell="bull|calm|ok"),
                        dict(vol=0, bond=1, cell="bull|calm|stress"))
        self.assertEqual(evs[0]["severity"], "warn")


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
        return self.alerts.run(payload(vol=0, bond=0, cell="bear|calm|ok"), dry_run=False, now=now)

    def test_event_reaches_the_hub_under_its_own_key(self):
        self.seed(payload(vol=0, bond=1, cell="bear|calm|stress"))
        events = self.bond_off()
        # Снятие флага тянет за собой смену ячейки и окно входа — в ленту уезжают
        # все три, по одному ключу на событие.
        self.assertEqual(sorted(self.mirrored), sorted(e["key"] for e in events))
        self.assertIn("bond_off:" + ASOF, self.mirrored)
        self.assertEqual(self.pending_keys(), [])

    def test_dead_hub_retries_without_a_second_telegram_message(self):
        # мутация: считать событие доставленным по одному телеграму -> упавший хаб
        # теряет событие навсегда, повторить его уже некому.
        self.seed(payload(vol=0, bond=1, cell="bear|calm|stress"))
        self.hub_outcome = self.nexus.FAIL
        self.bond_off()
        self.assertIn("bond_off:" + ASOF, self.pending_keys())
        self.hub_outcome = self.nexus.SENT
        self.bond_off(NOW + timedelta(hours=1))
        self.assertEqual(self.pending_keys(), [])
        self.assertEqual(self.mirrored.count("bond_off:" + ASOF), 2)
        # Телеграм на повторе отвечает DUP: второго сообщения в канал не уходит.
        self.assertEqual(len([t for t in self.sent if "Долговой рынок вышел из стресса" in t]), 1)

    def test_unconfigured_hub_never_blocks_delivery(self):
        # мутация: OFF трактовать как FAIL -> на машине без NEXUS_* очередь повторов
        # растёт вечно, а телеграм на каждом прогоне отвечает DUP впустую.
        self.seed(payload(vol=0, bond=1, cell="bear|calm|stress"))
        self.hub_outcome = self.nexus.OFF
        self.bond_off()
        self.assertEqual(self.pending_keys(), [])

    def test_dry_run_touches_neither_channel(self):
        self.seed(payload(vol=0, bond=1, cell="bear|calm|stress"))
        self.alerts.run(payload(vol=0, bond=0, cell="bear|calm|ok"), dry_run=True, now=NOW)
        self.assertEqual(self.mirrored, [])
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
