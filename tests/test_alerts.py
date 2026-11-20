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


LEGS = [{"id": "usd_mom63", "label": "Девальвационный моментум",
         "contrib": -0.15, "without": -0.39},
        {"id": "slope_10_2", "label": "Наклон кривой ОФЗ (10Y−2Y)",
         "contrib": 0.82, "without": -0.88},
        {"id": "urals_rub_gap", "label": "Гэп рублёвой бочки к тренду",
         "contrib": -1.60, "without": 0.34}]


# Те же слагаемые при оценке «за акции» (+0,35): знак держит кривая ОФЗ.
LEGS_UP = [{"id": "usd_mom63", "label": "Девальвационный моментум",
            "contrib": 0.15, "without": 0.45},
           {"id": "slope_10_2", "label": "Наклон кривой ОФЗ (10Y−2Y)",
            "contrib": 1.40, "without": -0.18},
           {"id": "urals_rub_gap", "label": "Гэп рублёвой бочки к тренду",
            "contrib": -0.50, "without": 0.78}]


def position_block(state, since=ASOF, comp=None, by_score=False, **extra):
    """Блок позиции слоя решения (verdict.position) в объёме правил о позиции.

    По умолчанию позицию меняют ворота (как в первых редакциях этих проверок);
    `by_score=True` — её меняет оценка рынка при открытых воротах, как 06.11.2026.
    """
    flat = state == "flat"
    if by_score:
        reason = "comp_neg" if flat else "comp_pos"
        text = ("оценка рынка ушла ниже −0,2 в день решения" if flat
                else "оценка рынка поднялась выше +0,2 в день решения")
        held = {"by": "comp" if flat else "open", "legs": LEGS,
                "leg": "urals_rub_gap" if flat else None}
        gate_open = True
    else:
        reason = "gate_close" if flat else "gate_open"
        text = ("ворота закрылись: ОФЗ под давлением (просадка RGBI глубже −4%)" if flat
                else "ворота открылись: ОФЗ вышли из стресса (просадка RGBI мельче −3%)")
        held = {"by": "gate" if flat else "open", "legs": LEGS_UP, "leg": None}
        gate_open = not flat
    if comp is None:
        comp = -0.31 if (flat and by_score) else 0.35
    block = {"state": state, "since": since, "reason": reason, "reason_text": text,
             "cash_rate": 12.9, "execute": "на следующем закрытии",
             "decision_day": ASOF, "is_decision_day": False, "next_decision": "2026-08-14",
             "comp_daily": comp, "comp_threshold": 0.2,
             "comp_state": -1 if (flat and by_score) else 1,
             "gate_open": gate_open, "held_by": held,
             "conditions": (
                 ["оценка рынка выше +0,2 в день решения (сейчас −0,31)"] if flat and by_score
                 else ["ворота откроются, когда снимется любой из флагов: ОФЗ: просадка "
                       "RGBI мельче −3% (сейчас −5,1%)"] if flat
                 else ["любой из: ворота закроются — нужны все три флага сразу; "
                       "оценка рынка ниже −0,2 в день решения (сейчас +0,35)"])}
    block.update(extra)
    return block


def gate_block(trend, vol, bond):
    """Блок ворот (states.gate) с теми же флагами, что у сырых признаков."""
    return {"trend": trend, "vol": vol, "bond": bond,
            "open": not (trend == 0 and vol == 1 and bond == 1),
            "cell_code": "%s|%s|%s" % ("bull" if trend == 1 else "bear",
                                       "stress" if vol == 1 else "calm",
                                       "stress" if bond == 1 else "ok")}


def payload(core=0.68, trend=0, vol=1, bond=1, asof=ASOF, health="ok",
            cell="bear|stress|stress", monitors=None, sources=None,
            review_due=False, streak=0, position=None, gate="auto",
            run_mode=None, compute_error=None):
    """Витрина в объёме, который читают правила алертов.

    `gate` — блок ворот с гистерезисом. По умолчанию ("auto") флаги ворот повторяют
    сырые признаки: события о состоянии рынка стоят на воротах, и фикстура, где
    слои сошлись, — простейший случай. `gate=None` — блока ворот нет (старый
    payload), словарь — ворота, которые стоят иначе, чем сырые признаки.
    `position` — "long"/"flat" (блок по умолчанию) или готовый блок позиции.
    """
    if gate == "auto":
        gate = (gate_block(trend, vol, bond)
                if all(x in (0, 1) for x in (trend, vol, bond)) else None)
    if isinstance(position, str):
        position = position_block(position)
    return {
        **({"run_mode": run_mode} if run_mode else {}),
        **({"compute_error": compute_error} if compute_error else {}),
        "asof_trading_day": asof,
        "core": {"value": core, "sign": (0 if core is None else (1 if core > 0 else -1)),
                 "health": {"status": health, "n": 24, "ic_24m": -0.02,
                            "review_due": review_due, "below_zero_months": streak,
                            "below_since": "2025-09-30" if streak else None,
                            "review_months": 12}},
        "states": {"current": {"trend": trend, "vol": vol, "bond": bond},
                   "distances": [{"id": "bond", "text": "просадка RGBI −1,2% от максимума"},
                                 {"id": "trend", "text": "индекс на 2,6% ниже MA200"},
                                 {"id": "vol", "text": "реализованная вола 18,1%"}],
                   **({"gate": gate} if gate else {})},
        "verdict": {"cell_code": cell, "cell_label": "токсичная",
                    "core_label": "умеренно за акции",
                    "cell_stats": {"mean_fwd1m_pct": -0.55, "hit": 0.4, "n": 12},
                    **({"position": position} if position else {})},
        "monitors": monitors if monitors is not None else [tile_cb()],
        "sources": sources if sources is not None else {"iss": {"status": "ok"}},
    }


# РАБОЧЕЕ РЫНОЧНОЕ СОБЫТИЕ для проверок доставки, очереди, зеркала и разбора. До
# 20.11.2026 им был снятый флаг ОФЗ, но семейство флагов ворот теперь живёт только в
# журнале страницы и в телеграм не ходит вовсе. Решение ЦБ — обычное рыночное событие
# без особых правил: ставка в снимке 15 %, в прогоне 14 %.
RATE_TITLE = "Банк России снизил ключевую ставку"
RATE_KEY = "cb_decision:%s:14.0" % ASOF


def rate_before(**kw):
    return payload(monitors=[tile_cb(key_rate=15.0)], **kw)


