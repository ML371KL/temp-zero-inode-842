"""Машина состояний (слой 2): три бита, ячейка, ворота сигналов, расстояния.

Состояния — это ворота риска, а не веса (REGIME.md §4: непрерывная модуляция весов
проиграла walk-forward с OOS IC −0,002). Значит, цена ошибки здесь не «немного
неточное число», а открытые ворота там, где исторически −2,9%/мес.

Панель tests/fixtures/panel_small.json построена правилами (docs/TESTING.md), ответы
в её блоке `expect` выведены из этих правил, а не из кода:
  trend  переключается на индексе 242 (imoex пересекает MA200 сверху вниз),
  vol    — на 250 (реализованная вола перескакивает порог 0.28),
  bond   — на 264 (просадка RGBI впервые глубже −4%),
  фаза ставки — на 200 (ключевая 18 → 17, то есть смягчение).
"""

import re
import unittest

from tests import need, panel_small


class StatesCase(unittest.TestCase):
    def setUp(self):
        self.states = need(self, "pipeline.compute.states", "compute_states", "cell_code",
                           "_gate_ok")
        self.constants = need(self, "pipeline.lib.constants", "CELL_STATS", "STATE_RULES", "CELL_RULES")
        self.panel = panel_small()
        self.expect = self.panel["expect"]
        self.out = self.states.compute_states(
            {"dates": self.panel["dates"], "cols": self.panel["cols"]})


class TestBits(StatesCase):
    def test_current_bits(self):
        # мутация: сравнение `>=` вместо `>` в тренде или воле -> бит перевернётся
        # на равенстве, и ячейка «токсичная» станет «медведь+спокойно».
        for axis, want in self.expect["current"].items():
            self.assertEqual(self.out["current"][axis], want, axis)

    def test_since_dates_are_run_starts(self):
        # since — начало НЕПРЕРЫВНОГО отрезка с текущим значением бита.
        # мутация: отдавать дату последнего изменения любого бита -> на панели
        # «облигационный флаг с 20.12» вместо 30.01.
        for axis, want in self.expect["since"].items():
            self.assertEqual(self.out["since"][axis], want, axis)
        # контракт §3 требует те же даты и внутри states.current.since
        self.assertEqual(self.out["current"]["since"]["trend"],
                         self.expect["since"]["trend"])

    def test_rate_phase_is_sign_of_last_change(self):
        # Ключевая ставка 16 → 18 (ужесточение) → 17 (смягчение). Ноль-«пауза» не
        # выставляется: в валидации маска easing это ровно st_rate == −1, и нули
        # закрыли бы ворота switch_spread в самой интересной части цикла.
        self.assertEqual(self.out["current"]["rate_phase"], -1)

    def test_era_flag(self):
        self.assertTrue(self.out["current"]["era_post22"])

    def test_asof_is_last_panel_day(self):
        self.assertEqual(self.out["asof"], self.expect["last_date"])


class TestBondThreshold(StatesCase):
    """Порог −4% по просадке RGBI — самая дорогая граница на панели."""

    def bond_bit(self, dd):
        out = self.states.compute_states({"dates": ["2026-08-11"], "cols": {"rgbi_dd": [dd]}})
        return out["current"]["bond"]

    def test_threshold_constant(self):
        self.assertEqual(self.constants.STATE_RULES["bond"]["threshold"], -0.04)

    def test_flag_turns_on_strictly_deeper_than_threshold(self):
        # «Просадка ГЛУБЖЕ −4%» (REGIME.md §2): ровно на пороге флага ещё нет.
        # мутация: `<=` вместо `<` -> флаг включается на границе; мутация порога
        # на −0.0399 -> включается раньше на 25 базисных пунктов, и «окно входа»
        # (вола-шип при спокойных ОФЗ) исчезает с панели вместе с +1,4…+3,8%/мес.
        self.assertEqual(self.bond_bit(-0.0399), 0)
        self.assertEqual(self.bond_bit(-0.04), 0)
        self.assertEqual(self.bond_bit(-0.0401), 1)
        self.assertEqual(self.bond_bit(-0.052), 1)

    def test_no_data_is_not_calm(self):
        # мутация: None -> 0 («спокойно») -> пропавший ряд RGBI открывает ворота.
        out = self.states.compute_states({"dates": ["2026-08-11"], "cols": {"rgbi_dd": [None]}})
        self.assertIsNone(out["current"]["bond"])
        self.assertIsNone(out["cell"])


