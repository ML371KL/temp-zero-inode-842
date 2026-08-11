"""Ядро (слой 1): месячный композит равновзвешенных z-скоров.

Порт модели M1, выигравшей walk-forward (OOS IC +0,227 против ~0 у адаптивных
весов). Три её свойства не подлежат «улучшению», и все три проверяются здесь:
равные веса, фиксированный состав, обрезка z по ±3. Плюс гистерезис знака — без
него дашборд рассылает «развороты ядра» на шуме около нуля.

Синтетика подобрана так, чтобы ответ считался на бумаге. Ряд из 60 месяцев,
чередующийся −1/+1, в последнем месяце даёт окно ровно из 60 значений: 30 плюсов
и 30 минусов, среднее 0, выборочное СКО sqrt(60/59), значит
    z = 1 / sqrt(60/59) = sqrt(59/60) = 0,9916317…
Любое другое число в этой точке означает другое окно, другое СКО или другой сдвиг.
"""

import math
import unittest

from tests import need

Q = math.sqrt(59.0 / 60.0)          # z последнего месяца чередующегося ряда
MONTHS = 60


def month_labels(n=MONTHS, year=2021, month=1):
    """Метки месяцев: по одному дню на месяц (месячный ресемпл возьмёт его же)."""
    out = []
    for i in range(n):
        y, m = year + (month - 1 + i) // 12, (month - 1 + i) % 12 + 1
        last = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
        if m == 2 and y % 4 == 0 and (y % 100 != 0 or y % 400 == 0):
            last = 29
        out.append(f"{y:04d}-{m:02d}-{last:02d}")
    return out


def panel_of(**cols):
    dates = month_labels()
    base = {"imoex": [3000.0 + 10.0 * i for i in range(MONTHS)]}
    base.update(cols)
    for name, values in base.items():
        assert len(values) == MONTHS, name
    return {"dates": dates, "cols": base}


def alternating(last_positive=True, size=1.0):
    """Чередование ∓size; последний месяц положительный или отрицательный."""
    sign = 1.0 if last_positive else -1.0
    return [sign * size * (1.0 if i % 2 == (MONTHS - 1) % 2 else -1.0)
            for i in range(MONTHS)]


class CoreCase(unittest.TestCase):
    def setUp(self):
        self.core = need(self, "pipeline.compute.core", "compute_core", "core_label")
        self.constants = need(self, "pipeline.lib.constants", "CORE_COMPONENTS",
                              "Z_CLIP", "CORE_FLIP_HYSTERESIS")
        self.ids = [c["id"] for c in self.constants.CORE_COMPONENTS]

    def by_id(self, out):
        return {c["id"]: c for c in out["components"]}


class TestComposite(CoreCase):
    """Знак и величина на синтетике с известным ответом."""

    def setUp(self):
        super().setUp()
        # usd_mom63 заканчивается плюсом, наклон кривой — минусом, гэп бочки —
        # минусом. Знаки компонентов из constants: +1, +1, −1, поэтому вклады
        # равны +q, −q, +q, а композит = q/3.
        self.out = self.core.compute_core(panel_of(
            usd_mom63=alternating(True),
            slope_10_2=alternating(False),
            urals_rub_gap=alternating(False),
        ), with_health=False)

    def test_value_and_label(self):
        # мутация: веса не равные (например, «по значимости») -> число уедет;
        # мутация: окно z 36 вместо 60 -> уедет СКО, а с ним и z.
        self.assertAlmostEqual(self.out["value"], round(Q / 3.0, 3), places=6)
        self.assertEqual(self.out["value"], 0.331)
        self.assertEqual(self.out["label"], "умеренный лонг")
        self.assertEqual(self.out["sign"], 1)
        self.assertFalse(self.out["degraded"])
        self.assertEqual(self.out["n_components"], 3)
        self.assertEqual(self.out["asof"], month_labels()[-1])

    def test_component_signs_are_applied(self):
        comps = self.by_id(self.out)
        self.assertEqual(comps["usd_mom63"]["z"], round(Q, 3))
        self.assertEqual(comps["slope_10_2"]["z"], round(-Q, 3))
        self.assertEqual(comps["urals_rub_gap"]["z"], round(-Q, 3))
        # Гэп рублёвой бочки — КОНТРАРИАН (sign −1): падение гэпа это вклад ЗА
        # лонг. мутация: потерять знак -> композит станет q/3 − 2q/3 и поменяет
        # направление на противоположное.
        self.assertEqual(comps["urals_rub_gap"]["sign"], -1)
        self.assertEqual(comps["urals_rub_gap"]["contrib"], round(Q, 3))
        self.assertEqual(comps["slope_10_2"]["contrib"], round(-Q, 3))

    def test_equal_weights(self):
        # Оптимизация весов на 271 месяце — самообман (REGIME.md §7).
        # мутация: веса из «значимости» компонентов -> сумма перестанет быть 1.
        # Вес отдаётся неокруглённым: округление до 4 знаков давало 0.3333, и витрина
        # печатала три ноги по «33%» — 99% в сумме. Сумма долей обязана быть ровно 1.
        weights = [c["weight"] for c in self.out["components"]]
        self.assertEqual(weights, [1 / 3] * 3)
        self.assertEqual(sum(weights), 1.0)

    def test_series_is_sorted_and_dated(self):
        series = self.out["series"]
        days = [row[0] for row in series]
        self.assertEqual(days, sorted(days))
        self.assertEqual(len(set(days)), len(days))
        self.assertTrue(all(d >= "2004-01-01" for d in days))

    def test_month_end_points_to_previous_month(self):
        # Витрина показывает дневное число, но решение принимается на месячном
        # шаге: рядом обязано лежать значение последнего ЗАКРЫТОГО месяца.
        labels = month_labels()
        self.assertEqual(self.out["month_end"]["date"], labels[-2])


