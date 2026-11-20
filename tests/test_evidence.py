"""Справка вокруг решения (compute/evidence.py, аудит 20.11.2026).

Контроль в деньгах, сила сигнала, голосование настроек и оценка по текущей нефти — блоки,
по которым владелец судит, насколько сегодняшней позиции можно верить. В решение они не
входят, и первое, что здесь закреплено, — что они его НЕ трогают: боевая настройка
голосования совпадает с позицией побитово, параметры daily_composite по умолчанию дают
тот же ряд, блок позиции после дописывания справки остаётся прежним.

Синтетика — с ответами, посчитанными руками; стор замороженного правила
(tests/test_rule_freeze.synthetic_store) — там, где нужна вся цепочка панель → решение.
Сверка с контрольными числами аудита на копии боевого стора — отдельным классом, который
объявляется только при STATE_DIR с копией (без него его нет вовсе, а не пропуск).
"""

import copy
import math
import os
import re
import unittest
from datetime import date, timedelta

from tests import need

STATE_DIR = (os.environ.get("STATE_DIR") or "").strip()
HAS_STORE = bool(STATE_DIR) and os.path.exists(os.path.join(STATE_DIR, "raw", "mcftr.json"))


def weekdays(start, n):
    out, cur = [], date.fromisoformat(start)
    while len(out) < n:
        if cur.weekday() < 5:
            out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


class MemStore:
    """Стор-словарь с load_series: живой журнал читается только через него."""

    def __init__(self, series):
        self.series = series

    def load_series(self, sid):
        return self.series.get(sid)


_CACHE = {}


def synthetic():
    """Синтетический стор замороженного правила + Brent и живой журнал; панель, месячный
    срез и решение считаются один раз на весь файл (≈1 с)."""
    if _CACHE:
        return _CACHE
    from tests.test_rule_freeze import synthetic_store, _Lcg
    from pipeline.compute import panel as P, core as C, decision as D
    series = synthetic_store()
    days = sorted(series["imoex"]["points"])
    rnd, brent, pts = _Lcg(842), 70.0, {}
    for d in days:
        brent = min(140.0, max(20.0, brent * (1.0 + 0.012 * rnd.n())))
        # Два последних месяца — ровные: оценка по текущей нефти сверяется руками.
        pts[d] = 80.0 if d[:7] == "2024-05" else 60.0 if d[:7] == "2024-06" else round(brent, 4)
    series["brent"] = {"id": "brent", "points": pts, "meta": {}}
    # Живой журнал последних 60 дней: 40 дней «деньги», затем 20 дней «акции».
    tail = days[-60:]
    series["said_position"] = {"id": "said_position", "meta": {},
                               "points": {d: (0.0 if i < 40 else 1.0) for i, d in enumerate(tail)}}
    series["said_rule"] = {"id": "said_rule", "meta": {}, "points": {d: 4.0 for d in tail}}
    store = MemStore(series)
    pnl = P.build_panel(store)
    mf = C.monthly_frame(pnl)
    _CACHE.update(store=store, series=series, panel=pnl, mf=mf,
                  decision=D.compute_decision(pnl, mf), days=days)
    return _CACHE


def all_variants():
    """Позиции 27 настроек на синтетике — один расчёт на весь файл."""
    syn = synthetic()
    if "variants" not in syn:
        from pipeline.compute import evidence as E
        daily = syn["decision"]["daily"]
        syn["variants"] = E.vote_positions(syn["panel"], syn["mf"], daily["gate_open"],
                                           daily["is_decision_day"])
        syn["vote"] = E.vote(syn["panel"], syn["mf"], daily)
    return syn["variants"]


class Base(unittest.TestCase):
    def setUp(self):
        self.E = need(self, "pipeline.compute.evidence", "control", "strength", "vote",
                      "vote_positions", "oil_now", "cash_rate_period", "attach")
        self.D = need(self, "pipeline.compute.decision", "daily_composite",
                      "comp_state_series", "run_automaton")
        self.T = need(self, "pipeline.compute.track", "daily_rows", "summarize")
        self.K = need(self, "pipeline.lib.constants", "CONTROL_MONTHS", "STRENGTH_ZONES",
                      "VOTE_OIL", "VOTE_WINDOWS", "VOTE_THRESHOLDS", "OIL_NOW_MIN_AGE_DAYS")


# ------------------------------------------------------------- параметры ядра
class TestDailyCompositeParams(Base):
    """Голосование считает оценку ТОЙ ЖЕ функцией, что и правило."""

    def setUp(self):
        super().setUp()
        self.syn = synthetic()
        self.base = [{"id": c["id"], "sign": c["sign"]} for c in self.K.CORE_COMPONENTS]

    def test_по_умолчанию_боевые_ноги_и_окно(self):
        """Мутация: значение по умолчанию у window стало 48 → ряд другой (и заморозка
        правила краснеет вместе с этим тестом)."""
        pnl, mf = self.syn["panel"], self.syn["mf"]
        default = self.D.daily_composite(pnl, mf)
        explicit = self.D.daily_composite(pnl, mf, components=self.base,
                                          window=self.K.Z_WINDOW_MONTHS)
        self.assertEqual(default, explicit, "тот же ряд побитово")
        self.assertEqual(default, self.syn["decision"]["daily"]["comp_live"])

    def test_окно_и_состав_действительно_меняют_ряд(self):
        """Мутация: параметр принят, но не используется → все 27 настроек голосования
        оказались бы одной и той же."""
        pnl, mf = self.syn["panel"], self.syn["mf"]
        default = self.D.daily_composite(pnl, mf)
        self.assertNotEqual(self.D.daily_composite(pnl, mf, window=48), default)
        two = self.D.daily_composite(pnl, mf, components=self.base[:2])
        self.assertNotEqual(two, default)
        # Одна нога со знаком минус — зеркало той же ноги со знаком плюс.
        plus = self.D.daily_composite(pnl, mf, components=[{"id": "usd_mom63", "sign": 1}])
        minus = self.D.daily_composite(pnl, mf, components=[{"id": "usd_mom63", "sign": -1}])
        pairs = [(a, b) for a, b in zip(plus, minus) if a is not None]
        self.assertTrue(pairs)
        for a, b in pairs:
            self.assertEqual(a, -b)


