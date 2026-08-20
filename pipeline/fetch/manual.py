"""Ручные вводы: inputs/*.yml (консенсус ЦБ, события, дивиденды, фолбэк ОРФР).

Почему свой мини-парсер YAML, а не PyYAML: пайплайн обязан подниматься на голой
Python 3.12 без venv (docs/CONTRACT.md §0), а ставить зависимость ради четырёх
плоских файлов — прямой путь к «на VPS не запускается». Поддерживается ровно то
подмножество, в котором написаны inputs/: словари, списки словарей, скаляры,
комментарии. Всё остальное (якоря, многострочные блоки |/>, вложенные потоки
[a, b]) сознательно НЕ поддерживается — если понадобится, пишите .json рядом:
загрузчик сам подхватит одноимённый .json.

Контракт fetch-модуля: функция -> (series_id, {date: value}, meta).
Ручные ряды числовыми точками бедны, поэтому договорённость такая:
points — числовая проекция (то, что можно нарисовать), а полный список записей
лежит в meta["records"]; мониторы читают именно meta["records"].
"""

import json
import os
import re
from datetime import date, datetime, timedelta, timezone

INPUTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "inputs")

# Типы событий, которые понимает панель (см. inputs/events.yml).
EVENT_TYPES = ("tax", "sanction_us", "sanction_eu", "depriv", "offering",
               "peace_pos", "peace_neg", "other")


class InputError(Exception):
    """Файл ручного ввода есть, но прочитать его нельзя (синтаксис/схема)."""


# --------------------------------------------------------------- мини-YAML

_NUM_RE = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")


def _strip_comment(line):
    """Срезать комментарий вне кавычек. Грабля: '#' внутри URL или строки —
    не комментарий, поэтому идём посимвольно, а не line.split('#')."""
    out = []
    quote = None
    prev = ""
    for ch in line:
        if quote:
            out.append(ch)
            if ch == quote and prev != "\\":
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif ch == "#" and (not out or out[-1] in (" ", "\t")):
            break
        else:
            out.append(ch)
        prev = ch
    return "".join(out).rstrip()


def _scalar(text):
    """Скаляр YAML → питоновское значение. Даты оставляем строками ISO:
    в сторе ключи и так строки, а datetime.date не сериализуется в JSON."""
    s = text.strip()
    if not s or s in ("~", "null", "Null", "NULL"):
        return None
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    low = s.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if _NUM_RE.match(s):
        return float(s) if ("." in s or "e" in low) else int(s)
    return s


def _lines(text):
    """Значимые строки: (отступ, содержимое). Табы запрещены YAML — рвём явно."""
    out = []
    for n, raw in enumerate(text.replace("\r\n", "\n").replace("\r", "\n").split("\n"), 1):
        if "\t" in raw[:len(raw) - len(raw.lstrip())]:
            raise InputError("строка %d: табуляция в отступе (YAML запрещает)" % n)
        body = _strip_comment(raw)
        if not body.strip():
            continue
        out.append((len(body) - len(body.lstrip(" ")), body.strip(), n))
    return out


def _parse_block(items, pos, indent):
    """Разобрать блок с отступом indent, начиная с items[pos]. -> (value, pos)."""
    if pos >= len(items):
        return None, pos
    if items[pos][1].startswith("- "):
        return _parse_list(items, pos, indent)
    return _parse_map(items, pos, indent)


def _parse_list(items, pos, indent):
    out = []
    while pos < len(items):
        ind, body, ln = items[pos]
        if ind < indent or not body.startswith("- "):
            break
        if ind > indent:
            raise InputError("строка %d: неровный отступ в списке" % ln)
        head = body[2:].strip()
        pos += 1
        if ":" in head and not head.startswith(("'", '"')):
            # элемент-словарь: первая пара на строке с дефисом, остальные — ниже
            key, _, val = head.partition(":")
            rec = {key.strip(): _scalar(val)}
            if val.strip() == "":
                sub, pos = _parse_block(items, pos, ind + 2) if (
                    pos < len(items) and items[pos][0] > ind) else (None, pos)
                rec[key.strip()] = sub
            child_indent = items[pos][0] if pos < len(items) else 0
            while pos < len(items) and items[pos][0] > ind and not items[pos][1].startswith("- "):
                sub_map, pos = _parse_map(items, pos, child_indent)
                rec.update(sub_map)
            out.append(rec)
        elif head:
            out.append(_scalar(head))
        else:
            sub, pos = _parse_block(items, pos, items[pos][0]) if (
                pos < len(items) and items[pos][0] > ind) else (None, pos)
            out.append(sub)
    return out, pos


