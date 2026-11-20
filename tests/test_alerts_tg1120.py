"""Тексты уведомлений после проверки 20.11.2026 (вечер, после PR #20 и #21).

Каждая проверка — находка проверки текстов на фикстурах и живом снимке, а не
покрытие ради покрытия:

* вход в акции по воротам при дневной оценке другого знака (так было 15.10.2026:
  знак решения «за акции», оценка −0,06) печатал «знак оценки — за акции (оценка
  −0,07)» и тут же «знак оценки держит нефть: без неё было бы +0,30» — два знака;
* при закрытых воротах и знаке «за деньги» факт называл одно условие из двух;
* отбой предупреждения о входе не говорил, что позицию удержали закрытые ворота;
* месяц цены нефти в день решения писал «позицию это само не меняет»;
* запись журнала о флаге облигаций называла порог сырого признака (−3,9%), а не
  ворот (−3,0%);
* санитарные звали в раздел «Источники», которого на странице нет; сутки писали
  часами («187 ч»); пустой ряд решения получал чужую дату; отбивка расчёта называла
  последнее падение вместо первого;
* новая смена позиции писала «прежнее до канала не дошло», когда до телеграма оно
  дошло, а не принял только хаб.

Время фиксированное (правило набора): NOW — вторник 11.08.2026, 19:05 МСК.
"""

import unittest
from datetime import timedelta
from unittest import mock

from tests import need
from tests.test_alerts import ASOF, LEGS, NOW, payload, position_block, tile_cb
from tests.test_alerts_audit1120 import AuditCase, barrel, iss, row, score

# Вход по воротам при отрицательной дневной оценке: знак решения «за акции» с 03.07.
LEGS_1510 = [{"id": "usd_mom63", "contrib": -0.30, "without": 0.05},
             {"id": "slope_10_2", "contrib": 0.90, "without": -0.55},
             {"id": "urals_rub_gap", "contrib": -0.80, "without": 0.30}]


def gate_entry(comp=-0.07):
    return position_block("long", comp=comp, comp_state_since="2026-07-03",
                          held_by={"by": "open", "legs": LEGS_1510, "leg": "slope_10_2"})


class TestHeldLines(AuditCase):

    def change(self, block, before="flat", **kw):
        self.seed(payload(position=before))
        evs = self.run_at(payload(position=block, **kw))
        return next(e for e in evs if e["kind"] == "position_change")

    def test_вход_по_воротам_при_оценке_другого_знака(self):
        """мутация: печатать строку «знак держит» без сверки со знаком решения ->
        «знак — за акции (оценка −0,07). Знак оценки держит нефть: без неё +0,30».
        """
        ev = self.change(gate_entry(), trend=1, vol=0, bond=1)
        self.assertIn("Что держит позицию: ворота открыты, знак оценки рынка — за акции "
                      "с пт 03.07 (сейчас оценка −0,07, порог выхода −0,2).", ev["detail"])
        self.assertNotIn("Знак оценки держит", ev["detail"])

    def test_знаки_совпадают_строка_о_держателе_остаётся(self):
        ev = self.change(gate_entry(comp=0.35), trend=1, vol=0, bond=1)
        self.assertIn("ворота открыты, знак оценки рынка — за акции (оценка +0,35, порог "
                      "выхода −0,2). Знак оценки держит", ev["detail"])
        self.assertNotIn("сейчас оценка", ev["detail"])

    def test_при_закрытых_воротах_и_знаке_за_деньги_нужны_оба_условия(self):
        """мутация: печатать только первое условие -> «ворота откроются…» читается
        как вход в акции без оценки рынка.
        """
        block = position_block("flat", comp=-0.31, comp_state=-1, gate_open=False,
                               held_by={"by": "both", "legs": LEGS},
                               conditions=["ворота откроются, когда снимется ЛЮБОЙ из флагов: "
                                           "ОФЗ: просадка RGBI мельче −3% (сейчас −5,1%)",
                                           "оценка рынка выше +0,2 в день решения (сейчас −0,31)",
                                           "до входа по оценке рынка при прочих равных хватило "
                                           "бы одного из: доллар на 20% дороже"])
        ev = self.change(block, before="long")
        line = next(x for x in ev["detail"].split("\n") if x.startswith("Что сменит"))
        self.assertIn("Нужно и второе: оценка рынка выше +0,2 в день решения "
                      "(сейчас −0,31).", line)
        self.assertNotIn("хватило бы", line)


class TestWatchOffGate(AuditCase):

    def test_отбой_входа_называет_закрытые_ворота(self):
        # мутация: убрать оговорку -> «оценка +0,25 при пороге входа +0,2. Позиция
        # осталась прежней — деньги» без причины читается как сбой правила.
        self.seed(payload(position=score("flat", comp=0.0)))
        self.run_at(payload(position=score("flat", comp=0.25, comp_state=-1), run_mode="daily"))
        evs = self.run_at(payload(position=score("flat", comp=0.25, comp_state=1, gate_open=False,
                                                 decision_day="2026-08-14", is_decision_day=True),
                                  run_mode="daily"), 72)
        self.assertEqual([e["kind"] for e in evs], ["position_watch_off"])
        self.assertIn("Позиция осталась прежней — деньги, её держат закрытые ворота.",
                      evs[0]["detail"])


