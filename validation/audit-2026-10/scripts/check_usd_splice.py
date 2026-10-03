"""§2.1 аудита: курс USD в ноге ядра usd_mom63.

panel.build_panel берёт официальный курс только в дни с ТОЧНОЙ записью ЦБ, а в
остальные торговые дни подставляет IMOEX/RTSI×31,4949. У ЦБ нет записей с датой
понедельника (курс пятницы действует сб–пн), поэтому каждый понедельник нога
считается по другому ряду; до 2013 года (начало официального ряда в продовом сторе)
— по нему целиком. Здесь: (1) отклонение склейки от официального курса, действующего
в тот же день; (2) знак и IC ноги; (3) позиции и результат правила в одноногую эру.
"""

import datetime

from common import (BASE, DATES, calc, json, math, official_usd, panel_with, perf,
                    positions, stt, usd_variant)


def deviations():
    px = dict(zip(DATES, BASE["cols"]["imoex"]))
    from common import _RTSI, _CBR, USD_SPLICE_K
    rows = []
    for d in DATES:
        if _RTSI.get(d, 0) > 0 and official_usd(d):
            rows.append((d, datetime.date.fromisoformat(d).weekday(),
                         math.log(px[d] / _RTSI[d] * USD_SPLICE_K / official_usd(d)), d in _CBR))
    since = [d for d in DATES if d >= "2004"]
    gaps = [d for d in since if d not in _CBR]
    mondays = [d for d in since if datetime.date.fromisoformat(d).weekday() == 0]
    mon_rec = [k for k in _CBR if k >= "2004" and datetime.date.fromisoformat(k).weekday() == 0]
    print(f"с 2004: торговых дней без точной записи ЦБ {len(gaps)}/{len(since)} "
          f"({100 * len(gaps) / len(since):.0f}%); торговых понедельников {len(mondays)}, "
          f"записей ЦБ с датой понедельника {len(mon_rec)}")
    for lo, hi, lab in (("2003", "2008", "2003–2007"), ("2008", "2010", "2008–2009"),
                        ("2010", "2013", "2010–2012"), ("2013", "2027", "2013–2026")):
        sel = [abs(r[2]) for r in rows if lo <= r[0] < hi]
        mon = [abs(r[2]) for r in rows if lo <= r[0] < hi and r[1] == 0]
        print(f"{lab}: |log(proxy/official)| median {100 * stt.median(sel):.2f}%  "
              f"p90 {100 * sorted(sel)[int(.9 * len(sel))]:.2f}%  max {100 * max(sel):.1f}%"
              f"   | Mondays median {100 * stt.median(mon):.2f}%")


def monthly_leg(kind):
    u = usd_variant(kind)
    m63 = calc.log_return(u, 63)
    labels, vals = calc.resample_month_end(DATES, m63)
    return labels, vals, calc.zscore_rolling(vals, 60, 24, clip=3.0)


def leg_compare():
    la, va, za = monthly_leg("prod")
    _, vc, zc = monthly_leg("clean")
    _, px = calc.resample_month_end(DATES, BASE["cols"]["imoex"])
    fwd = [math.log(px[i + 1] / px[i]) if i + 2 < len(px) else None for i in range(len(px))]
    for lo, hi in (("2004", "2008"), ("2008", "2010"), ("2010", "2013"), ("2013", "2027")):
        pr = [(a, c) for l, a, c in zip(la, za, zc) if lo <= l < hi and calc.is_num(a) and calc.is_num(c)]
        dis = sum(1 for a, c in pr if (a > 0) != (c > 0))
        print(f"{lo}–{int(hi) - 1}: sign of z(usd_mom63) differs prod vs clean in {dis}/{len(pr)} months "
              f"({100 * dis / len(pr):.0f}%)")
    for lo, hi in (("2004", "2027"), ("2004", "2010"), ("2010", "2027")):
        out = []
        for name, vals in (("prod", va), ("clean", vc)):
            xs = [v for l, v, f in zip(la, vals, fwd) if lo <= l < hi and calc.is_num(v) and calc.is_num(f)]
            ys = [f for l, v, f in zip(la, vals, fwd) if lo <= l < hi and calc.is_num(v) and calc.is_num(f)]
            ic, n = calc.spearman_ic(xs, ys)
            out.append(f"{name} IC {ic:+.3f} (n={n})")
        print(f"usd_mom63 monthly IC {lo}–{int(hi) - 1}: " + " | ".join(out))


def early_backtest():
    res = {k: (positions(panel_with(k), "P2"), positions(panel_with(k), "P0")) for k in ("prod", "clean")}
    for lo, hi in (("2004-01-06", "2009-12-31"), ("2004-01-06", "2016-11-30"), ("2010-01-01", "2016-11-30")):
        print(f"== {lo}..{hi} (одноногая эра; t+1; 0,2% за смену)")
        print("  b&h MCFTR       ", perf(None, lo, hi, bench=True))
        for i, rule in enumerate(("P2", "P0")):
            for k in ("prod", "clean"):
                print(f"  {rule} usd={k:5s}    ", perf(res[k][i], lo, hi))
    a, c = res["prod"][0], res["clean"][0]
    idx = [i for i, d in enumerate(DATES) if "2004-01-06" <= d <= "2008-12-31"]
    print("P2: days with different position 2004–2008:",
          f"{sum(1 for i in idx if a[i] != c[i])}/{len(idx)}")


if __name__ == "__main__":
    deviations()
    leg_compare()
    early_backtest()
