"""Витрина после аудита 20.11.2026: что показывается, когда поле есть, и что — когда его нет.

Расчётная часть отдала новые поля (docs/CONTRACT.md §3, «Справка вокруг решения»), а
витрина обязана читать их терпимо: любого может не быть — на старом data.json, на
внутридневном такте до первого суточного прогона новой версии, при отказе одного блока.
Ошибиться здесь легко и незаметно: страница не падает, она просто молча показывает не то.
Поэтому решения, которые принимает фронт, вынесены в чистые функции и исполняются
настоящим node — как в tests/test_switch_rows.py:

  * sourceAlerts — плашка об источнике на всю страницу только когда задет ряд РЕШЕНИЯ
    (core_affected); отстал один монитор — плашки нет; поля нет — как раньше;
  * journalRows — в таблице «На истории» первым стоит то, что панель говорила вживую
    (windows[].said), пересчёт — рядом; окно «с начала года» есть; без said — прежняя
    таблица пересчёта в годовых;
  * pricedWindows — окно в пять сессий (show: false) на витрину не идёт;
  * tileKey — число для свёрнутой строки монитора: есть число — печатаем с единицей,
    нет — в строке пусто, а не прочерк и не «undefined»;
  * clipRle — отрезок ленты пересчёта с запуска панели начинается с позиции, которая
    на день запуска уже стояла;
  * decisionLine и note у строки «Оценка рынка» — день решения и оговорка к сдвигам.

И три проверки текста: одно имя у числа («оценка рынка»), подпись у волатильности без
гистерезиса, новые блоки подключены на своих страницах.
"""

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.test_switch_rows import _slice_function

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "web" / "app.js"
CHARTS = ROOT / "web" / "charts.js"


class NodeCase(unittest.TestCase):
    NEED = ()

    def setUp(self):
        if not shutil.which("node"):
            self.skipTest("нет node — проверка исполняет функцию фронта, а не читает её")
        app = APP.read_text(encoding="utf-8")
        charts = CHARTS.read_text(encoding="utf-8")
        parts = [_slice_function(charts, "isNum"), _slice_function(charts, "fmtNum")]
        parts += [_slice_function(app, name) for name in self.NEED]
        self.harness = "\n".join(parts)

    def run_js(self, expr):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "harness.js"
            path.write_text(self.harness + "\nconsole.log(JSON.stringify(%s));" % expr, encoding="utf-8")
            out = subprocess.run(["node", str(path)], capture_output=True, timeout=60)
            if out.returncode:
                self.fail("node не исполнил функцию: " + out.stderr.decode("utf-8", "replace")[:400])
            return json.loads(out.stdout.decode("utf-8"))

    def call(self, fn, *args):
        return self.run_js("%s(%s)" % (fn, ", ".join(json.dumps(a, ensure_ascii=False) for a in args)))


# --------------------------------------------------------------------- источники

def family(status="ok", **over):
    s = {"asof": "2026-11-19", "status": status, "stale_reason": None}
    s.update(over)
    return s


CORE_OK = {"imoex": {"series": "imoex", "status": "ok", "asof": "2026-11-19", "label": "Индекс МосБиржи"},
           "rgbi": {"series": "rgbi", "status": "ok", "asof": "2026-11-19", "label": "Индекс гособлигаций"}}