class TestUralsMonthWording(AuditCase):

    def tick(self, month, usd, hours=0.0, **pos):
        block = score(pos.pop("state", "long"), comp=pos.pop("comp", -0.31),
                      held_by={"by": "open", "legs": LEGS}, **pos)
        return self.run_at(payload(position=block, run_mode="daily",
                                   monitors=[tile_cb(), barrel(month, usd)]), hours)

    def test_в_день_решения_не_пишет_что_позицию_это_не_меняет(self):
        """мутация: вернуть общую фразу -> «позицию это само не меняет: знак читается в
        день решения» рядом с «день решения — сегодняшний расчёт».
        """
        self.seed(payload())
        self.tick("2026-06-30", 63.52)
        ev = self.tick("2026-07-31", 59.02, 6, is_decision_day=True)[0]
        self.assertIn("Сегодня день решения", ev["meaning"])
        self.assertNotIn("само не меняет", ev["meaning"])

    def test_в_обычный_день_фраза_прежняя(self):
        self.seed(payload())
        self.tick("2026-06-30", 63.52)
        ev = self.tick("2026-07-31", 59.02, 6)[0]
        self.assertIn("Позицию это само не меняет", ev["meaning"])

    def test_при_закрытых_воротах_одной_оценки_мало(self):
        self.seed(payload())
        self.tick("2026-06-30", 63.52, state="flat", comp=-0.1, comp_state=-1, gate_open=False)
        ev = self.tick("2026-07-31", 70.0, 6, state="flat", comp=0.05, comp_state=-1,
                       gate_open=False)[0]
        self.assertIn("Порог входа в акции +0,2: до него 0,15; нужны и открытые ворота.",
                      ev["detail"])


class TestGateJournalThresholds(AuditCase):

    def test_флаг_облигаций_назван_порогом_ворот(self):
        """мутация: брать distances[].text как есть -> «флаг снимется при −3,9%» в
        записи о флаге ворот, которые снимают его выше −3,0%.
        """
        def pl(bond):
            out = payload(trend=0, vol=0, bond=bond, position=score("flat"))
            out["states"]["distances"] = [{
                "id": "bond", "value": -4.5, "threshold": -3.9, "off_threshold": -3.0,
                "text": "RGBI −4,5% от 252-дневного максимума; флаг снимется при −3,9%",
                "text_off": "для ворот флаг снимается выше −3,0% (включается ниже −3,9%)"}]
            return out
        self.seed(pl(0))
        evs = self.run_at(pl(1))
        bond = next(e for e in evs if e["kind"] == "bond_flag_on")
        self.assertEqual(bond["detail"], "RGBI −4,5% от 252-дневного максимума; для ворот флаг "
                                         "снимается выше −3,0% (включается ниже −3,9%).")
        self.assertEqual(self.market(), [], "журнальное ушло в канал рынка")


class TestOpsTexts(AuditCase):

    def test_санитарные_не_зовут_в_несуществующий_раздел(self):
        """Раздела «Источники» на странице нет с переделки витрины.

        мутация: вернуть «раздел «Источники» на панели» -> тот, кто чинит, ищет
        на странице то, чего там нет.
        """
        self.seed(payload(sources=iss()))
        self.run_at(payload(sources=iss(vdo="stale", zcyc="error")))
        self.run_at(payload(sources=iss()), 2)
        texts = self.ops()
        self.assertEqual(len(texts), 4, texts)
        for text in texts:
            self.assertNotIn("«Источники»", text)
        core = [t for t in texts if "Ряд решения zcyc" in t]
        self.assertIn("плашка об источнике вверху", core[0])
        self.assertIn("плашка об источнике вверху", core[1])

    def test_долгий_сбой_в_сутках_а_не_в_часах(self):
        # мутация: вернуть hours_minutes -> «Последний удачный опрос 187 ч 7 мин назад».
        sources = iss()
        sources["iss"]["core_series"]["zcyc"].update(status="stale", stale_reason="data",
                                                     lag_min=11227.2)
        self.seed(payload(sources=iss()))
        self.run_at(payload(sources=sources))
        self.assertIn("Последний удачный опрос 7 сут 19 ч назад.", self.ops()[-1])

    def test_пустой_ряд_решения_без_чужой_даты(self):
        # мутация: дата вердикта на месте даты ряда -> «данные от 11.08» рядом с
        # заголовком «в хранилище нет ни одной точки».
        sources = iss()
        sources["iss"]["core_series"]["zcyc"].update(status="missing", asof=None)
        self.seed(payload(sources=iss()))
        self.run_at(payload(sources=sources))
        alarm = self.ops()[-1]
        self.assertIn("пуст: в хранилище нет ни одной точки", alarm)
        self.assertIn("своих точек в хранилище нет.", alarm)
        self.assertNotIn("данные от", alarm)

    def test_отбивка_расчёта_называет_первое_падение(self):
        """Суточный такт теперь и утром: расчёт, лежащий полтора дня, падает пять раз.

        мутация: брать время из последнего compute_error -> «не проходил с 07:30 МСК».
        """
        self.seed(payload(position=score("flat")))
        for hours, stamp in ((0, "2026-08-11T16:05:00Z"), (5, "2026-08-11T21:05:00Z"),
                             (12.5, "2026-08-12T04:35:00Z")):
            self.run_at(payload(position=score("flat"), run_mode="daily",
                                compute_error=f"KeyError @ {stamp}"), hours,
                        crash="KeyError: 'x'")
        self.run_at(payload(position=score("flat"), run_mode="daily"), 24)
        ok = self.ops()[-1]
        self.assertTrue(ok.startswith("🟢 <b>Рынок РФ · Расчёт снова проходит"), ok)
        self.assertIn("Расчёт не проходил с 11.08, 19:05 МСК.", ok)
        self.assertNotIn("compute_down_since", self.state())


