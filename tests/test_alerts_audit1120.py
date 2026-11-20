"""Уведомления после независимого аудита 20.11.2026 (отчёт аудита, раздел 9).

Каждая проверка здесь — находка аудита, а не покрытие ради покрытия:

* смена позиции стиралась из очереди повторов через сутки и больше не рождалась —
  Телеграм лежит дольше суток, и владелец не узнаёт о решении вовсе;
* разворот позиции туда-обратно в пределах одной даты вердикта гасил третье
  сообщение ключом первого;
* перед выходом из акций 06.11 оценка всю неделю стояла ниже порога, и об этом не
  ушло ничего; новый месяц налоговой цены нефти, решивший позицию, прошёл молча;
* фраза «акции держим, только когда ворота открыты и оценка в плюсе» была неправдой,
  дата исполнения не называлась, одно число звалось оценкой «на неделю» и «на месяц»;
* тревога об источнике была одна на семью: монитор, пометивший семью устаревшей,
  заслонял отказ ряда решения в ней; падение расчёта выдавалось за свежий прогон.

Время везде фиксированное (правило набора): NOW — вторник 11.08.2026, 19:05 МСК.
"""

import unittest
from datetime import timedelta
from unittest import mock

from tests import need
from tests.test_alerts import (ASOF, LEGS, NOW, PREV_ASOF, AlertsCase, gate_block, payload,
                               position_block, tile_cb)

FRI = "2026-08-14"       # день решения недели, в которой стоит NOW
MON = "2026-08-17"       # день исполнения решения пятницы


def stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class AuditCase(AlertsCase):
    """Телеграм, у которого каналы падают порознь, и запись флага «без звука»."""

    def setUp(self):
        super().setUp()
        self.down = set()         # имена лежащих каналов: "alerts", "ops"
        self.silent = []          # тексты, ушедшие без звука
        self.logged = []

    def _send(self, text, silent=False, retries=2, channel="alerts"):
        if not self.online or channel in self.down:
            return False, "телеграм лежит"
        self.sent.append(text)
        self.by_channel.setdefault(channel, []).append(text)
        if silent:
            self.silent.append(text)
        return True, None

    def market(self):
        return self.by_channel.get("alerts") or []

    def ops(self):
        return self.by_channel.get("ops") or []

    def run_at(self, pl, hours=0.0, **kw):
        return self.alerts.run(pl, dry_run=False, now=NOW + timedelta(hours=hours),
                               log=self.logged.append, **kw)


def score(state, comp=None, **extra):
    """Блок позиции, которую определяет оценка рынка при открытых воротах."""
    return position_block(state, comp=comp, by_score=True, **extra)


# ============================================================ 1. смена позиции