def rate_after(**kw):
    return payload(monitors=[tile_cb(key_rate=14.0)], **kw)


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
                          "ERROR_BOT_TOKEN", "ERROR_CHAT_ID", "ANTHROPIC_API_KEY")}
        os.environ.update(STATE_DIR=self.tmp.name, TELEGRAM_BOT_TOKEN="тест",
                          TELEGRAM_CHAT_ID="-100", ERROR_BOT_TOKEN="тест-ops",
                          ERROR_CHAT_ID="-200")
        # Внешние каналы гасим явно (правило 2 набора: в сеть не ходим). На VPS эти
        # переменные заданы, и без снятия набор, запущенный там, постучался бы в хаб
        # и в Anthropic настоящими (платными) запросами на каждое событие.
        for key in ("NEXUS_EVENTS_URL", "NEXUS_INGEST_TOKEN", "ANTHROPIC_API_KEY"):
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
        events = self.alerts.run(payload(bond=0, health="review", review_due=True, streak=12,
                                         position="flat"), dry_run=False, now=NOW)
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
    """Окно входа стоит на флагах ВОРОТ: нервная торговля при спокойных ОФЗ."""

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
        # как о новом переходе, хотя сочетание не менялось неделями.
        self.alerts.save_state({"last": {"cell": "bear|calm|ok", "core_value": 0.68,
                                         "core_sign": 1, "trend": 0, "vol": None,
                                         "bond": None, "health": "ok",
                                         "gate": {"trend": 0, "vol": None, "bond": None,
                                                  "open": None}},
                                "core_sign_alerted": 1})
        evs = self.alerts.detect(payload(vol=1, bond=0, cell="bear|stress|ok"),
                                 self.state(), NOW)
        self.assertNotIn("buy_window_open", [e["kind"] for e in evs])

    def test_сырые_признаки_без_ворот_окна_не_дают(self):
        # мутация: вернуть правило на states.current -> окно входа снова зовёт
        # добавлять риск по сырому флагу, пока флаг ворот стоит.
        stays = gate_block(0, 1, 1)
        self.seed(payload(vol=1, bond=1, gate=stays))
        evs = self.alerts.run(payload(vol=1, bond=0, cell="bear|stress|ok", gate=stays),
                              dry_run=False, now=NOW)
        self.assertEqual(evs, [])


class TestDelivery(AlertsCase):
    def test_undelivered_event_waits_in_queue_without_marker(self):
        self.seed(rate_before())
        self.online = False
        evs = self.alerts.run(rate_after(), dry_run=False, now=NOW)
        self.assertTrue(evs)
        self.assertFalse(any(e["delivered"] for e in evs))
        self.assertEqual(self.markers(), [], "маркер дедупа не имеет права опережать доставку")
        self.assertIn(RATE_KEY, self.pending_keys())

    def test_repaired_telegram_delivers_pending_exactly_once(self):
        self.seed(rate_before())
        self.online = False
        self.alerts.run(rate_after(), dry_run=False, now=NOW)
        self.online = True
        self.alerts.run(rate_after(), dry_run=False, now=NOW + timedelta(hours=1))
        self.assertEqual(len([t for t in self.sent if RATE_TITLE in t]), 1)
        self.assertEqual(self.pending_keys(), [])
        # третий прогон ничего не повторяет
        self.alerts.run(rate_after(), dry_run=False, now=NOW + timedelta(hours=2))
        self.assertEqual(len([t for t in self.sent if RATE_TITLE in t]), 1)

    def test_stale_pending_is_dropped_after_a_day(self):
        self.seed(rate_before())
        self.online = False
        self.alerts.run(rate_after(), dry_run=False, now=NOW)
        self.online = True
        self.alerts.run(rate_after(), dry_run=False, now=NOW + timedelta(hours=25))
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
        self.seed(rate_before())
        state = self.state()
        state["pending"] = old
        self.alerts.save_state(state)
        self.online = False
        self.alerts.run(rate_after(), dry_run=False, now=NOW)
        keys = self.pending_keys()
        self.assertLessEqual(len(keys), self.alerts.PENDING_MAX)
        self.assertIn(RATE_KEY, keys)

    def test_dry_run_does_not_eat_the_transition(self):
        self.seed(rate_before())
        self.alerts.run(rate_after(), dry_run=True, now=NOW)
        self.assertEqual(self.sent, [])
        evs = self.alerts.run(rate_after(), dry_run=False, now=NOW)
        self.assertIn("cb_decision", [e["kind"] for e in evs])


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
        published = payload(core=0.68, bond=1, position="flat")
        evs = self.alerts.run(payload(core=0.61, bond=0, cell="bear|stress|ok",
                                      health="review", review_due=True, streak=12,
                                      position="long",
                                      sources={"iss": {"status": "error", "lag_min": 4300,
                                                       "asof": "2026-08-05"}}),
                              dry_run=False, now=NOW, seed_payload=published)
        kinds = [e["kind"] for e in evs]
        for kind in ("buy_window_open", "source_stale", "health_review", "position_change"):
            self.assertIn(kind, kinds)
        # Снятый облигационный флаг — часть того же поворота, поэтому он не отдельная
        # запись, а подпункт окна входа (см. TestRegimeMerge).
        window = next(e for e in evs if e["kind"] == "buy_window_open")
        self.assertIn("bond_off:" + ASOF, window.get("merged") or [])
        # Флаги ворот восстановлены из витрины, но в телеграм не идут: журнал.
        self.assertFalse([t for t in self.sent if "Окно входа" in t])

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


