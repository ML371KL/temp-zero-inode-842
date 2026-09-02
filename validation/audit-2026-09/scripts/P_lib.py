"""P_package: ЭТАЛОННЫЙ ДВИЖОК слоя решения панели «MOEX Radar» — один автомат позиции,
на котором пошагово измеряется пакет изменений (ступени P0…P7 и P3m, абляции, чувствительность,
плацебо, walk-forward). Контроль: P0 побитово = A_baseline pos_c, месячный вариант = pos_e.

Соглашения (бриф, правила 1–5):
  • сигнал на закрытии дня t → позиция с закрытия t+lag (lag=1 основное, lag=0 коридор);
  • лонг = MCFTR (полная доходность), флэт = mm_rate/252 (ставка, известная на закрытии t−1);
  • издержки cost за каждую смену позиции (0,2 %; чувствительность 0,1/0,3 %);
  • метрики на месячной сетке NAV; MDD по дневному NAV; бутстреп — стационарный, блок 12 мес.

Автомат (run_engine):
  comp_state — знак композита с гистерезисом ±thr по значениям, ВЗЯТЫМ В ДНИ РЕШЕНИЯ
               (P0: закрытый месяц, читается ежедневно = как прод; P1+: дневное значение по пятницам);
  gate       — ворота: ячейка (trend,vol,bond) ≠ (0,1,1); биты прода или с гистерезисом;
  es         — бит-выход «репрайсинг ожиданий» (y1 КБД − ключ выросла > thr за win дней);
  stab       — «стабилизация»: второй разрешённый вход в токсичной ячейке (dd252 < −15 % И ret21 > 0);
  выход по воротам / es — ЛЮБЫМ днём; выход по композиту — в день решения;
  вход по воротам — любым днём (gate_entry='any') или в день решения ('dec');
  возврат после es-выхода — в день решения (es_reentry='dec') или сразу ('any').
Запуск скриптов — из каталога audit/.
"""
import os
import sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
RES = os.path.join(ROOT, "results")

TOXIC = "bear|stress|stress"
LEGS = [("usd_mom63", +1), ("slope_10_2", +1), ("urals_rub_gap", -1)]
Z_WIN, Z_MIN, Z_CLIP = 60, 24, 3.0
BT_START = pd.Timestamp("2004-01-06")
BT_END = pd.Timestamp("2026-08-31")
COST = 0.002

WINDOWS = {
    "main_2010_2026": ("2010-01-01", "2026-08-31"),
    "full_2004_2026": ("2004-01-06", "2026-08-31"),
    "era_2010_2021": ("2010-01-01", "2021-12-31"),
    "era_2022_2024": ("2022-03-24", "2024-12-31"),
    "era_2025_2026": ("2025-01-01", "2026-08-31"),
    "ex2022_2010_2026": ("2010-01-01", "2026-08-31"),
    "split_A_2004_2017": ("2004-01-06", "2017-12-31"),
    "split_B_2018_2026": ("2018-01-01", "2026-08-31"),
}


# ============================================================ данные
def load():
    D = pd.read_csv(os.path.join(DATA, "panel_prod_daily.csv"), parse_dates=["date"]).set_index("date")
    M = pd.read_csv(os.path.join(DATA, "panel_prod_monthly.csv"), parse_dates=["date"]).set_index("date")
    C = pd.read_csv(os.path.join(DATA, "cash_and_tr.csv"), parse_dates=["date"]).set_index("date")
    dec = pd.read_csv(os.path.join(DATA, "cb_decisions.csv"), parse_dates=["date"]).sort_values("date")
    assert D.index.equals(C.index)
    return D, M, C, dec


def closed_month_ends(idx):
    per = idx.to_period("M")
    s = idx.to_series()
    last = (s.groupby(per).transform("max") == s).values
    return idx[last & (per < per[-1])]


def hyst_states(vals, thr):
    """+1/−1 состояние гистерезиса после каждой точки; 0 = ещё не определялось."""
    out = np.zeros(len(vals), dtype=int)
    s = 0
    for i, v in enumerate(vals):
        if np.isfinite(v):
            if v > thr:
                s = 1
            elif v < -thr:
                s = -1
        out[i] = s
    return out