class TestPositionMustDeliver(AuditCase):
    """Сообщение о смене позиции обязано дойти: по нему действуют на живые деньги."""

    def go_flat(self, hours=0.0):
        return self.run_at(payload(position=score("flat")), hours)

    def test_смена_позиции_переживает_обрыв_дольше_суток(self):
        """Прогон аудита n7_sim_delivery: Телеграм лежит с вечера пятницы до воскресенья.

        мутация: убрать must_deliver из _fresh (вернуть общие 24 часа) -> на 36-м
        часу событие стёрто из очереди, после починки канала не уходит ничего.
        """
        self.seed(payload(position=score("long")))
        self.down = {"alerts", "ops"}
        self.go_flat()
        for hours in (5, 16, 23, 30, 36):
            self.go_flat(hours)
            self.assertEqual(len([k for k in self.pending_keys() if k.startswith("position:")]),
                             1, f"на {hours}-м часу смены позиции в очереди нет")
        self.down = set()
        self.go_flat(37)
        got = [t for t in self.market() if "Позиция: переход в деньги" in t]
        self.assertEqual(len(got), 1, "после починки канала смена позиции не дошла")
        self.assertEqual([k for k in self.pending_keys() if k.startswith("position:")], [])
        # и ровно один раз
        self.go_flat(38)
        self.assertEqual(len([t for t in self.market() if "Позиция: переход" in t]), 1)

    def test_потолок_ожидания_четырнадцать_суток(self):
        # мутация: убрать потолок -> сообщение о решении двухнедельной давности
        # уходит как живое.
        self.seed(payload(position=score("long")))
        self.down = {"alerts", "ops"}
        self.go_flat()
        self.go_flat(24 * 13)
        self.assertEqual(len([k for k in self.pending_keys() if k.startswith("position:")]),
                         1, "до потолка событие обязано ждать")
        self.down = set()
        self.go_flat(24 * 14 + 1)
        self.assertEqual(self.market(), [])

    def test_следующая_смена_вытесняет_недоставленную_и_говорит_об_этом(self):
        """мутация: не вытеснять -> после починки приходят две смены подряд, и
        последней может оказаться не та; не дописывать пометку -> «переход в акции»
        приходит человеку, который не знал, что панель была в деньгах.
        """
        self.seed(payload(position=score("long")))
        self.down = {"alerts", "ops"}
        self.go_flat()
        self.down = set()
        self.run_at(payload(position=score("long")), 2)
        got = [t for t in self.market() if "Позиция: переход" in t]
        self.assertEqual(len(got), 1, got)
        self.assertIn("Позиция: переход в акции", got[0])
        self.assertIn("Сообщение о предыдущей смене (переход в деньги, 19:05 МСК) "
                      "до канала не дошло.", got[0])
        self.assertEqual(self.pending_keys(), [])

    def test_поздняя_доставка_до_закрытия_исполнения(self):
        """Решение вторника исполняется на закрытии среды, 19:00 МСК = 16:00 UTC.

        мутация: не класть exec_day в EVENT_FIELDS -> повтор из очереди теряет
        строку о закрытии; считать статус в момент рождения события -> «ещё
        впереди» уходит после закрытия.
        """
        self.seed(payload(position=score("long")))
        self.down = {"alerts", "ops"}
        self.go_flat()
        self.down = set()
        self.go_flat(20)                      # среда 15:05 МСК — до закрытия
        text = self.market()[-1]
        line = text.split("\n")[1]
        self.assertTrue(line.startswith("⏳ "), text)
        self.assertIn("опоздание 20 ч", line)
        self.assertIn("Закрытие исполнения (ср 12.08) ещё впереди", line)

    def test_поздняя_доставка_после_закрытия_исполнения(self):
        self.seed(payload(position=score("long")))
        self.down = {"alerts", "ops"}
        self.go_flat()
        self.down = set()
        self.go_flat(26)                      # среда 21:05 МСК — закрытие прошло
        line = self.market()[-1].split("\n")[1]
        self.assertIn("опоздание 1 сут 2 ч", line)
        self.assertIn("Закрытие исполнения (ср 12.08) уже прошло", line)

    def test_вовремя_доставленное_без_пометки(self):
        self.seed(payload(position=score("long")))
        self.go_flat()
        self.assertNotIn("⏳", self.market()[-1])

    def test_сорок_мелких_повторов_не_вытесняют_смену_позиции(self):
        # мутация: резать очередь одним срезом [-PENDING_MAX:] -> смена позиции
        # выпадает из очереди под напором свежих мелких событий.
        self.seed(payload(position=score("long")))
        self.down = {"alerts", "ops"}
        self.go_flat()
        state = self.state()
        state["pending"] += [{"key": f"info:{i}", "ts": stamp(NOW + timedelta(minutes=5)),
                              "kind": "cb_reminder", "severity": "info", "text": f"мелочь {i}"}
                             for i in range(self.alerts.PENDING_MAX + 5)]
        self.alerts.save_state(state)
        self.go_flat(0.2)
        keys = self.pending_keys()
        self.assertLessEqual(len(keys), self.alerts.PENDING_MAX)
        self.assertTrue([k for k in keys if k.startswith("position:")],
                        "смена позиции вытеснена из очереди мелкими повторами")

    def test_недоставка_дольше_часа_уходит_в_канал_отказов(self):
        """Канал рынка лежит, канал отказов жив: владелец узнаёт о решении из него.

        мутация: убрать _stuck_events -> о недоставке главного события не знает никто;
        не помнить отправленную тревогу -> она повторяется каждые пять минут.
        """
        self.seed(payload(position=score("long")))
        self.down = {"alerts"}
        self.go_flat()
        self.go_flat(0.5)
        self.assertEqual(self.ops(), [], "раньше часа тревожить рано")
        self.go_flat(1.1)
        self.assertEqual(len(self.ops()), 1, self.ops())
        alarm = self.ops()[0]
        self.assertTrue(alarm.startswith("🔴 <b>Рынок РФ · Смена позиции не доходит"), alarm)
        # Тревога сама несёт суть решения: читатель узнаёт позицию из неё.
        self.assertIn("акции → деньги (ставка 12,9%)", alarm)
        self.assertIn("Исполнять на закрытии ср 12.08 — оно ещё впереди.", alarm)
        self.assertIn("не доставлено 1 ч 6 мин", alarm)
        self.assertIn("Смотреть:", alarm)
        for hours in (1.2, 1.3, 5):
            self.go_flat(hours)
        self.assertEqual(len(self.ops()), 1, "тревога о недоставке повторилась")
        # Канал рынка починили: сообщение доходит, в канал отказов — отбивка.
        self.down = set()
        self.go_flat(6)
        self.assertEqual(len([t for t in self.market() if "Позиция: переход в деньги" in t]), 1)
        self.assertTrue(self.ops()[-1].startswith("🟢 <b>Рынок РФ · Сообщение о смене позиции "
                                                  "доставлено"), self.ops()[-1])
        self.go_flat(7)
        self.assertEqual(len(self.ops()), 2, "отбивка повторилась")

    def test_когда_лежат_оба_канала_остаётся_строка_журнала(self):
        # мутация: не писать в журнал -> при двух лежащих каналах следа нет нигде.
        self.seed(payload(position=score("long")))
        self.down = {"alerts", "ops"}
        self.go_flat()
        self.go_flat(2)
        self.assertTrue([m for m in self.logged if "ГЛАВНОЕ СОБЫТИЕ НЕ ДОСТАВЛЕНО" in m],
                        self.logged)
        self.assertEqual(self.sent, [])

    def test_разворот_туда_обратно_в_одну_дату_доходит_трижды(self):
        """Прогон аудита n11_flipflop: правка данных в выходные дважды переворачивает
        позицию при одной дате вердикта.

        мутация: вернуть ключ «position:<день>:<куда>» -> третья смена совпадает
        ключом с первой, телеграм отвечает «уже отправляли», и последним в канале
        остаётся «переход в деньги» при позиции «акции».
        """
        self.seed(payload(position=score("flat")))
        self.run_at(payload(position=score("long")), 0)
        self.run_at(payload(position=score("flat")), 12)
        self.run_at(payload(position=score("long")), 18)
        got = [t.split("\n")[0] for t in self.market()]
        self.assertEqual(len(got), 3, got)
        self.assertIn("переход в акции", got[-1])
        self.assertEqual(len({e["key"] for e in self.state()["feed"]}), 3)