# ---------------------------------------------------------------- голосование
class TestVote(Base):
    def setUp(self):
        super().setUp()
        self.syn = synthetic()
        self.daily = self.syn["decision"]["daily"]

    def test_боевая_настройка_совпадает_с_позицией_побитово(self):
        """Мутация: первым в VOTE_WINDOWS стоит не 60 (или порог не 0,2, или нефть не
        налоговая Urals) → «боевая» настройка считает другое правило."""
        variants = all_variants()
        key, pos = variants[0]
        self.assertEqual(key, ("urals_rub_gap", self.K.Z_WINDOW_MONTHS,
                               self.K.DECISION["comp_threshold"]))
        self.assertEqual(pos, self.daily["pos"])
        self.assertGreater(sum(pos), 100, "синтетика должна бывать в акциях")

    def test_семейство_три_на_три_на_три(self):
        variants = all_variants()
        keys = [k for k, _ in variants]
        self.assertEqual(len(keys), 27)
        self.assertEqual(len(set(keys)), 27)
        self.assertEqual({k[0] for k in keys}, {"urals_rub_gap", "rb_gap", "brent_usd_gap"})
        self.assertEqual({k[1] for k in keys}, {48, 60, 72})
        self.assertEqual({k[2] for k in keys}, {0.1, 0.2, 0.3})
        # Настройки не копии друг друга: и нефть, и окно, и порог меняют позицию.
        by = dict(variants)
        base = by[("urals_rub_gap", 60, 0.2)]
        for other in (("rb_gap", 60, 0.2), ("brent_usd_gap", 60, 0.2),
                      ("urals_rub_gap", 48, 0.2), ("urals_rub_gap", 60, 0.3)):
            self.assertNotEqual(by[other], base, other)

    def test_блок_считает_голоса_последнего_дня(self):
        all_variants()
        v = self.syn["vote"]
        variants = all_variants()
        longs = [sum(1 for _k, pos in variants if pos[t]) for t in range(len(self.daily["dates"]))]
        self.assertEqual(v["n_total"], 27)
        self.assertEqual(v["n_long"], longs[-1])
        self.assertAlmostEqual(v["share"], longs[-1] / 27, places=3)
        self.assertEqual(v["asof"], self.daily["dates"][-1])
        self.assertEqual(v["majority"], "long" if longs[-1] > 13 else "flat")
        self.assertEqual(v["rule_state"], "long" if self.daily["pos"][-1] else "flat")
        self.assertEqual(v["agrees"], v["majority"] == v["rule_state"])
        for part in ("by_oil", "by_window", "by_threshold"):
            self.assertEqual(sum(r["n_total"] for r in v[part]), 27, part)
            self.assertEqual(sum(r["n_long"] for r in v[part]), v["n_long"], part)

    def test_большинство_держится_с_дня_когда_оно_сменилось(self):
        """Мутация: отдавать дату смены ПОЗИЦИИ вместо даты смены большинства."""
        all_variants()
        v = self.syn["vote"]
        variants = all_variants()
        dates = self.daily["dates"]
        side = ["long" if sum(1 for _k, p in variants if p[t]) > 13 else "flat"
                for t in range(len(dates))]
        k = dates.index(v["majority_since"])
        self.assertTrue(all(s == side[-1] for s in side[k:]))
        self.assertNotEqual(side[k - 1], side[-1])

    def test_ряд_по_концам_месяцев(self):
        all_variants()
        v = self.syn["vote"]
        monthly = v["monthly"]
        self.assertEqual(len(monthly), self.K.VOTE_MONTHS + 1)   # 36 закрытых + текущий день
        self.assertEqual(monthly[-1][0], self.daily["dates"][-1])
        self.assertEqual(monthly[-1][1], v["share"])
        months = [d[:7] for d, _ in monthly]
        self.assertEqual(months, sorted(set(months)), "по одной точке на месяц, по порядку")
        for _d, share in monthly:
            self.assertTrue(0.0 <= share <= 1.0)

    def test_расхождение_с_позицией_блок_не_публикует(self):
        """Мутация: убрать сверку с позицией панели → голосование о другом правиле уехало
        бы на витрину молча."""
        daily = dict(self.daily)
        daily["pos"] = [1 - p for p in self.daily["pos"]]
        with self.assertRaises(RuntimeError):
            self.E.vote(self.syn["panel"], self.syn["mf"], daily)


