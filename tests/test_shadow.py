"""Тень (compute/shadow.py): каждый сигнал считается, ошибка одного не гасит блок,
история пишется в стор, теневая позиция не падает без decision.py.

Панель собирается настоящим compute/panel.py из синтетического стора, а не
подсовывается словарём: сигналы читают и панель, и стор, и расходиться они не
должны. Числа фикстур подобраны так, чтобы ожидаемое считалось в одну строку:
кривая ОФЗ прыгает ровно на 0,4 п.п. в известный день, ширина проваливается
ниже 40 % ровно в последний день, оборот и брутто-позиция физлиц — константы с
пятидневным скачком в конце (z по 120 дням заведомо больше 1).

Часы заморожены (NOW), сети нет.
"""

import json
import os
import sys
import types
import unittest
from datetime import date, datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from unittest import mock

from tests import need

NOW = datetime(2026, 8, 31, 16, 30, 0, tzinfo=timezone.utc)   # понедельник, 19:30 МСК
LAST = "2026-08-31"
FIRST = "2021-06-01"
JUMP = "2026-07-06"   # с этого дня год ОФЗ на 0,4 п.п. выше
FETCHED = "2026-08-31T16:00:00Z"


def weekdays(first, last):
    d, out = date.fromisoformat(first), []
    end = date.fromisoformat(last)
    while d <= end:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


