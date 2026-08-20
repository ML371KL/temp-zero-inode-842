"""Консенсус аналитиков: файл человека главнее, робот молчит без согласия.

ЦЕНА ОШИБКИ ЗДЕСЬ ОСОБАЯ. Это число попадает в самое громкое сообщение панели —
«Банк России снизил ставку… это на N базисных пунктов ниже ожиданий». Ошибиться
в нём хуже, чем не иметь его вовсе: в проде до 20.08.2026 лежала заглушка 16,0 на
заседание, где ЦБ дал 14,0, и она разошлась бы с фактом на 200 б.п.

Отсюда правила, которые здесь и закреплены:
  * ручное значение автоматика не трогает НИКОГДА;
  * одиночная находка в ряд не пишется — только согласие двух РАЗНЫХ каналов;
  * чужие проценты из того же текста (реклама вкладов «до 25% годовых», разбор
    отчётности «консенсус-прогноз по выручке») не должны становиться ставкой;
  * рассказ о СОСТОЯВШЕМСЯ решении — не ожидание.

В сеть не ходим: подменяется tg.messages.
"""

import unittest
from unittest import mock

from tests import need


def msg(text, at, channel="cbrstocks"):
    from datetime import datetime, timezone
    return {"id": 1, "text": text, "url": f"https://t.me/{channel}/1",
            "at": datetime.fromisoformat(at + "T10:00:00+00:00").astimezone(timezone.utc)}


SURVEY_TEXT = ("Консенсус-прогноз аналитиков: на заседании 24 июля ЦБ снизит "
               "ключевую ставку до 14,25% годовых")


class ConsensusCase(unittest.TestCase):
    def setUp(self):
        self.c = need(self, "pipeline.fetch.consensus", "survey", "agreed", "rate",
                      "_rate_from", "CHANNELS")

    def serve(self, by_channel):
        """{канал: [сообщения]} -> подменённый транспорт телеграма."""
        def fake(channel, pages=1, timeout=25, query=None):
            return list(by_channel.get(channel) or [])
        patcher = mock.patch.object(self.c.tg, "messages", side_effect=fake)
        patcher.start()
        self.addCleanup(patcher.stop)


class TestTextParsing(ConsensusCase):
    def test_сводка_опроса_разбирается(self):
        self.assertEqual(self.c._rate_from(SURVEY_TEXT), 14.25)

    def test_консенсус_по_отчётности_не_ставка(self):
        # Живой пример из канала: «Консенсус-прогноз аналитиков по предстоящей
        # отчётности Русала» — слово есть, ставки нет.
        text = ("#RUAL #Прогноз Консенсус-прогноз аналитиков по предстоящей "
                "отчетности Русала за 1п2026: выручка вырастет на 12%")
        self.assertIsNone(self.c._rate_from(text))

    def test_реклама_вкладов_не_ставка(self):
        # Наивный разбор ловил «до 25% годовых» из соседнего сюжета — ошибка в
        # 1000 базисных пунктов.
        text = "Аналитики отмечают: банки поднимают вклады до 25% годовых"
        got = self.c._rate_from(text)
        self.assertNotEqual(got, 25.0, "реклама вкладов принята за ключевую ставку")

    def test_состоявшееся_решение_не_ожидание(self):
        text = ("По итогам заседания ЦБ сохранил ключевую ставку на уровне 14% — "
                "как и ждали аналитики в консенсус-прогнозе")
        self.assertIsNone(self.c._rate_from(text))

    def test_текст_без_слова_опрос_игнорируется(self):
        text = "ЦБ снизит ключевую ставку до 14% — мнение нашего управляющего"
        self.assertIsNone(self.c._rate_from(text))

    def test_значение_вне_коридора_ставки_отбрасывается(self):
        text = "Консенсус аналитиков: ставка снизится до 0,5% годовых"
        self.assertIsNone(self.c._rate_from(text))


