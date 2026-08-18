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

    def test_ограничитель_стартов_вмещает_плановые_такты(self):
        """systemd считает ВСЕ старты, включая приходящие от таймера, а не одни повторы.

        ОПЛАЧЕНО ПРОДОМ: у витринного такта стояло `StartLimitBurst=2` на окно 900 с —
        верно для прежнего шага 15 минут и неверно для нынешних 5. В окне лежит ТРИ
        плановых такта, третий отбивался с «Start request repeated too quickly».
        За 13.08.2026 так молча пропало 73 такта из 169, включая закрывающий 21:00 UTC
        (00:00 МСК): его каждый день съедали 20:50 и 20:55. Юнит при этом не падает,
        прогона просто нет, а витрина стоит с прошлыми числами и «ok» у всех
        источников — отказ, который сам себя прячет.
        """
        cs = need(self, "ops.check_schedule", "start_limit_problems")
        sched = need(self, "pipeline.lib.schedule", "starts_of_day", "max_starts_in_window")
        self.assertEqual(cs.start_limit_problems(OPS), [])

        # Счёт на настоящем таймере: 169 срабатываний в сутки, по три в четверть часа.
        starts = sched.starts_of_day(calendars("moex-radar-intraday.timer"))
        self.assertEqual(len(starts), 169, "плотность витринного такта изменилась")
        self.assertEqual(sched.max_starts_in_window(starts, 15), 3)

        # И сам детектор: на прежнем значении он обязан кричать.
        burst = int(re.search(r"^StartLimitBurst=(\d+)",
                              unit_text("moex-radar-intraday.service"), re.M).group(1))
        self.assertGreaterEqual(burst, 3, "ограничитель снова душит плановый такт")

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

    def test_реколибровка_не_лезет_в_окно_сборки(self):
        """Сверка может подождать, витринный такт — нет.

        Столкновение не сломало бы ничего (flock разведёт, интрадей честно пропустит
        такт), но обмен «пропущенный такт витрины ради сверки, которая ждёт месяц»
        невыгоден. Первая редакция таймера стояла на 20:40 UTC — внутри окна
        витринных тактов (Mon–Fri 07:00–20:55 каждые пять минут).
        """
        unit = "moex-radar-recalibrate.timer"
        jitter = self._jitter(unit)
        intraday_end = 20 * 60 + 55
        for line in calendars(unit):
            hh, mm, _ss = (int(x) for x in line.split()[-2].split(":"))
            start = hh * 60 + mm
            self.assertGreater(start, intraday_end,
                               f"{line}: попадает в окно витринных тактов")
            for run in DAILY_RUNS_UTC:
                self.assertFalse(run - LOCK_WAIT_MIN <= start + jitter and
                                 start <= run + LOCK_WAIT_MIN,
                                 f"такт {line} упирается в суточный прогон")

    def test_у_реколибровки_есть_юнит_и_таймер(self):
        # Юнит без таймера — молчаливый отказ: файл есть, запускать некому
        # (ровно так пять рядов не обновлялись с установки, ops/check_schedule.py).
        for name in ("moex-radar-recalibrate.service", "moex-radar-recalibrate.timer"):
            self.assertTrue(os.path.exists(os.path.join(OPS, name)), name)
        self.assertIn("moex-radar recalibrate", unit_text("moex-radar-recalibrate.service"))

    def test_установщик_знает_каждый_юнит_репозитория(self):
        """Юнит-файл, которого нет в UNITS установщика, молча не будет установлен.

        Список в ops/install-vps.sh — рукописный, и его никто не сверял: новая
        пара service+timer в репозитории прошла бы CI зелёной, а на VPS её не
        включил бы никто (класс «режим без юнита», уже стоивший пяти рядов,
        которые не обновлялись с установки). Проверяем в обе стороны: и забытый
        юнит, и юнит-призрак, оставшийся в списке после удаления файлов.
        """
        sh = open(os.path.join(OPS, "install-vps.sh"), encoding="utf-8").read()
        m = re.search(r"UNITS=\(([^)]*)\)", sh, re.S)
        self.assertIsNotNone(m, "в install-vps.sh пропал список UNITS")
        installed = set(m.group(1).split())
        on_disk = {f[:-len(".service")] for f in os.listdir(OPS)
                   if f.startswith("moex-radar-") and f.endswith(".service")}
        self.assertEqual(installed, on_disk,
                         f"забытые установщиком: {sorted(on_disk - installed)}; "
                         f"призраки списка: {sorted(installed - on_disk)}")

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