# --------------------------------------------------------------- сила сигнала
class TestStrength(Base):
    def setUp(self):
        super().setUp()
        n = 60
        self.dates = weekdays("2026-01-05", n)                 # понедельник
        self.is_dec = [i % 5 == 4 for i in range(n)]           # пятницы: 4, 9, 14, …
        tr = [100.0]
        for i in range(1, n):
            # три скачка: день 5 (+10 %), день 25 (+5 %), день 26 (+20 %)
            tr.append(tr[-1] * {5: 1.10, 25: 1.05, 26: 1.20}.get(i, 1.0))
        self.tr = tr
        comp = [0.0] * n
        comp[4], comp[9], comp[14], comp[19] = 0.7, -0.3, -0.2, 0.2
        comp[24], comp[29], comp[34], comp[39] = None, 1.5, -1.5, 0.9
        comp[-1] = -0.25
        self.comp = comp

    def zones(self, **kw):
        s = self.E.strength(self.dates, self.comp, self.is_dec, self.tr,
                            kw.pop("cash", [0.0] * 60), since="2026-01-01", **kw)
        return s, {z["id"]: z for z in s["zones"]}

    def test_форвард_двадцать_сессий_от_закрытия_следующего_дня(self):
        """День решения 4: форвард — дни 6…25. Мутации: начать с дня 5 (день исполнения
        сам в счёт не идёт) → +15,5 %; горизонт 21 сессия → +26 %."""
        s, z = self.zones()
        self.assertEqual(z["z6"]["weeks"], 1)
        self.assertEqual(z["z6"]["mean_pct"], 5.0)
        self.assertEqual(z["z6"]["mean_log_pct"], round(math.log(1.05) * 100, 2))  # 4,88
        self.assertEqual(z["z6"]["hit"], 1.0)
        self.assertEqual(s["fwd_sessions"], 20)

    def test_границы_зон_и_пропуски(self):
        """−0,2 — нейтральная зона, +0,2 — уже «от +0,2 до +0,5»: [от, до).
        Мутация: границы (от, до] → обе точки уезжают в соседние зоны."""
        _s, z = self.zones()
        self.assertEqual([z[k]["weeks"] for k in ("z1", "z2", "z3", "z4", "z5", "z6", "z7")],
                         [1, 0, 1, 1, 1, 1, 1])
        # дни решения 9, 14, 19: в форварде оба скачка 25 и 26 → 1,05 × 1,20 − 1 = 26 %
        for zid in ("z3", "z4", "z5"):
            self.assertEqual(z[zid]["mean_pct"], 26.0, zid)
        # день 29 (оценка 1,5): скачков впереди нет → 0 %, и это НЕ неделя в плюс
        self.assertEqual((z["z7"]["mean_pct"], z["z7"]["hit"]), (0.0, 0.0))
        self.assertIsNone(z["z2"]["mean_pct"])
        self.assertEqual(z["z1"]["range_text"], "ниже −1,0")
        self.assertEqual(z["z4"]["range_text"], "от −0,2 до +0,2")
        self.assertEqual(z["z7"]["range_text"], "выше +1,0")

    def test_недели_без_полного_форварда_не_входят(self):
        """День 39: до конца ряда 20 сессий, а нужна 21 (день исполнения + 20).
        Мутация: считать по неполному форварду → недель 7, и последняя — с нулём."""
        s, z = self.zones()
        self.assertEqual(s["weeks"], 6)
        self.assertEqual(s["through"], self.dates[34])
        self.assertEqual(z["z6"]["weeks"], 1, "оценка 0,9 дня 39 в таблицу не попала")
        # Индекс за последние пять дней только протянут: день 34 теряет полный форвард.
        s2, _ = self.zones(tr_last=self.dates[54])
        self.assertEqual((s2["weeks"], s2["through"]), (5, self.dates[29]))

    def test_день_без_оценки_и_не_день_решения_пропущены(self):
        _s, z = self.zones()
        # день 24 — день решения без оценки; дни между пятницами не считаются вовсе
        self.assertEqual(sum(x["weeks"] for x in z.values()), 6)

    def test_сверх_вкладов_по_календарным_дням(self):
        """Ставка 10 %: за 20 сессий (28 календарных дней) вклады дают 1,1^(28/365) − 1.
        Мутация: не вычитать вклады → +5,00 вместо +4,27."""
        _s, z = self.zones(cash=[10.0] * 60)
        cash = 1.10 ** (28 / 365) - 1
        self.assertEqual(z["z6"]["mean_pct"], round((0.05 - cash) * 100, 2))
        self.assertEqual(z["z6"]["mean_log_pct"],
                         round((math.log(1.05) - math.log1p(cash)) * 100, 2))

    def test_метка_и_зона_текущей_оценки(self):
        """«Сильный» от 0,5 по модулю, «слабый» от порога решения 0,2, иначе «нейтральный»."""
        s, _ = self.zones()
        self.assertEqual((s["value"], s["zone"], s["label"]), (-0.25, "z3", "слабый"))
        self.assertIn("сигнал слабый", s["text"])
        self.assertIn("от −0,5 до −0,2", s["text"])
        self.assertIn("+26,0%", s["text"])                     # среднее своей зоны
        for value, label in ((0.5, "сильный"), (-0.5, "сильный"), (0.49, "слабый"),
                             (0.2, "слабый"), (-0.2, "слабый"), (0.19, "нейтральный"),
                             (0.0, "нейтральный"), (-1.7, "сильный")):
            self.comp[-1] = value
            self.assertEqual(self.zones()[0]["label"], label, value)

    def test_зоны_заданы_аудитом(self):
        self.assertEqual(list(self.K.STRENGTH_ZONES),
                         [(None, -1.0), (-1.0, -0.5), (-0.5, -0.2), (-0.2, 0.2),
                          (0.2, 0.5), (0.5, 1.0), (1.0, None)])


