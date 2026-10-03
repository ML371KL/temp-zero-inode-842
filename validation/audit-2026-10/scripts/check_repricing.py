"""§2.4 аудита: «репрайсинг ожиданий» Δ21(y1 − ключ) механически включается снижением
ключевой ставки.

Δ21(y1 − ключ) = Δ21(y1) − Δ21(ключ): если ЦБ снизил ставку сильнее, чем сдвинулась
годовая доходность (снижение было в цене), спред растёт без всякого ястребиного
репрайсинга. Взять ставку, «действовавшую в день среза» (monitors._t_expectations,
shadow._sig_repricing), этого не лечит — шаг всё равно попадает в 21-дневную
разность. Здесь: разложение срабатываний и та же теневая позиция P3m на «очищенном»
бите Δ21(y1) > 0,25 (без шага ключа); сверка теневой позиции с живой.
"""

from common import (DATES, Cm, Dm, Sm, calc, live_payload, panel_with, perf, rle)


def es_from(series, dates, thr=0.25):
    d21 = calc.diff(series, 21)
    bit = [None if not calc.is_num(v) else int(v > thr) for v in d21]
    bym = {dates[i][:7]: bit[i] for i in calc.month_end_indices(dates)}

    def prev(k):
        y, m = int(k[:4]), int(k[5:7])
        return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"
    return [int(bym.get(prev(d[:7])) or 0) for d in dates], bit, d21


def main():
    pan = panel_with("prod")
    cols = pan["cols"]
    y1, key = cols["y1"], cols["key_rate"]
    spread = [a - b if calc.is_num(a) and calc.is_num(b) else None for a, b in zip(y1, key)]
    es, bit, d21 = es_from(spread, DATES)
    dy1, dkey = calc.diff(y1, 21), calc.diff(key, 21)
    idx = [i for i in range(len(DATES)) if bit[i] is not None and DATES[i] >= "2015-01-01"]
    on = [i for i in idx if bit[i] == 1]
    cut = [i for i in on if calc.is_num(dkey[i]) and dkey[i] < 0]
    mech = [i for i in cut if calc.is_num(dy1[i]) and dy1[i] <= 0]
    print(f"2015+: бит включён {len(on)}/{len(idx)} дней; из них со снижением ключа в окне "
          f"{len(cut)} ({100 * len(cut) / len(on):.0f}%), при НЕ выросшей годовой доходности "
          f"{len(mech)} ({100 * len(mech) / len(on):.0f}%)")
    print("срабатывания на конце месяца при снижении ключа и падающей y1 (дата, Δспред, Δy1, Δключ):")
    for i in calc.month_end_indices(DATES):
        if DATES[i] >= "2015" and bit[i] == 1 and calc.is_num(dkey[i]) and dkey[i] < 0 and dy1[i] <= 0:
            print(f"   {DATES[i]}  {d21[i]:+.2f}  {dy1[i]:+.2f}  {dkey[i]:+.2f}")

    mf = Cm.monthly_frame(pan)
    raw = Sm._bits(DATES, cols)
    hyst = Sm._bits_hyst(DATES, cols, raw)
    gate = Sm.gate_open_series(raw, hyst)
    comp = Dm.daily_composite(pan, mf)
    dec = Dm.decision_days(DATES)
    cs = Dm.comp_state_series(comp, dec, 0.2)
    es_y1, _, _ = es_from(y1, DATES)
    p2 = Dm.run_automaton(DATES, gate, cs, dec)[0]
    p3m = Dm.run_automaton(DATES, gate, cs, dec, es)[0]
    p3y = Dm.run_automaton(DATES, gate, cs, dec, es_y1)[0]
    live = (live_payload().get("shadow") or {}).get("shadow_position", {}).get("history")
    print("теневая позиция совпадает с живой:", live == rle(DATES, p3m))
    for lo, hi in (("2010-01-01", "2026-08-31"), ("2015-01-01", "2026-08-31")):
        print(f"== {lo}..{hi} (t+1)")
        print("  P2                 ", perf(p2, lo, hi))
        print("  P2 + бит (как в тени)", perf(p3m, lo, hi))
        print("  P2 + бит Δy1 > 0,25  ", perf(p3y, lo, hi))


if __name__ == "__main__":
    main()
