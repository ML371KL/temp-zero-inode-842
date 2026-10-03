"""§1, §3.3–3.4, §2.5 аудита: сверка обвязки с продом и экономика правила.

(1) позиции P2 на прод-варианте курса обязаны побитово совпасть с
    verdict.position.history живого data.json;
(2) результат по окнам: удержание MCFTR, P2 (прод и чистый курс), P0, исполнение
    t / t+1, денежная нога вклады / RUONIA;
(3) грубая оценка налога: 13 % НДФЛ на реализованный доход при каждой смене позиции
    (убытки переносятся вперёд) против удержания с ЛДВ;
(4) здоровье: доля месяцев, где IC-24 неотличим от нуля, серии ниже нуля, и
    операционная мера — последние 12/24 месяца правила против денег.
"""

from common import (DATES, TRD, calc, cash_rate, datetime, live_payload, panel_with, perf,
                    positions, rle)
from pipeline.compute import core as Cm, health as Hm


def tax_sim(pos, lo, hi, lag=1, cost=0.002, tax=0.13):
    v, basis, carry, prev, n = 1.0, 1.0, 0.0, None, 0
    for i in range(2 + lag, len(DATES)):
        d = DATES[i]
        if not (lo <= d <= hi):
            continue
        r = TRD[i] / TRD[i - 1] - 1
        days = (datetime.date.fromisoformat(d) - datetime.date.fromisoformat(DATES[i - 1])).days
        c = (1 + cash_rate(DATES[i - 1]) / 100) ** (days / 365) - 1
        p = pos[i - 1 - lag]
        if prev is not None and p != prev:
            g = v - basis
            if g > 0:
                use = min(carry, g)
                carry -= use
                v -= tax * (g - use)
            else:
                carry -= g
            v *= 1 - cost
            basis = v
        prev = p
        v *= 1 + (r if p == 1 else c)
        n += 1
    g = v - basis
    if g > 0:
        v -= tax * max(0.0, g - carry)
    return round(100 * (v ** (252 / n) - 1), 1)


def main():
    live = live_payload()["verdict"]["position"]["history"]
    prod, clean = panel_with("prod"), panel_with("clean")
    p2, p2c, p0 = positions(prod, "P2"), positions(clean, "P2"), positions(prod, "P0")
    mine = rle(DATES, p2)
    print(f"отрезков позиции: живых {len(live)}, обвязки {len(mine)}; побитово: {live == mine}")
    import common
    common.PROD_OFFICIAL_FROM = "2003-01-01"
    alt = rle(DATES, positions(panel_with("prod"), "P2"))
    common.PROD_OFFICIAL_FROM = "2013-01-01"
    print(f"вариант «официальный курс с 2003» совпадает с живым: {alt == live} "
          "(False = в продовом сторе курса ЦБ до 2013 нет)")
    for lo, hi in (("2010-01-01", "2026-08-31"), ("2004-01-06", "2026-08-31"),
                   ("2025-01-01", "2026-10-02")):
        print(f"== {lo}..{hi}")
        print("  b&h MCFTR        ", perf(None, lo, hi, bench=True))
        print("  P2 (прод, t+1)   ", perf(p2, lo, hi))
        print("  P2 (чистый курс) ", perf(p2c, lo, hi))
        print("  P2 (исполнение t)", perf(p2, lo, hi, lag=0))
        print("  P2 (кэш RUONIA)  ", perf(p2, lo, hi, cash="ruonia"))
        print("  P0 (прод, t+1)   ", perf(p0, lo, hi))
    for lo, hi in (("2010-01-01", "2026-08-31"), ("2015-01-01", "2026-08-31")):
        print(f"налог {lo}..{hi}: P2 до налога {tax_sim(p2, lo, hi, tax=0)}% → после 13 % НДФЛ "
              f"{tax_sim(p2, lo, hi)}%; удержание MCFTR (ЛДВ) {perf(None, lo, hi, bench=True)['cagr']}%")
    h = Hm.compute_health(prod, mf=Cm.monthly_frame(prod))
    ser = h["series"]
    inside = sum(1 for _, v in ser if -0.4 <= v <= 0.4) / len(ser)
    print(f"здоровье сейчас: IC {h['ic_24m']} ДИ {h['ic_ci95']} статус {h['status']} "
          f"«{h['status_text']}»; IC-24 внутри ±0,4 в {100 * inside:.0f}% месяцев истории")
    for lo in ("2025-10-01", "2024-10-01"):
        print(f"P2 с {lo}: {perf(p2, lo, DATES[-1])}; b&h {perf(None, lo, DATES[-1], bench=True)['cagr']}%")


if __name__ == "__main__":
    main()
