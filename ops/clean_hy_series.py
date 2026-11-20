#!/usr/bin/env python3
"""Разовая чистка рядов после аудита 20.11.2026: ВДО и затравочные нули аукционов.

1. РЯД ВДО (rucbhycp_yield). С 21.10.2026 биржевая доходность индекса RUCBHYCP лежит
   внутри «разумного» коридора, но числом не является: средневзвешенную ломают
   бумаги на грани дефолта (35 -> 41 -> 48 -> 55 -> 70 -> 96 % при медиане состава
   около 36 %). Прежние фильтры это пропускали, и точки лежат в сторе. Исправленный
   фетчер (fetch/iss._index_yield_guarded) больше их не пишет, но и старые сам не
   убирает — убирает этот скрипт.

   Удаляются точки с --since (по умолчанию 2026-10-21) по конец ряда, которые НЕ
   числятся оценкой панели (meta.estimated_days). Оценки прежнего метода
   (средневзвешенная с отсечкой, например 40,40 % за 17.11.2026) в этот перечень не
   входят и тоже удаляются: в одном ряду с медианой они дали бы ступеньку от смены
   метода. Поля прошлой оценки в meta сбрасываются.

2. АУКЦИОНЫ ОФЗ (ofz_auctions, ofz_auction_failed). В затравке ряда лежат нули за
   24.06.2026 и 08.07.2026 — «аукцион не состоялся». На биржевой доске PACT за эти
   дни строк нет вовсе (живой запрос 20.11.2026): аукционов в эти дни не было, а не
   «были и провалились». Точки удаляются из обоих рядов.

Копия каждого ряда кладётся рядом: raw/<ряд>.json.before-median.

По умолчанию только показывает, что убрал бы. С --apply переписывает ряды в сторе
STATE_DIR и помечает их к выгрузке в зеркало. На боевом сервере запускать по решению
владельца, ПОСЛЕ выкладки исправленного кода (иначе следующий прогон запишет
биржевые точки снова):

    STATE_DIR=/var/lib/moex-radar python3 ops/clean_hy_series.py            # показать
    STATE_DIR=/var/lib/moex-radar python3 ops/clean_hy_series.py --apply    # убрать
"""

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.lib import store  # noqa: E402

HY_SERIES = "rucbhycp_yield"
HY_SINCE = "2026-10-21"
# Поля meta прошлой оценки: после чистки они описывали бы точки, которых нет.
HY_META_RESET = ("method", "estimate_method", "estimate_asof", "estimate_cover_pct",
                 "estimate_count", "estimate_dropped", "estimate_error",
                 "xcheck_constituents", "xcheck_diff_pp", "dropped_outliers",
                 "exchange_last", "exchange_skipped")
BACKUP_SUFFIX = ".before-median"

# Затравочные нули аукционов: дни, за которые на доске PACT нет ни одной строки.
AUCTION_PHANTOM_DAYS = ("2026-06-24", "2026-07-08")
AUCTION_SERIES = ("ofz_auctions", "ofz_auction_failed")


def hy_victims(points, estimated, since=HY_SINCE):
    """[день] точек ряда ВДО с since, которые не являются оценкой панели."""
    keep = {str(d)[:10] for d in (estimated or [])}
    return sorted(d for d in points if d >= since and d not in keep)


def backup(series_id):
    """Копия файла ряда рядом; занятое имя не затирается (.before-median.2 и далее)."""
    src = store.series_path(series_id)
    for n in range(1, 100):
        dst = src.with_name(src.name + BACKUP_SUFFIX + ("" if n == 1 else f".{n}"))
        if not dst.exists():
            shutil.copy2(src, dst)
            return dst
    raise RuntimeError(f"{series_id}: слишком много копий {BACKUP_SUFFIX}")


def clean_hy(apply, since=HY_SINCE):
    series = store.load_series(HY_SERIES)
    if not series:
        print(f"{HY_SERIES}: ряда нет — чистить нечего")
        return 0
    points = series.get("points") or {}
    meta = series.get("meta") or {}
    bad = hy_victims(points, meta.get("estimated_days"), since)
    kept = sorted(d for d in points if d >= since and d not in set(bad))
    print(f"{HY_SERIES}: точек {len(points)}, с {since} — {len(bad) + len(kept)}; "
          f"оценок панели (остаются) {len(kept)}, к удалению {len(bad)}")
    for d in bad:
        print(f"  − {d} = {points[d]}")
    if bad and apply:
        dst = backup(HY_SERIES)
        series["points"] = {d: v for d, v in points.items() if d not in set(bad)}
        left = [d for d, v in series["points"].items() if v is not None]
        last = max(left) if left else None
        estimated = [d for d in (meta.get("estimated_days") or []) if d in series["points"]]
        meta = dict(meta, asof=last, estimated_days=estimated,
                    note=f"чистка {BACKUP_SUFFIX[1:]}: убраны биржевые точки с {since}, "
                         f"не подтверждённые оценкой по составу")
        if not (last and last in estimated):
            for key in HY_META_RESET:
                meta[key] = None
        series["meta"] = meta
        store.save_series(HY_SERIES, series)
        print(f"  копия: {dst.name}; последняя точка ряда теперь {last}")
    return len(bad)


def clean_auctions(apply, days=AUCTION_PHANTOM_DAYS):
    total = 0
    for sid in AUCTION_SERIES:
        series = store.load_series(sid)
        if not series:
            print(f"{sid}: ряда нет")
            continue
        points = series.get("points") or {}
        # В ряду размещений фантом — именно НОЛЬ: если за день лежит объём, это уже
        # не затравка, и трогать его нельзя.
        bad = [d for d in days if d in points
               and (sid != "ofz_auctions" or not points[d])]
        total += len(bad)
        print(f"{sid}: точек {len(points)}, к удалению {len(bad)}"
              + (": " + ", ".join(f"{d}={points[d]}" for d in bad) if bad else ""))
        if bad and apply:
            dst = backup(sid)
            series["points"] = {d: v for d, v in points.items() if d not in set(bad)}
            store.save_series(sid, series)
            print(f"  копия: {dst.name}")
    return total


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true",
                    help="переписать ряды (по умолчанию — показать)")
    ap.add_argument("--since", default=HY_SINCE,
                    help=f"с какого дня чистить ряд ВДО (по умолчанию {HY_SINCE})")
    ap.add_argument("--only", choices=("hy", "auctions"),
                    help="чистить только один из двух рядов")
    args = ap.parse_args(argv)
    total = 0
    if args.only in (None, "hy"):
        total += clean_hy(args.apply, args.since)
    if args.only in (None, "auctions"):
        total += clean_auctions(args.apply)
    print(("убрано" if args.apply else "убрал бы") + f" {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