# ============================================================ 6. формулировки

class TestPositionWording(AuditCase):

    def change(self, block, before="long", hours=0.0):
        self.seed(payload(position=score(before)))
        evs = self.run_at(payload(position=block), hours)
        return next(e for e in evs if e["kind"] == "position_change")

    def test_правило_описано_верно(self):
        """С 15.10 по 05.11.2026 оценка была в минусе каждый день при позиции «акции».

        мутация: вернуть «акции держим, только когда ворота открыты и оценка в
        плюсе» -> сообщение снова обещает то, чего правило не делает.
        """
        ev = self.change(score("flat"))
        self.assertNotIn("оценка в плюсе", ev["meaning"])
        self.assertNotIn("только когда", ev["meaning"])
        self.assertIn("Знак читается в день решения", ev["meaning"])
        self.assertIn("с порогом ±0,2; между порогами держится прежний", ev["meaning"])

    def test_оценка_рынка_без_горизонта(self):
        # мутация: вернуть «на неделю вперёд» / «на месяц вперёд» -> одно число
        # снова названо двумя именами в двух сообщениях.
        ev = self.change(score("flat"))
        flip = self.alerts._core_flip(payload(core=-0.66), {"core_value": 0.66},
                                      {"core_sign_alerted": 1, "core_value_alerted": 0.66},
                                      NOW)[0]
        for text in (ev["text"], flip["text"]):
            self.assertNotIn("вперёд", text)
            self.assertNotIn("на месяц", text)
            self.assertNotIn("на неделю", text)
        self.assertEqual(flip["title"], "Оценка рынка развернулась вниз")

    def test_причина_с_числом_и_день_исполнения(self):
        ev = self.change(score("flat"))
        lines = ev["detail"].split("\n")
        self.assertEqual(lines[0], "Оценка рынка ушла ниже −0,2 в день решения: −0,31.")
        self.assertEqual(lines[1], "Решение от вт 11.08, исполнять на закрытии ср 12.08.")
        self.assertEqual(ev["exec_day"], "2026-08-12")
        self.assertFalse(ev.get("exec_missed"))

    def test_решение_пятницы_исполняется_в_понедельник(self):
        # мутация: считать день исполнения календарным «завтра» -> суббота.
        self.assertEqual(self.alerts.exec_day_of(FRI), MON)
        self.assertIsNone(self.alerts.exec_day_of("мусор"))

    def test_о_решении_узнали_позже(self):
        """Решение датировано пятницей 07.08, а расчёт, в котором оно нашлось, — 11.08.

        мутация: не сравнивать дату решения с днём расчёта -> читатель исполняет
        «на следующем закрытии» то, что надо было исполнить в понедельник.
        """
        ev = self.change(score("flat", since="2026-08-07"))
        self.assertIn("Решение от пт 07.08, исполнять на закрытии пн 10.08.", ev["detail"])
        self.assertIn("Панель узнала о решении позже дня решения — по расчёту за 11.08.",
                      ev["detail"])
        self.assertIn("Закрытие исполнения (пн 10.08) уже прошло.", ev["detail"])
        self.assertTrue(ev["exec_missed"])
        # Досылка такого события не повторяет «уже прошло» второй строкой.
        late = self.alerts.render(ev, NOW + timedelta(hours=3))
        self.assertEqual(late.count("уже прошло"), 1, late)
        self.assertIn("опоздание 3 ч", late)

    def test_что_держит_позицию_стоит_в_факте(self):
        """Разбор к выходу 06.11 назвал причиной стресс облигаций; правда — в факте.

        мутация: убрать held_lines из detail -> о том, что позицию держит оценка
        рынка, а её знак — нефть, читатель узнаёт только если модель не ошибётся.
        """
        ev = self.change(score("flat"))
        detail = ev["detail"]
        self.assertIn("Что держит позицию: оценка рынка — −0,31 при пороге входа +0,2; "
                      "ворота открыты.", detail)
        self.assertIn("Знак оценки держит нефть в рублях: без этого слагаемого она была "
                      "бы +0,34.", detail)
        self.assertIn("Слагаемые оценки (она — их среднее): курс доллара −0,15; "
                      "кривая ОФЗ +0,82; нефть в рублях −1,60.", detail)
        self.assertIn("Что сменит позицию: оценка рынка выше +0,2 в день решения "
                      "(сейчас −0,31).", detail)
        # Имена из таблицы модели наружу не идут.
        for inner in ("бочк", "моментум", "Гэп", "10Y", "usd_mom63"):
            self.assertNotIn(inner, ev["text"])
        self.assertIn(detail.split("\n")[2], self.alerts.render(ev))

    def test_без_held_by_сообщение_собирается(self):
        # Витрина старого вида: слой решения этих полей ещё не кладёт.
        block = score("flat")
        for key in ("held_by", "conditions", "comp_daily"):
            block.pop(key)
        ev = self.change(block)
        self.assertNotIn("Что держит", ev["detail"])
        self.assertIn("исполнять на закрытии ср 12.08", ev["detail"])


