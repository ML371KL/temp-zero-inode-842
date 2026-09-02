"""Build daily/monthly signal panel with honest publication lags.

Outputs: data/panel_daily.csv (signals already shifted to availability), data/panel_monthly.csv
"""
import sys, os, json
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
D = "data"


def load_idx(sec, col="CLOSE"):
    f = f"{D}/idx_{sec}.csv"
    if not os.path.exists(f):
        return None
    df = pd.read_csv(f, parse_dates=["TRADEDATE"])
    s = df.set_index("TRADEDATE")[col]
    s = s[~s.index.duplicated(keep="last")].sort_index()
    return s


# ---------- base: IMOEX ----------
px = load_idx("IMOEX")
val = load_idx("IMOEX", "VALUE")
mcftr = load_idx("MCFTR")
cal = px.index  # trading calendar

panel = pd.DataFrame(index=cal)
panel["imoex"] = px
lr = np.log(px).diff()
panel["ret1"] = lr
for H in [5, 21, 63]:
    panel[f"fwd{H}"] = np.log(px.shift(-H) / px)
if mcftr is not None:
    panel["fwd21_tr"] = np.log(mcftr.shift(-21) / mcftr)

# ---------- market internals ----------
panel["imoex_mom21"] = np.log(px / px.shift(21))
panel["imoex_mom63"] = np.log(px / px.shift(63))
panel["imoex_dd252"] = np.log(px / px.rolling(252).max())
panel["vol_z"] = (np.log(val).replace([np.inf, -np.inf], np.nan) - np.log(val).rolling(60).mean()) / np.log(val).rolling(60).std()

rvi = load_idx("RVI")
if rvi is not None:
    panel["rvi"] = rvi.reindex(cal).ffill(limit=3)
    panel["rvi_rev"] = panel["rvi"].rolling(20).max() - panel["rvi"]
    panel["rvi_spike"] = (panel["rvi"].rolling(20).max() > 45).astype(float) * panel["rvi_rev"]

mcx = load_idx("MCXSM")
if mcx is not None:
    m = mcx.reindex(cal).ffill(limit=3)
    panel["mcxsm_rel63"] = np.log(m / m.shift(63)) - panel["imoex_mom63"]

# ---------- bonds ----------
rgbi = load_idx("RGBI")
panel["rgbi_mom21"] = np.log(rgbi / rgbi.shift(21)).reindex(cal)
panel["rgbi_mom5"] = np.log(rgbi / rgbi.shift(5)).reindex(cal)
panel["rgbi_rev20"] = (rgbi / rgbi.rolling(20).min() - 1).reindex(cal)

hy_y = load_idx("RUCBHYCP", "YIELD")
ig_y = load_idx("RUCBCPNS", "YIELD")

# ---------- zcyc ----------
zc = None
if os.path.exists(f"{D}/zcyc_daily.csv"):
    zc = pd.read_csv(f"{D}/zcyc_daily.csv", parse_dates=["date"]).set_index("date").sort_index()
    zc = zc[~zc.index.duplicated(keep="last")]
    for c in ["y0.5", "y1.0", "y2.0", "y5.0", "y10.0"]:
        if c in zc.columns:
            panel[c] = zc[c].reindex(cal).ffill(limit=5)

# ---------- key rate ----------
kr = pd.read_csv(f"{D}/cbr_keyrate.csv", parse_dates=["date"]).set_index("date")["rate"]
kr = kr[~kr.index.duplicated(keep="last")].sort_index()
panel["key"] = kr.reindex(cal).ffill()

if "y1.0" in panel:
    panel["y1_minus_key"] = panel["y1.0"] - panel["key"]
    panel["y10_minus_key"] = panel["y10.0"] - panel["key"]
    panel["slope_10_2"] = panel["y10.0"] - panel["y2.0"]
    panel["d_y1_21"] = panel["y1.0"].diff(21)
    if hy_y is not None:
        hy = hy_y.reindex(cal).ffill(limit=3)
        panel["hy_spread"] = hy - (panel["y1.0"] + panel["y2.0"]) / 2
        panel["d_hy_spread21"] = panel["hy_spread"].diff(21)
    if ig_y is not None:
        ig = ig_y.reindex(cal).ffill(limit=3)
        panel["ig_spread"] = ig - panel["y2.0"]

rusfar = load_idx("RUSFAR3M")
if rusfar is not None:
    panel["rusfar_minus_key"] = rusfar.reindex(cal).ffill(limit=3) - panel["key"]

# ---------- FX / commodities ----------
usd = pd.read_csv(f"{D}/cbr_usd.csv", parse_dates=["date"]).set_index("date")["rate"]
usd = usd[~usd.index.duplicated(keep="last")].sort_index().reindex(cal).ffill()
panel["usd"] = usd
panel["usd_mom21"] = np.log(usd / usd.shift(21))
panel["usd_mom63"] = np.log(usd / usd.shift(63))

