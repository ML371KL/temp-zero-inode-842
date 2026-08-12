"""Разбор событий бесплатной моделью (контракт §6).

Проверяем то, чем этот канал портится молча — все пункты списка оплачены в 837,
где комментатор живёт дольше:

* платная модель не имеет права уйти в сеть без явного разрешения (прогонов до
  сотни в сутки, одна опечатка в переменной жгла бы деньги);
* черновые размышления модели — не ответ: однажды поток мыслей уехал читателю
  целиком и по-английски;
* перегрузка бесплатного провайдера приходит телом с `error` при коде 200, а не
  HTTP-ошибкой — без разбора этого случая цепочка не переключалась бы;
* англоязычный или зациклившийся ответ должен быть отброшен, а не показан;
* дефолтный `User-Agent` ловит 403 на защите от ботов (см. lib/nexus.py);
* отсутствие ключа — законный режим, а не отказ.

В сеть не ходим: подменяется urlopen внутри модуля.
"""

import json
import os
import random
import time
import unittest
import urllib.error
from unittest import mock

from tests import need

ANSWER = ("===0===\n"
          "Рынок отыгрывает смягчение ставки: длинные ОФЗ дорожают первыми, и это "
          "обычный порядок в начале цикла снижения.\n"
          "===1===\n"
          "Движение рядовое, за пределы обычного дневного размаха оно не выходит и "
          "решения не меняет.\n")


class FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def chat(content, reasoning=None, finish="stop"):
    message = {"content": content}
    if reasoning is not None:
        message["reasoning"] = reasoning
    return {"choices": [{"message": message, "finish_reason": finish}]}


class CommentaryCase(unittest.TestCase):
    def setUp(self):
        self.mod = need(self, "pipeline.lib.commentary", "comments", "parse", "annotate")
        self.prev = {k: os.environ.get(k) for k in
                     ("OPENROUTER_KEY", "MOEX_LLM_MODEL", "LLM_ALLOW_PAID")}
        self.addCleanup(self._restore)
        os.environ["OPENROUTER_KEY"] = "ключ"
        os.environ.pop("MOEX_LLM_MODEL", None)
        os.environ.pop("LLM_ALLOW_PAID", None)
        self.calls = []
        self.log = []

    def _restore(self):
        for key, val in self.prev.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    def _urlopen(self, *responses):
        """Ответы по одному на вызов; исключение в списке — бросается."""
        queue = list(responses)

        def _open(req, timeout=None):
            self.calls.append({"model": json.loads(req.data.decode("utf-8"))["model"],
                               "body": json.loads(req.data.decode("utf-8")),
                               "ua": req.get_header("User-agent"),
                               "auth": req.get_header("Authorization")})
            item = queue.pop(0) if queue else responses[-1]
            if isinstance(item, Exception):
                raise item
            return FakeResponse(item)
        return mock.patch.object(self.mod.urllib.request, "urlopen", _open)

    def events(self):
        return [{"kind": "state_cell_change", "text": "Смена ячейки: A → B."},
                {"kind": "core_flip", "text": "Ядро развернулось: +0.66."}]

    def panel(self):
        return {"quotes": {"imoex": {"label": "Индекс МосБиржи", "value": 2323.82,
                                     "chg_pct": 1.33, "asof": "2026-08-11"}},
                "states": {"current": {"trend": 0, "vol": 1, "bond": 1}},
                "monitors": [{"id": "cb_meeting", "payload": {"key_rate": 14.0}}],
                "verdict": {"cell_code": "bear|stress|stress", "cell_label": "токсичная"}}


