"""Экспорт рабочего датасета аудита из КОПИИ боевого стора тем же кодом, что и панель.
Выход: data/panel_prod_daily.csv, data/panel_prod_monthly.csv, data/raw_long.csv, data/invariant_check.txt
"""
import os, sys, json, csv, math
S = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = r"C:\Users\rodio\Desktop\Claude\temp-zero-inode-842"
os.environ["STATE_DIR"] = os.path.join(os.path.dirname(S), "store")
sys.path.insert(0, REPO)
sys.stdout.reconfigure(encoding="utf-8")
from pipeline.lib import store, calc, constants
from pipeline.compute import panel as panel_mod, core as core_mod, states as states_mod

print("raw dir:", store.raw_dir(), "series:", len(store.list_series()))
P = panel_mod.build_panel(store)
dates, cols = P["dates"], P["cols"]
bits = states_mod._bits(dates, cols)
print("panel days:", len(dates), dates[0], "..", dates[-1], "cols:", len(cols))

# --- daily
names = sorted(cols)
with open(os.path.join(S, "data", "panel_prod_daily.csv"), "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["date"] + names + ["st_trend", "st_vol", "st_bond", "st_rate", "cell"])
    for i, d in enumerate(dates):
        row = [d] + [("" if cols[n][i] is None else repr(cols[n][i])) for n in names]
        t, v, b, r = bits["trend"][i], bits["vol"][i], bits["bond"][i], bits["rate_phase"][i]
        cell = states_mod.cell_code(t, v, b) if None not in (t, v, b) else ""
        row += ["" if t is None else t, "" if v is None else v, "" if b is None else b,
                "" if r is None else r, cell]
        w.writerow(row)

# --- monthly frame (как ядро)
mf = core_mod.monthly_frame(P)
cids = [c["id"] for c in constants.CORE_COMPONENTS]
# бит состояния на последний торговый день месяца
midx = {d: i for i, d in enumerate(dates)}
with open(os.path.join(S, "data", "panel_prod_monthly.csv"), "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["date", "imoex", "fwd1m_log", "composite", "n_used"] +
               [f"raw_{c}" for c in cids] + [f"z_{c}" for c in cids] +
               ["st_trend", "st_vol", "st_bond", "st_rate", "cell"])
    for i, d in enumerate(mf["dates"]):
        j = midx[d]
        t, v, b, r = bits["trend"][j], bits["vol"][j], bits["bond"][j], bits["rate_phase"][j]
        cell = states_mod.cell_code(t, v, b) if None not in (t, v, b) else ""
        fmt = lambda x: "" if x is None else repr(x)
        w.writerow([d, fmt(mf["imoex"][i]), fmt(mf["fwd1m"][i]), fmt(mf["composite"][i]), mf["n_used"][i]] +
                   [fmt(mf["raw"][c][i]) for c in cids] + [fmt(mf["z"][c][i]) for c in cids] +
                   ["" if t is None else t, "" if v is None else v, "" if b is None else b,
                    "" if r is None else r, cell])
last = calc.last_valid(mf["composite"])
print("composite last:", mf["dates"][last[0]], round(last[1], 3))

# --- инвариант против эталона исследования
ref = {}
with open(os.path.join(S, "data", "walkforward_results.csv"), encoding="utf-8") as f:
    for row in csv.DictReader(f):
        if row.get("M1_fixed"):
            ref[row[""][:7] if "" in row else row[list(row)[0]][:7]] = float(row["M1_fixed"])
ours = {d[:7]: c for d, c in zip(mf["dates"], mf["composite"]) if c is not None}
diffs = [(k, abs(ours[k] - ref[k])) for k in ref if k in ours]
mx = max(diffs, key=lambda x: x[1]) if diffs else None
msg = f"invariant: {len(diffs)} months compared, max|diff| = {mx[1]:.12f} at {mx[0]}" if mx else "no overlap"
print(msg)
open(os.path.join(S, "data", "invariant_check.txt"), "w").write(msg + "\n")

# --- сырьё длинным списком
EXTRA = ["imoex", "imoex_value", "imoex2", "mcftr", "rgbi", "rtsi", "rvi", "mcxsm", "rusfar3m",
         "rucbhycp_yield", "rucbcpns_yield", "cny_tom", "gld_tom", "usd_cbr", "cny_cbr", "key_rate",
         "deposit_decade", "brent", "brent_moex", "urals_tax", "ofz_auctions", "budget_deficit",
         "cpi_weekly", "cpi_monthly", "lqdt_aum", "moex_retail", "polymarket_ceasefire", "cb_consensus",
         "events_registry", "dividends", "breadth",
         "zcyc_y0_5", "zcyc_y1", "zcyc_y2", "zcyc_y5", "zcyc_y10",
         "futoi_mx_pos", "futoi_mx_long", "futoi_mx_short", "futoi_mx_holders_long", "futoi_mx_holders_short",
         "futoi_mx_yur_pos", "futoi_mx_yur_long", "futoi_mx_yur_short",
         "orfr_flows_fiz", "orfr_flows_nfo_du", "orfr_flows_nfo_own", "orfr_flows_nonres",
         "orfr_flows_other_banks", "orfr_flows_szko"]
n = 0
with open(os.path.join(S, "data", "raw_long.csv"), "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f); w.writerow(["series", "date", "value"])
    for sid in EXTRA:
        ser = store.load_series(sid)
        if not ser:
            print("  missing", sid); continue
        for d, v in sorted((ser.get("points") or {}).items()):
            if isinstance(v, dict):
                v = v.get("value", v.get("close"))
            if calc.is_num(v):
                w.writerow([sid, d, repr(float(v))]); n += 1
print("raw_long rows:", n)
# ширина: список тикеров
px = sorted(s for s in store.list_series() if s.startswith("px_"))
with open(os.path.join(S, "data", "prices_wide.csv"), "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f); w.writerow(["date"] + px)
    ptsd = {s: (store.load_series(s) or {}).get("points") or {} for s in px}
    alld = sorted(set().union(*[set(p) for p in ptsd.values()]))
    for d in alld:
        w.writerow([d] + [("" if not calc.is_num(ptsd[s].get(d)) else repr(float(ptsd[s][d]))) for s in px])
print("prices_wide:", len(alld), "days x", len(px), "tickers")
