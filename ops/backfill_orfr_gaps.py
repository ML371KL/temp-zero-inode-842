#!/usr/bin/env python3
"""Долить ПРОПУСКИ в рядах потоков ОРФР из таблицы ЦБ. Существующие точки не трогает.

Зачем (аудит 20.11.2026). В рядах orfr_flows_szko и orfr_flows_other_banks пусты ноябрь
и декабрь 2025: затравка собиралась разбором текста PDF, а эти две категории в тексте
обзора за те месяцы не названы. В приложении-таблице ЦБ к выпуску за декабрь 2025 —
январь 2026 (ORFR_2025-12_2026-01.xlsx, лист «рис. 21») значения есть: СЗКО −5,68 и
1,93, прочие банки −0,85 и 2,27 млрд руб.

Что делает: читает таблицу тем же разбором, что и фетчер (orfr.parse_workbook; лист
с потоками акций находится по заголовку — в старых выпусках он назывался «рис. N»),
и пишет в шесть рядов orfr_flows_* ТОЛЬКО те месяцы, которых в ряду нет. Расхождения
существующих точек с таблицей печатает и не исправляет: ЦБ пересматривает данные от
выпуска к выпуску. Историю 2023–2025 целиком пересобирает ops/rebuild_orfr_history.py
(помесячные приложения charts_ORFR + квартальные таблицы ЦБ).

Копия каждого изменённого ряда кладётся рядом: raw/<ряд>.json.before-orfr-fill.
По умолчанию только показывает, что долил бы:

    STATE_DIR=/var/lib/moex-radar python3 ops/backfill_orfr_gaps.py ORFR_2025-12_2026-01.xlsx
    STATE_DIR=/var/lib/moex-radar python3 ops/backfill_orfr_gaps.py ORFR_2025-12_2026-01.xlsx --apply

Файл — локальный путь (в сеть скрипт не ходит). Период выпуска берётся из имени файла
либо задаётся --period ГГГГ-ММ.
"""

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.fetch import orfr  # noqa: E402
from pipeline.lib import store, xlsx  # noqa: E402

BACKUP_SUFFIX = ".before-orfr-fill"
# Расхождение существующей точки с таблицей, о котором стоит сказать (млрд руб):
# затравка хранит один знак после запятой, таблица — два.
DIFF_NOTE = 0.1


def plan(months, load=None):
    """({ряд: {день: значение}} к доливу, [(ряд, день, в ряду, в таблице)] расхождений)."""
    load = load or store.load_series
    fill, diffs = {}, []
    for month, values in sorted(months.items()):
        day = orfr._month_end(month)
        for cat, value in sorted(values.items()):
            sid = "orfr_flows_%s" % cat
            points = (load(sid) or {}).get("points") or {}
            have = points.get(day)
            if have is None:
                fill.setdefault(sid, {})[day] = value
            elif abs(have - value) >= DIFF_NOTE:
                diffs.append((sid, day, have, value))
    return fill, diffs


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


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("workbook", help="путь к таблице ЦБ (.xlsx)")
    ap.add_argument("--period", help="период выпуска ГГГГ-ММ (по умолчанию — из имени файла)")
    ap.add_argument("--apply", action="store_true",
                    help="записать в ряды (по умолчанию — показать)")
    args = ap.parse_args(argv)

    path = Path(args.workbook)
    period = args.period or orfr._period_from_name(path.name)
    if not period:
        print("период выпуска не определился по имени %s — задайте --period ГГГГ-ММ"
              % path.name, file=sys.stderr)
        return 2
    data = path.read_bytes()
    try:
        sheet = orfr.equity_sheet(xlsx.open_bytes(data))
        months, notes = orfr.parse_workbook(data, period, sheet=sheet)
    except (xlsx.XlsxError, ValueError, KeyError) as exc:
        print("таблица не разобралась: %s" % exc, file=sys.stderr)
        return 1
    print("таблица %s: лист «%s», месяцев %d (%s)"
          % (path.name, sheet, len(months), ", ".join(sorted(months))))
    for line in notes:
        print("  замечание: %s" % line)

    fill, diffs = plan(months)
    total = sum(len(v) for v in fill.values())
    for sid in sorted(fill):
        for day, value in sorted(fill[sid].items()):
            print("  + %s %s = %s" % (sid, day, value))
    for sid, day, have, value in diffs:
        print("  ≠ %s %s: в ряду %s, в таблице %s — НЕ меняю" % (sid, day, have, value))
    if args.apply:
        for sid, points in sorted(fill.items()):
            dst = backup(sid)
            # save_series, а не upsert_points: долив — не опрос источника, и время
            # последнего опроса (meta.fetched_at) он освежать не должен, иначе ряд
            # выглядел бы только что опрошенным.
            series = store.load_series(sid) or {"id": sid, "points": {}, "meta": {}}
            merged = dict(series.get("points") or {})
            merged.update(points)
            series["points"] = {d: merged[d] for d in sorted(merged)}
            series["meta"] = dict(series.get("meta") or {},
                                  backfill="пропуски долиты из %s (лист «%s»): %s"
                                           % (path.name, sheet, ", ".join(sorted(points))))
            store.save_series(sid, series)
            print("  %s: записано %d, копия %s" % (sid, len(points), dst.name if dst else "—"))
    print(("долито" if args.apply else "долил бы") + " %d; расхождений без правки %d"
          % (total, len(diffs)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
