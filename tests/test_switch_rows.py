"""Блок «Что изменит позицию»: строки «показатель · что нужно · сейчас».

До 03.10.2026 блок печатал фразу конвейера целиком — пять строк сплошного текста.
Теперь фронт раскладывает по строкам числа, которые уже лежат в витрине
(`position.flags`, `states.distances`, `position.switch_distance`). Раскладка — это
выбор порога, и ошибиться в нём легко: у каждого флага порогов два (гистерезис), и
какой из них «следующий», зависит от позиции. Перепутанный порог дал бы на первой
строке панели уверенное «флаг снимется ниже 28,3%» вместо 22,1% — и ни одной ошибки в
консоли. Поэтому здесь проверяется ровно выбор:

  * позиция «деньги», ворота закрыты — флаги СНИМАЮТСЯ: тренд по on_threshold,
    волатильность и облигации по off_threshold;
  * позиция «акции» — флаги СТАВЯТСЯ: тренд по off_threshold, волатильность и
    облигации по threshold;
  * строка «Оценка рынка» есть только когда решает она, и порог у неё со знаком
    позиции;
  * данных для строк нет — функция отдаёт null, и панель печатает фразу конвейера.

Функция вырезается из `web/app.js` и исполняется настоящим node — как в
tests/test_tile_asof.py: проверка текста сказала бы, что строка «правильная», а не
что она работает.
"""

import json
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "web" / "app.js"
CHARTS = ROOT / "web" / "charts.js"

# Живая витрина 02.10.2026: медведь · стресс · стресс, знак решения «за акции».
DISTANCES = [
    {"id": "trend", "label": "Тренд", "value": -10.02, "threshold": 0.0,
     "on_threshold": 2.0, "off_threshold": -2.0},
    {"id": "vol", "label": "Волатильность", "value": 22.5, "threshold": 28.3,
     "off_threshold": 22.1},
    {"id": "bond", "label": "Облигации", "value": -8.0, "threshold": -3.9,
     "off_threshold": -3.0},
]
CALM = {"calm_step_pct": 1.0, "vol_calm_days_min": 1, "vol_calm_days_max": 5,
        "vol_calm_status": "ok", "comp_target": None, "legs": []}


def position(**over):
    p = {"state": "flat", "gate_open": False, "comp_state": 1, "comp_daily": 0.3353,
         "comp_threshold": 0.2, "next_decision": "2026-10-09",
         "flags": {"trend": 0, "vol": 1, "bond": 1}, "switch_distance": dict(CALM)}
    p.update(over)
    return p


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


