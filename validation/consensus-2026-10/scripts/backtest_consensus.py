"""Backtest of fetch/consensus.py on archived t.me search results.

usage: python -B backtest_consensus.py <repo> <cache1.json> [<cache2.json> ...] [--channels a,b,c] [--all]
Uses the parser/agreement code of <repo>; network is not touched.
"""
import json, re, sys
from datetime import date, datetime, timedelta

args = sys.argv[1:]
repo = args.pop(0)
chan_override, show_all = None, False
if "--channels" in args:
    i = args.index("--channels"); chan_override = args[i + 1].split(","); del args[i:i + 2]
if "--all" in args:
    args.remove("--all"); show_all = True
sys.path.insert(0, repo)
from pipeline.fetch import consensus as c, tg

TRUTH = {"2026-02-13": 16.0, "2026-03-20": 15.0, "2026-04-24": 14.5, "2026-06-19": 14.0,
         "2026-07-24": 14.25, "2026-09-11": 14.0, "2026-10-23": 14.0}

by_channel = {}
for path in args:
    cache = json.load(open(path, encoding="utf-8"))
    for key, rec in cache.items():
        ch = key.split("|")[0]
        d = by_channel.setdefault(ch, {})
        for m in rec["msgs"]:
            d[m["id"]] = m

channels = chan_override or list(c.CHANNELS)
lookback = getattr(c, "LOOKBACK_DAYS", 14)
# Generic query semantics: message holds a survey word AND a rate word.
GENERIC = [(re.compile(r"опрос", re.I), re.compile(r"ставк", re.I)),
           (re.compile(r"консенсус", re.I), re.compile(r"ставк", re.I))]

tot_ok = tot_miss = tot_wrong = 0
false_cands = []
for meeting, truth in TRUTH.items():
    end = date.fromisoformat(meeting)
    start = end - timedelta(days=lookback)
    cands = []
    for ch in channels:
        for m in by_channel.get(ch, {}).values():
            if not m["at"]:
                continue
            day = datetime.fromisoformat(m["at"]).astimezone(tg.MSK).date()
            if not (start <= day < end):
                continue
            text = m["text"] or ""
            if not any(a.search(text) and b.search(text) for a, b in GENERIC):
                continue
            if tg.is_reprint(text):
                continue
            v = c._rate_from(text)
            if v is None:
                continue
            cands.append({"value": v, "channel": ch, "at": day.isoformat(), "url": m["url"], "text": text})
    deal = c.agreed(cands)
    per = {}
    for x in cands:
        per.setdefault(x["channel"], set()).add(x["value"])
    verdict = "нет согласия" if deal is None else ("ВЕРНО" if deal[0] == truth else "ОШИБКА")
    if deal is None:
        tot_miss += 1
    elif deal[0] == truth:
        tot_ok += 1
    else:
        tot_wrong += 1
    print(f"{meeting} факт-консенсус {truth:5}: {verdict:13} {deal}  каналы={ {k: sorted(v) for k, v in per.items()} }")
    for x in cands:
        if x["value"] != truth:
            false_cands.append((meeting, truth, x))
        if show_all:
            print(f"    [{x['at']}] {x['channel']} {x['value']} {x['url']} :: {x['text'][:160]!r}")
print(f"ИТОГ: верно {tot_ok}, без согласия {tot_miss}, ошибочно {tot_wrong} из {len(TRUTH)}")
print(f"кандидатов с числом не равным консенсусу: {len(false_cands)}")
for meeting, truth, x in false_cands:
    print(f"  {meeting} (консенсус {truth}) {x['channel']} {x['at']} -> {x['value']} {x['url']}")
    print(f"     {x['text'][:400]!r}")