def _parse_map(items, pos, indent):
    out = {}
    while pos < len(items):
        ind, body, ln = items[pos]
        if ind < indent or body.startswith("- "):
            break
        if ind > indent:
            raise InputError("строка %d: неровный отступ в словаре" % ln)
        if ":" not in body:
            raise InputError("строка %d: ожидалась пара 'ключ: значение'" % ln)
        key, _, val = body.partition(":")
        key = key.strip()
        pos += 1
        if val.strip() == "":
            if pos < len(items) and items[pos][0] > ind:
                out[key], pos = _parse_block(items, pos, items[pos][0])
            elif pos < len(items) and items[pos][1].startswith("- ") and items[pos][0] == ind:
                # список, записанный без дополнительного отступа (частый стиль)
                out[key], pos = _parse_list(items, pos, ind)
            else:
                out[key] = None
        else:
            out[key] = _scalar(val)
    return out, pos


def parse_yaml(text):
    """Плоское подмножество YAML -> dict | list. Кидает InputError на мусоре."""
    items = _lines(text)
    if not items:
        return {}
    value, pos = _parse_block(items, 0, items[0][0])
    if pos != len(items):
        raise InputError("строка %d: не разобрана до конца" % items[pos][2])
    return value


# ------------------------------------------------------------- загрузчики

def load_input(name):
    """inputs/<name>.yml | .yaml | .json -> (данные, путь). Нет файла -> (None, None)."""
    for ext in (".yml", ".yaml", ".json"):
        path = os.path.join(INPUTS_DIR, name + ext)
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
        if ext == ".json":
            try:
                return json.loads(raw), path
            except ValueError as exc:
                raise InputError("%s: битый JSON (%s)" % (path, exc))
        return parse_yaml(raw), path
    return None, None


def _records(data):
    """Файлы пишутся либо как список записей, либо как {items: [...]} с шапкой."""
    if data is None:
        return []
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for key in ("items", "records", "rows", "data"):
            if isinstance(data.get(key), list):
                return [r for r in data[key] if isinstance(r, dict)]
    return []


