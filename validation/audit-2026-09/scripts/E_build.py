"""E_algopack шаг 3: дневная таблица признаков из ALGOPACK (FUTOI по корням, tradestats/obstats
агрегаты, HI2) на торговом календаре панели -> data/algopack/E_features_daily.csv.
Все признаки датированы днём наблюдения (данные ALGOPACK публикуются в тот же день, задержка 0),
позиция по сигналу на закрытии дня t применяется с закрытия t (см. E_test/E_strategy)."""
import sys, os, glob, gzip
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
D = os.path.join(ROOT, "data")
A = os.path.join(D, "algopack")

panel = pd.read_csv(os.path.join(D, "panel_prod_daily.csv"), parse_dates=["date"]).set_index("date")
cal = panel.index[panel.index >= "2019-12-01"]
out = pd.DataFrame(index=cal)


def zroll(s, win, minp):
    m = s.rolling(win, min_periods=minp).mean()
    sd = s.rolling(win, min_periods=minp).std()
    return (s - m) / sd


def on_cal(s, limit=3):
    """Ряд по датам источника -> календарь панели, ffill не более limit дней."""
    s = s[~s.index.duplicated()].sort_index()
    return s.reindex(cal.union(s.index)).ffill(limit=limit).reindex(cal)


# ------------------------------------------------------------------ FUTOI
f = pd.read_csv(os.path.join(A, "futoi_daily.csv"), parse_dates=["date"])
f["gross"] = f["long"].abs() + f["short"].abs()
fz = f[f.group == "FIZ"].set_index(["root", "date"])
yu = f[f.group == "YUR"].set_index(["root", "date"])
for root in ["MX", "RI", "Si", "CR", "BR", "IMOEXF", "GD"]:
    if root not in fz.index.get_level_values(0):
        continue
    s = fz.loc[root].sort_index()
    y = yu.loc[root].sort_index()
    share = s.pos / s.gross
    out[f"fshare_{root}"] = on_cal(share)
    out[f"fz_{root}"] = zroll(out[f"fshare_{root}"], 120, 60)          # метод панели (z120)
    out[f"fz252_{root}"] = zroll(out[f"fshare_{root}"], 252, 126)
    holders = s.n_long + s.n_short
    out[f"fholders_{root}"] = on_cal(holders)
    out[f"fhold_g21_{root}"] = np.log(out[f"fholders_{root}"]).diff(21)
    out[f"fgross_{root}"] = on_cal(s.gross)
    out[f"fgross_g21_{root}"] = np.log(out[f"fgross_{root}"]).diff(21)
    # доля розницы в общем ОИ
    out[f"fretail_{root}"] = on_cal(s.gross / (s.gross + y.gross.reindex(s.index)))
    out[f"fretail_z_{root}"] = zroll(out[f"fretail_{root}"], 252, 126)
    # внутридневное изменение нетто-позиции физлиц (последний - первый снимок), в долях брутто
    out[f"fintra_{root}"] = on_cal((s.pos - s.pos_first) / s.gross)
    out[f"fintra5_{root}"] = out[f"fintra_{root}"].rolling(5, min_periods=3).sum()
    # доля лонгистов по числу лиц (толпа), и «кит»: средний лонг на лицо / средний шорт на лицо
    out[f"fnlong_{root}"] = on_cal(s.n_long / (s.n_long + s.n_short))
    out[f"fnlong_z_{root}"] = zroll(out[f"fnlong_{root}"], 120, 60)
    whale = np.log((s["long"].abs() / s.n_long.replace(0, np.nan)) / (s["short"].abs() / s.n_short.replace(0, np.nan)))
    out[f"fwhale_{root}"] = on_cal(whale)
    out[f"fwhale_z_{root}"] = zroll(out[f"fwhale_{root}"], 120, 60)
    # открытый интерес: всего (лонги физлиц + лонги юрлиц) и брутто юрлиц (хеджеры); рост за 21 дн
    oi = s["long"].abs() + y["long"].abs().reindex(s.index)
    out[f"foi_{root}"] = on_cal(oi)
    out[f"foi_g21_{root}"] = np.log(out[f"foi_{root}"]).diff(21)
    out[f"fyur_g21_{root}"] = np.log(on_cal(y.gross)).diff(21)
    # нетто-позиция физлиц к ОБЩЕМУ ОИ (а не к собственному брутто) — устойчивее к росту розницы
    out[f"fshare_oi_{root}"] = on_cal(s.pos / (2 * oi))
    out[f"fz_oi_{root}"] = zroll(out[f"fshare_oi_{root}"], 120, 60)
    # изменение нетто-доли за 5 и 21 день (поток, а не уровень)
    out[f"fshare_d5_{root}"] = out[f"fshare_{root}"].diff(5)
    out[f"fshare_d21_{root}"] = out[f"fshare_{root}"].diff(21)

