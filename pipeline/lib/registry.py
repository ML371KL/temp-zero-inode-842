"""Реестр рядов: id → источник, темп, лаг доступности, SLA, роль.

pub_lag_days — сколько суток прибавить к дате ПЕРИОДА, чтобы получить дату
ДОСТУПНОСТИ (когда значение реально можно было увидеть). Для дневных рыночных
рядов = 0 (значение известно на закрытии). Для месячных публикаций — окно выхода.
Эти лаги повторяют те, что использовались в валидации, — менять нельзя без
пересчёта validation/.

poll_window — (день_с, день_по) числа месяца, в которые пайплайн опрашивает
источник «до появления» (для месячных релизов).
"""

# ------------------------------------------------- норма ВОЗРАСТА ДАННЫХ
# Свежесть до 26.08.2026 мерилась ТОЛЬКО от fetched_at — времени последнего
# ОПРОСА. Для источника, который отвечает исправно, но отдаёт старое, эта проверка
# не срабатывает никогда: FRED каждый день бодро возвращает Brent недельной
# давности, опрос свежий, статус ok. Единственным, что вообще реагировало на
# возраст ДАННЫХ, была протяжка в панели (panel.FFILL_LIMITS) — и та не говорила
# ни слова: значение просто исчезало, когда лимит выходил.
#
# Норма ниже отвечает на другой вопрос: сколько дней значение имеет право быть
# старым, пока панель показывает его как сегодняшнее. Числа выведены из
# измеренных тактов самих рядов (замер 26.08.2026, окно с августа 2024):
#   дневные рыночные   — медианный разрыв 1 день, 90-й перцентиль 3, максимум 6
#                        (новогодние каникулы) -> 9 с запасом;
#   недельные          — ИПЦ Росстата: медиана 7, максимум 21 (те же каникулы) -> 25;
#   декадные           — ставка ЦБ: медиана 10, максимум 11 -> 25;
#   месячные           — релиз раз в 31 день плюс окно публикации -> 75.
#
# ГЛАВНОЕ СВОЙСТВО, ради которого норма и вводится: она обязана срабатывать
# РАНЬШЕ, чем истекает протяжка. Иначе предупреждение приходит после того, как
# сигнал молча исчез, и смысла в нём нет. Для Brent: протяжка 10 торговых дней
# ≈ 14 календарных, норма 12 -> два дня форы. Проверяется тестом.
DATA_AGE_DEFAULTS = {"daily": 9, "weekly": 25, "decade": 25, "monthly": 75}

# Ряды, у которых возраст данных НЕ является дефектом.
DATA_AGE_EXEMPT = {
    # «Аукциона не было» — это информация, а не поломка источника. Минфин держит
    # паузу с 20.07.2026, ряду 42 дня, и доска биржи при этом исправна; тайл сам
    # пишет «Аукционов нет 5 нед.». Тревога здесь звала бы чинить работающее.
    "ofz_auctions",
}

# Точечные нормы там, где такт ряда не совпадает с его темпом в реестре.
DATA_AGE_DAYS = {
    # Brent объявлен дневным, но живой источник НЕДЕЛЬНЫЙ: FRED отдаёт
    # DCOILBRENTEU из EIA, а EIA публикует эту «дневную» серию раз в неделю.
    # 12 = полторы недели и одновременно меньше, чем протяжка (10 торговых дней
    # ≈ 14 календарных): предупреждение приходит, пока число ещё на экране.
    "brent": 12,
    # Ставка по вкладам: декада ЦБ выходит раз в ~10 суток (максимум разрыва за
    # историю 11), протяжка 15 торговых дней ≈ 21 календарный. 18 = 1,6 такта и
    # при этом внутри протяжки — то же правило «предупредить, пока число на
    # экране», что у Brent.
    "deposit_decade": 18,
}


