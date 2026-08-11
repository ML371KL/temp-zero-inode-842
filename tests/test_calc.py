"""Свойства векторных утилит (pipeline/lib/calc.py).

Здесь проверяется не «функция что-то возвращает», а СЕМАНТИКА окон, ради которой
calc.py и написан руками: min_periods считает НЕПУСТЫЕ наблюдения, СКО выборочное
(ddof=1), квантиль линейно интерполированный, связи в рангах усредняются. Любое
расхождение в этих четырёх вещах тихо сдвигает пороги состояний и z-скоры ядра —
и не проявляется никак, кроме неверных чисел на панели.

Эталонные значения посчитаны руками и вписаны литералами: сверять результат с
пересчётом теми же функциями бессмысленно.
"""

import math
import unittest

from tests import need, panel_small

SQRT252 = 15.874507866387544


class TestNoneHandling(unittest.TestCase):
    """None обязан протекать через все утилиты как «нет значения», а не как 0."""

    def setUp(self):
        self.calc = need(self, "pipeline.lib.calc", "rolling_mean", "log_return")

    def test_is_num_rejects_nan(self):
        # мутация: `return v is not None` (без проверки на NaN) — тест краснеет.
        # NaN из парсера HTML сравнивается с порогом молча (NaN > x == False) и
        # портит бит состояния, не оставляя следов в логе.
        self.assertTrue(self.calc.is_num(0.0))
        self.assertFalse(self.calc.is_num(None))
        self.assertFalse(self.calc.is_num(float("nan")))

    def test_log_return_needs_positive_base(self):
        # мутация: убрать проверку a>0 and b>0 — math.log уронит прогон на нуле.
        self.assertEqual(self.calc.log_return([None, 100.0, 110.0]),
                         [None, None, math.log(1.1)])
        self.assertEqual(self.calc.log_return([0.0, 50.0]), [None, None])
        self.assertEqual(self.calc.log_return([100.0, None, 121.0], 2)[2],
                         math.log(1.21))

    def test_diff_pulls_none(self):
        # мутация: подстановка 0.0 вместо None — разность станет числом из воздуха.
        self.assertEqual(self.calc.diff([1.0, None, 4.0]), [None, None, None])
        self.assertEqual(self.calc.diff([1.0, None, 4.0], 2), [None, None, 3.0])

    def test_ffill_respects_limit_in_rows(self):
        # мутация: игнорировать limit -> [None,1,1,1,2]; мёртвый источник месяцами
        # изображал бы свежие данные.
        self.assertEqual(self.calc.ffill([None, 1.0, None, None, 2.0], limit=1),
                         [None, 1.0, 1.0, None, 2.0])
        self.assertEqual(self.calc.ffill([None, 1.0, None, None, 2.0]),
                         [None, 1.0, 1.0, 1.0, 2.0])

    def test_last_valid(self):
        self.assertEqual(self.calc.last_valid([1.0, 2.0, None]), (1, 2.0))
        self.assertEqual(self.calc.last_valid([None, None]), (None, None))


