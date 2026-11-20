"""Прогон после аудита 20.11.2026: блок sources, падение расчёта, порядок доставки.

Три находки отчёта аудита (раздел 9, пункты 7 и 8):

* тревога об источнике и плашка на странице стояли на статусе СЕМЬИ, а он — статус
  худшего ряда: монитор заслонял ряд решения. Блок sources теперь называет плохие
  ряды и говорит, затронуты ли ряды решения (`core_affected`);
* упавший расчёт публиковал прежний вердикт со свежим временем и молчал. Теперь в
  витрине поле `compute_error`, а в канале отказов — тревога;
* сообщение уходило раньше публикации страницы: ссылка из него вела на страницу с
  прежней позицией.

В сеть не ходим: бакет не настроен (публикация пишет только локальную копию),
телеграм подменён, часы прогона заморожены.
"""

import json
import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from tests import need
from tests.test_alerts import payload, position_block

UTC = timezone.utc
NOW = datetime(2026, 8, 11, 20, 55, 0, tzinfo=UTC)      # вторник, 23:55 МСК
FETCHED = "2026-08-11T20:54:00Z"
ENV = ("STATE_DIR", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ERROR_BOT_TOKEN",
       "ERROR_CHAT_ID", "NEXUS_EVENTS_URL", "NEXUS_INGEST_TOKEN", "ANTHROPIC_API_KEY",
       "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET",
       "RADAR_FETCH_BUDGET_S", "LLM_DEADLINE_AT")


class _Clock(datetime):
    """Часы прогона: main() берёт «сейчас» сам, тесту нужен фиксированный момент."""

    @classmethod
    def now(cls, tz=None):
        return NOW if tz else NOW.replace(tzinfo=None)


class TestSourcesNameBadSeries(unittest.TestCase):
    """Блок sources: какие ряды плохи и затронуты ли ряды решения."""

    def setUp(self):
        self.run = need(self, "pipeline.run", "_sources_fallback")

    def sources(self, specs):
        """specs: {ряд: (семья, роль, обязательный, статус | None)}."""
        fake = {sid: {"fetcher": f"{fam}.x", "role": role, "required": req,
                      "label": f"Подпись {sid}"}
                for sid, (fam, role, req, _st) in specs.items()}
        status = {sid: st for sid, (_f, _r, _q, st) in specs.items()}

        def points(_store, cand):
            st = status.get(cand)
            if st is None:
                return [], {}
            return ([("2026-08-11", 1.0)] if st != "missing" else [],
                    {"fetched_at": FETCHED, "asof": "2026-08-11", "status": st})

        with mock.patch.object(self.run.registry, "SERIES", fake), \
             mock.patch.object(self.run.store, "list_series", return_value=list(specs)), \
             mock.patch.object(self.run.monitors_mod, "series_points", points), \
             mock.patch.object(self.run.monitors_mod, "series_status",
                               lambda cand, pts, meta, now: status.get(cand) or "missing"):
            return self.run._sources_fallback(NOW)

    def test_монитор_плох_ряды_решения_целы(self):
        """Положение дел с 19.11.2026: семья биржи устарела из-за доходности ВДО.

        мутация: не класть core_affected -> страница не может отличить «отстал
        монитор» от «отстал ряд, на котором стоит позиция».
        """
        got = self.sources({"imoex": ("iss", "core", True, "ok"),
                            "zcyc": ("iss", "core", True, "ok"),
                            "vdo": ("iss", "monitor", False, "stale")})["iss"]
        self.assertEqual(got["status"], "stale")            # прежнее поле не тронуто
        self.assertEqual(got["series"], "vdo")
        self.assertIs(got["core_affected"], False)
        self.assertEqual([(b["series"], b["status"], b["core"]) for b in got["bad_series"]],
                         [("vdo", "stale", False)])
        self.assertEqual({k: v["status"] for k, v in got["core_series"].items()},
                         {"imoex": "ok", "zcyc": "ok"})
        self.assertEqual(got["core_series"]["zcyc"]["label"], "Подпись zcyc")

    def test_ряд_решения_плох(self):
        got = self.sources({"imoex": ("iss", "core", True, "ok"),
                            "zcyc": ("iss", "core", True, "error"),
                            "vdo": ("iss", "monitor", False, "stale")})["iss"]
        self.assertIs(got["core_affected"], True)
        self.assertEqual(sorted((b["series"], b["core"]) for b in got["bad_series"]),
                         [("vdo", False), ("zcyc", True)])

    def test_здоровая_семья(self):
        got = self.sources({"imoex": ("iss", "core", True, "ok")})["iss"]
        self.assertEqual(got["bad_series"], [])
        self.assertIs(got["core_affected"], False)

    def test_ядро_без_пометки_обязательный_тоже_ряд_решения(self):
        # Налоговая цена нефти: роль «ядро», обязательной не помечена — а позицию
        # 06.11.2026 решила она. Сигнал без пометки «обязательный» рядом решения не
        # считается.
        got = self.sources({"urals": ("minfin", "core", False, "stale"),
                            "deficit": ("minfin", "monitor", False, "ok"),
                            "brent": ("minfin", "signal", False, "error")})["minfin"]
        self.assertEqual(list(got["core_series"]), ["urals"])
        self.assertIs(got["core_affected"], True)

    def test_ряд_решения_которого_нет_в_хранилище_виден(self):
        # мутация: пропускать пустой ряд, как пропускается пустой монитор -> дыра
        # под позицией не видна ни странице, ни тревоге.
        got = self.sources({"imoex": ("iss", "core", True, None),
                            "vdo": ("iss", "monitor", False, "ok")})["iss"]
        self.assertEqual(got["status"], "ok", "статус семьи пустой ряд не определяет")
        self.assertEqual(got["core_series"]["imoex"]["status"], "missing")
        self.assertIs(got["core_affected"], True)

    def test_строка_семьи_сериализуется(self):
        # мутация: класть в bad_series саму строку семьи -> циклическая ссылка,
        # публикация падает на json.dumps.
        got = self.sources({"zcyc": ("iss", "core", True, "error")})
        json.dumps(got)