class TestCbHold(AlertsCase):
    """Сохранение ставки — тоже решение, и о нём надо сказать.

    ОПЛАЧЕНО ЗАСЕДАНИЕМ 11.09.2026: ЦБ сохранил 14,00%, панель промолчала совсем.
    Владелец получил «Завтра заседание Банка России» и тишину — неотличимую от
    «ничего не произошло». cb_decision срабатывает только на ИЗМЕНЕНИЕ ряда
    key_rate, а при сохранении ряд не меняется никогда.

    Ключевая тонкость — КОГДА можно так сказать. Решение попадает в ряд ЦБ на
    следующий рабочий день (замер по всем пяти сменам 2026 года: ровно +1 торговый
    день). В день заседания «ставка не изменилась» означает лишь «ещё не дошло».
    """

    def tile(self, since=4, key_rate=14.0, was=14.0, cons=14.0, meeting="2026-09-11"):
        return {"id": "cb_meeting", "status": "ok", "asof": ASOF,
                "headline": "До заседания 41 дн.",
                "payload": {"days_left": 41, "next_meeting": "2026-10-23",
                            "key_rate": key_rate, "consensus": None,
                            "last_meeting": meeting, "last_consensus": cons,
                            "rate_at_last_meeting": was,
                            "days_since_last_meeting": since,
                            "priced_text": "RUSFAR 3M у ключа"}}

    def fire(self, **kw):
        self.seed(payload())
        evs = self.alerts.run(payload(monitors=[self.tile(**kw)]), dry_run=False, now=NOW)
        return [e for e in evs if e["kind"] == "cb_hold"]

    def test_сохранение_сообщается_после_подтверждения(self):
        ev = self.fire()[0]
        self.assertIn("сохранил", ev["title"])
        self.assertIn("14,00%", ev["detail"])
        # Год текущий — не пишется (стандарт сообщений §3).
        self.assertIn("Заседание 11.09.", ev["detail"])
        self.assertNotIn("2026", ev["detail"])
        # «было → стало» у сохранения одинаковы и в сообщение не идут.
        self.assertFalse(ev.get("before"))
        self.assertFalse(ev.get("after"))

    def test_в_день_заседания_молчим(self):
        """«Ставка не изменилась» в день заседания — это «ещё не дошло».

        мутация: снять порог CB_HOLD_CONFIRM_DAYS -> панель объявит сохранением
        любое заседание в тот же вечер, включая то, где ставку снизили.
        """
        self.assertEqual(self.fire(since=0), [])
        self.assertEqual(self.fire(since=3), [])

    def test_при_изменении_ставки_события_нет(self):
        # Ставка на день заседания 14,25, сейчас 14,00 — это работа cb_decision.
        self.assertEqual(self.fire(was=14.25, key_rate=14.0), [])

    def test_совпадение_с_консенсусом_названо(self):
        ev = self.fire(cons=14.0)[0]
        self.assertIn("совпало", ev["meaning"])
        self.assertEqual(ev["severity"], "info")

    def test_расхождение_с_консенсусом_это_сюрприз(self):
        ev = self.fire(cons=13.75)[0]
        self.assertIn("25", ev["meaning"])
        self.assertIn("выше", ev["meaning"])
        self.assertEqual(ev["severity"], "warn")

    def test_без_консенсуса_не_выдумываем_сюрприз(self):
        # Сюрприза нет, и фразы «сказать нечем» тоже нет: пустое не печатается
        # (стандарт сообщений §1).
        ev = self.fire(cons=None)[0]
        self.assertFalse(ev.get("meaning"))
        self.assertNotIn("ожидани", ev["text"])
        self.assertEqual(ev["severity"], "info")

    def test_без_заседания_в_окне_молчим(self):
        # last_meeting гаснет через неделю (тайл), и событие не должно оживать.
        self.assertEqual(self.fire(meeting=None), [])

    def test_второй_прогон_не_повторяет(self):
        self.seed(payload())
        first = self.alerts.run(payload(monitors=[self.tile()]), dry_run=False, now=NOW)
        self.assertTrue([e for e in first if e["kind"] == "cb_hold"])
        sent_before = len(self.sent)
        self.alerts.run(payload(monitors=[self.tile()]), dry_run=False, now=NOW)
        self.assertEqual(len(self.sent), sent_before, "сохранение ушло в канал дважды")


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
        self.assertFalse(ev.get("meaning"), "сюрприз от чужих ожиданий")
        self.assertNotIn("14,00", ev["text"])

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

    def uptick_text(self, spread):
        def tile(pct):
            return [{"id": "deposit_spread", "status": "ok", "asof": ASOF,
                     "headline": "ставка", "payload": {"deposit_pct": pct,
                                                       "deposit_asof": ASOF,
                                                       "spread_pp": spread}}]
        # Первый прогон после затравки только запоминает якорь ставки, событие даёт второй.
        self.seed(payload(monitors=tile(12.88)))
        self.alerts.run(payload(monitors=tile(12.88)), dry_run=False, now=NOW)
        evs = self.alerts.run(payload(monitors=tile(12.95)), dry_run=False,
                              now=NOW + timedelta(days=1))
        got = [e for e in evs if e["kind"] == "deposit_uptick"]
        self.assertTrue(got)
        return got[0]["text"]

    def test_вклад_выше_дивидендов_называется_больше(self):
        # spread_pp = дивдоходность − вклад. Живой случай 14.09.2026: вклад 12,95%,
        # дивиденды 8,5%, спред −4,4 — вклад платит БОЛЬШЕ. Мутация: вернуть условие
        # «больше при spread > 0» — и событие снова скажет обратное правде.
        text = self.uptick_text(-4.43)
        self.assertIn("больше дивидендной доходности", text)
        self.assertNotIn("меньше дивидендной доходности", text)

    def test_дивиденды_выше_вклада_называются_меньше(self):
        text = self.uptick_text(1.2)
        self.assertIn("меньше дивидендной доходности", text)


