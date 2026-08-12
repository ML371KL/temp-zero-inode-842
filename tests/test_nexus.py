"""Зеркало событий в ленту хаба NEXUS (контракт §6).

Проверяем ровно то, чем этот канал может тихо испортиться:

* «канал не настроен» обязано отличаться от «не доставили» — иначе локальный
  прогон и dry-run копят вечную очередь повторов;
* заголовок и подпись в ленте не должны совпадать: хаб режет текст по строкам,
  и без разреза свёрнутая лента дублирует сама себя;
* десятичная точка в «+1.4%/мес» — не конец предложения;
* префикс «Внимание.» из alerts.render не имеет права стать заголовком;
* eventId = стабильный ключ события, иначе повтор из pending заводит в ленте
  вторую строку (текст события меняется вместе с числами каждый прогон);
* падение хаба не роняет прогон и не отправляет событие в небытие: оно остаётся
  в очереди, а телеграм на повторе отвечает DUP и второй раз не пишет.

В сеть не ходим: подменяется urlopen внутри модуля.
"""

import json
import os
import unittest
import urllib.error
from unittest import mock

from tests import need


class FakeResponse:
    def __init__(self, status=202):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b'{"accepted":true}'


class NexusCase(unittest.TestCase):
    def setUp(self):
        self.nexus = need(self, "pipeline.lib.nexus", "deliver", "compose", "SENT", "OFF", "FAIL")
        self.prev = {k: os.environ.get(k) for k in ("NEXUS_EVENTS_URL", "NEXUS_INGEST_TOKEN")}
        self.addCleanup(self._restore)
        os.environ.update(NEXUS_EVENTS_URL="https://hub.example/api/events",
                          NEXUS_INGEST_TOKEN="секрет")
        self.calls = []

    def _restore(self):
        for key, val in self.prev.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    def _urlopen(self, status=202, boom=None):
        def _open(req, timeout=None):
            self.calls.append({
                "url": req.full_url,
                "method": req.get_method(),
                "auth": req.get_header("Authorization"),
                "ua": req.get_header("User-agent"),
                "body": json.loads(req.data.decode("utf-8")),
            })
            if boom is not None:
                raise boom
            return FakeResponse(status)
        return mock.patch.object(self.nexus.urllib.request, "urlopen", _open)

    def event(self, text, key="cell:2026-08-12:bear|stress|stress", ts="2026-08-12T09:00:00Z"):
        return {"key": key, "kind": "state_cell_change", "severity": "warn",
                "text": text, "ts": ts}


class TestConfiguration(NexusCase):
    def test_missing_config_is_off_not_failure(self):
        # мутация: возвращать FAIL без настроек -> alerts.dispatch держит событие
        # недоставленным вечно, и очередь повторов растёт на каждой машине без хаба.
        os.environ.pop("NEXUS_EVENTS_URL", None)
        self.assertFalse(self.nexus.configured())
        self.assertEqual(self.nexus.deliver(self.event("Что-то случилось.")), self.nexus.OFF)

    def test_token_without_url_is_off(self):
        os.environ["NEXUS_EVENTS_URL"] = "   "
        self.assertEqual(self.nexus.deliver(self.event("Что-то случилось.")), self.nexus.OFF)

    def test_empty_text_is_off(self):
        self.assertEqual(self.nexus.deliver(self.event("   ")), self.nexus.OFF)