class TestHappyPath(CommentaryCase):
    def test_two_events_two_comments(self):
        with self._urlopen(chat(ANSWER)):
            got = self.mod.comments(self.events(), self.panel())
        self.assertEqual(len(got), 2)
        self.assertIn("ОФЗ", got[0])
        self.assertIn("рядовое", got[1])

    def test_request_shape(self):
        with self._urlopen(chat(ANSWER)):
            self.mod.comments(self.events(), self.panel())
        call = self.calls[0]
        self.assertEqual(call["model"], self.mod.FREE_MODEL)
        self.assertEqual(call["auth"], "Bearer ключ")
        # Тот же урок, что в lib/nexus.py: дефолтный UA ловит 403 на защите от ботов.
        self.assertTrue(call["ua"] and "urllib" not in call["ua"].lower())
        self.assertTrue(call["body"].get("reasoning", {}).get("exclude"))

    def test_market_context_has_no_panel_internals(self):
        # мутация: отдать модели вердикт и баллы -> «композит», «ячейка» и «ядро»
        # протекают в текст, а читатель ленты про устройство панели не знает.
        ctx = self.mod.market_now(self.panel())
        flat = json.dumps(ctx, ensure_ascii=False).lower()
        for leak in ("cell", "ячейк", "композит", "вердикт", "балл"):
            self.assertNotIn(leak, flat)
        self.assertIn("Индекс МосБиржи", ctx)
        self.assertEqual(ctx.get("ключевая_ставка_%"), 14.0)
        self.assertIn("ОФЗ в стрессе", ctx.get("режим_рынка", ""))


class TestGuards(CommentaryCase):
    def test_paid_model_never_leaves_without_permission(self):
        os.environ["MOEX_LLM_MODEL"] = "anthropic/claude-opus-5"
        with self._urlopen(chat(ANSWER)):
            self.assertIsNone(self.mod.comments(self.events(), self.panel(), log=self.log.append))
        self.assertEqual(self.calls, [], "платная модель ушла в сеть без разрешения")

    def test_paid_model_allowed_explicitly(self):
        os.environ["MOEX_LLM_MODEL"] = "anthropic/claude-opus-5"
        os.environ["LLM_ALLOW_PAID"] = "1"
        with self._urlopen(chat(ANSWER)):
            self.assertIsNotNone(self.mod.comments(self.events(), self.panel()))

    def test_whole_default_chain_is_free(self):
        self.assertTrue(all(m.endswith(":free") for m in self.mod.FREE_CHAIN))
        # Провайдеры разные намеренно: перегрузка одного не выключает разбор.
        self.assertGreaterEqual(len({m.split("/")[0] for m in self.mod.FREE_CHAIN}), 3)

    def test_no_key_is_silence_not_failure(self):
        os.environ.pop("OPENROUTER_KEY", None)
        self.assertIsNone(self.mod.comments(self.events(), self.panel()))
        self.assertEqual(self.calls, [])


