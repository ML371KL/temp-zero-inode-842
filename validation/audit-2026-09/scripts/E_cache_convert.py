"""E_algopack шаг 2a: перенос уже скачанных ALGOPACK-кэшей (algopack-validation, 01-02.09.2026)
в CSV каталога data/algopack/: futoi по корням (дневные последний/первый снимки) и micro
(5-мин tradestats+obstats, свёрнутые по сессиям). Без сетевых запросов."""
import sys, os, json, csv
sys.stdout.reconfigure(encoding="utf-8")
SRC = r"C:\Users\rodio\Desktop\Claude\algopack-validation\cache"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "algopack")
os.makedirs(OUT, exist_ok=True)

# --- futoi
rows = []
roots = sorted(f[6:-5] for f in os.listdir(SRC) if f.startswith("futoi_") and f.endswith(".json"))
for root in roots:
    d = json.load(open(os.path.join(SRC, f"futoi_{root}.json"), encoding="utf-8"))
    for day in sorted(d):
        rec = d[day]
        for grp in ("FIZ", "YUR"):
            g = rec.get(grp) or {}
            gf = rec.get(grp + "_first") or {}
            rows.append([root, day, grp, g.get("pos"), g.get("long"), g.get("short"), g.get("nl"), g.get("ns"),
                         g.get("time"), gf.get("pos"), gf.get("nl"), gf.get("ns")])
with open(os.path.join(OUT, "futoi_daily.csv"), "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["root", "date", "group", "pos", "long", "short", "n_long", "n_short", "last_time",
                "pos_first", "n_long_first", "n_short_first"])
    w.writerows(rows)
print("futoi: корни", roots, "строк", len(rows))
for root in roots:
    ds = sorted(json.load(open(os.path.join(SRC, f"futoi_{root}.json"), encoding="utf-8")))
    print(f"  {root}: {len(ds)} дней {ds[0]}..{ds[-1]}")

# --- micro
mdir = os.path.join(SRC, "micro")
fields = ["o", "h", "l", "c", "vwap", "val", "vol", "trades", "val_b", "val_s", "tr_b", "tr_s", "bars",
          "first_t", "last_t", "sp_bbo", "sp_1mio", "imb", "lv_b", "lv_s"]
rows = []
for fn in sorted(os.listdir(mdir)):
    t = fn[:-5]
    d = json.load(open(os.path.join(mdir, fn), encoding="utf-8"))
    for day in sorted(d):
        for s, a in d[day].items():
            rows.append([t, day, s] + [a.get(k) for k in fields])
with open(os.path.join(OUT, "micro_sessions.csv"), "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["ticker", "date", "session"] + fields)
    w.writerows(rows)
print("micro: строк", len(rows), "тикеров", len(os.listdir(mdir)))

# --- состав индекса (веса) еженедельно
comp = json.load(open(os.path.join(SRC, "index_composition.json"), encoding="utf-8"))
with open(os.path.join(OUT, "index_weights_weekly.csv"), "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f); w.writerow(["date", "ticker", "weight"])
    for day in sorted(comp):
        for t, wt in comp[day].items():
            w.writerow([day, t, wt])
print("веса: срезов", len(comp), sorted(comp)[0], sorted(comp)[-1])