# ================================================ 2. предупреждение и отбой

class TestPositionWatch(AuditCase):
    """«В день решения возможна смена позиции» — и отбой, если её не случилось."""

    def daily(self, block, hours=0.0, mode="daily"):
        return self.run_at(payload(position=block, run_mode=mode), hours)

    def kinds(self, events):
        return [e["kind"] for e in events]

    def test_оценка_ниже_порога_до_дня_решения_даёт_предупреждение(self):
        """02–05.11.2026: −0,24, −0,27, −0,31 при пороге −0,2 — и ни одного сообщения.

        мутация: убрать правило -> неделя тишины перед сменой позиции.
        """
        self.seed(payload(position=score("long", comp=0.05)))
        evs = self.daily(score("long", comp=-0.24))
        self.assertEqual(self.kinds(evs), ["position_watch"])
        ev = evs[0]
        self.assertEqual(ev["title"], "В день решения возможен переход в деньги")
        self.assertEqual(ev["severity"], "info")
        lines = ev["detail"].split("\n")
        self.assertEqual(lines[0], "Оценка рынка −0,24 — ниже порога −0,2 (расчёт за вт 11.08).")
        self.assertEqual(lines[1], "День решения — пт 14.08: если оценка останется за "
                                   "порогом, позиция сменится: акции → деньги (ставка 12,9%). "
                                   "Исполнение — на закрытии пн 17.08.")
        self.assertIn("Знак оценки держит нефть в рублях", lines[2])
        # Значок ниже, чем у смены позиции: календарь, а не колокол.
        self.assertTrue(self.market()[-1].startswith("🗓 "), self.market()[-1])

    def test_одно_предупреждение_на_день_решения_и_направление(self):
        # мутация: не помнить ключ -> предупреждение каждый вечер до пятницы.
        self.seed(payload(position=score("long", comp=0.05)))
        self.daily(score("long", comp=-0.24))
        for day, comp in enumerate((-0.27, -0.31), start=1):
            self.assertEqual(self.daily(score("long", comp=comp), 24 * day), [])
        self.assertEqual(len(self.market()), 1)

    def test_внутри_порогов_молчит(self):
        self.seed(payload(position=score("long", comp=0.05)))
        self.assertEqual(self.daily(score("long", comp=-0.19)), [])

    def test_только_суточный_прогон(self):
        # мутация: не смотреть на режим -> интрадей-такт, несущий вчерашний вердикт,
        # рассылает предупреждение по вчерашней оценке.
        self.seed(payload(position=score("long", comp=0.05)))
        for mode in ("intraday", "monthly", "weekly", "manual"):
            self.assertEqual(self.daily(score("long", comp=-0.3), mode=mode), [], mode)

    def test_в_день_решения_предупреждать_поздно(self):
        self.seed(payload(position=score("long", comp=0.05)))
        self.assertEqual(self.daily(score("long", comp=-0.3, is_decision_day=True)), [])

    def test_вход_в_акции_только_при_открытых_воротах_и_знаке_не_за_акции(self):
        base = dict(comp=0.25, comp_state=-1, gate_open=True)
        self.seed(payload(position=score("flat", comp=0.0)))
        cases = (("ворота закрыты", dict(base, gate_open=False), []),
                 ("знак уже за акции", dict(base, comp_state=1), []),
                 ("оценка не выше порога", dict(base, comp=0.2), []),
                 ("условие выполнено", base, ["position_watch"]))
        for name, fields, want in cases:
            with self.subTest(name):
                self.assertEqual(self.kinds(self.daily(score("flat", **fields))), want)
        self.assertIn("В день решения возможен переход в акции", self.market()[-1])
        self.assertIn("выше порога +0,2", self.market()[-1])

    def test_отбой_когда_смены_не_случилось(self):
        """мутация: убрать отбой -> предупреждение остаётся без ответа, и молчание в
        пятницу читается и как «обошлось», и как «сообщение не дошло».
        """
        self.seed(payload(position=score("long", comp=0.05)))
        self.daily(score("long", comp=-0.24))
        evs = self.daily(score("long", comp=-0.18, decision_day=FRI, is_decision_day=True,
                               next_decision="2026-08-21"), 72)
        self.assertEqual(self.kinds(evs), ["position_watch_off"])
        self.assertEqual(evs[0]["detail"],
                         "В день решения, пт 14.08, оценка рынка −0,18 при пороге выхода "
                         "−0,2. Позиция осталась прежней — акции.")
        # Отбой уходит без звука и один раз.
        self.assertEqual(self.silent, [self.market()[-1]])
        self.assertEqual(self.daily(score("long", comp=-0.18, decision_day=FRI,
                                          is_decision_day=True), 73), [])

    def test_до_дня_решения_отбоя_нет(self):
        self.seed(payload(position=score("long", comp=0.05)))
        self.daily(score("long", comp=-0.24))
        self.assertEqual(self.daily(score("long", comp=-0.1), 24), [],
                         "оценка вернулась за порог в среду — отбой только в день решения")

    def test_смена_позиции_сама_отвечает_на_предупреждение(self):
        self.seed(payload(position=score("long", comp=0.05)))
        self.daily(score("long", comp=-0.24))
        evs = self.daily(score("flat", decision_day=FRI, since=FRI, is_decision_day=True), 72)
        self.assertEqual(self.kinds(evs), ["position_change"])
        self.assertNotIn("watch", self.state())

    def test_недошедшее_предупреждение_отбоя_не_получает(self):
        # мутация: слать отбой без проверки доставки -> «день решения прошёл без
        # смены» приходит человеку, которого ни о чём не предупреждали.
        self.seed(payload(position=score("long", comp=0.05)))
        self.down = {"alerts", "ops"}
        self.daily(score("long", comp=-0.24))
        self.down = set()
        evs = self.daily(score("long", comp=-0.18, decision_day=FRI, is_decision_day=True), 72)
        self.assertEqual(evs, [])
        self.assertEqual(self.market(), [])


