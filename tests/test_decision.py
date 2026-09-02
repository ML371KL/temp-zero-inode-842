"""Слой решения (pipeline/compute/decision.py): позиция «акции / деньги».

Две половины набора, и у них разные роли:

* СИНТЕТИКА — без стора и без сети: гистерезис флагов, недельный такт знака,
  выход по воротам любым днём и по знаку только в день решения, автомат с
  битом-выходом для тени, дневной композит, совпадающий с месячным на конце
  закрытого месяца. Каждая проверка — правило из спецификации аудита 02.09.2026
  (results/P_package.md, ступень P2), а не «что вернул код»;
* РЕГРЕССИЯ ПО ФИКСТУРЕ — tests/fixtures/decision_fixture.json хранит RLE позиций,
  флагов и знака, посчитанные эталонным движком аудита (pandas, scripts/P_lib.py)
  на копии боевого стора 02.09.2026. Реализация обязана воспроизвести их ПОБИТОВО.
  Тест включается только при STATE_DIR, указывающем на КОПИЮ того стора (в нём
  raw/imoex.json): на CI и на чужой машине он честно пропускается с причиной.

Дата в тестах фиксирована (никаких «сегодня минус N»): календарь панели — это
список строк, а последний день фикстуры — вторник 2026-09-01.
"""

import json
import math
import os
import unittest
from datetime import date, timedelta

from tests import need, panel_small, fixture_json

STATE_DIR = (os.environ.get("STATE_DIR") or "").strip()
HAS_STORE = bool(STATE_DIR) and os.path.exists(os.path.join(STATE_DIR, "raw", "imoex.json"))
SKIP_STORE = ("регрессия по фикстуре требует STATE_DIR с копией боевого стора "
              "(raw/imoex.json внутри); сейчас STATE_DIR=" + (STATE_DIR or "не задан"))


def weekdays(start, n):
    """n будних дней подряд с даты start (строки ISO). Праздники не учитываем: для
    автомата важен только порядок дней и явно заданные дни решения."""
    out, cur = [], date.fromisoformat(start)
    while len(out) < n:
        if cur.weekday() < 5:
            out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def rle(dates, series, start=None):
    out, prev = [], object()
    for d, v in zip(dates, series):
        if start and d < start:
            continue
        if v != prev:
            out.append([d, v])
            prev = v
    return out


class DecisionCase(unittest.TestCase):
    def setUp(self):
        self.dec = need(self, "pipeline.compute.decision", "compute_decision",
                        "daily_composite", "run_automaton", "decision_days",
                        "comp_state_series", "next_decision_day", "last_day_decides")
        self.states = need(self, "pipeline.compute.states", "hyst_bits", "gate_open_series",
                           "_bits", "_bits_hyst")
        self.core = need(self, "pipeline.compute.core", "monthly_frame")
        self.K = need(self, "pipeline.lib.constants", "DECISION", "REGIMES")


class TestConstants(DecisionCase):
    def test_пороги_ступени_P2(self):
        # Числа аудита: смена любого из них — это другая ступень, а не настройка.
        d = self.K.DECISION
        self.assertEqual(d["comp_threshold"], 0.20)
        self.assertEqual(d["decision_weekday"], "FRI")
        self.assertEqual(d["vol_off_quantile"], 0.60)
        self.assertEqual((d["bond_on"], d["bond_off"]), (-0.04, -0.03))
        self.assertEqual(d["trend_band"], 0.02)
        self.assertEqual(d["execution"], "next_close")

    def test_три_режима_накрывают_восемь_ячеек_без_пересечений(self):
        cells = [tuple(c) for r in self.K.REGIMES for c in r["cells"]]
        self.assertEqual(len(cells), 8)
        self.assertEqual(len(set(cells)), 8)
        toxic = next(r for r in self.K.REGIMES if r["id"] == "toxic")
        self.assertEqual([tuple(c) for c in toxic["cells"]], [(0, 1, 1)])


