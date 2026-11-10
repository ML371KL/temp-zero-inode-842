"""Сторож замороженного правила (аудит 10.11.2026): позиция не меняется без новой версии.

По позиции панели (verdict.position) снаружи принимают решения на живые деньги, и правка
задним числом меняет и прошлое на витрине, и их поведение: 21.10.2026 одна настройка
флага волатильности превратила «на 8,6 п.п. хуже вкладов за год» в «на 3,8 лучше».
Поэтому всё, что определяет позицию, закреплено двумя отпечатками:

* ПАРАМЕТРЫ — ноги ядра и их знаки, окна и обрезка z, DECISION, пороги STATE_RULES,
  лимиты протяжки и лаги публикации рядов, из которых собраны ноги и ворота, начало
  расчёта, лаг и издержки журнала;
* ПОВЕДЕНИЕ — позиция, причины смен, знак решения и ворота на синтетическом сторе
  (детерминированный генератор ниже, только арифметика без libm), прогнанном через
  build_panel → monthly_frame → compute_decision. Ловит правку КОДА, которую параметры
  не видят: окно 756 дней в panel.py, порядок причин в автомате, такт дней решения.

Как менять правило законно: RULE_VERSION на единицу больше, строка в
constants.RULE_EDITIONS (дата и суть), и в FROZEN ниже — НОВАЯ строка с новыми
отпечатками. Переписывать строку уже выпущенной версии запрещено: это и есть правка
задним числом, ради которой сторож написан. Живой журнал с даты новой версии считает
её с нуля, прежний счёт остаётся (compute/track.py).
"""

import hashlib
import json
import unittest
from datetime import date, timedelta

from tests import need

# версия → (отпечаток параметров, отпечаток поведения). Только дописывать.
FROZEN = {
    # 21.10.2026: флаг волатильности снимается на том же 80-м перцентиле.
    4: ("7d33ff27c205c2d4fc0c79b8d36705a6f973f190287ae5f394aa2a1172a21a8b",
        "376fb299e21626f165c35e2b7fd479fddce35ff3330369f227e19a4e26f6ba2c"),
}


def _sha(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False)
                          .encode("utf-8")).hexdigest()


def rule_params():
    """Всё численное, что определяет позицию и журнал, — в каноническом виде."""
    from pipeline.lib import constants as K, registry
    from pipeline.compute import panel, decision, track
    lags = {sid: (registry.SERIES.get(sid) or {}).get("pub_lag_days")
            for sid in ("imoex", "mcftr", "rgbi", "usd_cbr", "zcyc", "urals_tax",
                        "deposit_decade")}
    return {
        "legs": [(c["id"], c["sign"]) for c in K.CORE_COMPONENTS],
        "z": [K.Z_WINDOW_MONTHS, K.Z_MIN_MONTHS, K.Z_CLIP],
        "decision": K.DECISION,
        "state_rules": {k: {kk: vv for kk, vv in v.items()
                            if kk in ("window", "lookback", "quantile", "threshold")}
                        for k, v in K.STATE_RULES.items() if k in ("trend", "vol", "bond")},
        "ffill": {k: panel.FFILL_LIMITS[k] for k in ("mcftr", "rgbi", "zcyc")},
        "ffill_deposit": panel.DEPOSIT_FFILL_LIMIT,
        "ffill_urals": panel.URALS_FFILL_LIMIT,
        "usd_splice": panel.USD_SPLICE_K,
        "pub_lags": lags,
        "start": decision.START,
        "journal": [track.LAG, track.SWITCH_COST],
    }


# ------------------------------------------------------------- синтетический стор
class _Lcg:
    """Линейный конгруэнтный генератор: одинаков на любой машине и версии Python."""

    def __init__(self, seed):
        self.s = seed

    def u(self):
        self.s = (self.s * 6364136223846793005 + 1442695040888963407) % (1 << 64)
        return (self.s >> 11) / float(1 << 53)

    def n(self):
        # Ирвин — Холл: сумма двенадцати равномерных минус 6 — почти нормальная, без log/cos.
        return sum(self.u() for _ in range(12)) - 6.0


def _weekdays(start, end):
    out, d = [], date.fromisoformat(start)
    stop = date.fromisoformat(end)
    while d <= stop:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


# Фазы рынка синтетики: (с какой даты, дрейф в день, вола в день). Два обвала с
# долговым стрессом — чтобы закрывались ворота, — и длинные спокойные отрезки, в которых
# решает оценка рынка.
_PHASES = [("2009-01-01", 0.0004, 0.010), ("2011-08-01", -0.0040, 0.030),
           ("2011-10-15", 0.0006, 0.012), ("2014-03-01", -0.0012, 0.016),
           ("2014-11-20", -0.0060, 0.040), ("2015-01-20", 0.0012, 0.014),
           ("2018-04-01", -0.0008, 0.020), ("2018-07-01", 0.0005, 0.010),
           ("2020-02-20", -0.0080, 0.045), ("2020-04-10", 0.0020, 0.018),
           ("2021-10-01", -0.0030, 0.022), ("2022-02-20", -0.0150, 0.060),
           ("2022-04-01", 0.0010, 0.016), ("2023-09-01", -0.0004, 0.012)]


