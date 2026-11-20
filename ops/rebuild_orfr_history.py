#!/usr/bin/env python3
"""Пересобрать историю потоков ОРФР за 2023–2025 годы по таблицам ЦБ.

Зачем (аудит 20.11.2026, раздел 7, п. 1). Затравка `seed/orfr_flows.csv` собиралась
разбором текста и графиков PDF «Обзора рисков финансовых рынков», и за 2023–2025 годы
она не сходится с таблицей ЦБ: в 2024–2025 годах пропуски, в сентябре 2025 перепутаны
НФО и доверительное управление, два месяца взяты из пересказа на smart-lab. Кроме
того, ЦБ дважды пересматривал эти годы:
* в выпуске за сентябрь 2025 история с 2023 года переписана (в поздних таблицах — с
  пометкой «без учёта крупных сделок между аффилированными организациями») и
  опубликована только среднемесячными по кварталам; помесячно в новом виде месяцы до
  июня 2025 не выходили;
* колонку «НФО» ЦБ продолжает пересматривать от выпуска к выпуску (сумма НФО и
  «Нефинансовых организаций» при этом не меняется — меняется разбивка).

Как считается месяц. Помесячный рисунок — из самой свежей помесячной публикации ЦБ, где
этот месяц есть (приложения `charts_ORFR_ГГГГ-ММ.xlsx` с апреля 2023 года несут скользящее
окно из 15–20 месяцев). Уровень квартала — из самой свежей квартальной таблицы ЦБ. Если
среднее трёх месяцев не равно кварталу ЦБ, разница раскладывается на три месяца поровну:
сумма квартала становится официальной, а разница соседних месяцев остаётся той, что
публиковал ЦБ. Сдвиг каждого квартала печатается; в строке затравки (колонка src)
названы обе таблицы-источника.

Колонка «Дочерние иностранные организации» была в таблицах до середины 2024 года; позже
её нет, и её поток вошёл в «Прочие банки» (январь 2024: 5,05 − 8,82 = −3,77 в таблице к
выпуску за июнь 2024 и −3,78 в таблице к выпуску за декабрь 2024 — январь 2025). Поэтому
её числа прибавляются к прочим банкам. «Нефинансовые организации» панель не ведёт.

Два шага.

    # 1) локально, по скачанным таблицам (в сеть скрипт не ходит):
    python3 ops/rebuild_orfr_history.py build charts_ORFR_*.xlsx ORFR_*.xlsx
    python3 ops/rebuild_orfr_history.py build <те же файлы> --write-seed

    # 2) на сервере, после слияния — строки затравки за 2023–2025 в ряды стора,
    #    под замком стора и от пользователя dash; по умолчанию только показывает:
    STATE_DIR=/var/lib/moex-radar flock -w 240 /var/lib/moex-radar/.run.lock \\
        python3 ops/rebuild_orfr_history.py apply [--apply]

Копия каждого изменённого ряда кладётся рядом: raw/<ряд>.json.before-orfr-rebuild.
Повторный `apply` ничего не меняет. Месяцы вне периода (до 2023 и с 2026) не трогаются.
"""

import argparse
import csv
import io
import re
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.fetch import orfr  # noqa: E402
from pipeline.lib import store, xlsx  # noqa: E402

SINCE, UNTIL = "2023-01", "2025-12"
SEED = ROOT / "seed" / "orfr_flows.csv"
BACKUP_SUFFIX = ".before-orfr-rebuild"
CATS = orfr.CATEGORIES
EXTRA_TITLES = {"дочерние иностранные организации": "other_banks"}
EXCEL_EPOCH = date(1899, 12, 30)
# Точность сравнения точек стора с затравкой: в затравке два знака после запятой.
SAME = 0.005

_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4}
_QUARTER_RE = re.compile(r"^(iv|i{1,3})\s*квартал|^([1-4])\s*к(?:в|\b|$)")


# ------------------------------------------------------------------ чтение таблиц

def _norm(cell):
    return str(cell or "").strip().lower()


def _quarter_of_label(label):
    """'II квартал' / '2к' / '2 кв.' -> 2; месяц и прочее -> None."""
    m = _QUARTER_RE.match(_norm(label))
    if not m:
        return None
    return _ROMAN[m.group(1)] if m.group(1) else int(m.group(2))


def _month_of_serial(cell):
    """Дата Excel числом (44652.0) -> 'YYYY-MM'. Не похоже на дату -> None."""
    if isinstance(cell, bool) or not isinstance(cell, (int, float)):
        return None
    if not 20000 < cell < 80000:          # 1954…2119: всё остальное — не дата
        return None
    day = EXCEL_EPOCH + timedelta(days=int(cell))
    return "%04d-%02d" % (day.year, day.month)


