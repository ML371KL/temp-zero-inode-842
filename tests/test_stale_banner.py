"""Баннер «Данные устарели» обязан отличать поломку от расписания.

ЧТО СЛОМАЛОСЬ В ПРОДЕ (13–14.08.2026). Норма свежести стояла плоская: 150 минут
круглые сутки. А конвейер по расписанию МОЛЧИТ десять часов — последний такт в
21:00 UTC (00:00 МСК), первый следующий в 07:00 (10:00 МСК). Значит каждую ночь,
с половины третьего до десяти утра по Москве, панель писала «Данные устарели.
Числа на экране — последние успешные, а не сегодняшние» — при том что числа были
свежайшие из возможных: биржа закрыта, меняться нечему. Владелец увидел этот
баннер в 05:48 МСК и пошёл искать поломку, которой не было.

Тревога, которая горит треть суток, перестаёт что-либо значить — та же болезнь,
от которой в pipeline/alerts.py лечатся «переходами вместо состояний».

РЕШЕНИЕ, которое здесь закрепляется: витрина публикует ОБЕЩАНИЕ `next_publish_at`
(«очередная публикация ждётся не позже»), и баннер загорается, когда обещание
нарушено дольше `stale_grace_minutes`. Мёртвый конвейер нового обещания не
выпустит, поэтому старое протухнет само — способность ловить настоящий отказ
не теряется, а внутри сессии становится ОСТРЕЕ: полчаса вместо двух с половиной.

Функция вырезается из `web/app.js` и исполняется настоящим node: текстовая
проверка сказала бы, что строка «правильная», а не что она РАБОТАЕТ.
"""

import json
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "web" / "app.js"

# Все метки фиксированные: «сейчас» задаётся подменой Date.now внутри harness,
# поэтому проверка не зависит от календаря машины (правило 1 набора).
NIGHT = "2026-08-14T02:53:00Z"      # 05:53 МСК, тишина по расписанию
MORNING = "2026-08-14T07:45:00Z"    # 10:45 МСК, сессия идёт
LAST_TICK = "2026-08-13T21:00:05Z"  # закрывающий такт предыдущего дня
DUE_MORNING = "2026-08-14T07:00:00Z"


def _slice_function(source, name):
    """Тело функции по балансу фигурных скобок."""
    start = source.index("function " + name)
    depth, i = 0, source.index("{", start)
    while True:
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start:i + 1]
        i += 1


