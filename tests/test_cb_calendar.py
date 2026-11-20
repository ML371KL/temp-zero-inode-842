"""Календарь заседаний ЦБ не кончается 18.12.2026.

Аудит 20.11.2026: в коде был только список 2026 года. С 19.12.2026 тайл «Заседание
ЦБ» ушёл бы в «календарь закончился, нужен новый», а автоподбор консенсуса
(fetch/consensus.py) перестал бы видеть заседания. Даты 2027 года сверены с
cbr.ru/dkp/cal_mp/ 20.11.2026.

Часы заморожены: тайл принимает now параметром, подбор консенсуса — тоже.
"""

import os
import unittest
from datetime import date, datetime, timezone
from tempfile import TemporaryDirectory

from tests import need

UTC = timezone.utc


class TestCalendarConstants(unittest.TestCase):
    def setUp(self):
        self.c = need(self, "pipeline.lib.constants", "CB_MEETINGS", "CB_MEETINGS_2026",
                      "CB_MEETINGS_2027")

    def test_2027_год_как_на_сайте_цб(self):
        # Литералом, с сайта ЦБ: опечатка в одной дате сдвигает «дней до заседания»
        # и ломает точное совпадение даты консенсуса с датой заседания.
        self.assertEqual(self.c.CB_MEETINGS_2027,
                         ["2027-02-12", "2027-03-19", "2027-04-30", "2027-06-11",
                          "2027-07-30", "2027-09-10", "2027-10-22", "2027-12-17"])

    def test_имя_2026_сохранено_и_общий_список_по_порядку(self):
        self.assertEqual(self.c.CB_MEETINGS_2026[-1], "2026-12-18")
        self.assertEqual(self.c.CB_MEETINGS,
                         self.c.CB_MEETINGS_2026 + self.c.CB_MEETINGS_2027)
        self.assertEqual(self.c.CB_MEETINGS, sorted(set(self.c.CB_MEETINGS)))

    def test_все_заседания_в_пятницу(self):
        # ЦБ заседает по пятницам: дата на другой день недели — опечатка.
        for day in self.c.CB_MEETINGS:
            self.assertEqual(date.fromisoformat(day).weekday(), 4, day)


class TestTileAfterDecemberMeeting(unittest.TestCase):
    def setUp(self):
        self.monitors = need(self, "pipeline.compute.monitors", "BUILDERS")
        self.store = need(self, "pipeline.lib.store", "save_series")
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        prev = os.environ.get("STATE_DIR")
        os.environ["STATE_DIR"] = self.tmp.name
        self.addCleanup(lambda: os.environ.pop("STATE_DIR", None) if prev is None
                        else os.environ.__setitem__("STATE_DIR", prev))

    def tile(self, now):
        stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        day = now.strftime("%Y-%m-%d")
        for sid, pts in (("key_rate", {"2026-10-26": 14.0}), ("rusfar3m", {day: 14.06})):
            self.store.save_series(sid, {"id": sid, "points": pts,
                                         "meta": {"status": "ok", "fetched_at": stamp}})
        return dict(self.monitors.BUILDERS)["cb_meeting"](self.store, now)

    def test_19_декабря_тайл_называет_февральское_заседание(self):
        # мутация: читать только CB_MEETINGS_2026 -> «календарь заседаний
        # закончился, нужен новый», next_meeting пуст, статус stale.
        t = self.tile(datetime(2026, 12, 19, 9, 0, tzinfo=UTC))
        p = t["payload"]
        self.assertEqual(p["next_meeting"], "2027-02-12")
        self.assertEqual(p["days_left"], 55)          # 12 дней декабря + 31 + 12
        self.assertIn("До заседания 12.02 — 55 дн.", t["headline"])
        self.assertNotIn("закончился", t["headline"])
        self.assertEqual(t["status"], "ok")
        # Вчерашнее заседание свежее недели — его поля для события о решении целы.
        self.assertEqual(p["last_meeting"], "2026-12-18")
        self.assertEqual(p["days_since_last_meeting"], 1)

    def test_накануне_декабрьского_заседания_ничего_не_изменилось(self):
        t = self.tile(datetime(2026, 12, 17, 9, 0, tzinfo=UTC))
        self.assertEqual(t["payload"]["next_meeting"], "2026-12-18")
        self.assertEqual(t["payload"]["days_left"], 1)
        self.assertNotIn("заканчивается", t["note"])

    def test_на_последнем_известном_заседании_тайл_напоминает_о_календаре(self):
        # Через год история повторится, если о ней никто не вспомнит: когда впереди
        # одно известное заседание, тайл говорит это сам.
        # мутация: убрать напоминание -> календарь снова кончается молча.
        t = self.tile(datetime(2027, 11, 1, 9, 0, tzinfo=UTC))
        self.assertEqual(t["payload"]["next_meeting"], "2027-12-17")
        self.assertEqual(t["payload"]["calendar_left"], 1)
        self.assertIn("После заседания 17.12.2027 календарь заседаний в панели "
                      "заканчивается", t["note"])


class TestConsensusSeesNextYear(unittest.TestCase):
    def setUp(self):
        self.c = need(self, "pipeline.fetch.consensus", "_next_meetings", "LOOKBACK_DAYS")
        self.manual = need(self, "pipeline.fetch.manual", "parse_yaml", "_records")

    def test_автоподбор_видит_заседание_2027_года(self):
        # мутация: читать только CB_MEETINGS_2026 -> в феврале 2027 подбирать нечего.
        self.assertEqual(self.c._next_meetings(date(2027, 2, 1)), ["2027-02-12"])
        self.assertEqual(self.c._next_meetings(date(2026, 12, 19)), [])   # до окна далеко
        self.assertEqual(self.c._next_meetings(date(2026, 12, 10)), ["2026-12-18"])

    def test_формат_файла_консенсуса_принимает_строки_2027_года(self):
        # Сами числа не выдумываем: строка с пустым expected_rate — «заседание
        # известно, консенсуса ещё нет».
        data = self.manual.parse_yaml(
            "updated: 2026-12-19\n"
            "items:\n"
            "  - date: 2026-12-18\n"
            "    expected_rate: 13.5\n"
            "    source: \"пример\"\n"
            "  - date: 2027-02-12\n"
            "    expected_rate:\n"
            "    source:\n"
            "    note: \"заполнить ~09.02.2027\"\n")
        recs = self.manual._records(data)
        self.assertEqual([str(r["date"]) for r in recs], ["2026-12-18", "2027-02-12"])
        self.assertIn(recs[1].get("expected_rate"), (None, ""))


if __name__ == "__main__":
    unittest.main()
