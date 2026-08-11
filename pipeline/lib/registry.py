"""Реестр рядов: id → источник, темп, лаг доступности, SLA, роль.

pub_lag_days — сколько суток прибавить к дате ПЕРИОДА, чтобы получить дату
ДОСТУПНОСТИ (когда значение реально можно было увидеть). Для дневных рыночных
рядов = 0 (значение известно на закрытии). Для месячных публикаций — окно выхода.
Эти лаги повторяют те, что использовались в валидации, — менять нельзя без
пересчёта validation/.

poll_window — (день_с, день_по) числа месяца, в которые пайплайн опрашивает
источник «до появления» (для месячных релизов).
"""

DAILY_MARKET = "iss_daily"
INTRADAY = "iss_intraday"

SERIES = {
    # ---------------------------------------------------------- рыночные (ISS)
    "imoex": dict(fetcher="iss.index", args={"sec": "IMOEX"}, cadence="daily",
                  pub_lag_days=0, sla="iss_daily", required=True, role="core",
                  label="Индекс МосБиржи"),
    "imoex_value": dict(fetcher="iss.index_value", args={"sec": "IMOEX"}, cadence="daily",
                        pub_lag_days=0, sla="iss_daily", required=False, role="state",
                        label="Оборот индекса"),
    "imoex2": dict(fetcher="iss.index", args={"sec": "IMOEX2"}, cadence="daily",
                   pub_lag_days=0, sla="iss_daily", required=False, role="monitor",
                   label="IMOEX2 (вкл. выходные сессии)"),
    "mcftr": dict(fetcher="iss.index", args={"sec": "MCFTR"}, cadence="daily",
                  pub_lag_days=0, sla="iss_daily", required=True, role="signal",
                  label="Индекс полной доходности"),
    "rgbi": dict(fetcher="iss.index", args={"sec": "RGBI"}, cadence="daily",
                 pub_lag_days=0, sla="iss_daily", required=True, role="state",
                 label="Индекс гособлигаций"),
    "rvi": dict(fetcher="iss.index", args={"sec": "RVI"}, cadence="daily",
                pub_lag_days=0, sla="iss_daily", required=False, role="monitor",
                label="Индекс волатильности"),
    "mcxsm": dict(fetcher="iss.index", args={"sec": "MCXSM"}, cadence="daily",
                  pub_lag_days=0, sla="iss_daily", required=False, role="monitor",
                  label="Индекс малой и средней капитализации"),
    "rtsi": dict(fetcher="iss.index", args={"sec": "RTSI"}, cadence="daily",
                 pub_lag_days=0, sla="iss_daily", required=False, role="signal",
                 label="Индекс РТС"),
    "rusfar3m": dict(fetcher="iss.index", args={"sec": "RUSFAR3M"}, cadence="daily",
                     pub_lag_days=0, sla="iss_daily", required=False, role="monitor",
                     label="RUSFAR 3M"),
    "rucbhycp_yield": dict(fetcher="iss.index_yield", args={"sec": "RUCBHYCP"}, cadence="daily",
                           pub_lag_days=0, sla="iss_daily", required=False, role="monitor",
                           label="Доходность ВДО"),
    "rucbcpns_yield": dict(fetcher="iss.index_yield", args={"sec": "RUCBCPNS"}, cadence="daily",
                           pub_lag_days=0, sla="iss_daily", required=False, role="monitor",
                           label="Доходность корпоблигаций"),
    "cny_tom": dict(fetcher="iss.selt", args={"sec": "CNYRUB_TOM"}, cadence="daily",
                    pub_lag_days=0, sla="iss_daily", required=False, role="signal",
                    label="Юань/рубль (биржевой)"),
    "gld_tom": dict(fetcher="iss.selt", args={"sec": "GLDRUB_TOM"}, cadence="daily",
                    pub_lag_days=0, sla="iss_daily", required=False, role="monitor",
                    label="Золото в рублях"),
    "zcyc": dict(fetcher="iss.zcyc", args={}, cadence="daily", pub_lag_days=0,
                 sla="iss_daily", required=True, role="core",
                 label="Кривая бескупонной доходности ОФЗ",
                 subkeys=["y0.5", "y1.0", "y2.0", "y5.0", "y10.0"]),
    "futoi_mx": dict(fetcher="iss.futoi", args={"ticker": "MX"}, cadence="daily",
                     pub_lag_days=0, sla="iss_daily", required=False, role="monitor",
                     label="Открытые позиции физлиц (фьючерс на индекс)",
                     note="бесплатный ISS отдаёт с задержкой 14 дней; свежий срез — скрейп "
                          "moex.com/ru/derivatives/open-positions.aspx"),
    "breadth": dict(fetcher="iss.breadth", args={}, cadence="daily", pub_lag_days=0,
                    sla="iss_daily", required=False, role="monitor",
                    label="Доля бумаг выше 200-дневной"),

    # ------------------------------------------------------------------- ЦБ РФ
    "usd_cbr": dict(fetcher="cbr.fx", args={"code": "R01235"}, cadence="daily",
                    pub_lag_days=0, sla="cbr_daily", required=True, role="core",
                    label="Официальный курс USD",
                    note="курс публикуется днём и применяется со следующего дня; "
                         "храним по дате ПРИМЕНЕНИЯ (как в валидации)"),
    "cny_cbr": dict(fetcher="cbr.fx", args={"code": "R01375"}, cadence="daily",
                    pub_lag_days=0, sla="cbr_daily", required=False, role="monitor",
                    label="Официальный курс CNY"),
    "key_rate": dict(fetcher="cbr.keyrate", args={}, cadence="event", pub_lag_days=0,
                     sla="cbr_daily", required=True, role="state", label="Ключевая ставка"),
    "deposit_decade": dict(fetcher="cbr.deposit", args={}, cadence="decade", pub_lag_days=4,
                           sla="cbr_decade", required=False, role="signal",
                           label="Макс. ставка по вкладам топ-10 банков"),

    # -------------------------------------------------------------- внешние
    "brent": dict(fetcher="external.brent_fred", args={}, cadence="daily", pub_lag_days=0,
                  sla="iss_daily", required=False, role="signal", label="Brent",
                  note="FRED отдаёт с лагом 3–7 дней; интрадей-прокси — фьючерс BR на ISS"),
    "brent_moex": dict(fetcher="iss.futures_br", args={}, cadence="daily", pub_lag_days=0,
                       sla="iss_daily", required=False, role="signal",
                       label="Brent (фьючерс BR на МосБирже)"),

    # ------------------------------------------------------------------ Минфин
    "urals_tax": dict(fetcher="minfin.urals", args={}, cadence="monthly", pub_lag_days=5,
                      poll_window=(1, 8), sla="minfin_monthly", required=False, role="core",
                      label="Налоговая цена Urals"),
    "ofz_auctions": dict(fetcher="minfin.auctions", args={}, cadence="weekly", pub_lag_days=0,
                         sla="minfin_monthly", required=False, role="monitor",
                         label="Аукционы ОФЗ"),
    "ngd": dict(fetcher="minfin.ngd", args={}, cadence="monthly", pub_lag_days=5,
                poll_window=(3, 10), sla="minfin_monthly", required=False, role="monitor",
                label="Нефтегазовые доходы и операции по бюджетному правилу"),
    "budget_deficit": dict(fetcher="minfin.budget", args={}, cadence="monthly", pub_lag_days=12,
                           poll_window=(9, 16), sla="minfin_monthly", required=False,
                           role="monitor", label="Исполнение федерального бюджета"),
    "fnb": dict(fetcher="minfin.fnb", args={}, cadence="monthly", pub_lag_days=10,
                poll_window=(3, 14), sla="minfin_monthly", required=False, role="monitor",
                label="Ликвидная часть ФНБ"),

    # ----------------------------------------------------------------- Росстат
    "cpi_weekly": dict(fetcher="rosstat.cpi_weekly", args={}, cadence="weekly", pub_lag_days=2,
                       sla="rosstat_weekly", required=False, role="monitor",
                       label="Недельная инфляция",
                       note="для акций предиктивность ОПРОВЕРГНУТА (tier dead); "
                            "держим как вход ожиданий ставки"),
    "cpi_monthly": dict(fetcher="rosstat.cpi_monthly", args={}, cadence="monthly",
                        pub_lag_days=13, poll_window=(9, 18), sla="minfin_monthly",
                        required=False, role="monitor", label="Месячная инфляция"),

    # ------------------------------------------------------------------ потоки
    "orfr_flows": dict(fetcher="orfr.flows", args={}, cadence="monthly", pub_lag_days=15,
                       poll_window=(5, 17), sla="orfr_monthly", required=False, role="monitor",
                       label="Нетто-покупки акций по категориям (ОРФР ЦБ)",
                       subkeys=["fiz", "nfo_du", "nfo_own", "szko", "other_banks", "nonres"],
                       note="парсер PDF; при провале — ручной ввод в inputs/orfr.yml"),
    "lqdt_aum": dict(fetcher="investfunds.money_funds", args={}, cadence="daily", pub_lag_days=1,
                     sla="investfunds_daily", required=False, role="monitor",
                     label="СЧА фондов денежного рынка"),
    "moex_retail": dict(fetcher="moex_press.retail", args={}, cadence="monthly", pub_lag_days=10,
                        poll_window=(5, 14), sla="orfr_monthly", required=False, role="monitor",
                        label="Частные инвесторы: клиенты и народный портфель"),

    # ------------------------------------------------------------- предсказания
    "polymarket_ceasefire": dict(fetcher="polymarket.ceasefire", args={}, cadence="daily",
                                 pub_lag_days=0, sla="polymarket", required=False,
                                 role="monitor", label="Вероятность перемирия (Polymarket)"),

    # -------------------------------------------------- ручные вводы (inputs/)
    "cb_consensus": dict(fetcher="manual.consensus", args={}, cadence="event", pub_lag_days=0,
                         sla=None, required=False, role="monitor",
                         label="Консенсус по ставке перед заседанием",
                         note="inputs/consensus.yml"),
    "events_registry": dict(fetcher="manual.events", args={}, cadence="event", pub_lag_days=0,
                            sla=None, required=False, role="monitor",
                            label="Реестр событий (налоги/санкции/переговоры)",
                            note="inputs/events.yml"),
    "dividends": dict(fetcher="manual.dividends", args={}, cadence="event", pub_lag_days=0,
                      sla=None, required=False, role="monitor",
                      label="Дивидендный календарь", note="inputs/dividends.yml"),
}

# Что тянет каждый режим прогона.
MODES = {
    "intraday": ["imoex", "imoex2", "rgbi", "rvi", "cny_tom", "gld_tom", "brent_moex"],
    "daily": [k for k, v in SERIES.items()
              if v["cadence"] in ("daily", "event") and not v["fetcher"].startswith("manual")],
    "weekly": ["cpi_weekly", "ofz_auctions", "imoex2"],
    "monthly": [k for k, v in SERIES.items() if v["cadence"] in ("monthly", "decade")],
    "manual": [k for k, v in SERIES.items() if v["fetcher"].startswith("manual")],
}


def series_for_mode(mode):
    ids = MODES.get(mode, [])
    return [(sid, SERIES[sid]) for sid in ids if sid in SERIES]


def poll_due(spec, day_of_month):
    """Нужно ли сегодня опрашивать месячный источник."""
    win = spec.get("poll_window")
    if not win:
        return True
    return win[0] <= day_of_month <= win[1]
