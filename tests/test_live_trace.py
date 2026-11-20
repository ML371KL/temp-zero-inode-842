"""Даты счёта, тексты сдвигов и живой след рядом с пересчётом (аудит 20.11.2026, §4.1, §4.5).

Три класса дефектов, которые этот файл закрепляет:

1. ДАТА СВЕЖЕЕ ДАННЫХ. Индекс полной доходности биржа отдаёт на день позже вечернего
   прогона, колонка панели протянута вперёд, и журнал подписывался днём, которого в
   счёте не было (19.11.2026: подпись 19.11, падение −2,1 % в числа не вошло). Счёт
   обязан кончаться последним днём с СОБСТВЕННОЙ точкой индекса и называть его.
2. ТЕКСТ НАОБОРОТ. «Рублёвая бочка ниже своего тренда ещё на 33 %» при бочке ВЫШЕ тренда
   на 47 %. Сдвиг называется от нынешнего значения.
3. ПЕРЕСЧЁТ, ВЫДАННЫЙ ЗА ИСТОРИЮ ПАНЕЛИ. Окна журнала и волны посчитаны нынешней версией
   правила задним числом; рядом обязано стоять то, что панель говорила вживую.

Все даты фиксированы, ряды — синтетика с ответами, посчитанными руками.
"""

import unittest
from datetime import date, timedelta

from tests import need


def weekdays(start, n):
    out, cur = [], date.fromisoformat(start)
    while len(out) < n:
        if cur.weekday() < 5:
            out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def path(*moves, start=100.0, step=10):
    """Ряд из отрезков: каждый ход — доля за отрезок, равными шагами по step дней."""
    out = [start]
    for m in moves:
        k = (1.0 + m) ** (1.0 / step)
        for _ in range(step):
            out.append(out[-1] * k)
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.t = need(self, "pipeline.compute.track", "own_last", "counted_rows", "journal",
                      "live_journal", "said_positions", "said_windows", "said_history")
        self.s = need(self, "pipeline.compute.skills", "waves", "waves_said")
        self.d = need(self, "pipeline.compute.decision", "_leg_text", "LEGS_NOTE",
                      "switch_distance")
        self.K = need(self, "pipeline.lib.constants", "PANEL_LAUNCH")