def daily_composite(D, Mc):
    """Дневное значение композита «как compute_core»: 59 закрытых месяцев + сегодняшнее значение ног,
    z по окну 60, min 24, ddof=1, обрезка ±3. На последний торговый день закрытого месяца = месячному."""
    month_of_day = D.index.to_period("M")
    m_per = Mc.index.to_period("M")
    out_sum = np.zeros(len(D)); out_n = np.zeros(len(D))
    for leg, sgn in LEGS:
        raw_m = Mc["raw_" + leg].values.astype(float)
        vals_d = D[leg].groupby(month_of_day).ffill().values.astype(float)
        z_d = np.full(len(D), np.nan)
        for per, ii in pd.Series(np.arange(len(D)), index=month_of_day).groupby(level=0):
            ii = ii.values
            k = int(np.searchsorted(m_per.asi8, per.ordinal))
            prev = raw_m[max(0, k - (Z_WIN - 1)):k]
            prev = prev[~np.isnan(prev)]
            n_prev = len(prev); s1 = prev.sum(); s2 = (prev ** 2).sum()
            v = vals_d[ii]; ok = ~np.isnan(v)
            n = n_prev + ok
            mean = (s1 + np.where(ok, v, 0.0)) / np.maximum(n, 1)
            ss = s2 + np.where(ok, v, 0.0) ** 2
            var = (ss - n * mean ** 2) / np.maximum(n - 1, 1)
            sd = np.sqrt(np.maximum(var, 0.0))
            with np.errstate(divide="ignore", invalid="ignore"):
                z = np.clip((v - mean) / sd, -Z_CLIP, Z_CLIP)
            z[~ok | (n < Z_MIN) | (sd <= 0)] = np.nan
            z_d[ii] = z
        good = ~np.isnan(z_d)
        out_sum[good] += sgn * z_d[good]; out_n[good] += 1
    return pd.Series(np.where(out_n > 0, out_sum / np.maximum(out_n, 1), np.nan), index=D.index)