# =================================================== 3. новый месяц цены нефти

def barrel(month, usd):
    return {"id": "rub_barrel", "status": "ok", "asof": month, "headline": "налоговая бочка",
            "payload": {"tax_month": month, "urals_usd": usd}}


class TestUralsMonth(AuditCase):
    """В ряду налоговой цены нефти появился новый месяц — событие с оценкой до и после."""

    def tick(self, month, usd, hours=0.0, mode="monthly", comp=-0.25, oil=-1.42, state="long"):
        legs = [dict(row, contrib=oil) if row["id"] == "urals_rub_gap" else row for row in LEGS]
        block = score(state, comp=comp, held_by={"by": "open", "legs": legs})
        return self.run_at(payload(position=block, run_mode=mode,
                                   monitors=[tile_cb(), barrel(month, usd)]), hours)

    def test_первый_прогон_запоминает_месяц_молча(self):
        self.seed(payload())
        self.assertEqual(self.tick("2026-06-30", 63.52), [])
        self.assertEqual(self.state()["urals_alerted"], {"month": "2026-06-30", "usd": 63.52})

    def test_новый_месяц_даёт_событие_с_оценкой_до_и_после(self):
        """05.11.2026 октябрьская цена сдвинула оценку на 0,06 и решила позицию — молча.

        мутация: убрать правило -> событие, решившее позицию, сообщения не даёт.
        """
        self.seed(payload())
        self.tick("2026-06-30", 63.52, comp=-0.25, oil=-1.42)
        evs = self.tick("2026-07-31", 59.02, 6, comp=-0.31, oil=-1.60)
        self.assertEqual([e["kind"] for e in evs], ["urals_month"])
        ev = evs[0]
        self.assertEqual(ev["title"], "Вышла налоговая цена нефти за июль")
        self.assertEqual(ev["before"], "$63,52 (июнь)")
        self.assertEqual(ev["after"], "$59,02 за баррель Urals (июль)")
        lines = ev["detail"].split("\n")
        self.assertEqual(lines[0], "Оценка рынка: −0,25 → −0,31; вклад нефти в рублях "
                                   "−1,42 → −1,60 — сдвиг оценки на −0,06.")
        self.assertEqual(lines[1], "Порог выхода из акций −0,2: оценка ниже его на 0,11.")
        self.assertEqual(lines[2], "День решения — пт 14.08.")
        self.assertTrue(self.market()[-1].startswith("🗓 "))

    def test_запас_до_порога_назван_когда_он_есть(self):
        self.seed(payload())
        self.tick("2026-06-30", 63.52, comp=0.05, oil=-0.9)
        ev = self.tick("2026-07-31", 59.02, 6, comp=-0.05, oil=-1.2)[0]
        self.assertIn("Порог выхода из акций −0,2: до него 0,15.", ev["detail"])

    def test_тот_же_месяц_с_поправленной_ценой_не_событие(self):
        # мутация: сравнивать цену, а не месяц -> правка источником прошлого месяца
        # уходит как «вышла цена».
        self.seed(payload())
        self.tick("2026-06-30", 63.52)
        self.assertEqual(self.tick("2026-06-30", 63.60, 6), [])

    def test_месяц_назад_не_событие(self):
        self.seed(payload())
        self.tick("2026-07-31", 59.02)
        self.assertEqual(self.tick("2026-06-30", 63.52, 6), [])
        self.assertEqual(self.state()["urals_alerted"]["month"], "2026-07-31")

    def test_архивный_месяц_не_подаётся_новостью(self):
        """Урок auction_failed 12.08.2026: смена записи в истории — не событие.

        мутация: не проверять возраст -> восстановление стора рассылает «вышла цена
        за апрель» в августе.
        """
        self.seed(payload())
        self.tick("2026-03-31", 60.0)
        self.assertEqual(self.tick("2026-04-30", 61.0, 6), [])
        # Память при этом сдвинулась: следующий настоящий месяц меряется от него.
        self.assertEqual(self.state()["urals_alerted"]["month"], "2026-04-30")

    def test_интрадей_такт_ждёт_полного_расчёта(self):
        """Интрадей несёт вердикт прошлого расчёта: «оценка после» была бы оценкой ДО.

        мутация: не ждать -> событие уходит с прежней оценкой, а память сдвинута, и
        полный прогон с настоящим сдвигом уже молчит.
        """
        self.seed(payload())
        self.tick("2026-06-30", 63.52, comp=-0.25, oil=-1.42)
        self.assertEqual(self.tick("2026-07-31", 59.02, 1, mode="intraday",
                                   comp=-0.25, oil=-1.42), [])
        self.assertEqual(self.state()["urals_alerted"]["month"], "2026-06-30")
        evs = self.tick("2026-07-31", 59.02, 6, mode="daily", comp=-0.31, oil=-1.60)
        self.assertIn("urals_month", [e["kind"] for e in evs])