class TestWindows(unittest.TestCase):
    """Окна: min_periods по непустым, ddof=1, монотонная очередь для экстремумов."""

    def setUp(self):
        self.calc = need(self, "pipeline.lib.calc", "rolling_mean", "rolling_std",
                         "rolling_max", "rolling_min")

    def test_rolling_mean_counts_observations_not_rows(self):
        # Окно из 3 СТРОК с дырой посередине содержит 2 наблюдения: при
        # min_periods=3 значения нет, при 2 — есть, и это среднее по непустым.
        # мутация: считать min_periods по строкам -> на i=3 появится 3.0 при mp=3.
        xs = [1.0, 2.0, None, 4.0, 5.0]
        self.assertEqual(self.calc.rolling_mean(xs, 3), [None] * 5)
        self.assertEqual(self.calc.rolling_mean(xs, 3, 2),
                         [None, 1.5, 1.5, 3.0, 4.5])

    def test_rolling_std_is_sample_ddof1(self):
        # 2,4,4,4,5,5,7,9: популяционное СКО ровно 2.0, выборочное — sqrt(32/7).
        # мутация: ddof=0 -> 2.0, и порог 80-го перцентиля волы уедет вниз.
        xs = [2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]
        self.assertAlmostEqual(self.calc.rolling_std(xs, 8)[-1],
                               2.138089935299395, places=12)
        self.assertAlmostEqual(self.calc.rolling_std(xs, 8, ddof=0)[-1], 2.0, places=12)

    def test_rolling_std_needs_two_observations(self):
        # мутация: делить на (c-ddof) без проверки c>ddof -> ZeroDivisionError.
        self.assertIsNone(self.calc.rolling_std([5.0], 3, 1)[0])

    def test_rolling_extremes_skip_none(self):
        # Окно 2 строки: на i=3 это [5, 1] -> максимум 5, минимум 1.
        # мутация: выкидывать индекс из очереди по строке, а не по значению,
        # -> максимум «забудется» и вернётся 1.
        xs = [3.0, None, 5.0, 1.0]
        self.assertEqual(self.calc.rolling_max(xs, 2, 1), [3.0, 3.0, 5.0, 5.0])
        self.assertEqual(self.calc.rolling_min(xs, 2, 1), [3.0, 3.0, 5.0, 1.0])


class TestQuantile(unittest.TestCase):
    """Квантиль типа linear: позиция (n−1)·q с интерполяцией (numpy/pandas по умолчанию)."""

    def setUp(self):
        self.calc = need(self, "pipeline.lib.calc", "quantile", "rolling_quantile")

    def test_reference_values(self):
        xs = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
        # (n−1)·q: 0.5 -> 4.5 -> 5+0.5·(6−5); 0.8 -> 7.2 -> 8+0.2·(9−8).
        # мутация: «ближайший ранг» (numpy interpolation='lower'/'nearest')
        # -> 5.0 и 8.0; порог стресса волы сместился бы на целое наблюдение.
        self.assertEqual(self.calc.quantile(xs, 0.5), 5.5)
        self.assertAlmostEqual(self.calc.quantile(xs, 0.8), 8.2, places=12)
        self.assertEqual(self.calc.quantile(xs, 0.25), 3.25)
        self.assertEqual(self.calc.quantile(xs, 0.0), 1.0)
        self.assertEqual(self.calc.quantile(xs, 1.0), 10.0)

    def test_quantile_ignores_none(self):
        self.assertEqual(self.calc.quantile([None, 3.0, 1.0, None, 2.0], 0.5), 2.0)
        self.assertEqual(self.calc.quantile([None, 7.5], 0.5), 7.5)
        self.assertIsNone(self.calc.quantile([None, None], 0.5))

    def test_rolling_quantile_matches_plain_on_full_window(self):
        # Скользящая версия держит окно отсортированным и удаляет выбывшее значение
        # через bisect. мутация: удалять не тот элемент -> расхождение с quantile().
        xs = [5.0, 1.0, 4.0, None, 2.0, 9.0, 3.0, 7.0]
        rolling = self.calc.rolling_quantile(xs, len(xs), 0.8, min_periods=1)
        self.assertAlmostEqual(rolling[-1], self.calc.quantile(xs, 0.8), places=12)

    def test_rolling_quantile_forgets_left_edge(self):
        # Окно 3: на последнем шаге в нём [4, 2, 9]; 100 из начала обязан выпасть.
        xs = [100.0, 1.0, 4.0, 2.0, 9.0]
        self.assertAlmostEqual(self.calc.rolling_quantile(xs, 3, 1.0)[-1], 9.0, places=12)