def read_table(data):
    """Байты .xlsx ЦБ -> ({'YYYY-MM': {кат: млрд}}, {'YYYY-Qn': {кат: среднемесячное}}).

    Вёрстки, которые встречаются в приложениях к обзору:
    * скользящее окно месяцев с датой Excel в колонке «Дата» (2023 — середина 2025);
    * «Годы» + «Месяцы/кварталы»: кварталы строками «I квартал» или «1к», текущие
      месяцы — названием (с сентября 2025).
    Лист ищется по заголовку (orfr.equity_sheet), колонки — по шапке.
    """
    book = xlsx.open_bytes(data)
    rows = book.rows(orfr.equity_sheet(book))
    head_at = next((i for i, row in enumerate(rows)
                    if "физические лица" in [_norm(c) for c in row]), None)
    if head_at is None:
        raise xlsx.XlsxError("нет шапки с категориями участников")
    header = [_norm(c) for c in rows[head_at]]
    cols = {}
    for idx, title in enumerate(header):
        cat = orfr.COLUMN_TITLES.get(title) or EXTRA_TITLES.get(title)
        if cat:
            cols[idx] = cat
    missing = [c for c in CATS if c not in cols.values()]
    if missing:
        raise xlsx.XlsxError("в шапке нет колонок: %s" % ", ".join(missing))
    date_col = header.index("дата") if "дата" in header else None
    year_col = header.index("годы") if "годы" in header else None
    if date_col is None and year_col is None:
        raise xlsx.XlsxError("в шапке нет ни «Дата», ни «Годы»")

    months, quarters, year = {}, {}, None
    for row in rows[head_at + 1:]:
        def cell(i):
            return row[i] if i is not None and i < len(row) else None
        key = None
        if date_col is not None:
            month = _month_of_serial(cell(date_col))
            if month:
                key = ("m", month)
        else:
            ycell = cell(year_col)
            if isinstance(ycell, (int, float)) and not isinstance(ycell, bool):
                year = int(ycell)
            label = cell(year_col + 1)
            month = orfr._month_from_cell(label)
            quarter = _quarter_of_label(label)
            if year and month:
                key = ("m", "%04d-%02d" % (year, month))
            elif year and quarter:
                key = ("q", "%04d-Q%d" % (year, quarter))
        if key is None:
            continue
        values = {}
        for idx, cat in cols.items():
            raw = cell(idx)
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                values[cat] = values.get(cat, 0.0) + float(raw)
        if not values:
            continue                         # строки будущих месяцев ЦБ оставляет пустыми
        values = {c: round(v, 2) for c, v in values.items()}
        (months if key[0] == "m" else quarters)[key[1]] = values
    return months, quarters


