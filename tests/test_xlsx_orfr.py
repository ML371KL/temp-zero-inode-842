"""Приложение ЦБ к «Обзору рисков» читается таблицей, а не PDF.

ЧТО СЛОМАЛОСЬ. ЦБ с марта 2026 публикует приложение только в .xls/.xlsx: последний
PDF — февральский. `latest_pdf()` продолжал честно брать «самый свежий PDF» и потому
навсегда застрял на феврале. Выглядело это как исправная работа: `fetched_at`
обновлялся каждый месяц, статус стоял `ok`, тайл показывал июль — потому что март,
апрель, май, июнь и июль человек переносил в `seed/orfr_flows.csv` руками. То есть
живого источника у ряда не было полгода, и узнать об этом было неоткуда.

Проверено на настоящих выпусках перед тем, как писать эти тесты: ORFR_2026-7.xlsx
дал 6 категорий из 6 совпадающими с ручной затравкой, ORFR_2026-6.xlsx — 18 значений
из 18 (три месяца × шесть категорий).

Книга здесь СОБИРАЕТСЯ на месте, а не лежит фикстурой: настоящий выпуск весит 1,5 МБ,
и в наборе он был бы мёртвым грузом, который никто не перечитает. В сеть не ходим.
"""

import io
import unittest
import zipfile
from unittest import mock

from tests import need

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

HEADER = ["Годы", "Месяцы/кварталы", "Нерезиденты", "НФО", "Прочие банки", "СЗКО",
          "Физические лица", "Доверительное управление", "Нефинансовые организации"]

# Строки настоящего ORFR_2026-7.xlsx (лист «РА По участникам»).
REAL_ROWS = [
    ["Среднемесячные покупки/продажи акций по категориям участников, млрд руб."],
    ["* Без учета крупных сделок между аффилированными организациями."],
    [],
    HEADER,
    [2026, "1к", 1.96, -7.81, 0.13, 3.11, 14.59, -14.23, 2.25],
    [None, "2к", -7.96, -2.72, 2.53, -8.09, 30.49, -17.67, 3.41],
    [None, "Июль", 15.29, 12.86, -0.36, -22.68, 24.43, -37.85, 8.3],
]
JULY = {"fiz": 24.43, "nfo_du": -37.85, "nfo_own": 12.86,
        "szko": -22.68, "other_banks": -0.36, "nonres": 15.29}


def _col_name(idx):
    name = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        name = chr(65 + rem) + name
    return name


def build_workbook(rows, sheet_name="РА По участникам", skip_empty=True):
    """Минимальная .xlsx-книга из таблицы значений.

    `skip_empty=True` повторяет поведение настоящих файлов: ПУСТЫЕ ЯЧЕЙКИ В XML
    ПРОСТО ОТСУТСТВУЮТ. Это главная ловушка формата — читатель, считающий ячейки
    подряд, сдвигает строку влево и приписывает физлицам поток нерезидентов.
    """
    shared, index = [], {}
    for row in rows:
        for value in row:
            if isinstance(value, str) and value not in index:
                index[value] = len(shared)
                shared.append(value)

    body = []
    for r, row in enumerate(rows, start=1):
        cells = []
        for c, value in enumerate(row):
            ref = "%s%d" % (_col_name(c), r)
            if value is None:
                if not skip_empty:
                    cells.append('<c r="%s"/>' % ref)
                continue
            if isinstance(value, str):
                cells.append('<c r="%s" t="s"><v>%d</v></c>' % (ref, index[value]))
            else:
                cells.append('<c r="%s"><v>%s</v></c>' % (ref, value))
        body.append('<row r="%d">%s</row>' % (r, "".join(cells)))

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml",
                   '<workbook xmlns="%s" xmlns:r="%s"><sheets>'
                   '<sheet name="%s" sheetId="1" r:id="rId1"/>'
                   '</sheets></workbook>' % (NS, NS_REL, sheet_name))
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr("xl/sharedStrings.xml",
                   '<sst xmlns="%s">%s</sst>'
                   % (NS, "".join("<si><t>%s</t></si>" % s for s in shared)))
        z.writestr("xl/worksheets/sheet1.xml",
                   '<worksheet xmlns="%s"><sheetData>%s</sheetData></worksheet>'
                   % (NS, "".join(body)))
    return buf.getvalue()


# Строки настоящего ORFR_2025-12_2026-01.xlsx, лист «рис. 21» (тот же блок, что позже
# стал листом «РА По участникам»; «Прочие Банки» с заглавной — как в файле).
OLD_HEADER = ["Годы", "Месяцы/кварталы", "Нерезиденты", "НФО", "Прочие Банки", "СЗКО",
              "Физические лица", "Доверительное управление", "Нефинансовые организации"]