# ---------------------------------------------------------- контроль в деньгах
class TestControl(Base):
    N = 1300                                                    # ≈5 лет торговых дней

    def series(self, tail_step, tail=260, head_fall=0):
        tr = [100.0]
        for i in range(1, self.N):
            if i < head_fall:
                step = -0.0010
            elif i >= self.N - tail:
                step = tail_step
            else:
                step = 0.0005
            tr.append(tr[-1] * (1.0 + step))
        return weekdays("2010-01-04", self.N), tr, [8.0] * self.N

    def test_правило_не_хуже_вкладов(self):
        dates, tr, cash = self.series(0.0005)                  # акции +13 % в год
        c = self.E.control(dates, [1] * self.N, tr, cash)
        self.assertEqual(c["status"], "ok")
        self.assertEqual([w["months"] for w in c["windows"]], [12, 24, 36])
        for w in c["windows"]:
            self.assertEqual(w["status"], "ok")
            self.assertGreater(w["vs_cash_pp"], 0)
            self.assertIn("не хуже вкладов", w["text"])
            self.assertEqual(w["vs_cash_pp"], round(w["rule_pct"] - w["cash_pct"], 2))
            self.assertEqual(w["vs_tr_pp"], round(w["rule_pct"] - w["tr_pct"], 2))
        self.assertEqual(c["rule"], "recalc")

    def test_отставание_в_пределах_худшего_на_истории(self):
        """Первый год истории правило теряло −0,1 % в день (худшее на истории ≈ −30 п.п.),
        последний год акции растут медленнее вкладов: хуже вкладов, но не хуже худшего."""
        dates, tr, cash = self.series(0.0002, head_fall=300)
        c = self.E.control(dates, [1] * self.N, tr, cash)
        w12 = c["windows"][0]
        self.assertEqual(w12["status"], "lag")
        self.assertLess(w12["vs_cash_pp"], 0)
        self.assertLess(w12["norm"]["cash"]["worst_prior_pp"], w12["vs_cash_pp"])
        self.assertGreater(w12["norm"]["cash"]["worse_share"], 0)
        self.assertIn("хуже вкладов на", w12["text"])
        self.assertIn("худшее отставание", w12["text"])

    def test_хуже_худшего_на_истории(self):
        """Последние 252 сессии — худшие за всю историю, а девять дней перед ними рынок
        рос по 1 %: календарный год (≈261 сессия) мягче худшего скользящего окна, которое
        лежит внутри него. Мутация: сравнивать с минимумом по ВСЕМ окнам (в него входит и
        само проверяемое время) → статус «lag», и «beyond» не сработал бы никогда."""
        dates, tr, cash = self.series(-0.0015, tail=252)       # −31,5 % за 252 сессии
        for i in range(self.N - 261, self.N - 252):
            for k in range(i, self.N):
                tr[k] *= 1.01 / 1.0005
        c = self.E.control(dates, [1] * self.N, tr, cash)
        w12 = c["windows"][0]
        self.assertEqual(c["status"], "beyond")
        self.assertEqual(w12["status"], "beyond")
        self.assertLess(w12["vs_cash_pp"], -25)
        self.assertGreater(w12["norm"]["cash"]["worst_prior_pp"], 0,
                           "до текущего окна правило вклады обгоняло")
        self.assertLess(w12["norm"]["cash"]["worst_pp"], w12["vs_cash_pp"],
                        "среди всех окон есть хуже — оно внутри проверяемого года")
        self.assertIn("от вкладов не отставало", w12["text"])

    def test_текст_называет_прежнее_худшее_отставание(self):
        """В начале истории правило уже отставало (сто дней по −0,1 %), последний год —
        втрое хуже: статус «beyond», и текст называет прежнее худшее, а не нынешнее."""
        dates, tr, cash = self.series(-0.0015, tail=252, head_fall=100)
        c = self.E.control(dates, [1] * self.N, tr, cash)
        w12 = c["windows"][0]
        prior = w12["norm"]["cash"]["worst_prior_pp"]
        self.assertEqual(w12["status"], "beyond")
        self.assertTrue(-15 < prior < -5, prior)
        self.assertLess(w12["vs_cash_pp"], 3 * prior)
        self.assertEqual(w12["norm"]["cash"]["worst_prior_to"], dates[253],
                         "худшее прежнее окно — первые 252 строки счёта (дни 2…253)")
        want = str(abs(prior)).replace(".", ",") + " п.п.)"
        self.assertIn("больше худшего отставания с 2010 года (" + want, w12["text"])

    def test_окно_целиком_во_вкладах_не_хуже_вкладов(self):
        """Правило 500 дней держало акции (+0,5…0,6 % в день), затем ушло во вклады. В
        окнах, целиком лежащих во вкладах, разница — ровно ноль, а не шум округления со
        случайным знаком. Хуже вкладов — единственное годовое окно: то, что начинается
        днём выхода (издержки 0,2 % и ни дня акций); уже один день акций издержки
        перекрывает.
        Мутация: считать «хуже» по разности двух накопленных произведений → в «хуже
        вкладов» попадают сотни окон по знаку шума (на бою — 29 % вместо 23 %)."""
        dates = weekdays("2010-01-04", self.N)
        tr = [100.0]
        for i in range(1, self.N):
            tr.append(tr[-1] * (1.005 + 0.0001 * ((i * 7919) % 11)))
        pos = [1] * 500 + [0] * (self.N - 500)
        cash = [6.0 + 0.37 * ((i * 31) % 17) for i in range(self.N)]   # неровная ставка
        c = self.E.control(dates, pos, tr, cash)
        w12 = c["windows"][0]
        self.assertEqual(w12["vs_cash_pp"], 0.0)
        self.assertEqual(w12["status"], "ok")
        norm = w12["norm"]
        self.assertEqual(norm["windows"], self.N - 2 - 252 + 1)   # строк счёта N − 2
        self.assertEqual(norm["cash"]["worse_share"], round(1 / norm["windows"], 3))
        self.assertEqual(norm["cash"]["worst_pp"], -0.2)

    def test_правило_всегда_во_вкладах(self):
        dates, tr, cash = self.series(-0.0015)
        c = self.E.control(dates, [0] * self.N, tr, cash)
        self.assertEqual(c["status"], "ok")
        for w in c["windows"]:
            self.assertEqual(w["vs_cash_pp"], 0.0)
            self.assertEqual(w["norm"]["cash"]["worse_share"], 0.0)
            self.assertEqual(w["norm"]["cash"]["worst_pp"], 0.0)

    def test_окна_считаются_по_сказанному_а_норма_по_пересчёту(self):
        """Мутация: считать окна по пересчёту → контроль повторяет таблицу «на истории»,
        которую и должен был исправить."""
        dates, tr, cash = self.series(0.0005)
        said = [1] * (self.N - 200) + [0] * 200                # вживую панель вышла в деньги
        c = self.E.control(dates, [1] * self.N, tr, cash, said_pos=said)
        w12 = c["windows"][0]
        self.assertEqual(c["rule"], "said")
        self.assertNotEqual(w12["rule_pct"], w12["rule_recalc_pct"])
        rows = self.T.daily_rows(dates, said, tr, cash)
        direct = self.T.summarize(rows, w12["from"], w12["to"], 0.0)
        self.assertEqual(w12["rule_pct"], direct["rule_pct"])
        ref = self.E.control(dates, [1] * self.N, tr, cash)["windows"][0]
        self.assertEqual(w12["norm"], ref["norm"], "норма от сказанного не зависит")
        self.assertEqual(w12["rule_recalc_pct"], ref["rule_pct"])

    def test_окно_длиннее_истории_не_публикуется(self):
        """Мутация: считать «36 месяцев» по полутора годам данных под тем же именем."""
        n = 400
        dates = weekdays("2010-01-04", n)
        tr = [100.0 * 1.0005 ** i for i in range(n)]
        c = self.E.control(dates, [1] * n, tr, [8.0] * n)
        self.assertEqual([w["months"] for w in c["windows"]], [12])
        self.assertIsNone(c["windows"][0]["norm"], "скользящих окон меньше 252 — нормы нет")
        self.assertEqual(c["windows"][0]["status"], "ok")

    def test_счёт_по_последний_день_индекса(self):
        dates, tr, cash = self.series(0.0005)
        c = self.E.control(dates, [1] * self.N, tr, cash, tr_last=dates[-2])
        self.assertEqual(c["asof"], dates[-2])
        self.assertEqual(c["windows"][0]["to"], dates[-2])