class TestCell(StatesCase):
    def test_cell_matches_stats_table(self):
        cell = self.out["cell"]
        self.assertEqual(cell["key"], self.expect["cell_key"])
        self.assertEqual(cell["code"], self.expect["cell_code"])
        stats = self.constants.CELL_STATS[tuple(self.expect["cell_key"])]
        # мутация: перепутать порядок бит в ключе (trend/vol/bond) -> ячейка
        # (0,1,1) с −2,94%/мес подменится на (1,1,0) с +3,83%/мес, то есть
        # «руки в карманах» превратится в «лучшая точка входа».
        self.assertEqual(cell["stats"]["mean_fwd1m_pct"], stats["mean_fwd1m_pct"])
        self.assertEqual(cell["stats"]["n"], stats["n"])
        self.assertEqual(cell["stats"]["hit"], stats["hit"])
        self.assertEqual(cell["label"], stats["label"])
        self.assertEqual(cell["stats"]["mean_fwd1m_pct"],
                         self.expect["cell_stats"]["mean_fwd1m_pct"])

    def test_cell_code_words(self):
        self.assertEqual(self.states.cell_code(0, 1, 1), "bear|stress|stress")
        self.assertEqual(self.states.cell_code(1, 0, 0), "bull|calm|ok")
        self.assertEqual(self.states.cell_code(1, 1, 0), "bull|stress|ok")

    def test_rule_of_the_day_is_not_empty(self):
        self.assertTrue(self.out["cell"]["rule"].strip())

    def test_every_cell_carries_its_distribution(self):
        """Среднее без медианы и края — хвостовая статистика, выданная за прогноз.

        Аудит 12.08.2026: у токсичной ячейки среднее −2,94%, а медиана +0,64% и
        13 плюсовых месяцев из 24; минус создают четыре обвала. Витрина, где стоит
        одно среднее, обещает «примерно −3% в следующем месяце» — читатель получает
        +5% и перестаёт верить панели, хотя панель говорила о хвосте.
        """
        for key, cell in self.constants.CELL_STATS.items():
            with self.subTest(cell=cell["label"]):
                for field in ("median_fwd1m_pct", "worst_pct", "best_pct"):
                    self.assertIn(field, cell)
                self.assertLessEqual(cell["worst_pct"], cell["median_fwd1m_pct"])
                self.assertLessEqual(cell["median_fwd1m_pct"], cell["best_pct"])
                self.assertLessEqual(cell["worst_pct"], cell["mean_fwd1m_pct"])

    def test_toxic_cell_median_is_positive(self):
        # Именно этот факт правило дня обязано называть: ворота закрыты ради хвоста
        # (худший месяц −30%), а не потому что «обычно тут падают».
        toxic = self.constants.CELL_STATS[(0, 1, 1)]
        self.assertGreater(toxic["median_fwd1m_pct"], 0)
        self.assertLess(toxic["mean_fwd1m_pct"], 0)
        self.assertLess(toxic["worst_pct"], -25)
        rule = self.constants.CELL_RULES[(0, 1, 1)]
        self.assertIn("медиана", rule.lower())
        self.assertIn("2008", rule, "правило обязано называть отказ ядра, а не только ячейку")

    def test_числа_в_правилах_взяты_из_статистики_той_же_ячейки(self):
        """Правило дня цитирует CELL_STATS от руки — и ничто это не держало.

        Тексты писались копированием: «+3.8%/мес, hit 0.75», «hit 0.61 при n=54»,
        «4 месяца из 25». Реколибровка меняет CELL_STATS, а строки рядом остаются
        прежними — и панель начинает уверенно называть числа, которых в модели уже
        нет. Здесь каждое число из текста сверяется со статистикой СВОЕЙ ячейки.

        мутация: поменять hit у (1,1,0) на 0.70 -> тест красный, а без него панель
        печатала бы «hit 0.75» рядом с карточкой, где стоит 0.70.
        """
        # В текстах стоит юникодный минус — приводим к обычному, иначе float() падает.
        norm = lambda s: s.replace("−", "-").replace("–", "-")
        pct = re.compile(r"([+-]?\d+[.,]\d+)\s*%\s*/\s*мес")
        hit = re.compile(r"hit\s+(\d+[.,]\d+)")
        enn = re.compile(r"n\s*=\s*(\d+)")
        sample = re.compile(r"из\s+(\d+)\b")
        median = re.compile(r"медиана\s+([+-]?\d+[.,]\d+)\s*%")
        num = lambda m: float(m.replace(",", "."))

        checked = 0
        for key, rule in self.constants.CELL_RULES.items():
            stats = self.constants.CELL_STATS[key]
            text = norm(rule)
            with self.subTest(cell=stats["label"]):
                for m in pct.findall(text):
                    self.assertAlmostEqual(
                        num(m), stats["mean_fwd1m_pct"], delta=0.05,
                        msg=f"{stats['label']}: в тексте {m}%/мес, в статистике "
                            f"{stats['mean_fwd1m_pct']}")
                    checked += 1
                for m in hit.findall(text):
                    self.assertAlmostEqual(num(m), stats["hit"], delta=0.005,
                                           msg=f"{stats['label']}: hit в тексте {m}")
                    checked += 1
                for m in enn.findall(text):
                    self.assertEqual(int(m), stats["n"],
                                     msg=f"{stats['label']}: n в тексте {m}")
                    checked += 1
                for m in sample.findall(text):
                    # «4 месяца из N» считается по ЗАКРЫТЫМ месяцам — той же
                    # выборке, что медиана и hit рядом.
                    closed = stats.get("n_closed", stats["n"])
                    self.assertEqual(int(m), closed,
                                     msg=f"{stats['label']}: выборка в тексте {m}, "
                                         f"закрытых месяцев {closed}")
                    checked += 1
                for m in median.findall(text):
                    self.assertAlmostEqual(
                        num(m), stats["median_fwd1m_pct"], delta=0.05,
                        msg=f"{stats['label']}: медиана в тексте {m}")
                    checked += 1
        self.assertGreaterEqual(checked, 8, "разбор перестал находить числа в текстах — "
                                            "проверка выродилась в пустую")

    def test_доля_плюсовых_кратна_своей_выборке(self):
        """hit — это m/n_closed, значит hit·n_closed обязано быть целым.

        Проверка дешёвая и вскрывает то, что глазами не видно. 12.08.2026 она уже
        сработала: в таблице стояло «hit 0,54 при n=25», доля, не кратная 1/25.
        Тогдашнее исправление на 0,56 пересчитывало долю ПО 25 ПАРАМ, включая
        незакрытый месяц, который в тот день стоял в плюсе, — и 0,56 перестала
        воспроизводиться, как только рынок сдал назад (20.08.2026: 13 плюсовых из
        25 = 0,52, а на 24 закрытых 13/24 = 0,54). Число вернули к 0,54 и назвали
        его выборку полем n_closed.

        мутация: hit токсичной ячейки 0.54 -> 0.56 при n_closed=24 -> красный.
        """
        for key, stats in self.constants.CELL_STATS.items():
            closed = stats.get("n_closed", stats["n"])
            with self.subTest(cell=stats["label"]):
                hits = stats["hit"] * closed
                # hit округлён до двух знаков, поэтому допуск — цена этого округления
                # на своей выборке, не больше.
                self.assertLessEqual(
                    abs(hits - round(hits)), 0.005 * closed + 1e-9,
                    f"{stats['label']}: hit={stats['hit']} не кратен 1/{closed} "
                    f"({hits:.2f} плюсовых) — доля и выборка из разных подсчётов")

    def test_у_каждой_ячейки_выборка_не_больше_исследовательской(self):
        for key, stats in self.constants.CELL_STATS.items():
            closed = stats.get("n_closed", stats["n"])
            with self.subTest(cell=stats["label"]):
                self.assertLessEqual(closed, stats["n"])
                self.assertGreaterEqual(closed, stats["n"] - 1,
                                        "закрытых месяцев не может быть меньше n−1: "
                                        "незакрытым бывает только последний")

    def test_худший_месяц_в_тексте_не_мягче_статистики(self):
        # «от −16% до −30%»: правая граница обязана совпасть с worst_pct ячейки,
        # иначе текст обещает читателю хвост мягче настоящего.
        toxic = self.constants.CELL_STATS[(0, 1, 1)]
        text = self.constants.CELL_RULES[(0, 1, 1)].replace("−", "-")
        worst = min(float(x) for x in re.findall(r"(-\d+(?:[.,]\d+)?)\s*%", text))
        self.assertLessEqual(worst, toxic["worst_pct"] + 0.05,
                             f"в тексте худший {worst}%, в статистике {toxic['worst_pct']}%")

    def test_all_eight_cells_listed_once(self):
        cells = self.out["cells"]
        self.assertEqual(len(cells), 8)
        self.assertEqual(len({tuple(c["key"]) for c in cells}), 8)
        current = [c for c in cells if c["current"]]
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["key"], self.expect["cell_key"])