class TestDecisionDays(DecisionCase):
    def test_последний_торговый_день_недели(self):
        # пн–пт 24–28.08.2026, пн 31.08, вт 01.09: решение в пятницу; вторник —
        # последний день панели, но по календарю среда–пятница торговые, значит
        # неделя ещё не решена (в проде вторник не имеет права быть днём решения:
        # в среду он перестал бы им быть, и знак дрожал бы внутри недели).
        dates = weekdays("2026-08-24", 7)
        self.assertEqual(dates[-1], "2026-09-01")
        self.assertEqual(self.dec.decision_days(dates),
                         [False, False, False, False, True, False, False])

    def test_четверг_перед_праздничной_пятницей(self):
        # 1 мая 2026 — пятница и праздник: последний торговый день недели — четверг.
        dates = ["2026-04-27", "2026-04-28", "2026-04-29", "2026-04-30", "2026-05-04"]
        self.assertEqual(self.dec.decision_days(dates), [False, False, False, True, False])
        self.assertTrue(self.dec.last_day_decides("2026-04-30"))
        self.assertFalse(self.dec.last_day_decides("2026-09-01"))
        self.assertTrue(self.dec.last_day_decides("2026-09-04"))

    def test_суббота_биржи_относится_к_следующей_неделе(self):
        # period W-FRI эталона: суббота–пятница. Торговая суббота 01.03.2025 —
        # первая точка НОВОЙ недели, решение прошлой — в пятницу 28.02.
        dates = ["2025-02-27", "2025-02-28", "2025-03-01", "2025-03-03", "2025-03-07"]
        self.assertEqual(self.dec.decision_days(dates), [False, True, False, False, True])

    def test_следующий_день_решения(self):
        self.assertEqual(self.dec.next_decision_day("2026-09-01"), "2026-09-04")
        self.assertEqual(self.dec.next_decision_day("2026-09-04"), "2026-09-11")
        self.assertEqual(self.dec.next_decision_day("2026-04-28"), "2026-04-30")
        self.assertEqual(self.dec.next_decision_day("2026-04-30"), "2026-05-08")


class TestCompState(DecisionCase):
    def test_гистерезис_знака(self):
        # ±0,2: между порогами прежний знак, 0 — знак ещё не определялся.
        vals = [0.1, 0.3, 0.1, -0.1, -0.25, 0.15, 0.25]
        dec = [True] * len(vals)
        self.assertEqual(self.dec.comp_state_series(vals, dec, 0.2), [0, 1, 1, 1, -1, -1, 1])

    def test_знак_меняется_только_в_день_решения(self):
        # мутация: читать знак ежедневно -> −1 уже на второй день, а не в пятницу.
        vals = [0.5, -0.5, -0.5, -0.5, -0.5]
        dec = [True, False, False, False, True]
        self.assertEqual(self.dec.comp_state_series(vals, dec, 0.2), [1, 1, 1, 1, -1])

    def test_нет_данных_в_день_решения_держит_прежний_знак(self):
        vals = [0.5, None, 0.5]
        self.assertEqual(self.dec.comp_state_series(vals, [True, True, True], 0.2), [1, 1, 1])


