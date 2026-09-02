"""E_algopack шаг 1б: уточняющая разведка (интервалы, метрики hi2, fo по тикеру, корни futoi)."""
import sys, os, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import E_lib as L

def q(title, url, name="data", n=2):
    obj, err = L.get(url)
    print(f"\n### {title}")
    if err:
        print("   ERR", err); return [], []
    cols, rows = L.block(obj, name)
    print(f"   rows={len(rows)}")
    for r in rows[:n]:
        print("     ", r)
    if len(rows) > n:
        print("      ...", rows[-1])
    return cols, rows

for iv in ("60", "1", "10", "D", "day", "1D", "24h"):
    q(f"tradestats interval={iv}", f"{L.ALGO}/datashop/algopack/eq/tradestats/SBER.json?from=2024-01-09&till=2024-01-10&iss.meta=off&interval={iv}", n=1)

cols, rows = q("hi2 SBER один день", f"{L.ALGO}/datashop/algopack/eq/hi2/SBER.json?from=2024-06-14&till=2024-06-14&iss.meta=off", n=0)
if rows:
    im = cols.index("metric"); iv = cols.index("value")
    for r in rows:
        print("     ", r[im], r[iv], r[cols.index('tradetime')])

q("hi2 SBER 2020-01", f"{L.ALGO}/datashop/algopack/eq/hi2/SBER.json?from=2020-01-01&till=2020-01-10&iss.meta=off", n=2)
q("fo tradestats RIU6 по тикеру", f"{L.ALGO}/datashop/algopack/fo/tradestats/RIU6.json?from=2026-08-28&till=2026-08-28&iss.meta=off", n=1)
q("fo tradestats RI по корню", f"{L.ALGO}/datashop/algopack/fo/tradestats/RI.json?from=2026-08-28&till=2026-08-28&iss.meta=off", n=1)
q("fo tradestats RIH0 2020", f"{L.ALGO}/datashop/algopack/fo/tradestats/RIH0.json?from=2020-01-10&till=2020-01-10&iss.meta=off", n=1)
q("fx tradestats USD000UTSTOM 2020", f"{L.ALGO}/datashop/algopack/fx/tradestats/USD000UTSTOM.json?from=2020-01-10&till=2020-01-10&iss.meta=off", n=1)
q("fx orderstats CNYRUB_TOM 2026", f"{L.ALGO}/datashop/algopack/fx/orderstats/CNYRUB_TOM.json?from=2026-08-28&till=2026-08-28&iss.meta=off", n=1)

# корни futoi: пройдём последнюю выдачу целиком (2 страницы)
cols, rows, pages = L.paged(f"{L.ALGO}/analyticalproducts/futoi/securities.json?iss.meta=off", "futoi", max_pages=3)
if rows:
    it = cols.index("ticker")
    cnt = collections.Counter(r[it] for r in rows)
    print("\n### futoi корни (строк в последней выдаче):", len(cnt))
    print("   ", sorted(cnt))

# страниц за месяц tradestats в 2022-06 (калибровка бюджета)
c, r, p = L.paged(f"{L.ALGO}/datashop/algopack/eq/tradestats/SBER.json?from=2022-06-01&till=2022-06-30&iss.meta=off", "data")
print("\n### tradestats SBER 2022-06: строк", len(r), "страниц", p)
c, r, p = L.paged(f"{L.ALGO}/datashop/algopack/eq/obstats/SBER.json?from=2022-06-01&till=2022-06-30&iss.meta=off", "data")
print("### obstats SBER 2022-06: строк", len(r), "страниц", p)

# alerts: типы
cols, rows = q("alerts типы 2026-08-28", f"{L.ALGO}/datashop/algopack/eq/alerts.json?date=2026-08-28&iss.meta=off", n=0)
if rows:
    ia = cols.index("alert_type")
    print("   ", collections.Counter(r[ia] for r in rows).most_common())

print("\nЗапросов:", L.N_REQ)