brent = pd.read_csv(f"{D}/brent.csv", parse_dates=["date"]).set_index("date")["brent"].astype(float)
brent = brent.sort_index().reindex(cal).ffill(limit=5)
panel["brent"] = brent
panel["brent_mom21"] = np.log(brent / brent.shift(21))
rb = brent * usd
panel["rub_barrel"] = rb
panel["rb_mom21"] = np.log(rb / rb.shift(21))
panel["rb_gap252"] = np.log(rb / rb.rolling(252).mean())

if os.path.exists(f"{D}/fx_GLDRUB_TOM.csv"):
    gld = pd.read_csv(f"{D}/fx_GLDRUB_TOM.csv", parse_dates=["TRADEDATE"]).set_index("TRADEDATE")["CLOSE"]
    gld = gld[~gld.index.duplicated(keep="last")].sort_index().reindex(cal).ffill(limit=5)
    panel["gld_mom63"] = np.log(gld / gld.shift(63))

# ---------- external (negative controls) ----------
for nm, col in [("spx", "close"), ("eem", "close")]:
    f = f"{D}/yh_{nm}.csv"
    if os.path.exists(f):
        s = pd.read_csv(f, parse_dates=["date"]).set_index("date")[col]
        s = s[~s.index.duplicated(keep="last")].sort_index().reindex(cal).ffill(limit=5)
        panel[f"{nm}_mom21"] = np.log(s / s.shift(21))

# ---------- deposit decade rate ----------
dep_f = f"{D}/cbr_deposit.csv"
if os.path.exists(dep_f):
    dep = pd.read_csv(dep_f, parse_dates=["date"]).set_index("date")["rate"].sort_index()
    # availability: decade value published ~4 days after decade end
    dep.index = dep.index + pd.Timedelta(days=4)
    depd = dep.reindex(cal.union(dep.index)).ffill().reindex(cal)
    panel["deposit"] = depd
    panel["deposit_mom"] = depd.diff(30)  # ~3 decades
    # first uptick after long decline
    ch = np.sign(dep.diff())
    panel["real_key_dep_gap"] = panel["key"] - depd

# ---------- trailing dividend yield (MCFTR vs IMOEX) ----------
# MCFTR is reindexed to the IMOEX calendar BEFORE shift(252): its own calendar
# misses 2022-01-07 / 2022-03-24, which desynchronised the 252d anchors (up to
# 40pp error in H1-2023 when the anchor crossed the Feb-2022 crash).
if mcftr is not None:
    m_al = mcftr.reindex(cal).ffill(limit=5)
    dy = (np.log(m_al / m_al.shift(252)) - np.log(px / px.shift(252))) * 100
    panel["div_yield_trail"] = dy
    if "deposit" in panel:
        panel["switch_spread"] = panel["div_yield_trail"] - panel["deposit"]

# ---------- FUTOI ----------
for tk in ["MX", "MIX"]:
    f = f"{D}/futoi_{tk}.csv"
    if os.path.exists(f):
        fu = pd.read_csv(f, parse_dates=["date"])
        fiz = fu[fu.clgroup == "FIZ"].set_index("date")["pos"].sort_index()
        fiz = fiz[~fiz.index.duplicated(keep="last")].reindex(cal).ffill(limit=3)
        oi = fu[fu.clgroup == "FIZ"].set_index("date")
        oi_tot = (oi["pos_long"] - oi["pos_short"]).sort_index()
        oi_tot = oi_tot[~oi_tot.index.duplicated(keep="last")].reindex(cal).ffill(limit=3)
        ratio = fiz / oi_tot
        panel[f"futoi_{tk}_fizsh"] = ratio
        panel[f"futoi_{tk}_pct"] = ratio.rolling(252, min_periods=120).rank(pct=True)

# ---------- monthly signals -> daily with availability lag ----------
def month_avail(month_str, lag_days):
    end = pd.Timestamp(month_str) + pd.offsets.MonthEnd(0)
    return end + pd.Timedelta(days=lag_days)


def monthly_to_daily(rows, valcol, lag_days, name):
    s = pd.Series({month_avail(r["month"], lag_days): r[valcol] for r in rows if r.get(valcol) is not None})
    s = s.sort_index()
    sd = s.reindex(cal.union(s.index)).ffill().reindex(cal)
    panel[name] = sd
    return s


wf = {}
if os.path.exists(f"{D}/workflow_collected.json"):
    wf = json.load(open(f"{D}/workflow_collected.json", encoding="utf-8"))

