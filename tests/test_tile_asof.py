"""Подпись «данные: …» под тайлом монитора.

Ярлык обязан означать одно и то же во всей сетке: ДАТУ НАБЛЮДЕНИЯ. Поэтому дату
события из будущего (заседание ЦБ через месяц) в него не пускают — иначе свежесть
такого тайла не может протухнуть по определению.

Что здесь проверяется — ровно то, что уже ломалось в проде 12.08.2026:

1. **горизонт брался из `asof_trading_day`.** Ядро и состояния считаются по
   ЗАКРЫТИЮ, поэтому внутри дня торговый день вчерашний — и СЕГОДНЯШНЯЯ дата
   данных отвергалась как «будущая». Четыре тайла из шестнадцати показывали
   «нет данных» при совершенно исправных числах: дивидендный календарь,
   вероятность перемирия, позиции физлиц, бюджетный узел;
2. **защита от даты события** при этом обязана остаться: заседание ЦБ через месяц
   в подпись по-прежнему не идёт, вместо него берётся дата данных из payload.

Функция вырезается из `web/app.js` и исполняется настоящим node: текстовая
проверка регуляркой сказала бы, что строка «правильная», а не что она РАБОТАЕТ.
"""

import json
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "web" / "app.js"

TODAY = "2026-08-12"          # день сборки витрины
TRADING_DAY = "2026-08-11"    # последнее закрытие — по определению вчерашнее
MEETING = "2026-09-11"        # дата события в будущем


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


class TileAsofCase(unittest.TestCase):
    def setUp(self):
        if not shutil.which("node"):
            self.skipTest("нет node — проверка исполняет функцию фронта, а не читает её")
        source = APP.read_text(encoding="utf-8")
        keys = next(line for line in source.splitlines() if "DATA_ASOF_KEYS = [" in line)
        self.harness = "\n".join([
            keys.strip().rstrip(";") + ";",
            # Заглушки формата: проверяем ВЫБОР даты, а не то, как её печатают.
            "function fmtDay(s){return s;}",
            "function fmtMon(s){return s;}",
            _slice_function(source, "tileAsof"),
        ])

    def ask(self, tile, panel):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "harness.js"
            path.write_text(
                self.harness + "\nconsole.log(JSON.stringify(tileAsof(%s, %s)));"
                % (json.dumps(tile, ensure_ascii=False), json.dumps(panel, ensure_ascii=False)),
                encoding="utf-8")
            out = subprocess.run(["node", str(path)], capture_output=True, timeout=60)
            if out.returncode:
                self.fail("node не исполнил функцию: " + out.stderr.decode("utf-8", "replace")[:300])
            return json.loads(out.stdout.decode("utf-8"))

    def panel(self):
        return {"asof_trading_day": TRADING_DAY, "generated_at": TODAY + "T10:47:17Z"}

    def test_сегодняшние_данные_показываются(self):
        # мутация: сравнивать с asof_trading_day -> «нет данных» у четырёх тайлов
        # со свежайшими числами (боевой случай 12.08.2026).
        self.assertEqual(self.ask({"asof": TODAY, "payload": {}}, self.panel()),
                         "данные: " + TODAY)

    def test_вчерашние_данные_показываются(self):
        self.assertEqual(self.ask({"asof": TRADING_DAY, "payload": {}}, self.panel()),
                         "данные: " + TRADING_DAY)

    def test_дата_события_из_будущего_не_показывается(self):
        # Тайл заседания ЦБ: в asof дата БУДУЩЕГО заседания, дат данных в payload нет.
        self.assertEqual(self.ask({"asof": MEETING, "payload": {}}, self.panel()),
                         "нет данных")

    def test_из_будущего_берётся_дата_данных_из_payload(self):
        self.assertEqual(
            self.ask({"asof": MEETING, "payload": {"key_rate_asof": TRADING_DAY}}, self.panel()),
            "данные: " + TRADING_DAY)

    def test_без_даты_вовсе_молчим(self):
        self.assertEqual(self.ask({"payload": {}}, self.panel()), "нет данных")


if __name__ == "__main__":
    unittest.main()