# композиты по индексу: среднее z по RI и MX (разные мультипликаторы -> z-усреднение)
out["fz_idx"] = out[["fz_RI", "fz_MX"]].mean(axis=1)
out["fz252_idx"] = out[["fz252_RI", "fz252_MX"]].mean(axis=1)
out["fshare_d21_idx"] = out[["fshare_d21_RI", "fshare_d21_MX"]].mean(axis=1)
# хедж рубля против риска: z доли физлиц в Si минус z по индексу
out["fhedge"] = out["fz_Si"] - out["fz_idx"]
out["fhedge_cr"] = out[["fz_Si", "fz_CR"]].mean(axis=1) - out["fz_idx"]
out["fhold_g21_idx"] = out[["fhold_g21_RI", "fhold_g21_MX"]].mean(axis=1)
out["fintra5_idx"] = out[["fintra5_RI", "fintra5_MX"]].mean(axis=1)
out["fretail_z_idx"] = out[["fretail_z_RI", "fretail_z_MX"]].mean(axis=1)
out["fnlong_z_idx"] = out[["fnlong_z_RI", "fnlong_z_MX"]].mean(axis=1)
out["fwhale_z_idx"] = out[["fwhale_z_RI", "fwhale_z_MX"]].mean(axis=1)
out["foi_g21_idx"] = out[["foi_g21_RI", "foi_g21_MX"]].mean(axis=1)
out["fyur_g21_idx"] = out[["fyur_g21_RI", "fyur_g21_MX"]].mean(axis=1)
out["fz_oi_idx"] = out[["fz_oi_RI", "fz_oi_MX"]].mean(axis=1)

# ------------------------------------------------------------------ TRADESTATS дневные
CORE = ["SBER", "LKOH", "GAZP"]   # с 2020 (tradestats+obstats), GMKN/ROSN только с 2025 (кэш)
rows = []
for fn in sorted(glob.glob(os.path.join(A, "tradestats_raw", "*.csv.gz"))):
    try:
        r = pd.read_csv(fn)
    except Exception:
        continue
    if r.empty:
        continue
    g = r.groupby("tradedate").agg(val=("val", "sum"), val_b=("val_b", "sum"), val_s=("val_s", "sum"),
                                   trades=("trades", "sum"), tr_b=("trades_b", "sum"), tr_s=("trades_s", "sum"),
                                   vol=("vol", "sum"), bars=("val", "size"))
    g["ticker"] = r.secid.iloc[0]
    rows.append(g.reset_index().rename(columns={"tradedate": "date"}))
raw = pd.concat(rows) if rows else pd.DataFrame()
if not raw.empty:
    raw["date"] = pd.to_datetime(raw.date)
    raw["src"] = "raw"
    # obstats (стакан) 2020-2023: дневное среднее дисбаланса по 5-мин срезам
    orows = []
    for fn in sorted(glob.glob(os.path.join(A, "obstats_raw", "*.csv.gz"))):
        try:
            o = pd.read_csv(fn)
        except Exception:
            continue
        if o.empty:
            continue
        og = o.groupby("tradedate").agg(ob_imb=("imbalance_vol", "mean"), ob_imb_val=("imbalance_val", "mean"),
                                        ob_imb_bbo=("imbalance_vol_bbo", "mean"), sp_bbo=("spread_bbo", "median"),
                                        sp_1mio=("spread_1mio", "median"))
        og["ticker"] = o.secid.iloc[0]
        orows.append(og.reset_index().rename(columns={"tradedate": "date"}))
    if orows:
        ob = pd.concat(orows); ob["date"] = pd.to_datetime(ob.date)
        raw = raw.merge(ob, on=["ticker", "date"], how="left")