class TestLegacyDepositSign(AlertsCase):
    """Записи 14.09 и 22.09.2026 со знаком наоборот исправляются в ленте один раз.

    Код события исправлен 03.10.2026, но лента витрины хранит старый текст — и
    комментарий модели, построенный на неверном факте («дивидендный премиум 4,5 п.п.»).
    """

    OLD = ("Банки подняли ставки по вкладам 12,88% → 12,95%. Прибавка 0,07 процентного "
           "пункта. Вклад теперь даёт на 4,5 процентного пункта меньше дивидендной "
           "доходности акций. Чем выгоднее вклад, тем дольше деньги не пойдут с депозитов "
           "на биржу.")

    def write(self, feed):
        import json
        self.alerts.save_state({"feed": feed})
        return json.loads(json.dumps(self.alerts.load_state()["feed"]))

    def test_старая_запись_исправлена_и_без_комментария(self):
        ev = {"key": "deposit_uptick:2026-09-11", "kind": "deposit_uptick", "severity": "info",
              "ts": "2026-09-14T15:04:46Z", "text": self.OLD,
              "meaning": "Вклад теперь даёт на 4,5 процентного пункта меньше дивидендной "
                         "доходности акций.",
              "comment": "При нынешнем дивидендном премиум в 4,5 п.п. …"}
        got = self.write([ev])[0]
        # мутация: не вызывать правку в load_state -> в ленте остаётся «меньше»
        self.assertIn("на 4,5 процентного пункта больше дивидендной доходности", got["text"])
        self.assertIn("больше дивидендной доходности", got["meaning"])
        self.assertIn("Исправлено 03.10.2026", got["text"])
        self.assertNotIn("comment", got)
        # второй прогон не переворачивает обратно и не дописывает пометку дважды
        again = self.write([got])[0]
        self.assertEqual(again["text"], got["text"])
        payload_text = self.alerts.payload_events([])[0]["text"]
        self.assertIn("больше дивидендной доходности", payload_text)

    def test_новые_записи_не_трогаются(self):
        text = self.OLD.replace("меньше", "больше")
        ev = {"key": "deposit_uptick:2026-10-12", "kind": "deposit_uptick", "severity": "info",
              "ts": "2026-10-14T15:00:00Z", "text": text, "comment": "к"}
        got = self.write([ev])[0]
        self.assertEqual(got["text"], text)
        self.assertEqual(got["comment"], "к")


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
                "gate": {"trend": 1, "vol": 0, "bond": 1, "open": True},
                "comp_daily": -0.25, "oil_contrib": -1.42, "legs_n": 3,
                # cbr лежал на прошлом прогоне и в этом ожил: сценарий обязан
                # порождать и тревогу (iss), и отбивку (cbr), иначе отбивка
                # ускользает от инвариантов ниже — числа, запятой и минуса. То же у
                # рядов решения: zcyc был здоров и отстал, usd_cbr отставал и ожил.
                "position": "long", "sources": {"iss": "ok", "cbr": "error"},
                "core_series": {"zcyc": "ok", "usd_cbr": "error"}}
        state = {"last": prev, "core_sign_alerted": 1, "core_value_alerted": 0.66,
                 # Якорь депозитной ставки — уровень, о котором СООБЩАЛИ (не
                 # вчерашний снимок): рост меряется от него.
                 "deposit_alerted": 16.0,
                 # Последний сообщённый месяц налоговой цены нефти: прогон ниже
                 # приносит следующий.
                 "urals_alerted": {"month": "2026-06-30", "usd": 63.52}}
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
                 "payload": {"deposit_pct": 16.4, "deposit_asof": ASOF, "spread_pp": 8.1}},
                {"id": "rub_barrel", "status": "ok", "asof": "2026-07-31",
                 "headline": "налоговая бочка",
                 "payload": {"tax_month": "2026-07-31", "urals_usd": 59.02}}]
        core_rows = {"zcyc": {"series": "zcyc_y10", "status": "stale", "asof": "2026-08-05",
                              "lag_min": 4300, "stale_reason": "data",
                              "label": "Кривая бескупонной доходности ОФЗ"}}
        bad = payload(core=-0.66, trend=0, vol=1, bond=0, cell="bear|stress|ok",
                      health="review", review_due=True, streak=12, position="flat",
                      monitors=mons, run_mode="daily",
                      sources={"iss": {"status": "stale", "lag_min": 4300,
                                       "asof": "2026-08-05", "series": "rvi",
                                       "bad_series": [
                                           {"series": "rvi", "status": "stale",
                                            "asof": "2026-08-05", "lag_min": 4300,
                                            "core": False},
                                           dict(core_rows["zcyc"], core=True)],
                                       "core_affected": True, "core_series": core_rows},
                               "cbr": {"status": "ok", "asof": ASOF, "bad_series": [],
                                       "core_affected": False,
                                       "core_series": {"usd_cbr": {
                                           "series": "usd_cbr", "status": "ok",
                                           "asof": ASOF, "label": "Официальный курс USD"}}}})
        # merge=False — СЫРОЙ выход правил. После слияния семейства режима часть
        # видов в одном прогоне не появляется по построению (заголовком становится
        # один, остальные уходят подпунктами), и «вид недостижим» стало бы
        # неотличимо от «правило сломалось». Слияние проверяется отдельно —
        # TestRegimeMerge.
        events = self.alerts.detect(bad, state, NOW, merge=False)
        # Расчёт упал: пустая оценка и пометка compute_error в витрине.
        events += self.alerts.detect(
            payload(core=None, cell=None, compute_error="KeyError @ 2026-08-11T16:05:00Z"),
            {"last": prev}, NOW, merge=False, crash="KeyError: 'imoex'")
        events += self.alerts.detect(
            payload(bond=1, cell="bear|stress|stress"),
            {"last": dict(prev, bond=0, compute_error="KeyError @ 2026-08-10T20:55:00Z",
                          gate={"trend": 0, "vol": 1, "bond": 0, "open": True})},
            NOW, merge=False)
        # Предупреждение перед днём решения: позиция «акции», оценка ниже порога.
        events += self.alerts.detect(
            payload(position=position_block("long", comp=-0.31, by_score=True),
                    run_mode="daily"), {"last": prev}, NOW, merge=False)
        # Отбой: предупреждение дошло, день решения наступил, позиция прежняя.
        events += self.alerts.detect(
            payload(position=position_block("long", comp=-0.15, by_score=True)),
            {"last": prev, "watch": {"key": "position_watch:%s:flat" % PREV_ASOF,
                                     "decision": PREV_ASOF, "to": "flat", "delivered": True}},
            NOW, merge=False)
        # Главное событие не доходит до канала рынка — и отбивка, когда дошло.
        stuck = {"key": "position:x", "kind": "position_change", "title": "Позиция: переход в деньги",
                 "before": "акции", "after": "деньги (ставка 12,9%)", "exec_day": "2026-08-12",
                 "ts": (NOW - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "outcome": self.telegram.FAIL, "delivered": False}
        memo = {}
        events += self.alerts._stuck_events([stuck], memo, NOW)
        events += self.alerts._stuck_events([dict(stuck, delivered=True)],
                                            {"stuck": {"position:x": "t"}}, NOW)
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
                     "auction_failed", "deposit_uptick", "source_stale", "source_ok",
                     "health_review",
                     "core_missing", "lease_lost", "payload_oversize", "position_change",
                     # аудит 20.11.2026
                     "position_watch", "position_watch_off", "urals_month",
                     "core_series_stale", "core_series_ok", "compute_failed", "compute_ok",
                     "delivery_stuck", "delivery_ok"):
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
            # Даты (11.08.2026, 11.08) из проверки вычёркиваем: точка там разделитель
            # частей даты, а не дробной части, и пишется она так намеренно.
            body = re.sub(r"\b\d{2}\.\d{2}(?:\.\d{4})?\b", " ", ev["text"])
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
        self.seed(rate_before())
        self.alerts.run(rate_after(), dry_run=False, now=NOW)
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
            rate_after(sources={"iss": {"status": "ok"},
                                "moex_press": {"status": "error", "asof": "2026-06-30"}}),
            dry_run=False, now=now)

    def test_ops_event_goes_to_the_ops_channel_only(self):
        # мутация: слать санитарные тем же каналом -> лента рынка тонет в отказах
        # источников, ровно как это выглядело в проде 12 августа.
        self.seed(rate_before())
        events = self.stale()
        kinds = {e["kind"] for e in events}
        self.assertIn("source_stale", kinds, "проверка бессмысленна без санитарного события")
        ops = self.by_channel.get("ops") or []
        self.assertTrue(any("moex_press" in t for t in ops))
        self.assertFalse(any("moex_press" in t for t in self.by_channel.get("alerts") or []))

    def test_ops_event_never_reaches_the_feed(self):
        self.seed(rate_before())
        self.stale()
        feed = self.alerts.payload_events()
        self.assertTrue(feed, "рыночные события в ленте остаться обязаны")
        self.assertFalse([e for e in feed if "moex_press" in (e.get("text") or "")])
        self.assertFalse([e for e in feed if e.get("kind") in self.alerts.OPS_KINDS])

    def test_old_ops_events_are_swept_out_of_a_saved_feed(self):
        # Состояние переживает обновление кода: до разделения санитарные лежали в
        # feed, и без вычистки они висели бы в журнале ещё двадцать событий.
        self.seed(rate_before())
        state = self.state()
        state["feed"] = [{"key": "source_stale:iss:x", "kind": "source_stale",
                          "ts": "2026-08-10T00:00:00Z", "severity": "warn", "text": "старьё"}]
        self.alerts.save_state(state)
        self.assertEqual(self.alerts.payload_events(), [])

    def test_ops_events_are_not_mirrored_to_the_hub(self):
        self.seed(rate_before())
        mirrored = []
        nexus = need(self, "pipeline.lib.nexus", "deliver", "SENT")
        with mock.patch.object(nexus, "deliver",
                               lambda ev: mirrored.append(ev["key"]) or nexus.SENT):
            self.stale()
        self.assertFalse([k for k in mirrored if k.startswith("source_stale")])


