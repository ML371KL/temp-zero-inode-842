#!/usr/bin/env python3
"""Убрать из рядов live_* точки-фантомы, записанные днём ОПРОСА, а не днём сделки.

До 10.11.2026 tinvest.live_quotes датировал живую цену моментом опроса (аудит
10.11.2026, §3.8). Последний такт в 00:00 МСК записывал цену вчерашней сессии
сегодняшним числом: так в рядах появились точки по субботам и праздникам и точка
«сегодня» с вчерашней ценой. Витрина читает только последнюю точку ряда, поэтому
старые фантомы на экран не попадают, но они лежат в сторе и в зеркале raw/.

Фантом — точка ряда live_X с датой d, у которой нет закрытия в дневном ряду X, и
  * d раньше последнего закрытия X (прошедший неторговый день: суббота, праздник), или
  * d позже последнего закрытия X, а цена равна предыдущей живой точке (копия).

По умолчанию только показывает, что убрал бы. С --apply переписывает ряды в сторе
STATE_DIR и помечает их к выгрузке в зеркало. На боевом сервере запускать только по
решению владельца, ПОСЛЕ выкладки исправленного live_quotes (иначе следующий такт
в полночь запишет фантом снова):

    STATE_DIR=/var/lib/moex-radar python3 ops/clean_live_phantoms.py            # показать
    STATE_DIR=/var/lib/moex-radar python3 ops/clean_live_phantoms.py --apply    # убрать
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.lib import store  # noqa: E402

PREFIX = "live_"


def phantoms(live_points, close_points):
    """[дата] фантомов ряда live_X по его точкам и закрытиям X (оба {дата: число})."""
    closes = {d for d, v in close_points.items() if isinstance(v, (int, float))}
    last_close = max(closes) if closes else None
    out, prev = [], None
    for d in sorted(live_points):
        v = live_points[d]
        if d not in closes and last_close is not None:
            if d < last_close or (prev is not None and v == prev):
                out.append(d)
                continue
        prev = v
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="переписать ряды (по умолчанию — показать)")
    args = ap.parse_args(argv)
    total = 0
    for sid in store.list_series():
        if not sid.startswith(PREFIX):
            continue
        live = store.load_series(sid) or {}
        base = store.load_series(sid[len(PREFIX):]) or {}
        pts = live.get("points") or {}
        bad = phantoms(pts, base.get("points") or {})
        total += len(bad)
        print(f"{sid}: точек {len(pts)}, фантомов {len(bad)}"
              + (f": {', '.join(bad)}" if bad else ""))
        if bad and args.apply:
            live["points"] = {d: v for d, v in pts.items() if d not in set(bad)}
            store.save_series(sid, live)
    print(("убрано" if args.apply else "убрал бы") + f" {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
