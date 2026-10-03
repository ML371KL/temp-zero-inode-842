"""§3.1 аудита: карточки «Трендследование» (mom63, знак +) и «Покупка просадки»
(dd252, знак −) включены ОДНИМ условием — медвежий тренд — на сильно связанных
величинах, поэтому их вердикты систематически противоположны. Нормировка — как
у витрины: z по 252 дням (min 120), порог вердикта ±0,5.
"""

from common import BASE, DATES, Sm, calc, stt


def main():
    cols = BASE["cols"]
    bits = Sm._bits(DATES, cols)
    zm = calc.zscore_rolling(cols["mom63"], 252, 120)
    zd = calc.zscore_rolling(cols["dd252"], 252, 120)
    pr = [(a, b) for t, (a, b) in enumerate(zip(zm, zd))
          if bits["trend"][t] == 0 and calc.is_num(a) and calc.is_num(b) and DATES[t] >= "2004"]
    opp = sum(1 for a, b in pr if (a > .5 and -b < -.5) or (a < -.5 and -b > .5))
    same = sum(1 for a, b in pr if (a > .5 and -b > .5) or (a < -.5 and -b < -.5))
    print(f"медвежьи дни с 2004: {len(pr)}; corr(z_mom63, z_dd252) = "
          f"{stt.correlation([p[0] for p in pr], [p[1] for p in pr]):.2f}")
    print(f"противоположные вердикты: {opp} ({100 * opp / len(pr):.0f}%), одинаковые: "
          f"{same} ({100 * same / len(pr):.0f}%)")


if __name__ == "__main__":
    main()