class TestComment(AlertsCase):
    """Комментарий модели: одинаковый во всех трёх местах, необязательный везде."""

    def test_comment_reaches_telegram_and_feed(self):
        self.seed(rate_before())
        with mock.patch.object(self.alerts.commentary, "annotate",
                               lambda evs, pl, log=None: [e.update(comment="разбор") for e in evs
                                                          if not self.alerts.is_ops(e)]):
            self.alerts.run(rate_after(), dry_run=False, now=NOW)
        self.assertTrue(any("💬 разбор" in t for t in self.sent))
        self.assertTrue(all(e.get("comment") == "разбор" for e in self.alerts.payload_events()))

    def test_event_without_comment_is_a_plain_fact(self):
        # Ключа нет — комментатор возвращает None, и это НЕ отказ доставки.
        self.seed(rate_before())
        self.alerts.run(rate_after(), dry_run=False, now=NOW)
        self.assertTrue(self.sent)
        self.assertFalse(any("💬" in t for t in self.sent))
        self.assertEqual(self.pending_keys(), [])

    def test_dry_run_does_not_call_the_model(self):
        # Прогон «на посмотреть» не должен зависеть от чужого провайдера.
        self.seed(rate_before())
        calls = []
        with mock.patch.object(self.alerts.commentary, "annotate",
                               lambda *a, **k: calls.append(1)):
            self.alerts.run(rate_after(), dry_run=True, now=NOW)
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
        self.assertIn("от 05.08.", ops, "повтор потерял факт с датой данных")

    def test_повтор_рыночного_не_слипается_в_жирный_ком(self):
        self.seed(rate_before())
        self.online = False
        self.alerts.run(rate_after(), dry_run=False, now=NOW)
        self.online = True
        self.alerts.run(rate_after(), dry_run=False, now=NOW + timedelta(hours=1))
        market = "\n".join(self.by_channel.get("alerts") or [])
        self.assertIn("<b>", market)
        # Заголовок закрывается до подробностей: жирным — только он.
        head = market.split("</b>")[0]
        self.assertLess(len(head), 120,
                        "повтор завернул весь текст события в одну жирную строку")

    def test_слитый_поворот_несёт_что_за_этим_стоит_и_в_ленте(self):
        """Аудит 28.10.2026: `causes` не было в EVENT_FIELDS.

        Слитый поворот (merge_regime) объясняет себя блоком «Что за этим стоит». С
        20.11.2026 он живёт только в журнале страницы, и блок обязан доехать туда:
        лента собирается из тех же EVENT_FIELDS, что и очередь повторов.
        """
        self.seed(payload(vol=0, bond=1, cell="bear|calm|stress"))
        self.alerts.run(payload(vol=1, bond=0, cell="bear|stress|ok"),
                        dry_run=False, now=NOW)
        saved = self.state()["feed"][-1]
        self.assertTrue(saved.get("causes"), "запись журнала потеряла подпункты")
        self.assertIn("Долговой рынок вышел из стресса",
                      self.alerts.payload_events()[-1]["text"])


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
        self.assertIn("от 13.08.", ev["fact"])

    def test_без_имени_ряда_текст_прежний(self):
        # Семья может не знать ряда (старое состояние, чужой писатель) — тогда
        # сообщение остаётся про источник целиком, а не про «None».
        ev = self.fire()
        self.assertNotIn("None", ev["title"] + ev["fact"])
        self.assertIn("iss", ev["title"])

    def test_причина_протухания_названа(self):
        """«Не опрашивался» и «отвечает, но отдаёт старое» чинятся в разных местах.

        Первое — конвейер или расписание, второе — сам источник. Одним словом
        «устарел» на обе владельца посылало искать не там: 26.08.2026 у ALGOPACK
        опрос шёл каждый час, а данные отставали на неделю.
        """
        ev = self.fire(series="brent", stale_reason="data")
        self.assertIn("отдаёт старые данные", ev["title"])
        self.assertNotIn("не опрашивался", ev["title"])

    def test_молчащий_источник_описан_иначе(self):
        ev = self.fire(series="ofz_auctions", stale_reason="poll")
        self.assertIn("не опрашивался", ev["title"])
        self.assertNotIn("отдаёт старые", ev["title"])

    def test_ряд_совпал_с_именем_семьи_не_дублируется(self):
        ev = self.fire(series="iss")
        self.assertNotIn("iss (iss)", ev["title"])


