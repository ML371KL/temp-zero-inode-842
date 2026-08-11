#!/usr/bin/env python3
"""Затравка стора историей, уже собранной исследованием.

ПОЧЕМУ отдельный скрипт, а не `--mode bootstrap`: полный бутстрап с нуля стоит
~5000 запросов к ISS (одна только КБД — 2900 дней по одному запросу на дату,
FUTOI — 750, ширина рынка — 1300). Эти ряды уже скачаны в ходе валидации и лежат
рядом; тянуть их второй раз значит без нужды биться в чужие лимиты с одного IP.
Скрипт разовый: после него дневной прогон работает инкрементально.

Источники затравки:
  1. seed/*.csv в самом репозитории — незаменимое (потоки ОРФР, консенсусы ЦБ,
     налоговая Urals, недельный ИПЦ, аукционы, реестр событий). См. seed/README.md.
  2. --validation <путь> — рыночные ряды из validation/data (idx_*.csv, fx_*.csv,
     zcyc_daily.csv, futoi_MX.csv, stocks_daily.csv, cbr_*.csv, brent.csv).
     Каталог не входит в репозиторий: он большой и полностью восстановим у источника.

Запуск:
  python ops/seed_store.py --validation ../moex-drivers/validation/data
"""

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline.lib import store  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def _put(series_id, points, unit, cadence, source, note=None):
    """Записать ряд, не затирая уже накопленное пайплайном."""
    points = {d: v for d, v in points.items() if v is not None}
    if not points:
        print(f"  {series_id}: пусто, пропуск")
        return 0
    store.upsert_points(series_id, points, {
        "source": source, "unit": unit, "cadence": cadence,
        "status": "ok", "note": note or "затравка из исследования (ops/seed_store.py)",
    })
    print(f"  {series_id}: {len(points)} точек, {min(points)} .. {max(points)}")
    return len(points)


def _read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _num(row, key):
    raw = (row.get(key) or "").strip()
    if raw in ("", "nan", "None", "NaN"):
        return None
    try:
        return float(raw)
    except ValueError:
        return None


# ---------------------------------------------------------------- рыночные ряды
# (файл в validation/data, колонка даты, колонка значения) -> series_id
MARKET = [
    ("idx_IMOEX.csv", "TRADEDATE", "CLOSE", "imoex", "points"),
    ("idx_IMOEX.csv", "TRADEDATE", "VALUE", "imoex_value", "rub"),
    ("idx_MCFTR.csv", "TRADEDATE", "CLOSE", "mcftr", "points"),
    ("idx_RGBI.csv", "TRADEDATE", "CLOSE", "rgbi", "points"),
    ("idx_RVI.csv", "TRADEDATE", "CLOSE", "rvi", "points"),
    ("idx_MCXSM.csv", "TRADEDATE", "CLOSE", "mcxsm", "points"),
    ("idx_RTSI.csv", "TRADEDATE", "CLOSE", "rtsi", "points"),
    ("idx_IMOEX2.csv", "TRADEDATE", "CLOSE", "imoex2", "points"),
    ("idx_RUSFAR3M.csv", "TRADEDATE", "CLOSE", "rusfar3m", "pct"),
    ("idx_RUCBHYCP.csv", "TRADEDATE", "YIELD", "rucbhycp_yield", "pct"),
    ("idx_RUCBCPNS.csv", "TRADEDATE", "YIELD", "rucbcpns_yield", "pct"),
    ("idx_MREDC.csv", "TRADEDATE", "CLOSE", "mredc", "rub_m2"),
    ("fx_CNYRUB_TOM.csv", "TRADEDATE", "CLOSE", "cny_tom", "rub"),
    ("fx_GLDRUB_TOM.csv", "TRADEDATE", "CLOSE", "gld_tom", "rub_g"),
    ("cbr_usd.csv", "date", "rate", "usd_cbr", "rub"),
    ("cbr_cny.csv", "date", "rate", "cny_cbr", "rub"),
    ("cbr_keyrate.csv", "date", "rate", "key_rate", "pct"),
    ("cbr_deposit.csv", "date", "rate", "deposit_decade", "pct"),
    ("brent.csv", "date", "brent", "brent", "usd_bbl"),
]