class LatenessCase(unittest.TestCase):
    def setUp(self):
        if not shutil.which("node"):
            self.skipTest("нет node — проверка исполняет функцию фронта, а не читает её")
        source = APP.read_text(encoding="utf-8")
        self.harness = "\n".join([
            "function isNum(v){return typeof v === 'number' && isFinite(v);}",
            _slice_function(source, "ageMinutes"),
            _slice_function(source, "publishLateness"),
        ])

    def ask(self, payload, now):
        """Значение publishLateness при заданном «сейчас»."""
        script = (
            "const NOW = Date.parse(%s);\n" % json.dumps(now)
            + "Date.now = () => NOW;\n"
            + self.harness
            + "\nconst r = publishLateness(%s);\n" % json.dumps(payload, ensure_ascii=False)
            + "console.log(JSON.stringify(r && {late: r.late, byMin: Math.round(r.byMin),"
              " dueAt: r.dueAt ? r.dueAt.toISOString() : null}));"
        )
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "harness.js"
            path.write_text(script, encoding="utf-8")
            out = subprocess.run(["node", str(path)], capture_output=True, text=True,
                                 encoding="utf-8", timeout=60)
            self.assertEqual(out.returncode, 0, out.stderr)
            return json.loads(out.stdout.strip())

    def payload(self, **kw):
        base = {"generated_at": LAST_TICK, "next_publish_at": DUE_MORNING,
                "stale_grace_minutes": 30, "stale_after_minutes": 150}
        base.update(kw)
        return base

    # --------------------------------------------------------------- расписание
    def test_ночная_тишина_не_тревога(self):
        """ГЛАВНОЕ: 05:53 МСК, публикации почти шесть часов — и это норма."""
        got = self.ask(self.payload(), NIGHT)
        self.assertFalse(got["late"], "ночная тишина по расписанию снова читается как поломка")
        self.assertEqual(got["dueAt"], "2026-08-14T07:00:00.000Z")

    def test_плоская_норма_на_этих_же_данных_кричала_бы(self):
        # Ровно то, что было в проде: возраст 353 мин против нормы 150.
        got = self.ask(self.payload(next_publish_at=None), NIGHT)
        self.assertTrue(got["late"], "запасная ветка обязана остаться прежней")
        self.assertIsNone(got["dueAt"])

    # ------------------------------------------------------------- настоящий отказ
    def test_нарушенное_обещание_это_тревога(self):
        """Утро, такт ждали в 07:00, витрина всё ещё вчерашняя — вот это поломка."""
        got = self.ask(self.payload(), MORNING)
        self.assertTrue(got["late"])
        self.assertEqual(got["byMin"], 45)

    def test_внутри_допуска_молчим(self):
        # 07:20 — обещание нарушено на 20 минут при допуске 30: одиночные пропуски
        # такта (моргание сети, ожидание замка) тревогой не считаются.
        got = self.ask(self.payload(), "2026-08-14T07:20:00Z")
        self.assertFalse(got["late"])
        self.assertEqual(got["byMin"], 20)

    def test_внутри_сессии_реакция_острее_прежней(self):
        # Такт каждые 5 минут: обещание на 12:05, к 12:40 пропущено семь подряд.
        # Плоская норма молчала бы до 14:30 — здесь тревога уже в 12:35.
        payload = self.payload(generated_at="2026-08-14T12:00:04Z",
                               next_publish_at="2026-08-14T12:05:00Z")
        self.assertFalse(self.ask(payload, "2026-08-14T12:30:00Z")["late"])
        self.assertTrue(self.ask(payload, "2026-08-14T12:40:00Z")["late"])

    def test_битое_обещание_откатывает_на_плоскую_норму(self):
        for junk in ("", "не дата", None):
            with self.subTest(value=junk):
                got = self.ask(self.payload(next_publish_at=junk), NIGHT)
                self.assertIsNone(got["dueAt"], "мусор не имеет права стать сроком")
                self.assertTrue(got["late"])


class PayloadPromiseCase(unittest.TestCase):
    """Обещание в витрине должно совпадать с расписанием настоящих таймеров."""

    def setUp(self):
        from tests import need
        self.sched = need(self, "pipeline.lib.schedule", "next_publish_at", "starts_of_day")

    def test_ночью_обещание_указывает_на_утренний_такт(self):
        from datetime import datetime, timezone
        got = self.sched.next_publish_at(datetime(2026, 8, 14, 2, 53, tzinfo=timezone.utc))
        self.assertEqual(got.strftime("%Y-%m-%dT%H:%M:%SZ"), DUE_MORNING)

    def test_в_сессии_обещание_это_ближайший_пятиминутный_такт(self):
        from datetime import datetime, timezone
        got = self.sched.next_publish_at(datetime(2026, 8, 14, 12, 1, tzinfo=timezone.utc))
        self.assertEqual(got.strftime("%H:%M"), "12:05")

    def test_в_выходные_обещание_не_ссылается_на_будний_такт(self):
        # Витринный такт — Mon..Fri; в субботу публикуют только месячный и ручной.
        from datetime import datetime, timezone
        got = self.sched.next_publish_at(datetime(2026, 8, 15, 10, 0, tzinfo=timezone.utc))
        self.assertEqual(got.strftime("%a %H:%M"), "Sat 15:00")

    def test_реколибровка_не_считается_публикацией(self):
        # Она ничего не пишет в data.json: ожидание обновления от неё никогда бы
        # не сбылось, и баннер загорался бы каждое 5-е число.
        lines = self.sched.publishing_calendars(str(ROOT / "ops"))
        self.assertTrue(lines)
        self.assertFalse([l for l in lines if l.startswith("*-*-05")],
                         "такт реколибровки попал в расписание публикаций")


if __name__ == "__main__":
    unittest.main()