class Market:
    """Все ряды, нужные автомату, на дневном индексе панели."""

    def __init__(self):
        D, M, C, dec = load()
        self.D, self.M, self.C, self.dec = D, M, C, dec
        self.idx = D.index
        self.n = len(D)
        me = closed_month_ends(D.index)
        Mc = M.loc[M.index.isin(me)].copy()
        assert Mc.index[-1] == BT_END
        self.Mc = Mc
        self.comp_closed = Mc["composite"].astype(float).reindex(D.index).ffill()
        self.comp_live = daily_composite(D, Mc)
        chk = (self.comp_live.reindex(Mc.index) - Mc["composite"]).abs().max()
        assert chk < 1e-9, chk
        # рынок
        self.mcftr = C["mcftr_ffill"]
        self.ret_eq = self.mcftr.pct_change()
        self.logret_eq = np.log(self.mcftr).diff()
        self.ret_mm = (C["mm_rate"] / 100.0 / 252.0).shift(1)
        self.mm_rate = C["mm_rate"]
        self.imoex = D["imoex"]
        # биты прода
        self.st_trend = D["st_trend"]; self.st_vol = D["st_vol"]; self.st_bond = D["st_bond"]
        self.cell_ok = D["cell"].notna()
        # сырьё для гистерезиса битов
        self.ratio_imoex = D["imoex"] / D["ma200"] - 1.0
        ma_tr = self.mcftr.rolling(200, min_periods=200).mean()
        self.ratio_mcftr = self.mcftr / ma_tr - 1.0
        self.rv = D["realized_vol_21"]; self.q80 = D["vol_thresh80"]
        self.q60 = self.rv.rolling(756, min_periods=252).quantile(0.60)
        self.rgbi_dd = D["rgbi_dd"]
        # репрайсинг ожиданий
        self.y1 = D["y1"]
        self.key_eff = D["key_rate"]
        kd = dec.set_index("date")["new_rate"]
        key_dec = kd.reindex(self.idx.union(kd.index)).ffill().reindex(self.idx)
        key_dec[self.idx < kd.index[0]] = np.nan
        self.key_dec = key_dec
        # стабилизация
        self.dd252 = D["dd252"]
        self.ret21 = np.log(D["imoex"] / D["imoex"].shift(21))
        # «распределительные дни» по обороту (D1): log(ср. оборот в дни падения / в дни роста, 21 дн)
        raw = pd.read_csv(os.path.join(DATA, "raw_long.csv"), parse_dates=["date"])
        vv = raw[raw.series == "imoex_value"].set_index("date")["value"].sort_index()
        vv = vv[~vv.index.duplicated()].reindex(self.idx)
        vv = vv.where(vv > 0)
        up = D["ret1"] > 0
        self.dist = np.log(vv.where(~up).rolling(21, min_periods=5).mean() / vv.where(up).rolling(21, min_periods=5).mean())

    def veto_bit(self, thr=0.10, lag=0):
        d = self.dist.shift(lag)
        b = (d > thr).astype(int).to_numpy().copy()
        b[~np.isfinite(d.to_numpy())] = 0
        return b

    # ---------------------------------------------------- дни решения
    def decision_mask(self, kind):
        if kind == "daily":
            return np.ones(self.n, dtype=bool)
        if kind == "monthly":
            per = self.idx.to_period("M")
        else:  # 'W-FRI', 'W-MON', ...
            per = self.idx.to_period(kind)
        s = self.idx.to_series()
        return (s.groupby(per).transform("max") == s).values

    # ---------------------------------------------------- биты и ворота
    def bit_hyst(self, x, on_thr, off_thr, on_if_greater, init):
        """Бит с двумя порогами. init — продовый бит для старта (чтобы не выдумывать состояние)."""
        xv = x.values; iv = init.values
        out = np.zeros(len(xv), dtype=float); cur = np.nan
        for i in range(len(xv)):
            v = xv[i]
            if not np.isfinite(v):
                out[i] = cur; continue
            if not np.isfinite(cur):
                cur = iv[i] if np.isfinite(iv[i]) else (1.0 if ((v > on_thr) if on_if_greater else (v < on_thr)) else 0.0)
            if on_if_greater:
                if v > on_thr: cur = 1.0
                elif v < off_thr: cur = 0.0
            else:
                if v < on_thr: cur = 1.0
                elif v > off_thr: cur = 0.0
            out[i] = cur
        return pd.Series(out, index=x.index)

    def bits(self, spec):
        """spec: dict(trend='prod'|'hyst'|'mcftr'|'mcftr_hyst', vol='prod'|'hyst', bond='prod'|'hyst'|'thr3').
        Возвращает (trend(1=бык), vol(1=стресс), bond(1=стресс))."""
        tr = spec.get("trend", "prod"); vo = spec.get("vol", "prod"); bo = spec.get("bond", "prod")
        band = spec.get("trend_band", 0.02)
        if tr == "prod":
            trend = self.st_trend.astype(float)
        elif tr == "hyst":
            trend = self.bit_hyst(self.ratio_imoex, band, -band, True, self.st_trend)
        elif tr == "mcftr":
            trend = (self.ratio_mcftr > 0).astype(float).where(self.ratio_mcftr.notna())
        elif tr == "mcftr_hyst":
            trend = self.bit_hyst(self.ratio_mcftr, band, -band, True, (self.ratio_mcftr > 0).astype(float).where(self.ratio_mcftr.notna()))
        else:
            raise ValueError(tr)
        if vo == "prod":
            vol = self.st_vol.astype(float)
        elif vo == "hyst":
            # включается при rv > p80 (порог прода), выключается при rv < p60
            rvv = self.rv.values; a = self.q80.values; b = self.q60.values; iv = self.st_vol.values
            out = np.zeros(self.n); cur = np.nan
            for i in range(self.n):
                if not (np.isfinite(rvv[i]) and np.isfinite(a[i]) and np.isfinite(b[i])):
                    out[i] = cur; continue
                if not np.isfinite(cur):
                    cur = iv[i] if np.isfinite(iv[i]) else float(rvv[i] > a[i])
                if rvv[i] > a[i]: cur = 1.0
                elif rvv[i] < b[i]: cur = 0.0
                out[i] = cur
            vol = pd.Series(out, index=self.idx)
        else:
            raise ValueError(vo)
        if bo == "prod":
            bond = self.st_bond.astype(float)
        elif bo == "hyst":
            bond = self.bit_hyst(self.rgbi_dd, spec.get("bond_on", -0.04), spec.get("bond_off", -0.03), False, self.st_bond)
        elif bo == "thr3":
            bond = (self.rgbi_dd < -0.03).astype(float).where(self.rgbi_dd.notna())
        else:
            raise ValueError(bo)
        return trend, vol, bond

    def gate(self, spec):
        trend, vol, bond = self.bits(spec)
        ok = trend.notna() & vol.notna() & bond.notna() & self.cell_ok
        toxic = (trend == 0) & (vol == 1) & (bond == 1)
        g = (ok & ~toxic).astype(int).to_numpy().copy()
        g[np.asarray(self.idx < BT_START)] = 0
        return g, trend, vol, bond

    # ---------------------------------------------------- репрайсинг ожиданий
    def es_bit(self, thr=0.25, win=21, key="eff"):
        k = self.key_eff if key == "eff" else self.key_dec
        spread = self.y1 - k
        d = spread - spread.shift(win)
        es = (d > thr).astype(int).to_numpy().copy()
        es[~np.isfinite(d.to_numpy())] = 0
        return es

    # ---------------------------------------------------- ряд композита для решения
    def comp_values(self, src):
        return (self.comp_closed if src == "closed" else self.comp_live).values.astype(float)