class TestBadAnswers(CommentaryCase):
    def test_thinking_without_markup_is_not_an_answer(self):
        # Поток мыслей («The user wants me to analyze…») однажды уехал читателю.
        with self._urlopen(chat("", reasoning="The user wants me to analyze the event"),
                           chat(ANSWER)):
            got = self.mod.comments(self.events(), self.panel(), log=self.log.append)
        self.assertIn("ОФЗ", got[0])
        self.assertEqual([c["model"] for c in self.calls][:2],
                         [self.mod.FREE_MODEL, self.mod.FREE_CHAIN[1]])

    def test_thinking_with_markup_is_accepted(self):
        with self._urlopen(chat("", reasoning="ладно, пишу\n" + ANSWER)):
            got = self.mod.comments(self.events(), self.panel())
        self.assertIn("ОФЗ", got[0])

    def test_provider_overload_arrives_as_200_with_error(self):
        overload = {"error": {"message": "ResourceExhausted: Worker local total request limit"}}
        with self._urlopen(overload, chat(ANSWER)):
            got = self.mod.comments(self.events(), self.panel(), log=self.log.append)
        self.assertIsNotNone(got)
        self.assertEqual(len(self.calls), 2, "цепочка не переключилась на следующую модель")

    def test_english_answer_is_dropped(self):
        english = "===0===\nThe central bank cut rates and bonds rallied sharply today.\n"
        with self._urlopen(chat(english), chat(ANSWER)):
            got = self.mod.comments(self.events(), self.panel(), log=self.log.append)
        self.assertIn("ОФЗ", got[0])

    def test_transliterating_model_is_dropped(self):
        # Из первого же боевого ответа: «atractтивными». Промпт запрещает смешивать
        # алфавиты внутри слова, но требование можно и проигнорировать.
        mixed = ("===0===\nДоходности выглядят atractтивными, а рынок фondовый остаётся "
                 "под давлением ставки и слабого рубля в течение месяца.\n")
        with self._urlopen(chat(mixed), chat(ANSWER)):
            got = self.mod.comments(self.events(), self.panel(), log=self.log.append)
        self.assertIn("ОФЗ", got[0])
        self.assertEqual(len(self.calls), 2)

    def test_single_slip_is_tolerated(self):
        # Одна описка в длинном разборе — не повод выбрасывать разумный текст:
        # следующая бесплатная модель почти наверняка ответит хуже.
        one = ("===0===\nДоходности выглядят atractтивными на фоне ставки 14%, и это "
               "меняет расклад для длинных облигаций в ближайшие месяцы.\n")
        with self._urlopen(chat(one)):
            got = self.mod.comments(self.events(), self.panel())
        self.assertIn("atract", got[0])
        self.assertEqual(len(self.calls), 1)

    def test_looping_answer_is_dropped(self):
        loop = "===0===\n" + "рынок падает и падает, " * 12 + "\n"
        with self._urlopen(chat(loop), chat(ANSWER)):
            got = self.mod.comments(self.events(), self.panel(), log=self.log.append)
        self.assertIn("ОФЗ", got[0])

    def test_unparsable_answer_moves_to_the_next_model(self):
        with self._urlopen(chat("просто текст без разметки"), chat(ANSWER)):
            got = self.mod.comments(self.events(), self.panel(), log=self.log.append)
        self.assertIsNotNone(got)
        self.assertEqual(len(self.calls), 2)

    def test_slow_chain_stops_at_the_budget(self):
        # Прогон публикует витрину ПОСЛЕ алертов: четыре медленные модели подряд
        # съели бы RuntimeMaxSec юнита, и панель не обновилась бы из-за украшения
        # к событию. Замер с прод-ключа: 152 с у основной при таймауте сокета 90 с.
        # старт цепочки, затем проверка бюджета перед второй моделью
        clock = iter([0.0, 500.0, 500.0, 500.0, 500.0])
        with mock.patch.object(self.mod.time, "monotonic", lambda: next(clock)), \
             self._urlopen(urllib.error.URLError("провайдер молчит")):
            self.assertIsNone(self.mod.comments(self.events(), self.panel(), log=self.log.append))
        self.assertEqual(len(self.calls), 1, "после исчерпания бюджета цепочка обязана встать")
        self.assertTrue(any("бюджет" in m for m in self.log))

    def test_broken_budget_variable_does_not_kill_the_run(self):
        # Раньше бюджет читался на уровне модуля: `LLM_BUDGET_S=скоро` — это
        # ValueError в момент import, то есть опечатка в окружении роняла ВЕСЬ
        # прогон конвейера, а не комментарии. Контракт модуля обратный.
        prev = os.environ.get("LLM_BUDGET_S")
        self.addCleanup(lambda: os.environ.__setitem__("LLM_BUDGET_S", prev)
                        if prev is not None else os.environ.pop("LLM_BUDGET_S", None))
        for junk in ("скоро", "", "  ", "-5", "0"):
            with self.subTest(value=junk):
                os.environ["LLM_BUDGET_S"] = junk
                self.assertEqual(self.mod.budget_s(), self.mod.BUDGET_DEFAULT_S)
        os.environ["LLM_BUDGET_S"] = "30"
        self.assertEqual(self.mod.budget_s(), 30.0)

    def test_enormous_answer_is_checked_by_its_head(self):
        # Поиск зацикливания квадратичен по длине: 8 тыс. знаков — 0,41 с, 100 КБ —
        # около минуты. Интрадей-такт идёт каждые 5 минут под RuntimeMaxSec, и
        # минута в регулярном выражении там лишняя. Проверяем ГОЛОВУ ответа.
        rnd = random.Random(7)  # без часов: последовательность фиксирована зерном
        def noise(n):
            return "".join(rnd.choice("абвгдеёжзийклмнопрстуфхцчшщыьэюя ") for _ in range(n))

        clean = noise(self.mod._SCAN_LIMIT)
        loop = "одна и та же длинная фраза подряд. " * 3000
        started = time.monotonic()
        self.assertIsNone(self.mod._defect(clean + loop))
        self.assertLess(time.monotonic() - started, 1.0, "разбор ответа встал в регулярке")
        # А брак в ГОЛОВЕ по-прежнему ловится — окно урезает цену, а не строгость.
        self.assertEqual(self.mod._defect(loop + clean), "текст зациклился")

    def test_every_model_down_is_silence_not_exception(self):
        with self._urlopen(urllib.error.URLError("сеть легла")):
            got = self.mod.comments(self.events(), self.panel(), log=self.log.append)
        self.assertIsNone(got)
        self.assertEqual(len(self.calls), len(self.mod.FREE_CHAIN))

    def test_broken_logger_never_breaks_the_run(self):
        # ОПЛАЧЕНО ПРОБНЫМ ПРОГОНОМ: print с кириллицей в консоль cp1252 бросает
        # UnicodeEncodeError — подкласс ValueError, и он приходил ровно в тот
        # обработчик, который сам зовёт say(). Второе исключение не ловил никто.
        def boom(_message):
            raise UnicodeEncodeError("charmap", "я", 0, 1, "не лезет в кодировку")

        with self._urlopen(urllib.error.URLError("сеть легла")):
            self.assertIsNone(self.mod.comments(self.events(), self.panel(), log=boom))

    def test_partial_answer_keeps_what_is_good(self):
        half = "===0===\n" + "Длинные ОФЗ дорожают первыми — обычный порядок в начале цикла снижения ставки.\n"
        with self._urlopen(chat(half)):
            got = self.mod.comments(self.events(), self.panel())
        self.assertIn("ОФЗ", got[0])
        self.assertIsNone(got[1], "второе событие остаётся без комментария, а не с чужим")