def _quarter(month):
    return "%s-Q%d" % (month[:4], (int(month[5:7]) - 1) // 3 + 1)


def _quarter_months(quarter):
    year, n = int(quarter[:4]), int(quarter[-1])
    return ["%04d-%02d" % (year, 3 * n - 2 + i) for i in range(3)]


def _months(since, until):
    year, month = int(since[:4]), int(since[5:7])
    out = []
    while "%04d-%02d" % (year, month) <= until:
        out.append("%04d-%02d" % (year, month))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


def collect(tables):
    """[(имя файла, месяцы, кварталы)] -> (месяцы, их источник, кварталы, их источник).

    Свежесть таблицы — её последний месяц (имена у ЦБ бывают вида ORFR_2024-25-1, по
    ним порядок не определить). Месяц берётся из самой свежей таблицы, где он есть
    со всеми шестью категориями; квартал — из самой свежей таблицы, где он есть.
    """
    def newest(item):
        name, months, quarters = item
        ends = list(months) + [_quarter_months(q)[-1] for q in quarters]
        return (max(ends) if ends else "", name)

    base, base_src, official, official_src = {}, {}, {}, {}
    for name, months, quarters in sorted(tables, key=newest):
        for month, values in months.items():
            if all(c in values for c in CATS):
                base[month] = dict(values)
                base_src[month] = name
        for quarter, values in quarters.items():
            if all(c in values for c in CATS):
                official[quarter] = dict(values)
                official_src[quarter] = name
    return base, base_src, official, official_src


def rebuild(base, official, since=SINCE, until=UNTIL):
    """-> ({'YYYY-MM': {кат: млрд}}, {'YYYY-Qn': {кат: сдвиг на месяц}}, [месяцы без данных]).

    Месяц = помесячное число ЦБ + равная доля разницы между кварталом ЦБ и средним трёх
    помесячных чисел. Квартал без квартальной таблицы или без любого из трёх месяцев
    остаётся помесячным как есть.
    """
    out, shifts, absent = {}, {}, []
    for month in _months(since, until):
        if month not in base:
            absent.append(month)
            continue
        quarter = _quarter(month)
        trio = _quarter_months(quarter)
        values = {}
        for cat in CATS:
            value = base[month][cat]
            level = (official.get(quarter) or {}).get(cat)
            if level is not None and all(m in base for m in trio):
                shift = level - sum(base[m][cat] for m in trio) / 3.0
                shifts.setdefault(quarter, {})[cat] = round(shift, 2)
                value += shift
            value = round(value, 2)
            values[cat] = 0.0 if value == 0 else value
        out[month] = values
    return out, shifts, absent


# ------------------------------------------------------------------ затравка

def _fmt(value):
    return repr(round(float(value), 2))


def seed_rows(path=SEED, since=SINCE, until=UNTIL):
    """Строки затравки за период -> {'YYYY-MM': {кат: млрд}} (пустые ячейки пропускаются)."""
    out = {}
    with open(path, encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            month = (row.get("month") or "").strip()
            if not (since <= month <= until):
                continue
            values = {}
            for cat in CATS:
                raw = (row.get(cat) or "").strip()
                if raw:
                    values[cat] = float(raw)
            out[month] = values
    return out


def write_seed(rebuilt, sources, path=SEED):
    """Переписать строки затравки за пересобранные месяцы; остальные строки — байт в байт.

    sources: {'YYYY-MM': текст колонки src}.
    """
    text = Path(path).read_bytes().decode("utf-8")
    lines = text.splitlines(keepends=True)
    header = next(csv.reader([lines[0]]))
    eol = "\r\n" if lines[0].endswith("\r\n") else "\n"
    done, out = set(), []
    for line in lines:
        month = line.split(",", 1)[0].strip()
        if month in rebuilt:
            out.append(_csv_line(header, month, rebuilt[month], sources.get(month, ""), eol))
            done.add(month)
        else:
            out.append(line)
    rest = sorted(set(rebuilt) - done)
    if rest:  # месяцев, которых в затравке не было, вставляем по порядку
        body = [ln if ln.endswith(("\n", "\r")) else ln + eol for ln in out[1:] if ln.strip()]
        body += [_csv_line(header, m, rebuilt[m], sources.get(m, ""), eol) for m in rest]
        body.sort(key=lambda ln: ln.split(",", 1)[0])
        out = [out[0]] + body
    Path(path).write_bytes("".join(out).encode("utf-8"))
    return len(done) + len(rest)


def _csv_line(header, month, values, src, eol):
    buf = io.StringIO()
    row = []
    for col in header:
        if col == "month":
            row.append(month)
        elif col == "src":
            row.append(src)
        else:
            row.append(_fmt(values[col]) if col in values else "")
    csv.writer(buf, lineterminator=eol).writerow(row)
    return buf.getvalue()


# ------------------------------------------------------------------ стор

def plan_apply(rows, load=None):
    """{'YYYY-MM': {кат: млрд}} -> {ряд: {день: (в ряду, в затравке)}} только с отличиями."""
    load = load or store.load_series
    changes = {}
    for cat in CATS:
        sid = "orfr_flows_%s" % cat
        points = (load(sid) or {}).get("points") or {}
        for month, values in sorted(rows.items()):
            if cat not in values:
                continue
            day = orfr._month_end(month)
            have, new = points.get(day), values[cat]
            if have is None or abs(have - new) > SAME:
                changes.setdefault(sid, {})[day] = (have, new)
    return changes


def backup(series_id):
    src = store.series_path(series_id)
    if not src.exists():
        return None
    for n in range(1, 100):
        dst = src.with_name(src.name + BACKUP_SUFFIX + ("" if n == 1 else ".%d" % n))
        if not dst.exists():
            shutil.copy2(src, dst)
            return dst
    raise RuntimeError("%s: слишком много копий %s" % (series_id, BACKUP_SUFFIX))


def apply_changes(changes, since=SINCE, until=UNTIL):
    """Записать отличия в ряды. Время опроса (meta.fetched_at) не трогается."""
    written = {}
    for sid, days in sorted(changes.items()):
        dst = backup(sid)
        series = store.load_series(sid) or {"id": sid, "points": {}, "meta": {}}
        points = dict(series.get("points") or {})
        points.update({day: new for day, (_have, new) in days.items()})
        series["points"] = {d: points[d] for d in sorted(points)}
        series["meta"] = dict(series.get("meta") or {}, rebuild=(
            "%s…%s пересобраны по таблицам ЦБ: помесячно — приложения charts_ORFR, уровень "
            "квартала — квартальные таблицы ЦБ (seed/orfr_flows.csv, "
            "ops/rebuild_orfr_history.py); изменено точек: %d" % (since, until, len(days))))
        store.save_series(sid, series)
        written[sid] = (len(days), dst)
    return written


# ------------------------------------------------------------------ команды

def _build(args):
    tables, failed = [], []
    for name in args.workbooks:
        path = Path(name)
        try:
            months, quarters = read_table(path.read_bytes())
        except (OSError, xlsx.XlsxError, ValueError, KeyError) as exc:
            failed.append("%s: %s" % (path.name, exc))
            continue
        tables.append((path.name, months, quarters))
        print("%s: месяцев %d (%s…%s), кварталов %d"
              % (path.name, len(months), min(months) if months else "—",
                 max(months) if months else "—", len(quarters)))
    for line in failed:
        print("  не разобралась: %s" % line)
    base, base_src, official, official_src = collect(tables)
    rebuilt, shifts, absent = rebuild(base, official, args.since, args.until)
    if absent:
        print("НЕТ ДАННЫХ за месяцы: %s" % ", ".join(absent))

    seed = seed_rows(SEED, args.since, args.until)
    print("\nквартал | категория: затравка → ЦБ помесячно → квартал ЦБ (сдвиг на месяц)")
    for quarter in sorted({_quarter(m) for m in rebuilt}):
        trio = _quarter_months(quarter)
        parts = []
        for cat in CATS:
            have = [seed.get(m, {}).get(cat) for m in trio]
            was = ("%.2f" % (sum(have) / 3)) if all(v is not None for v in have) else "?"
            got = [base[m][cat] for m in trio if m in base]
            monthly = ("%.2f" % (sum(got) / 3)) if len(got) == 3 else "?"
            level = (official.get(quarter) or {}).get(cat)
            parts.append("%s %s → %s → %s (%+.2f)" % (
                cat, was, monthly, "—" if level is None else "%.2f" % level,
                (shifts.get(quarter) or {}).get(cat, 0.0)))
        print("%s [%s; %s] %s" % (quarter, base_src.get(trio[0], "—"),
                                   official_src.get(quarter, "—"), "; ".join(parts)))

    changed = sum(1 for m, values in rebuilt.items() for c, v in values.items()
                  if seed.get(m, {}).get(c) is None or abs(seed[m][c] - v) > SAME)
    print("\nв затравке изменится значений: %d из %d" % (changed, 6 * len(rebuilt)))
    if args.write_seed:
        sources = {}
        for month in rebuilt:
            quarter = _quarter(month)
            src = "cbr %s" % base_src[month]
            if quarter in shifts and any(abs(s) > 0 for s in shifts[quarter].values()):
                src += "; квартал — %s" % official_src[quarter]
            sources[month] = src
        n = write_seed(rebuilt, sources)
        print("затравка переписана: строк %d (%s)" % (n, SEED.relative_to(ROOT)))
    return 0 if not absent else 1


def _apply(args):
    rows = seed_rows(SEED, args.since, args.until)
    if not rows:
        print("в затравке нет строк за %s…%s" % (args.since, args.until), file=sys.stderr)
        return 2
    changes = plan_apply(rows)
    total = sum(len(days) for days in changes.values())
    for sid in sorted(changes):
        for day, (have, new) in sorted(changes[sid].items()):
            print("  %s %s: %s → %s" % (sid, day, "—" if have is None else have, new))
    if args.apply and changes:
        for sid, (n, dst) in sorted(apply_changes(changes, args.since, args.until).items()):
            print("  %s: записано %d, копия %s" % (sid, n, dst.name if dst else "—"))
    print(("заменено" if args.apply else "заменил бы") + " точек: %d (рядов %d)"
          % (total, len(changes)))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="по таблицам ЦБ посчитать месяцы (и переписать затравку)")
    b.add_argument("workbooks", nargs="+", help="скачанные таблицы ЦБ (.xlsx)")
    b.add_argument("--write-seed", action="store_true",
                   help="переписать строки seed/orfr_flows.csv за период")
    a = sub.add_parser("apply", help="перенести строки затравки за период в ряды стора")
    a.add_argument("--apply", action="store_true",
                   help="записать в ряды (по умолчанию — показать)")
    for p in (b, a):
        p.add_argument("--since", default=SINCE, help="первый месяц ГГГГ-ММ")
        p.add_argument("--until", default=UNTIL, help="последний месяц ГГГГ-ММ")
    args = ap.parse_args(argv)
    return _build(args) if args.cmd == "build" else _apply(args)


if __name__ == "__main__":
    sys.exit(main())