class TestSourceAlerts(NodeCase):
    NEED = ("sourceAlerts",)

    def test_монитор_отстал_плашки_нет(self):
        """Живой случай 19.11.2026: в семье биржи отстал один ряд ВДО, ряды решения целы.
        Мутация: не читать core_affected — плашка на всю страницу возвращается."""
        src = {"iss": family("stale", asof="2026-11-18", stale_reason="data", core_affected=False,
                             bad_series=[{"series": "rucbhycp_yield", "status": "stale", "core": False}],
                             core_series=CORE_OK),
               "cbr": family(core_affected=False, bad_series=[], core_series={})}
        self.assertEqual(self.call("sourceAlerts", src), {"mute": [], "old": []})

    def test_ряд_решения_отстал_плашка_называет_ряд(self):
        """Мутация: печатать имя семьи («iss») вместо подписи ряда решения."""
        core = json.loads(json.dumps(CORE_OK))
        core["imoex"].update(status="stale", asof="2026-11-17", stale_reason="data")
        src = {"iss": family("stale", asof="2026-11-17", stale_reason="data", core_affected=True, core_series=core)}
        self.assertEqual(self.call("sourceAlerts", src),
                         {"mute": [], "old": ["Индекс МосБиржи (данные от 17.11.2026)"]})

    def test_ряд_решения_молчит(self):
        core = {"usd_cbr": {"series": "usd_cbr", "status": "error", "asof": "2026-11-19", "label": "Официальный курс USD"}}
        src = {"cbr": family("error", core_affected=True, core_series=core)}
        self.assertEqual(self.call("sourceAlerts", src), {"mute": ["Официальный курс USD"], "old": []})

    def test_без_core_series_плашка_по_семье(self):
        """core_affected есть, а списка рядов нет — плашка называет семью словами читателя."""
        src = {"iss": family("stale", asof="2026-11-17", stale_reason="data", core_affected=True)}
        self.assertEqual(self.call("sourceAlerts", src), {"mute": [], "old": ["МосБиржа (данные от 17.11.2026)"]})

    def test_поля_нет_поведение_прежнее(self):
        """Старая витрина: одна запись на семью; «не опрошен» и «отдаёт старое» — разные списки."""
        src = {"iss": family("stale", asof="2026-11-18", stale_reason="data"),
               "cbr": family("error"), "minfin": family("stale", stale_reason="poll"),
               "dividends": family("manual_needed"), "rosstat": family("ok"), "новая_семья": family("error")}
        got = self.call("sourceAlerts", src)
        self.assertEqual(got["old"], ["МосБиржа (данные от 18.11.2026)"])
        self.assertEqual(got["mute"], ["Банк России", "Минфин", "дивидендный календарь", "новая_семья"])

    def test_пустые_источники(self):
        self.assertEqual(self.call("sourceAlerts", None), {"mute": [], "old": []})
        self.assertEqual(self.call("sourceAlerts", {"iss": None}), {"mute": [], "old": []})


# ----------------------------------------------------------------- что дало правило

def window(wid, rule, said=None, **over):
    w = {"id": wid, "rule_pct": rule, "curve_pct": 1.0, "tr_pct": 2.0, "cash_pct": 3.0,
         "rule_ann_pct": rule + 0.5, "curve_ann_pct": 1.5, "tr_ann_pct": 2.5, "cash_ann_pct": 3.5}
    if said is not None:
        w["said"] = None if said == "null" else {"rule_pct": said, "live_days": 70}
    w.update(over)
    return w


class TestJournalRows(NodeCase):
    NEED = ("journalRows",)

    def test_вживую_первым_пересчёт_рядом(self):
        """Числа аудита: 12 месяцев — +11,8% вживую против +17,7% пересчётом.
        Мутация: поставить в rule пересчёт — таблица снова выдаёт пересчёт за историю панели."""
        wins = [window("since_change", 0.1, 0.1), window("ytd", 8.7, 3.3),
                window("m12", 17.66, 11.81), window("m24", 37.04, 30.23)]
        got = self.call("journalRows", wins)
        self.assertTrue(got["said"])
        self.assertEqual([r["label"] for r in got["rows"]], ["с начала года", "12 мес.", "24 мес."])
        self.assertEqual([(r["rule"], r["recalc"]) for r in got["rows"]],
                         [(3.3, 8.7), (11.81, 17.66), (30.23, 37.04)])
        # итог за окно, не годовые: столбцы рядом — накопленные проценты
        self.assertEqual((got["rows"][0]["curve"], got["rows"][0]["tr"], got["rows"][0]["cash"]), (1.0, 2.0, 3.0))

    def test_окно_без_дня_вживую_остаётся_пересчётом(self):
        """said = null — в окно не попало ни одного дня живого журнала: число одно, без скобок."""
        got = self.call("journalRows", [window("ytd", 8.7, "null"), window("m12", 17.66, 11.81)])
        self.assertTrue(got["said"])
        self.assertEqual((got["rows"][0]["rule"], got["rows"][0]["recalc"]), (8.7, None))
        self.assertEqual((got["rows"][1]["rule"], got["rows"][1]["recalc"]), (11.81, 17.66))

    def test_без_said_прежняя_таблица(self):
        """Старая витрина: пересчёт за 12 и 24 месяца в годовых, окна «с начала года» нет."""
        got = self.call("journalRows", [window("ytd", 8.7), window("m12", 17.66), window("m24", 37.04)])
        self.assertFalse(got["said"])
        self.assertEqual([r["id"] for r in got["rows"]], ["m12", "m24"])
        self.assertEqual(got["rows"][0]["rule"], 18.16)
        self.assertEqual(got["rows"][0]["cash"], 3.5)
        self.assertIsNone(got["rows"][0]["recalc"])

    def test_пусто(self):
        self.assertEqual(self.call("journalRows", []), {"said": False, "rows": []})
        self.assertEqual(self.call("journalRows", None), {"said": False, "rows": []})