class TestActiveSignals(StatesCase):
    def setUp(self):
        super().setUp()
        self.by_id = {s["id"]: s for s in self.out["active_signals"]}

    def test_only_signals_of_this_cell_are_on(self):
        # Ячейка (медведь, стресс, облиг. стресс), фаза смягчения, эра пост-2022.
        # Включены: mom63 (вола-стресс), switch_spread (смягчение), rb_gap
        # (вола-стресс), dy_trail (пост-2022), rgbi_mom21 (медведь).
        # мутация: игнорировать `when` -> на панели появится покупка просадки
        # (dd252), которая при облигационном стрессе даёт −0,55%/мес вместо +1,43%.
        self.assertEqual(sorted(self.by_id), sorted(self.expect["active_signals"]))
        for sid in self.expect["inactive_signals"]:
            self.assertNotIn(sid, self.by_id)

    def test_verdicts_follow_sign_times_z(self):
        # Вердикт = знак сигнала × его z по 252 дням. У rb_gap знак −1, поэтому
        # провал значения вниз это «за лонг», а не «против».
        # мутация: потерять знак сигнала -> контрарианские ноги (rb_gap, dd252,
        # futoi) начнут советовать ровно наоборот.
        for sid, verdict in self.expect["active_signals"].items():
            self.assertEqual(self.by_id[sid]["verdict"], verdict, sid)

    def test_gate_and_why_are_explained(self):
        for sid, sig in self.by_id.items():
            self.assertTrue(sig["why"], sid)     # почему сигнал включён именно тут
            self.assertTrue(sig["gate"], sid)
            self.assertIn(sig["tier"], ("A", "B"), sid)

    def test_missing_column_is_not_a_verdict(self):
        # мутация: считать отсутствие данных нейтральностью -> мёртвый ряд
        # выглядит как честный «нейтральный» сигнал.
        cols = dict(self.panel["cols"])
        cols["mom63"] = [None] * len(self.panel["dates"])
        out = self.states.compute_states({"dates": self.panel["dates"], "cols": cols})
        mom = next(s for s in out["active_signals"] if s["id"] == "mom63")
        self.assertEqual(mom["verdict"], "нет данных")
        self.assertIsNone(mom["z"])


