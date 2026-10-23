"""Archive of t.me search results (two-word + month queries) for consensus backtest.

usage: python -B cache_tg.py <repo> <out.json>
"""
import json, os, sys, time

repo, out = sys.argv[1], sys.argv[2]
sys.path.insert(0, repo)
from pipeline.fetch import tg

CHANNELS = ["cbrstocks", "rbc_news", "if_market_news", "interfaxonline", "prime1",
            "markettwits", "banksta", "tass_agency", "rian_ru", "izvestia", "vedomosti"]
MONTHS = ["февраля", "марта", "апреля", "июня", "июля", "сентября", "октября"]
BASE = ["опрос ставка", "консенсус ставка"]
QUERIES = BASE + [f"{b} {m}" for b in BASE for m in MONTHS]

cache = {}
if os.path.exists(out):
    cache = json.load(open(out, encoding="utf-8"))

for ch in CHANNELS:
    for q in QUERIES:
        key = f"{ch}|{q}"
        if key in cache:
            continue
        msgs, seen, before, err = [], set(), None, None
        for _ in range(3):
            url = tg.PREVIEW % ch + "?q=" + tg.quote(q) + (f"&before={before}" if before else "")
            try:
                batch = tg.parse_page(tg.get_text(url, headers=tg._UA, timeout=30), ch)
            except Exception as e:
                err = str(e)
                break
            fresh = [m for m in batch if m["id"] is not None and m["id"] not in seen]
            if not fresh:
                break
            seen.update(m["id"] for m in fresh)
            msgs += fresh
            before = min(m["id"] for m in fresh)
            time.sleep(0.8)
        cache[key] = {"err": err, "msgs": [
            {"id": m["id"], "url": m.get("url"), "text": m.get("text"),
             "at": m["at"].isoformat() if m.get("at") else None} for m in msgs]}
        ats = [m["at"] for m in cache[key]["msgs"] if m["at"]]
        print(key, len(msgs), (min(ats)[:10] if ats else None), (max(ats)[:10] if ats else None), err, flush=True)
        json.dump(cache, open(out, "w", encoding="utf-8"), ensure_ascii=False)