class TestPricedWindows(NodeCase):
    NEED = ("pricedWindows",)

    def test_окно_неделя_не_показывается(self):
        """Мутация: читать все окна — возвращается строка «неделя» по пяти точкам."""
        q = {"windows": [{"sessions": 5, "show": False}, {"sessions": 21, "show": True}, {"sessions": 63, "show": True}]}
        self.assertEqual([w["sessions"] for w in self.call("pricedWindows", q)], [21, 63])

    def test_без_пометки_показываются_все(self):
        """Старая витрина без show — окна как были."""
        q = {"windows": [{"sessions": 5}, {"sessions": 21}, None]}
        self.assertEqual([w["sessions"] for w in self.call("pricedWindows", q)], [5, 21])
        self.assertEqual(self.call("pricedWindows", {}), [])
        self.assertEqual(self.call("pricedWindows", None), [])


# ------------------------------------------------------------------------ мониторы

class TestTileKey(NodeCase):
    NEED = ("tileKey",)

    def key(self, tid, payload):
        return self.call("tileKey", {"id": tid, "payload": payload})

    def test_число_с_единицей(self):
        self.assertEqual(self.key("deposit_spread", {"spread_pp": -4.63}), "−4,6 п.п.")
        self.assertEqual(self.key("expectations", {"spread_y1_key_pp": 0.15}), "+0,15 п.п.")
        self.assertEqual(self.key("orfr", {"last": {"fiz": -9.2}}), "−9,2 млрд ₽")
        self.assertEqual(self.key("futoi", {"z120": -0.06}), "−0,06")
        self.assertEqual(self.key("mcxsm", {"rs_63d_pct": -4.2}), "−4,2%")
        self.assertEqual(self.key("cb_meeting", {"days_left": 28}), "28 дн.")
        self.assertEqual(self.key("sep_node", {"active": False, "days_to_start": 294}), "294 дн.")
        self.assertEqual(self.key("sep_node", {"active": True, "days_to_start": 0}), "идёт")

    def test_нет_числа_в_строке_пусто(self):
        """Мутация: fmtNum без проверки — в строке «— млрд ₽» или «undefined»."""
        for tid in ("hy_spread", "ofz_auctions", "polymarket", "dividends", "cb_meeting", "retail",
                    "lqdt", "rvi", "breadth", "rub_barrel", "orfr", "expectations", "futoi"):
            with self.subTest(tile=tid):
                self.assertIsNone(self.key(tid, {}))
        self.assertIsNone(self.call("tileKey", {"id": "orfr"}))
        self.assertIsNone(self.key("новый_тайл", {"value": 5}))

    def test_несостоявшийся_аукцион_словом(self):
        """Провал аукциона — не «0,0 млрд ₽»."""
        self.assertEqual(self.key("ofz_auctions", {"failed": True, "placed_bn": 0.0}), "не состоялся")
        self.assertEqual(self.key("ofz_auctions", {"failed": False, "placed_bn": 70.6}), "70,6 млрд ₽")