class TestCountedThroughOwnPoint(Base):
    """Журнал, живой след и волны считаются по последний день с собственной точкой индекса."""

    def setUp(self):
        super().setUp()
        self.dates = weekdays("2026-09-07", 8)
        # Последний день панели без своей точки индекса: панель протянула бы 106.
        self.tr = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, None]
        self.cash = [0.0] * 8
        self.pos = [0, 0, 0, 0, 0, 0, 0, 1]             # вход решён в последний день
        self.reasons = [""] * 7 + ["comp_pos"]
        self.last_own = self.dates[6]

    def test_дата_последней_собственной_точки_берётся_из_покрытия_панели(self):
        """Мутация: брать последний день панели → 19.11 вместо 18.11."""
        panel = {"dates": self.dates, "coverage": {"mcftr": {"last": self.last_own}}}
        self.assertEqual(self.t.own_last(panel), self.last_own)
        self.assertIsNone(self.t.own_last({"dates": self.dates}))
        self.assertIsNone(self.t.own_last({"coverage": {"mcftr": {"last": None}}}))

    def test_журнал_кончается_днём_индекса_и_называет_его(self):
        """Мутация: убрать обрезку хвоста → asof = последний день панели, в окне лишний
        нулевой день."""
        j = self.t.journal(self.dates, self.pos, self.reasons, self.tr, self.cash,
                           rule_since=self.dates[0], tr_last=self.last_own)
        self.assertEqual(j["asof"], self.last_own)
        for w in j["windows"]:
            self.assertEqual(w["to"], self.last_own, w["id"])
            self.assertEqual(w["days"], 5)               # строки дней 2…6
        old = self.t.journal(self.dates, self.pos, self.reasons, self.tr, self.cash,
                             rule_since=self.dates[0])
        self.assertEqual(old["asof"], self.dates[7], "без даты индекса — как раньше")
        self.assertEqual(old["windows"][0]["days"], 6)

    def test_сделка_последнего_дня_из_журнала_не_пропадает(self):
        """Решение принято, даже если индекс за этот день ещё не пришёл.
        Мутация: обрезать и позиции, а не только строки счёта → trades пуст."""
        j = self.t.journal(self.dates, self.pos, self.reasons, self.tr, self.cash,
                           rule_since=self.dates[0], tr_last=self.last_own)
        self.assertEqual([t["decided"] for t in j["trades"]], [self.dates[7]])
        self.assertEqual(j["trades"][0]["to"], "long")

    def test_строки_счёта_обрезаются_только_с_хвоста(self):
        rows = [(d, 0, 0.0, 0.0, 0.0) for d in self.dates]
        self.assertEqual(len(self.t.counted_rows(rows, self.dates[5])), 6)
        self.assertEqual(self.t.counted_rows(rows, None), rows)
        self.assertEqual(self.t.counted_rows(rows, "2030-01-01"), rows)
        self.assertEqual(self.t.counted_rows(rows, "2000-01-01"), [])

    def test_живой_след_кончается_тем_же_днём(self):
        """Мутация: не передать tr_last в live_journal → живой след и пересчёт подписаны
        разными днями."""
        dates = weekdays("2026-08-10", 12)
        tr = [100.0 + i for i in range(11)] + [None]
        said = {d: (0, 1) for d in dates if d >= self.K.PANEL_LAUNCH}
        live = self.t.live_journal(dates, said, tr, [0.0] * 12, tr_last=dates[10])
        self.assertEqual(live["asof"], dates[10])
        self.assertEqual(live["windows"][0]["to"], dates[10])
        self.assertEqual(live["said_last"], dates[11], "сказанное за последний день записано")
        self.assertEqual(self.t.live_journal(dates, said, tr, [0.0] * 12)["asof"], dates[11])

    def test_волны_размечаются_по_день_индекса(self):
        """Мутация: размечать по последний день панели → открытая волна подписана днём,
        за который индекса нет."""
        tr = path(0.30, -0.25, 0.40) + [None]            # 31 точка и день без индекса
        dates = weekdays("2009-08-03", 32)
        pos = [1] * 32
        w = self.s.waves(dates, tr, pos, through=dates[30])
        self.assertEqual(w["asof"], dates[30])
        self.assertEqual(w["rows"][-1]["to"], dates[30])
        self.assertTrue(w["rows"][-1]["open"])
        old = self.s.waves(dates, tr, pos)
        self.assertEqual(old["rows"][-1]["to"], dates[31])
        self.assertEqual(old["asof"], dates[31])