# ------------------------------------------------------- оценка по текущей нефти
class TestOilNow(Base):
    def setUp(self):
        super().setUp()
        self.syn = synthetic()
        self.pnl, self.mf, self.store = self.syn["panel"], self.syn["mf"], self.syn["store"]

    def test_месяц_в_расчёте_и_его_возраст(self):
        """Панель кончается 28.06.2024; майская цена доступна с 05.06 (лаг 5 дней).
        Мутация: возраст от даты публикации, а не от конца месяца → 23 дня вместо 28."""
        o = self.E.oil_now(self.store, self.pnl, self.mf)
        self.assertEqual(o["leg_month"], "2024-05")
        self.assertEqual(o["leg_age_days"], 28)
        self.assertEqual(o["min_age_days"], self.K.OIL_NOW_MIN_AGE_DAYS)
        urals = self.syn["series"]["urals_tax"]["points"]["2024-05-28"]
        usd = [v for d, v in self.syn["series"]["usd_cbr"]["points"].items() if d[:7] == "2024-05"]
        self.assertEqual(o["leg_barrel_rub"], round(urals * sum(usd) / len(usd)))

    def test_оценка_только_когда_месяц_старше_порога(self):
        """Мутация: заполнять всегда → в начале месяца «текущая нефть» из двух сессий."""
        self.assertIsNone(self.E.oil_now(self.store, self.pnl, self.mf,
                                         min_age_days=28)["estimate"])
        self.assertIsNotNone(self.E.oil_now(self.store, self.pnl, self.mf,
                                            min_age_days=27)["estimate"])

    def test_мало_сессий_в_месяце_оценки_нет(self):
        """Панель обрезана в первые дни июня 2024: сессий с ценой нефти в месяце на одну
        меньше нормы и ровно норма. Возраст майской цены порогом не ограничен (параметр),
        но «средняя с начала месяца» по нескольким дням — шум, а не оценка.
        Мутация: убрать условие на число сессий → оценка появляется и на коротком месяце."""
        from pipeline.compute import core as core_mod
        need = self.K.OIL_NOW_MIN_SESSIONS
        dates = self.pnl["dates"]
        june = [i for i, d in enumerate(dates) if d[:7] == "2024-06"]
        self.assertGreater(len(june), need)
        for keep, expect in ((need - 1, False), (need, True)):
            cut = june[keep - 1] + 1
            pnl = {"dates": dates[:cut], "coverage": self.pnl.get("coverage"),
                   "cols": {k: v[:cut] for k, v in self.pnl["cols"].items()}}
            mf = core_mod.monthly_frame(pnl)
            o = self.E.oil_now(self.store, pnl, mf, min_age_days=0)
            if o is None or o["leg_month"] != "2024-05":
                self.skipTest("на обрезанной панели майская цена ещё не в расчёте")
            self.assertEqual(o["estimate"] is not None, expect, f"сессий в июне: {keep}")

    def test_цена_текущего_месяца_и_оценка_рынка_с_ней(self):
        o = self.E.oil_now(self.store, self.pnl, self.mf)
        est = o["estimate"]
        pts = self.syn["series"]
        urals_may = pts["urals_tax"]["points"]["2024-05-28"]
        usd = pts["usd_cbr"]["points"]
        june = [v for d, v in usd.items() if d[:7] == "2024-06"]
        fx = sum(june) / len(june)
        # Brent: май ровно 80, июнь ровно 60 → отношение налоговой цены к Brent = urals/80.
        # Мутация: взять отношение текущего месяца или забыть его вовсе → другая цена.
        barrel = 60.0 * fx * (urals_may / 80.0)
        self.assertEqual(est["month"], "2024-06")
        self.assertEqual(est["sessions"], len(june))
        self.assertEqual(est["brent_usd"], 60.0)
        self.assertEqual(est["ratio"], round(urals_may / 80.0, 3))
        self.assertEqual(est["barrel_rub"], round(barrel))
        # разрыв: к среднему за 24 месяца — 23 последних известных месяца и оценённый
        rub = []
        for key in sorted(pts["urals_tax"]["points"]):
            m = [v for d, v in usd.items() if d[:7] == key[:7]]
            rub.append(pts["urals_tax"]["points"][key] * sum(m) / len(m))
        tail = rub[-23:] + [barrel]
        gap = math.log(barrel / (sum(tail) / len(tail)))
        self.assertEqual(est["gap"], round(gap, 3))
        # вклад и оценка — нормировка правила: закрытые месяцы окна + значение дня
        raw = self.mf["raw"]["urals_rub_gap"]
        m = [lab[:7] for lab in self.mf["dates"]].index("2024-06")
        prev = [v for v in raw[max(0, m - 59):m] if v is not None]
        mean = (sum(prev) + gap) / (len(prev) + 1)
        sd = math.sqrt((sum((p - mean) ** 2 for p in prev) + (gap - mean) ** 2) / len(prev))
        contrib = -max(-3.0, min(3.0, (gap - mean) / sd))      # знак ноги нефти — минус
        self.assertAlmostEqual(est["contrib"], contrib, places=3)
        legs = self.syn["decision"]["position"]["held_by"]["legs"]
        others = sum(leg["contrib"] for leg in legs if leg["id"] != "urals_rub_gap")
        self.assertAlmostEqual(est["comp_daily_if"], (others + contrib) / 3, places=3)
        self.assertEqual(o["comp_daily"], self.syn["decision"]["position"]["comp_daily"])
        self.assertNotEqual(est["comp_daily_if"], o["comp_daily"])
        self.assertIn("оценка рынка была бы", o["text"])
        self.assertIn("за май 2024", o["text"])
        self.assertIn("По ценам июня", o["text"])

    def test_без_сверки_с_панелью_оценка_не_заполняется(self):
        """Пересчёт разрыва месяца, стоящего в расчёте, не сошёлся с колонкой панели —
        «текущую нефть» не с чем сравнивать. Мутация: убрать сверку."""
        pnl = {"dates": self.pnl["dates"], "coverage": self.pnl.get("coverage"),
               "cols": dict(self.pnl["cols"])}
        pnl["cols"]["urals_rub_gap"] = [None if v is None else v + 0.01
                                        for v in self.pnl["cols"]["urals_rub_gap"]]
        o = self.E.oil_now(self.store, pnl, self.mf)
        self.assertEqual(o["leg_month"], "2024-05")
        self.assertIsNone(o["estimate"])
        self.assertIsNone(o["text"])

    def test_нефти_в_расчёте_нет_блока_нет(self):
        pnl = {"dates": self.pnl["dates"], "cols": dict(self.pnl["cols"])}
        pnl["cols"]["urals_rub_gap"] = [None] * len(pnl["dates"])
        self.assertIsNone(self.E.oil_now(self.store, pnl, self.mf))


