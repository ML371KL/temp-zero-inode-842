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


class NotifyCase(unittest.TestCase):
    def setUp(self):
        self.rc = need(self, "ops.recalibrate", "notify", "QUARTER_MONTHS")
        self.telegram = need(self, "pipeline.lib.telegram", "deliver", "SENT")
        self.sent = []

        def fake(key, text, silent=False, cooldown_hours=None, channel="alerts"):
            self.sent.append({"key": key, "text": text, "channel": channel})
            return self.telegram.SENT

        patcher = mock.patch.object(self.telegram, "deliver", fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def call(self, verdicts=(), review_due=False, when=FEB, mode="auto"):
        health = {"review_due": review_due, "below_zero_months": 7 if review_due else 0}
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
        self.call(verdicts=["bear|stress|stress: -3.14% против -2.94%"], when=FEB)
        self.call(verdicts=["bear|stress|stress: -3.14% против -2.94%"], when=MAR)
        self.assertEqual(len({m["key"] for m in self.sent}), 1,
                         "та же находка в другом месяце обязана дать ТОТ ЖЕ ключ")

    def test_изменившаяся_находка_говорит_заново(self):
        self.call(verdicts=["bear|stress|stress: -3.14% против -2.94%"], when=FEB)
        self.call(verdicts=["bear|stress|stress: -3.60% против -2.94%"], when=MAR)
        self.assertEqual(len({m["key"] for m in self.sent}), 2)

    def test_порядок_находок_не_меняет_ключ(self):
        self.call(verdicts=["а", "б"], when=FEB)
        self.call(verdicts=["б", "а"], when=MAR)
        self.assertEqual(len({m["key"] for m in self.sent}), 1)

    def test_порог_регламента_это_находка(self):
        self.call(review_due=True, when=FEB)
        self.assertIn("порог §7", self.sent[0]["text"])
        self.assertIn("есть расхождения", self.sent[0]["text"])

    def test_уходит_в_ops_канал(self):
        # Это сообщение про обслуживание модели, а не про рынок: в ленте витрины
        # ему не место (contract §6, OPS_KINDS).
        self.call(verdicts=["что-то"], when=FEB)
        self.assertEqual(self.sent[0]["channel"], "ops")

    def test_режим_never_молчит_всегда(self):
        self.assertEqual(self.call(verdicts=["что-то"], when=JAN, mode="never"), "выключено")
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