class TestAutomaton(DecisionCase):
    """run_automaton на готовых рядах: две недели будних дней, пятницы — дни решения."""

    def setUp(self):
        super().setUp()
        self.dates = weekdays("2024-01-08", 10)   # пн 08.01 … пт 19.01
        self.dec_days = [d in ("2024-01-12", "2024-01-19") for d in self.dates]

    def run_(self, gate, comp, es=None):
        return self.dec.run_automaton(self.dates, gate, comp, self.dec_days, es=es)

    def test_выход_по_воротам_любым_днём(self):
        # Ворота закрылись в среду — выходим в среду, пятницу не ждём.
        gate = [True, True, False, False, False, False, False, False, False, False]
        pos, why = self.run_(gate, [1] * 10)
        self.assertEqual(pos, [1, 1, 0, 0, 0, 0, 0, 0, 0, 0])
        self.assertEqual(why[0], "entry")
        self.assertEqual(why[2], "gate_close")

    def test_выход_по_знаку_только_в_день_решения(self):
        # Знак −1 со среды (на входе автомата), но ворота открыты: сидим до пятницы.
        # мутация: разрешить выход по знаку любым днём -> pos[2] == 0.
        comp = [1, 1, -1, -1, -1, -1, -1, -1, -1, -1]
        pos, why = self.run_([True] * 10, comp)
        self.assertEqual(pos[:6], [1, 1, 1, 1, 0, 0])
        self.assertEqual(why[4], "comp_neg")
        self.assertEqual(why[2], "")

    def test_ворота_раньше_знака_в_причине(self):
        # В пятницу закрылись ворота И знак ушёл в минус: причина — ворота.
        gate = [True, True, True, True, False, False, False, False, False, False]
        comp = [1, 1, 1, 1, -1, -1, -1, -1, -1, -1]
        _, why = self.run_(gate, comp)
        self.assertEqual(why[4], "gate_close")

    def test_вход_по_воротам_любым_днём(self):
        gate = [False, False, False, True, True, True, True, True, True, True]
        pos, why = self.run_(gate, [1] * 10)
        self.assertEqual(pos, [0, 0, 0, 1, 1, 1, 1, 1, 1, 1])
        self.assertEqual(why[3], "gate_open")

    def test_вход_по_знаку_называется_знаком(self):
        comp = [0, 0, 0, 0, 1, 1, 1, 1, 1, 1]
        pos, why = self.run_([True] * 10, comp)
        self.assertEqual(pos, [0, 0, 0, 0, 1, 1, 1, 1, 1, 1])
        self.assertEqual(why[4], "comp_pos")

    def test_нет_данных_по_воротам_это_закрытые_ворота(self):
        gate = [True, True, None, True, True, True, True, True, True, True]
        pos, why = self.run_(gate, [1] * 10)
        self.assertEqual(pos[1:4], [1, 0, 1])
        self.assertEqual((why[2], why[3]), ("gate_close", "gate_open"))

    def test_до_старта_позиция_не_считается(self):
        dates = weekdays("2003-12-29", 10)      # переходит через 2004-01-06
        dec = [True] * 10
        pos, why = self.dec.run_automaton(dates, [True] * 10, [1] * 10, dec)
        first = next(i for i, d in enumerate(dates) if d >= self.dec.START)
        self.assertEqual(pos[:first], [0] * first)
        self.assertEqual(pos[first:], [1] * (10 - first))
        # До старта ворота считаются закрытыми по построению (эталон обнуляет gate
        # до BT_START), поэтому первый вход зовётся «ворота открылись».
        self.assertEqual(why[first], "gate_open")
        self.assertFalse(any(why[:first]))

    def test_бит_выхода_тени(self):
        # es=1 в среду: выход в среду; в четверг бит снят, но возврат — только в
        # день решения (пятница), причина «es_clear».
        # мутация: es_reentry='any' -> pos[3] == 1.
        es = [0, 0, 1, 0, 0, 0, 0, 0, 0, 0]
        pos, why = self.run_([True] * 10, [1] * 10, es=es)
        self.assertEqual(pos[:6], [1, 1, 0, 0, 1, 1])
        self.assertEqual((why[2], why[4]), ("es_exit", "es_clear"))

    def test_без_es_поведение_прежнее(self):
        pos_a, why_a = self.run_([True] * 10, [1] * 10)
        pos_b, why_b = self.run_([True] * 10, [1] * 10, es=[0] * 10)
        self.assertEqual((pos_a, why_a), (pos_b, why_b))