ZCYC_COLS = {"y0.5": "zcyc_y0_5", "y1.0": "zcyc_y1", "y2.0": "zcyc_y2",
             "y5.0": "zcyc_y5", "y10.0": "zcyc_y10"}


def seed_market(data_dir):
    total = 0
    for fname, dcol, vcol, sid, unit in MARKET:
        path = data_dir / fname
        if not path.exists():
            print(f"  {sid}: нет файла {fname}, пропуск")
            continue
        rows = _read_csv(path)
        pts = {}
        for r in rows:
            d = (r.get(dcol) or "")[:10]
            v = _num(r, vcol)
            if d and v is not None:
                pts[d] = v
        cad = "decade" if sid == "deposit_decade" else "daily"
        total += _put(sid, pts, unit, cad, "validation-seed")

    # КБД: одна строка на дату, пять сроков в колонках
    zp = data_dir / "zcyc_daily.csv"
    if zp.exists():
        rows = _read_csv(zp)
        for col, sid in ZCYC_COLS.items():
            pts = {}
            for r in rows:
                d = (r.get("date") or "")[:10]
                v = _num(r, col)
                if d and v is not None:
                    pts[d] = v
            total += _put(sid, pts, "pct", "daily", "validation-seed")

    # FUTOI: последняя запись дня по группе
    fp = data_dir / "futoi_MX.csv"
    if fp.exists():
        rows = _read_csv(fp)
        cols = {("FIZ", "pos"): "futoi_mx_pos", ("FIZ", "pos_long"): "futoi_mx_long",
                ("FIZ", "pos_short"): "futoi_mx_short",
                ("FIZ", "long_num"): "futoi_mx_holders_long",
                ("FIZ", "short_num"): "futoi_mx_holders_short",
                ("YUR", "pos"): "futoi_mx_yur_pos"}
        buckets = {sid: {} for sid in cols.values()}
        for r in rows:
            grp, d = r.get("clgroup"), (r.get("date") or "")[:10]
            for (g, field), sid in cols.items():
                if g == grp:
                    v = _num(r, field)
                    if d and v is not None:
                        buckets[sid][d] = v
        for sid, pts in buckets.items():
            unit = "persons" if "holders" in sid else "contracts"
            total += _put(sid, pts, unit, "daily", "validation-seed")

    # Ширина рынка: пересчитываем агрегат «доля выше 200-дневной» из дневных цен
    sp = data_dir / "stocks_daily.csv"
    if sp.exists():
        by_tic = {}
        for r in _read_csv(sp):
            d, t, v = (r.get("TRADEDATE") or "")[:10], r.get("SECID"), _num(r, "px")
            if d and t and v is not None:
                by_tic.setdefault(t, {})[d] = v
        all_dates = sorted({d for s in by_tic.values() for d in s})
        above, cnt = {}, {}
        for t, series in by_tic.items():
            ds = sorted(series)
            vals = [series[d] for d in ds]
            for i in range(len(ds)):
                if i < 199:
                    continue
                window = [x for x in vals[i - 199:i + 1] if x is not None]
                if len(window) < 150:
                    continue
                ma = sum(window) / len(window)
                d = ds[i]
                cnt[d] = cnt.get(d, 0) + 1
                if vals[i] > ma:
                    above[d] = above.get(d, 0) + 1
        pts = {d: round(above.get(d, 0) / cnt[d], 6) for d in all_dates
               if cnt.get(d, 0) >= 15}
        total += _put("breadth", pts, "share", "daily", "validation-seed",
                      "доля бумаг выше 200-дневной, пересчитано из дневных цен")

        # Сырые цены бумаг тоже кладём в стор. Без них фетчер ширины видит пустой
        # стор и на КАЖДОМ прогоне тянет 45 бумаг с 2014 года постранично — это
        # ~1300 запросов и 15 минут (замерено на первом прогоне). С затравкой он
        # дочитывает только новые дни.
        # Имя ряда — В НИЖНЕМ РЕГИСТРЕ: ровно так их пишет fetch/iss.breadth
        # (px_sber), и только при совпадении имён фетчер увидит затравку своей и
        # пойдёт инкрементально, а не потянет 45 бумаг с 2014 года заново.
        for tic, series in by_tic.items():
            total += _put(f"px_{tic.lower()}", series, "rub", "daily", "validation-seed",
                          "цены для расчёта ширины рынка")
    return total