def synthetic_store():
    rnd = _Lcg(20261110)
    days = _weekdays("2009-01-05", "2024-06-28")
    px, tr, rg, usd = 1000.0, 1000.0, 100.0, 30.0
    y2, y10 = 7.0, 8.0
    pts = {k: {} for k in ("imoex", "mcftr", "rgbi", "usd_cbr", "zcyc_y2", "zcyc_y10",
                           "deposit_decade", "key_rate", "urals_tax")}
    ph = 0
    urals, month = 60.0, None
    for i, d in enumerate(days):
        iso = d.isoformat()
        while ph + 1 < len(_PHASES) and _PHASES[ph + 1][0] <= iso:
            ph += 1
        drift, vol = _PHASES[ph][1], _PHASES[ph][2]
        stress = vol >= 0.03
        r = drift + vol * rnd.n()
        px *= 1.0 + r
        tr *= 1.0 + r + 0.0002
        rg *= 1.0 + (-0.0025 if stress else 0.0002) + 0.003 * rnd.n()
        usd *= 1.0 + (0.002 if stress else -0.0001) + 0.006 * rnd.n()
        y2 += (0.04 if stress else -0.002) + 0.05 * rnd.n()
        y10 += (0.015 if stress else -0.001) + 0.04 * rnd.n()
        y2, y10 = max(2.0, y2), max(2.0, y10)
        for k, v in (("imoex", px), ("mcftr", tr), ("rgbi", rg), ("usd_cbr", usd),
                     ("zcyc_y2", y2), ("zcyc_y10", y10)):
            pts[k][iso] = round(v, 6)
        if i % 7 == 0:
            pts["deposit_decade"][iso] = round(6.0 + y2 * 0.4, 4)
        if i % 30 == 0:
            pts["key_rate"][iso] = round(y2 - 0.5, 2)
        ym = iso[:7]
        if ym != month:
            if month is not None:
                urals *= 1.0 + 0.06 * rnd.n()
                urals = min(120.0, max(25.0, urals))
                pts["urals_tax"][month + "-28"] = round(urals, 4)
            month = ym
    return {sid: {"id": sid, "points": p, "meta": {}} for sid, p in pts.items()}


def behaviour():
    """Дискретные ряды решения на синтетике: их и закрепляет отпечаток поведения."""
    from pipeline.compute import panel as P, core as C, decision as D
    pnl = P.build_panel(synthetic_store())
    dec = D.compute_decision(pnl, C.monthly_frame(pnl))
    dl = dec["daily"]
    return {"dates": [dl["dates"][0], dl["dates"][-1], len(dl["dates"])],
            "pos": dl["pos"], "reasons": dl["reasons"], "comp_state": dl["comp_state"],
            "gate_open": [bool(x) for x in dl["gate_open"]],
            "bits": {k: dl["bits"][k] for k in ("trend", "vol", "bond")}}


class TestRuleFreeze(unittest.TestCase):
    def setUp(self):
        self.K = need(self, "pipeline.lib.constants", "RULE_VERSION", "RULE_EDITIONS",
                      "RULE_SINCE", "PANEL_LAUNCH")
        need(self, "pipeline.compute.track", "LAG", "SWITCH_COST", "curve_positions")

    def test_версия_и_список_редакций_согласованы(self):
        eds = self.K.RULE_EDITIONS
        self.assertEqual([e["version"] for e in eds], list(range(1, len(eds) + 1)),
                         "версии идут подряд с единицы")
        self.assertEqual([e["since"] for e in eds], sorted(e["since"] for e in eds))
        self.assertEqual(len({e["since"] for e in eds}), len(eds))
        self.assertEqual(self.K.RULE_VERSION, eds[-1]["version"])
        self.assertEqual(self.K.RULE_SINCE, eds[-1]["since"])
        self.assertEqual(eds[0]["since"], self.K.PANEL_LAUNCH)
        for e in eds:
            self.assertTrue(e["what"].strip())
        # Каждая выпущенная версия закреплена отпечатком; новая — новой строкой FROZEN.
        self.assertIn(self.K.RULE_VERSION, FROZEN,
                      "версию подняли, а отпечаток новой версии в FROZEN не дописали")
        self.assertEqual(max(FROZEN), self.K.RULE_VERSION)

    def test_параметры_правила_не_менялись_без_новой_версии(self):
        """Мутация: comp_threshold 0,2 → 0,25, окно z 60 → 48, лимит протяжки RGBI 5 → 7,
        издержки журнала 0,2% → 0,1% — отпечаток другой, тест краснеет, пока не поднята
        версия и не дописана строка FROZEN."""
        got = _sha(rule_params())
        self.assertEqual(got, FROZEN[self.K.RULE_VERSION][0],
                         "правило изменилось без новой версии; новый отпечаток параметров: " + got)

    def test_поведение_правила_не_менялось_без_новой_версии(self):
        """Мутация: окно порога волы 756 → 504 в panel.py, порядок «ворота раньше знака»
        в автомате, день решения четверг вместо пятницы — позиция на синтетике другая."""
        b = behaviour()
        got = _sha(b)
        self.assertEqual(got, FROZEN[self.K.RULE_VERSION][1],
                         "поведение правила изменилось без новой версии; новый отпечаток: " + got)

    def test_синтетика_проверяет_все_ветки_автомата(self):
        # Отпечаток поведения чего-то стоит, только если синтетика проходит через все
        # причины смен: иначе правка ветки, по которой синтетика не ходит, его не сдвинет.
        b = behaviour()
        reasons = {r for r in b["reasons"] if r}
        for why in ("gate_close", "gate_open", "comp_neg", "comp_pos"):
            self.assertIn(why, reasons, f"синтетика не проходит через «{why}»")
        self.assertGreaterEqual(sum(1 for r in b["reasons"] if r), 8)

    def test_нейтральная_полоса_оценки_равна_порогу_решения(self):
        # «−0,28 · нейтрально» рядом с «деньги: оценка ниже −0,2» (аудит 10.11.2026).
        thr = self.K.DECISION["comp_threshold"]
        neutral = [(lo, hi) for lo, hi, w in self.K.CORE_LABELS if w == "нейтрально"]
        self.assertEqual(neutral, [(-thr, thr)])


if __name__ == "__main__":
    unittest.main()