class TestZScore(unittest.TestCase):
    def setUp(self):
        self.calc = need(self, "pipeline.lib.calc", "zscore_rolling", "zscore_last")

    def test_none_when_not_enough_observations(self):
        # мутация: считать z с первой же точки -> компоненты ядра «оживают» на
        # 2–3 наблюдениях, и композит начинается с шума вместо тишины.
        self.assertEqual(self.calc.zscore_rolling([1.0, 2.0, 3.0], 5, 4), [None] * 3)
        got = self.calc.zscore_rolling([1.0, 2.0, 3.0], 5, 3)
        self.assertIsNone(got[1])
        self.assertAlmostEqual(got[2], 1.0, places=12)  # (3−2)/1, СКО выборочное

    def test_zero_variance_gives_none(self):
        # мутация: не проверять s>0 -> деление на ноль на неподвижном ряде.
        self.assertEqual(self.calc.zscore_rolling([4.0] * 30, 10, 5), [None] * 30)

    def test_clip_bites_at_three_sigma(self):
        # Ряд из 20 нулей и выброса 100: сырой z = 4.36. Обрезка ±3 — часть
        # контракта ядра (constants.Z_CLIP), без неё один выброс тащит композит.
        xs = [0.0] * 20 + [100.0]
        raw = self.calc.zscore_rolling(xs, 21, 3)[-1]
        self.assertGreater(raw, 3.0)
        self.assertAlmostEqual(raw, 4.364357804719847, places=9)
        self.assertEqual(self.calc.zscore_rolling(xs, 21, 3, clip=3.0)[-1], 3.0)
        self.assertEqual(self.calc.zscore_rolling([-v for v in xs], 21, 3, clip=3.0)[-1],
                         -3.0)

    def test_last_equals_tail_of_rolling(self):
        # Две реализации одного окна обязаны совпадать: states.py считает витринные
        # z через zscore_last, ядро — через zscore_rolling.
        # мутация: сдвиг окна на шаг в любой из них -> расхождение.
        xs = [float(i % 7) + 0.5 * (i % 3) for i in range(80)]
        self.assertAlmostEqual(self.calc.zscore_last(xs, 30, 20),
                               self.calc.zscore_rolling(xs, 30, 20)[-1], places=9)


class TestStatistics(unittest.TestCase):
    def setUp(self):
        self.calc = need(self, "pipeline.lib.calc", "spearman_ic", "rank_average")

    def test_perfect_monotone_pairs(self):
        xs = [1.0, 2.0, 3.0, 4.0, 5.0]
        self.assertAlmostEqual(self.calc.spearman_ic(xs, xs)[0], 1.0, places=12)
        self.assertAlmostEqual(self.calc.spearman_ic(xs, xs[::-1])[0], -1.0, places=12)

    def test_ties_use_average_ranks(self):
        # x=[1,2,2,3] -> ранги [1, 2.5, 2.5, 4]; y строго растёт.
        # rho = 4.5/sqrt(4.5·5) = sqrt(0.9) = 0.9486832980505138.
        # мутация: порядковые ранги вместо средних -> rho = 1.0, и IC ядра на
        # месячной выборке с повторами (ставка не менялась) окажется завышенным.
        rho, n = self.calc.spearman_ic([1.0, 2.0, 2.0, 3.0], [1.0, 2.0, 3.0, 4.0])
        self.assertEqual(n, 4)
        self.assertAlmostEqual(rho, math.sqrt(0.9), places=12)
        self.assertEqual(self.calc.rank_average([1.0, 2.0, 2.0, 3.0]), [1.0, 2.5, 2.5, 4.0])

    def test_pairs_with_holes_are_dropped(self):
        rho, n = self.calc.spearman_ic([1.0, None, 3.0, 4.0], [1.0, 5.0, 3.0, None])
        self.assertEqual(n, 2)      # осталось две полные пары
        self.assertIsNone(rho)      # на двух парах ранговой корреляции нет

    def test_sign_changes_ignores_zero_and_none(self):
        # мутация: считать 0 сменой знака -> «развороты ядра» на плоском ряде.
        self.assertEqual(self.calc.sign_changes([1.0, -1.0, None, 0.0, -2.0, 3.0]), [1, 5])

    def test_hysteresis_holds_sign_inside_band(self):
        # Значения внутри ±0.1 знак НЕ переключают — это защита от дребезга
        # композита около нуля (constants.CORE_FLIP_HYSTERESIS).
        # мутация: сравнивать с 0 вместо порога -> [1,-1,1,1,-1,-1] и рассылка
        # «разворотов» каждый второй день.
        xs = [0.05, -0.05, 0.2, 0.05, -0.05, -0.2]
        self.assertEqual(self.calc.hysteresis_sign(xs, 0.1), [None, None, 1, 1, 1, -1])


