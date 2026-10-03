"""Общая обвязка проверок аудита 03.10.2026 — на функциях САМОЙ панели.

Ничего не пересказывает: панель, ядро, флаги, ворота, дневной композит и автомат
позиции берутся из pipeline/compute/*. Отличие от прода — только там, где проверка
это объявляет явно (вариант курса USD, денежная нога, мера доходности).

Окружение:
  STATE_DIR   — стор, собранный `ops/seed_store.py` + `run.py --mode bootstrap --dry-run`
                (по умолчанию .state);
  AUDIT_WORK  — каталог справочных файлов из fetch_refs.py (по умолчанию
                .state/audit-2026-10; в git не попадает).

Сверка обвязки с продом: positions('P2') на прод-варианте курса обязана побитово
повторить verdict.position.history из живого data.json (check_backtest.py).
"""

import bisect
import datetime
import json
import math
import os
import statistics as stt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("STATE_DIR", str(ROOT / ".state"))
WORK = Path(os.environ.get("AUDIT_WORK") or (ROOT / ".state" / "audit-2026-10"))

from pipeline.compute import core as Cm, decision as Dm, panel as P, states as Sm  # noqa: E402
from pipeline.lib import calc, constants, store  # noqa: E402

__all__ = ["ROOT", "WORK", "calc", "constants", "store", "Cm", "Dm", "P", "Sm",
           "BASE", "DATES", "TRD", "official_usd", "usd_variant", "panel_with",
           "positions", "perf", "rle", "cash_rate", "live_payload", "stt", "math",
           "json", "datetime"]


def _pts(sid):
    d = store.load_series(sid) or {}
    return {k: v for k, v in (d.get("points") or {}).items() if isinstance(v, (int, float))}


def _work_json(name):
    path = WORK / name
    if not path.exists():
        raise SystemExit(f"нет {path}: сначала python {Path(__file__).parent / 'fetch_refs.py'}")
    return json.loads(path.read_text(encoding="utf-8"))


BASE = P.build_panel(store)
DATES = BASE["dates"]
_PX = dict(zip(DATES, BASE["cols"]["imoex"]))
_RTSI = _pts("rtsi")
_CBR = _work_json("usd_cbr_full.json")     # официальный курс ЦБ с 1997, дата = дата применения
_RECS = sorted(_CBR)
USD_SPLICE_K = P.USD_SPLICE_K
# Продовый стор затравлен официальным курсом исследования, который начинался в 2013
# году (validation/audit-2026-09/scripts/long_panel.py: «official (2013+)»). После правки
# 03.10.2026 прод сам дотягивает курс с 1997 года, и вариант 'prod' ниже — это прод ДО
# правки, а 'clean' — после.
PROD_OFFICIAL_FROM = "2013-01-01"


def official_usd(day):
    """Официальный курс, ДЕЙСТВУЮЩИЙ в день day: последняя запись ЦБ с датой ≤ day.

    У ЦБ нет записей с датой понедельника: курс, установленный в пятницу, действует
    в субботу, воскресенье и понедельник, и в XML_dynamic он лежит одной субботней
    строкой.
    """
    i = bisect.bisect_right(_RECS, day) - 1
    return _CBR[_RECS[i]] if i >= 0 else None


def usd_variant(kind):
    """'prod' — как panel.build_panel на продовом сторе: официальный курс только в дни
    с ТОЧНОЙ записью ЦБ (с 2013), в остальные дни IMOEX/RTSI×K; 'clean' — официальный
    курс, действующий в этот день, без склейки вовсе."""
    out = []
    for d in DATES:
        if kind == "clean":
            out.append(official_usd(d))
            continue
        v = _CBR.get(d) if d >= PROD_OFFICIAL_FROM else None
        if v is None and _RTSI.get(d, 0) > 0:
            v = _PX[d] / _RTSI[d] * USD_SPLICE_K
        out.append(v)
    return calc.ffill(out)


def panel_with(usd_kind="prod"):
    cols = dict(BASE["cols"])
    u = usd_variant(usd_kind)
    cols["usd"] = u
    cols["usd_mom63"] = calc.log_return(u, 63)
    return {"dates": DATES, "cols": cols}