# ================================================ 7. ряды решения и падение расчёта

def row(series, status="ok", asof=ASOF, **extra):
    return dict({"series": series, "status": status, "asof": asof, "lag_min": 5.0,
                 "data_age_days": 0, "stale_reason": None}, **extra)


def iss(vdo="ok", zcyc="ok", imoex="ok"):
    """Семья биржевых рядов: монитор (доходность ВДО) и два ряда решения."""
    rank = {"ok": 0, "stale": 1, "missing": 2, "error": 3}
    rows = {"rucbhycp_yield": (row("rucbhycp_yield", vdo, "2026-08-10",
                                   stale_reason="data" if vdo == "stale" else None), False),
            "zcyc": (row("zcyc_y10", zcyc), True),
            "imoex": (row("imoex", imoex), True)}
    worst = max(rows.values(), key=lambda r: rank[r[0]["status"]])[0]
    bad = [dict(r, core=core) for r, core in rows.values() if r["status"] != "ok"]
    labels = {"zcyc": "Кривая бескупонной доходности ОФЗ", "imoex": "Индекс МосБиржи"}
    return {"iss": dict(worst, fetched_at="2026-08-11T16:00:00Z", bad_series=bad,
                        core_affected=any(b["core"] for b in bad),
                        core_series={sid: dict(r, label=labels[sid])
                                     for sid, (r, core) in rows.items() if core})}


