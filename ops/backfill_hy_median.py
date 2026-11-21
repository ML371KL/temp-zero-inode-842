#!/usr/bin/env python3
"""Досчитать ряд доходности ВДО (rucbhycp_yield) взвешенной медианой состава за прошлые дни.

Зачем (21.11.2026). С выкладки аудита 20.11.2026 значение дня у ВДО — взвешенная медиана
доходностей бумаг индекса (iss.index_yield_median): биржевая средневзвешенная ломается
на бумагах у дефолта (за 17.11.2026 биржа отдаёт 121,28 %). Но медиана считается по
ЖИВЫМ котировкам, то есть только за текущий день. Разовая чистка 20.11 убрала из ряда
биржевой мусор 21.10–18.11, и там осталась дыра; до 21.10 в ряду вперемешку биржевые
числа и оценки прежних методов (86,51 % за 07.10, 45 % за 23–24.09). Из-за дыры и
разнородности тайл не показывает перцентиль за год и изменение за 21 день.

Что делает. Тем же методом считает прошлые дни по истории биржи: состав индекса с
весами на дату (statistics/.../analytics/RUCBHYCP?date=) и доходность закрытия каждой
бумаги (history/.../bonds/securities/<бумага>, YIELDCLOSE; основной режим TQ*, ноль и
минус — «сделок не было»). Торговые дни — дни истории самого индекса: в году бывают
рабочие субботы (01.11.2025). День, где котировки закрытия покрывают меньше 90 % веса,
не считается — как и в живом расчёте. Сверка: 19.11.2026 — 35,63 % по истории против
35,69 % по снимку котировок закрытия (аудит 20.11.2026, §8).

Два шага:

    # 1) расчёт — запросы к бирже (~1 400), БЕЗ замка стора, чтобы не держать такты:
    python3 ops/backfill_hy_median.py compute --since 2025-11-01 --until 2026-11-18 \\
        --out /tmp/hy-median.json
    # 2) запись — под замком стора и от dash; по умолчанию только показывает:
    STATE_DIR=/var/lib/moex-radar flock -w 240 /var/lib/moex-radar/.run.lock \\
        python3 ops/backfill_hy_median.py apply /tmp/hy-median.json [--apply]

Запись: точки периода заменяются медианой, их дни добавляются в meta.estimated_days (окно
догрузки их больше не переписывает); точки периода, за которые медиана не посчиталась,
убираются — в ряду не остаётся чисел другого метода. Вне периода ничего не меняется,
время опроса (meta.fetched_at) тоже. Копия ряда — raw/rucbhycp_yield.json.before-hy-backfill.
Если посчитано меньше 90 % торговых дней периода, запись отказывается: сеть подвела —
не повод стирать ряд. Повторный apply ничего не меняет.
"""

import argparse
import json
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.fetch import iss  # noqa: E402
from pipeline.lib import http, store  # noqa: E402

SEC = "RUCBHYCP"
SID = "rucbhycp_yield"
MIN_WEIGHT = 90.0          # % веса индекса с котировкой закрытия — как в живом расчёте
MIN_DAYS_SHARE = 0.9       # доля посчитанных торговых дней, без которой запись отказывает
BACKUP_SUFFIX = ".before-hy-backfill"
WORKERS = 4
PAGE = 100
MAX_PAGES = 100
SAME = 0.005


# ------------------------------------------------------------------ биржа

def _pages(path, params, block):
    """Все строки блока ISS с листанием по курсору -> (колонки, строки)."""
    columns, rows, start = None, [], 0
    for _ in range(MAX_PAGES):
        query = dict(params, start=start, limit=PAGE)
        query["iss.only"] = "%s,%s.cursor" % (block, block)
        payload = http.get_json(iss._url(path, query))
        part = payload.get(block) or {}
        columns = columns or part.get("columns") or []
        data = part.get("data") or []
        rows.extend(data)
        cursor = ((payload.get(block + ".cursor") or {}).get("data") or [[]])[0]
        if len(cursor) >= 3 and isinstance(cursor[1], int):
            start = cursor[0] + cursor[2]
            if not data or start >= cursor[1]:
                break
        else:
            if len(data) < PAGE:
                break
            start += len(data)
    return columns or [], rows


def trading_days(since, until):
    """Дни истории индекса в периоде — торговый календарь ряда."""
    cols, rows = _pages("history/engines/stock/markets/index/securities/%s" % SEC,
                        {"from": since, "till": until, "history.columns": "TRADEDATE"},
                        "history")
    i = cols.index("TRADEDATE")
    return sorted({str(r[i])[:10] for r in rows if r[i]})


