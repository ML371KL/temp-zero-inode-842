"""ops/rebuild_orfr_history.py: история потоков ОРФР 2023–2025 по таблицам ЦБ.

Аудит 20.11.2026 (раздел 7, п. 1): затравка за эти годы не сходилась с таблицей ЦБ —
пропуски, перепутанные НФО и доверительное управление в сентябре 2025, месяцы из
пересказа на smart-lab. ЦБ к тому же пересчитал историю по новой методике и выложил её
только поквартально. Здесь закреплено, что:
* все три вёрстки приложений ЦБ читаются, «Дочерние иностранные организации» уходят в
  прочие банки, «Нефинансовые организации» не попадают никуда;
* месяц берётся из самой свежей помесячной таблицы, квартал — из самой свежей
  квартальной, и среднее трёх месяцев равно кварталу ЦБ;
* затравка в репозитории за 2023–2025 полная и сходится с кварталами ЦБ;
* перенос в стор трогает только 2023–2025, по умолчанию ничего не пишет, кладёт копию
  ряда и при повторе ничего не меняет.

Книги собираются на месте (tests.test_xlsx_orfr.build_workbook); в сеть не ходим.
"""

import contextlib
import importlib.util
import io
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests import ROOT, need
from tests.test_xlsx_orfr import build_workbook

CATS = ("fiz", "nfo_du", "nfo_own", "szko", "other_banks", "nonres")