def synthetic_panel(months=40, start="2020-01-01"):
    """Панель с одной ногой ядра (usd_mom63) и ценой: ~21 будний день в месяце."""
    dates, cur = [], date.fromisoformat(start)
    while len({d[:7] for d in dates}) < months + 1:
        if cur.weekday() < 5:
            dates.append(cur.isoformat())
        cur += timedelta(days=1)
    dates = [d for d in dates if d[:7] < sorted({d[:7] for d in dates})[-1]]
    n = len(dates)
    usd = [math.sin(i / 17.0) * 0.1 + i * 1e-4 for i in range(n)]
    px = [3000.0 + 100.0 * math.sin(i / 40.0) for i in range(n)]
    return {"dates": dates, "cols": {"imoex": px, "usd_mom63": usd,
                                     "slope_10_2": [None] * n, "urals_rub_gap": [None] * n}}


class TestDailyComposite(DecisionCase):
    def test_на_конце_закрытого_месяца_равен_месячному(self):
        # Доказательство, что дневное число — то же ядро: на последнем торговом дне
        # каждого закрытого месяца совпадение ≤ 1e-9. Последний месяц панели
        # закрытым не считается, но и на нём формула та же — сверяем и его.
        panel = synthetic_panel()
        mf = self.core.monthly_frame(panel)
        comp = self.dec.daily_composite(panel, mf)
        calc = need(self, "pipeline.lib.calc", "month_end_indices")
        me = calc.month_end_indices(panel["dates"])
        checked = 0
        for i, j in enumerate(me):
            a, b = comp[j], mf["composite"][i]
            self.assertEqual(a is None, b is None, panel["dates"][j])
            if a is not None:
                self.assertAlmostEqual(a, b, delta=1e-9, msg=panel["dates"][j])
                checked += 1
        self.assertGreaterEqual(checked, 10)

    def test_до_24_месяцев_композита_нет(self):
        panel = synthetic_panel(months=20)
        mf = self.core.monthly_frame(panel)
        self.assertTrue(all(v is None for v in self.dec.daily_composite(panel, mf)))

    def test_внутри_месяца_берётся_значение_дня(self):
        # мутация: подставить месячное значение на все дни -> ряд ступенчатый.
        panel = synthetic_panel()
        mf = self.core.monthly_frame(panel)
        comp = self.dec.daily_composite(panel, mf)
        tail = [v for v in comp[-40:] if v is not None]
        self.assertGreater(len(set(round(v, 9) for v in tail)), 30)

    def test_пропуск_ноги_внутри_месяца_протягивается(self):
        panel = synthetic_panel()
        d = panel["dates"]
        j = len(d) - 3
        panel["cols"]["usd_mom63"][j] = None
        mf = self.core.monthly_frame(panel)
        comp = self.dec.daily_composite(panel, mf)
        self.assertIsNotNone(comp[j])
        self.assertAlmostEqual(comp[j], comp[j - 1], delta=1e-12)