OLD_ROWS = [
    ["Покупки /продажи акций по категориям участников на вторичных биржевых торгах "
     "(в среднем в месяц), млрд руб."],
    ["Источник: ПАО Московская Биржа."],
    [],
    OLD_HEADER,
    [2025, "I квартал", 1.86, -4.63, -16.97, 8.5, 10.71, 3.29, -2.77],
    [None, "II квартал", 2.54, -13.76, -8.41, 7.81, 18.28, -8.87, 2.41],
    [None, "III квартал", -4.0, -17.84, -0.37, 12.67, 21.34, -11.08, -0.71],
    [None, "Октябрь", -1.52, 3.8, -1.76, -2.86, 13.3, -9.64, -1.31],
    [None, "Ноябрь", -2.99, 10.5, -0.85, -5.68, -22.33, 19.82, 1.51],
    [None, "Декабрь", -12.85, -34.31, 2.27, 1.93, 0.44, 50.59, -8.07],
    [2026, "Январь", 5.34, 14.49, 1.87, -12.9, 2.44, -10.42, -0.82],
]


class OldWorkbookCase(unittest.TestCase):
    """Таблица к выпуску за декабрь 2025 — январь 2026: лист называется «рис. 21»."""

    def setUp(self):
        self.orfr = need(self, "pipeline.fetch.orfr", "parse_workbook", "equity_sheet")
        self.x = need(self, "pipeline.lib.xlsx", "open_bytes", "XlsxError")
        self.book = build_workbook(OLD_ROWS, sheet_name="рис. 21")

    def test_лист_находится_по_заголовку_а_месяцы_разбираются(self):
        sheet = self.orfr.equity_sheet(self.x.open_bytes(self.book))
        self.assertEqual(sheet, "рис. 21")
        months, _notes = self.orfr.parse_workbook(self.book, "2026-01", sheet=sheet)
        # Кварталы — среднемесячные и в месячный ряд не идут; год тянется от ячейки.
        self.assertEqual(sorted(months), ["2025-10", "2025-11", "2025-12", "2026-01"])
        self.assertEqual(months["2025-11"]["szko"], -5.68)
        self.assertEqual(months["2025-11"]["other_banks"], -0.85)
        self.assertEqual(months["2025-12"]["szko"], 1.93)
        self.assertEqual(months["2025-12"]["other_banks"], 2.27)

    def test_лист_про_облигации_потоками_акций_не_считается(self):
        # Рядом лежат листы с теми же колонками участников про ОФЗ и корпоративные
        # облигации. мутация: искать лист только по колонкам -> в ряд потоков акций
        # уезжают покупки облигаций.
        rows = [["Покупки/продажи ОФЗ по категориям участников, млрд руб."], [], [],
                OLD_HEADER, [2025, "Ноябрь", 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]]
        with self.assertRaises(self.x.XlsxError):
            self.orfr.equity_sheet(self.x.open_bytes(build_workbook(rows, "рис. 7")))

    def test_фетчер_по_прежнему_читает_лист_по_имени(self):
        # Без явного листа старая книга — отказ, а не тихий выбор похожего листа.
        with self.assertRaises(self.x.XlsxError):
            self.orfr.parse_workbook(self.book, "2026-01")


