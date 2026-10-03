"""Когда реколибровка говорит, а когда молчит.

Расхождение живёт МЕСЯЦАМИ: пока константы не пересчитаны целиком, строка
«−3,14% против −2,94%» верна и в сентябре, и в октябре. Первая редакция ключа
была по дате, и та же находка приходила бы каждый месяц заново — то самое
«состояние вместо перехода», против которого построен весь остальной алертинг
панели (pipeline/alerts.py). Здесь закреплено обратное правило.
"""

import unittest
from datetime import datetime, timezone
from unittest import mock

from tests import need

JAN = datetime(2027, 1, 5, 21, 30, tzinfo=timezone.utc)   # квартальный месяц
FEB = datetime(2027, 2, 5, 21, 30, tzinfo=timezone.utc)   # обычный
MAR = datetime(2027, 3, 5, 21, 30, tzinfo=timezone.utc)   # обычный
APR = datetime(2027, 4, 5, 21, 30, tzinfo=timezone.utc)   # квартальный


class CellDriftCase(unittest.TestCase):
    """Порог дрейфа обязан быть относительным, иначе он кричит вечно.

    Первая редакция ставила абсолютные 0,15 п.п. на величину, чья собственная
    ошибка среднего 0,6–2,9 п.п. У токсичной ячейки «расхождение» 0,20 п.п. — это
    0,07 стандартной ошибки, то есть числа −2,94 и −3,14 статистически одно и то же.
    Такой детектор гарантированно срабатывает на шуме и приучает себя не читать.
    """

    def setUp(self):
        self.rc = need(self, "ops.recalibrate", "cell_flagged", "DRIFT_SE_RATIO",
                       "TONE_CRIT_PCT")

    def test_боевой_случай_токсичной_ячейки_молчит(self):
        # n=24, ошибка среднего 2,9 п.п. — реальные числа стора на 12.08.2026.
        flagged, _ = self.rc.cell_flagged(-3.14, -2.94, 2.90)
        self.assertFalse(flagged)

    def test_настоящий_сдвиг_ловится(self):
        flagged, why = self.rc.cell_flagged(-5.00, -2.94, 2.90)
        self.assertTrue(flagged)
        self.assertIn("ст.ош.", why)

    def test_точная_ячейка_имеет_узкий_порог(self):
        # n=110, ошибка 0,6 п.п.: тот же абсолютный сдвиг здесь уже новость.
        self.assertFalse(self.rc.cell_flagged(0.93, 0.93, 0.60)[0])
        self.assertTrue(self.rc.cell_flagged(1.60, 0.93, 0.60)[0])

    def test_смена_знака_это_новость_при_любой_выборке(self):
        flagged, why = self.rc.cell_flagged(0.10, -0.10, 99.0)
        self.assertTrue(flagged, "знак меняет вывод, а не четвёртый знак")
        self.assertIn("знак", why)

    def test_переход_через_порог_тона_это_новость(self):
        # По −1,5% фронт красит ячейку в критический тон (web/app.js).
        flagged, why = self.rc.cell_flagged(-1.60, -1.40, 99.0)
        self.assertTrue(flagged)
        self.assertIn("тон", why)

    def test_одно_наблюдение_не_дрейф(self):
        self.assertFalse(self.rc.cell_flagged(-3.0, -2.9, 0.0)[0])

    def test_нет_эталона_нет_претензии(self):
        self.assertFalse(self.rc.cell_flagged(-3.0, None, 2.9)[0])


class HealthSectionCase(unittest.TestCase):
    """Раздел здоровья не имеет права упасть на штатном значении.

    `ic_24m` бывает None по построению — «мало завершённых месяцев с данными»
    (pipeline/compute/health.py). Формат `+.3f` на None — это TypeError, то есть
    отчёт не создаётся, уведомление не уходит ВОВСЕ, а на VPS падение таймера
    беззвучно. Проверка, которая молчит при поломке, неотличима от проверки,
    которая молчит потому, что всё хорошо.
    """

    def setUp(self):
        self.rc = need(self, "ops.recalibrate", "section_health")

    def render(self, health):
        out = []
        self.rc.section_health(health, out)
        return "\n".join(out)

    def test_ic_none_не_роняет_отчёт(self):
        text = self.render({"ic_24m": None, "n": 3, "status": "warn",
                            "below_zero_months": 0, "below_since": None,
                            "review_months": 6, "review_due": False})
        self.assertIn("не считается", text)
        self.assertNotIn("None", text)

    def test_обычное_значение_печатается_числом(self):
        text = self.render({"ic_24m": -0.045, "n": 24, "status": "warn",
                            "below_zero_months": 2, "below_since": "2026-06-30",
                            "review_months": 6, "review_due": False})
        self.assertIn("−0.045".replace("−", "-"), text)
        self.assertIn("24 мес", text)