class TestComponentDropout(CoreCase):
    """Компонент без данных выпадает, вес перераспределяется между остальными."""

    def setUp(self):
        super().setUp()
        self.out = self.core.compute_core(panel_of(
            usd_mom63=alternating(True),
            slope_10_2=alternating(False),
            urals_rub_gap=[None] * MONTHS,
        ), with_health=False)

    def test_divisor_changes_with_the_number_of_legs(self):
        # Те же две ноги, что и в TestComposite, но делитель стал 2: вклады
        # +q и −q гасятся ровно в ноль.
        # мутация: делить всегда на len(CORE_COMPONENTS) -> получится q/3 и
        # композит будет уверенно врать про «умеренный лонг» на нулевом сигнале.
        self.assertEqual(self.out["value"], 0.0)
        self.assertEqual(self.out["n_components"], 2)
        self.assertEqual(self.out["n_expected"], 3)
        self.assertEqual(self.out["label"], "нейтрально")

    def test_missing_leg_has_zero_weight_and_is_marked(self):
        comps = self.by_id(self.out)
        self.assertEqual(comps["usd_mom63"]["weight"], 0.5)
        self.assertEqual(comps["slope_10_2"]["weight"], 0.5)
        self.assertEqual(comps["urals_rub_gap"]["weight"], 0.0)
        self.assertFalse(comps["urals_rub_gap"]["available"])
        self.assertIsNone(comps["urals_rub_gap"]["z"])
        self.assertEqual(comps["urals_rub_gap"]["raw_fmt"], "нет данных")

    def test_single_leg_is_marked_degraded(self):
        # usd_mom63 в одиночку до 2017 давал p=0,12 — «сигнал есть, доверия мало».
        # мутация: не помечать degraded -> одноногое ядро выглядит как полное.
        out = self.core.compute_core(panel_of(
            usd_mom63=alternating(True),
            slope_10_2=[None] * MONTHS,
            urals_rub_gap=[None] * MONTHS,
        ), with_health=False)
        self.assertTrue(out["degraded"])
        self.assertEqual(out["n_components"], 1)
        self.assertAlmostEqual(out["value"], round(Q, 3), places=6)


class TestZClip(CoreCase):
    def test_clip_at_three_sigma(self):
        # 59 нулей и выброс: сырой z = 7,62. Обрезка ±3 (constants.Z_CLIP) —
        # часть контракта ядра.
        # мутация: снять обрезку -> один выброс месяца утащит композит на 7,6,
        # то есть на порядок за пределы шкалы «сильный лонг».
        spike = [0.0] * (MONTHS - 1) + [1000.0]
        out = self.core.compute_core(panel_of(
            usd_mom63=spike,
            slope_10_2=[None] * MONTHS,
            urals_rub_gap=[None] * MONTHS,
        ), with_health=False)
        self.assertEqual(out["value"], self.constants.Z_CLIP)
        self.assertEqual(self.by_id(out)["usd_mom63"]["z"], self.constants.Z_CLIP)
        self.assertEqual(out["label"], "сильный лонг")

    def test_clip_is_symmetric(self):
        spike = [0.0] * (MONTHS - 1) + [-1000.0]
        out = self.core.compute_core(panel_of(
            usd_mom63=spike,
            slope_10_2=[None] * MONTHS,
            urals_rub_gap=[None] * MONTHS,
        ), with_health=False)
        self.assertEqual(out["value"], -self.constants.Z_CLIP)