class TestSourceRecovery(AlertsCase):
    """Отбивка о том, что источник ожил.

    Её здесь не было, и молчание после тревоги оказывалось двусмысленным: «уже
    починилось» и «до сих пор лежит» выглядят одинаково — никак. Оплачено
    08.09.2026: ISS отвалился на десять минут, следующий прогон в 15:00 прошёл
    начисто, а владелец ещё через семнадцать часов считал источник лежащим и
    написал «раньше никогда так надолго не отваливалось».

    мутация: убрать ветку выздоровления -> канал снова умеет только пугать.
    """

    def test_возврат_в_строй_сообщается(self):
        self.seed(payload(sources={"iss": {"status": "error", "lag_min": 90,
                                           "asof": "2026-08-05"}}))
        evs = self.alerts.run(payload(sources={"iss": {"status": "ok", "asof": ASOF}}),
                              dry_run=False, now=NOW)
        ev = next(e for e in evs if e["kind"] == "source_ok")
        self.assertIn("iss", ev["title"])
        self.assertIn("снова", ev["title"])
        self.assertEqual(ev["severity"], "info")
        self.assertIn("от 11.08.", ev["fact"])

    def test_отбивка_санитарная(self):
        # Тревога уходит в ops-канал — отбивке место там же, иначе она осядет в
        # ленте рынка, которой отказ обвязки ничего не сообщает.
        self.assertIn("source_ok", self.alerts.OPS_KINDS)

    def test_здоровый_источник_молчит(self):
        # Без предшествующей тревоги отбивки быть не должно, иначе каждый прогон
        # рапортовал бы о каждом живом источнике.
        self.seed(payload(sources={"iss": {"status": "ok", "asof": ASOF}}))
        evs = self.alerts.run(payload(sources={"iss": {"status": "ok", "asof": ASOF}}),
                              dry_run=False, now=NOW)
        self.assertFalse([e for e in evs if e["kind"] == "source_ok"])

    def test_после_отбивки_тревога_снова_звучит(self):
        """Отбивка не глушит сторож — это главное её свойство.

        Состояние переписывается на «ok», поэтому следующий отказ обязан сообщить
        о себе. Иначе разрядка превратилась бы в глушилку, а это ровно та беда,
        от которой в стороже панелей завели проверку пар «тревога/выздоровление».
        """
        self.seed(payload(sources={"iss": {"status": "error", "asof": "2026-08-05"}}))
        self.alerts.run(payload(sources={"iss": {"status": "ok", "asof": ASOF}}),
                        dry_run=False, now=NOW)
        later = "2026-08-12"
        evs = self.alerts.run(payload(asof=later,
                                      sources={"iss": {"status": "error", "lag_min": 30,
                                                       "asof": later}}),
                              dry_run=False, now=NOW + timedelta(days=1))
        self.assertTrue([e for e in evs if e["kind"] == "source_stale"])


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

    def due(self, now=NOW, streak=12):
        return self.alerts.run(payload(review_due=True, streak=streak, health="review"),
                               dry_run=False, now=now)

    def test_ниже_нуля_без_порога_молчит(self):
        # ГЛАВНОЕ изменение 02.09.2026: первый месяц ниже нуля — не событие. Прежний
        # health_dead слал приговор на IC −0,08 при интервале, накрывающем ноль втрое.
        # мутация: вернуть тревогу на статус warn -> здесь появится событие.
        self.seed(payload())
        evs = self.alerts.run(payload(health="warn", streak=5), dry_run=False, now=NOW)
        self.assertEqual([e["kind"] for e in evs if e["kind"].startswith("health")], [])

    def kinds(self, events):
        return [e["kind"] for e in events]

    def test_переход_даёт_ровно_одно_событие(self):
        self.seed(payload())
        first = self.due()
        self.assertEqual(self.kinds(first).count("health_review"), 1)
        second = self.due(NOW + timedelta(days=1))
        self.assertEqual(self.kinds(second).count("health_review"), 0)

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
        self.assertFalse([e for e in feed if e.get("kind") == "health_review"])
        self.assertFalse([e for e in self.state().get("feed") or []
                          if e.get("kind") == "health_review"])

    def test_недоставленное_событие_не_теряется(self):
        # ГЛАВНОЕ: снимок писался безусловно, поэтому недоставленное событие
        # исчезало навсегда — в ленту оно не идёт, очередь повторов живёт сутки,
        # а заново правило его не породит, пока держится та же серия.
        self.seed(payload())
        self.online = False
        self.assertEqual(self.kinds(self.due()).count("health_review"), 1)
        self.assertFalse(self.state()["last"].get("health_review_due"),
                         "защёлка не имеет права защёлкнуться без доставки")
        self.online = True
        # Прошли сутки — очередь повторов уже пуста, спасти может только защёлка.
        again = self.due(NOW + timedelta(hours=30))
        self.assertEqual(self.kinds(again).count("health_review"), 1)

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
        self.assertEqual(self.kinds(batch).count("health_review"), 1)
        self.assertEqual([e["outcome"] for e in batch if e["kind"] == "health_review"],
                         [self.telegram.OFF])
        self.assertFalse(self.state()["last"].get("health_review_due"),
                         "защёлка сработала, хотя событие никуда не ушло")
        # Канал настроили — находка обязана дойти.
        os.environ["ERROR_BOT_TOKEN"] = "тест-ops"
        again = self.due(NOW + timedelta(hours=30))
        self.assertEqual(self.kinds(again).count("health_review"), 1)
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
    """Совпавшие переходы машины состояний — одна запись, а не три.

    ОПЛАЧЕНО ПРОГОНОМ 14.08.2026: ячейка — это и есть три её признака вместе,
    поэтому снятие облигационного флага НЕИЗБЕЖНО меняет ячейку, а окно входа —
    частный случай той же смены. Переход «ОФЗ вышли из стресса» рождал три
    уведомления подряд про одно движение.

    Приём взят у 837: внутренние переключения не рассылаются отдельно, а собираются
    объяснением к одному внешнему событию — блоком «Что за этим стоит». С 20.11.2026
    семейство стоит на флагах ворот и живёт только в журнале страницы; слияние
    осталось тем же — одна запись журнала на один поворот.
    """

    def turn(self, before, after):
        """Один поворот рынка -> список событий этого прогона."""
        self.seed(payload(**before))
        return self.alerts.run(payload(**after), dry_run=False, now=NOW)

    def test_три_перехода_становятся_одной_записью(self):
        evs = self.turn(dict(vol=1, bond=1, cell="bear|stress|stress"),
                        dict(vol=1, bond=0, cell="bear|stress|ok"))
        self.assertEqual(len(evs), 1, [e["kind"] for e in evs])
        self.assertEqual(evs[0]["kind"], "buy_window_open")
        self.assertEqual(len(self.alerts.payload_events()), 1, "в журнале больше одной записи")

    def test_заголовком_становится_самое_предметное(self):
        """«Долговой рынок вошёл в стресс» конкретнее, чем «Признаки … сменились».

        Обратный порядок давал заголовки, из которых нельзя понять, ЧТО произошло.
        """
        evs = self.turn(dict(vol=0, bond=0, cell="bull|calm|ok"),
                        dict(vol=0, bond=1, cell="bull|calm|stress"))
        self.assertEqual([e["kind"] for e in evs], ["bond_flag_on"])
        self.assertIn("Признаки рынка для ворот сменились", " ".join(evs[0]["causes"]))

    def test_свёрнутое_не_теряется_а_становится_подпунктом(self):
        evs = self.turn(dict(vol=1, bond=1, cell="bear|stress|stress"),
                        dict(vol=1, bond=0, cell="bear|stress|ok"))
        message = self.alerts.render(evs[0])
        self.assertIn("<b>Что за этим стоит</b>", message)
        self.assertIn("Долговой рынок вышел из стресса", message)
        # Подробность свёрнутого — расстояние RGBI до порога — тоже объяснение.
        self.assertIn("Просадка RGBI −1,2% от максимума", message)

    def test_ключи_свёрнутых_переезжают_в_главное(self):
        """Дедуп ленты работает по ключу: свёрнутые события не должны всплыть
        в журнале отдельными записями."""
        evs = self.turn(dict(vol=1, bond=1, cell="bear|stress|stress"),
                        dict(vol=1, bond=0, cell="bear|stress|ok"))
        merged = evs[0].get("merged") or []
        self.assertIn("bond_off:" + ASOF, merged)
        self.assertTrue(any(k.startswith("cell:") for k in merged), merged)

    def test_подпункт_не_повторяет_строку_выше(self):
        # Свёрнутое событие описывает то же движение, и его «было → стало» сплошь
        # и рядом совпадает с уже напечатанным. Дубль в блоке объяснений —
        # не объяснение. То же со строкой «что это значит для ворот и позиции»:
        # она одна на весь поворот.
        evs = self.turn(dict(vol=1, bond=1, cell="bear|stress|stress"),
                        dict(vol=1, bond=0, cell="bear|stress|ok"))
        move = f"{evs[0]['before']} → {evs[0]['after']}"
        for cause in evs[0]["causes"]:
            self.assertNotIn(move, cause, f"подпункт повторяет строку движения: {cause}")
            self.assertNotIn(evs[0]["meaning"], cause, "строка о воротах напечатана дважды")

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
        # тревогой: иначе слияние понижает важность записи.
        evs = self.turn(dict(vol=0, bond=0, cell="bull|calm|ok"),
                        dict(vol=0, bond=1, cell="bull|calm|stress"))
        self.assertEqual(evs[0]["severity"], "warn")


