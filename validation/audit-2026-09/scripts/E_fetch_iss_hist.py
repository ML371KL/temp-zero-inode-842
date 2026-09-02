"""E_algopack шаг 2e: БЕСПЛАТНАЯ дневная история ISS (NUMTRADES, VALUE, VOLUME, CLOSE) по тяжеловесам
с 2011 — для проверки, нужен ли ALGOPACK сигналу «средний размер сделки / число сделок».
Запуск: python scripts/E_fetch_iss_hist.py [тикеры] [с yyyy-mm-dd]"""
import sys, os, csv
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import E_lib as L
L.PAUSE = 0.3
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "algopack", "iss_history_daily.csv")
tickers = (sys.argv[1] if len(sys.argv) > 1 else "SBER,LKOH,GAZP,GMKN,ROSN").split(",")
frm = sys.argv[2] if len(sys.argv) > 2 else "2011-01-01"
KEEP = ["TRADEDATE", "SECID", "NUMTRADES", "VALUE", "VOLUME", "CLOSE", "WAPRICE"]
rows_all = []
for t in tickers:
    start = 0
    n_t = 0
    while True:
        url = (f"{L.ISS}/history/engines/stock/markets/shares/boards/TQBR/securities/{t}.json"
               f"?from={frm}&till=2026-09-02&iss.meta=off&start={start}")
        obj, err = L.get(url, auth=False)
        if err or not obj:
            print(t, "ERR", err, flush=True); break
        cols, rows = L.block(obj, "history")
        if not rows:
            break
        ix = [cols.index(k) if k in cols else None for k in KEEP]
        for r in rows:
            rows_all.append([r[i] if i is not None else None for i in ix])
        n_t += len(rows); start += len(rows)
        if len(rows) < 100:
            break
    print(f"{t}: строк {n_t}, запросов {L.N_REQ}", flush=True)
with open(OUT, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f); w.writerow([k.lower() for k in KEEP]); w.writerows(rows_all)
print("DONE, запросов:", L.N_REQ)
