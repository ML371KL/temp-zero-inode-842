"""E_algopack шаг 2c: индекс концентрации HI2 (дневной, 11 метрик) по тикерам за всю историю.
Запуск: python scripts/E_fetch_hi2.py [тикеры]"""
import sys, os, csv
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import E_lib as L
L.PAUSE = 0.6  # параллельно идёт закачка tradestats — держим общий темп щадящим

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "algopack", "hi2")
os.makedirs(OUT, exist_ok=True)
tickers = (sys.argv[1] if len(sys.argv) > 1 else
           "SBER,LKOH,GAZP,GMKN,TATN,NVTK,ROSN,PLZL,MGNT,VTBR,MOEX,SNGS").split(",")
for t in tickers:
    fn = os.path.join(OUT, f"{t}.csv")
    if os.path.exists(fn):
        continue
    url = f"{L.ALGO}/datashop/algopack/eq/hi2/{t}.json?from=2020-01-01&till=2026-09-02&iss.meta=off"
    cols, rows, pages = L.paged(url, "data", max_pages=40)
    keep = ["tradedate", "tradetime", "secid", "metric", "value"]
    ix = [cols.index(k) for k in keep] if rows else []
    with open(fn, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(keep)
        for r in rows:
            w.writerow([r[i] for i in ix])
    print(f"{t}: строк {len(rows)}, страниц {pages}, запросов {L.N_REQ}", flush=True)
print("DONE, запросов:", L.N_REQ)
