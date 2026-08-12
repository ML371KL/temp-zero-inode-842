"""Расписание — часть контракта панели, а не настройка машины.

`ops/check_schedule.py` уже ловит «режим есть, а юнита нет». Здесь закрепляется то,
что он проверить не может: КОГДА именно тикают таймеры. Каждая проверка — оплаченная
ошибка:

  * такт витрины: шаг 5 минут (был 15, и это была вся задержка панели по индексам,
    которые биржа отдаёт без лага — docs/LATENCY.md §3.1). Список минут записан
    перечислением намеренно: `0/5` разные версии systemd читают по-разному;
  * месячный опрос: три такта в сутки. Один полуденный опаздывал почти на сутки на
    каждом вечернем релизе — замеры живых публикаций 12.08.2026: НГД 14:04, ОРФР
    15:20, ФНБ 18:45, бюджет 19:15 МСК;
  * суффикс UTC в КАЖДОЙ строке OnCalendar: без него спецификация читается в поясе
    машины, и главный прогон дня уезжает на три часа при первой же смене tz;
  * месячные такты не должны попадать в окно суточного прогона: тот держит общий
    замок стора, а месячный ждёт его лишь 240 с и иначе краснеет впустую.
"""

import os
import re
import unittest

from tests import ROOT, need

OPS = os.path.join(str(ROOT), "ops")
DAILY_RUNS_UTC = (16 * 60 + 5, 20 * 60 + 55)   # 19:05 и 23:55 МСК
LOCK_WAIT_MIN = 4                              # flock -w 240 в ops/moex-radar.sh


def unit_text(name):
    with open(os.path.join(OPS, name), encoding="utf-8") as fh:
        return fh.read()


def calendars(name):
    return re.findall(r"^OnCalendar=(.+?)\s*$", unit_text(name), re.M)


class TimerCase(unittest.TestCase):
    def test_каждая_строка_календаря_с_суффиксом_utc(self):
        for unit in sorted(f for f in os.listdir(OPS) if f.endswith(".timer")):
            for line in calendars(unit):
                self.assertTrue(line.endswith("UTC"), f"{unit}: «{line}» без пояса")

    def test_интрадей_тикает_каждые_пять_минут(self):
        lines = calendars("moex-radar-intraday.timer")
        main = next(l for l in lines if ".." in l.split()[-2])
        minutes = main.split()[-2].split(":")[1]
        self.assertEqual([int(x) for x in minutes.split(",")], list(range(0, 60, 5)),
                         "шаг витринного такта перестал быть пятиминутным")
        self.assertIn("07..20", main, "окно такта должно покрывать сессию 10:00–23:xx МСК")
        # Закрывающий такт в полночь по Москве — отдельной строкой.
        self.assertTrue(any(l.startswith("Mon..Fri *-*-* 21:00:00") for l in lines))

    def test_витринный_такт_не_доигрывается_после_простоя(self):
        self.assertIn("Persistent=false", unit_text("moex-radar-intraday.timer"))

    def test_месячный_опрос_трижды_в_сутки(self):
        lines = calendars("moex-radar-monthly.timer")
        self.assertEqual(len(lines), 3, "вечерние релизы ждут отдельного такта")
        hours = sorted(int(l.split()[-2].split(":")[0]) for l in lines)
        self.assertEqual(hours, [9, 15, 19])   # 12:00, 18:00 и 22:30 МСК

    def test_месячные_такты_не_лезут_в_окно_суточного(self):
        jitter = self._jitter("moex-radar-monthly.timer")
        for line in calendars("moex-radar-monthly.timer"):
            hh, mm, _ss = (int(x) for x in line.split()[-2].split(":"))
            start = hh * 60 + mm
            for run in DAILY_RUNS_UTC:
                self.assertFalse(run - LOCK_WAIT_MIN <= start + jitter and
                                 start <= run + LOCK_WAIT_MIN,
                                 f"такт {line} упирается в суточный прогон {run // 60}:{run % 60:02d} UTC")

    def _jitter(self, unit):
        m = re.search(r"^RandomizedDelaySec=(\d+)", unit_text(unit), re.M)
        return int(m.group(1)) // 60 if m else 0


class IntradaySeriesCase(unittest.TestCase):
    """Что именно успевает витринный такт — тоже часть обещания о скорости."""

    def setUp(self):
        self.registry = need(self, "pipeline.lib.registry", "MODES", "SERIES")

    def test_ставка_и_polymarket_обновляются_внутри_дня(self):
        intraday = self.registry.MODES["intraday"]
        # Ставка: решение публикуется в 13:30 МСК, суточный прогон приходит в 19:05.
        # Без этой строки алерт «сюрприз против консенсуса» ждал 5,5 часа.
        self.assertIn("key_rate", intraday)
        # Polymarket отдаёт вероятность непрерывно, а опрашивался раз в сутки.
        self.assertIn("polymarket_ceasefire", intraday)

    def test_интрадей_не_тянет_тяжёлое(self):
        # Ширина рынка (45 историй) и КБД (запрос на день) в пятиминутный такт не
        # помещаются и там не нужны: обе величины определены на закрытии.
        for heavy in ("breadth", "zcyc", "futoi_mx"):
            self.assertNotIn(heavy, self.registry.MODES["intraday"])


if __name__ == "__main__":
    unittest.main()