class TestCashRatePeriod(Base):
    def test_дата_декады_а_не_последний_день_панели(self):
        """Декада 10.11 доступна с 14.11 (лаг публикации 4 дня из реестра).
        Мутация: брать последнюю точку ряда без лага → 13.11 названа декада, которой в
        панели ещё нет."""
        store = {"deposit_decade": {"points": {"2026-10-31": 13.09, "2026-11-10": 13.3,
                                               "2026-11-20": None}}}
        self.assertEqual(self.E.cash_rate_period(store, ["2026-11-12", "2026-11-13"]),
                         "2026-10-31")
        self.assertEqual(self.E.cash_rate_period(store, ["2026-11-13", "2026-11-16"]),
                         "2026-11-10")
        self.assertEqual(self.E.cash_rate_period(store, ["2026-11-19"]), "2026-11-10")
        self.assertIsNone(self.E.cash_rate_period(store, ["2026-10-01"]))
        self.assertIsNone(self.E.cash_rate_period({}, ["2026-11-19"]))


# -------------------------------------------------------------- сборка и тексты
class TestAttach(Base):
    def setUp(self):
        super().setUp()
        self.syn = synthetic()
        self.decision = copy.deepcopy(self.syn["decision"])

    def frozen(self, decision):
        pos = decision["position"]
        return ({k: pos[k] for k in ("state", "since", "reason", "comp_daily", "comp_state",
                                     "gate_open", "flags", "history", "conditions",
                                     "cash_rate", "cash_rate_asof")},
                decision["daily"]["pos"], decision["daily"]["comp_state"])

    def test_справка_дописана_а_позиция_не_тронута(self):
        """Мутация: любой блок справки меняет ряды решения → снимок до и после расходится."""
        before = copy.deepcopy(self.frozen(self.decision))
        journal_before = copy.deepcopy(self.decision["position"]["journal"])
        extras, errors = self.E.attach(self.syn["store"], self.syn["panel"], self.syn["mf"],
                                       self.decision)
        self.assertEqual(errors, [])
        self.assertEqual(self.frozen(self.decision), before)
        self.assertEqual(set(extras), {"control", "strength", "vote"})
        pos = self.decision["position"]
        self.assertEqual(pos["held_by"]["oil_now"]["leg_month"], "2024-05")
        self.assertIn(pos["held_by"]["by"], ("open", "comp", "gate", "both"))
        self.assertTrue(pos["cash_rate_period"] <= pos["cash_rate_asof"])
        tail = self.syn["days"][-60:]
        self.assertEqual(pos["said_history"], [[tail[0], 0], [tail[40], 1]])
        # прежние поля окон журнала не изменились, добавилось только said
        for old, new in zip(journal_before["windows"], pos["journal"]["windows"]):
            self.assertIn("said", new)
            self.assertEqual({k: v for k, v in new.items() if k != "said"}, old)
        self.assertEqual(extras["control"]["rule"], "said")
        self.assertEqual(self.decision["skills"]["waves"]["said_since"], tail[0])

    def test_отказ_одного_блока_не_прячет_остальные(self):
        """Мутация: общий try на всю справку → падение голосования уносит контроль и силу."""
        def boom(*_a, **_k):
            raise ValueError("сломано")
        orig = self.E.vote
        self.E.vote = boom
        self.addCleanup(setattr, self.E, "vote", orig)
        extras, errors = self.E.attach(self.syn["store"], self.syn["panel"], self.syn["mf"],
                                       self.decision)
        self.assertEqual(set(extras), {"control", "strength"})
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith("голосование: ValueError"))

    def test_без_журнала_контроль_считается_по_пересчёту(self):
        series = {k: v for k, v in self.syn["series"].items() if not k.startswith("said_")}
        extras, errors = self.E.attach(MemStore(series), self.syn["panel"], self.syn["mf"],
                                       self.decision)
        self.assertEqual(errors, [])
        self.assertEqual(extras["control"]["rule"], "recalc")
        self.assertNotIn("said_history", self.decision["position"])
        self.assertNotIn("said", self.decision["position"]["journal"]["windows"][0])

    def test_блоки_едут_в_вердикт_и_влезают_в_лимит(self):
        run = need(self, "pipeline.run", "_attach_evidence", "EVIDENCE_KEYS")
        publish = need(self, "pipeline.publish", "build_payload", "dumps", "MAX_BYTES")
        extras, _ = self.E.attach(self.syn["store"], self.syn["panel"], self.syn["mf"],
                                  self.decision)
        self.decision["evidence"] = dict(extras, чужой={"x": 1})
        payload = publish.build_payload(core={"value": 0.1}, states={}, decision=self.decision,
                                        asof=self.syn["days"][-1],
                                        generated_at="2024-06-28T21:00:00Z")
        self.assertNotIn("vote", payload["verdict"], "publish этих блоков не знает")
        run._attach_evidence(payload, self.decision)
        for key in run.EVIDENCE_KEYS:
            self.assertIn(key, payload["verdict"])
        self.assertNotIn("чужой", payload["verdict"])
        self.assertEqual(payload["verdict"]["position"]["state"],
                         self.decision["position"]["state"])
        # allow_nan=False внутри dumps: NaN в новых блоках уронил бы публикацию
        self.assertLess(len(publish.dumps(payload)), publish.MAX_BYTES)
        # вердикт прошлого прогона (внутридневной такт) и пустое решение — без падений
        self.assertEqual(run._attach_evidence({"verdict": {"a": 1}}, None),
                         {"verdict": {"a": 1}})