class TestSupersedeTruth(AuditCase):

    def test_прежняя_смена_дошла_до_телеграма_хаб_не_принял(self):
        """Доставлено = оба канала: при лежащем хабе смена позиции живёт в очереди, хотя
        в телеграм она ушла.

        мутация: дописывать «не дошло» всем вытесненным -> новое сообщение говорит
        владельцу неправду о прошлом.
        """
        nexus = need(self, "pipeline.lib.nexus", "deliver", "FAIL", "SENT")
        hub = {"outcome": nexus.FAIL}
        with mock.patch.object(nexus, "deliver", lambda ev: hub["outcome"]):
            self.seed(payload(position=score("long")))
            self.run_at(payload(position=score("flat")))
            self.assertTrue([k for k in self.pending_keys() if k.startswith("position:")])
            hub["outcome"] = nexus.SENT
            self.run_at(payload(position=score("long")), 2)
        last = [t for t in self.market() if "Позиция: переход в акции" in t]
        self.assertEqual(len(last), 1)
        self.assertNotIn("до канала не дошло", last[0])

    def test_не_дошедшая_до_телеграма_по_прежнему_названа(self):
        self.seed(payload(position=score("long")))
        self.down = {"alerts", "ops"}
        self.run_at(payload(position=score("flat")))
        self.down = set()
        self.run_at(payload(position=score("long")), 2)
        last = [t for t in self.market() if "Позиция: переход в акции" in t]
        self.assertIn("до канала не дошло", last[0])


class TestCoreFlipJournalOnly(AuditCase):
    """Дневной разворот оценки — запись журнала страницы, а не сообщение (хвост аудита)."""

    def test_разворот_оценки_не_спорит_со_сменой_позиции(self):
        """День решения: оценка +0,15 → −0,31, позиция сменилась.

        мутация: убрать core_flip из JOURNAL_KINDS -> тем же прогоном в канал уходят
        «Оценка рынка развернулась вниз… Позицию это само не меняет» и «Позиция:
        переход в деньги — оценка рынка ушла ниже −0,2».
        """
        nexus = need(self, "pipeline.lib.nexus", "deliver", "SENT")
        mirrored = []
        with mock.patch.object(nexus, "deliver",
                               lambda ev: mirrored.append(ev["kind"]) or nexus.SENT):
            self.seed(payload(core=0.15, position=score("long", comp=0.15)))
            evs = self.run_at(payload(core=-0.31, position=score(
                "flat", comp=-0.31, is_decision_day=True)))
        self.assertEqual(sorted(e["kind"] for e in evs), ["core_flip", "position_change"])
        self.assertEqual(len(self.market()), 1, self.market())
        self.assertIn("Позиция: переход в деньги", self.market()[0])
        self.assertEqual(mirrored, ["position_change"], "разворот ушёл в хаб")
        journal = " | ".join(e["text"] for e in self.alerts.payload_events())
        self.assertIn("Оценка рынка развернулась вниз", journal)

    def test_разбор_развороту_не_заказывается(self):
        commentary = need(self, "pipeline.lib.commentary", "annotate", "comments")
        asked = []
        with mock.patch.object(commentary, "comments",
                               lambda evs, pl, log=None: asked.append([e["kind"] for e in evs])):
            commentary.annotate([{"key": "core_flip:-1:x", "kind": "core_flip",
                                  "title": "Оценка рынка развернулась вниз"}], payload())
        self.assertEqual(asked, [])


if __name__ == "__main__":
    unittest.main()