m = pd.read_csv(os.path.join(A, "micro_sessions.csv"), parse_dates=["date"])
m = m[m.session != "wknd"]
m = m[m.date < "2026-09-01"]   # 01.09 в кэше неполный (last_t 12:15)
mg = m.groupby(["ticker", "date"]).agg(val=("val", "sum"), val_b=("val_b", "sum"), val_s=("val_s", "sum"),
                                       trades=("trades", "sum"), tr_b=("tr_b", "sum"), tr_s=("tr_s", "sum"),
                                       vol=("vol", "sum"), bars=("bars", "sum"),
                                       ob_imb=("imb", "mean"), sp_bbo=("sp_bbo", "first"), sp_1mio=("sp_1mio", "first")).reset_index()
mg["src"] = "micro"
ts = pd.concat([raw, mg], ignore_index=True) if not raw.empty else mg
ts = ts.sort_values(["ticker", "date"]).drop_duplicates(["ticker", "date"], keep="last")
ts = ts[ts.val > 0]
ts["imb"] = (ts.val_b - ts.val_s) / ts.val
ts["imb_tr"] = (ts.tr_b - ts.tr_s) / ts.trades.replace(0, np.nan)
ts["avg_trade"] = ts.val / ts.trades.replace(0, np.nan)
ts.to_csv(os.path.join(A, "E_tradestats_daily.csv"), index=False)
print("tradestats дневные: строк", len(ts), "тикеров", ts.ticker.nunique(), ts.date.min().date(), ts.date.max().date())
print(ts.groupby("ticker").date.agg(["min", "max", "count"]).to_string())


def agg_market(sub, tag):
    """Рыночные агрегаты по value-взвешиванию (сумма покупок-продаж / сумма оборота)."""
    g = sub.groupby("date")
    a = pd.DataFrame({
        "imb": g.apply(lambda x: (x.val_b.sum() - x.val_s.sum()) / x.val.sum()),
        "imb_ew": g.imb.mean(),
        "imb_tr": g.apply(lambda x: (x.tr_b.sum() - x.tr_s.sum()) / x.trades.sum()),
        "trades": g.trades.sum(), "val": g.val.sum(),
        "avg_trade": g.apply(lambda x: x.val.sum() / x.trades.sum()),
        "n": g.size(),
    })
    if "ob_imb" in sub:
        # дисбаланс стакана: только дни, где он есть у ВСЕХ бумаг набора (иначе состав скачет)
        def _ob(x):
            y = x.dropna(subset=["ob_imb"])
            if len(y) < len(x):
                return np.nan
            return np.average(y.ob_imb, weights=y.val)
        a["ob_imb"] = g.apply(_ob)
    a = a.reindex(cal)
    res = {}
    res[f"imb_{tag}"] = a.imb
    res[f"imb5_{tag}"] = a.imb.rolling(5, min_periods=4).mean()
    res[f"imb21_{tag}"] = a.imb.rolling(21, min_periods=15).mean()
    res[f"imb5z_{tag}"] = zroll(res[f"imb5_{tag}"], 252, 126)
    res[f"imb21z_{tag}"] = zroll(res[f"imb21_{tag}"], 252, 126)
    res[f"imbew21_{tag}"] = a.imb_ew.rolling(21, min_periods=15).mean()
    res[f"imbtr21_{tag}"] = a.imb_tr.rolling(21, min_periods=15).mean()
    lt = np.log(a.trades)
    res[f"trades_g_{tag}"] = lt.rolling(5, min_periods=4).mean() - lt.rolling(63, min_periods=40).mean()
    lat = np.log(a.avg_trade)
    res[f"avgtr_{tag}"] = lat.rolling(5, min_periods=4).mean() - lat.rolling(126, min_periods=60).mean()
    res[f"avgtr_z_{tag}"] = zroll(lat, 252, 126)
    lv = np.log(a.val)
    res[f"val_g_{tag}"] = lv.rolling(5, min_periods=4).mean() - lv.rolling(63, min_periods=40).mean()
    if "ob_imb" in a:
        res[f"obimb_{tag}"] = a.ob_imb
        res[f"obimb5_{tag}"] = a.ob_imb.rolling(5, min_periods=4).mean()
        res[f"obimb21_{tag}"] = a.ob_imb.rolling(21, min_periods=15).mean()
    res[f"n_{tag}"] = a.n
    return pd.DataFrame(res)