# Денежная нога как в бэктестах аудита 02.09.2026: вклады топ-10 с 2009-07, раньше —
# ставка рефинансирования минус 3 п.п. (грубо, окно до 2009 не главное).
_REFI = [("2003-06-21", 16), ("2004-01-15", 14), ("2004-06-15", 13), ("2005-12-26", 12),
         ("2006-06-26", 11.5), ("2006-10-23", 11), ("2007-01-29", 10.5), ("2007-06-19", 10),
         ("2008-02-04", 10.25), ("2008-04-29", 10.5), ("2008-06-10", 10.75),
         ("2008-07-14", 11), ("2008-11-12", 12), ("2008-12-01", 13), ("2009-04-24", 12.5),
         ("2009-05-14", 12), ("2009-06-05", 11.5), ("2009-07-13", 11)]
_DEP = _pts("deposit_decade")
_DK = sorted(_DEP)
_RU = _work_json("ruonia.json")
_RK = sorted(_RU)


def cash_rate(day, kind="dep"):
    if kind == "ruonia" and day >= _RK[0]:
        return _RU[_RK[bisect.bisect_right(_RK, day) - 1]]
    if _DK and day >= _DK[0]:
        return _DEP[_DK[bisect.bisect_right(_DK, day) - 1]]
    r = None
    for a, v in _REFI:
        if a <= day:
            r = v
    return (r - 3) if r else 8.0


TRD = calc.ffill([_pts("mcftr").get(d) for d in DATES])


def positions(pan, rule="P2", es=None):
    """Позиция по закрытию дня: 'P2' — действующее правило (decision.py), 'P0' — прежнее."""
    dates, cols = pan["dates"], pan["cols"]
    mf = Cm.monthly_frame(pan)
    raw = Sm._bits(dates, cols)
    hyst = Sm._bits_hyst(dates, cols, raw)
    if rule == "P2":
        gate = Sm.gate_open_series(raw, hyst)
        comp = Dm.daily_composite(pan, mf)
        dec = Dm.decision_days(dates)
        cs = Dm.comp_state_series(comp, dec, constants.DECISION["comp_threshold"])
        return Dm.run_automaton(dates, gate, cs, dec, es)[0]
    gate = [None not in k and k != (0, 1, 1)
            for k in zip(raw["trend"], raw["vol"], raw["bond"])]
    always = [True] * len(dates)
    cs = Dm.comp_state_series(Dm._closed_month_composite(dates, mf), always,
                              constants.CORE_FLIP_HYSTERESIS)
    return Dm.run_automaton(dates, gate, cs, always)[0]


def perf(pos, lo, hi, lag=1, cost=0.002, cash="dep", bench=False):
    """Лонг = MCFTR, флэт = денежная нога; lag=1 — исполнение на следующем закрытии
    (как DECISION['execution']), lag=0 — в день сигнала. Шарп «сырой» (как считает
    панель) и над деньгами."""
    eq, peak, mdd = 1.0, 1.0, 0.0
    rets, exr, inm, sw, n, prev = [], [], 0, 0, 0, None
    for i in range(2 + lag, len(DATES)):
        d = DATES[i]
        if not (lo <= d <= hi) or not (calc.is_num(TRD[i]) and calc.is_num(TRD[i - 1])):
            continue
        r = TRD[i] / TRD[i - 1] - 1
        days = (datetime.date.fromisoformat(d) - datetime.date.fromisoformat(DATES[i - 1])).days
        c = (1 + cash_rate(DATES[i - 1], cash) / 100) ** (days / 365) - 1
        p = 1 if bench else pos[i - 1 - lag]
        rr = r if p == 1 else c
        if prev is not None and p != prev:
            rr -= cost
            sw += 1
        prev = p
        rets.append(rr)
        exr.append(rr - c)
        inm += p
        n += 1
        eq *= 1 + rr
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    yrs = n / 252
    vol = stt.pstdev(rets) * math.sqrt(252)
    exv = stt.pstdev(exr) * math.sqrt(252)
    return {"cagr": round(100 * (eq ** (1 / yrs) - 1), 1),
            "sharpe": round(stt.mean(rets) * 252 / vol, 2) if vol else None,
            "ex_sharpe": round(stt.mean(exr) * 252 / exv, 2) if exv else None,
            "mdd": round(100 * mdd, 1), "in_mkt": round(100 * inm / max(n, 1)),
            "sw_yr": round(sw / max(yrs, 1e-9), 1)}


def rle(dates, pos, start=Dm.START):
    out, prev = [], None
    for d, p in zip(dates, pos):
        if d < start:
            continue
        if p != prev:
            out.append([d, p])
            prev = p
    return out


def live_payload():
    return _work_json("data.json")