class TestPriceMath(unittest.TestCase):
    def setUp(self):
        self.calc = need(self, "pipeline.lib.calc", "drawdown_from_max", "realized_vol")

    def test_drawdown_is_log_and_never_positive(self):
        xs = [100.0, 120.0, 90.0, 120.0]
        got = self.calc.drawdown_from_max(xs, 4, 1)
        self.assertEqual(got[0], 0.0)
        self.assertEqual(got[1], 0.0)
        self.assertAlmostEqual(got[2], math.log(0.75), places=12)
        self.assertEqual(got[3], 0.0)

    def test_drawdown_on_frozen_panel_never_positive(self):
        # Свойство на 300 днях замороженной панели: просадка от максимума не может
        # быть положительной ни в одной точке.
        # мутация: перепутать местами x и максимум -> половина ряда станет > 0.
        panel = panel_small()
        px = panel["cols"]["imoex"]
        dd = self.calc.drawdown_from_max(px, 252, 20)
        self.assertTrue(any(v is not None for v in dd))
        self.assertTrue(all(v <= 0.0 for v in dd if v is not None))

    def test_realized_vol_annualizes_by_sqrt_252(self):
        rets = [0.02, -0.02, 0.02, -0.02]
        sd = self.calc.rolling_std(rets, 4)[-1]
        self.assertAlmostEqual(sd, 0.023094010767585034, places=12)
        vol = self.calc.realized_vol(rets, 4)[-1]
        # мутация: sqrt(365) вместо sqrt(252) или отсутствие годовой нормировки —
        # порог 80-го перцентиля перестанет сходиться с валидацией.
        self.assertAlmostEqual(vol / sd, SQRT252, places=12)
        self.assertAlmostEqual(self.calc.realized_vol(rets, 4, annualized=False)[-1],
                               sd, places=12)

    def test_realized_vol_non_negative_on_frozen_panel(self):
        panel = panel_small()
        rets = self.calc.log_return(panel["cols"]["imoex"], 1)
        vol = self.calc.realized_vol(rets, 21)
        self.assertTrue(any(v is not None for v in vol))
        self.assertTrue(all(v >= 0.0 for v in vol if v is not None))


class TestMonthly(unittest.TestCase):
    def setUp(self):
        self.calc = need(self, "pipeline.lib.calc", "month_end_indices", "resample_month_end")

    def test_month_end_indices(self):
        dates = ["2026-01-29", "2026-01-30", "2026-02-02", "2026-02-27", "2026-03-02"]
        self.assertEqual(self.calc.month_end_indices(dates), [1, 3, 4])

    def test_resample_takes_last_non_empty_of_month(self):
        # pandas .resample('ME').last() пропускает NaN: если ряд не обновился в
        # последний день месяца, берётся предыдущее наблюдение ТОГО ЖЕ месяца.
        # мутация: брать значение последней строки месяца -> февраль станет None,
        # и месячная нога ядра будет терять месяцы на каждом «тихом» закрытии.
        dates = ["2026-01-30", "2026-02-02", "2026-02-27", "2026-03-02"]
        labels, vals = self.calc.resample_month_end(dates, [1.0, 3.0, None, None])
        self.assertEqual(labels, ["2026-01-30", "2026-02-27", "2026-03-02"])
        self.assertEqual(vals, [1.0, 3.0, None])

    def test_month_key(self):
        self.assertEqual(self.calc.month_key("2026-08-11"), "2026-08")


if __name__ == "__main__":
    unittest.main()