class TestDistances(StatesCase):
    def setUp(self):
        super().setUp()
        self.by_id = {d["id"]: d for d in self.out["distances"]}

    def test_all_three_axes_present(self):
        self.assertEqual(sorted(self.by_id), ["bond", "trend", "vol"])

    def test_numbers_and_direction(self):
        # Последний день панели: imoex 2410 против MA200 3012.5 (−20,0%),
        # вола 35,0% против порога 28,0% (+7,0 п.п.), просадка RGBI −5,1%
        # при пороге −3,9% (1,1 п.п. запаса).
        # Просадка RGBI показывается в ОБЫЧНЫХ процентах, а считается в логарифмических:
        # rgbi_dd = −0.052 (лог) -> exp(−0.052)−1 = −5,07% на витрине; порог −4% (лог)
        # -> −3,92%. Обе величины конвертируются вместе, поэтому момент переключения
        # флага не сдвигается, а подпись перестаёт завышать глубину падения ОФЗ.
        # мутация: показать лог-меру -> «−5,2%» при фактических −5,1% (в 2022-м разрыв
        # доходил до 6 п.п.: −37,5% против −31,3%).
        # мутация: посчитать расстояние в обратную сторону (ma/px вместо px/ma)
        # -> знак перевернётся, и «на 20% ниже средней» станет «выше».
        for axis, want in self.expect["distances"].items():
            got = self.by_id[axis]
            self.assertAlmostEqual(got["value"], want["value"], places=2, msg=axis)
            self.assertAlmostEqual(got["threshold"], want["threshold"], places=2, msg=axis)
            self.assertAlmostEqual(got["gap_pct"], want["gap_pct"], places=2, msg=axis)

    def test_texts_speak_the_right_side(self):
        self.assertIn("ниже", self.by_id["trend"]["text"])
        self.assertIn("выше", self.by_id["vol"]["text"])
        # флаг уже включён -> он «снимется», а не «включится»
        self.assertIn("снимется", self.by_id["bond"]["text"])
        # типографский минус в числах, а не дефис
        self.assertIn("−", self.by_id["bond"]["text"])

    def test_bond_text_flips_verb_when_flag_is_off(self):
        out = self.states.compute_states({"dates": ["2026-08-11"], "cols": {"rgbi_dd": [-0.01]}})
        text = next(d["text"] for d in out["distances"] if d["id"] == "bond")
        self.assertIn("включится", text)


