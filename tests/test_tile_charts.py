"""График тайла рисует число из заголовка, подпись следует из данных (аудит 20.11.2026, §4.5).

У трёх тайлов маленький график показывал не ту величину, что крупное число: «Вклады против
дивидендов» — ставку вкладов вместо спреда, «Позиции физлиц» — долю нетто вместо z,
«Малые компании» — отношение индексов вместо относительной доходности за 63 дня. И две
подписи спорили с данными своего же тайла: «устойчивого спреда > 0 не наступало ни разу»
при 115 декадах с положительным спредом и «в 2026 впервые опережают» при отставании.

Новые ряды — отдельными полями payload (spread_series, z120_series, rs63_series): прежнее
поле series не меняется. Проверка одна на всех: последняя точка нового ряда равна числу
заголовка, а значения посчитаны руками из входов. И в блоке «Что в цене»: окно в пять
сессий помечено непоказываемым, пояснение к остатку не выдаёт остаток за знание.

Часы заморожены (now параметром), стор — временный каталог.
"""

import math
import os
import unittest
from datetime import date, datetime, timedelta, timezone
from tempfile import TemporaryDirectory

from tests import need

NOW = datetime(2026, 8, 17, 12, 0, 0, tzinfo=timezone.utc)
FETCHED = "2026-08-17T11:30:00Z"


def days(end, n):
    last = date.fromisoformat(end)
    return [(last - timedelta(days=n - 1 - i)).isoformat() for i in range(n)]


class TileCase(unittest.TestCase):
    def setUp(self):
        self.monitors = need(self, "pipeline.compute.monitors", "BUILDERS")
        self.store = need(self, "pipeline.lib.store", "save_series", "load_series")
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prev = os.environ.get("STATE_DIR")
        os.environ["STATE_DIR"] = self.tmp.name
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self.prev is None:
            os.environ.pop("STATE_DIR", None)
        else:
            os.environ["STATE_DIR"] = self.prev

    def put(self, sid, points):
        self.store.save_series(sid, {"id": sid, "points": dict(points),
                                     "meta": {"status": "ok", "fetched_at": FETCHED}})

    def tile(self, tid):
        return dict(self.monitors.BUILDERS)[tid](self.store, NOW)


class TestDepositSpread(TileCase):
    """Индекс ровный, поэтому дивдоходность — чистая функция шагов MCFTR: 0 % до дня 260,
    ln(1,10) = 9,53 % на днях 260…279 и ln(1,18) = 16,55 % дальше."""

    def seed(self, deposits):
        self.span = days("2026-08-15", 300)
        self.put("imoex", {d: 3000.0 for d in self.span})
        self.put("mcftr", {d: (1000.0 if k < 260 else 1100.0 if k < 280 else 1180.0)
                           for k, d in enumerate(self.span)})
        self.put("deposit_decade", {self.span[k]: v for k, v in deposits.items()})

    def test_график_показывает_спред_а_не_ставку(self):
        """Мутация: отдать в spread_series ставку вкладов → точки 12,0…15,9 вместо спреда."""
        self.seed({255: 12.0, 265: 9.0, 275: 10.0, 285: 15.6, 295: 15.9})
        t = self.tile("deposit_spread")
        p = t["payload"]
        dy_low, dy_high = math.log(1.10) * 100, math.log(1.18) * 100
        self.assertEqual(p["spread_pp"], round(dy_high - 15.9, 2))            # +0,65
        self.assertEqual(p["spread_series"], [
            [self.span[255], -12.0],                                           # 0 − 12,0
            [self.span[265], round(dy_low - 9.0, 2)],                          # +0,53
            [self.span[275], round(dy_low - 10.0, 2)],                         # −0,47
            [self.span[285], round(dy_high - 15.6, 2)],                        # +0,95
            [self.span[295], round(dy_high - 15.9, 2)],
            # дивдоходность свежее последней декады: хвост графика — само число заголовка
            [self.span[299], p["spread_pp"]],
        ])
        self.assertEqual(p["spread_series"][-1][0], p["dy_asof"])
        # прежнее поле не тронуто: ставка вкладов по декадам
        self.assertEqual([v for _d, v in p["series"]], [12.0, 9.0, 10.0, 15.6, 15.9])

    def test_подпись_следует_из_данных(self):
        """Мутация: вернуть «не наступало ни разу» → подпись спорит с positive_days_before."""
        self.seed({255: 12.0, 265: 9.0, 275: 10.0, 285: 15.6, 295: 15.9})
        t = self.tile("deposit_spread")
        p = t["payload"]
        self.assertEqual(p["positive_days_before"], 2)        # декады 265 и 285
        self.assertEqual(p["decades_measured"], 4)            # последняя декада — «сейчас»
        self.assertEqual((p["positive_first"], p["positive_last"]),
                         (self.span[265], self.span[285]))
        self.assertIn("спред выше нуля был в 2 декадах из 4 (2026 год).", t["note"])
        self.assertNotIn("ни разу", t["note"])

    def test_если_спред_не_был_положительным_так_и_сказано(self):
        self.seed({255: 20.0, 265: 20.0, 275: 20.0, 285: 20.0, 295: 20.0})
        t = self.tile("deposit_spread")
        self.assertEqual(t["payload"]["positive_days_before"], 0)
        self.assertIsNone(t["payload"]["positive_first"])
        self.assertIn("за 4 декады истории ряда спред выше нуля не был ни разу.", t["note"])


