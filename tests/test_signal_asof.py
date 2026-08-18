"""Карточка активного сигнала подписывает число ЕГО датой, а не словом «сейчас».

Часть рядов второго слоя структурно отстаёт: позиция физлиц (futoi) без подписки —
до двух недель, плюс протяжка до трёх торговых дней. Пайплайн кладёт в каждый
сигнал asof и lag_days ровно для подписи (CONTRACT §3: «фронт обязан показать
дату, при большом lag_days — бейджем»), но до 18.08.2026 карточка печатала
«сейчас +0,57» — читатель принимал двухнедельную позицию за сегодняшнюю.

Разметка карточки строится DOM-хелпером, поэтому проверяем двумя срезами:
дата-функция вырезается и исполняется настоящим node, а сборка карточки — по
источнику: слова «сейчас» рядом со значением сигнала больше нет, asof/lag_days
читаются.
"""

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "web" / "app.js"


def _slice_function(source, name):
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


class SignalAsofCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if cls.node is None:
            raise unittest.SkipTest("node не установлен")
        cls.source = APP.read_text(encoding="utf-8")

    def run_js(self, body):
        code = _slice_function(self.source, "ruDay") + "\n" + body
        out = subprocess.run([self.node, "-e", code], capture_output=True,
                             text=True, encoding="utf-8", timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.strip()

    def test_дата_наблюдения_в_формате_панели(self):
        self.assertEqual(self.run_js("console.log(ruDay('2026-08-04'))"), "04.08")

    def test_мусор_не_прикидывается_датой(self):
        # Неразобранное значение честно показывается как есть, а не падает.
        self.assertEqual(self.run_js("console.log(ruDay('n/a'))"), "n/a")
        self.assertEqual(self.run_js("console.log(ruDay(null))"), "")

    def test_карточка_сигнала_несёт_дату_и_бейдж_отставания(self):
        # Сборка карточки: значение подписывается asof, большое отставание —
        # бейджем. Слову «сейчас» рядом со значением сигнала места нет.
        sig_block = self.source[self.source.index("active_signals || []"):]
        sig_block = sig_block[:sig_block.index("sig__why")]
        self.assertIn("ruDay(s.asof)", sig_block,
                      "значение сигнала потеряло подпись датой")
        self.assertIn("s.lag_days", sig_block, "бейдж отставания исчез")
        # «сейчас» ищем только в КОДЕ: комментарий, объясняющий, почему слова нет,
        # сам содержит это слово.
        code_only = "\n".join(line for line in sig_block.splitlines()
                              if not line.strip().startswith("//"))
        self.assertNotIn("сейчас", code_only,
                         "отстающее число снова подписано словом «сейчас»")


if __name__ == "__main__":
    unittest.main()