class BackfillGapsCase(unittest.TestCase):
    """ops/backfill_orfr_gaps.py: долив пропусков, существующие точки не трогаются."""

    def setUp(self):
        import importlib.util
        import os
        from pathlib import Path
        from tempfile import TemporaryDirectory
        self.store = need(self, "pipeline.lib.store", "upsert_points", "load_series")
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        prev = os.environ.get("STATE_DIR")
        os.environ["STATE_DIR"] = self.tmp.name
        self.addCleanup(lambda: os.environ.pop("STATE_DIR", None) if prev is None
                        else os.environ.__setitem__("STATE_DIR", prev))
        path = Path(__file__).resolve().parents[1] / "ops" / "backfill_orfr_gaps.py"
        spec = importlib.util.spec_from_file_location("backfill_orfr_gaps", path)
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        self.book = Path(self.tmp.name) / "ORFR_2025-12_2026-01.xlsx"
        self.book.write_bytes(build_workbook(OLD_ROWS, sheet_name="рис. 21"))
        # Боевые ряды на 20.11.2026: у СЗКО и прочих банков нет ноября и декабря 2025,
        # у остальных категорий месяцы есть (значения — из копии стора аудита).
        seed = {"fiz": {"2025-10-31": 13.3, "2025-11-30": -22.3, "2025-12-31": 0.4,
                        "2026-01-31": 2.44},
                "nfo_du": {"2025-10-31": -9.6, "2025-11-30": 19.8, "2025-12-31": 50.6,
                           "2026-01-31": -10.42},
                "nfo_own": {"2025-10-31": 4.5, "2025-11-30": 16.0, "2025-12-31": -34.3,
                            "2026-01-31": 12.38},
                "szko": {"2025-10-31": -2.9, "2026-01-31": -12.9},
                "other_banks": {"2025-10-31": -1.8, "2026-01-31": 1.87},
                "nonres": {"2025-10-31": -1.5, "2025-11-30": -3.0, "2025-12-31": -12.9,
                           "2026-01-31": 5.34}}
        for cat, points in seed.items():
            self.store.upsert_points("orfr_flows_%s" % cat, points)

    def run_script(self, *argv):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.mod.main([str(self.book)] + list(argv))
        return code, out.getvalue()

    def points(self, cat):
        return self.store.load_series("orfr_flows_%s" % cat)["points"]

    def test_без_apply_ничего_не_пишется(self):
        code, out = self.run_script()
        self.assertEqual(code, 0)
        self.assertNotIn("2025-11-30", self.points("szko"))
        self.assertIn("долил бы 4", out)

    def test_apply_доливает_только_пропуски(self):
        self.run_script("--apply")
        self.assertEqual(self.points("szko")["2025-11-30"], -5.68)
        self.assertEqual(self.points("szko")["2025-12-31"], 1.93)
        self.assertEqual(self.points("other_banks")["2025-11-30"], -0.85)
        self.assertEqual(self.points("other_banks")["2025-12-31"], 2.27)
        # мутация: писать все месяцы таблицы -> затравка пересобрана молча: у НФО за
        # ноябрь 2025 в ряду 16,0, в таблице 10,5 — пересмотр ЦБ, решать не скрипту.
        self.assertEqual(self.points("nfo_own")["2025-11-30"], 16.0)
        self.assertEqual(self.points("fiz")["2025-11-30"], -22.3)
        self.assertEqual(self.points("szko")["2025-10-31"], -2.9)

    def test_расхождения_названы_но_не_исправлены(self):
        _code, out = self.run_script("--apply")
        self.assertIn("orfr_flows_nfo_own 2025-11-30: в ряду 16.0, в таблице 10.5", out)
        self.assertIn("НЕ меняю", out)

    def test_копия_ряда_остаётся_рядом(self):
        self.run_script("--apply")
        copy = self.store.raw_dir() / "orfr_flows_szko.json.before-orfr-fill"
        self.assertTrue(copy.exists())
        self.assertFalse((self.store.raw_dir()
                          / "orfr_flows_fiz.json.before-orfr-fill").exists(),
                         "ряд без долива копировать незачем")


class ReaderCase(unittest.TestCase):
    def setUp(self):
        self.x = need(self, "pipeline.lib.xlsx", "open_bytes", "XlsxError")

    def test_читает_строки_и_числа(self):
        book = self.x.open_bytes(build_workbook(REAL_ROWS))
        rows = book.rows("РА По участникам")
        self.assertEqual(rows[3], HEADER)
        self.assertEqual(rows[6][1], "Июль")
        self.assertAlmostEqual(rows[6][6], 24.43)

    def test_пропущенные_ячейки_не_сдвигают_строку(self):
        """ГЛАВНАЯ ЛОВУШКА ФОРМАТА: у года объединённая ячейка, и в XML её нет.

        Читатель, складывающий ячейки подряд, сдвинул бы строку «Июль» на одну
        колонку влево — и записал бы физлицам поток доверительного управления.
        """
        book = self.x.open_bytes(build_workbook(REAL_ROWS))
        july = book.rows("РА По участникам")[6]
        self.assertIsNone(july[0], "пустая ячейка года обязана остаться пустой")
        self.assertEqual(len(july), len(HEADER))
        self.assertAlmostEqual(july[6], JULY["fiz"])

    def test_имена_листов_и_отсутствующий_лист(self):
        book = self.x.open_bytes(build_workbook(REAL_ROWS))
        self.assertIn("РА По участникам", book.sheets)
        with self.assertRaises(self.x.XlsxError):
            book.rows("такого листа нет")

    def test_не_книга_это_ошибка_а_не_падение(self):
        with self.assertRaises(self.x.XlsxError):
            self.x.open_bytes(b"<html>503</html>").rows("любой")