class TestCompose(NexusCase):
    def test_first_sentence_becomes_the_headline(self):
        # мутация: слать текст одной строкой -> хаб берёт подпись из заголовка,
        # и в ленте строка дублируется сама собой.
        text = self.nexus.compose(self.event(
            "Облигационный флаг ВКЛЮЧЁН. RGBI −5.5% от максимума. Покупка просадок отключается."))
        head, tail = text.split("\n", 1)
        self.assertEqual(head, "Облигационный флаг ВКЛЮЧЁН.")
        self.assertIn("Покупка просадок отключается.", tail)

    def test_decimal_point_is_not_a_sentence_break(self):
        # «+1.4%/мес (hit 0.64)» — одна фраза: после десятичной точки пробела нет.
        text = self.nexus.compose(self.event(
            "Облигационный флаг снят. Покупка просадок снова в силе: +1.4%/мес (hit 0.64)."))
        self.assertEqual(text.split("\n")[0], "Облигационный флаг снят.")
        self.assertIn("+1.4%/мес (hit 0.64).", text.split("\n")[1])

    def test_russian_abbreviation_is_not_a_sentence_break(self):
        """«б.п.», «руб.», «нед.» — точка внутри фразы, а не её конец.

        ОПЛАЧЕНО ПРОДОМ: заголовок решения ЦБ в общей ленте хаба обрывался на
        «…СЮРПРИЗ +25 б.п.», а «к консенсусу 15.50%.» уезжало в подпись — обрубок
        посреди самого важного сообщения панели. Отличает конец фразы от сокращения
        только заглавная буква после пробела.
        """
        cases = [
            ("ЦБ: ключевая 15.75% (−25 б.п.) — СЮРПРИЗ +25 б.п. к консенсусу 15.50%.", 1),
            ("Потоки ОРФР: ДУ −37.9 млрд руб. за июль — рекорд оттока. Физлица добирали.", 2),
            ("Недельная инфляция 0.06% против 0.11% нед. назад. Дезинфляция продолжается.", 2),
        ]
        for text, lines in cases:
            with self.subTest(text=text[:40]):
                got = self.nexus.compose(self.event(text))
                self.assertEqual(len(got.split("\n")), lines, got)
                for piece in got.split("\n"):
                    self.assertFalse(piece.endswith(" б.п.") or piece.endswith(" руб.")
                                     or piece.endswith(" нед."),
                                     f"строка оборвана на сокращении: {piece}")

    def test_sentence_after_a_digit_still_splits(self):
        # Обратная сторона правила: цифра начинает предложение не реже заглавной
        # («2008 год был последним таким случаем»), и терять разрез на ней нельзя.
        got = self.nexus.compose(self.event(
            "Композит сменил знак: +0.31. 2008 год был последним таким случаем."))
        self.assertEqual(got.split("\n")[0], "Композит сменил знак: +0.31.")

    def test_comment_travels_to_the_hub_as_its_own_paragraph(self):
        # Разбор обязан уехать в ленту хаба вместе с фактом: иначе на панели
        # комментарий есть, в телеграме есть, а в хабе событие голое.
        ev = self.event("Облигационный флаг снят. Покупка просадок снова в силе.")
        ev["comment"] = "Длинные ОФЗ дорожают первыми — обычный порядок в начале цикла."
        text = self.nexus.compose(ev)
        head, rest = text.split("\n", 1)
        self.assertEqual(head, "Облигационный флаг снят.")
        self.assertTrue(rest.rstrip().endswith("обычный порядок в начале цикла."))
        self.assertIn("\n\n💬 ", text)

    def test_missing_comment_leaves_the_fact_alone(self):
        self.assertNotIn("💬", self.nexus.compose(self.event("Смена ячейки: A → B.")))

    def test_single_sentence_stays_single_line(self):
        text = self.nexus.compose(self.event("Смена ячейки: A → B (токсичная)."))
        self.assertNotIn("\n", text)

    def test_warning_prefix_never_becomes_the_headline(self):
        # alerts.render добавляет «Внимание. » для severity=warn — в ленту уходит
        # ev['text'], иначе заголовком всех тревог станет слово «Внимание.».
        alerts = need(self, "pipeline.alerts", "render")
        ev = self.event("Аукцион ОФЗ провален. Минфин не даёт премию.")
        self.assertTrue(alerts.render(ev).startswith("Внимание. "))
        with self._urlopen():
            self.nexus.deliver(ev)
        self.assertEqual(self.calls[0]["body"]["text"].split("\n")[0], "Аукцион ОФЗ провален.")


class TestDelivery(NexusCase):
    def test_post_carries_source_key_and_time(self):
        with self._urlopen():
            self.assertEqual(self.nexus.deliver(self.event("Смена ячейки: A → B.")),
                             self.nexus.SENT)
        call = self.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], "https://hub.example/api/events")
        self.assertEqual(call["auth"], "Bearer секрет")
        self.assertEqual(call["body"]["source"], "842")
        # eventId — стабильный ключ, а не текст: числа в тексте пересчитываются
        # каждый прогон, и дедуп по тексту заводил бы новую строку на повторе.
        self.assertEqual(call["body"]["eventId"], "cell:2026-08-12:bear|stress|stress")
        self.assertEqual(call["body"]["occurredAt"], "2026-08-12T09:00:00Z")

    def test_request_carries_a_non_default_user_agent(self):
        # ОПЛАЧЕНО ЖИВЫМ ПРОГОНОМ: без своего User-Agent Cloudflare перед воркером
        # отвечает 403 «error code: 1010» и запрос до воркера не доходит вовсе —
        # зеркало возвращает FAIL, а причина в коде ниоткуда не видна.
        with self._urlopen():
            self.nexus.deliver(self.event("Смена ячейки."))
        ua = self.calls[0]["ua"]
        self.assertTrue(ua, "без User-Agent urllib подставит свой, и Cloudflare даст 1010")
        self.assertNotIn("urllib", ua.lower())

    def test_http_error_is_failure_not_exception(self):
        boom = urllib.error.HTTPError("https://hub.example/api/events", 401, "no", {}, None)
        with self._urlopen(boom=boom):
            self.assertEqual(self.nexus.deliver(self.event("Смена ячейки.")), self.nexus.FAIL)

    def test_network_outage_is_failure_not_exception(self):
        with self._urlopen(boom=urllib.error.URLError("хаб недоступен")):
            self.assertEqual(self.nexus.deliver(self.event("Смена ячейки.")), self.nexus.FAIL)

    def test_unexpected_exception_never_escapes(self):
        # Контракт модуля: зеркало не имеет права уронить публикацию панели.
        with self._urlopen(boom=RuntimeError("что угодно")):
            self.assertEqual(self.nexus.deliver(self.event("Смена ячейки.")), self.nexus.FAIL)

    def test_non_2xx_status_is_failure(self):
        with self._urlopen(status=500):
            self.assertEqual(self.nexus.deliver(self.event("Смена ячейки.")), self.nexus.FAIL)


if __name__ == "__main__":
    unittest.main()