class TestTexts(Base):
    """Тексты новых блоков — словами читателя: русские числа и никакой внутренней кухни."""

    JARGON = re.compile(r"\bног[аиуе]?\b|\bногой\b|\bбит\b|композит|ячейк|\bядр[оа]\b|"
                        r"гистерезис|\bz\b|said|recalc", re.I)

    def texts(self):
        syn = synthetic()
        decision = copy.deepcopy(syn["decision"])
        extras, _ = self.E.attach(syn["store"], syn["panel"], syn["mf"], decision)
        out = [w["text"] for w in extras["control"]["windows"]]
        out += [extras["strength"]["text"], extras["vote"]["text"], extras["vote"]["family"],
                decision["position"]["held_by"]["oil_now"]["text"],
                decision["position"]["journal"]["said_basis"], extras["strength"]["measure"]]
        out += [z["range_text"] for z in extras["strength"]["zones"]]
        out += [r["label"] for r in extras["vote"]["by_oil"]]
        out += [leg["text"] for leg in decision["position"]["switch_distance"]["legs"]
                if leg.get("text")]
        return [t for t in out if t]

    def test_без_внутренних_слов_и_с_русскими_числами(self):
        texts = self.texts()
        self.assertGreater(len(texts), 15)
        for t in texts:
            with self.subTest(text=t[:60]):
                self.assertIsNone(self.JARGON.search(t.replace("mean_", "").replace(
                    "median_", "")), t)
                plain = re.sub(r"\d{2}\.\d{2}\.\d{4}", "", t)          # даты дд.мм.гггг
                self.assertIsNone(re.search(r"\d\.\d", plain), "десятичная точка: " + t)
                self.assertIsNone(re.search(r"(?<![\w−])-\d", t), "дефис вместо минуса: " + t)