def data_age_norm(series_id):
    """Сколько суток значение ряда может быть старым. None — проверять нечего."""
    if series_id in DATA_AGE_EXEMPT:
        return None
    if series_id in DATA_AGE_DAYS:
        return DATA_AGE_DAYS[series_id]
    spec = SERIES.get(series_id)
    if spec is None:
        base = max((k for k in SERIES if series_id.startswith(k)), key=len, default=None)
        if base in DATA_AGE_EXEMPT:
            return None
        if base in DATA_AGE_DAYS:
            return DATA_AGE_DAYS[base]
        spec = SERIES.get(base) or {}
    # cadence="event" (заседания ЦБ, реестр событий, консенсус) нормы не имеет:
    # там отсутствие новой точки означает, что события не было.
    return DATA_AGE_DEFAULTS.get(spec.get("cadence"))


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
                     note="без подписки ISS отдаёт с запретом на последние 14 суток; "
                          "с ключом ALGOPACK (env MOEX_ALGOPACK_TOKEN) тот же путь идёт "
                          "через apim.moex.com и приходит без задержки. Скрейпа нет: "
                          "moex.com/ru/derivatives/open-positions рисуется скриптом, "
                          "а open-positions-csv.aspx отвечает 200 и НОЛЬ байт"),
    "breadth": dict(fetcher="iss.breadth", args={}, cadence="daily", pub_lag_days=0,
                    sla="iss_daily", required=False, role="monitor",
                    label="Доля бумаг выше 200-дневной"),
    # Концентрация участников (HHI) по ВСЕМУ рынку акций — платный датасет ALGOPACK.
    # Точка дня — агрегаты равновзвешенно по бумагам, где метрика определена:
    # nf = среднее (hhi_netflow_buy − hhi_netflow_sell), bs = среднее
    # (hhi_agressive_buy − hhi_agressive_sell), n — бумаг в агрегате,
    # hhi_volume_mean. Подряды пишутся отдельными файлами hi2_market_<subkey>,
    # как zcyc -> zcyc_y1 (точка в id недопустима в имени файла и ключе R2).
    # Аудит 02.09.2026: IC nf к 21 дню +0,32 (n=73), ловит дно на 10–50 дней
    # раньше ворот — живёт ТЕНЬЮ (compute/shadow.py), в решение не входит.
    "hi2_market": dict(fetcher="algopack.hi2_market", args={}, cadence="daily",
                       pub_lag_days=0, sla="iss_daily", required=False, role="monitor",
                       label="Концентрация участников (HI2, весь рынок)",
                       subkeys=["nf", "bs", "n", "hhi_volume_mean"],
                       note="2 запроса/день к https://apim.moex.com/iss/datashop/algopack/"
                            "eq/hi2.json?date=YYYY-MM-DD (страницы по 1000 строк, 11 метрик "
                            "× ~140 бумаг, публикуется 18:46 МСК); бесплатного дублёра нет — "
                            "без ключа ALGOPACK (env MOEX_ALGOPACK_TOKEN) ряд стареет, тайл "
                            "молчит. Ретро при пустом сторе — не более 260 торговых дней"),

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
    # ngd (нефтегазовые доходы) и fnb (ликвидная часть ФНБ) УБРАНЫ из реестра
    # 12.08.2026. Причина не в источниках, а в потребителях: их нет. Проверено
    # grep по pipeline/compute/ и web/ — ни тайла, ни колонки панели, ни строки
    # фронта. Ряд, который никто не читает, — это не «контекст», а расход: он
    # ходит в сеть трижды в сутки, а его неудача красит семью источников и
    # поднимает алерт. Из-за одного fnb семья minfin вечно светилась missing.
    # Фетчеры minfin.ngd()/minfin.fnb() НЕ удалены и покрыты тестами: если для
    # них появится тайл, хватит вернуть сюда две строки.
    "urals_tax": dict(fetcher="minfin.urals", args={}, cadence="monthly", pub_lag_days=5,
                      poll_window=(1, 12), sla="minfin_monthly", required=False, role="core",
                      label="Налоговая цена Urals",
                      note="окно шире срока публикации (1–8) намеренно: первоисточник "
                           "economy.gov.ru недоступен, а зеркала СМИ держат релиз на "
                           "первой странице ленты считанные дни — лишние попытки "
                           "ничего не стоят, а пропуск месяца стоит ноги ядра"),
    "ofz_auctions": dict(fetcher="auctions.auctions", args={}, cadence="weekly",
                         pub_lag_days=0, sla="iss_daily", required=False, role="monitor",
                         label="Аукционы ОФЗ",
                         note="источник — биржевая доска PACT, а не Минфин: minfin.gov.ru "
                              "отдаёт 503 с прод-машины даже на статику. Биржа даёт "
                              "размещение и различает «провалился»/«не проводился», но НЕ "
                              "даёт спрос и не включает ДРПА (docs/SOURCES.md)"),
    "budget_deficit": dict(fetcher="minfin.budget", args={}, cadence="monthly", pub_lag_days=12,
                           poll_window=(9, 16), sla="minfin_monthly", required=False,
                           role="monitor", label="Исполнение федерального бюджета"),

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
                                 role="monitor", label="Вероятность соглашения о перемирии (Polymarket)"),

    # -------------------------------------------------- ручные вводы (inputs/)
    "cb_consensus": dict(fetcher="consensus.rate", args={}, cadence="event", pub_lag_days=0,
                         sla=None, required=False, role="monitor",
                         label="Консенсус по ставке перед заседанием",
                         note="inputs/consensus.yml"),
    "events_registry": dict(fetcher="manual.events", args={}, cadence="event", pub_lag_days=0,
                            sla=None, required=False, role="monitor",
                            label="Реестр событий (налоги/санкции/переговоры)",
                            note="inputs/events.yml"),
    "dividends": dict(fetcher="dividends.calendar", args={}, cadence="weekly",
                      pub_lag_days=0, sla=None, required=False, role="monitor",
                      label="Дивидендный календарь",
                      note="календарь T-Invest API (резерв — smart-lab), отфильтрованный "
                           "по составу IMOEX с весами (ISS analytics): веса дают "
                           "ожидаемый гэп индекса. Датасет биржи "
                           "securities/{sec}/dividends мёртв (у всех бумаг записи "
                           "кончаются 2025). inputs/dividends.yml остался резервом. "
                           "asof ряда — день ЧТЕНИЯ календаря: точки лежат в будущем, "
                           "и умолчание make_meta по последней точке здесь неверно"),
}