class TestTileCharts(unittest.TestCase):
    """График тайла рисует ту же величину, что число в строке (ряды с 20.11.2026), а при их
    отсутствии — прежний ряд под прежним именем. Мутация: оставить p.series с подписью «спред»."""

    def setUp(self):
        self.app = APP.read_text(encoding="utf-8")
        self.body = _slice_function(self.app, "tileBody")

    def case(self, tid):
        m = re.search(r"case '%s':(.*?)\n        break;" % tid, self.body, re.S)
        self.assertIsNotNone(m, f"в tileBody нет ветки {tid}")
        return m.group(1)

    def test_три_тайла_берут_новый_ряд_и_умеют_без_него(self):
        for tid, key in (("deposit_spread", "spread_series"), ("futoi", "z120_series"), ("mcxsm", "rs63_series")):
            with self.subTest(tile=tid):
                code = self.case(tid)
                self.assertIn("pick('%s')" % key, code)
                self.assertIn("p.%s" % key, code)
                self.assertIn("else if (p.series)", code, "нет запасного ряда для старой витрины")

    def test_запасной_ряд_вкладов_не_назван_спредом(self):
        code = self.case("deposit_spread")
        fallback = code[code.index("else if (p.series)"):]
        self.assertIn("ставка вкладов", fallback)
        self.assertNotIn("спред", fallback)

    def test_тексты_пяти_тайлов_не_зашиты(self):
        """ВДО, аукционы, Polymarket, заседание ЦБ печатают заголовок конвейера, а не фразу фронта.
        Мутация: вернуть «спрос — млрд, bid-to-cover —»."""
        for tid in ("hy_spread", "ofz_auctions", "polymarket", "cb_meeting"):
            with self.subTest(tile=tid):
                self.assertIn("headline()", self.case(tid))
        shown = code_only(self.body)
        self.assertNotIn("bid-to-cover", shown)
        self.assertNotIn("консенсус не внесён", shown)
        auctions = self.case("ofz_auctions")
        self.assertIn("isNum(p.demand_bn)", auctions)
        retail = self.case("retail")
        self.assertIn("isNum(p.clients_total_mln)", retail)


# ------------------------------------------------------------------ лента и позиция

class TestClipRle(NodeCase):
    NEED = ("clipRle",)

    def test_отрезок_с_запуска_начинается_со_стоявшей_позиции(self):
        """Пересчёт на день запуска 11.08 стоял в деньгах с мая; дальше — его смены.
        Мутация: начать с первой смены после запуска — левый край полосы пуст."""
        rle = [["2026-03-13", 1], ["2026-05-22", 0], ["2026-09-15", 1], ["2026-09-16", 0],
               ["2026-09-18", 1], ["2026-11-06", 0]]
        self.assertEqual(self.call("clipRle", rle, "2026-08-11"),
                         [["2026-08-11", 0], ["2026-09-15", 1], ["2026-09-16", 0], ["2026-09-18", 1], ["2026-11-06", 0]])

    def test_смена_в_день_запуска(self):
        self.assertEqual(self.call("clipRle", [["2026-01-10", 0], ["2026-08-11", 1]], "2026-08-11"), [["2026-08-11", 1]])

    def test_история_позже_запуска_и_пустая(self):
        self.assertEqual(self.call("clipRle", [["2026-09-01", 1]], "2026-08-11"), [["2026-09-01", 1]])
        self.assertEqual(self.call("clipRle", [], "2026-08-11"), [])
        self.assertEqual(self.call("clipRle", None, "2026-08-11"), [])


class TestDecisionLine(NodeCase):
    NEED = ("ruText", "ruDay", "weekdayOf", "decisionLine")

    def setUp(self):
        super().setUp()
        self.harness = ("var WEEKDAY = ['воскресенье', 'понедельник', 'вторник', 'среда', 'четверг', "
                        "'пятница', 'суббота'];\n" + self.harness)

    def test_день_решения_и_исполнение(self):
        """Торгового календаря на странице нет: дата исполнения не выдумывается."""
        got = self.call("decisionLine", {"next_decision": "2026-11-20", "execute": "на следующем закрытии"})
        self.assertEqual(got, "День решения — 20.11 (пятница); исполнение — на следующем закрытии.")

    def test_без_дня_решения_строки_нет(self):
        self.assertIsNone(self.call("decisionLine", {"execute": "на следующем закрытии"}))