class TestRibbon(StatesCase):
    def test_monthly_step_and_sorted(self):
        series = self.out["series"]
        self.assertTrue(series)
        days = [row[0] for row in series]
        self.assertEqual(days, sorted(days))
        self.assertEqual(len(set(d[:7] for d in days)), len(days))  # по одной точке на месяц
        # мутация: дневная лента -> 5,6 тыс. точек с 2004 года и +100 КБ в
        # data.json при лимите 250 КБ.
        self.assertLess(len(series), 20)

    def test_last_point_is_the_current_cell(self):
        self.assertEqual(self.out["series"][-1],
                         [self.expect["last_date"], self.expect["cell_code"]])

    def test_codes_are_known(self):
        known = {self.states.cell_code(*key) for key in self.constants.CELL_STATS}
        for _day, code in self.out["series"]:
            self.assertIn(code, known)


class TestEraGate(StatesCase):
    """Era-ворота dy_trail в ЗАКРЫТОМ состоянии.

    До 18.08.2026 они не проверялись нигде: мутации «снять era-проверку из
    _gate_ok» и «era_post22 всегда True» проходили зелёными. Цена — сигнал,
    валидированный ТОЛЬКО на эре после 2022 (структурный слом дивполитики),
    молча включался бы на всей истории.
    """

    def test_до_эры_ворота_закрыты(self):
        gate = self.states._gate_ok
        self.assertFalse(gate({"era": "post22"}, {"era_post22": False}),
                         "era-ворота пропустили сигнал до 2022 года")
        self.assertTrue(gate({"era": "post22"}, {"era_post22": True}))

    def test_чужая_эра_не_угадывается(self):
        # Неизвестное значение эры не должно тихо превращаться в «открыто».
        self.assertTrue(self.states._gate_ok({"era": "post22"},
                                             {"era_post22": True}))
        # want, которого _gate_ok не знает («post30»), сегодня проходит ветку
        # молча — это осознанное ограничение: реестр значений эры один (post22),
        # и появление второго обязано прийти вместе с правкой ворот. Закрепляем
        # ХОТЯ БЫ то, что post22 при выключенном флаге не пропускается.
        self.assertFalse(self.states._gate_ok({"era": "post22", "trend": 1},
                                              {"era_post22": False, "trend": 1}))

    def test_dy_trail_неактивен_до_эры(self):
        # Сквозной прогон: та же панель, но с датами до 2022 года — сигнал
        # с воротами эры обязан пропасть из активных.
        dates = [d.replace("202", "201", 1) for d in self.panel["dates"]]
        out = self.states.compute_states({"dates": dates, "cols": self.panel["cols"]})
        self.assertFalse(out["current"]["era_post22"])
        active = {s["id"] for s in out["active_signals"]}
        self.assertNotIn("dy_trail", active,
                         "сигнал эры после 2022 активен в 2010-х")


class TestDegenerate(unittest.TestCase):
    def test_empty_panel_does_not_crash(self):
        # Отказ источников не имеет права ронять прогон (контракт §0).
        states = need(self, "pipeline.compute.states", "compute_states")
        out = states.compute_states({"dates": [], "cols": {}})
        self.assertEqual(out["current"], {})
        self.assertEqual(out["series"], [])
        self.assertEqual(out["distances"], [])


if __name__ == "__main__":
    unittest.main()
