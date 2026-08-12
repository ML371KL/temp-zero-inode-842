"""Два канала доставки: рынок и отказы обвязки.

ПОЧЕМУ ЭТОТ ФАЙЛ. Разделение каналов проверялось только через `tests/test_alerts.py`,
где `telegram.send` подменён целиком: `config()`, `CHANNELS` и сборка запроса не
исполнялись тестами ни разу. Зелёными проходили подмена ops-бота на рыночный
(санитарные события уезжают в канал рынка — ровно то, ради чего каналы разводили),
слияние каталогов маркеров и подмена `rglob` на `glob` в чистке (маркеры ops-канала
копились бы вечно). Аудит 13.08.2026.

В сеть не ходим: подменяется `urlopen` внутри модуля, поэтому собранный адрес,
`chat_id` и тело запроса проверяются такими, какими их увидел бы Telegram.
"""

import json
import os
import time
import unittest
import urllib.error
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from tests import need


class FakeResponse:
    def __init__(self, ok=True, description=None):
        self._body = json.dumps({"ok": ok, "description": description}).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


class TelegramCase(unittest.TestCase):
    def setUp(self):
        self.tg = need(self, "pipeline.lib.telegram", "deliver", "send", "config",
                       "CHANNELS", "SENT", "DUP", "FAIL", "OFF",
                       "prune_markers", "LONG_LIVED_DAYS")
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        keys = ("STATE_DIR", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
                "ERROR_BOT_TOKEN", "ERROR_CHAT_ID")
        self.prev = {k: os.environ.get(k) for k in keys}
        self.addCleanup(self._restore)
        os.environ.update(STATE_DIR=self.tmp.name,
                          TELEGRAM_BOT_TOKEN="market-token", TELEGRAM_CHAT_ID="-100market",
                          ERROR_BOT_TOKEN="ops-token", ERROR_CHAT_ID="-200ops")
        self.calls = []
        self.fail_next = False

        def fake(req, timeout=None):
            self.calls.append({"url": req.full_url,
                               "body": json.loads(req.data.decode("utf-8"))})
            if self.fail_next:
                raise urllib.error.URLError("телеграм лежит")
            return FakeResponse()

        patcher = mock.patch.object(self.tg.urllib.request, "urlopen", fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _restore(self):
        for key, val in self.prev.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    def markers(self):
        return sorted(str(p.relative_to(self.root)).replace("\\", "/")
                      for p in (self.root / "notify").rglob("*.json"))


class TestChannels(TelegramCase):
    def test_каналы_идут_разными_ботами_в_разные_чаты(self):
        # мутация: подставить ops-каналу рыночного бота -> санитарные события
        # уезжают в канал рынка, и разделение перестаёт существовать.
        self.tg.deliver("k1", "рыночное", channel="alerts")
        self.tg.deliver("k2", "санитарное", channel="ops")
        self.assertIn("market-token", self.calls[0]["url"])
        self.assertEqual(self.calls[0]["body"]["chat_id"], "-100market")
        self.assertIn("ops-token", self.calls[1]["url"])
        self.assertEqual(self.calls[1]["body"]["chat_id"], "-200ops")

    def test_неизвестный_канал_падает_на_рыночный(self):
        self.assertEqual(self.tg.config("такого-нет"), self.tg.config("alerts"))

    def test_канал_без_настроек_отвечает_off_а_не_fail(self):
        # OFF и FAIL расходятся судьбой события: FAIL кладётся в очередь повторов и
        # повторяется каждый прогон, OFF — закрывается. Канал без переменных сам не
        # появится, поэтому FAIL здесь означал вечную очередь на машине без токена.
        os.environ.pop("ERROR_BOT_TOKEN")
        self.assertIsNone(self.tg.config("ops"))
        self.assertEqual(self.tg.deliver("k", "текст", channel="ops"), self.tg.OFF)
        self.assertEqual(self.calls, [], "в сеть не ходим, если слать нечем")
        self.assertEqual(self.markers(), [], "маркер ставит только успешная отправка")

    def test_сетевой_отказ_остаётся_fail(self):
        # Мутация «возвращать OFF на любую неудачу» съела бы повторы: настроенный
        # канал, упавший на минуту, обязан отдать событие обратно в очередь.
        self.fail_next = True
        self.assertEqual(self.tg.deliver("k", "текст", channel="ops"), self.tg.FAIL)

    def test_ошибка_в_тексте_называет_переменные_нужного_канала(self):
        os.environ.pop("ERROR_CHAT_ID")
        ok, err = self.tg.send("текст", channel="ops")
        self.assertFalse(ok)
        self.assertIn("ERROR_BOT_TOKEN", err)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", err)


class TestMarkers(TelegramCase):
    def test_один_ключ_в_разных_каналах_не_гасит_себя(self):
        # мутация: общий каталог маркеров -> санитарное событие с тем же ключом,
        # что рыночное, молча не уйдёт вовсе.
        self.assertEqual(self.tg.deliver("одинаковый", "рынок", channel="alerts"), self.tg.SENT)
        self.assertEqual(self.tg.deliver("одинаковый", "отказ", channel="ops"), self.tg.SENT)
        self.assertEqual(len(self.calls), 2)
        paths = self.markers()
        self.assertEqual(len(paths), 2, paths)
        self.assertTrue(any(p.startswith("notify/ops/") for p in paths), paths)
        self.assertTrue(any(p.count("/") == 1 for p in paths), paths)
        # Имя файла у обоих одно — разводит их именно каталог канала.
        self.assertEqual(len({p.rsplit("/", 1)[-1] for p in paths}), 1, paths)

    def test_повтор_в_том_же_канале_гасится(self):
        self.assertEqual(self.tg.deliver("k", "текст", channel="ops"), self.tg.SENT)
        self.assertEqual(self.tg.deliver("k", "текст", channel="ops"), self.tg.DUP)
        self.assertEqual(len(self.calls), 1)

    def test_недоставленное_не_ставит_маркер(self):
        # Маркер двигает только успех: иначе после сетевого сбоя событие считается
        # отправленным и пропадает навсегда.
        self.fail_next = True
        self.assertEqual(self.tg.deliver("k", "текст", channel="ops"), self.tg.FAIL)
        self.assertEqual(self.markers(), [])

    def test_маркер_реколибровки_переживает_обычную_чистку(self):
        """Отчёт §7 обещает «пока состав проблем тот же — тишина». Держит это обещание
        ровно маркер, а обычный срок маркера — 45 суток при месячном отчёте.

        Считалось: находка не повторится. Было бы: маркер от 5 сентября исчезает к
        20 октября, и 5 ноября та же самая находка уходит заново — «состояние вместо
        перехода», против чего построен весь алертинг панели. Находка живёт месяцами
        по построению: пока константы не пересчитаны, расхождение ячейки верно и
        через полгода.
        """
        self.tg.deliver("recalibrate:abc123", "есть расхождения", channel="ops")
        self.tg.deliver("source_stale:iss", "источник отстал", channel="ops")
        self.assertEqual(len(self.markers()), 2)

        old = time.time() - 100 * 86400  # больше 45 суток, но меньше срока долгожителя
        for path in (self.root / "notify").rglob("*.json"):
            os.utime(path, (old, old))
        self.assertEqual(self.tg.prune_markers(days=45), 1, "вычищена не одна запись")
        left = self.markers()
        self.assertEqual(len(left), 1, left)
        key = json.loads((self.root / left[0]).read_text(encoding="utf-8"))["key"]
        self.assertEqual(key, "recalibrate:abc123")

        # Но не вечно: через год уходит и он — иначе каталог растёт без конца.
        forever = time.time() - (self.tg.LONG_LIVED_DAYS + 10) * 86400
        os.utime(self.root / left[0], (forever, forever))
        self.assertEqual(self.tg.prune_markers(days=45), 1)
        self.assertEqual(self.markers(), [])

    def test_чистка_видит_маркеры_обоих_каналов(self):
        # мутация: rglob -> glob, и каталог ops-канала растёт вечно.
        self.tg.deliver("a", "текст", channel="alerts")
        self.tg.deliver("b", "текст", channel="ops")
        old = time.time() - 100 * 86400
        for path in (self.root / "notify").rglob("*.json"):
            os.utime(path, (old, old))
        self.assertEqual(self.tg.prune_markers(days=45), 2)
        self.assertEqual(self.markers(), [])


if __name__ == "__main__":
    unittest.main()