class TestSwitchNote(NodeCase):
    NEED = ("ruDay", "plural", "switchRows")

    DISTANCES = [
        {"id": "trend", "value": -2.59, "threshold": 0.0, "on_threshold": 2.0, "off_threshold": -2.0},
        {"id": "vol", "value": 18.1, "threshold": 28.3, "off_threshold": 28.3},
        {"id": "bond", "value": -7.9, "threshold": -3.9, "off_threshold": -3.0},
    ]

    def position(self, sd):
        return {"state": "flat", "gate_open": True, "comp_state": -1, "comp_daily": -0.2462, "comp_threshold": 0.2,
                "next_decision": "2026-11-20", "flags": {"trend": 0, "vol": 0, "bond": 1}, "switch_distance": sd}

    def test_оговорка_к_сдвигам_идёт_рядом(self):
        """Живая витрина 19.11.2026. Мутация: потерять legs_note — сдвиги читаются как три
        независимых условия, хотя показатели двигаются вместе."""
        sd = {"comp_target": 0.2, "legs_note": "каждый сдвиг — при неизменных остальных",
              "legs": [{"id": "usd_mom63", "text": "доллар на 20% дороже, чем сейчас"},
                       {"id": "urals_rub_gap", "text": "нефть в рублях на 33% дешевле, чем сейчас в расчёте"}]}
        row = self.call("switchRows", self.position(sd), self.DISTANCES)["rows"][-1]
        self.assertEqual(row["k"], "Оценка рынка")
        self.assertEqual(row["hint"], "хватило бы одного: доллар на 20% дороже, чем сейчас; "
                                      "нефть в рублях на 33% дешевле, чем сейчас в расчёте")
        self.assertEqual(row["note"], "каждый сдвиг — при неизменных остальных")

    def test_без_сдвигов_оговорки_нет(self):
        for sd in ({"comp_target": 0.2, "legs": [], "legs_note": "каждый сдвиг — при неизменных остальных"},
                   {"comp_target": 0.2, "legs": [{"id": "usd_mom63", "text": "доллар на 20% дороже"}]},
                   {"comp_target": 0.2, "legs": [{"id": "usd_mom63", "text": None}], "legs_note": None}):
            with self.subTest(sd=sd):
                row = self.call("switchRows", self.position(sd), self.DISTANCES)["rows"][-1]
                self.assertIsNone(row["note"])


class TestSentence(NodeCase):
    NEED = ("capFirst", "sentence")

    def test_фраза_конвейера_становится_предложением(self):
        self.assertEqual(self.run_js("sentence(capFirst('остаток — всё, что не описали ставки и нефть'))"),
                         "Остаток — всё, что не описали ставки и нефть.")
        self.assertEqual(self.run_js("sentence('Уже с точкой.')"), "Уже с точкой.")
        self.assertEqual(self.run_js("[sentence(null), sentence(''), capFirst(null)]"), ["", "", ""])


# ----------------------------------------------------------------------- тексты

def code_only(js):
    """Исходник без комментариев: проверяются слова, которые попадают на экран."""
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return "\n".join(line for line in js.splitlines() if not line.strip().startswith("//"))


