"""Проверка смысла ИИ-разбора и учёт его работы (pipeline/lib/llm_guard.py).

Файл одинаков в 842 и 843, как и сам модуль. Поведение проверки закреплено общими
векторами `tests/llm_guard_vectors.json`: тот же файл гоняет блок «5б» в 837/838, так
что JS и Python обязаны отвечать на них одинаково — до буквы причины.

Что стережём помимо векторов:
* сводка проверки и журнал: вырезанное и отброшенное попадает в лог с причиной и
  началом текста (не длиннее 200 знаков) — без этого не отличить выдумку от ложного
  срабатывания;
* недельная строка уходит один раз, сбой Telegram повторяется следующим прогоном;
* отказ API по доступу (401/402/403, кончились кредиты, модель снята) — отдельная
  тревога один раз за эпизод и «снова работает» после него;
* проверка не бросает и не виснет ни на каком входе.
"""

import json
import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

from pipeline.lib import llm_guard as g

VECTORS = os.path.join(os.path.dirname(__file__), "llm_guard_vectors.json")
# Часы набора заморожены — правило CI: тесты не смотрят на текущее время.
MON = datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc)      # понедельник, неделя 2026-W44


def load_vectors():
    with open(VECTORS, encoding="utf-8") as fh:
        return json.load(fh)


class TestVectors(unittest.TestCase):
    def test_проверка_совпадает_с_векторами(self):
        for case in load_vectors()["check"]:
            with self.subTest(case=case["name"]):
                got = g.check(case["text"], case["input"], case["index"], case["strict"], case["extra"])
                self.assertEqual(got, case["want"])

    def test_ожидания_человека_в_векторах_выполнены(self):
        # «want» пересобирается генератором, «expect» пишет человек: если генератор
        # когда-нибудь запишет поведение, которого не ждали, это видно здесь.
        for case in load_vectors()["check"]:
            with self.subTest(case=case["name"]):
                want, expect = case["want"], case["expect"]
                self.assertEqual(bool(want["text"]), expect["kept"])
                self.assertEqual([c[2] for c in want["cut"]], expect["cut"])
                self.assertEqual(want["kind"], expect.get("kind"))

    def test_метки_совпадают_с_векторами(self):
        for case in load_vectors()["parse"]:
            with self.subTest(case=case["name"]):
                got, err = g.parse_blocks(case["text"], case["n"])
                self.assertEqual(got, case["want"])
                self.assertEqual(err, case["error"])


class TestReview(unittest.TestCase):
    USER = json.dumps({"сегодня": "28.10.2026", "market_now": {"ключевая_ставка_%": 14.0},
                       "events": [{"i": 0, "событие": "Ставка 14%."}, {"i": 1, "событие": "Вклады 13,01%."}]},
                      ensure_ascii=False)

    def test_вырезанное_и_отброшенное_в_журнале_с_причиной(self):
        long_tail = ("Индекс вырос до 9876 пунктов на фоне спокойной волатильности, " + ", ".join(
            f"и {w} это подтверждают" for w in ("облигации", "вклады", "валюта", "сырьё", "фонды", "банки",
                                                 "брокеры", "экспортёры", "импортёры", "заёмщики")) + ".")
        answer = ("===0===\nСтавка 14% держит деньги в облигациях. Индекс уйдёт ещё на 30–50 бп. "
                  "Вклады остаются сильной альтернативой.\n===1===\n" + long_tail)
        log = []
        got, summary = g.review(answer, self.USER, 2, say=log.append)
        self.assertEqual(got[0], "Ставка 14% держит деньги в облигациях. Вклады остаются сильной альтернативой.")
        self.assertIsNone(got[1])
        self.assertEqual(summary, {"shown": 1, "cut": 1, "rejected": {"числа": 1}})
        cut = [m for m in log if "вырезано" in m]
        self.assertEqual(len(cut), 2)                     # фраза события 0 и единственная фраза события 1
        self.assertIn("30–50 бп", cut[0])
        dropped = [m for m in log if "отброшен" in m]
        self.assertEqual(len(dropped), 1)
        self.assertIn("событие 1", dropped[0])
        for m in log:
            quoted = m[m.index("«") + 1:m.rindex("»")]
            self.assertLessEqual(len(quoted), g.LOG_CHARS, "фраза в журнале длиннее 200 знаков")

    def test_сбой_нумерации_бракует_весь_ответ_и_пишет_его_начало(self):
        log = []
        got, summary = g.review("===1===\nПервый разбор про ставку.\n===2===\nВторой.", self.USER, 2, say=log.append)
        self.assertEqual(got, [None, None])
        self.assertEqual(summary["rejected"], {"метки": 2})
        self.assertIn("===1=== ===2=== вместо 0…1", log[0])
        self.assertIn("Первый разбор", log[0])

    def test_сломанный_журнал_не_ломает_проверку(self):
        def boom(_):
            raise UnicodeEncodeError("charmap", "я", 0, 1, "не лезет")
        got, _ = g.review("===0===\nСтавка 14% держит деньги в облигациях, и это главное.", self.USER, 2, say=boom)
        self.assertIsNotNone(got[0])