class ShadowCase(unittest.TestCase):
    def setUp(self):
        self.shadow = need(self, "pipeline.compute.shadow", "compute_shadow", "SIGNALS")
        self.panel_mod = need(self, "pipeline.compute.panel", "build_panel")
        self.states_mod = need(self, "pipeline.compute.states", "compute_states")
        self.store = need(self, "pipeline.lib.store", "save_series", "load_series")
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prev = os.environ.get("STATE_DIR")
        os.environ["STATE_DIR"] = self.tmp.name
        self.addCleanup(self._restore)
        self.days = weekdays(FIRST, LAST)
        self.seed()
        self.panel = self.panel_mod.build_panel(self.store)
        self.states = self.states_mod.compute_states(self.panel)

    def _restore(self):
        if self.prev is None:
            os.environ.pop("STATE_DIR", None)
        else:
            os.environ["STATE_DIR"] = self.prev

    def put(self, sid, points, meta=None):
        m = {"status": "ok", "fetched_at": FETCHED}
        m.update(meta or {})
        self.store.save_series(sid, {"id": sid, "points": dict(points), "meta": m})

    def seed(self):
        days, n = self.days, len(self.days)
        # Индекс: ровно 3000, за 30 дней до конца падает к 2600 (просадка −13 %),
        # последние 6 дней чуть растёт (2600 → 2620) — для «роста IMOEX» у ротации.
        px = {}
        for i, d in enumerate(days):
            if i < n - 30:
                px[d] = 3000.0
            elif i < n - 6:
                px[d] = 3000.0 - 400.0 * (i - (n - 30)) / 24.0
            else:
                px[d] = 2600.0 + 4.0 * (i - (n - 6))
        self.put("imoex", px)
        self.put("mcftr", {d: v * 1.1 for d, v in px.items()})   # дивдоходность = 0
        self.put("rgbi", {d: 130.0 for d in days})
        self.put("rtsi", {d: 1000.0 for d in days})
        self.put("usd_cbr", {d: 80.0 + 8.0 * ((i % 400) / 400.0) for i, d in enumerate(days)})
        self.put("brent", {d: 70.0 + 15.0 * ((i % 300) / 300.0) for i, d in enumerate(days)})
        self.put("zcyc_y1", {d: (14.4 if d >= JUMP else 14.0) for d in days})
        self.put("key_rate", {FIRST: 14.0})
        self.put("breadth", {d: (0.35 if d == LAST else 0.55) for d in days})
        self.put("imoex_value", {d: (2.0e11 if i >= n - 5 else 5.0e10) for i, d in enumerate(days)})
        self.put("futoi_mx_long", {d: (120000.0 if i >= n - 5 else 60000.0) for i, d in enumerate(days)})
        self.put("futoi_mx_short", {d: -40000.0 for d in days})
        # Декада раз в пять торговых дней: панель тянет ставку не дальше 15 строк
        # (panel.DEPOSIT_FFILL_LIMIT), одна точка на всю историю дала бы None.
        self.put("deposit_decade", {d: 1.0 for d in days[::5]})   # switch_spread = 0 − 1 = −1 > −2
        self.put("lqdt_aum", dict(zip(days[-6:], [1300.0, 1290.0, 1280.0, 1270.0, 1260.0, 1250.0])))
        self.put("dividends", {}, {"asof": "2026-08-30", "items": [
            {"ticker": "SBER", "ex_date": "2026-09-15", "yield_pct": 9.3,
             "index_drag_pct": 1.302, "weight_pct": 14.0},
            {"ticker": "GAZP", "ex_date": "2026-07-15", "yield_pct": 5.1,
             "index_drag_pct": 0.8, "weight_pct": 9.0}]})
        hi2_days = days[-400:]
        self.put("hi2_market_nf", {d: (400.0 if i >= 379 else 100.0) for i, d in enumerate(hi2_days)})

    def run_shadow(self, decision=None):
        return self.shadow.compute_shadow(self.store, self.panel, self.states, decision, NOW)

    # ------------------------------------------------------------- сигналы
    def test_все_сигналы_на_месте_и_сериализуемы(self):
        out = self.run_shadow()
        ids = [s["id"] for s in out["signals"]]
        self.assertEqual(ids, [sid for sid, *_ in self.shadow.SIGNALS])
        self.assertEqual(set(ids), {"repricing", "usd_ma200", "brent_usd_gap", "hi2_nf21z",
                                    "breadth_early", "volume_capitulation", "futoi_gross",
                                    "dividend_season", "rotation_trigger", "trades_contrarian"})
        self.assertEqual(out["asof"], LAST)
        self.assertTrue(out["note"].startswith("Тень"))
        for s in out["signals"]:
            for key in ("id", "label", "value", "asof", "state", "note", "history", "status"):
                self.assertIn(key, s, s["id"])
        # data.json пишется с allow_nan=False — NaN в тени уронил бы витрину целиком.
        json.dumps(out, ensure_ascii=False, allow_nan=False)

    def test_repricing_дневной_бит_и_бит_на_конце_месяца(self):
        """Скачок 06.07: на 31.07 Δ21 = 14.4 − 14.0 = +0.4 (бит 1), на 31.08 обе точки
        окна уже 14.4 → Δ21 = 0 (бит 0). Закрытый месяц — июль, значит state = 1,
        state_daily = 0, а es для августа (читает июль) = 1."""
        out = self.run_shadow()
        s = next(x for x in out["signals"] if x["id"] == "repricing")
        self.assertEqual(s["status"], "ok")
        self.assertEqual(s["asof"], LAST)
        self.assertEqual(s["value"], 0.0)
        self.assertEqual(s["state_daily"], 0)
        self.assertEqual(s["state_month_end"], 1)
        self.assertEqual(s["state"], 1)
        self.assertEqual(s["month_end_date"], "2026-07-31")
        self.assertEqual(s["spread_pp"], 0.4)
        self.assertEqual(s["threshold_pp"], 0.25)
        self.assertLessEqual(len(s["history"]), 24)
        self.assertEqual(s["history"][-2], ["2026-07-31", 0.4])

    def test_breadth_early_бит_и_значение(self):
        s = next(x for x in self.run_shadow()["signals"] if x["id"] == "breadth_early")
        self.assertEqual(s["value"], 0.35)
        self.assertEqual(s["state"], 1)
        self.assertEqual(s["asof"], LAST)

    def test_розничная_эра_биты(self):
        by = {s["id"]: s for s in self.run_shadow()["signals"]}
        vc = by["volume_capitulation"]
        self.assertEqual(vc["state"], 1)          # z120 оборота ≈ +4.8 при dd −13 %
        self.assertGreater(vc["value"], 1.0)
        self.assertLess(vc["dd252"], -0.10)
        fg = by["futoi_gross"]
        self.assertEqual(fg["state"], 1)          # брутто 100 000 → 160 000
        self.assertEqual(fg["gross"], 160000.0)
        ds = by["dividend_season"]
        self.assertEqual(ds["phase"], "ex_window")   # SBER 15.09 в окне 30 дней
        self.assertEqual(ds["value"], 1.302)
        self.assertEqual(ds["state"], 1)
        self.assertEqual(ds["asof"], LAST)           # «сегодня» из NOW, не из часов
        rt = by["rotation_trigger"]
        self.assertEqual(rt["state"], 1)          # СЧА падает 5 дней, индекс растёт, спред −1
        self.assertEqual(rt["falling_days"], 5)
        self.assertEqual(rt["value"], -50.0)
        self.assertTrue(rt["imoex_up"])
        self.assertEqual(rt["switch_spread_pp"], -1.0)
        tc = by["trades_contrarian"]
        self.assertEqual(tc["status"], "no_series")
        self.assertIn("NUMTRADES", tc["note"])

    def test_ноги_и_hi2(self):
        by = {s["id"]: s for s in self.run_shadow()["signals"]}
        for sid in ("usd_ma200", "brent_usd_gap"):
            s = by[sid]
            self.assertEqual(s["status"], "ok", sid)
            self.assertIsNotNone(s["value"], sid)
            self.assertIsNone(s["state"])
            self.assertLessEqual(abs(s["value"]), 3.0)   # обрезка ±3, как в ядре
            self.assertEqual(s["asof"], LAST)
            self.assertEqual(s["contrib"], round(s["sign"] * s["value"], 3))
        self.assertEqual(by["brent_usd_gap"]["sign"], -1)
        self.assertEqual(by["usd_ma200"]["sign"], 1)
        h = by["hi2_nf21z"]
        self.assertEqual(h["status"], "ok")
        self.assertGreater(h["value"], 1.0)     # 21 день по 400 против года по 100
        self.assertEqual(h["nf21"], 400.0)
        self.assertEqual(h["asof"], LAST)

    def test_hi2_без_ряда_это_no_series(self):
        os.remove(os.path.join(self.tmp.name, "raw", "hi2_market_nf.json"))
        h = next(x for x in self.run_shadow()["signals"] if x["id"] == "hi2_nf21z")
        self.assertEqual(h["status"], "no_series")
        self.assertIn("ALGOPACK", h["note"])

    # ----------------------------------------------------------- изоляция
    def test_ошибка_одного_сигнала_не_гасит_остальные(self):
        with mock.patch.object(self.shadow, "_sig_breadth_early",
                               side_effect=RuntimeError("boom")):
            out = self.run_shadow()
        by = {s["id"]: s for s in out["signals"]}
        self.assertEqual(by["breadth_early"]["status"], "error")
        self.assertIn("RuntimeError: boom", by["breadth_early"]["error"])
        self.assertEqual(by["repricing"]["status"], "ok")
        self.assertEqual(by["futoi_gross"]["state"], 1)
        json.dumps(out, allow_nan=False)

    def test_история_пишется_в_стор_рядами_shadow(self):
        out = self.run_shadow()
        by = {s["id"]: s for s in out["signals"]}
        self.assertTrue(by["repricing"]["history_saved"])
        self.assertEqual(self.store.load_series("shadow_repricing")["points"], {LAST: 0.0})
        # Биты пишутся состоянием, ноги — значением: у ширины в сторе 1.0, не 0.35.
        self.assertEqual(self.store.load_series("shadow_breadth_early")["points"], {LAST: 1.0})
        self.assertEqual(self.store.load_series("shadow_rotation_trigger")["points"], {LAST: 1.0})
        self.assertAlmostEqual(self.store.load_series("shadow_usd_ma200")["points"][LAST],
                               by["usd_ma200"]["value"], places=3)
        self.assertFalse(by["trades_contrarian"]["history_saved"])
        self.assertIsNone(self.store.load_series("shadow_trades_contrarian"))
        meta = self.store.load_series("shadow_repricing")["meta"]
        self.assertEqual(meta["source"], "shadow")
        self.assertEqual(meta["cadence"], "derived")
        # Календарный сигнал без прошлого в панели берёт историю из стора.
        self.assertEqual(by["rotation_trigger"]["history"], [[LAST, 1.0]])

    def test_стор_без_записи_не_ломает_блок(self):
        class ReadOnly:
            def __init__(self, inner):
                self.inner = inner

            def load_series(self, sid):
                return self.inner.load_series(sid)

        out = self.shadow.compute_shadow(ReadOnly(self.store), self.panel, self.states, None, NOW)
        self.assertFalse(any(s["history_saved"] for s in out["signals"]))
        self.assertEqual(next(x for x in out["signals"] if x["id"] == "breadth_early")["state"], 1)

    # ----------------------------------------------------- теневая позиция
    def test_без_decision_позиция_unavailable_а_сигналы_считаются(self):
        with mock.patch.dict(sys.modules, {"pipeline.compute.decision": None}):
            out = self.run_shadow()
        self.assertEqual(out["shadow_position"]["status"], "unavailable")
        self.assertIn("decision", out["shadow_position"]["reason"])
        self.assertEqual(next(x for x in out["signals"] if x["id"] == "repricing")["state"], 1)

    def test_позиция_считается_автоматом_решения_с_битом_репрайсинга(self):
        """Подставной decision.py: автомат «в акциях, пока es=0». es для августа = 1
        (бит июля), значит тень стоит в деньгах с первого торгового дня августа, а
        основной автомат (es=None) — в акциях: расхождение обязано быть названо."""
        captured = {}
        fake = types.ModuleType("pipeline.compute.decision")

        def daily_composite(panel, mf):
            return [0.5] * len(panel["dates"])

        def run_automaton(dates, gate, comp_state, is_dec, es=None):
            captured.setdefault("calls", []).append(es)
            self.assertEqual(len(gate), len(dates))
            self.assertEqual(len(comp_state), len(dates))
            self.assertTrue(all(isinstance(g, bool) for g in gate))
            self.assertTrue(any(is_dec))
            if es is None:
                return [1] * len(dates), ["entry"] * len(dates)
            return [0 if e else 1 for e in es], ["es_block" if e else "entry" for e in es]

        fake.daily_composite, fake.run_automaton = daily_composite, run_automaton
        with mock.patch.dict(sys.modules, {"pipeline.compute.decision": fake}):
            out = self.run_shadow(decision={"position": {"state": "long"}})
        p = out["shadow_position"]
        self.assertEqual(p["status"], "ok")
        self.assertEqual(p["state"], "flat")
        self.assertEqual(p["since"], "2026-08-03")
        self.assertEqual(p["reason"], "es_block")
        self.assertEqual(p["es_now"], 1)
        self.assertTrue(p["differs_from_main"])
        self.assertEqual(p["main_state"], "long")
        self.assertEqual(p["diff_days_5y"], 21)        # 21 торговый день августа
        self.assertEqual(p["history"][-1], ["2026-08-03", 0])
        self.assertTrue(p["history_saved"])
        self.assertEqual(self.store.load_series("shadow_position")["points"], {LAST: 0.0})
        es = captured["calls"][0]
        self.assertEqual(len(es), len(self.days))
        self.assertEqual(es[-1], 1)
        self.assertEqual(es[self.days.index("2026-07-31")], 0)   # июль читает июнь (0)

    def test_позиция_на_настоящем_decision_py(self):
        """Живой автомат (IMPL-1): тень обязана собраться со статусом ok и назвать
        причину из словаря автомата; es в августе = 1, значит либо тень стоит в
        деньгах по es_exit, либо вход был запрещён es_block — лонга быть не может."""
        need(self, "pipeline.compute.decision", "run_automaton", "daily_composite")
        out = self.run_shadow(decision=None)
        p = out["shadow_position"]
        self.assertEqual(p["status"], "ok", p)
        self.assertEqual(p["state"], "flat")
        self.assertEqual(p["es_now"], 1)
        self.assertIn(p["reason"], ("es_exit", "es_block", "gate_close", "comp_neg",
                                    "entry", "gate_open", "comp_pos", "es_clear", "", None))
        self.assertTrue(all(d >= "2004-01-06" for d, _ in p["history"]))
        self.assertIsInstance(p["switches_per_year"], float)
        json.dumps(out, allow_nan=False)

    def test_пустая_панель_не_падает(self):
        out = self.shadow.compute_shadow(self.store, {"dates": [], "cols": {}}, {}, None, NOW)
        self.assertEqual(out["signals"], [])
        self.assertEqual(out["shadow_position"]["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