# ============================================================ автомат позиции
def run_engine(mk, cfg):
    """cfg: comp ('closed'|'live'), comp_thr, comp_dec ('daily'|'W-FRI'|...), comp_hyst ('sampled'|'daily'),
    bits (dict), es (None|dict(thr,win,key)), es_reentry ('dec'|'any'), gate_entry ('any'|'dec'),
    stab (None|dict(dd=-0.15, on=0.0, off=-0.02, need_comp=True)).
    Возвращает DataFrame: pos (0/1 по закрытию t), reason (строка в день смены), comp_state, gate, es, stab."""
    n = mk.n
    dec = mk.decision_mask(cfg.get("comp_dec", "daily"))
    v = mk.comp_values(cfg.get("comp", "closed"))
    thr = cfg.get("comp_thr", 0.10)
    if cfg.get("comp_hyst", "sampled") == "daily":
        st_daily = hyst_states(v, thr)   # бегущее состояние по дневному ряду, читается в дни решения
    gate, trend, vol, bond = mk.gate(cfg.get("bits", {}))
    es_cfg = cfg.get("es")
    if cfg.get("es_bit") is not None:          # плацебо: готовый бит вместо расчётного
        es = np.asarray(cfg["es_bit"], dtype=int); es_cfg = es_cfg or {"placebo": True}
    else:
        es = mk.es_bit(**es_cfg) if es_cfg else np.zeros(n, dtype=int)
    es_re_any = cfg.get("es_reentry", "dec") == "any"
    gate_any = cfg.get("gate_entry", "any") == "any"
    gate_exit_any = cfg.get("gate_exit", "any") == "any"   # выход по воротам любым днём / только в день решения
    es_exit_any = cfg.get("es_exit", "any") == "any"       # выход по es любым днём / только в день решения
    stab_cfg = cfg.get("stab")
    dd = mk.dd252.values; r21 = mk.ret21.values
    start_i = int(np.searchsorted(mk.idx.values, BT_START.to_datetime64()))

    pos = np.zeros(n, dtype=float)
    reason = np.full(n, "", dtype=object)
    comp_state_arr = np.zeros(n, dtype=int); stab_arr = np.zeros(n, dtype=int)
    comp_state = 0; es_block = False; stab_state = 0; cur = 0.0
    for t in range(n):
        if dec[t] and np.isfinite(v[t]):
            if cfg.get("comp_hyst", "sampled") == "daily":
                comp_state = int(st_daily[t])
            else:
                if v[t] > thr: comp_state = 1
                elif v[t] < -thr: comp_state = -1
        if stab_cfg and dec[t] and np.isfinite(dd[t]) and np.isfinite(r21[t]):
            if stab_state == 0 and dd[t] < stab_cfg.get("dd", -0.15) and r21[t] > stab_cfg.get("on", 0.0):
                stab_state = 1
            elif stab_state == 1 and (r21[t] < stab_cfg.get("off", -0.02)):
                stab_state = 0
        if es[t] == 1:
            es_block = True
        elif es_block and (es_re_any or dec[t]):
            es_block = False
        comp_state_arr[t] = comp_state; stab_arr[t] = stab_state
        if t < start_i:
            continue
        comp_ok = comp_state == 1 or (stab_cfg and stab_state == 1 and not stab_cfg.get("need_comp", True))
        g_ok = gate[t] == 1 or (stab_cfg and stab_state == 1)
        want = bool(g_ok and comp_ok and not es_block)
        stab_prev = stab_arr[t - 1] if t > 0 else 0
        if cur == 1.0:
            if not want:
                c_es = es[t] == 1 or es_block; c_gate = not g_ok; c_comp = not comp_ok
                allowed = dec[t] or (c_gate and gate_exit_any) or (c_es and es_exit_any)
                if allowed:
                    if c_es and es[t] == 1 and (es_exit_any or dec[t]): why = "es_exit"
                    elif c_gate and (gate_exit_any or dec[t]): why = "stab_off" if (stab_cfg and stab_prev == 1 and stab_state == 0) else "gate_close"
                    elif c_comp: why = "comp_neg"
                    else: why = "es_block"
                    cur = 0.0; reason[t] = why
        else:
            if want and (dec[t] or gate_any):
                if not es_block and es_cfg and t > 0 and reason_last_es(reason, t):
                    why = "es_clear"
                elif gate[t] == 1 and t > 0 and gate[t - 1] == 0: why = "gate_open"
                elif gate[t] == 0 and stab_state == 1: why = "stab_on"
                elif comp_state == 1 and comp_state_arr[t - 1] != 1: why = "comp_pos"
                else: why = "entry"
                cur = 1.0; reason[t] = why
        pos[t] = cur
    return pd.DataFrame({"pos": pos, "reason": reason, "comp_state": comp_state_arr, "gate": gate,
                         "es": es, "stab": stab_arr, "trend": trend.values, "vol": vol.values, "bond": bond.values},
                        index=mk.idx)


def reason_last_es(reason, t):
    """Последняя смена позиции до t была выходом по es?"""
    for j in range(t - 1, max(-1, t - 400), -1):
        if reason[j]:
            return reason[j] == "es_exit"
    return False