class TestLegTexts(Base):
    """Сдвиг показателя называется от НЫНЕШНЕГО значения."""

    def test_нефть_дешевле_чем_сейчас_а_не_ниже_тренда(self):
        """Мутация: вернуть «ниже своего тренда ещё на» → при бочке выше тренда на 47 %
        текст читается наоборот."""
        text = self.d._leg_text("urals_rub_gap", -0.3938)     # 19.11.2026
        self.assertEqual(text, "нефть в рублях на 33% дешевле, чем сейчас в расчёте")
        self.assertNotIn("тренд", text)
        self.assertEqual(self.d._leg_text("urals_rub_gap", 0.2231),
                         "нефть в рублях на 25% дороже, чем сейчас в расчёте")

    def test_доллар_и_кривая_тоже_от_нынешнего(self):
        # exp(0,1821) − 1 = 19,97 % → «20%»; мутация: печатать лог-сдвиг как процент → 18%.
        self.assertEqual(self.d._leg_text("usd_mom63", 0.1821),
                         "доллар на 20% дороже, чем сейчас")
        self.assertEqual(self.d._leg_text("usd_mom63", -0.0305),
                         "доллар на 3% дешевле, чем сейчас")
        self.assertEqual(self.d._leg_text("slope_10_2", 2.5914),
                         "кривая ОФЗ на 2,6 п.п. круче, чем сейчас (10 лет минус 2 года)")
        self.assertEqual(self.d._leg_text("slope_10_2", -0.44),
                         "кривая ОФЗ на 0,4 п.п. положе, чем сейчас (10 лет минус 2 года)")
        self.assertIsNone(self.d._leg_text("slope_10_2", None))
        self.assertIsNone(self.d._leg_text("чужой", 1.0))

    def test_оговорка_едет_отдельным_полем_только_при_сдвигах(self):
        """Мутация: не класть legs_note → витрина печатает сдвиги без «при прочих равных»."""
        core = need(self, "pipeline.compute.core", "monthly_frame")
        frz = need(self, "tests.test_rule_freeze", "synthetic_store")
        pnl = need(self, "pipeline.compute.panel", "build_panel").build_panel(
            frz.synthetic_store())
        mf = core.monthly_frame(pnl)
        flags = {"trend": 1, "vol": 0, "bond": 0}
        long_ = self.d.switch_distance(pnl, mf, "long", 1, flags)
        self.assertTrue(any(leg["text"] for leg in long_["legs"]))
        self.assertEqual(long_["legs_note"], self.d.LEGS_NOTE)
        self.assertIn("двигаются вместе", long_["legs_note"])
        waiting = self.d.switch_distance(pnl, mf, "flat", 1, flags)   # знак уже «за акции»
        self.assertEqual(waiting["legs"], [])
        self.assertIsNone(waiting["legs_note"])


class TestSaidNextToRecalc(Base):
    """Окна журнала: рядом с пересчётом — «до запуска пересчёт, с запуска сказанное»."""

    def setUp(self):
        super().setUp()
        self.dates = weekdays("2026-09-07", 10)
        self.tr = [100.0 * 1.01 ** i for i in range(10)]      # +1 % в день
        self.cash = [0.0] * 10
        self.pos = [1] * 10                                    # пересчёт: всё время акции
        # Вживую панель с пятого дня говорила «деньги».
        self.said = {d: (0, 4) for d in self.dates[4:]}

    def test_ряд_до_журнала_пересчёт_с_журнала_сказанное(self):
        """Мутация: до первой записи считать «деньги» (а не пересчёт) → окна до запуска
        теряют доходность акций."""
        dates = self.dates[:6]
        got = self.t.said_positions(dates, [1] * 6, {dates[3]: (0, 4), dates[5]: (1, 4)})
        # день 4 без записи читается прежней сказанной позицией
        self.assertEqual(got, [1, 1, 1, 0, 0, 1])
        self.assertEqual(self.t.said_positions(dates, [1, 0, 1, 0, 1, 0], {}),
                         [1, 0, 1, 0, 1, 0])

    def test_окно_получает_итог_по_сказанному(self):
        j = self.t.journal(self.dates, self.pos, [""] * 10, self.tr, self.cash,
                           rule_since=self.dates[0])
        hyb = self.t.said_windows(j, self.dates, self.pos, self.said, self.tr, self.cash)
        self.assertEqual(hyb, [1, 1, 1, 1, 0, 0, 0, 0, 0, 0])
        win = next(w for w in j["windows"] if w["id"] == "m12")
        # пересчёт: дни 2…9 в акциях, 1,01^8 − 1 = 8,29 %
        self.assertEqual(win["rule_pct"], 8.29)
        said = win["said"]
        # сказанное: дни 2…5 в акциях (решения дней 0…3), день 6 — выход с издержками
        # 0,2 %, дальше вклады под 0 %: 1,01^4 × 0,998 − 1 = 3,85 %.
        # Мутация: исполнять в день решения (lag 0) → 1,01^2 × 0,998 − 1 = 1,81 %.
        self.assertEqual(said["rule_pct"], 3.85)
        self.assertEqual(said["vs_recalc_pp"], -4.44)
        self.assertEqual(said["excess_pp"], 3.85)              # вклады дали ноль
        self.assertEqual(said["in_market_pct"], 50)
        self.assertEqual(said["switches"], 1)
        self.assertEqual(said["live_from"], self.dates[4])
        self.assertEqual(said["live_days"], 5)                 # дни 5…9 — после первой записи
        self.assertNotIn("ex_sharpe", said, "окно короче квартала — Шарпа нет и у пересчёта")
        # поля пересчёта не тронуты
        self.assertEqual(win["tr_pct"], 8.29)
        self.assertIn("с запуска", j["said_basis"])

    def test_окно_без_сказанного_получает_null(self):
        """Журнал начался после последнего дня счёта: сказанного в окнах нет.
        Мутация: писать в такое окно копию пересчёта под именем сказанного."""
        j = self.t.journal(self.dates, self.pos, [""] * 10, self.tr, self.cash,
                           rule_since=self.dates[0], tr_last=self.dates[8])
        self.t.said_windows(j, self.dates, self.pos, {self.dates[9]: (0, 4)},
                            self.tr, self.cash, tr_last=self.dates[8])
        self.assertTrue(j["windows"])
        for w in j["windows"]:
            self.assertIsNone(w["said"], w["id"])

    def test_без_журнала_окна_не_трогаются(self):
        j = self.t.journal(self.dates, self.pos, [""] * 10, self.tr, self.cash,
                           rule_since=self.dates[0])
        self.assertIsNone(self.t.said_windows(j, self.dates, self.pos, {}, self.tr, self.cash))
        self.assertNotIn("said", j["windows"][0])

    def test_отрезки_живого_следа_только_смены(self):
        d = self.dates
        said = {d[0]: (0, 1), d[1]: (0, 1), d[2]: (1, 3), d[3]: (1, 4), d[5]: (0, 4)}
        self.assertEqual(self.t.said_history(d, said), [[d[0], 0], [d[2], 1], [d[5], 0]])
        self.assertEqual(self.t.said_history(d, {}), [])