# ------------------------------------------------------------------- тень
# Теневые сигналы (аудит 02.09.2026, compute/shadow.py): считаются на каждом
# суточном прогоне, на позицию НЕ влияют, а история пишется в стор рядами
# shadow_<id>, чтобы через 12 месяцев наблюдения было что сверять ВНЕ выборки.
# Фетчера у них нет — точку кладёт сам расчёт (fetcher=None): режимы прогона их
# не опрашивают (cadence "derived" не входит ни в один MODES), а норма возраста
# данных на них не распространяется — отсутствие новой точки означает, что
# прогон не считал тень, и об этом скажет сам блок shadow в data.json.
SHADOW_SIGNALS = {
    "repricing": "Тень: репрайсинг ожиданий по ставке (Δ21 спреда год−ключ)",
    "usd_ma200": "Тень: курс к своей MA200 (z)",
    "brent_usd_gap": "Тень: Brent к своей MA504 в долларах (z)",
    "hi2_nf21z": "Тень: концентрация нетто-потока HI2 (z 21д)",
    "breadth_early": "Тень: ранний бит ширины рынка (<40 %)",
    "volume_capitulation": "Тень: капитуляция по обороту",
    "futoi_gross": "Тень: брутто-вовлечённость физлиц в MX",
    "dividend_season": "Тень: дивидендный сезон (ожидаемый гэп индекса)",
    "rotation_trigger": "Тень: большая ротация из фондов ликвидности",
    "position": "Тень: позиция с битом репрайсинга",
}
for _sid, _label in SHADOW_SIGNALS.items():
    SERIES[f"shadow_{_sid}"] = dict(fetcher=None, args={}, cadence="derived",
                                    pub_lag_days=0, sla=None, required=False,
                                    role="shadow", label=_label,
                                    note="пишется compute/shadow.py, источника нет")
del _sid, _label

# Возраст точки теневого ряда — не дефект источника (источника нет): норму не
# проверяем, как и у аукционов. Множество пополняется здесь, а не литералом выше,
# чтобы список теней жил в одном месте.
DATA_AGE_EXEMPT.update(k for k, v in SERIES.items() if v.get("role") == "shadow")

# Что тянет каждый режим прогона.
#
# key_rate и polymarket_ceasefire стоят в интрадее не ради цены, а ради СКОРОСТИ
# события (docs/LATENCY.md §5): решение по ставке публикуется в 13:30 МСК, а
# суточный прогон приходит в 19:05 — пять с половиной часов ждала и панель, и
# алерт «сюрприз против консенсуса». Обе ноги дешёвые: ставка берётся SOAP-ответом
# в ~2 КБ, Polymarket — двумя запросами к API за ~1 с. Композит и машина состояний
# от этого не двигаются: интрадей их не пересчитывает по определению (run.py).
# Ряды, которые тикают РУЧНЫМ тактом, хотя фетчер у них не manual.*: консенсус
# аналитиков читается из файла человека и лишь дополняется зеркалами опросов
# (fetch/consensus.py). Суточный такт гонял бы за ними десяток запросов к t.me
# каждый день ради числа, которое меняется восемь раз в год.
MANUAL_EXTRA = ("cb_consensus",)

def _fetcher(spec):
    """Имя фетчера строкой; у теневых рядов его нет вовсе (None) — пустая строка,
    чтобы списки режимов ниже не падали на .startswith у None."""
    return str(spec.get("fetcher") or "")


MODES = {
    "intraday": ["imoex", "imoex2", "rgbi", "rvi", "cny_tom", "gld_tom", "brent_moex",
                 "key_rate", "polymarket_ceasefire"],
    "daily": [k for k, v in SERIES.items()
              if v["cadence"] in ("daily", "event")
              and not _fetcher(v).startswith("manual") and k not in MANUAL_EXTRA],
    "weekly": ["cpi_weekly", "ofz_auctions", "imoex2", "dividends"],
    "monthly": [k for k, v in SERIES.items() if v["cadence"] in ("monthly", "decade")],
    "manual": ([k for k, v in SERIES.items() if _fetcher(v).startswith("manual")]
               + [k for k in MANUAL_EXTRA if k in SERIES]),
}


def fetchable():
    """Ряды, у которых есть фетчер — то, что вообще можно опрашивать (bootstrap).

    Теневые ряды (role="shadow") фетчера не имеют: попытка «загрузить» их кончилась
    бы отказом и пометкой status=error на ряду, который на самом деле исправен.
    """
    return [sid for sid, spec in SERIES.items() if _fetcher(spec)]


def series_for_mode(mode):
    ids = MODES.get(mode, [])
    return [(sid, SERIES[sid]) for sid in ids if sid in SERIES]


def poll_due(spec, day_of_month):
    """Нужно ли сегодня опрашивать месячный источник."""
    win = spec.get("poll_window")
    if not win:
        return True
    return win[0] <= day_of_month <= win[1]