def composition(day):
    """Состав индекса на дату -> ({бумага: вес, %}, дата состава у биржи)."""
    cols, rows = _pages("statistics/engines/stock/markets/index/analytics/%s" % SEC,
                        {"date": day}, "analytics")
    ix = {name: n for n, name in enumerate(cols)}
    out, comp_day = {}, None
    for row in rows:
        sec, weight = row[ix["secids"]], row[ix["weight"]]
        if sec is None or isinstance(weight, bool) or not isinstance(weight, (int, float)):
            continue
        out[str(sec)] = float(weight)
        if comp_day is None and "tradedate" in ix:
            comp_day = str(row[ix["tradedate"]])[:10]
    return out, comp_day


def bond_closes(secid, since, until):
    """Доходность закрытия бумаги по дням -> {день: %}. Основной режим (TQ*) — первым."""
    cols, rows = _pages("history/engines/stock/markets/bonds/securities/%s" % secid,
                        {"from": since, "till": until,
                         "history.columns": "TRADEDATE,BOARDID,YIELDCLOSE"}, "history")
    ix = {name: n for n, name in enumerate(cols)}
    out, main = {}, set()
    for row in rows:
        day, value = str(row[ix["TRADEDATE"]])[:10], row[ix["YIELDCLOSE"]]
        board = str(row[ix["BOARDID"]] or "")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            continue                                   # ноль и минус — «сделок не было»
        if day in main:
            continue
        if board.startswith("TQ"):
            out[day] = float(value)
            main.add(day)
        else:
            out.setdefault(day, float(value))
    return out


def compute(since, until, workers=WORKERS, say=print):
    """Медиана состава по закрытию за каждый торговый день периода -> результат (dict)."""
    days = trading_days(since, until)
    if not days:
        raise http.FetchError("история %s за %s…%s пуста" % (SEC, since, until))
    say("торговых дней %d (%s…%s)" % (len(days), days[0], days[-1]))

    def safe_composition(day):
        try:
            return composition(day)
        except http.FetchError as exc:
            return {}, "ошибка: %s" % str(exc)[:120]

    def safe_closes(secid):
        try:
            return bond_closes(secid, since, until)
        except http.FetchError as exc:
            say("  %s: история не получена (%s)" % (secid, str(exc)[:120]))
            return {}

    with ThreadPoolExecutor(workers) as pool:
        comps = dict(zip(days, pool.map(safe_composition, days)))
    secids = sorted({s for weights, _day in comps.values() for s in weights})
    say("бумаг в составах за период: %d" % len(secids))
    with ThreadPoolExecutor(workers) as pool:
        closes = dict(zip(secids, pool.map(safe_closes, secids)))

    out, skipped = {}, {}
    for day in days:
        weights, comp_day = comps[day]
        if not weights:
            skipped[day] = "состав не отдан" + ("" if not comp_day else " (%s)" % comp_day)
            continue
        hit = [(w, closes[s][day]) for s, w in weights.items() if day in closes.get(s, {})]
        covered = sum(w for w, _ in hit)
        if covered < MIN_WEIGHT:
            skipped[day] = "котировки закрытия покрывают %.1f %% веса" % covered
            continue
        value = iss._weighted_median(hit)
        if value is None:
            skipped[day] = "нет ни одной котировки"
            continue
        out[day] = {"value": round(value, 2), "cover_pct": round(covered, 1),
                    "count": len(hit), "no_quote": len(weights) - len(hit),
                    "composition_day": comp_day}
    return {"sec": SEC, "since": since, "until": until, "trading_days": days,
            "days": out, "skipped": skipped}


# ------------------------------------------------------------------ стор

def plan(result, series):
    """-> (новые точки, [(день, было, стало)], [(день, стало)], [(день, было)] к удалению)."""
    since, until = result["since"], result["until"]
    points = {d: v for d, v in ((series or {}).get("points") or {}).items() if v is not None}
    new = {d: row["value"] for d, row in result["days"].items() if since <= d <= until}
    changed = [(d, points[d], v) for d, v in sorted(new.items())
               if d in points and abs(points[d] - v) > SAME]
    added = [(d, v) for d, v in sorted(new.items()) if d not in points]
    removed = [(d, points[d]) for d in sorted(points)
               if since <= d <= until and d not in new]
    return new, changed, added, removed