def run_engine_veto(mk, cfg):
    """Расширение run_engine: вето-бит (P7). cfg['veto_bit'] — готовый 0/1 массив; cfg['veto_mode']:
    'dec' — вето читается в дни решения композита, 'any' — ежедневно. Вето = и выход, и запрет входа
    (как veto у D1). Без veto_bit — обычный run_engine."""
    cfg = dict(cfg)
    vb = cfg.pop("veto_bit", None); vm = cfg.pop("veto_mode", "dec")
    if vb is None:
        return run_engine(mk, cfg)
    eng0 = run_engine(mk, cfg)          # без вето — comp_state, gate, es, stab уже посчитаны
    dec = mk.decision_mask(cfg.get("comp_dec", "daily"))
    n = mk.n
    gate = eng0["gate"].values; cs = eng0["comp_state"].values; es = eng0["es"].values; stab = eng0["stab"].values
    es_re_any = cfg.get("es_reentry", "dec") == "any"; gate_any = cfg.get("gate_entry", "any") == "any"
    gate_exit_any = cfg.get("gate_exit", "any") == "any"; es_exit_any = cfg.get("es_exit", "any") == "any"
    stab_cfg = cfg.get("stab")
    start_i = int(np.searchsorted(mk.idx.values, BT_START.to_datetime64()))
    pos = np.zeros(n); reason = np.full(n, "", dtype=object)
    cur = 0.0; es_block = False; veto_state = 0
    for t in range(n):
        if es[t] == 1:
            es_block = True
        elif es_block and (es_re_any or dec[t]):
            es_block = False
        if vm == "any" or dec[t]:
            veto_state = int(vb[t])
        if t < start_i:
            continue
        comp_ok = cs[t] == 1 or (stab_cfg and stab[t] == 1 and not stab_cfg.get("need_comp", True))
        g_ok = gate[t] == 1 or (stab_cfg and stab[t] == 1)
        want = bool(g_ok and comp_ok and not es_block and veto_state == 0)
        stab_prev = stab[t - 1] if t > 0 else 0
        if cur == 1.0:
            if not want:
                c_es = es[t] == 1 or es_block; c_gate = not g_ok; c_comp = not comp_ok; c_veto = veto_state == 1
                allowed = dec[t] or (c_gate and gate_exit_any) or (c_es and es_exit_any) or (c_veto and vm == "any")
                if allowed:
                    if c_veto and not (c_gate or c_es or c_comp): why = "veto_exit"
                    elif c_es and es[t] == 1 and (es_exit_any or dec[t]): why = "es_exit"
                    elif c_gate and (gate_exit_any or dec[t]): why = "stab_off" if (stab_cfg and stab_prev == 1 and stab[t] == 0) else "gate_close"
                    elif c_comp: why = "comp_neg"
                    elif c_veto: why = "veto_exit"
                    else: why = "es_block"
                    cur = 0.0; reason[t] = why
        else:
            if want and (dec[t] or gate_any):
                if t > 0 and gate[t] == 1 and gate[t - 1] == 0: why = "gate_open"
                elif gate[t] == 0 and stab[t] == 1: why = "stab_on"
                elif cs[t] == 1 and cs[t - 1] != 1: why = "comp_pos"
                elif reason_last_es(reason, t): why = "es_clear"
                else: why = "entry"
                cur = 1.0; reason[t] = why
        pos[t] = cur
    eng0["pos"] = pos; eng0["reason"] = reason; eng0["veto"] = vb
    return eng0


def sample_hold(mk, bit, kind):
    """Бит читается только в дни decision_mask(kind) и держится до следующего чтения."""
    m = mk.decision_mask(kind)
    return pd.Series(np.where(m, bit, np.nan), index=mk.idx).ffill().fillna(0).astype(int).values


def run_cfg(mk, cfg):
    """Полная конфигурация: + veto=dict(thr, lag, mode='dec'|'any'|'dec_monthly'), overlay='staged'|'trail',
    es_sample='monthly'|'W-FRI'|… (бит репрайсинга читается по расписанию и держится)."""
    base = {k: v for k, v in cfg.items() if k not in ("veto", "overlay", "es_sample")}
    if cfg.get("es_sample") and cfg.get("es") and base.get("es_bit") is None:
        base["es_bit"] = sample_hold(mk, mk.es_bit(**cfg["es"]), cfg["es_sample"])
    veto = cfg.get("veto")
    if veto:
        vb = mk.veto_bit(veto.get("thr", 0.10), veto.get("lag", 0))
        mode = veto.get("mode", "dec")
        if mode == "dec_monthly":   # вето читается на конце месяца и держится месяц (как у D1)
            me = mk.decision_mask("monthly")
            vb = pd.Series(np.where(me, vb, np.nan), index=mk.idx).ffill().fillna(0).astype(int).values
            mode = "any"
        base = dict(base, veto_bit=vb, veto_mode=mode)
    eng = run_engine_veto(mk, base)
    if cfg.get("overlay") == "staged":
        eng["pos"] = overlay_staged(eng["pos"], 21, 0.5)
    elif cfg.get("overlay") == "trail":
        p2, ntr = overlay_trail(eng["pos"], mk.mcftr, 0.08, 21)
        eng.loc[(eng["pos"] > 0) & (p2 == 0) & (eng["pos"].shift(1) > 0), "reason"] = "trail_stop"
        eng["pos"] = p2
    return eng


# ============================================================ оверлеи (P6, справочно)
def overlay_staged(pos, days=21, frac=0.5):
    v = pos.values; out = np.zeros(len(v)); age = 10 ** 6
    for i in range(len(v)):
        if v[i] > 0:
            age = 0 if (i == 0 or v[i - 1] == 0) else age + 1
            out[i] = frac if age < days else 1.0
        else:
            out[i] = 0.0
    return pd.Series(out, index=pos.index)


