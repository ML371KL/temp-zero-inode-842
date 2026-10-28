"""Единый стандарт сообщений Telegram (29.10.2026) на всех видах событий 842.

Стандарт общий для всех панелей владельца (TELEGRAM-STYLE-2026-10-29): пометка
панели первым словом заголовка, одна эмодзи по словарю, подвал «Открыть панель ·
28.10, 14:05 МСК», русские числа (запятая, «−», разряды с 10 000), год текущий не
пишется, ни ISO, ни сырых float. Проверки идут по ВСЕМ видам событий, которые
умеют правила (TestTexts.all_kinds), — новый вид без формы стандарта упадёт здесь.
"""

import re
import unittest
from datetime import timedelta

from tests import need
from tests import test_alerts as ta

EMOJI = ("🔔", "📈", "📉", "⚠️", "🗓", "ℹ️", "🔴", "🟡", "🟢")
HEAD = re.compile(r"^(?:%s) <b>Рынок РФ · [А-ЯЁA-Z0-9]" % "|".join(map(re.escape, EMOJI)))
FOOT = re.compile(r'^<a href="https://tzi-842\.pages\.dev">Открыть панель</a> · '
                  r"\d{2}\.\d{2}, \d{2}:\d{2} МСК$")
DATES = re.compile(r"\b\d{2}\.\d{2}(?:\.\d{4})?\b")


class StandardCase(unittest.TestCase):
    def setUp(self):
        self.case = ta.TestTexts("test_every_event_carries_a_number")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.alerts = need(self, "pipeline.alerts", "render", "is_ops")
        self.nexus = need(self, "pipeline.lib.nexus", "compose")
        self.events = self.case.all_kinds()

    def rendered(self):
        for ev in self.events:
            yield ev, self.alerts.render(ev, now=ta.NOW)


class TestFrame(StandardCase):

    def test_пометка_панели_и_одна_эмодзи(self):
        for ev, text in self.rendered():
            with self.subTest(kind=ev["kind"]):
                head = text.split("\n")[0]
                self.assertRegex(head, HEAD)
                rest = head.split(" ", 1)[1]
                self.assertFalse(any(e in rest for e in EMOJI), f"цепочка эмодзи: {head}")

    def test_подвал_ссылка_и_время_по_москве(self):
        for ev, text in self.rendered():
            with self.subTest(kind=ev["kind"]):
                lines = text.split("\n")
                self.assertRegex(lines[-1], FOOT)
                self.assertEqual(lines[-2], "", "подвал отбивается пустой строкой")

    def test_время_подвала_это_время_события(self):
        ev = next(e for e in self.events if not self.alerts.is_ops(e))
        # NOW = 11.08 16:05 UTC = 19:05 МСК
        self.assertTrue(self.alerts.render(ev, now=ta.NOW).endswith("· 11.08, 19:05 МСК"))

    def test_досылка_помечена_под_заголовком(self):
        ev = next(e for e in self.events if not self.alerts.is_ops(e))
        late = self.alerts.render(ev, now=ta.NOW + timedelta(hours=2))
        self.assertTrue(late.split("\n")[1].startswith("⏳ <i>Досылка: событие от 19:05 МСК"),
                        late)
        self.assertNotIn("⏳", self.alerts.render(ev, now=ta.NOW + timedelta(minutes=5)))


class TestNumbers(StandardCase):

    def test_без_iso_utc_и_сырых_float(self):
        for ev, text in self.rendered():
            with self.subTest(kind=ev["kind"]):
                for body in (text, ev["text"]):
                    self.assertNotRegex(body, r"\d{4}-\d{2}-\d{2}")
                    self.assertNotRegex(body, r"\bUTC\b|\d{2}:\d{2}:\d{2}|\dZ\b")
                    clean = DATES.sub(" ", body)
                    self.assertIsNone(re.search(r"\d\.\d", clean), f"точка в числе: {body}")

    def test_минус_типографский(self):
        for ev, text in self.rendered():
            with self.subTest(kind=ev["kind"]):
                self.assertIsNone(re.search(r"(?<![\w])-\d", text), text)

    def test_четырёхзначные_слитно(self):
        for ev, text in self.rendered():
            with self.subTest(kind=ev["kind"]):
                self.assertIsNone(re.search(r"(?<![\d  ])\d[  ]\d{3}(?![\d])", text),
                                  f"четырёхзначное с разрядом: {text}")

    def test_год_текущий_не_пишется(self):
        for ev, text in self.rendered():
            with self.subTest(kind=ev["kind"]):
                self.assertNotRegex(text, r"\b\d{2}\.\d{2}\.2026\b")

    def test_функция_чисел(self):
        w = need(self, "pipeline.lib.wording", "num", "pp", "bp", "bn_rub")
        self.assertEqual(w.num(2391.72, 2), "2391,72")
        self.assertEqual(w.num(86000, 0), "86 000")
        self.assertEqual(w.num(-0.004, 2, plus=True), "+0,00")
        self.assertEqual(w.pp(0.25), "+0,25 п.п.")
        self.assertEqual(w.bp(-25), "−25 б.п.")
        self.assertEqual(w.bn_rub(-7.08, plus=True), "−7,1 млрд ₽")


class TestDirection(StandardCase):

    def test_значок_по_направлению(self):
        w = need(self, "pipeline.lib.wording", "emoji")
        self.assertEqual(w.emoji({"kind": "cb_decision", "direction": "down"}), "📉")
        self.assertEqual(w.emoji({"kind": "cb_decision", "direction": "up"}), "📈")
        # Событие из очереди повторов без поля direction — по «было → стало».
        self.assertEqual(w.emoji({"kind": "cb_decision", "before": "15,00%",
                                  "after": "14,00%"}), "📉")
        self.assertEqual(w.emoji({"kind": "core_flip", "before": "−0,30 (нейтрально)",
                                  "after": "+0,40"}), "📈")
        self.assertEqual(w.emoji({"kind": "position_change"}), "🔔")

    def test_разворот_вниз_со_значком_вниз(self):
        flip = next(e for e in self.events if e["kind"] == "core_flip")
        self.assertTrue(self.alerts.render(flip).startswith("📉 "))


class TestPlainForHub(StandardCase):

    def test_лента_и_хаб_без_разметки_и_висящих_точек(self):
        for ev in self.events:
            if self.alerts.is_ops(ev):
                continue
            with self.subTest(kind=ev["kind"]):
                for body in (ev["text"], self.nexus.compose(ev)):
                    self.assertNotRegex(body, r"<|&lt;|&gt;|&amp;")
                    self.assertNotRegex(body, r"·\s*$|·\s*\n|· ·")

    def test_заголовок_отдельной_фразой(self):
        """B5: «Позиция: деньги → акции деньги (ставка 13,1%) → акции»."""
        for ev in self.events:
            with self.subTest(kind=ev["kind"]):
                title = ev["title"].rstrip(".")
                # Регистр первой буквы не важен: заголовок санитарного пишется со
                # строчной, а плоский текст — предложениями.
                self.assertEqual(ev["text"][:len(title)].lower(), title.lower(), ev["text"][:80])
                after = ev["text"][len(title):]
                if after:
                    self.assertRegex(after, r"^[.!?:…]", "заголовок слипся со следующей фразой")


if __name__ == "__main__":
    unittest.main()