class TestWavesSaid(Base):
    def test_волны_жизни_панели_получают_взятое_вживую(self):
        tr = path(0.30, -0.25, 0.40)                           # рост, падение, рост (открыт)
        dates = weekdays("2009-08-03", 31)
        w = self.s.waves(dates, tr, [1] * 31)                  # пересчёт: всегда в акциях
        last = w["rows"][-1]
        self.assertEqual((last["taken_pct"], last["entry_days"], last["missed_pct"]),
                         (100, 1, 0.0))
        # Вживую «акции» сказаны только по закрытию дня 24 → в акциях с дня 26.
        said_pos = [0] * 24 + [1] * 7
        done = self.s.waves_said(w, dates, tr, said_pos, dates[15])
        self.assertEqual(done, 2, "падение и открытый рост пересекаются с жизнью панели")
        # рост 20…30: в акциях дни 26…30 — пять равных шагов из десяти.
        # Мутация: считать вход от дня решения, а не исполнения → 6 шагов и вход через 5.
        self.assertEqual(last["said_taken_pct"], 50)
        self.assertEqual(last["said_entry_days"], 6)
        # до входа волна прошла 1,4^(5/10) − 1 = 18,3 %
        self.assertEqual(last["said_missed_pct"], 18.3)
        self.assertEqual(w["rows"][1]["said_taken_pct"], 0, "падение пересижено в деньгах")
        self.assertNotIn("said_entry_days", w["rows"][1], "у падений входа нет")
        self.assertNotIn("said_taken_pct", w["rows"][0], "волна кончилась до запуска панели")
        self.assertEqual(w["last_rises"][-1]["said_entry_days"], 6)
        self.assertEqual(w["said_since"], dates[15])
        # поля пересчёта остались прежними
        self.assertEqual((last["taken_pct"], last["entry_days"]), (100, 1))

    def test_без_сказанного_волны_не_меняются(self):
        tr = path(0.30, -0.25, 0.40)
        dates = weekdays("2009-08-03", 31)
        w = self.s.waves(dates, tr, [1] * 31)
        self.assertEqual(self.s.waves_said(w, dates, tr, None, dates[15]), 0)
        self.assertNotIn("said_since", w)


if __name__ == "__main__":
    unittest.main()
