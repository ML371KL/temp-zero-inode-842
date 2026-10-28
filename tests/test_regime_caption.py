"""Подпись таблицы режима в шапке: сколько сочетаний сведено вместе.

До 28.10.2026 подпись была одна на все режимы — «Следующий месяц в этом режиме», —
а строка над таблицей называла одно сочетание («медведь · спокойно · стресс»).
Числа под подписью — режим целиком (states._regime_stats сводит все его сочетания:
у «стресса» их пять, 112 месяцев), и +1,59% читалось как статистика одного
сочетания, хотя у него в таблице восьми сочетаний +0,51% и 25 наблюдений.

Проверяется ровно подпись: пять и два сочетания — число со склонением; одно
сочетание или нет списка (старый data.json) — прежние слова, без падения. Функция
вырезается из `web/app.js` и исполняется настоящим node — как в
tests/test_switch_rows.py.
"""

import json
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.test_switch_rows import _slice_function

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "web" / "app.js"

BASE = "Следующий месяц в этом режиме"
STRESS = ["bull|stress|ok", "bear|stress|ok", "bull|stress|stress",
          "bull|calm|stress", "bear|calm|stress"]


class RegimeCaptionCase(unittest.TestCase):
    def setUp(self):
        if not shutil.which("node"):
            self.skipTest("нет node — проверка исполняет функцию фронта, а не читает её")
        app = APP.read_text(encoding="utf-8")
        self.harness = "\n".join([
            _slice_function(app, "plural"),
            _slice_function(app, "regimeCaption"),
        ])

    def ask(self, arg_js):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "harness.js"
            path.write_text(self.harness + "\nconsole.log(JSON.stringify(regimeCaption(%s)));" % arg_js,
                            encoding="utf-8")
            out = subprocess.run(["node", str(path)], capture_output=True, timeout=60)
            if out.returncode:
                self.fail("node не исполнил функцию: " + out.stderr.decode("utf-8", "replace")[:300])
            return json.loads(out.stdout.decode("utf-8"))

    def test_stress_names_five_combinations(self):
        """«Стресс, ворота открыты» — пять сочетаний, и подпись это говорит."""
        self.assertEqual(self.ask(json.dumps(STRESS)), BASE + " — 5 сочетаний вместе")

    def test_calm_two_combinations_declined(self):
        """Два сочетания — «2 сочетания»: склонение под число, а не «2 сочетаний»."""
        self.assertEqual(self.ask(json.dumps(["bull|calm|ok", "bear|calm|ok"])),
                         BASE + " — 2 сочетания вместе")

    def test_single_combination_keeps_old_words(self):
        """«Ворота закрыты» — одно сочетание: сводить нечего, подпись прежняя."""
        self.assertEqual(self.ask(json.dumps(["bear|stress|stress"])), BASE)

    def test_missing_list_keeps_old_words(self):
        """Старый data.json без списка сочетаний — прежняя подпись, не падение."""
        for arg in ("undefined", "null", "[]", "5"):
            with self.subTest(arg=arg):
                self.assertEqual(self.ask(arg), BASE)


if __name__ == "__main__":
    unittest.main()