class InvariantCase(unittest.TestCase):
    """Опора инварианта после правки курса 03.10.2026.

    До правки прод совпадал с композитом исследования побитово, и любое расхождение
    значило «переписали ряд». После правки расхождение с исследованием НАМЕРЕННОЕ:
    сравнивать с ним дальше — это ежемесячная ложная тревога, а молча принять — это
    инвариант, который ничего не ловит. Поэтому опора — композит самого прода,
    замороженный после дотяжки курса, а до заморозки отчёт честно говорит, что
    инвариант не проверяется.
    """

    LABELS = ["2026-01-30", "2026-02-27", "2026-03-31", "2026-04-30", "2026-05-29"]
    COMP = [0.1 + 0.2, -0.2, 1 / 3, 0.4, 0.5]

    def setUp(self):
        self.rc = need(self, "ops.recalibrate", "section_invariant", "freeze_reference")
        from pathlib import Path
        from tempfile import TemporaryDirectory
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        # Композит исследования — другой: так выглядит прод после правки курса.
        self.research = self.dir / "walkforward_results.csv"
        self.research.write_text(
            ",M1_fixed,fwd\n" + "".join(f"{d[:7]}-28,{v + 0.05},0.0\n"
                                        for d, v in zip(self.LABELS, self.COMP)),
            encoding="utf-8")
        self.frozen = self.dir / "composite_reference.csv"

    def freeze(self, usd_first="1997-01-01"):
        return self.rc.freeze_reference(self.LABELS, self.COMP, self.frozen, usd_first,
                                        "1997-01-01", 31)

    def check(self, comp):
        out = []
        status = self.rc.section_invariant(self.LABELS, comp, out, frozen_path=self.frozen,
                                           research_path=self.research)
        return status, "\n".join(out)

    def test_без_заморозки_это_не_тревога_и_не_зелёный_свет(self):
        status, text = self.check(self.COMP)
        # мутация: вернуть сверку с исследованием как единственную -> "drift" каждый месяц
        self.assertEqual(status, "refreeze")
        self.assertIn("НЕ ПРОВЕРЯЕТСЯ", text)

    def test_замороженная_опора_совпадает_побитово(self):
        self.freeze()
        status, text = self.check(list(self.COMP))
        self.assertEqual(status, "ok")
        self.assertIn("Справка", text)  # расхождение с исследованием — справкой

    def test_переписанный_ряд_после_заморозки_ловится(self):
        self.freeze()
        comp = list(self.COMP)
        comp[1] += 1e-6
        self.assertEqual(self.check(comp)[0], "drift")

    def test_два_последних_месяца_не_замораживаются(self):
        # Открытый месяц меняется по построению, хвост закрытого правят задним числом.
        self.assertEqual(self.freeze(), len(self.LABELS) - 2)
        comp = list(self.COMP)
        comp[-1] += 0.3
        comp[-2] += 0.3
        self.assertEqual(self.check(comp)[0], "ok")

    def test_заморозка_до_дотяжки_курса_отклоняется(self):
        # До дотяжки композит до 2013 года ещё на склейке с RTSI и сменится на первом
        # прогоне фетчера — замороженное число дало бы ложную тревогу.
        with self.assertRaises(SystemExit):
            self.freeze(usd_first="2013-01-10")
        self.assertFalse(self.frozen.exists())