class TestGateEvents(AlertsCase):
    """События о состоянии рынка стоят на флагах ВОРОТ и живут только в журнале.

    ОПЛАЧЕНО (аудит 20.11.2026, §9.5): за 30 дней в канал рынка ушло 14 сообщений,
    действия требовало одно. Восемь — «Признаки рынка сменились»: индекс восемь раз
    пересёк 200-дневную среднюю в пределах ±1 % при неизменных воротах и позиции, и
    каждое пришло со звуком, разбором модели и значком смены позиции.

    До того (15.09.2026) то же событие уже подводило иначе: сырая вола ушла на 0,3
    п.п. ниже порога, в канал уехало «Режим рынка сменился» с ободряющей
    статистикой, а ворота стояли закрытыми и позиция не менялась.
    """

    STAYS = gate_block(0, 0, 1)        # ворота: падающий рынок, спокойно, ОФЗ в стрессе

    def test_сырой_тренд_у_средней_при_неизменных_воротах_молчит(self):
        """Ровно шум ноября: сырой признак тренда перекидывается, ворота стоят.

        мутация: вернуть правило на states.current -> событие рождается на каждое
        пересечение средней, и тест краснеет.
        """
        self.seed(payload(trend=0, vol=0, bond=1, cell="bear|calm|stress", gate=self.STAYS))
        for day, trend in enumerate((1, 0, 1, 0), start=1):
            evs = self.alerts.run(payload(trend=trend, vol=0, bond=1, gate=self.STAYS,
                                          cell=("bull" if trend else "bear") + "|calm|stress"),
                                  dry_run=False, now=NOW + timedelta(days=day))
            self.assertEqual(evs, [], f"день {day}: сырой признак дал событие")
        self.assertEqual(self.sent, [])
        self.assertEqual(self.alerts.payload_events(), [])

    def test_смена_флага_ворот_идёт_в_журнал_и_никуда_больше(self):
        """мутация: убрать ветку is_journal из dispatch -> запись уходит в телеграм
        и в хаб; убрать фильтр в prepare -> для неё заказывается разбор модели.
        """
        nexus = need(self, "pipeline.lib.nexus", "deliver", "SENT")
        mirrored, asked = [], []
        self.seed(payload(trend=0, vol=0, bond=1, cell="bear|calm|stress"))
        with mock.patch.object(nexus, "deliver",
                               lambda ev: mirrored.append(ev["key"]) or nexus.SENT), \
             mock.patch.object(self.alerts.commentary, "comments",
                               lambda evs, pl, log=None: asked.append(len(evs))):
            evs = self.alerts.run(payload(trend=1, vol=0, bond=1, cell="bull|calm|stress"),
                                  dry_run=False, now=NOW)
        self.assertEqual([e["kind"] for e in evs], ["state_cell_change"])
        self.assertEqual(self.sent, [], "запись о воротах ушла в телеграм")
        self.assertEqual(mirrored, [], "запись о воротах ушла в хаб")
        self.assertEqual(asked, [], "для записи о воротах заказан разбор модели")
        self.assertEqual([e["kind"] for e in self.alerts.payload_events()],
                         ["state_cell_change"], "записи нет в журнале страницы")
        self.assertEqual(self.pending_keys(), [], "журнальная запись легла в очередь повторов")

    def test_запись_говорит_что_стало_с_воротами_и_позицией(self):
        """мутация: не класть meaning -> запись не отвечает на вопрос, ради которого
        её читают: повлияла ли смена флага на позицию.
        """
        flat = position_block("flat", by_score=True)
        self.seed(payload(trend=0, vol=0, bond=1, cell="bear|calm|stress", position=flat))
        evs = self.alerts.run(payload(trend=1, vol=0, bond=1, cell="bull|calm|stress",
                                      position=flat), dry_run=False, now=NOW)
        self.assertEqual(evs[0]["title"], "Признаки рынка для ворот сменились")
        self.assertEqual(evs[0]["meaning"],
                         "Ворота остались открытыми; позиция не изменилась — деньги, "
                         "её держит оценка рынка (−0,31).")
        self.assertIn("падающий рынок", evs[0]["before"])
        self.assertIn("растущий рынок", evs[0]["after"])

    def test_закрытие_ворот_при_позиции_деньги_только_журнал(self):
        """Ворота закрылись, а позиция и так «деньги»: действия не требуется.

        мутация: слать закрытие ворот в канал -> читатель получает тревожное
        сообщение, по которому делать нечего.
        """
        flat = position_block("flat", by_score=True)
        self.seed(payload(trend=0, vol=0, bond=1, cell="bear|calm|stress", position=flat))
        closed = position_block("flat", by_score=True, gate_open=False,
                                held_by={"by": "both", "legs": LEGS})
        evs = self.alerts.run(payload(trend=0, vol=1, bond=1, cell="bear|stress|stress",
                                      position=closed), dry_run=False, now=NOW)
        self.assertEqual(self.sent, [])
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["meaning"],
                         "Ворота закрылись; позиция не изменилась — деньги, её держат "
                         "и ворота, и оценка рынка (−0,31).")

    def test_без_блока_ворот_события_нет(self):
        # Старый payload: ворот в нём нет, и утверждать про них нечего.
        self.seed(payload(trend=1, vol=0, bond=0, cell="bull|calm|ok", gate=None))
        evs = self.alerts.run(payload(trend=0, vol=0, bond=0, cell="bear|calm|ok", gate=None),
                              dry_run=False, now=NOW)
        self.assertEqual(evs, [])

    def test_первый_прогон_новой_версии_молчит(self):
        # Снимок старой версии флагов ворот не хранит — переходом это не считается.
        self.seed(payload(trend=1, vol=0, bond=0, cell="bull|calm|ok", gate=None))
        evs = self.alerts.run(payload(trend=0, vol=0, bond=0, cell="bear|calm|ok"),
                              dry_run=False, now=NOW)
        self.assertEqual(evs, [])


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
        """Рабочее рыночное событие (имя метода историческое): решение ЦБ."""
        return self.alerts.run(rate_after(), dry_run=False, now=now)

    def test_event_reaches_the_hub_under_its_own_key(self):
        self.seed(rate_before())
        events = self.bond_off()
        self.assertEqual(sorted(self.mirrored), sorted(e["key"] for e in events))
        self.assertIn(RATE_KEY, self.mirrored)
        self.assertEqual(self.pending_keys(), [])

    def test_dead_hub_retries_without_a_second_telegram_message(self):
        # мутация: считать событие доставленным по одному телеграму -> упавший хаб
        # теряет событие навсегда, повторить его уже некому.
        self.seed(rate_before())
        self.hub_outcome = self.nexus.FAIL
        self.bond_off()
        self.assertIn(RATE_KEY, self.pending_keys())
        self.hub_outcome = self.nexus.SENT
        self.bond_off(NOW + timedelta(hours=1))
        self.assertEqual(self.pending_keys(), [])
        self.assertEqual(self.mirrored.count(RATE_KEY), 2)
        # Телеграм на повторе отвечает DUP: второго сообщения в канал не уходит.
        self.assertEqual(len([t for t in self.sent if RATE_TITLE in t]), 1)

    def test_unconfigured_hub_never_blocks_delivery(self):
        # мутация: OFF трактовать как FAIL -> на машине без NEXUS_* очередь повторов
        # растёт вечно, а телеграм на каждом прогоне отвечает DUP впустую.
        self.seed(rate_before())
        self.hub_outcome = self.nexus.OFF
        self.bond_off()
        self.assertEqual(self.pending_keys(), [])

    def test_dry_run_touches_neither_channel(self):
        self.seed(rate_before())
        self.alerts.run(rate_after(), dry_run=True, now=NOW)
        self.assertEqual(self.mirrored, [])
        self.assertEqual(self.sent, [])