class TestHysteresisBits(DecisionCase):
    def bits(self, **cols):
        n = max(len(v) for v in cols.values())
        dates = weekdays("2024-03-04", n)
        raw = self.states._bits(dates, cols)
        return raw, self.states._bits_hyst(dates, cols, raw)

    def test_тренд_снимается_ниже_минус_двух_процентов(self):
        ratio = [0.05, 0.01, -0.01, -0.03, -0.01, 0.01, 0.03]
        raw, hyst = self.bits(imoex=[100 * (1 + r) for r in ratio], ma200=[100.0] * 7)
        self.assertEqual(raw["trend"], [1, 1, 0, 0, 0, 1, 1])
        self.assertEqual(hyst["trend"], [1, 1, 1, 0, 0, 0, 1])

    def test_вола_снимается_ниже_p60(self):
        rv = [0.30, 0.25, 0.22, 0.19, 0.22, 0.31]
        raw, hyst = self.bits(realized_vol_21=rv, vol_thresh80=[0.28] * 6,
                              vol_thresh60=[0.20] * 6)
        self.assertEqual(raw["vol"], [1, 0, 0, 0, 0, 1])
        self.assertEqual(hyst["vol"], [1, 1, 1, 0, 0, 1])

    def test_офз_снимается_выше_минус_трёх(self):
        dd = [-0.02, -0.05, -0.035, -0.025, -0.05]
        raw, hyst = self.bits(rgbi_dd=dd)
        self.assertEqual(raw["bond"], [0, 1, 0, 0, 1])
        self.assertEqual(hyst["bond"], [0, 1, 1, 0, 1])

    def test_пропуск_держит_состояние_а_старт_из_сырого_бита(self):
        raw, hyst = self.bits(imoex=[105.0, None, 95.0], ma200=[100.0, 100.0, 100.0])
        self.assertEqual(raw["trend"], [1, None, 0])
        self.assertEqual(hyst["trend"], [1, 1, 0])

    def test_ворота_закрыты_без_данных_и_в_токсичном_сочетании(self):
        raw = {"trend": [0, 0, None, 1], "vol": [1, 1, 1, 1], "bond": [1, 0, 1, 1]}
        hyst = {"trend": [0, 0, 0, 1], "vol": [1, 1, 1, 1], "bond": [1, 1, 1, 1]}
        # 0: токсично; 1: гистерезисный bond ещё 1 -> токсично; 2: сырой None -> закрыто;
        # 3: (1,1,1) -> открыто.
        self.assertEqual(self.states.gate_open_series(raw, hyst), [False, False, False, True])


class TestComputeDecisionShape(DecisionCase):
    def setUp(self):
        super().setUp()
        p = panel_small()
        self.panel = {"dates": p["dates"], "cols": p["cols"]}
        st = need(self, "pipeline.compute.states", "compute_states")
        self.st = st.compute_states(self.panel)
        self.out = self.dec.compute_decision(self.panel, self.core.monthly_frame(self.panel),
                                             self.st)

    def test_блок_позиции_полон(self):
        pos = self.out["position"]
        for key in ("state", "since", "reason", "reason_text", "execute", "decision_day",
                    "next_decision", "comp_daily", "comp_state", "comp_threshold",
                    "gate_open", "regime", "cash_rate", "cash_rate_asof", "conditions",
                    "history", "switches_per_year"):
            self.assertIn(key, pos, key)
        self.assertIn(pos["state"], ("long", "flat"))
        self.assertEqual(pos["decision_day"], self.panel["dates"][-1])
        self.assertGreater(pos["next_decision"], pos["decision_day"])
        self.assertTrue(pos["conditions"] and all(isinstance(c, str) for c in pos["conditions"]))
        days = [row[0] for row in pos["history"]]
        self.assertEqual(days, sorted(days))
        self.assertEqual(pos["comp_threshold"], 0.2)

    def test_на_панели_без_ног_ядра_позиция_деньги(self):
        # Ворота закрыты (токсичная ячейка фикстуры) и знака нет: деньги.
        self.assertEqual(self.out["position"]["state"], "flat")
        self.assertFalse(self.out["position"]["gate_open"])
        self.assertEqual(self.out["position"]["regime"], "toxic")

    def test_старое_правило_рядом(self):
        prev = self.out["position_prev_rule"]
        for key in ("state", "since", "rule"):
            self.assertIn(key, prev)
        self.assertIn(prev["state"], ("long", "flat"))

    def test_пустая_панель_не_падает(self):
        out = self.dec.compute_decision({"dates": [], "cols": {}}, {"dates": [], "raw": {},
                                                                    "composite": []})
        self.assertIsNone(out["position"])