class TestDecisionSeriesAlarm(AuditCase):
    """Ряд решения тревожит сам — независимо от того, что уже плохо в его семье."""

    def kinds(self, events):
        return [e["kind"] for e in events]

    def test_монитор_не_заслоняет_ряд_решения(self):
        """Так обстояло дело с 19.11.2026: семья помечена устаревшей из-за ВДО.

        мутация: вернуть тревогу «одна на семью» -> отказ кривой ОФЗ в уже
        помеченной семье не даёт ничего.
        """
        self.seed(payload(sources=iss()))
        first = self.run_at(payload(sources=iss(vdo="stale")))
        self.assertEqual(self.kinds(first), ["source_stale"])
        evs = self.run_at(payload(sources=iss(vdo="stale", zcyc="error")), 5)
        self.assertEqual(self.kinds(evs), ["core_series_stale"])
        alarm = self.ops()[-1]
        self.assertTrue(alarm.startswith("🔴 <b>Рынок РФ · Ряд решения zcyc не отвечает</b>"),
                        alarm)
        self.assertIn("«Кривая бескупонной доходности ОФЗ» (ряд zcyc (zcyc_y10), "
                      "источник iss): данные от 11.08.", alarm)
        self.assertIn("От этого ряда зависит решение о позиции", alarm)
        self.assertIn("Смотреть:", alarm)
        self.assertEqual(self.market(), [], "санитарное ушло в канал рынка")

    def test_каждый_ряд_решения_тревожит_отдельно(self):
        self.seed(payload(sources=iss()))
        self.run_at(payload(sources=iss(zcyc="error")))
        evs = self.run_at(payload(sources=iss(zcyc="error", imoex="stale")), 5)
        self.assertEqual([e["key"].split(":")[1] for e in evs], ["imoex"])
        self.assertEqual(self.run_at(payload(sources=iss(zcyc="error", imoex="stale")), 10), [])

    def test_отказ_только_ряда_решения_семейной_тревоги_не_дублирует(self):
        # мутация: считать статус семьи вместе с рядами решения -> об одном отказе
        # приходят два сообщения, а после отбивки ряда семья остаётся «уже плохой».
        self.seed(payload(sources=iss()))
        evs = self.run_at(payload(sources=iss(zcyc="error")))
        self.assertEqual(self.kinds(evs), ["core_series_stale"])
        self.assertEqual(self.state()["last"]["sources"], {"iss": "ok"})
        self.assertEqual(self.state()["last"]["core_series"], {"zcyc": "error", "imoex": "ok"})

    def test_выздоровление_ряда_решения_отбивается(self):
        self.seed(payload(sources=iss()))
        self.run_at(payload(sources=iss(zcyc="error")))
        evs = self.run_at(payload(sources=iss()), 2)
        self.assertEqual(self.kinds(evs), ["core_series_ok"])
        self.assertTrue(self.ops()[-1].startswith("🟢 <b>Рынок РФ · Ряд решения zcyc снова "
                                                  "обновляется</b>"))
        self.assertIn("Сбой длился 2 ч", self.ops()[-1])

    def test_недоставленная_тревога_рождается_заново(self):
        # мутация: убрать защёлку core_series -> снимок запомнил «error», правило
        # считает, что уже сообщало, и тревога потеряна навсегда.
        self.seed(payload(sources=iss()))
        self.down = {"ops"}
        self.run_at(payload(sources=iss(zcyc="error")))
        self.assertEqual(self.state()["last"]["core_series"]["zcyc"], "ok")
        self.down = set()
        self.run_at(payload(sources=iss(zcyc="error")), 30)
        self.assertEqual(len([t for t in self.ops() if "Ряд решения zcyc" in t]), 1)

    def test_короткий_сбой_на_интрадей_такте_выдерживается(self):
        # Та же выдержка, что у семей: три такта — тишина, четвёртый — тревога.
        self.seed(payload(sources=iss()))
        for minutes in (0, 5, 10):
            evs = self.run_at(payload(sources=iss(zcyc="error"), run_mode="intraday"),
                              minutes / 60)
            self.assertEqual(evs, [], f"{minutes} мин")
        evs = self.run_at(payload(sources=iss(zcyc="error"), run_mode="intraday"), 15 / 60)
        self.assertEqual(self.kinds(evs), ["core_series_stale"])
        self.assertIn("Сбой с 19:05 МСК, длится 15 мин.", self.ops()[-1])

    def test_второй_отказ_в_тот_же_день_новая_тревога(self):
        # мутация: ключ по дню вердикта -> повторный отказ гасится дедупом телеграма.
        self.seed(payload(sources=iss()))
        self.run_at(payload(sources=iss(zcyc="error")))
        self.run_at(payload(sources=iss()), 1)
        self.run_at(payload(sources=iss(zcyc="error")), 2)
        self.assertEqual(len([t for t in self.ops() if "Ряд решения zcyc не отвечает" in t]), 2)

    def test_витрина_старого_вида_работает_по_прежнему(self):
        # Без полей bad_series/core_series семья — это её худший ряд, как раньше.
        self.seed(payload(sources={"iss": {"status": "ok"}}))
        evs = self.run_at(payload(sources={"iss": {"status": "error", "asof": ASOF}}))
        self.assertEqual(self.kinds(evs), ["source_stale"])

    def test_какие_ряды_считаются_рядами_решения(self):
        registry = need(self, "pipeline.lib.registry", "SERIES")
        got = sorted(sid for sid, spec in registry.SERIES.items()
                     if self.alerts.is_decision_series(spec))
        # мутация: потерять налоговую цену нефти (она не помечена обязательной) ->
        # слагаемое оценки, решившее позицию 06.11, остаётся без своей тревоги.
        self.assertEqual(got, ["imoex", "key_rate", "mcftr", "rgbi", "urals_tax",
                               "usd_cbr", "zcyc"])


