"""Чтение .xlsx стандартной библиотекой: zip + XML, без openpyxl и pandas.

ЗАЧЕМ. Конвейер обязан запускаться на любой машине без venv (CONTRACT §0, проверка
в CI «Пайплайн остался на стандартной библиотеке»). А ЦБ с марта 2026 публикует
приложение к «Обзору рисков финансовых рынков» ТОЛЬКО таблицей: последний PDF —
февральский, дальше идут .xls/.xlsx. Пока читателя таблиц не было, `fetch/orfr.py`
брал самый свежий PDF, то есть навсегда застрял на феврале, а живые числа человек
переносил руками в `seed/orfr_flows.csv`.

Формат xlsx — это zip с XML внутри, и нужного нам подмножества хватает на сотню
строк: имена листов из `xl/workbook.xml`, словарь строк из `xl/sharedStrings.xml`,
значения из `xl/worksheets/sheetN.xml`.

ЧЕГО ЭТОТ ЧИТАТЕЛЬ НАМЕРЕННО НЕ УМЕЕТ: формул (берётся закэшированное значение
`<v>`, как его посчитал Excel), форматов, дат (они лежат числом — вызывающий сам
решает, дата это или число), стилей. Всё это нам не нужно, а поддержка каждого
пункта — это шанс тихо ошибиться в числе.
"""

import io
import re
import zipfile
import xml.etree.ElementTree as ET

__all__ = ["Workbook", "open_bytes"]

NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_M = "{%s}" % NS_MAIN
_CELL_REF = re.compile(r"([A-Z]+)(\d+)")


class XlsxError(ValueError):
    """Книга не разобралась. Наследник ValueError: вызывающие уже ловят его."""


def _col_index(ref):
    """'A1' -> 0, 'C7' -> 2. Нужен, потому что ПУСТЫЕ ЯЧЕЙКИ В XML ПРОСТО ОТСУТСТВУЮТ:
    без разбора адреса строка «съезжает» влево и числа уезжают в чужие колонки."""
    match = _CELL_REF.match(ref or "")
    if not match:
        return None
    letters = match.group(1)
    idx = 0
    for ch in letters:
        idx = idx * 26 + (ord(ch) - 64)
    return idx - 1


class Workbook:
    def __init__(self, data):
        try:
            self._zip = zipfile.ZipFile(io.BytesIO(data))
        except (zipfile.BadZipFile, OSError) as exc:
            raise XlsxError("не zip-архив: %s" % exc) from exc
        self._shared = None
        self._sheets = None

    # ------------------------------------------------------------------ служебное
    def _read(self, path):
        try:
            return self._zip.read(path)
        except KeyError as exc:
            raise XlsxError("в книге нет %s" % path) from exc

    @property
    def shared(self):
        """Словарь общих строк. В xlsx текст ячеек вынесен сюда, а в листе стоит индекс."""
        if self._shared is None:
            if "xl/sharedStrings.xml" not in self._zip.namelist():
                self._shared = []
            else:
                root = ET.fromstring(self._read("xl/sharedStrings.xml"))
                # Текст может быть разрезан на куски <r><t>…</t></r> (разное
                # начертание внутри ячейки) — склеиваем все <t> подряд.
                self._shared = ["".join(t.text or "" for t in si.iter(_M + "t"))
                                for si in root]
        return self._shared

    @property
    def sheets(self):
        """-> {имя листа: путь внутри архива}, в порядке книги."""
        if self._sheets is None:
            rels = {}
            for rel in ET.fromstring(self._read("xl/_rels/workbook.xml.rels")):
                rels[rel.get("Id")] = rel.get("Target")
            out = {}
            root = ET.fromstring(self._read("xl/workbook.xml"))
            for sheet in root.iter(_M + "sheet"):
                target = rels.get(sheet.get("{%s}id" % NS_REL))
                if not target:
                    continue
                if not target.startswith("xl/"):
                    target = "xl/" + target.lstrip("/")
                out[(sheet.get("name") or "").strip()] = target
            self._sheets = out
        return self._sheets

    # -------------------------------------------------------------------- чтение
    def rows(self, name):
        """Лист по имени -> список строк, каждая строка — список значений.

        Значение: str для текста, float для числа, None для пустой ячейки.
        """
        path = self.sheets.get(name)
        if path is None:
            raise XlsxError("листа %r нет; есть: %s"
                            % (name, ", ".join(sorted(self.sheets))[:300]))
        sheet = ET.fromstring(self._read(path))
        out = []
        for row in sheet.iter(_M + "row"):
            cells = []
            for cell in row:
                if not cell.tag.endswith("}c"):
                    continue
                idx = _col_index(cell.get("r"))
                if idx is None:
                    idx = len(cells)
                while len(cells) < idx:
                    cells.append(None)   # пропущенные адреса = пустые ячейки
                cells.append(self._value(cell))
            out.append(cells)
        return out

    def _value(self, cell):
        kind = cell.get("t")
        if kind == "inlineStr":
            node = cell.find(_M + "is")
            return "".join(t.text or "" for t in node.iter(_M + "t")) if node is not None else None
        node = cell.find(_M + "v")
        raw = None if node is None else node.text
        if raw is None:
            return None
        if kind == "s":
            try:
                return self.shared[int(raw)]
            except (ValueError, IndexError):
                return None
        if kind in ("str", "e"):
            return raw
        try:
            return float(raw)
        except ValueError:
            return raw


def open_bytes(data):
    return Workbook(data)
