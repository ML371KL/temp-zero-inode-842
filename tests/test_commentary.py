"""Разбор событий моделью Claude через Messages API (контракт §6).

Проверяем то, чем этот канал портится молча:

* запрос собран по документации: ключ в `x-api-key`, версия API, бета серверного
  резерва, `thinking` и `effort`, без `temperature` (на этой версии API это 400);
* модели передаётся сегодняшняя дата — без неё она опиралась на устаревшие знания;
* в ответе читаются только блоки `text`: размышления — не ответ;
* отказ модели по политике и обрыв по `max_tokens` — не разбор и не повод повторять;
* 429/5xx/529 повторяются с уважением `retry-after`, 4xx — нет;
* медленный ответ не съедает дедлайн прогона: предел по часам, а не по сокету;
* суточный потолок в долларах останавливает разбор, а не доставку;
* англоязычный, транслитный, зациклившийся ответ отбрасывается, а не показывается;
* отсутствие ключа — законный режим, а не отказ.

В сеть не ходим: подменяется urlopen внутри модуля.
"""

import email.message
import io
import json
import os
import random
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
from datetime import datetime, timezone
from unittest import mock

from tests import need

# Часы набора заморожены — правило CI: тесты не смотрят на текущее время.
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
EPOCH = NOW.timestamp()
TODAY = "28.10.2026"

ANSWER = ("===0===\n"
          "Рынок отыгрывает смягчение ставки: длинные ОФЗ дорожают первыми, и это "
          "обычный порядок в начале цикла снижения.\n"
          "===1===\n"
          "Движение рядовое, за пределы обычного дневного размаха оно не выходит и "
          "решения не меняет.\n")


def message(text, stop="end_turn", thinking=True, usage=None):
    content = [{"type": "thinking", "thinking": "", "signature": "sig"}] if thinking else []
    content.append({"type": "text", "text": text})
    return {"id": "msg_test", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
            "content": content, "stop_reason": stop, "stop_details": None,
            "usage": usage or {"input_tokens": 1500, "output_tokens": 2500,
                               "output_tokens_details": {"thinking_tokens": 2000}}}


def http_error(status, kind="overloaded_error", retry_after=None):
    headers = email.message.Message()
    if retry_after is not None:
        headers["retry-after"] = str(retry_after)
    body = json.dumps({"type": "error", "error": {"type": kind, "message": "занято"}}).encode()
    return urllib.error.HTTPError("https://api.anthropic.com/v1/messages", status, "err", headers, io.BytesIO(body))


class FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