class TestComputeFailed(AuditCase):
    """Упавший расчёт не выдаётся за свежий прогон: тревога в канал отказов."""

    ERR = "KeyError @ 2026-08-11T16:05:00Z"

    def test_тревога_с_защёлкой_до_доставки_и_отбивка(self):
        """мутация: убрать правило -> вчерашний вердикт со свежим временем, юнит
        зелёный, событий нет; убрать защёлку -> недоставленная тревога теряется.
        """
        self.seed(payload(position=score("flat")))
        self.down = {"ops"}
        broken = payload(position=score("flat"), compute_error=self.ERR, run_mode="daily")
        evs = self.run_at(broken, crash="KeyError: 'slope_10_2'")
        self.assertEqual([e["kind"] for e in evs], ["compute_failed"])
        self.assertIsNone(self.state()["last"]["compute_error"], "защёлка не удержала")
        self.down = set()
        # Следующий такт несёт ту же пометку (run.py) — тревога рождается заново.
        self.run_at(dict(broken, run_mode="intraday"), 0.1)
        alarm = self.ops()[-1]
        self.assertTrue(alarm.startswith("🔴 <b>Рынок РФ · Расчёт упал: на странице прежний "
                                         "вердикт</b>"), alarm)
        self.assertIn("KeyError", alarm)
        self.assertIn("Опубликован вердикт от 11.08, позиция — деньги.", alarm)
        self.assertIn("Смотреть: journalctl", alarm)
        # Пока расчёт лежит — одна тревога, а не по одной на такт.
        self.run_at(dict(broken, run_mode="intraday"), 0.2)
        self.assertEqual(len(self.ops()), 1)
        self.assertEqual(self.market(), [])
        self.assertEqual(self.alerts.payload_events(), [], "санитарное попало в журнал")
        # Расчёт прошёл — отбивка.
        evs = self.run_at(payload(position=score("flat"), run_mode="daily"), 5)
        self.assertEqual([e["kind"] for e in evs], ["compute_ok"])
        # Отбивка ушла уже 12.08 по Москве, поэтому начало сбоя названо с датой.
        self.assertIn("Расчёт не проходил с 11.08, 19:05 МСК.", self.ops()[-1])

    def test_интрадей_без_пометки_отбивку_не_рождает(self):
        # Снять пометку умеет только полный расчёт.
        self.seed(payload(compute_error=self.ERR))
        self.assertEqual(self.run_at(payload(run_mode="intraday"), 0.1), [])


# ======================================================= порядок: страница, потом сообщение

class TestPrepareThenDeliver(AuditCase):

    def test_до_deliver_ничего_не_уходит_и_состояние_не_пишется(self):
        """Цикл разрезан, чтобы страница публиковалась ДО отправки сообщения.

        мутация: отправлять в prepare -> ссылка из сообщения о смене позиции снова
        ведёт на страницу с прежней позицией.
        """
        self.seed(payload(position=score("long")))
        before = self.state()
        batch, ctx = self.alerts.prepare(payload(position=score("flat")), now=NOW)
        self.assertEqual([e["kind"] for e in batch], ["position_change"])
        self.assertEqual(self.sent, [])
        self.assertEqual(self.state(), before, "prepare записал состояние")
        # Лента для страницы собирается уже здесь — событие в ней есть.
        self.assertEqual([e["kind"] for e in self.alerts.payload_events(batch)],
                         ["position_change"])
        self.alerts.deliver(batch, ctx)
        self.assertEqual(len(self.market()), 1)
        self.assertEqual(self.state()["last"]["position"], "flat")

    def test_обрыв_между_публикацией_и_отправкой_события_не_теряет(self):
        # prepare без deliver = прогон убит после публикации: следующий прогон
        # находит ту же смену заново и доставляет её один раз.
        self.seed(payload(position=score("long")))
        self.alerts.prepare(payload(position=score("flat")), now=NOW)
        self.run_at(payload(position=score("flat")), 0.1)
        self.assertEqual(len([t for t in self.market() if "Позиция: переход в деньги" in t]), 1)


if __name__ == "__main__":
    unittest.main()