def load_module():
    path = ROOT / "ops" / "rebuild_orfr_history.py"
    spec = importlib.util.spec_from_file_location("rebuild_orfr_history", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Скользящее окно с датой Excel (вёрстка 2023 — середины 2024): впереди пустая колонка,
# есть «Дочерние иностранные организации» и «Нефинансовые организации». Числа —
# из charts_ORFR_2024-06.xlsx (лист «рис. 16»), май и июнь 2023, округлены до сотых
# (в файле они с шестью знаками).
OLD_ROLLING = [
    ["Покупки (+) /продажи (-) акций по категориям участников (млрд руб.)"],
    ["Источник: ПАО Московская Биржа."],
    [None, "Дата", "Нерезиденты", "Дочерние иностранные организации", "НФО",
     "Прочие Банки", "СЗКО", "Физические лица", "Доверительное управление",
     "Нефинансовые организации"],
    [None, 45047.0, -0.65, 0.21, -0.84, -2.54, -0.96, 14.9, -7.16, -2.96],
    [None, 45078.0, -3.29, -0.21, 1.35, -3.53, 0.51, 13.06, -2.57, -5.33],
]

# «Годы» + «Месяцы/кварталы» (с сентября 2025): кварталы — среднемесячные, будущие
# месяцы пустые. Числа — из ORFR_2025-9.XLSX (лист «рис. 30»).
QUARTERLY = [
    ["Покупки /продажи акций по категориям участников на вторичных биржевых торгах "
     "(в среднем в месяц), млрд руб."],
    ["Источник: ПАО Московская Биржа."],
    ["Годы", "Месяцы/кварталы", "Нерезиденты", "НФО", "Прочие Банки", "СЗКО",
     "Физические лица", "Доверительное управление", "Нефинансовые организации"],
    [2025, "II квартал", 2.54, -12.05, -8.41, 7.81, 18.28, -8.87, 0.71],
    [None, "Июль", -8.47, -17.03, 5.17, 23.46, 15.24, -18.02, -0.36],
    [None, "Август", 0.57, -9.75, 1.19, -4.38, 5.68, 2.4, 4.3],
    [None, "Сентябрь", -4.11, -29.81, -7.48, 18.93, 43.1, -17.63, -3.01],
    [None, "Октябрь"],
]


class ReadTableCase(unittest.TestCase):
    def setUp(self):
        need(self, "pipeline.fetch.orfr", "equity_sheet", "COLUMN_TITLES")
        self.mod = load_module()

    def test_окно_с_датой_excel_и_дочерними_иностранными(self):
        months, quarters = self.mod.read_table(build_workbook(OLD_ROLLING, "рис. 16"))
        self.assertEqual(sorted(months), ["2023-05", "2023-06"])
        self.assertEqual(quarters, {})
        may = months["2023-05"]
        self.assertEqual(may["fiz"], 14.9)
        self.assertEqual(may["nfo_du"], -7.16)
        self.assertEqual(may["nfo_own"], -0.84)
        # мутация: потерять колонку дочерних иностранных -> прочие банки −2,54 вместо
        # −2,33, и квартал не сходится с поздними таблицами ЦБ, где её уже нет.
        self.assertEqual(may["other_banks"], -2.33)
        self.assertEqual(months["2023-06"]["other_banks"], -3.74)
        self.assertEqual(set(may), set(CATS), "нефинансовые организации панель не ведёт")

    def test_кварталы_и_месяцы_разделены_пустые_месяцы_пропущены(self):
        months, quarters = self.mod.read_table(build_workbook(QUARTERLY, "рис. 30"))
        self.assertEqual(sorted(months), ["2025-07", "2025-08", "2025-09"])
        self.assertEqual(sorted(quarters), ["2025-Q2"])
        self.assertEqual(quarters["2025-Q2"]["szko"], 7.81)
        # Сентябрь 2025 — тот месяц, где в затравке были перепутаны НФО и ДУ.
        self.assertEqual(months["2025-09"]["nfo_own"], -29.81)
        self.assertEqual(months["2025-09"]["nfo_du"], -17.63)

    def test_квартал_подписью_1к(self):
        rows = [["Среднемесячные покупки/продажи акций по категориям участников, млрд руб."],
                [], ["Годы", "Месяцы/кварталы", "Нерезиденты", "НФО", "Прочие банки",
                     "СЗКО", "Физические лица", "Доверительное управление"],
                [2024, "1к", -3.9, 2.51, 5.95, 9.73, -8.37, -8.09],
                [None, "4к", -4.34, 19.04, 11.28, 0.01, -30.27, 1.24]]
        _months, quarters = self.mod.read_table(build_workbook(rows, "РА По участникам"))
        self.assertEqual(sorted(quarters), ["2024-Q1", "2024-Q4"])
        self.assertEqual(quarters["2024-Q4"]["fiz"], -30.27)


class RebuildCase(unittest.TestCase):
    def setUp(self):
        need(self, "pipeline.fetch.orfr", "equity_sheet")
        self.mod = load_module()
        one = {c: 0.0 for c in CATS}
        # Старая таблица: апрель–июнь; новая: тот же июнь иначе и июль, плюс квартал.
        # Имена нарочно идут в обратном порядке: у ЦБ бывает ORFR_2024-25-1, и по имени
        # свежесть таблицы не определить — её задаёт последний месяц.
        self.old = ("z_older.xlsx",
                    {"2025-04": dict(one, szko=16.28), "2025-05": dict(one, szko=3.29),
                     "2025-06": dict(one, szko=-1.0)}, {})
        self.new = ("a_newer.xlsx", {"2025-06": dict(one, szko=-7.1),
                                     "2025-07": dict(one, szko=23.46)}, {})
        self.quarterly = ("ORFR_2026-10.xlsx", {"2026-10": dict(one)},
                          {"2025-Q2": dict(one, szko=7.81)})

    def test_свежая_таблица_побеждает_по_содержимому_а_не_по_имени(self):
        base, src, official, osrc = self.mod.collect([self.new, self.quarterly, self.old])
        # мутация: упорядочить таблицы по имени -> июнь из старой таблицы (−1,0).
        self.assertEqual(base["2025-06"]["szko"], -7.1)
        self.assertEqual(src["2025-06"], "a_newer.xlsx")
        self.assertEqual(src["2025-04"], "z_older.xlsx")
        self.assertEqual(official["2025-Q2"]["szko"], 7.81)
        self.assertEqual(osrc["2025-Q2"], "ORFR_2026-10.xlsx")

    def test_квартал_равен_цб_а_рисунок_месяцев_сохраняется(self):
        base, _src, official, _osrc = self.mod.collect([self.old, self.new, self.quarterly])
        out, shifts, absent = self.mod.rebuild(base, official, "2025-04", "2025-08")
        self.assertEqual(absent, ["2025-08"])
        self.assertEqual(out["2025-07"]["szko"], 23.46, "квартала ЦБ нет — месяц как есть")
        szko = [out[m]["szko"] for m in ("2025-04", "2025-05", "2025-06")]
        self.assertAlmostEqual(sum(szko) / 3, 7.81, delta=0.01)
        # мутация: поставить всем трём месяцам среднее квартала -> рисунок месяцев
        # потерян, разница соседних месяцев не та, что публиковал ЦБ.
        self.assertAlmostEqual(szko[0] - szko[1], 16.28 - 3.29, delta=0.011)
        self.assertAlmostEqual(shifts["2025-Q2"]["szko"], 7.81 - (16.28 + 3.29 - 7.1) / 3,
                               delta=0.01)

    def test_без_квартала_цб_месяц_остаётся_как_есть(self):
        base, _src, official, _osrc = self.mod.collect([self.old, self.new])
        out, shifts, _absent = self.mod.rebuild(base, official, "2025-04", "2025-06")
        self.assertEqual(out["2025-04"]["szko"], 16.28)
        self.assertEqual(shifts, {})


class SeedMatchesCbrCase(unittest.TestCase):
    """Затравка в репозитории: 2023–2025 полные и сходятся с кварталами ЦБ."""

    # Среднемесячные за квартал из таблиц ЦБ: 2023 — ORFR_2025-11.xlsx («рис. 21»),
    # 2024–2025 — ORFR_2026-10.xlsx («РА По участникам»). Порядок как у ЦБ:
    # нерезиденты, НФО, прочие банки, СЗКО, физлица, доверительное управление.
    CBR = {
        "2023-Q1": (-2.65, -5.53, -1.19, 0.96, 8.77, 0.18),
        "2023-Q2": (-1.62, -3.6, -1.29, -3.81, 19.41, -7.2),
        "2023-Q3": (-14.05, 6.06, -0.18, 0.64, 23.59, -16.29),
        "2023-Q4": (-18.37, 14.01, 5.02, -1.26, 4.68, -6.36),
        "2024-Q1": (-3.9, 2.51, 5.95, 9.73, -8.37, -8.09),
        "2024-Q2": (-6.57, -20.85, -9.63, 17.81, 3.44, 16.66),
        "2024-Q3": (-15.21, 0.07, 9.19, 8.67, -5.32, -3.01),
        "2024-Q4": (-4.34, 19.04, 11.28, 0.01, -30.27, 1.24),
        "2025-Q1": (1.86, -5.34, -16.97, 8.5, 10.71, 3.29),
        "2025-Q2": (2.54, -11.46, -8.41, 7.81, 18.28, -8.87),
        "2025-Q3": (-4.0, -18.58, -0.37, 12.67, 21.34, -11.08),
        "2025-Q4": (-5.79, -6.58, -0.11, -2.2, -2.86, 20.26),
    }
    ORDER = ("nonres", "nfo_own", "other_banks", "szko", "fiz", "nfo_du")

    def setUp(self):
        need(self, "pipeline.fetch.orfr", "CATEGORIES")
        self.mod = load_module()
        self.rows = self.mod.seed_rows(self.mod.SEED, "2023-01", "2025-12")

    def test_все_месяцы_и_категории_есть(self):
        self.assertEqual(len(self.rows), 36)
        for month, values in self.rows.items():
            self.assertEqual(set(values), set(CATS), month)

    def test_кварталы_сходятся_с_таблицами_цб(self):
        for quarter, levels in self.CBR.items():
            trio = self.mod._quarter_months(quarter)
            for cat, level in zip(self.ORDER, levels):
                mean = sum(self.rows[m][cat] for m in trio) / 3
                self.assertAlmostEqual(mean, level, delta=0.01, msg="%s %s" % (quarter, cat))

    def test_сентябрь_2025_без_перестановки_нфо_и_ду(self):
        self.assertEqual(self.rows["2025-09"]["nfo_du"], -17.63)
        self.assertNotEqual(self.rows["2025-09"]["nfo_own"], -17.6)


class WriteSeedCase(unittest.TestCase):
    def setUp(self):
        self.mod = load_module()
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "orfr_flows.csv"
        self.text = ("month,fiz,nfo_du,nfo_own,szko,other_banks,nonres,src\n"
                     "2022-12,8.9,-1.9,-2.3,1.2,-1.9,-2.3,https://x/ORFR_2022-11.pdf\n"
                     "2023-01,5.4,1.1,0.5,1.0,-4.4,-2.8,https://x/ORFR_2023-01.pdf\n"
                     "2026-07,24.43,-37.85,12.86,-22.68,-0.36,15.29,https://x/ORFR_2026-7.xlsx\n")
        self.path.write_text(self.text, encoding="utf-8", newline="")

    def test_переписаны_только_месяцы_пересборки(self):
        new = {"2023-01": {"fiz": 5.41, "nfo_du": 1.06, "nfo_own": 0.52, "szko": 1.02,
                           "other_banks": -4.35, "nonres": -2.76},
               "2023-02": {"fiz": 3.86, "nfo_du": -1.14, "nfo_own": -2.17, "szko": 0.38,
                           "other_banks": 0.33, "nonres": -3.55}}
        n = self.mod.write_seed(new, {"2023-01": "cbr a.xlsx", "2023-02": "cbr a.xlsx"},
                                self.path)
        self.assertEqual(n, 2)
        lines = self.path.read_text(encoding="utf-8").splitlines()
        old = self.text.splitlines()
        self.assertEqual(lines[0], old[0])
        self.assertEqual(lines[1], old[1], "2022 год не трогается")
        self.assertEqual(lines[2], "2023-01,5.41,1.06,0.52,1.02,-4.35,-2.76,cbr a.xlsx")
        self.assertEqual(lines[3].split(",")[0], "2023-02", "новый месяц встаёт по порядку")
        self.assertEqual(lines[4], old[3], "2026 год не трогается")


class ApplyCase(unittest.TestCase):
    """Перенос строк затравки в стор: только 2023–2025, копия, повтор без изменений."""

    def setUp(self):
        self.store = need(self, "pipeline.lib.store", "upsert_points", "load_series")
        self.mod = load_module()
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        prev = os.environ.get("STATE_DIR")
        os.environ["STATE_DIR"] = self.tmp.name
        self.addCleanup(lambda: os.environ.pop("STATE_DIR", None) if prev is None
                        else os.environ.__setitem__("STATE_DIR", prev))
        seed = Path(self.tmp.name) / "orfr_flows.csv"
        seed.write_text(
            "month,fiz,nfo_du,nfo_own,szko,other_banks,nonres,src\n"
            "2022-12,8.9,-1.9,-2.3,1.2,-1.9,-2.3,x\n"
            "2025-09,43.1,-17.63,-29.53,18.93,-7.48,-4.11,cbr ORFR_2025-9.XLSX\n"
            "2025-11,-22.33,19.82,10.59,-5.68,-0.85,-2.99,cbr ORFR_2025-12_2026-01.xlsx\n"
            "2026-01,2.44,-10.42,12.38,-12.9,1.87,5.34,x\n", encoding="utf-8")
        self.mod.SEED = seed
        # Боевые ряды: сентябрь 2025 с перестановкой НФО/ДУ, ноябрь у СЗКО пуст,
        # 2022 и 2026 расходятся с затравкой — их пересборка трогать не должна.
        for cat, points in {
                "nfo_du": {"2022-12-31": -1.0, "2025-09-30": -29.8, "2025-11-30": 19.8,
                           "2026-01-31": -10.0},
                "nfo_own": {"2025-09-30": -17.6, "2025-11-30": 16.0},
                "fiz": {"2025-09-30": 43.1, "2025-11-30": -22.33},
                "szko": {"2025-09-30": 18.93},
                "other_banks": {"2025-09-30": -7.48, "2025-11-30": -0.85},
                "nonres": {"2025-09-30": -4.11, "2025-11-30": -2.99}}.items():
            self.store.upsert_points("orfr_flows_%s" % cat, points,
                                     meta_patch={"fetched_at": "2026-11-15T09:00:00+00:00"})

    def run_script(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.mod.main(["apply"] + list(argv))
        return code, out.getvalue()

    def points(self, cat):
        return self.store.load_series("orfr_flows_%s" % cat)["points"]

    def test_без_apply_ничего_не_пишется(self):
        code, out = self.run_script()
        self.assertEqual(code, 0)
        self.assertEqual(self.points("nfo_du")["2025-09-30"], -29.8)
        self.assertIn("orfr_flows_nfo_du 2025-09-30: -29.8 → -17.63", out)
        self.assertIn("orfr_flows_szko 2025-11-30: — → -5.68", out)
        # сентябрь: НФО и ДУ; ноябрь: ДУ (19,8 → 19,82), НФО и пропуск СЗКО.
        self.assertIn("заменил бы точек: 5 (рядов 3)", out)

    def test_apply_меняет_только_период_и_кладёт_копию(self):
        self.run_script("--apply")
        self.assertEqual(self.points("nfo_du")["2025-09-30"], -17.63)
        self.assertEqual(self.points("nfo_own")["2025-09-30"], -29.53)
        self.assertEqual(self.points("nfo_own")["2025-11-30"], 10.59)
        self.assertEqual(self.points("szko")["2025-11-30"], -5.68, "пропуск заполнен")
        self.assertEqual(self.points("nfo_du")["2022-12-31"], -1.0, "2022 не трогается")
        self.assertEqual(self.points("nfo_du")["2026-01-31"], -10.0, "2026 не трогается")
        raw = self.store.raw_dir()
        self.assertTrue((raw / "orfr_flows_nfo_du.json.before-orfr-rebuild").exists())
        self.assertFalse((raw / "orfr_flows_fiz.json.before-orfr-rebuild").exists(),
                         "ряд без изменений копировать незачем")
        meta = self.store.load_series("orfr_flows_nfo_du")["meta"]
        self.assertEqual(meta.get("fetched_at"), "2026-11-15T09:00:00+00:00",
                         "пересборка — не опрос источника")
        self.assertIn("пересобраны по таблицам ЦБ", meta.get("rebuild", ""))

    def test_повтор_ничего_не_меняет(self):
        self.run_script("--apply")
        _code, out = self.run_script("--apply")
        self.assertIn("заменено точек: 0", out)


if __name__ == "__main__":
    unittest.main()