class SwitchRowsCase(unittest.TestCase):
    def setUp(self):
        if not shutil.which("node"):
            self.skipTest("нет node — проверка исполняет функцию фронта, а не читает её")
        app = APP.read_text(encoding="utf-8")
        charts = CHARTS.read_text(encoding="utf-8")
        self.harness = "\n".join([
            _slice_function(charts, "isNum"),
            _slice_function(charts, "fmtNum"),
            _slice_function(app, "ruDay"),
            _slice_function(app, "plural"),
            _slice_function(app, "switchRows"),
        ])

    def run_js(self, expr):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "harness.js"
            path.write_text(self.harness + "\nconsole.log(JSON.stringify(%s));" % expr,
                            encoding="utf-8")
            out = subprocess.run(["node", str(path)], capture_output=True, timeout=60)
            if out.returncode:
                self.fail("node не исполнил функцию: " + out.stderr.decode("utf-8", "replace")[:300])
            return json.loads(out.stdout.decode("utf-8"))

    def ask(self, pos, dists=DISTANCES):
        return self.run_js("switchRows(%s, %s)" % (json.dumps(pos, ensure_ascii=False),
                                                   json.dumps(dists, ensure_ascii=False)))

    # ------------------------------------------------------------- деньги
    def test_closed_gate_lists_flag_release_thresholds(self):
        """Ворота закрыты: три флага, у каждого порог СНЯТИЯ и текущее значение."""
        got = self.ask(position())
        self.assertIn("любой из флагов", got["lead"])
        rows = {r["k"]: r for r in got["rows"]}
        self.assertEqual(list(rows), ["Тренд", "Волатильность", "ОФЗ"])
        self.assertEqual(rows["Тренд"]["need"], "индекс выше 200-дневной средней на 2%")
        self.assertEqual(rows["Тренд"]["now"], "−10,0%")
        # Мутация: взять порог включения (28,3) вместо порога снятия (22,1).
        self.assertEqual(rows["Волатильность"]["need"], "ниже 22,1%")
        self.assertEqual(rows["Волатильность"]["now"], "22,5%")
        self.assertEqual(rows["Волатильность"]["hint"], "через 1–5 торговых дней при спокойном рынке")
        # Мутация: взять −3,9% (порог включения) вместо −3,0%.
        self.assertEqual(rows["ОФЗ"]["need"], "просадка RGBI мельче −3,0%")
        self.assertEqual(rows["ОФЗ"]["now"], "−8,0%")

    def test_score_row_only_when_score_decides(self):
        """Знак решения «за акции» — строки про оценку рынка нет; иначе она последняя."""
        self.assertNotIn("Оценка рынка", [r["k"] for r in self.ask(position())["rows"]])
        legs = [{"id": "usd_mom63", "text": "доллар дешевле сегодняшнего на 3%"},
                {"id": "slope_10_2", "text": None}]
        got = self.ask(position(comp_state=-1, comp_daily=-0.05,
                                switch_distance=dict(CALM, comp_target=0.2, legs=legs)))
        self.assertIn("и то и другое", got["lead"])
        last = got["rows"][-1]
        self.assertEqual(last["k"], "Оценка рынка")
        self.assertEqual(last["need"], "выше +0,2 в день решения (09.10)")
        self.assertEqual(last["now"], "−0,05")
        self.assertEqual(last["hint"], "хватило бы одного: доллар дешевле сегодняшнего на 3%")
        self.assertEqual(len(got["rows"]), 4)

    def test_open_gate_waits_for_score_only(self):
        got = self.ask(position(gate_open=True, comp_state=0, comp_daily=0.1,
                                flags={"trend": 1, "vol": 0, "bond": 0}))
        self.assertEqual([r["k"] for r in got["rows"]], ["Оценка рынка"])
        self.assertIn("Ворота открыты", got["lead"])

    def test_calm_wording_follows_status(self):
        def hint(**sd):
            rows = self.ask(position(switch_distance=dict(CALM, **sd)))["rows"]
            return next(r for r in rows if r["k"] == "Волатильность").get("hint")
        self.assertEqual(hint(vol_calm_days_min=3, vol_calm_days_max=3),
                         "через 3 торговых дня при спокойном рынке")
        self.assertEqual(hint(vol_calm_days_min=1, vol_calm_days_max=1),
                         "через 1 торговый день при спокойном рынке")
        self.assertEqual(hint(vol_calm_status="beyond", vol_calm_days_min=None, vol_calm_days_max=None),
                         "не раньше чем через месяц")
        self.assertIn("если индекс замрёт",
                      hint(vol_calm_status="slow_beyond", vol_calm_days_min=7, vol_calm_days_max=None))
        self.assertIsNone(hint(vol_calm_status="no_data", vol_calm_days_min=None, vol_calm_days_max=None))

    # -------------------------------------------------------------- акции
    def test_long_lists_flag_raise_thresholds(self):
        """Позиция «акции»: пороги, по которым флаги СТАВЯТСЯ, и порог выхода по оценке."""
        dists = [dict(DISTANCES[0], value=4.2), dict(DISTANCES[1], value=19.0),
                 dict(DISTANCES[2], value=-3.4)]
        got = self.ask(position(state="long", gate_open=True, comp_state=1, comp_daily=0.61,
                                flags={"trend": 1, "vol": 0, "bond": 1},
                                switch_distance=dict(CALM, vol_calm_status=None)), dists)
        self.assertIn("сменится на деньги", got["lead"])
        rows = {r["k"]: r for r in got["rows"]}
        self.assertEqual(list(rows), ["Тренд", "Волатильность", "ОФЗ", "Оценка рынка"])
        self.assertEqual(rows["Тренд"]["need"], "индекс ниже 200-дневной средней на 2%")
        # Мутация: показать порог снятия (22,1) там, где флаг ещё не стоит.
        self.assertEqual(rows["Волатильность"]["need"], "выше 28,3%")
        self.assertEqual(rows["ОФЗ"]["need"], "просадка RGBI глубже −3,9%")
        self.assertEqual(rows["Оценка рынка"]["need"], "ниже −0,2 в день решения (09.10)")
        # Флаг облигаций уже стоит (гистерезис): значение −3,4% само этого не скажет.
        self.assertEqual(rows["ОФЗ"]["now"], "−3,4% · флаг стоит")
        self.assertEqual(rows["Тренд"]["now"], "+4,2%")
        self.assertEqual(rows["Волатильность"]["now"], "19,0%")

    # ---------------------------------------------------------- нет данных
    def test_falls_back_when_numbers_are_missing(self):
        """Старая витрина или флаг без значения — null: панель печатает фразу конвейера."""
        self.assertIsNone(self.ask(position(), []))
        self.assertIsNone(self.ask(position(), DISTANCES[:2]))
        self.assertIsNone(self.ask(position(flags={"trend": 0, "vol": None, "bond": 1})))
        self.assertIsNone(self.ask(position(), [dict(DISTANCES[0], value=None)] + DISTANCES[1:]))
        no_flags = position()
        del no_flags["flags"]
        self.assertIsNone(self.ask(no_flags))

    def test_plural(self):
        got = self.run_js("[1,2,5,11,12,21,22,25,34,111].map(function(n){return plural(n,'месяц','месяца','месяцев');})")
        self.assertEqual(got, ["месяц", "месяца", "месяцев", "месяцев", "месяцев",
                               "месяц", "месяца", "месяцев", "месяца", "месяцев"])


if __name__ == "__main__":
    unittest.main()