class CommentaryCase(unittest.TestCase):
    ENV = ("ANTHROPIC_API_KEY", "MOEX_LLM_MODEL", "LLM_EFFORT", "LLM_BUDGET_S",
           "LLM_DEADLINE_AT", "LLM_DAILY_USD", "STATE_DIR")

    def setUp(self):
        self.mod = need(self, "pipeline.lib.commentary", "comments", "parse", "annotate")
        self.prev = {k: os.environ.get(k) for k in self.ENV}
        self.addCleanup(self._restore)
        for k in self.ENV:
            os.environ.pop(k, None)
        os.environ["ANTHROPIC_API_KEY"] = "ключ"
        self.state = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.state, True)
        os.environ["STATE_DIR"] = self.state
        self.calls = []
        self.log = []
        self.sleeps = []
        for target, name, value in ((self.mod.time, "sleep", self.sleeps.append),
                                    (self.mod, "_now", lambda: NOW),
                                    (self.mod, "_epoch", lambda: EPOCH)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

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
            self.calls.append({"url": req.full_url,
                               "body": json.loads(req.data.decode("utf-8")),
                               "headers": {k.lower(): v for k, v in req.header_items()},
                               "timeout": timeout})
            item = queue.pop(0) if queue else responses[-1]
            if isinstance(item, BaseException):
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
        with self._urlopen(message(ANSWER)):
            got = self.mod.comments(self.events(), self.panel())
        self.assertEqual(len(got), 2)
        self.assertIn("ОФЗ", got[0])
        self.assertIn("рядовое", got[1])

    def test_request_shape(self):
        with self._urlopen(message(ANSWER)):
            self.mod.comments(self.events(), self.panel())
        call = self.calls[0]
        self.assertEqual(call["url"], "https://api.anthropic.com/v1/messages")
        self.assertEqual(call["headers"]["x-api-key"], "ключ")
        self.assertEqual(call["headers"]["anthropic-version"], "2026-10-21")
        self.assertIn("server-side-fallback-2026-07-01", call["headers"]["anthropic-beta"])
        # Тот же урок, что в lib/nexus.py: дефолтный UA ловит 403 на защите от ботов.
        self.assertNotIn("urllib", call["headers"]["user-agent"].lower())
        body = call["body"]
        self.assertEqual(body["model"], "claude-sonnet-5-5")
        self.assertEqual(body["thinking"], {"type": "adaptive"})
        self.assertEqual(body["output_config"], {"effort": "xhigh"})
        self.assertEqual(body["fallbacks"], "default")
        for banned in ("temperature", "top_p", "top_k", "reasoning"):
            self.assertNotIn(banned, body, f"{banned} в запросе — на этой версии API это 400")
        self.assertEqual(body["system"], self.mod.SYSTEM)

    def test_model_gets_todays_date(self):
        # Без даты модель опиралась на свои устаревшие знания о «текущих» событиях.
        with self._urlopen(message(ANSWER)):
            self.mod.comments(self.events(), self.panel())
        user = json.loads(self.calls[0]["body"]["messages"][0]["content"])
        self.assertEqual(user["сегодня"], TODAY)

    def test_model_and_effort_can_be_overridden(self):
        os.environ["MOEX_LLM_MODEL"] = "claude-opus-5-5"
        os.environ["LLM_EFFORT"] = "max"
        with self._urlopen(message(ANSWER)):
            self.mod.comments(self.events(), self.panel())
        self.assertEqual(self.calls[0]["body"]["model"], "claude-opus-5-5")
        self.assertEqual(self.calls[0]["body"]["output_config"], {"effort": "max"})

    def test_unknown_effort_falls_back_to_default(self):
        os.environ["LLM_EFFORT"] = "очень высокий"
        self.assertEqual(self.mod.effort(), "xhigh")

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

    def test_only_text_blocks_are_the_answer(self):
        # Размышления — не ответ: однажды поток мыслей уехал читателю целиком.
        data = message(ANSWER)
        data["content"].insert(0, {"type": "thinking", "thinking": "The user wants me to analyze", "signature": "s"})
        with self._urlopen(data):
            got = self.mod.comments(self.events(), self.panel())
        self.assertNotIn("user wants", " ".join(got))

    def test_no_key_is_silence_not_failure(self):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        self.assertIsNone(self.mod.comments(self.events(), self.panel()))
        self.assertEqual(self.calls, [])


class TestRefusalsAndRetries(CommentaryCase):
    def test_refusal_is_not_an_answer_and_not_retried(self):
        data = message("", stop="refusal")
        data["content"] = []
        data["stop_details"] = {"type": "refusal", "category": "general_harms"}
        with self._urlopen(data):
            self.assertIsNone(self.mod.comments(self.events(), self.panel(), log=self.log.append))
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(any("отказалась" in m for m in self.log))

    def test_cut_by_max_tokens_is_not_an_answer(self):
        with self._urlopen(message("===0===\nОборванный разбор про ставку и облигации, который", stop="max_tokens")):
            self.assertIsNone(self.mod.comments(self.events(), self.panel(), log=self.log.append))
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(any("max_tokens" in m for m in self.log))

    def test_overload_is_retried(self):
        with self._urlopen(http_error(529), message(ANSWER)):
            got = self.mod.comments(self.events(), self.panel(), log=self.log.append)
        self.assertIsNotNone(got)
        self.assertEqual(len(self.calls), 2)

    def test_retry_after_is_respected(self):
        with self._urlopen(http_error(429, "rate_limit_error", retry_after=7), message(ANSWER)):
            self.assertIsNotNone(self.mod.comments(self.events(), self.panel()))
        self.assertEqual(self.sleeps, [7.0])

    def test_client_errors_are_not_retried(self):
        with self._urlopen(http_error(400, "invalid_request_error"), message(ANSWER)):
            self.assertIsNone(self.mod.comments(self.events(), self.panel(), log=self.log.append))
        self.assertEqual(len(self.calls), 1, "400 не лечится повтором — это лишние деньги")

    def test_error_body_at_200_is_retried(self):
        with self._urlopen({"type": "error", "error": {"type": "overloaded_error", "message": "x"}}, message(ANSWER)):
            self.assertIsNotNone(self.mod.comments(self.events(), self.panel()))
        self.assertEqual(len(self.calls), 2)

    def test_every_attempt_down_is_silence_not_exception(self):
        with self._urlopen(urllib.error.URLError("сеть легла")):
            got = self.mod.comments(self.events(), self.panel(), log=self.log.append)
        self.assertIsNone(got)
        self.assertEqual(len(self.calls), self.mod.ATTEMPTS)

    def test_broken_logger_never_breaks_the_run(self):
        # ОПЛАЧЕНО ПРОБНЫМ ПРОГОНОМ: print с кириллицей в консоль cp1252 бросает
        # UnicodeEncodeError — подкласс ValueError, и он приходил ровно в тот
        # обработчик, который сам зовёт say(). Второе исключение не ловил никто.
        def boom(_message):
            raise UnicodeEncodeError("charmap", "я", 0, 1, "не лезет в кодировку")

        with self._urlopen(urllib.error.URLError("сеть легла")):
            self.assertIsNone(self.mod.comments(self.events(), self.panel(), log=boom))


class TestTimeAndMoney(CommentaryCase):
    def test_hanging_request_is_cut_by_the_clock(self):
        # urlopen(timeout=) ограничивает ПРОСТОЙ сокета, а не длительность: висящий
        # ответ съедал бы дедлайн прогона. Предел — по часам, в фоновом потоке.
        def hang(body, key, timeout):
            # time.sleep в этом наборе подменён (повторы не ждут), поэтому висим на событии.
            threading.Event().wait(5)
        os.environ["LLM_BUDGET_S"] = "1"
        with mock.patch.object(self.mod, "_post", hang), \
             mock.patch.object(self.mod, "MIN_ATTEMPT_S", 0.5):
            started = time.monotonic()
            self.assertIsNone(self.mod.comments(self.events(), self.panel(), log=self.log.append))
        self.assertLess(time.monotonic() - started, 4.0, "ожидание ответа не ограничено по часам")

    def test_run_deadline_leaves_no_time_for_the_model(self):
        os.environ["LLM_DEADLINE_AT"] = str(EPOCH + 5)
        with self._urlopen(message(ANSWER)):
            self.assertIsNone(self.mod.comments(self.events(), self.panel(), log=self.log.append))
        self.assertEqual(self.calls, [], "запрос начат, когда до дедлайна прогона не хватает времени")
        self.assertTrue(any("дедлайн" in m for m in self.log))

    def test_request_timeout_follows_the_budget(self):
        os.environ["LLM_DEADLINE_AT"] = str(EPOCH + 40)
        with self._urlopen(message(ANSWER)):
            self.mod.comments(self.events(), self.panel())
        self.assertLessEqual(self.calls[0]["timeout"], 40)

    def test_broken_budget_variable_does_not_kill_the_run(self):
        # Раньше бюджет читался на уровне модуля: `LLM_BUDGET_S=скоро` — это
        # ValueError в момент import, то есть опечатка в окружении роняла ВЕСЬ
        # прогон конвейера, а не комментарии. Контракт модуля обратный.
        for junk in ("скоро", "", "  ", "-5", "0"):
            with self.subTest(value=junk):
                os.environ["LLM_BUDGET_S"] = junk
                self.assertEqual(self.mod.budget_s(), self.mod.BUDGET_DEFAULT_S)
        os.environ["LLM_BUDGET_S"] = "30"
        self.assertEqual(self.mod.budget_s(), 30.0)
        os.environ["LLM_DEADLINE_AT"] = "завтра"
        self.assertIsNone(self.mod.deadline_left())

    def test_spend_is_counted_from_usage(self):
        usage = {"input_tokens": 1_000_000, "output_tokens": 100_000}
        self.assertAlmostEqual(self.mod.cost_usd(usage), 2.0 + 1.0)
        with self._urlopen(message(ANSWER, usage={"input_tokens": 2000, "output_tokens": 3000})):
            self.mod.comments(self.events(), self.panel())
        with open(os.path.join(self.state, self.mod.SPEND_FILE), encoding="utf-8") as fh:
            spent = json.load(fh)
        self.assertEqual(spent["day"], TODAY)
        self.assertAlmostEqual(spent["usd"], 0.034, places=6)

    def test_daily_cap_stops_the_model_not_the_delivery(self):
        os.environ["LLM_DAILY_USD"] = "0.05"
        with open(os.path.join(self.state, self.mod.SPEND_FILE), "w", encoding="utf-8") as fh:
            json.dump({"day": TODAY, "usd": 0.06}, fh)
        with self._urlopen(message(ANSWER)):
            self.assertIsNone(self.mod.comments(self.events(), self.panel(), log=self.log.append))
        self.assertEqual(self.calls, [], "потолок расходов исчерпан, а запрос ушёл")
        self.assertTrue(any("потолок" in m for m in self.log))

    def test_yesterdays_spend_does_not_count(self):
        os.environ["LLM_DAILY_USD"] = "0.05"
        with open(os.path.join(self.state, self.mod.SPEND_FILE), "w", encoding="utf-8") as fh:
            json.dump({"day": "01.01.2026", "usd": 9.0}, fh)
        with self._urlopen(message(ANSWER)):
            self.assertIsNotNone(self.mod.comments(self.events(), self.panel()))


class TestBadAnswers(CommentaryCase):
    def test_english_answer_is_dropped(self):
        english = "===0===\nThe central bank cut rates and bonds rallied sharply today.\n"
        with self._urlopen(message(english)):
            self.assertIsNone(self.mod.comments(self.events(), self.panel(), log=self.log.append))

    def test_mixed_alphabets_are_dropped(self):
        # Из первого же боевого ответа бесплатной модели: «atractтивными».
        mixed = ("===0===\nДоходности выглядят atractтивными, а рынок фondовый остаётся "
                 "под давлением ставки и слабого рубля в течение месяца.\n")
        with self._urlopen(message(mixed)):
            self.assertIsNone(self.mod.comments(self.events(), self.panel(), log=self.log.append))

    def test_single_slip_is_tolerated(self):
        # Одна описка в длинном разборе — не повод выбрасывать разумный текст.
        one = ("===0===\nДоходности выглядят atractтивными на фоне ставки 14%, и это "
               "меняет расклад для длинных облигаций в ближайшие месяцы.\n")
        with self._urlopen(message(one)):
            got = self.mod.comments(self.events(), self.panel())
        self.assertIn("atract", got[0])

    def test_transliteration_and_foreign_script_are_dropped(self):
        # Оба дефекта видели вживую в 837: «kvantitativnogo uzhestocheniya», «продажи حكومات».
        for text in ("Модель пишет kvantitativnogo uzhestocheniya вместо русского языка в разборе ставки.",
                     "Продажи حكومات давят на рынок облигаций и ставку в течение всего месяца."):
            with self.subTest(text=text[:20]):
                self.assertIsNotNone(self.mod._defect(text))

    def test_looping_answer_is_dropped(self):
        loop = "===0===\n" + "рынок падает и падает, " * 12 + "\n"
        with self._urlopen(message(loop)):
            self.assertIsNone(self.mod.comments(self.events(), self.panel(), log=self.log.append))

    def test_метка_блока_без_новой_строки(self):
        """ОПЛАЧЕНО ЖИВЫМ ОТВЕТОМ 14.08.2026: метка пришла склеенной с текстом.

        «…построенную на экспортно-импортных равновесиях.===1===\nСтавка…» —
        строгая регулярка такую метку не видела вовсе. Разбор возвращал ОДИН блок:
        комментарий второго события утекал хвостом в первое (читатель видел
        «===1===» прямо в сообщении), а второе оставалось без разбора совсем.
        """
        glued = ("===0===\nКурс ушёл слабее зоны — отклонение −6,7%."
                 "===1===\nСтавка выросла до 14,8%.")
        got = self.mod.parse(glued, 2)
        self.assertEqual(got, ["Курс ушёл слабее зоны — отклонение −6,7%.",
                               "Ставка выросла до 14,8%."])
        for text in got:
            self.assertNotIn("===", text, "метка утекла в текст сообщения")

    def test_чистый_формат_не_сломан(self):
        got = self.mod.parse("===0===\nПервый.\n===1===\nВторой.", 2)
        self.assertEqual(got, ["Первый.", "Второй."])

    def test_unparsable_answer_is_not_retried(self):
        with self._urlopen(message("просто текст без разметки"), message(ANSWER)):
            self.assertIsNone(self.mod.comments(self.events(), self.panel(), log=self.log.append))
        self.assertEqual(len(self.calls), 1, "повтор разборчивого запроса — лишние деньги")

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

    def test_partial_answer_keeps_what_is_good(self):
        half = "===0===\n" + "Длинные ОФЗ дорожают первыми — обычный порядок в начале цикла снижения ставки.\n"
        with self._urlopen(message(half)):
            got = self.mod.comments(self.events(), self.panel())
        self.assertIn("ОФЗ", got[0])
        self.assertIsNone(got[1], "второе событие остаётся без комментария, а не с чужим")


class TestAnnotate(CommentaryCase):
    def test_ops_events_never_get_a_comment(self):
        # Санитарные уходят в ops-канал: разбор рынка там не к месту и стоит денег.
        evs = [{"kind": "source_stale", "text": "Источник iss: error."},
               {"kind": "core_flip", "text": "Ядро развернулось: +0.66."}]
        with self._urlopen(message("===0===\nРазбор одного события про разворот ядра сюда.\n")):
            filled = self.mod.annotate(evs, self.panel())
        self.assertEqual(filled, 1)
        self.assertNotIn("comment", evs[0])
        self.assertIn("разворот", evs[1]["comment"])
        user = json.loads(self.calls[0]["body"]["messages"][0]["content"])
        self.assertEqual(len(user["events"]), 1)

    def test_already_commented_event_is_not_asked_again(self):
        # Событие из очереди повторов уже несёт свой разбор — второй вызов был бы
        # лишними деньгами и лишним риском получить другой текст.
        evs = [{"kind": "core_flip", "text": "Ядро развернулось.", "comment": "уже есть"}]
        with self._urlopen(message(ANSWER)):
            self.assertEqual(self.mod.annotate(evs, self.panel()), 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(evs[0]["comment"], "уже есть")


if __name__ == "__main__":
    unittest.main()