class RuleSectionCase(unittest.TestCase):
    """Раздел 4 меряет ДЕЙСТВУЮЩЕЕ правило и в той мере, в какой оно решает.

    До 03.10.2026 здесь стояли месячные «слои» по цене индекса с нулём во флэте —
    другое правило и другая мера (аудит 03.10.2026, §2.6). Отчёт, по которому решают
    о реколибровке, оценивал модель, которой в проде уже не было.
    """

    def setUp(self):
        self.rc = need(self, "ops.recalibrate", "section_rule")
        from datetime import date, timedelta
        days, cur = [], date(2024, 9, 2)
        while cur <= date(2026, 10, 2):
            if cur.weekday() < 5:
                days.append(cur.isoformat())
            cur += timedelta(days=1)
        n = len(days)
        # Полная доходность растёт, цена стоит: мера обязана быть первой.
        self.panel = {"dates": days, "cols": {"imoex": [3000.0] * n,
                                              "mcftr": [1000.0 * 1.0003 ** i for i in range(n)],
                                              "deposit": [15.0] * n}}
        self.daily = {"pos": [1] * n, "pos_prev_rule": [i % 2 for i in range(n)]}

    def test_правило_полная_доходность_и_вклады(self):
        out = []
        rows = self.rc.section_rule(self.panel, self.daily, out)
        text = "\n".join(out)
        self.assertTrue(rows)
        for needle in ("P2, действующее", "P0, прежнее", "удержание MCFTR",
                       "с заморозки 2026-09-02", "MCFTR", "вклады топ-10"):
            self.assertIn(needle, text)
        cells = [c.strip() for c in
                 [ln for ln in out if ln.startswith("| 12 месяцев | удержание MCFTR")][0]
                 .split("|")]
        # мутация: мерить по cols["imoex"] -> рынок за год 0%
        self.assertGreater(float(cells[3].rstrip("%")), 5.0)
        self.assertEqual(cells[5], "15.0%")     # вклады во флэте, а не ноль
        # Окно «с 2010» на истории с 2024 года — годовой темп по самим данным.
        since = [c.strip() for c in
                 [ln for ln in out if ln.startswith("| с 2010 | удержание MCFTR")][0].split("|")]
        self.assertGreater(float(since[4].rstrip("%")), 5.0)

    def test_нет_индекса_полной_доходности_это_несостоявшаяся_проверка(self):
        panel = {"dates": self.panel["dates"], "cols": {"imoex": self.panel["cols"]["imoex"]}}
        out = []
        self.assertIsNone(self.rc.section_rule(panel, self.daily, out))
        self.assertIn("не состоялась", "\n".join(out))