class MainCase(unittest.TestCase):
    """main() целиком: расчёт подменён, бакет не настроен, телеграм записывается."""

    def setUp(self):
        self.run = need(self, "pipeline.run", "main", "build_payload_for_mode")
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prev_env = {k: os.environ.get(k) for k in ENV}
        self.addCleanup(self._restore_env)
        for key in ENV:
            os.environ.pop(key, None)
        os.environ.update(STATE_DIR=self.tmp.name, TELEGRAM_BOT_TOKEN="тест",
                          TELEGRAM_CHAT_ID="-100", ERROR_BOT_TOKEN="тест-ops",
                          ERROR_CHAT_ID="-200")
        self.sent = []          # (канал, текст, что лежало на странице в момент отправки)
        # Незамоканный поход в сеть — провал теста, а не тихий запрос наружу.
        net = mock.patch("urllib.request.urlopen",
                         side_effect=AssertionError("тест пошёл в сеть"))
        net.start()
        self.addCleanup(net.stop)
        for target, name, value in (
                (self.run, "datetime", _Clock),
                (self.run.telegram, "send", self._send),
                (self.run.commentary, "ops_tick", lambda *a, **k: None),
                # Интрадей-режим сам ходит к бирже за живой ценой — в наборе это запрещено.
                (self.run, "fetch_live_quotes", lambda journal, now: None),
                (self.run, "_monitors", lambda now, journal: []),
                (self.run, "_sources", lambda now, journal: {}),
                (self.run, "_quotes", lambda now: {})):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _restore_env(self):
        for key, val in self.prev_env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    def _send(self, text, silent=False, retries=2, channel="alerts"):
        self.sent.append((channel, text, (self.page() or {}).get("verdict", {})
                          .get("position", {}).get("state")))
        return True, None

    def page(self):
        try:
            return json.loads((Path(self.tmp.name) / "out" / "data.json")
                              .read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def publish_first(self, position="long"):
        """Прошлая витрина на диске — как после вчерашнего удачного прогона."""
        self.go(payload(position=position_block(position, by_score=True)))
        self.sent.clear()

    def go(self, built, mode="daily"):
        """Прогон с подменённым расчётом: built — готовая витрина или исключение."""
        def build(_mode, _now, _journal):
            if isinstance(built, Exception):
                raise built
            return dict(built)
        with mock.patch.object(self.run, "build_payload_for_mode", build):
            return self.run.main(["--mode", mode, "--only", "нет_такого_ряда"])


class TestComputeCrash(MainCase):

    def test_падение_расчёта_помечено_в_витрине_и_уходит_тревогой(self):
        """мутация: убрать пометку -> вчерашний вердикт публикуется со свежим
        временем и ничем не отличается от сегодняшнего; убрать crash из prepare ->
        тревога приходит без текста ошибки.
        """
        self.publish_first("flat")
        self.assertIsNone(self.page()["compute_error"], "у удачного прогона поле пустое")
        self.go(KeyError("нет_столбца"))
        page = self.page()
        self.assertEqual(page["compute_error"], "KeyError @ 2026-08-11T20:55:00Z")
        self.assertEqual(page["verdict"]["position"]["state"], "flat", "вердикт — прежний")
        # В публичной витрине — только тип и время, без текста исключения.
        self.assertNotIn("нет_столбца", json.dumps(page, ensure_ascii=False))
        ops = [t for ch, t, _ in self.sent if ch == "ops"]
        self.assertEqual(len(ops), 1, self.sent)
        self.assertIn("Расчёт упал: на странице прежний вердикт", ops[0])
        self.assertIn("KeyError: 'нет_столбца'", ops[0])
        self.assertEqual([t for ch, t, _ in self.sent if ch == "alerts"], [])

    def test_удачный_расчёт_снимает_пометку(self):
        self.publish_first("flat")
        self.go(KeyError("нет_столбца"))
        self.sent.clear()
        self.go(payload(position=position_block("flat", by_score=True)))
        self.assertIsNone(self.page()["compute_error"])
        self.assertTrue([t for ch, t, _ in self.sent
                         if ch == "ops" and "Расчёт снова проходит" in t], self.sent)

    def test_упавший_интрадей_такт_пометку_не_ставит_а_несёт(self):
        # мутация: ставить пометку и на интрадей-сбое -> один сбой такта держит
        # «вердикт прежний» до вечернего расчёта, хотя вердикт и так вчерашний.
        self.publish_first("flat")
        self.go(RuntimeError("тайл"), mode="intraday")
        self.assertIsNone(self.page()["compute_error"])
        self.go(KeyError("нет_столбца"))                    # упал суточный
        self.sent.clear()
        self.go(RuntimeError("тайл"), mode="intraday")     # упавший такт несёт пометку
        self.assertEqual(self.page()["compute_error"], "KeyError @ 2026-08-11T20:55:00Z")

    def test_интрадей_такт_наследует_пометку_прошлого_прогона(self):
        """Настоящий build_payload_for_mode: такт берёт вердикт с диска — и пометку.

        мутация: не переносить поле -> пометка живёт пять минут, до первого такта.
        """
        self.publish_first("flat")
        self.go(KeyError("нет_столбца"))
        code = self.run.main(["--mode", "intraday", "--only", "нет_такого_ряда"])
        self.assertIn(code, (0, 2))
        self.assertEqual(self.page()["compute_error"], "KeyError @ 2026-08-11T20:55:00Z")
        self.assertEqual(self.page()["run_mode"], "intraday")


class TestPageBeforeMessage(MainCase):

    def test_страница_обновлена_до_отправки_сообщения(self):
        """мутация: вернуть отправку до публикации -> в момент отправки на странице
        прежняя позиция, и ссылка из сообщения ведёт на неё.
        """
        self.publish_first("long")
        self.go(payload(position=position_block("flat", by_score=True)))
        market = [(t, seen) for ch, t, seen in self.sent if ch == "alerts"]
        self.assertEqual(len(market), 1, self.sent)
        self.assertIn("Позиция: переход в деньги", market[0][0])
        self.assertEqual(market[0][1], "flat", "сообщение ушло раньше публикации страницы")
        # Событие при этом уже в ленте опубликованной страницы.
        self.assertEqual([e["kind"] for e in self.page()["events"]], ["position_change"])


if __name__ == "__main__":
    unittest.main()