def _meta(path, status, note=None, extra=None):
    meta = {"source": "manual", "url": path, "status": status, "note": note,
            "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    if extra:
        meta.update(extra)
    return meta


def _num(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", ".").replace(" ", ""))
        except ValueError:
            return None
    return None


def _is_date(value):
    return isinstance(value, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", value) is not None


def consensus():
    """inputs/consensus.yml -> ("cb_consensus", {дата заседания: ожидаемая ставка}, meta)."""
    data, path = load_input("consensus")
    if data is None:
        return "cb_consensus", {}, _meta(None, "missing", "нет inputs/consensus.yml")
    recs = _records(data)
    points, clean = {}, []
    for rec in recs:
        date = rec.get("date")
        rate = _num(rec.get("expected_rate"))
        if not _is_date(date):
            continue
        clean.append(rec)
        if rate is not None:
            points[date] = rate
    status = "ok" if points else ("missing" if not clean else "stale")
    return "cb_consensus", points, _meta(path, status, extra={"records": clean})


def events():
    """inputs/events.yml -> ("events_registry", {дата: сумма expected_sign}, meta).

    В точке — сумма ожидаемых знаков за день (−1/0/+1 на событие): это всё, что
    имеет смысл рисовать. Разбор события целиком берут из meta["records"].
    """
    data, path = load_input("events")
    if data is None:
        return "events_registry", {}, _meta(None, "missing", "нет inputs/events.yml")
    points, clean, bad = {}, [], []
    for rec in _records(data):
        date = rec.get("date")
        if not _is_date(date):
            continue
        if rec.get("type") not in EVENT_TYPES:
            bad.append(rec.get("title") or date)
        sign = _num(rec.get("expected_sign")) or 0.0
        points[date] = points.get(date, 0.0) + sign
        clean.append(rec)
    note = None if not bad else "неизвестный type у: %s" % "; ".join(str(b) for b in bad[:5])
    # authoritative: файл описывает реестр ЦЕЛИКОМ. Строку из него удалили —
    # значит события не было, и точка обязана уйти из ряда (store.upsert_points
    # прополет). Без этого выдуманное событие остаётся в проде и после правки файла.
    return "events_registry", points, _meta(path, "ok" if clean else "missing", note,
                                            {"records": clean, "authoritative": True})


def dividends():
    """inputs/dividends.yml -> ("dividends", {ex_date: суммарная дивдоходность, %}, meta).

    Суммируем yield_pct всех бумаг с отсечкой в этот день: величина «дивидендного
    гэпа дня» по портфелю индекса — единственное, что осмысленно свести в число.
    """
    data, path = load_input("dividends")
    if data is None:
        return "dividends", {}, _meta(None, "missing", "нет inputs/dividends.yml")
    points, clean = {}, []
    for rec in _records(data):
        date = rec.get("ex_date")
        if not _is_date(date):
            continue
        clean.append(rec)
        yld = _num(rec.get("yield_pct"))
        if yld is not None:
            points[date] = round(points.get(date, 0.0) + yld, 4)
    return "dividends", points, _meta(path, "ok" if clean else "missing",
                                      extra={"records": clean})


def orfr_manual():
    """inputs/orfr.yml -> ({"2026-07-31": {"fiz": 24.4, …}}, путь, {дата: источник}).

    В значениях точек ТОЛЬКО числа по категориям: ссылка на PDF — метаданные, и
    её место в meta, а не внутри ряда (иначе она поедет в стор как «значение»).
    Ключ точки — последний день месяца (как в сторе), хотя человек пишет 2026-07.
    """
    data, path = load_input("orfr")
    points, sources = {}, {}
    for rec in _records(data):
        month = rec.get("month")
        if isinstance(month, str) and re.match(r"^\d{4}-\d{2}$", month.strip()):
            key = _month_end(month.strip())
        elif _is_date(month):
            key = month
        else:
            continue
        row = {}
        for cat in ("fiz", "nfo_du", "nfo_own", "szko", "other_banks", "nonres"):
            val = _num(rec.get(cat))
            if val is not None:
                row[cat] = val
        if row:
            points[key] = row
            sources[key] = rec.get("source") or path
    return points, path, sources


def urals_manual():
    """inputs/urals.yml -> ({"2026-08-31": 61.3}, путь).

    Гарантированный пол для ноги ядра. Значение — НАЛОГОВАЯ цена Юралс в долларах
    за баррель (та, что в письмах ФНС и релизах Минэка «О среднем уровне цен нефти
    сорта „Юралс“»), а не рыночная котировка из лент: это разные величины, и в ряд
    идёт первая (см. шапку fetch/consultant.py).
    """
    data, path = load_input("urals")
    points = {}
    for rec in _records(data):
        month = rec.get("month")
        if isinstance(month, str) and re.match(r"^\d{4}-\d{2}$", month.strip()):
            key = _month_end(month.strip())
        elif _is_date(month):
            key = month
        else:
            continue
        value = _num(rec.get("usd", rec.get("price")))
        # Коридор тот же, что у автоматического парсера: опечатка в рублях (5 400)
        # или в копейках (0,59) не должна попасть в ногу ядра.
        if value is not None and 5.0 < value < 250.0:
            points[key] = value
    return points, path


def _month_end(ym):
    """'2026-07' -> '2026-07-31' без календарных зависимостей."""
    year, month = int(ym[:4]), int(ym[5:7])
    nxt = (year + 1, 1) if month == 12 else (year, month + 1)
    return (date(nxt[0], nxt[1], 1) - timedelta(days=1)).isoformat()