class TestHysteresis(CoreCase):
    """Дребезг около нуля не имеет права порождать смену знака."""

    def setUp(self):
        super().setUp()
        # Сорок месяцев уверенного размаха ±10 (последний из них — плюс, знак
        # становится +1), затем двадцать месяцев мелкой ряби ±0,5. В окне 60
        # СКО ≈ 8,24, поэтому |z| ряби ≈ 0,06 — меньше порога 0,10.
        wobble = [(10.0 if i % 2 else -10.0) if i < 40 else (0.5 if i % 2 == 0 else -0.5)
                  for i in range(MONTHS)]
        self.labels = month_labels()
        self.out = self.core.compute_core(panel_of(
            usd_mom63=wobble,
            slope_10_2=[None] * MONTHS,
            urals_rub_gap=[None] * MONTHS,
        ), with_health=False)

    def test_value_is_inside_the_band(self):
        self.assertLess(abs(self.out["value"]), self.constants.CORE_FLIP_HYSTERESIS)

    def test_sign_survives_the_wobble(self):
        # Значение отрицательное, а знак остался положительным — это и есть
        # гистерезис. мутация: sign = знак значения -> ядро «развернётся»
        # на 0,06 и разошлёт алерт о развороте (в 838 это давало смену каждый
        # второй день).
        self.assertLess(self.out["value"], 0.0)
        self.assertEqual(self.out["sign"], 1)

    def test_sign_since_is_the_start_of_the_run(self):
        # Знак стал +1 на 40-м месяце (индекс 39) и с тех пор не менялся.
        self.assertEqual(self.out["sign_since"], self.labels[39])


class TestLabels(CoreCase):
    def test_label_boundaries(self):
        # Границы шкалы из constants.CORE_LABELS: интервалы полуоткрытые [lo, hi).
        self.assertEqual(self.core.core_label(0.0), "нейтрально")
        self.assertEqual(self.core.core_label(0.29), "нейтрально")
        self.assertEqual(self.core.core_label(0.3), "умеренный лонг")
        self.assertEqual(self.core.core_label(1.0), "сильный лонг")
        self.assertEqual(self.core.core_label(-0.3), "нейтрально")
        self.assertEqual(self.core.core_label(-0.31), "умеренный шорт")
        self.assertEqual(self.core.core_label(-1.0), "умеренный шорт")
        self.assertEqual(self.core.core_label(-1.01), "сильный шорт")
        self.assertEqual(self.core.core_label(None), "нет данных")


class TestSilence(CoreCase):
    def test_core_without_data_is_silent_not_zero(self):
        # мутация: вернуть value=0.0 при отсутствии данных -> «нейтрально»
        # неотличимо от «мы ничего не знаем», и панель врёт уверенностью.
        out = self.core.compute_core(panel_of(
            usd_mom63=[None] * MONTHS,
            slope_10_2=[None] * MONTHS,
            urals_rub_gap=[None] * MONTHS,
        ), with_health=False)
        self.assertIsNone(out["value"])
        self.assertEqual(out["sign"], 0)
        self.assertTrue(out["degraded"])
        self.assertEqual(out["n_components"], 0)
        self.assertEqual(len(out["components"]), len(self.ids))
        self.assertEqual(out["series"], [])

    def test_short_history_gives_no_z(self):
        # min 24 месяца (constants.Z_MIN_MONTHS): на 12 месяцах ядро молчит.
        dates = month_labels(12)
        panel = {"dates": dates, "cols": {
            "imoex": [3000.0 + i for i in range(12)],
            "usd_mom63": [float(i % 2) for i in range(12)],
            "slope_10_2": [float(i % 3) for i in range(12)],
            "urals_rub_gap": [float(i % 5) for i in range(12)],
        }}
        self.assertIsNone(self.core.compute_core(panel, with_health=False)["value"])


class TestHealth(CoreCase):
    def test_health_block_is_attached(self):
        out = self.core.compute_core(panel_of(
            usd_mom63=alternating(True),
            slope_10_2=alternating(False),
            urals_rub_gap=alternating(False),
        ))
        health = out["health"]
        for key in ("ic_24m", "n", "status", "series", "coverage"):
            self.assertIn(key, health)
        self.assertIn(health["status"], ("ok", "warn", "dead"))
        # IC считается только по ЗАВЕРШЁННЫМ месяцам: последняя пара «сигнал →
        # форвард» смотрела бы в будущее (health.py, правило 1).
        # мутация: включить последние два месяца -> n вырастет на 2, а IC станет
        # считаться по доходности незакрытого месяца.
        self.assertLessEqual(health["n"], MONTHS - 2)


if __name__ == "__main__":
    unittest.main()