@unittest.skipUnless(HAS_STORE, SKIP_STORE)
class TestFixtureRegression(unittest.TestCase):
    """Побитовое совпадение с эталоном аудита на копии боевого стора.

    Единственное известное расхождение семантик — последний день панели: эталон
    (бэктест) считает его днём решения просто потому, что данных дальше нет, прод —
    по календарю (вторник 01.09.2026 им не является). На фикстуре это ничего не
    меняет: 01.09 знак +1 при значении +0,81 остался бы +1, ворота закрыты.
    """

    @classmethod
    def setUpClass(cls):
        from pipeline.lib import store, calc
        from pipeline.compute import panel as panel_mod, core, states, decision
        cls.calc = calc
        cls.fx = fixture_json("decision_fixture.json")
        cls.panel = panel_mod.build_panel(store)
        cls.mf = core.monthly_frame(cls.panel)
        cls.states = states.compute_states(cls.panel)
        cls.out = decision.compute_decision(cls.panel, cls.mf, cls.states)
        cls.d = cls.out["daily"]
        cls.dates = cls.d["dates"]
        cls.idx = {dt: i for i, dt in enumerate(cls.dates)}

    def test_диапазон_панели_совпадает_с_эталоном(self):
        self.assertEqual([self.dates[0], self.dates[-1]], self.fx["range"])

    def test_позиция_P2_побитово(self):
        self.assertEqual(rle(self.dates, self.d["pos"]), self.fx["P2_pos_rle"])

    def test_позиция_старого_правила_P0_побитово(self):
        self.assertEqual(rle(self.dates, self.d["pos_prev_rule"]), self.fx["P0_pos_rle"])

    def test_знак_композита_побитово(self):
        self.assertEqual(rle(self.dates, self.d["comp_state"]), self.fx["P2_comp_state_rle"])

    def test_флаги_с_гистерезисом_побитово(self):
        for name in ("trend", "vol", "bond"):
            with self.subTest(flag=name):
                ser = [None if v is None else int(v) for v in self.d["bits"][name]]
                got = [r for r in rle(self.dates, ser) if r[1] is not None]
                self.assertEqual(got, self.fx[f"P2_{name}_rle"])

    def test_причины_смен_с_2018(self):
        got = [[dt, r] for dt, r in zip(self.dates, self.d["reasons"]) if r and dt >= "2018-01-01"]
        self.assertEqual(got, self.fx["P2_reasons"])

    def test_дневной_композит_хвост_и_выборка(self):
        for name in ("comp_live_tail", "comp_live_samples"):
            for dt, want in self.fx[name]:
                got = self.d["comp_live"][self.idx[dt]]
                with self.subTest(series=name, day=dt):
                    if want is None:
                        self.assertIsNone(got)
                    else:
                        self.assertAlmostEqual(got, want, delta=1e-6)

    def test_инвариант_на_конце_закрытого_месяца(self):
        me = self.calc.month_end_indices(self.dates)
        for i, j in enumerate(me[:-1]):
            a, b = self.d["comp_live"][j], self.mf["composite"][i]
            self.assertEqual(a is None, b is None, self.dates[j])
            if a is not None:
                self.assertAlmostEqual(a, b, delta=1e-9, msg=self.dates[j])

    def test_порог_p60_хвост(self):
        q60 = self.panel["cols"]["vol_thresh60"]
        for dt, want in self.fx["q60_tail"]:
            self.assertAlmostEqual(q60[self.idx[dt]], want, delta=1e-6, msg=dt)

    def test_текущая_позиция_по_стору(self):
        pos = self.out["position"]
        self.assertEqual((pos["state"], pos["since"], pos["reason"]),
                         ("flat", self.fx["P2_pos_rle"][-1][0], "comp_neg"))
        self.assertFalse(pos["gate_open"])
        self.assertEqual(pos["comp_state"], 1)
        self.assertEqual(pos["regime"], "toxic")
        self.assertAlmostEqual(pos["comp_daily"], self.fx["comp_live_tail"][-1][1], delta=1e-4)
        self.assertEqual(self.out["position_prev_rule"]["since"], self.fx["P0_pos_rle"][-1][0])


if __name__ == "__main__":
    unittest.main()