class TestFutoi(TileCase):
    def test_график_показывает_z_из_заголовка(self):
        """Мутация: отдать в z120_series долю нетто → последняя точка 0,05…, а не z."""
        span = days("2026-08-14", 130)
        net = [100.0 + (k * 37) % 50 for k in range(130)]
        self.put("futoi_mx_fiz_pos", dict(zip(span, net)))
        self.put("futoi_mx_fiz_long", {d: 1000.0 for d in span})
        self.put("futoi_mx_fiz_short", {d: -1000.0 for d in span})
        p = self.tile("futoi")["payload"]
        share = [v / 2000.0 for v in net]                     # брутто = 1000 − (−1000)
        win = share[-120:]
        mean = sum(win) / 120
        sd = math.sqrt(sum((x - mean) ** 2 for x in win) / 119)
        self.assertEqual(p["z120"], round((share[-1] - mean) / sd, 2))
        zs = p["z120_series"]
        self.assertEqual(zs[-1], [span[-1], p["z120"]])
        # из последних 120 дней z считается с 40-го наблюдения ряда: дни 39…129
        self.assertEqual(len(zs), 91)
        self.assertEqual(zs[0][0], span[39])
        first = share[:40]
        m0 = sum(first) / 40
        s0 = math.sqrt(sum((x - m0) ** 2 for x in first) / 39)
        self.assertEqual(zs[0][1], round((share[39] - m0) / s0, 2))
        # прежнее поле — доля нетто, как раньше
        self.assertEqual(p["series"][-1], [span[-1], round(share[-1], 4)])


class TestMcxsm(TileCase):
    def seed(self):
        self.span = days("2026-08-14", 260)
        self.put("imoex", {d: 3000.0 for d in self.span})
        self.put("mcxsm", {d: 1500.0 + k for k, d in enumerate(self.span)})

    def test_график_показывает_относительную_доходность_за_63_дня(self):
        """Мутация: отдать в rs63_series отношение индексов → 0,58 вместо +3,7 %."""
        self.seed()
        p = self.tile("mcxsm")["payload"]
        # индекс ровный: (1759 / 1696 − 1) × 100 = 3,71
        self.assertEqual(p["rs_63d_pct"], 3.7)
        rs = p["rs63_series"]
        self.assertEqual(len(rs), 120)
        self.assertEqual(rs[-1], [self.span[-1], p["rs_63d_pct"]])
        self.assertEqual(rs[0], [self.span[140], round((1640.0 / 1577.0 - 1) * 100, 1)])
        self.assertEqual(p["series"][-1], [self.span[-1], round(1759.0 / 3000.0, 4)])

    def test_подпись_не_утверждает_того_чего_нет_в_данных(self):
        """Мутация: вернуть «в 2026 впервые опережают» (опережали в 2020, 2022 и 2023)."""
        self.seed()
        t = self.tile("mcxsm")
        self.assertNotIn("впервые", t["note"])
        self.assertIn("Термометр фазы ставки", t["note"])

    def test_короткая_история_даёт_пустой_график(self):
        span = days("2026-08-14", 40)
        self.put("imoex", {d: 3000.0 for d in span})
        self.put("mcxsm", {d: 1500.0 for d in span})
        self.assertEqual(self.tile("mcxsm")["payload"]["rs63_series"], [])


class TestPricedFlags(unittest.TestCase):
    def setUp(self):
        self.priced = need(self, "pipeline.compute.priced", "block", "inputs",
                           "MIN_SHOW_SESSIONS", "OTHER_NOTE")
        syn = need(self, "tests.test_priced", "synthetic").synthetic()
        dates, idx, y10, oil, stocks, events = syn
        orig = self.priced.inputs
        self.priced.inputs = lambda _store, _panel: {
            "dates": dates, "idx": idx, "y10": y10, "oil": oil, "stocks": stocks,
            "events": events}
        self.addCleanup(setattr, self.priced, "inputs", orig)
        self.block = self.priced.block(None, {})

    def test_окно_в_пять_сессий_помечено_непоказываемым(self):
        """Мутация: порог ниже пяти → на витрину возвращается «объяснено 10 %» по пяти
        точкам. Расчёт окна при этом остаётся: его читают проверки."""
        show = {w["sessions"]: w["show"] for w in self.block["windows"]}
        self.assertEqual(show, {5: False, 21: True, 63: True})
        self.assertEqual(self.block["min_show_sessions"], 21)
        week = self.block["windows"][0]
        for key in ("index_lp", "rates_lp", "oil_lp", "other_lp", "explained"):
            self.assertIn(key, week)
        self.assertAlmostEqual(week["rates_lp"] + week["oil_lp"] + week["other_lp"],
                               week["index_lp"], places=1)

    def test_пояснение_к_остатку_не_выдаёт_его_за_знание(self):
        note = self.block["other_note"]
        self.assertIn("включая сдвиг самих коэффициентов", note)
        for word in ("переговор", "санкци", "поток", "дивиденд"):
            self.assertNotIn(word, note)


if __name__ == "__main__":
    unittest.main()