core = ts[ts.ticker.isin(CORE)]
# ядро считаем только на днях, где есть все бумаги ядра (иначе состав скачет)
full = core.groupby("date").ticker.nunique()
core = core[core.date.isin(full[full == len(CORE)].index)]
out = out.join(agg_market(core, "c3"))
out = out.join(agg_market(ts[ts.src == "micro"], "all"))
# SBER отдельно: единственная бумага с НЕПРЕРЫВНЫМ стаканом 2020-2026 (obstats raw + micro)
out = out.join(agg_market(ts[ts.ticker == "SBER"], "sber")[["obimb_sber", "obimb5_sber", "obimb21_sber", "avgtr_z_sber"]])
# число сделок ядра (драйвер среднего размера сделки) — z252 от 5-дн лог-уровня
_lt = np.log(core.groupby("date").trades.sum()).reindex(cal)
out["ntr_z_c3"] = zroll(_lt.rolling(5, min_periods=4).mean(), 252, 126)

# ------------------------------------------------------------------ HI2
rows = []
for fn in sorted(glob.glob(os.path.join(A, "hi2", "*.csv"))):
    h = pd.read_csv(fn)
    if h.empty:
        continue
    rows.append(h)
if rows:
    h = pd.concat(rows)
    h["date"] = pd.to_datetime(h.tradedate)
    h = h[h.date.dt.weekday < 5]
    piv = h.pivot_table(index=["secid", "date"], columns="metric", values="value", aggfunc="last")
    piv.to_csv(os.path.join(A, "E_hi2_daily.csv"))
    print("hi2: тикеров", piv.index.get_level_values(0).nunique(), "строк", len(piv), "метрики", list(piv.columns))
    piv = piv.reset_index()
    piv["bs"] = piv.hhi_agressive_buy - piv.hhi_agressive_sell
    piv["nf"] = piv.hhi_netflow_buy - piv.hhi_netflow_sell
    piv["ap"] = np.log(piv.hhi_agressive) - np.log(piv.hhi_passive)
    piv["pbs"] = piv.hhi_passive_buy - piv.hhi_passive_sell
    piv["lbs"] = np.log(piv.hhi_agressive_buy) - np.log(piv.hhi_agressive_sell)
    piv["lnf"] = np.log(piv.hhi_netflow_buy) - np.log(piv.hhi_netflow_sell)
    g = piv.groupby("date")
    hm = pd.DataFrame({"hi_bs": g.bs.mean(), "hi_lbs": g.lbs.mean(), "hi_nf": g.nf.mean(), "hi_lnf": g.lnf.mean(),
                       "hi_ap": g.ap.mean(), "hi_pbs": g.pbs.mean(),
                       "hi_vol": np.log(g.hhi_volume.mean()), "hi_agr": np.log(g.hhi_agressive.mean()),
                       "hi_n": g.size()}).reindex(cal)
    for c in ["hi_bs", "hi_lbs", "hi_nf", "hi_lnf", "hi_ap", "hi_pbs"]:
        out[c] = hm[c]
        out[c + "5"] = hm[c].rolling(5, min_periods=4).mean()
        out[c + "21"] = hm[c].rolling(21, min_periods=15).mean()
        out[c + "21z"] = zroll(out[c + "21"], 252, 126)
    out["hi_vol"] = hm.hi_vol
    out["hi_vol_z"] = zroll(hm.hi_vol.rolling(5, min_periods=4).mean(), 252, 126)
    out["hi_agr_z"] = zroll(hm.hi_agr.rolling(5, min_periods=4).mean(), 252, 126)
    out["hi_n"] = hm.hi_n

out.index.name = "date"
out.to_csv(os.path.join(A, "E_features_daily.csv"))
print("признаков:", out.shape[1], "дней:", len(out), cal[0].date(), cal[-1].date())
print("покрытие (первый/последний непустой):")
for c in out.columns:
    s = out[c].dropna()
    if len(s):
        print(f"  {c:22s} {s.index[0].date()}..{s.index[-1].date()} n={len(s)}")