class HealthReviewWordingCase(AlertsCase):
    """Тревога о здоровье не выносит модели приговор, которого нет в данных.

    01.09.2026 владельцу ушло «модель перестала работать на свежей истории» и
    «знаку оценки доверять нельзя» — на IC −0,08 при интервале, накрывающем ноль
    втрое, и одном месяце ниже нуля. С 02.09.2026 тревога одна (health_review) и
    приходит только на пороге ×12; её текст — про плановую ревалидацию.
    """

    def fire(self, **hl):
        base = {"status": "review", "n": 24, "ic_24m": -0.12,
                "ic_ci95": [-0.53, 0.29], "below_zero_months": 12,
                "below_since": "2025-09-30", "review_months": 12, "review_due": True}
        base.update(hl)
        p = payload()
        p["core"]["health"] = base
        self.seed(payload())
        evs = self.alerts.run(p, dry_run=False, now=NOW)
        return next(e for e in evs if e["kind"] == "health_review")

    def test_заголовок_описывает_порог_а_не_приговор(self):
        ev = self.fire()
        self.assertNotIn("перестала работать", ev["title"])
        self.assertIn("порог", ev["title"])

    def test_интервал_и_счётчик_месяцев_в_факте(self):
        ev = self.fire()
        self.assertIn("интервал", ev["fact"])
        self.assertIn("12 месяцев подряд", ev["fact"])
        # «с сентября», а не «с сентябрь» (аудит 28.10.2026, B5).
        self.assertIn("(с сентября 2025)", ev["fact"])

    def test_смысл_называет_реколибровку_а_не_позицию(self):
        ev = self.fire()
        self.assertNotIn("доверять нельзя", ev["meaning"])
        self.assertIn("реколибровка", ev["meaning"])
        self.assertIn("не сокращение позиции", ev["meaning"])

    def test_без_интервала_факт_остаётся_целым(self):
        ev = self.fire(ic_ci95=None)
        self.assertNotIn("интервал", ev["fact"])
        self.assertRegex(ev["fact"], r"\d")


class TestPositionChange(AlertsCase):
    """Событие слоя решения: смена позиции «акции ↔ деньги» (02.09.2026)."""

    def test_переход_в_деньги(self):
        self.seed(payload(position="long"))
        evs = self.alerts.run(payload(position="flat"), dry_run=False, now=NOW)
        ev = next(e for e in evs if e["kind"] == "position_change")
        # Заголовок называет действие; движение со ставкой — строкой ниже. Прежний
        # заголовок повторял её дословно (аудит 28.10.2026, B5).
        self.assertEqual(ev["title"], "Позиция: переход в деньги")
        self.assertEqual(ev["before"], "акции")
        self.assertEqual(ev["after"], "деньги (ставка 12,9%)")
        self.assertIn("ворота закрылись", ev["detail"].lower())
        # День недели и дата исполнения названы: «на следующем закрытии» заставляло
        # считать по календарю (аудит 20.11.2026, §9.8). 11.08.2026 — вторник.
        self.assertIn("Решение от вт 11.08, исполнять на закрытии ср 12.08.", ev["detail"])
        self.assertEqual(ev["severity"], "warn")

    def test_переход_в_акции(self):
        self.seed(payload(position="flat"))
        evs = self.alerts.run(payload(position="long"), dry_run=False, now=NOW)
        ev = next(e for e in evs if e["kind"] == "position_change")
        self.assertEqual(ev["title"], "Позиция: переход в акции")
        self.assertEqual((ev["before"], ev["after"]), ("деньги (ставка 12,9%)", "акции"))
        self.assertEqual(ev["severity"], "info")

    def test_без_смены_молчит(self):
        self.seed(payload(position="flat"))
        evs = self.alerts.run(payload(position="flat"), dry_run=False, now=NOW)
        self.assertFalse([e for e in evs if e["kind"] == "position_change"])

    def test_снимок_без_позиции_не_переход(self):
        # Первый прогон новой версии поверх старого снимка (без position) — молча.
        self.seed(payload())
        evs = self.alerts.run(payload(position="long"), dry_run=False, now=NOW)
        self.assertFalse([e for e in evs if e["kind"] == "position_change"])
        again = self.alerts.run(payload(position="flat"), dry_run=False,
                                now=NOW + timedelta(days=1))
        self.assertEqual([e["kind"] for e in again if e["kind"] == "position_change"],
                         ["position_change"])

    def test_не_сливается_с_семейством_режима(self):
        # Смена режима и смена позиции — две новости: позиция есть итог ворот и
        # наклона, и её событие не уходит подпунктом в «Что за этим стоит».
        self.seed(payload(position="long", vol=0, bond=1, cell="bear|calm|stress"))
        evs = self.alerts.run(payload(position="flat", vol=1, bond=1,
                                      cell="bear|stress|stress"), dry_run=False, now=NOW)
        kinds = [e["kind"] for e in evs]
        self.assertIn("position_change", kinds)
        self.assertIn("state_cell_change", kinds)
        self.assertNotIn("position_change", str([e.get("merged") for e in evs]))
        # В канал уходит только смена позиции; запись о воротах — в журнал, и она
        # говорит, что позиция сменилась.
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Позиция: переход в деньги", self.sent[0])
        gate = next(e for e in evs if e["kind"] == "state_cell_change")
        self.assertIn("Ворота закрылись; позиция сменилась: акции → деньги.", gate["meaning"])

    def test_уходит_в_канал_рынка_и_в_ленту(self):
        self.seed(payload(position="long"))
        self.alerts.run(payload(position="flat"), dry_run=False, now=NOW)
        self.assertTrue(any("Позиция" in t for t in self.by_channel.get("alerts") or []))
        feed = self.alerts.payload_events()
        self.assertTrue([e for e in feed if e["kind"] == "position_change"])


if __name__ == "__main__":
    unittest.main()
