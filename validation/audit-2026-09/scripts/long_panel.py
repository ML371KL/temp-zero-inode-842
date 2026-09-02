"""Long panel 2003-2026: signals on full history + ex-ante state variables."""
import sys, os
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
D = "data"

def load_idx(sec, col="CLOSE"):
    df = pd.read_csv(f"{D}/idx_{sec}.csv", parse_dates=["TRADEDATE"])
    s = df.set_index("TRADEDATE")[col]
    return s[~s.index.duplicated(keep="last")].sort_index()

px = load_idx("IMOEX")
val = load_idx("IMOEX", "VALUE")
mcftr = load_idx("MCFTR")
rgbi = load_idx("RGBI")
rtsi = load_idx("RTSI")
cal = px.index
P = pd.DataFrame(index=cal)
P["imoex"] = px
P["ret1"] = np.log(px).diff()
for H in [21, 63]:
    P[f"fwd{H}"] = np.log(px.shift(-H) / px)

# ---- long FX: official (2013+) spliced with implied IMOEX/RTSI (before) ----
usd_of = pd.read_csv(f"{D}/cbr_usd.csv", parse_dates=["date"]).set_index("date")["rate"]
usd_of = usd_of[~usd_of.index.duplicated(keep="last")].sort_index()
ratio = (px / rtsi).dropna()
k = 31.4949
usd_imp = ratio * k
usd_long = usd_of.reindex(cal)
usd_long = usd_long.fillna(usd_imp.reindex(cal))
usd_long = usd_long.ffill()
P["usd"] = usd_long
P["usd_mom63"] = np.log(usd_long / usd_long.shift(63))

# ---- signals on long history ----
P["dd252"] = np.log(px / px.rolling(252).max())
P["mom63"] = np.log(px / px.shift(63))
P["rgbi_mom21"] = np.log(rgbi / rgbi.shift(21)).reindex(cal)
P["rgbi_dd"] = np.log(rgbi / rgbi.rolling(252).max()).reindex(cal).ffill(limit=5)

m_al = mcftr.reindex(cal).ffill(limit=5)
P["dy_trail"] = (np.log(m_al / m_al.shift(252)) - np.log(px / px.shift(252))) * 100

brent = pd.read_csv(f"{D}/brent.csv", parse_dates=["date"]).set_index("date")["brent"].astype(float)
brent = brent.sort_index().reindex(cal).ffill(limit=7)
rb = brent * usd_long
P["rb_gap"] = np.log(rb / rb.rolling(504).mean())
P["brent_mom63"] = np.log(brent / brent.shift(63))

lv = np.log(val.replace(0, np.nan))
P["vol_z"] = (lv - lv.rolling(60).mean()) / lv.rolling(60).std()

# deposit / switch spread (2009+)
dep = pd.read_csv(f"{D}/cbr_deposit.csv", parse_dates=["date"]).set_index("date")["rate"].sort_index()
dep.index = dep.index + pd.Timedelta(days=4)
P["deposit"] = dep.reindex(cal.union(dep.index)).ffill().reindex(cal)
P["switch_spread"] = P["dy_trail"] - P["deposit"]

# MCXSM rel (2013+)
mcx = load_idx("MCXSM").reindex(cal).ffill(limit=3)
P["mcxsm_rel63"] = np.log(mcx / mcx.shift(63)) - P["mom63"]

# RVI (2014+)
rvi = load_idx("RVI").reindex(cal).ffill(limit=3)
P["rvi"] = rvi

# zcyc (2015+)
zc = pd.read_csv(f"{D}/zcyc_daily.csv", parse_dates=["date"]).set_index("date").sort_index()
zc = zc[~zc.index.duplicated(keep="last")]
for c in ["y1.0", "y2.0", "y10.0"]:
    P[c] = zc[c].reindex(cal).ffill(limit=5)
P["slope_10_2"] = P["y10.0"] - P["y2.0"]

# HY spread (2021+)
hy = load_idx("RUCBHYCP", "YIELD").reindex(cal).ffill(limit=3)
P["hy_spread"] = hy - (P["y1.0"] + P["y2.0"]) / 2

# FUTOI re-engineered (2020+)
fu = pd.read_csv(f"{D}/futoi_MX.csv", parse_dates=["date"])
fiz = fu[fu.clgroup == "FIZ"].set_index("date").sort_index()
fizsh = (fiz["pos"] / (fiz["pos_long"] - fiz["pos_short"])).reindex(cal).ffill(limit=3)
P["futoi_z120"] = (fizsh - fizsh.rolling(120, min_periods=60).mean()) / fizsh.rolling(120, min_periods=60).std()
P["futoi_chg21"] = fizsh.diff(21)

# breadth (2014+)
if os.path.exists(f"{D}/stocks_daily.csv"):
    st = pd.read_csv(f"{D}/stocks_daily.csv", parse_dates=["TRADEDATE"])
    piv = st.pivot_table(index="TRADEDATE", columns="SECID", values="px", aggfunc="last").sort_index()
    ma200 = piv.rolling(200, min_periods=150).mean()
    above = (piv > ma200).where(piv.notna() & ma200.notna())
    cnt = above.notna().sum(axis=1).astype(float).replace(0, np.nan)
    P["breadth"] = (above.sum(axis=1) / cnt).where(cnt >= 15).reindex(cal)

# tax-Urals rub barrel gap (2015+, from prior panel)
old = pd.read_csv(f"{D}/panel_daily.csv", index_col=0, parse_dates=True)
P["urals_rub_gap"] = old["urals_rub_gap"].reindex(cal)

# September dummy
P["sep_node"] = (((cal.month == 9) & (cal.day >= 10)) | ((cal.month == 10) & (cal.day <= 5))).astype(float)

# ================= STATE VARIABLES (ex-ante) =================
ma200 = px.rolling(200).mean()
P["st_trend"] = (px > ma200).astype(float)            # 1 bull / 0 bear
rv = P["ret1"].rolling(21).std() * np.sqrt(252)
P["st_vol"] = (rv.rank(pct=True) * 0).fillna(0)       # placeholder replaced below
P["st_vol"] = (rv > rv.rolling(756, min_periods=252).quantile(0.8)).astype(float)  # 1 = stress vol
kr = pd.read_csv(f"{D}/cbr_keyrate.csv", parse_dates=["date"]).set_index("date")["rate"]
kr = kr[~kr.index.duplicated(keep="last")].sort_index().reindex(cal).ffill()
P["key"] = kr
kchg = kr.diff()
last_chg = kchg.replace(0, np.nan).ffill()            # sign of the most recent change
days_since = kchg.replace(0, np.nan).notna().cumsum()
P["st_rate"] = np.sign(last_chg)                      # -1 easing / +1 tightening (NaN pre-2013)
P["st_bond"] = (P["rgbi_dd"] < -0.04).astype(float)   # 1 = bond stress
P["era_post22"] = (cal >= "2022-03-24").astype(float)

P.to_csv(f"{D}/panel_long.csv")
have = {c: int(P[c].notna().sum()) for c in P.columns}
print(f"long panel: {P.shape}")
for c, n in have.items():
    first = P[c].first_valid_index()
    print(f"  {c:16s} n={n:6d} from {str(first)[:10]}")
EOF_MARKER_NOT_USED = None