class TestWording(unittest.TestCase):
    def setUp(self):
        self.app = APP.read_text(encoding="utf-8")
        self.code = code_only(self.app)

    def test_одно_имя_у_числа(self):
        """Аудит 20.11.2026: −0,25 носило пять имён. На главной — только «оценка рынка».
        Мутация: вернуть плашку «Цикл ставки» или подпись «по наклону»."""
        main = "\n".join(code_only(_slice_function(self.app, fn)) for fn in (
            "renderPosition", "renderTrust", "strengthBody", "voteBody", "journalTables", "renderRibbon",
            "renderPlaques", "renderSwitch", "switchRows", "decisionLine", "positionHolds",
            "renderGuides", "renderSkills", "renderPriced", "renderContext", "monitorRows", "renderHowto",
            "renderDistances", "renderEvents", "renderTable", "renderBanners", "sourceAlerts"))
        for word in ("Цикл ставки", "Тормоз", "аклон", "омпозит", "ядр", "чейк", " нога", " ноги", " бит"):
            with self.subTest(word=word):
                found = [m.start() for m in re.finditer(re.escape(word), main)]
                # «наклон кривой ОФЗ» — имя показателя, оно остаётся
                found = [i for i in found if "кривой" not in main[i:i + 30] and "кривая" not in main[max(0, i - 30):i]]
                self.assertEqual(found, [], f"на главной осталось слово «{word.strip()}»: " +
                                 "; ".join(main[max(0, i - 40):i + 40].replace("\n", " ") for i in found[:3]))
        self.assertIn("text: 'Оценка рынка'", _slice_function(self.app, "renderPlaques"))
        self.assertIn("Вклады показателей; оценка — их среднее", _slice_function(self.app, "renderPlaques"))

    def test_версия_правила_и_смены_в_год_ушли_в_лабораторию(self):
        pos = code_only(_slice_function(self.app, "renderPosition"))
        self.assertNotIn("switches_per_year", pos)
        self.assertNotIn("rule_version", pos)
        ctl = _slice_function(self.app, "renderControl")
        self.assertIn("p.switches_per_year", ctl)
        self.assertIn("p.rule_version", ctl)

    def test_ставка_вкладов_подписана_своей_датой(self):
        """Мутация: вернуть cash_rate_asof — ставка от 10.11 снова подписана 19.11."""
        self.assertNotIn("cash_rate_asof", self.code)
        self.assertIn("p.cash_rate_period ? ' на ' + fmtDay(p.cash_rate_period)", self.code)

    def test_волатильность_без_гистерезиса(self):
        """Гистерезис у флага волатильности снят 21.10.2026 (версия 4 правила)."""
        table = _slice_function(self.app, "renderTable")
        self.assertIn("k === 'vol' ? ' (один порог)' : ' (с гистерезисом)'", table)

    def test_остаток_не_выдаётся_за_знание(self):
        priced = code_only(_slice_function(self.app, "renderPriced"))
        self.assertIn("q.other_note", priced)
        self.assertNotIn("Объяснено", priced)
        for word in ("переговор", "санкци", "потоки", "дивиденды"):
            self.assertNotIn(word, priced)

    def test_контроль_в_деньгах_на_месте_здоровья(self):
        """Ранговая связь — свёрнутой строкой без статуса и цвета; статус — у счёта в деньгах."""
        ctl = _slice_function(self.app, "renderControl")
        self.assertIn("v.control", ctl)
        for status in ("ok:", "lag:", "beyond:"):
            self.assertIn(status, self.app[self.app.index("var CONTROL_WORD"):self.app.index("function renderControl")])
        ic = ctl[ctl.index("if (hasIc)"):]
        self.assertIn("h('details'", ic)
        self.assertNotIn("toneOf(hl.ic_24m)", ic)
        self.assertNotIn("hl.status", ctl)
        self.assertNotIn("связь различима", self.code)

    def test_пустая_карточка_тени_не_рисуется(self):
        self.assertIn("s.status !== 'no_series'", _slice_function(self.app, "renderShadow"))

    def test_плашка_о_сорванном_расчёте(self):
        banners = _slice_function(self.app, "renderBanners")
        self.assertIn("d.compute_error", banners)
        self.assertIn("Расчёт сегодня не прошёл", banners)

    def test_журнал_не_зависит_от_вида_события(self):
        """Новые виды (position_watch, urals_month, …) печатаются как обычные записи:
        в отрисовке журнала нет ни одной развилки по kind."""
        events = code_only(_slice_function(self.app, "renderEvents"))
        self.assertNotRegex(events, r"e\.kind\s*(===|!==|==)")
        self.assertNotIn("switch (e.kind", events)


if __name__ == "__main__":
    unittest.main()