class TestAgreement(ConsensusCase):
    def cand(self, value, channel, at="2026-07-20"):
        return {"value": value, "channel": channel, "at": at, "url": "u"}

    def test_нужны_два_разных_канала(self):
        one = [self.cand(14.25, "cbrstocks")]
        self.assertIsNone(self.c.agreed(one))
        two = one + [self.cand(14.25, "rbc_news")]
        self.assertEqual(self.c.agreed(two), (14.25, ["cbrstocks", "rbc_news"]))

    def test_два_сообщения_одного_канала_это_один_голос(self):
        same = [self.cand(14.25, "cbrstocks", "2026-07-18"),
                self.cand(14.25, "cbrstocks", "2026-07-20")]
        self.assertIsNone(self.c.agreed(same), "один канал изобразил согласие")

    def test_при_разнобое_побеждает_большинство_каналов(self):
        mixed = [self.cand(14.0, "cbrstocks"), self.cand(14.25, "rbc_news"),
                 self.cand(14.25, "if_market_news"), self.cand(14.25, "prime1")]
        value, chans = self.c.agreed(mixed)
        self.assertEqual(value, 14.25)
        self.assertEqual(len(chans), 3)


class TestWindow(ConsensusCase):
    def test_берётся_только_окно_перед_заседанием(self):
        self.serve({"cbrstocks": [msg(SURVEY_TEXT, "2026-07-20"),      # за 4 дня — да
                                  msg(SURVEY_TEXT, "2026-06-01"),      # месяц назад — нет
                                  msg(SURVEY_TEXT, "2026-07-25")]})    # после — нет
        found = self.c.survey("2026-07-24", channels=("cbrstocks",))
        self.assertEqual([c["at"] for c in found], ["2026-07-20"])

    def test_отказ_канала_не_роняет_ряд(self):
        from pipeline.lib.http import FetchError

        def fake(channel, pages=1, timeout=25, query=None):
            if channel == "rbc_news":
                raise FetchError("канал закрыт")
            return [msg(SURVEY_TEXT, "2026-07-20", channel)]

        with mock.patch.object(self.c.tg, "messages", side_effect=fake):
            found = self.c.survey("2026-07-24", channels=("cbrstocks", "rbc_news"))
        self.assertEqual([c["channel"] for c in found], ["cbrstocks"])


class TestPriority(ConsensusCase):
    """Ручной ввод главнее автоматики — всегда."""

    def setUp(self):
        super().setUp()
        self.manual = {"2026-09-11": 13.5}
        patcher = mock.patch.object(
            self.c.manual_mod, "consensus",
            side_effect=lambda: ("cb_consensus", dict(self.manual),
                                 {"status": "ok", "source": "manual"}))
        patcher.start()
        self.addCleanup(patcher.stop)
        # Заседания считаем от фиксированной даты, а не от часов машины.
        self.now = __import__("datetime").date(2026, 9, 5)

    def test_вписанное_человеком_не_переписывается(self):
        self.serve({ch: [msg(SURVEY_TEXT.replace("14,25", "14,00"), "2026-09-02", ch)]
                    for ch in self.c.CHANNELS})
        _sid, points, _meta = self.c.rate(now=self.now)
        self.assertEqual(points["2026-09-11"], 13.5, "робот переписал человека")

    def test_пустое_заседание_заполняется_согласием(self):
        self.manual = {}
        text = ("Консенсус-прогноз: на сентябрьском заседании ЦБ сохранит "
                "ключевую ставку на уровне 14% годовых")
        self.serve({"cbrstocks": [msg(text, "2026-09-02", "cbrstocks")],
                    "rbc_news": [msg(text, "2026-09-03", "rbc_news")]})
        _sid, points, meta = self.c.rate(now=self.now)
        self.assertEqual(points["2026-09-11"], 14.0)
        self.assertEqual(meta["auto"][0]["channels"], ["cbrstocks", "rbc_news"])
        self.assertIn("по опросам", meta["note"])

    def test_одиночная_находка_идёт_в_подсказку_а_не_в_ряд(self):
        self.manual = {}
        text = ("Консенсус-прогноз: ЦБ снизит ключевую ставку до 13,75% на "
                "сентябрьском заседании")
        self.serve({"cbrstocks": [msg(text, "2026-09-02", "cbrstocks")]})
        _sid, points, meta = self.c.rate(now=self.now)
        self.assertNotIn("2026-09-11", points, "робот подставил своё число без согласия")
        self.assertEqual(len(meta["candidates"]), 1)
        self.assertIn("вписать вручную", meta["note"])


if __name__ == "__main__":
    unittest.main()