def overlay_trail(pos, price, x=0.08, cooldown=21):
    """Трейлинг-стоп x от максимума MCFTR с момента входа; после срабатывания — флэт cooldown дней,
    затем снова следуем базовому правилу (как F2_risk.overlay)."""
    s = pos.values; p = price.values; out = np.zeros(len(s)); n_trig = 0
    in_pos = 0.0; peak = np.nan; cd = 0
    for i in range(len(s)):
        want = s[i]
        if cd > 0:
            cd -= 1; want = 0.0
        if in_pos == 0 and want == 1:
            peak = p[i]
        elif in_pos == 1 and want == 1:
            peak = max(peak, p[i]) if np.isfinite(p[i]) else peak
            if np.isfinite(p[i]) and p[i] / peak - 1 < -x:
                want = 0.0; cd = cooldown; n_trig += 1
        out[i] = want; in_pos = want
    return pd.Series(out, index=pos.index), n_trig


# ============================================================ бэктест и метрики
def backtest(pos, mk, cost=COST, exec_lag=1):
    """pos — позиция по закрытию t (0…1). exec_lag=1: действует с закрытия t+1."""
    p = pos.shift(exec_lag).fillna(0.0) if exec_lag else pos.astype(float)
    p_prev = p.shift(1).fillna(0.0)
    r_eq = mk.ret_eq.fillna(0.0); r_mm = mk.ret_mm.fillna(0.0)
    ret = p_prev * r_eq + (1 - p_prev) * r_mm
    turn = (p - p_prev).abs()
    nav = ((1 + ret) * (1 - cost * turn)).cumprod()
    return pd.DataFrame({"p": p, "ret": ret, "turn": turn, "nav": nav})


def slice_nav(nav, s, e):
    idx = nav.index
    before = idx[idx < pd.Timestamp(s)]
    base_i = before[-1] if len(before) else idx[0]
    w = nav.loc[base_i:e]
    return w / w.iloc[0]


def sharpe_m(x):
    return x.mean() / x.std() * np.sqrt(12) if x.std() > 0 else np.nan


def monthly_logret(nav):
    return np.log(nav.resample("ME").last()).diff().dropna()


def metrics(bt, bh_bt, mm_bt, s, e, ex2022=False):
    nav = slice_nav(bt["nav"], s, e); nav_bh = slice_nav(bh_bt["nav"], s, e); nav_mm = slice_nav(mm_bt["nav"], s, e)
    p = bt["p"].loc[nav.index[0]:nav.index[-1]]
    trades = bt["turn"].loc[nav.index[1]:nav.index[-1]]
    if ex2022:
        keep = nav.index.year != 2022
        r = nav.pct_change().fillna(0)[keep]; rb = nav_bh.pct_change().fillna(0)[keep]; rm = nav_mm.pct_change().fillna(0)[keep]
        nav = (1 + r).cumprod(); nav_bh = (1 + rb).cumprod(); nav_mm = (1 + rm).cumprod()
        p = p[p.index.year != 2022]; trades = trades[trades.index.year != 2022]
    years = (nav.index[-1] - nav.index[0]).days / 365.25 - (1.0 if ex2022 else 0.0)
    cagr = nav.iloc[-1] ** (1 / years) - 1
    cagr_bh = nav_bh.iloc[-1] ** (1 / years) - 1
    cagr_mm = nav_mm.iloc[-1] ** (1 / years) - 1
    rm_ = monthly_logret(nav); rb_ = monthly_logret(nav_bh); rmm_ = monthly_logret(nav_mm)
    vol = rm_.std() * np.sqrt(12)
    dd = nav / nav.cummax() - 1
    mdd = dd.min(); mdd_date = dd.idxmin()
    ddb = nav_bh / nav_bh.cummax() - 1
    yr = nav.groupby(nav.index.year).last(); yr_bh = nav_bh.groupby(nav_bh.index.year).last()
    yr_ret = yr.pct_change().dropna(); yr_ret_bh = yr_bh.pct_change().dropna()
    beat = float((yr_ret > yr_ret_bh).mean()) if len(yr_ret) else np.nan
    pm = p.resample("ME").mean().reindex(rm_.index)
    right = np.where(pm >= 0.5, rm_ > rmm_, rm_ > rb_)
    return dict(cagr=cagr, vol=vol, sharpe=sharpe_m(rm_), sharpe_ex_mm=sharpe_m(rm_ - rmm_), mdd=mdd,
                mdd_date=str(mdd_date.date()), time_in_mkt=float(p.mean()), trades_per_year=float(trades.sum() / years),
                years_beat_bh=beat, hit_month=float(np.mean(right)), n_months=len(rm_),
                cagr_bh=cagr_bh, sharpe_bh=sharpe_m(rb_), mdd_bh=float(ddb.min()), cagr_mm=cagr_mm, years=years)