class TestRobust(unittest.TestCase):
    def test_мусор_на_входе_не_бросает(self):
        for text, user in ((None, None), ("", "{"), (12345, [1, 2]), ("Текст " * 30, {"events": "не список"}),
                           ("Ставка 14% и 1e309 нечто. " * 3, '{"x": 1e999}')):
            with self.subTest(text=str(text)[:20]):
                res = g.check(text, user, 0)
                self.assertIn("text", res)
        self.assertEqual(g.parse_blocks(None, 2), (None, "нет меток ===N==="))

    def test_огромный_ответ_проверяется_быстро(self):
        # Поиск зацикливания квадратичен по длине: смотрим только голову ответа.
        noise = "".join("абвгдеёжзийклмнопрстуфхцчшщыьэюя "[(i * 7919) % 33] for i in range(g.SCAN_LIMIT))
        loop = "одна и та же длинная фраза подряд. " * 3000
        started = time.monotonic()
        g.check(noise + loop)
        self.assertLess(time.monotonic() - started, 3.0, "проверка встала на длинном ответе")
        self.assertEqual(g.check(loop + noise)["reject"], "текст зациклился")


class TestStats(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "llm_stats.json")
        self.sent = []
        self.ok = True

    def send(self, text):
        self.sent.append(text)
        return self.ok

    def tick(self, now):
        stats = g.load_stats(self.path)
        out = g.ops_tick(stats, now, "842", self.send, "ключ — ANTHROPIC_API_KEY в env.")
        g.save_stats(self.path, stats)
        return out

    def rec(self, now, **kw):
        g.save_stats(self.path, g.record(g.load_stats(self.path), now, **kw))

    def test_недельная_строка_уходит_один_раз_в_понедельник(self):
        self.rec(MON, asked=3, summary={"shown": 2, "cut": 1, "rejected": {"числа": 1}}, usd=0.0412)
        self.rec(MON + timedelta(days=2), asked=1, failed="API 529", usd=0.0)
        self.assertEqual(self.tick(MON + timedelta(days=3)), [], "строку за неделю слать рано")
        self.assertEqual(self.tick(MON + timedelta(days=7)), ["неделя 2026-W44"])
        self.assertEqual(self.sent, ["📊 842 · ИИ-разборы за 26.10–01.11: показано 2 из 4; отбраковано 1 "
                                     "(числа 1); вырезано фраз 1; без ответа API 529 1; потрачено $0,04"])
        self.assertEqual(self.tick(MON + timedelta(days=7, hours=1)), [], "строка ушла дважды")

    def test_сбой_отправки_повторяется_следующим_прогоном(self):
        self.rec(MON, asked=1, summary={"shown": 1, "cut": 0, "rejected": {}}, usd=0.02)
        self.ok = False
        self.assertEqual(self.tick(MON + timedelta(days=7)), [])
        self.ok = True
        self.assertEqual(self.tick(MON + timedelta(days=7, minutes=5)), ["неделя 2026-W44"])
        self.assertEqual(len(self.sent), 2)

    def test_неделя_без_событий(self):
        self.tick(MON)
        self.tick(MON + timedelta(days=7))
        self.assertEqual(self.sent, ["📊 842 · ИИ-разборы за 26.10–01.11: событий для разбора не было; "
                                     "потрачено $0,00"])

    def test_отказ_по_доступу_одна_тревога_и_снова_работает(self):
        self.rec(MON, asked=2, failed="API 401",
                 access={"status": 401, "kind": "authentication_error", "message": "invalid x-api-key"})
        self.tick(MON)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("842 · ИИ-разбор не работает", self.sent[0])
        self.assertIn("401", self.sent[0])
        self.assertIn("ключ не принят", self.sent[0])
        self.assertIn("Смотреть:", self.sent[0])
        # тот же отказ в следующих прогонах — без новой тревоги
        self.rec(MON + timedelta(minutes=5), asked=1, failed="API 401",
                 access={"status": 401, "kind": "authentication_error", "message": "invalid x-api-key"})
        self.tick(MON + timedelta(minutes=5))
        self.assertEqual(len(self.sent), 1, "тревога о том же отказе повторилась")
        # ответ пришёл — «снова работает»
        self.rec(MON + timedelta(hours=2), asked=1, summary={"shown": 1, "cut": 0, "rejected": {}}, ok=True)
        self.tick(MON + timedelta(hours=2))
        self.assertEqual(len(self.sent), 2)
        self.assertIn("снова работает", self.sent[1])
        self.assertIn("26.10 12:00", self.sent[1])

    def test_эпизод_без_отправленной_тревоги_молча_закрывается(self):
        self.rec(MON, asked=1, failed="API 402", access={"status": 402, "kind": "billing_error", "message": ""})
        self.rec(MON + timedelta(minutes=1), asked=1, summary={"shown": 1, "cut": 0, "rejected": {}}, ok=True)
        self.tick(MON + timedelta(minutes=2))
        self.assertEqual(self.sent, [], "«снова работает» без тревоги — шум")

    def test_что_считается_отказом_по_доступу(self):
        yes = [(401, ""), (402, ""), (403, ""), (400, "Your credit balance is too low to access the API"),
               (404, "model: claude-sonnet-5-5")]
        no = [(400, "messages: roles must alternate"), (404, "not found"), (429, ""), (500, ""), (529, "")]
        for status, msg in yes:
            self.assertTrue(g.access_problem(status, msg), (status, msg))
        for status, msg in no:
            self.assertFalse(g.access_problem(status, msg), (status, msg))

    def test_битый_файл_учёта_не_ломает_прогон(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{не json")
        self.assertEqual(self.tick(MON), [])
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"cur": "строка вместо записи", "access": 5}, fh)
        self.tick(MON)
        self.rec(MON, asked=1, usd=0.01)


if __name__ == "__main__":
    unittest.main()