def enough(result):
    days = result.get("trading_days") or []
    return bool(days) and len(result["days"]) >= MIN_DAYS_SHARE * len(days)


def backup():
    src = store.series_path(SID)
    if not src.exists():
        return None
    for n in range(1, 100):
        dst = src.with_name(src.name + BACKUP_SUFFIX + ("" if n == 1 else ".%d" % n))
        if not dst.exists():
            shutil.copy2(src, dst)
            return dst
    raise RuntimeError("%s: слишком много копий %s" % (SID, BACKUP_SUFFIX))


def write(result, new, removed):
    since, until = result["since"], result["until"]
    series = store.load_series(SID) or {"id": SID, "unit": "pct", "points": {}, "meta": {}}
    points = {d: v for d, v in (series.get("points") or {}).items()
              if not (since <= d <= until)}
    points.update(new)
    meta = dict(series.get("meta") or {})
    estimated = {str(d)[:10] for d in (meta.get("estimated_days") or [])
                 if not (since <= str(d)[:10] <= until)}
    meta["estimated_days"] = sorted((estimated | set(new)) & set(points))
    meta["backfill"] = ("%s…%s: взвешенная медиана состава по доходностям закрытия "
                        "(ops/backfill_hy_median.py); дней %d, убрано точек другого "
                        "метода %d" % (since, until, len(new), len(removed)))
    series["points"] = {d: points[d] for d in sorted(points)}
    series["meta"] = meta
    store.save_series(SID, series)


# ------------------------------------------------------------------ команды

def _compute(args):
    result = compute(args.since, args.until, args.workers)
    days = result["days"]
    for day, why in sorted(result["skipped"].items()):
        print("  не посчитан %s: %s" % (day, why))
    if days:
        months = {}
        for day in sorted(days):
            months.setdefault(day[:7], []).append(days[day]["value"])
        print("по месяцам (первый день — последний день):")
        for month, values in sorted(months.items()):
            print("  %s: %.2f … %.2f (дней %d)" % (month, values[0], values[-1], len(values)))
        low = min(days.values(), key=lambda r: r["cover_pct"])
        print("наименьшее покрытие: %.1f %%" % low["cover_pct"])
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print("посчитано дней %d из %d; не посчитано %d; файл %s"
          % (len(days), len(result["trading_days"]), len(result["skipped"]), args.out))
    return 0 if enough(result) else 1


def _apply(args):
    result = json.loads(Path(args.result).read_text(encoding="utf-8"))
    if result.get("sec") != SEC:
        print("файл не про %s" % SEC, file=sys.stderr)
        return 2
    if not enough(result):
        print("посчитано %d дней из %d — меньше %d %%; не пишу"
              % (len(result["days"]), len(result.get("trading_days") or []),
                 MIN_DAYS_SHARE * 100), file=sys.stderr)
        return 2
    new, changed, added, removed = plan(result, store.load_series(SID))
    for day, was, now in changed[:12]:
        print("  %s: %s → %s" % (day, was, now))
    if len(changed) > 12:
        print("  … ещё изменений: %d" % (len(changed) - 12))
    for day, was in removed:
        print("  убрать %s: %s (медиана за день не посчиталась)" % (day, was))
    print("период %s…%s: заменить %d, добавить %d, убрать %d"
          % (result["since"], result["until"], len(changed), len(added), len(removed)))
    if args.apply and (changed or added or removed):
        dst = backup()
        write(result, new, removed)
        print("записано; копия %s" % (dst.name if dst else "—"))
    elif args.apply:
        print("менять нечего")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("compute", help="посчитать медиану за период по истории биржи")
    c.add_argument("--since", required=True, help="первый день ГГГГ-ММ-ДД")
    c.add_argument("--until", required=True, help="последний день ГГГГ-ММ-ДД")
    c.add_argument("--out", required=True, help="куда записать результат (JSON)")
    c.add_argument("--workers", type=int, default=WORKERS, help="параллельных запросов")
    a = sub.add_parser("apply", help="записать результат расчёта в ряд стора")
    a.add_argument("result", help="файл, который написал compute")
    a.add_argument("--apply", action="store_true", help="записать (по умолчанию — показать)")
    args = ap.parse_args(argv)
    return _compute(args) if args.cmd == "compute" else _apply(args)


if __name__ == "__main__":
    sys.exit(main())