class ParseCase(unittest.TestCase):
    def setUp(self):
        self.orfr = need(self, "pipeline.fetch.orfr", "parse_workbook",
                         "latest_workbook", "CATEGORIES")

    def test_месячная_строка_разбирается_целиком(self):
        months, notes = self.orfr.parse_workbook(build_workbook(REAL_ROWS), "2026-07")
        self.assertEqual(sorted(months), ["2026-07"])
        self.assertEqual(months["2026-07"], JULY)
        self.assertEqual(notes, [])

    def test_кварталы_в_месячный_ряд_не_попадают(self):
        """В квартальных строках лежат СРЕДНЕМЕСЯЧНЫЕ значения за квартал.

        Подмешать их в месячный ряд — записать среднее за три месяца как факт
        одного: физлицам за 1к приписалось бы +14,59 вместо настоящих месяцев.
        """
        months, _ = self.orfr.parse_workbook(build_workbook(REAL_ROWS), "2026-07")
        self.assertNotIn("2026-01", months)
        self.assertNotIn("2026-03", months)
        self.assertEqual(len(months), 1, months)

    def test_колонки_ищутся_по_заголовку_а_не_по_номеру(self):
        """мутация: брать колонки по номеру -> перестановка у ЦБ молча переставляет
        потоки между категориями, и физлицам достаётся поток нерезидентов."""
        swapped = [list(r) for r in REAL_ROWS]
        for row in swapped[3:]:
            if len(row) > 6:
                row[2], row[6] = row[6], row[2]        # Нерезиденты <-> Физические лица
        months, _ = self.orfr.parse_workbook(build_workbook(swapped), "2026-07")
        self.assertEqual(months["2026-07"], JULY, "колонки поехали за номером, а не за шапкой")

    def test_год_тянется_из_объединённой_ячейки(self):
        rows = [list(r) for r in REAL_ROWS]
        rows.append([None, "Август", 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0])
        months, _ = self.orfr.parse_workbook(build_workbook(rows), "2026-08")
        self.assertIn("2026-08", months, "месяц без года потерял год блока")
        self.assertAlmostEqual(months["2026-08"]["fiz"], 5.0)

    def test_несколько_месяцев_в_одном_выпуске(self):
        rows = [list(r) for r in REAL_ROWS[:6]]
        rows += [[None, "Апрель", -7.65, -7.85, 3.73, -12.48, 43.71, -19.97, 0.0],
                 [None, "Май", -4.15, -4.49, 0.36, -4.57, 19.9, -10.25, 0.0],
                 [None, "Июнь", -12.07, 4.19, 3.51, -7.2, 27.85, -22.78, 0.0]]
        months, notes = self.orfr.parse_workbook(build_workbook(rows), "2026-06")
        self.assertEqual(sorted(months), ["2026-04", "2026-05", "2026-06"])
        self.assertAlmostEqual(months["2026-06"]["fiz"], 27.85)
        self.assertEqual(notes, [])

    def test_пропавшая_колонка_это_отказ_а_не_дыра(self):
        broken = [list(r) for r in REAL_ROWS]
        broken[3] = [c for c in HEADER if c != "СЗКО"]
        with self.assertRaises(Exception) as ctx:
            self.orfr.parse_workbook(build_workbook(broken), "2026-07")
        self.assertIn("szko", str(ctx.exception))

    def test_чужая_вёрстка_не_угадывается(self):
        with self.assertRaises(Exception):
            self.orfr.parse_workbook(build_workbook([["мусор"], [1, 2, 3]]), "2026-07")

    def test_период_не_из_выпуска_отмечается_замечанием(self):
        _months, notes = self.orfr.parse_workbook(build_workbook(REAL_ROWS), "2026-09")
        self.assertTrue(any("2026-09" in n for n in notes),
                        "расхождение выпуска и таблицы обязано попасть в note")


class ChoiceCase(unittest.TestCase):
    def setUp(self):
        self.orfr = need(self, "pipeline.fetch.orfr", "latest_workbook", "latest_pdf")

    def test_берётся_самая_свежая_таблица(self):
        html = """
          <a href="/Collection/File/59729/ORFR_2026-2.xlsx">февраль</a>
          <a href="/Collection/File/62248/ORFR_2026-7.xlsx">июль</a>
          <a href="/Collection/File/62118/ORFR_2026-6.xlsx">июнь</a>
        """
        url, period = self.orfr.latest_workbook(html)
        self.assertEqual(period, "2026-07")
        self.assertTrue(url.endswith("ORFR_2026-7.xlsx"))
        self.assertTrue(url.startswith("https://"), "относительный адрес не развернулся")

    def test_таблица_свежее_последнего_pdf(self):
        """Ровно состояние страницы ЦБ на 14.08.2026: PDF кончились на феврале."""
        html = ('<a href="/f/ORFR_2026-2.pdf">pdf</a>'
                '<a href="/f/ORFR_2026-7.xlsx">xlsx</a>')
        self.assertEqual(self.orfr.latest_pdf(html)[1], "2026-02")
        self.assertEqual(self.orfr.latest_workbook(html)[1], "2026-07")

    def test_без_таблиц_ответ_пустой_а_не_исключение(self):
        self.assertEqual(self.orfr.latest_workbook("<a href='/f/ORFR_2026-2.pdf'>p</a>"),
                         (None, None))