class TestAnnotate(CommentaryCase):
    def test_ops_events_never_get_a_comment(self):
        # Санитарные уходят в ops-канал: разбор рынка там не к месту и стоит вызова.
        evs = [{"kind": "source_stale", "text": "Источник iss: error."},
               {"kind": "core_flip", "text": "Ядро развернулось: +0.66."}]
        with self._urlopen(chat("===0===\nРазбор одного события про разворот ядра сюда.\n")):
            filled = self.mod.annotate(evs, self.panel())
        self.assertEqual(filled, 1)
        self.assertNotIn("comment", evs[0])
        self.assertIn("разворот", evs[1]["comment"])
        self.assertEqual(len(self.calls[0]["body"]["messages"][1]["content"].split('"i":')) - 1, 1)

    def test_already_commented_event_is_not_asked_again(self):
        # Событие из очереди повторов уже несёт свой разбор — второй вызов был бы
        # лишними деньгами и лишним риском получить другой текст.
        evs = [{"kind": "core_flip", "text": "Ядро развернулось.", "comment": "уже есть"}]
        with self._urlopen(chat(ANSWER)):
            self.assertEqual(self.mod.annotate(evs, self.panel()), 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(evs[0]["comment"], "уже есть")


if __name__ == "__main__":
    unittest.main()