def all_windows(bt, bh_bt, mm_bt):
    out = {}
    for wn, (s, e) in WINDOWS.items():
        out[wn] = metrics(bt, bh_bt, mm_bt, s, e, ex2022=wn.startswith("ex2022"))
    return out


# ============================================================ бутстреп ΔШарпа
def stat_boot_indices(n, mean_block, rng):
    p = 1.0 / mean_block
    starts = rng.integers(0, n, size=n)
    jumps = rng.random(n) < p
    jumps[0] = True
    idx = np.empty(n, dtype=int)
    cur = starts[0]
    for t in range(n):
        if jumps[t]:
            cur = starts[t]
        else:
            cur = (cur + 1) % n
        idx[t] = cur
    return idx


def sharpe_diff_boot(ra, rb, rmm=None, n_boot=2000, block=12, seed=11):
    """ΔШарп(a−b) по месячным лог-доходностям; парная стационарная перевыборка.
    Возвращает dict: delta, ci05, ci95, p_le0 (одностор.), и то же для избыточных над ММ (если rmm задан)."""
    df = pd.concat([ra.rename("a"), rb.rename("b")] + ([rmm.rename("m")] if rmm is not None else []), axis=1).dropna()
    a = df["a"].values; b = df["b"].values; n = len(a)
    m = df["m"].values if rmm is not None else None
    rng = np.random.default_rng(seed)

    def sh(x):
        s = x.std(ddof=1)
        return x.mean() / s * np.sqrt(12) if s > 0 else 0.0

    obs = sh(a) - sh(b)
    obs_ex = (sh(a - m) - sh(b - m)) if m is not None else np.nan
    diffs = np.empty(n_boot); diffs_ex = np.empty(n_boot)
    for k in range(n_boot):
        ix = stat_boot_indices(n, block, rng)
        diffs[k] = sh(a[ix]) - sh(b[ix])
        if m is not None:
            diffs_ex[k] = sh(a[ix] - m[ix]) - sh(b[ix] - m[ix])
    out = dict(n=n, delta=obs, ci05=float(np.percentile(diffs, 5)), ci95=float(np.percentile(diffs, 95)),
               p_le0=float((diffs <= 0).mean()))
    if m is not None:
        out.update(delta_ex=obs_ex, ci05_ex=float(np.percentile(diffs_ex, 5)), ci95_ex=float(np.percentile(diffs_ex, 95)),
                   p_le0_ex=float((diffs_ex <= 0).mean()))
    return out


# ============================================================ своевременность (правило 7): зигзаг 15 % по IMOEX
def zigzag_episodes(px, thr=0.15):
    n = len(px); eps = []; mode = "up"; peak = 0; trough = None
    for i in range(n):
        if mode == "up":
            if px[i] > px[peak]:
                peak = i
            elif px[i] <= px[peak] * (1 - thr):
                mode = "down"; trough = i
        else:
            if px[i] < px[trough]:
                trough = i
            elif px[i] >= px[trough] * (1 + thr):
                eps.append([peak, trough]); mode = "up"; peak = i
    open_ep = (mode == "down")
    if open_ep:
        eps.append([peak, trough])
    for k, e in enumerate(eps):
        nxt = eps[k + 1][0] if k + 1 < len(eps) else (n - 1 if (open_ep and k == len(eps) - 1) else peak)
        e.append(nxt)
    return eps


def _run_start(pos, j):
    v = pos[j]; k = j
    while k - 1 >= 0 and pos[k - 1] == v:
        k -= 1
    return k


def _share(pos_eff, a, b, arr):
    if b <= a:
        return np.nan
    tot = arr[a + 1:b + 1].sum(); borne = (pos_eff[a + 1:b + 1] * arr[a + 1:b + 1]).sum()
    return 1 - borne / tot if abs(tot) > 1e-12 else np.nan


