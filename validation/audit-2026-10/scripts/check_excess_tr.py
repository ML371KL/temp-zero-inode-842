"""§2.2 аудита: статистика режимов «над деньгами» считается по ЦЕНОВОМУ индексу.

states._regime_stats вычитает ставку вкладов из доходности IMOEX без дивидендов, а
позиция «акции» — это индекс полной доходности (руководство, словарь). Здесь:
(1) продовый расчёт (должен побитово совпасть с живым data.json); (2) тот же срез,
но с MCFTR; (3) средняя дивидендная составляющая MCFTR − IMOEX по эпохам.
"""

from common import BASE, DATES, TRD, Sm, calc, cash_rate, constants, live_payload, math, stt


def main():
    cols = BASE["cols"]
    st = Sm.compute_states(BASE)
    live = (live_payload().get("states") or {}).get("regime_stats") or {}
    hyst = Sm._bits_hyst(DATES, cols)
    me = calc.month_end_indices(DATES)
    labels, pxm = calc.resample_month_end(DATES, cols["imoex"])
    _, trm = calc.resample_month_end(DATES, TRD)
    _, depm = calc.resample_month_end(DATES, cols["deposit"])
    by = {r["id"]: {"px": [], "tr": []} for r in constants.REGIMES}
    for i in range(len(me) - 2):
        if labels[i] < "2004-01-01":
            continue
        j = me[i]
        reg = Sm.regime_of(hyst["trend"][j], hyst["vol"][j], hyst["bond"][j])
        if reg is None or not calc.is_num(depm[i]):
            continue
        by[reg["id"]]["px"].append(math.log(pxm[i + 1] / pxm[i]) - depm[i] / 100 / 12)
        by[reg["id"]]["tr"].append(math.log(trm[i + 1] / trm[i]) - math.log(1 + depm[i] / 100 / 12))
    for rid, r in st["regime_stats"].items():
        same = live.get(rid, {}).get("excess") == r["excess"]
        print(f"{rid}: prod excess {r['excess']}  == live data.json: {same}")
        print(f"   price-based: {Sm._summary(by[rid]['px'])}")
        print(f"   TOTAL RETURN: {Sm._summary(by[rid]['tr'])}")
    _, trm2 = calc.resample_month_end(DATES, TRD)
    eras = {}
    for k in range(len(labels) - 1):
        a, b, c, d = pxm[k], pxm[k + 1], trm2[k], trm2[k + 1]
        if not all(calc.is_num(x) and x > 0 for x in (a, b, c, d)):
            continue
        y = int(labels[k][:4])
        era = "2004–09" if y < 2010 else "2010–14" if y < 2015 else "2015–21" if y < 2022 else "2022–26"
        eras.setdefault(era, []).append(math.log(d / c) - math.log(b / a))
    for era, v in sorted(eras.items()):
        print(f"dividend component MCFTR−IMOEX {era}: {100 * stt.mean(v):.2f} п.п./мес")


if __name__ == "__main__":
    main()