# ------------------------------------------------------------------- seed/*.csv
def seed_repo(seed_dir):
    total = 0

    p = seed_dir / "urals_derived.csv"
    if p.exists():
        pts = {}
        for r in _read_csv(p):
            m = (r.get("month") or "").strip()
            v = _num(r, "usd")
            if len(m) == 7 and v is not None:
                pts[_month_end(m)] = v
        total += _put("urals_tax", pts, "usd_bbl", "monthly", "seed/urals_derived.csv")

    p = seed_dir / "cpi_weekly.csv"
    if p.exists():
        pts = {}
        for r in _read_csv(p):
            d = (r.get("week_end") or "")[:10]
            v = _num(r, "wow_pct")
            if d and v is not None:
                pts[d] = v
        total += _put("cpi_weekly", pts, "pct_wow", "weekly", "seed/cpi_weekly.csv")

    p = seed_dir / "cpi_derived.csv"
    if p.exists():
        pts = {}
        for r in _read_csv(p):
            m = (r.get("m") or r.get("month") or "").strip()
            v = _num(r, "mm")
            if len(m) == 7 and v is not None:
                pts[_month_end(m)] = v
        total += _put("cpi_monthly", pts, "pct_mom", "monthly", "seed/cpi_derived.csv")

    # Потоки ОРФР кладём ПЛОСКО, по ряду на категорию (orfr_flows_fiz и т.д.):
    # стор хранит числа, а не словари, и мониторы ищут именно такие имена.
    p = seed_dir / "orfr_flows.csv"
    if p.exists():
        cats = ["fiz", "nfo_du", "nfo_own", "szko", "other_banks", "nonres"]
        rows = _read_csv(p)
        for cat in cats:
            pts = {}
            for r in rows:
                m = (r.get("month") or "").strip()
                v = _num(r, cat)
                if len(m) == 7 and v is not None:
                    pts[_month_end(m)] = v
            total += _put(f"orfr_flows_{cat}", pts, "bln_rub", "monthly",
                          "seed/orfr_flows.csv")

    # Аукционы: числовой ряд = размещённый объём; детали последнего дня — в meta,
    # оттуда их читает тайл (monitors._t_ofz_auctions).
    p = seed_dir / "auctions.csv"
    if p.exists():
        placed, failed_days, last = {}, {}, None
        for r in _read_csv(p):
            d = (r.get("date") or "")[:10]
            if not d:
                continue
            v = _num(r, "placed_bln")
            failed = str(r.get("failed", "")).strip().lower() in ("true", "1", "yes")
            if v is not None:
                placed[d] = v
            if failed:
                failed_days[d] = 1.0
            if last is None or d > last.get("date", ""):
                last = {"date": d, "placed_bn": v, "demand_bn": _num(r, "demand_bln"),
                        "failed": failed, "issue": None}
        total += _put("ofz_auctions", placed, "bln_rub", "weekly", "seed/auctions.csv")
        if last:
            store.upsert_points("ofz_auctions", {}, {"last": last})
        total += _put("ofz_auction_failed", failed_days, "flag", "weekly",
                      "seed/auctions.csv", "1 = в этот день аукцион не состоялся")
    return total


def _month_end(ym):
    y, m = int(ym[:4]), int(ym[5:7])
    if m == 12:
        return f"{y}-12-31"
    import datetime as _dt
    return (_dt.date(y, m + 1, 1) - _dt.timedelta(days=1)).isoformat()


def main():
    ap = argparse.ArgumentParser(description="Затравка стора историей исследования")
    ap.add_argument("--validation", type=Path, default=None,
                    help="каталог validation/data с рыночными рядами")
    ap.add_argument("--seed", type=Path, default=ROOT / "seed")
    args = ap.parse_args()

    print(f"стор: {store.state_dir() if hasattr(store, 'state_dir') else 'STATE_DIR'}")
    total = 0
    print("— незаменимое из seed/:")
    total += seed_repo(args.seed)
    if args.validation:
        print(f"— рыночные ряды из {args.validation}:")
        total += seed_market(args.validation)
    print(f"\nвсего точек: {total}")
    print("дальше: python pipeline/run.py --mode daily --dry-run")


if __name__ == "__main__":
    main()