class NotifyCase(unittest.TestCase):
    def setUp(self):
        self.rc = need(self, "ops.recalibrate", "notify", "QUARTER_MONTHS")
        self.telegram = need(self, "pipeline.lib.telegram", "deliver", "SENT")
        # Свой STATE_DIR: notify помнит последний доложенный состав в файле, и без
        # изоляции тесты травили бы друг друга (и рабочий .state репозитория).
        import os
        from tempfile import TemporaryDirectory
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        prev = os.environ.get("STATE_DIR")
        os.environ["STATE_DIR"] = self.tmp.name
        self.addCleanup(lambda: os.environ.__setitem__("STATE_DIR", prev)
                        if prev is not None else os.environ.pop("STATE_DIR", None))
        self.sent = []

        def fake(key, text, silent=False, cooldown_hours=None, channel="alerts"):
            self.sent.append({"key": key, "text": text, "channel": channel})
            return self.telegram.SENT

        patcher = mock.patch.object(self.telegram, "deliver", fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def call(self, verdicts=(), review_due=False, when=FEB, mode="auto", streak=7):
        """verdicts — пары (имя находки, текст): ключ строится по ИМЕНАМ."""
        health = {"review_due": review_due, "below_zero_months": streak if review_due else 0}
        return self.rc.notify(list(verdicts), health, mode, "/tmp/r.md", now=when)

    def test_молчит_когда_нечего_сказать(self):
        # мутация: слать всегда -> двенадцать «всё хорошо» в год, и к весне их
        # перестают открывать вместе с настоящей находкой.
        self.assertIn("молчу", self.call(when=FEB))
        self.assertEqual(self.sent, [])

    def test_квартальное_подтверждение_приходит_без_находок(self):
        self.assertIn("telegram", self.call(when=JAN))
        self.assertIn("расхождений нет", self.sent[0]["text"])

    def test_подтверждение_одно_на_квартал(self):
        self.call(when=JAN)
        first = self.sent[0]["key"]
        self.sent.clear()
        self.call(when=APR)
        self.assertNotEqual(self.sent[0]["key"], first, "новый квартал — новое сердцебиение")

    def test_одна_и_та_же_находка_не_повторяется(self):
        # Ключ по СОДЕРЖАНИЮ: телеграм дедупит по нему и второй раз промолчит.
        self.call(verdicts=[("cell:bear|stress|stress", "-3.14% против -2.94%")], when=FEB)
        self.call(verdicts=[("cell:bear|stress|stress", "-3.14% против -2.94%")], when=MAR)
        self.assertEqual(len({m["key"] for m in self.sent}), 1,
                         "та же находка в другом месяце обязана дать ТОТ ЖЕ ключ")

    def test_растущий_счётчик_не_плодит_сообщения(self):
        # У порога §7 в тексте счётчик месяцев, он растёт ПО ПОСТРОЕНИЮ. Ключ по
        # тексту слал бы сообщение каждый месяц до самой реколибровки.
        self.call(review_due=True, streak=7, when=FEB)
        self.call(review_due=True, streak=8, when=MAR)
        self.assertEqual(len({m["key"] for m in self.sent}), 1)

    def test_дрейф_чисел_в_тексте_не_плодит_сообщения(self):
        # Числа ячейки уточняются каждый месяц; проблема при этом та же самая.
        self.call(verdicts=[("cell:x", "-3.14% против -2.94%")], when=FEB)
        self.call(verdicts=[("cell:x", "-3.31% против -2.94%")], when=MAR)
        self.assertEqual(len({m["key"] for m in self.sent}), 1)

    def test_изменившаяся_находка_говорит_заново(self):
        self.call(verdicts=[("cell:bear|stress|stress", "-3.14% против -2.94%")], when=FEB)
        self.call(verdicts=[("cell:bull|calm|ok", "другая ячейка")], when=MAR)
        self.assertEqual(len({m["key"] for m in self.sent}), 2)

    def test_порядок_находок_не_меняет_ключ(self):
        self.call(verdicts=[("а", "текст а"), ("б", "текст б")], when=FEB)
        self.call(verdicts=[("б", "текст б"), ("а", "текст а")], when=MAR)
        self.assertEqual(len({m["key"] for m in self.sent}), 1)

    def test_порог_регламента_это_находка(self):
        self.call(review_due=True, when=FEB)
        self.assertIn("порог §7", self.sent[0]["text"])
        self.assertIn("есть расхождения", self.sent[0]["text"])

    def test_уходит_в_ops_канал(self):
        # Это сообщение про обслуживание модели, а не про рынок: в ленте витрины
        # ему не место (contract §6, OPS_KINDS).
        self.call(verdicts=[("что-то", "текст")], when=FEB)
        self.assertEqual(self.sent[0]["channel"], "ops")

    def test_режим_never_молчит_всегда(self):
        self.assertEqual(self.call(verdicts=[("что-то", "текст")], when=JAN, mode="never"), "выключено")
        self.assertEqual(self.sent, [])

    def test_исчезновение_находок_сообщается(self):
        # Обещание докстринга: «появилась ИЛИ УШЛА проблема — сообщение». Раньше
        # пустой набор в неквартальный месяц молчал, и владелец, получивший «есть
        # расхождения», оставался с этим знанием навсегда.
        self.call(verdicts=[("cell:x", "разошлось")], when=FEB)
        self.call(when=MAR)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("сняты", self.sent[1]["text"])

    def test_вернувшаяся_находка_говорит_заново(self):
        # Память — последний ДОЛОЖЕННЫЙ состав, а не маркер на 400 суток: состав
        # «разошлось -> чисто -> снова разошлось» обязан дать три сообщения.
        self.call(verdicts=[("cell:x", "разошлось")], when=FEB)
        self.call(when=MAR)
        self.call(verdicts=[("cell:x", "разошлось")], when=APR)
        self.assertEqual(len(self.sent), 3)
        self.assertIn("есть расхождения", self.sent[2]["text"])


if __name__ == "__main__":
    unittest.main()