# CPI monthly -> yoy, SAAR
if wf.get("cpi_monthly"):
    rows = wf["cpi_monthly"]["rows"]
    cpi = pd.DataFrame(rows).drop_duplicates("month").sort_values("month")
    cpi["m"] = pd.PeriodIndex(cpi["month"], freq="M")
    cpi = cpi.set_index("m")
    mm = cpi["mm_pct"].astype(float)
    # seasonal factors from 2011-2019 window if available else 2015-2019
    base = mm[(mm.index.year >= 2014) & (mm.index.year <= 2019)]
    seas = base.groupby(base.index.month).mean()
    seas = seas - seas.mean()
    sa = mm - mm.index.month.map(seas)
    saar1 = ((1 + sa / 100) ** 12 - 1) * 100
    saar3 = saar1.rolling(3).mean()
    yoy = ((1 + mm / 100).rolling(12).apply(np.prod) - 1) * 100
    dfm = pd.DataFrame({"mm": mm, "saar1": saar1, "saar3": saar3, "yoy": yoy})
    dfm.to_csv(f"{D}/cpi_derived.csv")
    rows2 = [{"month": str(p), "saar3": v} for p, v in saar3.items() if v == v]
    monthly_to_daily(rows2, "saar3", 13, "saar3")
    rows3 = [{"month": str(p), "yoy": v} for p, v in yoy.items() if v == v]
    monthly_to_daily(rows3, "yoy", 13, "cpi_yoy")
    panel["real_key_saar"] = panel["key"] - panel["saar3"]
    panel["real_key_yoy"] = panel["key"] - panel["cpi_yoy"]

# Urals tax price -> tax rub barrel gap
if wf.get("urals"):
    rows = []
    for part in wf["urals"]:
        rows.extend(part["rows"])
    ur = pd.DataFrame(rows).drop_duplicates("month").sort_values("month")
    usd_m = usd.resample("ME").mean()
    ur["m_end"] = pd.to_datetime(ur["month"]) + pd.offsets.MonthEnd(0)
    ur["rub"] = ur.apply(lambda r: r["usd"] * usd_m.get(r["m_end"], np.nan), axis=1)
    ur["rub_gap"] = np.log(ur["rub"] / ur["rub"].rolling(24, min_periods=12).mean())
    rows2 = [{"month": r["month"], "v": r["rub_gap"]} for _, r in ur.iterrows() if r["rub_gap"] == r["rub_gap"]]
    monthly_to_daily(rows2, "v", 5, "urals_rub_gap")
    ur.to_csv(f"{D}/urals_derived.csv", index=False)

# ORFR flows
if wf.get("orfr"):
    rows = []
    for part in wf["orfr"]:
        rows.extend(part["rows"])
    orfr = pd.DataFrame(rows).drop_duplicates("month").sort_values("month")
    orfr.to_csv(f"{D}/orfr_flows.csv", index=False)
    for col, nm in [("fiz", "orfr_fiz"), ("nfo_du", "orfr_du"), ("nonres", "orfr_nonres")]:
        if col in orfr.columns:
            rr = [{"month": r["month"], "v": r[col]} for _, r in orfr.iterrows() if pd.notna(r.get(col))]
            if len(rr) > 10:
                monthly_to_daily(rr, "v", 15, nm)
    # DU 3m rolling sum and its delta (exhaustion)
    if "nfo_du" in orfr.columns:
        du = orfr.set_index("month")["nfo_du"].astype(float)
        du3 = du.rolling(3).sum()
        ddu = du3.diff()
        rr = [{"month": m, "v": v} for m, v in du3.items() if v == v]
        monthly_to_daily(rr, "v", 15, "orfr_du3m")
        rr = [{"month": m, "v": v} for m, v in ddu.items() if v == v]
        monthly_to_daily(rr, "v", 15, "orfr_du3m_chg")

# ---------- breadth (% of liquid tickers above 200DMA) ----------
if os.path.exists(f"{D}/stocks_daily.csv"):
    st = pd.read_csv(f"{D}/stocks_daily.csv", parse_dates=["TRADEDATE"])
    piv = st.pivot_table(index="TRADEDATE", columns="SECID", values="px", aggfunc="last").sort_index()
    ma200 = piv.rolling(200, min_periods=150).mean()
    above = (piv > ma200).where(piv.notna() & ma200.notna())
    cnt = above.notna().sum(axis=1).astype(float).replace(0, np.nan)
    breadth = above.sum(axis=1) / cnt
    breadth[cnt < 15] = np.nan
    panel["breadth_pct200"] = breadth.reindex(cal)

panel.to_csv(f"{D}/panel_daily.csv")
have = [c for c in panel.columns if panel[c].notna().sum() > 100]
print(f"panel: {panel.shape}, usable cols: {len(have)}")
print(sorted(have))