# ------------------------------------------- сверка с аудитом на копии боевого стора
if HAS_STORE:
    class TestAuditNumbers(unittest.TestCase):
        """Контрольные числа аудита 20.11.2026 (results/a6, a8, a9, a11, agentA).

        Класс объявляется только при STATE_DIR с копией стора; числа — для копии от
        20.11.2026 (последний день панели 19.11.2026), на другой копии тесты пропущены.
        """

        @classmethod
        def setUpClass(cls):
            from pipeline.lib import store
            from pipeline.compute import (panel as P, core as C, decision as D,
                                          evidence as E, track as T, skills as S)
            cls.store, cls.E, cls.T, cls.S = store, E, T, S
            cls.pnl = P.build_panel(store)
            cls.mf = C.monthly_frame(cls.pnl)
            cls.dec = D.compute_decision(cls.pnl, cls.mf)
            cls.daily = cls.dec["daily"]
            cls.cols = cls.pnl["cols"]

        def setUp(self):
            if self.daily["dates"][-1] != "2026-11-19":
                self.skipTest("числа сверки — для копии стора от 20.11.2026")

        def test_счёт_кончается_днём_индекса(self):
            self.assertEqual(self.daily["tr_last"], "2026-11-18")
            self.assertEqual(self.dec["position"]["journal"]["asof"], "2026-11-18")
            self.assertEqual(self.dec["skills"]["waves"]["asof"], "2026-11-18")

        def test_голосование_14_из_27(self):
            v = self.E.vote(self.pnl, self.mf, self.daily)
            self.assertEqual((v["n_long"], v["n_total"]), (14, 27))
            got = {d[:7]: round(s, 2) for d, s in v["monthly"]}
            want = {"2026-01": 0.93, "2026-02": 0.89, "2026-03": 0.41, "2026-04": 0.33,
                    "2026-05": 0.22, "2026-06": 0.0, "2026-07": 0.0, "2026-08": 0.0,
                    "2026-09": 0.85, "2026-10": 0.44, "2026-11": 0.52}
            self.assertEqual({k: got[k] for k in want}, want)

        def test_лестница_силы_сигнала(self):
            s = self.E.strength(self.daily["dates"], self.daily["comp_live"],
                                self.daily["is_decision_day"], self.cols["mcftr"],
                                self.cols["deposit"], self.daily["tr_last"])
            self.assertEqual([z["weeks"] for z in s["zones"]], [56, 175, 96, 148, 154, 115, 126])
            self.assertEqual([z["mean_log_pct"] for z in s["zones"]],
                             [-3.24, -1.41, -1.02, -0.03, 1.21, 0.71, 2.51])
            self.assertEqual([round(z["hit"] * 100) for z in s["zones"]],
                             [21, 46, 52, 58, 64, 63, 70])
            self.assertEqual((s["zone"], s["label"]), ("z3", "слабый"))

        def test_норма_контроля(self):
            c = self.E.control(self.daily["dates"], self.daily["pos"], self.cols["mcftr"],
                               self.cols["deposit"], None, self.daily["tr_last"])
            norm = [w["norm"] for w in c["windows"]]
            # доля окон «хуже вкладов»: у аудита 24 / 16 / 12 % — годовые окна, где правило
            # всё время во вкладах (5,7 %), там делил шум округления; здесь они — ноль.
            self.assertEqual([round(x["cash"]["worse_share"] * 100) for x in norm], [23, 16, 12])
            self.assertEqual([round(x["cash"]["worst_pp"]) for x in norm], [-18, -17, -18])
            self.assertEqual([round(x["tr"]["worse_share"] * 100) for x in norm], [34, 27, 26])

        def test_живой_след_на_базе_помощника(self):
            """Помощник аудита считал по 19.11 с протянутым индексом: 12 мес +8,6,
            24 мес +30,2, с начала года +3,3, с запуска +4,16."""
            said = self.T.said_load(self.store)
            d, pos = self.daily["dates"], self.daily["pos"]
            j = self.T.journal(d, pos, self.daily["reasons"], self.cols["mcftr"],
                               self.cols["deposit"])
            self.T.said_windows(j, d, pos, said, self.cols["mcftr"], self.cols["deposit"])
            got = {w["id"]: (w["rule_pct"], w["said"]["rule_pct"]) for w in j["windows"]}
            self.assertEqual(got["m12"], (14.23, 8.56))
            self.assertEqual(got["m24"], (37.01, 30.2))
            self.assertEqual(got["ytd"], (8.74, 3.34))
            live = self.T.live_journal(d, said, self.cols["mcftr"], self.cols["deposit"])
            self.assertEqual(live["windows"][0]["rule_pct"], 4.16)

        def test_открытая_волна_вживую(self):
            said = self.T.said_load(self.store)
            d = self.daily["dates"]
            w = self.dec["skills"]["waves"]
            self.S.waves_said(w, d, self.cols["mcftr"],
                              self.T.said_positions(d, self.daily["pos"], said), min(said))
            row = w["rows"][-1]
            self.assertEqual(row["from"], "2026-07-17")
            self.assertEqual((row["taken_pct"], row["entry_days"], row["missed_pct"]),
                             (30, 44, 20.3))
            self.assertEqual((row["said_taken_pct"], row["said_entry_days"],
                              row["said_missed_pct"]), (7, 66, 29.0))

        def test_оценка_по_ноябрьской_нефти(self):
            o = self.E.oil_now(self.store, self.pnl, self.mf, min_age_days=0)
            self.assertEqual((o["leg_month"], o["leg_age_days"], o["leg_barrel_rub"]),
                             ("2026-10", 19, 8429))
            est = o["estimate"]
            self.assertEqual(est["barrel_rub"], 6442)
            self.assertEqual(round(est["contrib"], 2), -0.69)
            self.assertEqual(round(est["comp_daily_if"], 2), 0.06)
            self.assertEqual(self.E.cash_rate_period(self.store, self.daily["dates"]),
                             "2026-11-10")


if __name__ == "__main__":
    unittest.main()
