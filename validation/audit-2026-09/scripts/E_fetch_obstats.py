"""E_algopack шаг 2d: закачка 5-мин obstats (стакан) ALGOPACK по тяжеловесам индекса за 2020-01..2023-12
(2024+ уже есть в кэше algopack-validation) и свёртка до дня.
Возобновляемая: готовые тикер-месяцы пропускаются. Запуск: python scripts/E_fetch_tradestats.py [тикеры] [с yyyy-mm] [по yyyy-mm]
"""
import sys, os, csv, gzip, calendar, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import E_lib as L
L.PAUSE = 0.5

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "algopack", "obstats_raw")
os.makedirs(OUT, exist_ok=True)

tickers = (sys.argv[1] if len(sys.argv) > 1 else "SBER,LKOH,GAZP").split(",")
y0, m0 = map(int, (sys.argv[2] if len(sys.argv) > 2 else "2020-01").split("-"))
y1, m1 = map(int, (sys.argv[3] if len(sys.argv) > 3 else "2023-12").split("-"))
months = []
y, m = y0, m0
while (y, m) <= (y1, m1):
    months.append((y, m)); m += 1
    if m == 13: y, m = y + 1, 1

KEEP = ["tradedate", "tradetime", "secid", "spread_bbo", "spread_lv10", "spread_1mio", "levels_b", "levels_s",
        "vol_b", "vol_s", "val_b", "val_s", "imbalance_vol_bbo", "imbalance_val_bbo", "imbalance_vol", "imbalance_val"]

log = open(os.path.join(OUT, "_fetch_log.txt"), "a", encoding="utf-8")
for t in tickers:
    for (yy, mm) in months:
        fn = os.path.join(OUT, f"{t}_{yy}{mm:02d}.csv.gz")
        if os.path.exists(fn):
            continue
        frm = f"{yy}-{mm:02d}-01"; till = f"{yy}-{mm:02d}-{calendar.monthrange(yy, mm)[1]}"
        url = f"{L.ALGO}/datashop/algopack/eq/obstats/{t}.json?from={frm}&till={till}&iss.meta=off"
        cols, rows, pages = L.paged(url, "data")
        if not rows:
            log.write(f"{t} {frm}: пусто (страниц {pages}, запросов всего {L.N_REQ})\n"); log.flush()
            # пишем пустой файл-маркер, чтобы не перезапрашивать
            with gzip.open(fn, "wt", encoding="utf-8", newline="") as f:
                csv.writer(f).writerow(KEEP)
            continue
        ix = [cols.index(k) if k in cols else None for k in KEEP]
        with gzip.open(fn, "wt", encoding="utf-8", newline="") as f:
            w = csv.writer(f); w.writerow(KEEP)
            for r in rows:
                w.writerow([r[i] if i is not None else None for i in ix])
        log.write(f"{t} {frm}: строк {len(rows)}, страниц {pages}, запросов всего {L.N_REQ}\n"); log.flush()
        print(f"{t} {frm}: строк {len(rows)}, страниц {pages}, запросов всего {L.N_REQ}", flush=True)
print("DONE, запросов:", L.N_REQ)