class FlowsCase(unittest.TestCase):
    """Точка входа целиком: страница ЦБ -> ряды. Транспорт подменён, в сеть не ходим."""

    def setUp(self):
        self.orfr = need(self, "pipeline.fetch.orfr", "flows", "SERIES_ID")
        self.asked = []

    def serve(self, html, workbook=None, pdf_raises=False):
        from unittest import mock

        def get_text(url, **kw):
            self.asked.append(url)
            return html

        def get_bytes(url, **kw):
            self.asked.append(url)
            if url.endswith(".xlsx"):
                if workbook is None:
                    raise self.orfr.FetchError("404")
                return workbook
            if pdf_raises:
                raise self.orfr.FetchError("PDF не отдался")
            return "%PDF-1.4 без текстового слоя".encode("utf-8")

        for target, fake in (("get_text", get_text), ("get_bytes", get_bytes)):
            patcher = mock.patch.object(self.orfr, target, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def page(self):
        # Настоящее состояние страницы ЦБ на 14.08.2026: PDF кончились на феврале,
        # таблицы идут до июля.
        return ('<a href="/f/ORFR_2026-2.pdf">pdf</a>'
                '<a href="/f/ORFR_2026-7.xlsx">xlsx</a>')

    def test_таблица_выигрывает_у_последнего_pdf(self):
        """мутация «PDF вперёд таблицы» -> ряд снова замирает на феврале 2026,
        и это выглядит исправной работой: fetched_at свежий, статус ok."""
        self.serve(self.page(), workbook=build_workbook(REAL_ROWS))
        rows = self.orfr.flows()
        meta = rows[0][2]
        self.assertEqual(meta["status"], "ok")
        self.assertEqual(meta["asof"], "2026-07")
        self.assertEqual(meta.get("format"), "xlsx")
        self.assertTrue(meta["url"].endswith(".xlsx"), meta["url"])
        self.assertFalse([u for u in self.asked if u.endswith(".pdf")],
                         "за PDF ходили, хотя таблица разобралась")
        values = {sid.replace(self.orfr.SERIES_ID + "_", ""): vals["2026-07-31"]
                  for sid, vals, _m in rows}
        self.assertEqual(values, JULY)

    def test_битая_таблица_откатывает_на_pdf_и_называет_причину(self):
        # Отказ таблицы обязан быть виден: иначе в журнале останется «PDF не
        # разобрался», и никто не узнает, что живой источник вообще не тронут.
        self.serve(self.page(), workbook=b"<html>503</html>")
        meta = self.orfr.flows()[0][2]
        self.assertIn(meta["status"], ("manual_needed", "error"))
        self.assertIn("таблица", (meta.get("note") or "").lower())

    def test_успешный_ископаемый_pdf_не_прячет_отказ_таблицы(self):
        """PDF-приложение ЦБ кончилось на феврале 2026 — «удачно разобранный PDF»
        после отказа таблицы означает «февраль навсегда». До 18.08.2026 такой
        прогон отчитывался status=ok/asof=2026-02: разовый 503 на .xlsx замораживал
        ряд на полгода, и в журнале каждый месяц стояло «ok»."""
        self.serve(self.page(), workbook=b"<html>503</html>")
        with mock.patch.object(self.orfr, "_get_pdf", return_value=b"%PDF"), \
             mock.patch.object(self.orfr, "pdf_text", return_value="т" * 3000), \
             mock.patch.object(self.orfr, "parse_flows",
                               return_value=({"fiz": 12.3, "nfo_du": -37.9}, [])):
            rows = self.orfr.flows()
        meta = rows[0][2]
        self.assertEqual(meta["status"], "error",
                         "отказ живого источника спрятан за успехом ископаемого")
        self.assertIn("ТАБЛИЦА НЕ ПРОЧИТАНА", meta.get("note") or "")
        # Числа февраля при этом не выбрасываются: для своего месяца они верны.
        self.assertTrue(any("2026-02-28" in vals for _sid, vals, _m in rows))


if __name__ == "__main__":
    unittest.main()
