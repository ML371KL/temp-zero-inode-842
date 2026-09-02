"""E_algopack шаг 1: разведка каталога ALGOPACK. Ключ не печатается."""
import sys, os, json, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import E_lib as L

def show(title, url, name=None, auth=True, n=3):
    obj, err = L.get(url, auth=auth)
    print(f"\n### {title}\n    {url.replace(L.ALGO,'ALGO').replace(L.ISS,'ISS')}")
    if err:
        print("    ERR", err); return None
    if name is None:
        # печатаем имена блоков и размеры
        for k, v in obj.items():
            if isinstance(v, dict) and "columns" in v:
                print(f"    блок {k}: cols={v['columns'][:12]} rows={len(v.get('data') or [])}")
                for r in (v.get("data") or [])[:n]:
                    print("      ", r[:12])
        return obj
    cols, rows = L.block(obj, name)
    print(f"    cols={cols}\n    rows={len(rows)}")
    for r in rows[:n]:
        print("      ", r)
    if rows:
        print("       ...", rows[-1])
    return obj

# 0. TLS/доступ
show("корень datashop", f"{L.ALGO}/datashop/algopack.json?iss.meta=off")
show("eq", f"{L.ALGO}/datashop/algopack/eq.json?iss.meta=off")
show("fo", f"{L.ALGO}/datashop/algopack/fo.json?iss.meta=off")
show("fx", f"{L.ALGO}/datashop/algopack/fx.json?iss.meta=off")

# 1. глубина tradestats eq
for y in (2019, 2020, 2021):
    show(f"tradestats SBER {y}-01", f"{L.ALGO}/datashop/algopack/eq/tradestats/SBER.json?from={y}-01-01&till={y}-01-31&iss.meta=off", "data", n=2)
# 2. попытки дневной агрегации / большой страницы
show("tradestats SBER limit=5000", f"{L.ALGO}/datashop/algopack/eq/tradestats/SBER.json?from=2024-01-01&till=2024-01-31&iss.meta=off&limit=5000", "data", n=1)
show("tradestats SBER interval=24", f"{L.ALGO}/datashop/algopack/eq/tradestats/SBER.json?from=2024-01-01&till=2024-01-31&iss.meta=off&interval=24", "data", n=2)
show("tradestats SBER iss.interval=24", f"{L.ALGO}/datashop/algopack/eq/tradestats/SBER.json?from=2024-01-01&till=2024-01-31&iss.meta=off&iss.interval=24", "data", n=2)
show("tradestats SBER period=D", f"{L.ALGO}/datashop/algopack/eq/tradestats/SBER.json?from=2024-01-01&till=2024-01-31&iss.meta=off&period=D", "data", n=2)
show("tradestats SBER latest", f"{L.ALGO}/datashop/algopack/eq/tradestats/SBER.json?from=2024-01-01&till=2024-01-31&iss.meta=off&latest=1", "data", n=2)
# 3. все тикеры на дату
show("tradestats all date 2020-01-10", f"{L.ALGO}/datashop/algopack/eq/tradestats.json?date=2020-01-10&iss.meta=off", "data", n=2)
# 4. orderstats / obstats глубина
show("orderstats SBER 2020-01", f"{L.ALGO}/datashop/algopack/eq/orderstats/SBER.json?from=2020-01-01&till=2020-01-10&iss.meta=off", "data", n=2)
show("obstats SBER 2020-01", f"{L.ALGO}/datashop/algopack/eq/obstats/SBER.json?from=2020-01-01&till=2020-01-10&iss.meta=off", "data", n=2)
# 5. fo / fx
show("fo tradestats date", f"{L.ALGO}/datashop/algopack/fo/tradestats.json?date=2026-08-28&iss.meta=off", "data", n=3)
show("fx tradestats date", f"{L.ALGO}/datashop/algopack/fx/tradestats.json?date=2026-08-28&iss.meta=off", "data", n=3)
show("fx tradestats CNYRUB_TOM 2023-01", f"{L.ALGO}/datashop/algopack/fx/tradestats/CNYRUB_TOM.json?from=2023-01-09&till=2023-01-12&iss.meta=off", "data", n=2)
# 6. hi2
show("hi2 корень", f"{L.ALGO}/datashop/algopack/eq/hi2.json?iss.meta=off")
show("hi2 SBER", f"{L.ALGO}/datashop/algopack/eq/hi2/SBER.json?from=2024-01-01&till=2024-01-31&iss.meta=off")
show("hi2 date", f"{L.ALGO}/datashop/algopack/eq/hi2.json?date=2026-08-28&iss.meta=off")
# 7. alerts / другие продукты
show("alerts", f"{L.ALGO}/datashop/algopack/eq/alerts.json?date=2026-08-28&iss.meta=off")
show("futoi securities", f"{L.ALGO}/analyticalproducts/futoi/securities.json?iss.meta=off")
show("futoi MX 2020-01-03", f"{L.ALGO}/analyticalproducts/futoi/securities/MX.json?from=2020-01-03&till=2020-01-03&iss.meta=off", "futoi", n=2)
show("futoi MX 2019-12", f"{L.ALGO}/analyticalproducts/futoi/securities/MX.json?from=2019-12-01&till=2019-12-31&iss.meta=off", "futoi", n=2)
# 8. индексные аналитики (бесплатный ISS): состав/веса
show("ISS analytics IMOEX", f"{L.ISS}/statistics/engines/stock/markets/index/analytics/IMOEX.json?date=2020-06-15&limit=100&iss.meta=off", "analytics", auth=False, n=2)
# 9. bond-market продукт?
show("bond tradestats", f"{L.ALGO}/datashop/algopack/bond.json?iss.meta=off")
show("fo obstats RI date", f"{L.ALGO}/datashop/algopack/fo/obstats.json?date=2026-08-28&iss.meta=off", "data", n=2)

print("\nЗапросов:", L.N_REQ)
for l in L.LOG[:60]:
    print("  ", l)