def timeliness(pos, mk, eps, exec_lag=1):
    """pos — Series 0/1 (или доля) по закрытию t. Лаги в торговых днях от дня решения."""
    idx = mk.idx; n = mk.n
    p01 = (pos.values > 0).astype(int)
    rl = mk.logret_eq.fillna(0).values
    eff = np.roll(pos.values, 1 + exec_lag); eff[:1 + exec_lag] = 0
    rows = []
    for k, (Pk, Tk, Rk) in enumerate(eps):
        depth = mk.imoex.values[Tk] / mk.imoex.values[Pk] - 1
        if p01[Pk] == 1:
            ex = next((j for j in range(Pk + 1, Rk + 1) if p01[j] == 0), None)
            exit_lag = (ex - Pk) if ex is not None else np.nan
            exit_note = "не выходила" if ex is None else ("выход после дна" if ex > Tk else "выход до дна")
            exit_date = str(idx[ex].date()) if ex is not None else ""
        else:
            j0 = _run_start(p01, Pk); exit_lag = -(Pk - j0); exit_note = "флэт ещё до пика"; exit_date = str(idx[j0].date())
        if p01[Tk] == 0:
            en = next((j for j in range(Tk + 1, n) if p01[j] == 1), None)
            entry_lag = (en - Tk) if en is not None else np.nan
            entry_note = "не вошла" if en is None else ("вход после след. пика" if en > Rk else "вход до след. пика")
            entry_date = str(idx[en].date()) if en is not None else ""
        else:
            j1 = _run_start(p01, Tk); entry_lag = -(Tk - j1)
            entry_note = "не выходила" if j1 <= Pk else "лонг ещё до дна"; entry_date = str(idx[j1].date())
        e63 = min(Tk + 63, n - 1)
        rows.append(dict(ep=k + 1, peak=str(idx[Pk].date()), trough=str(idx[Tk].date()), next_peak=str(idx[Rk].date()),
                         depth=depth, fall_days=Tk - Pk, pos_at_peak=int(p01[Pk]), exit_lag_days=exit_lag, exit_date=exit_date,
                         exit_note=exit_note, avoided_share=_share(eff, Pk, Tk, rl), bh_fall=np.exp(rl[Pk + 1:Tk + 1].sum()) - 1,
                         pos_at_trough=int(p01[Tk]), entry_lag_days=entry_lag, entry_date=entry_date, entry_note=entry_note,
                         missed_63=_share(eff, Tk, e63, rl), missed_to_next_peak=_share(eff, Tk, Rk, rl),
                         bh_63=np.exp(rl[Tk + 1:e63 + 1].sum()) - 1, bh_to_next_peak=np.exp(rl[Tk + 1:Rk + 1].sum()) - 1,
                         tim_fall=eff[Pk + 1:Tk + 1].mean()))
    return pd.DataFrame(rows)


def timeliness_summary(E):
    lp = E[E.pos_at_peak == 1]; exited = lp[lp.exit_lag_days.notna()]
    fl = E[E.pos_at_trough == 0]
    return dict(n_episodes=len(E), n_long_at_peak=len(lp), n_exited_before_trough=int((lp.exit_note == "выход до дна").sum()),
                n_never_exited=int((lp.exit_note == "не выходила").sum()), n_flat_before_peak=int((E.pos_at_peak == 0).sum()),
                exit_lag_mean=exited.exit_lag_days.mean(), exit_lag_median=exited.exit_lag_days.median(),
                avoided_mean=E.avoided_share.mean(), avoided_median=E.avoided_share.median(),
                n_flat_at_trough=len(fl), entry_lag_mean=fl.entry_lag_days.mean(), entry_lag_median=fl.entry_lag_days.median(),
                n_never_entered=int((fl.entry_note == "не вошла").sum()),
                missed63_mean=E.missed_63.mean(), missed63_median=E.missed_63.median(),
                missed_to_next_peak_mean=E.missed_to_next_peak.mean())


# ============================================================ переключения
def switches(eng, exec_lag=1):
    r = eng[eng.reason != ""]
    out = []
    for d, row in r.iterrows():
        i = eng.index.get_loc(d)
        exec_d = eng.index[min(i + exec_lag, len(eng) - 1)]
        out.append(dict(signal_date=str(d.date()), exec_date=str(exec_d.date()), to="LONG" if row.pos == 1 else "FLAT", reason=row.reason))
    return pd.DataFrame(out)


REASON_RU = {"gate_close": "ворота закрылись (токсичная ячейка)", "es_exit": "репрайсинг ожиданий (y1−ключ +25 б.п./21 дн)",
             "comp_neg": "знак композита стал −", "stab_off": "стабилизация снята", "es_block": "ждём снятия репрайсинга",
             "gate_open": "ворота открылись", "comp_pos": "знак композита стал +", "es_clear": "репрайсинг снят (день решения)",
             "stab_on": "стабилизация (dd252<−15 % и ret21>0)", "entry": "вход (все условия)", "trail_stop": "трейлинг-стоп",
             "veto_exit": "вето по обороту (распределительные дни)"}


def random_runs_bit(real_bit, avail_mask, rng):
    """Плацебо-бит: та же доля и то же распределение длин серий (вкл/выкл), что у real_bit внутри avail_mask;
    вне окна доступности — 0."""
    b = np.asarray(real_bit, dtype=int); av = np.asarray(avail_mask, dtype=bool)
    ii = np.where(av)[0]
    seg = b[ii]
    # серии
    runs = []; cur = seg[0]; L = 0
    for v in seg:
        if v == cur: L += 1
        else: runs.append((cur, L)); cur = v; L = 1
    runs.append((cur, L))
    on = np.array([L for v, L in runs if v == 1]); off = np.array([L for v, L in runs if v == 0])
    out = np.zeros(len(b), dtype=int)
    if len(on) == 0 or len(off) == 0:
        return out
    fake = []; state = 0
    while len(fake) < len(seg):
        L = int(rng.choice(off) if state == 0 else rng.choice(on))
        fake.extend([state] * L); state = 1 - state
    out[ii] = np.array(fake[:len(seg)])
    return out
