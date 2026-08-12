"""Аукционы ОФЗ: размещение по данным биржи + анонс ближайшего аукциона.

Ряд `ofz_auctions`. Тир `monitor` — предиктивность не доказана, ряд читается глазами
как индикатор фискальной премии в длинном конце.

ПОЧЕМУ БИРЖА, А НЕ МИНФИН. Официальный и полный источник — годовой файл Минфина
«Результаты проведенных аукционов по размещению государственных ценных бумаг»
(xlsx на странице `/ru/perfomance/public_debt/internal/operations/ofz/auction/`);
именно из него исследование собрало историю ряда. С прод-машины он недоступен:
`minfin.gov.ru` отдаёт **503 даже на статику** `/common/upload/library/…` — WAF режет
домен целиком, а не только динамические страницы (замер 12.08.2026). Прежний парсер
искал на странице тег `<table>`, которого там нет и не было: данные лежат в
приложенном файле, а не в разметке.

ЧТО ДАЁТ БИРЖА. Аукционы ОФЗ проходят в режиме «Аукцион: адресные заявки» — доска
**PACT** рынка облигаций. Сверка против эталона исследования (12.08.2026):

    01.07.2026 (один выпуск)        ISS 10,36  Минфин 10,36   в копейку
    15.07.2026 (провал)             ISS  0     Минфин  0      сходится
    22.07 / 29.07 / 05.08 (не было) строк нет  не проводились сходится
    20.05.2026 (два выпуска + ДРПА) ISS 160,30 Минфин 174,44  −8%
    10.06.2026 (два выпуска + ДРПА) ISS  68,28 Минфин  83,64  −18%

Расхождение — это ДРПА (дополнительное размещение после аукциона): биржа не кладёт
его в дневной агрегат PACT, и в ISS его нет нигде — проверены все 37 досок рынка
облигаций, обе торговые сессии и следующий торговый день. Поэтому число ряда честно
называется БИРЖЕВЫМ объёмом размещения, а `meta.method` помечает точки, собранные
этим путём: история из файлов Минфина и новые точки посчитаны по-разному, и шов
обязан быть виден, а не подразумеваться.

СПРОСА У БИРЖИ НЕТ. Совокупный спрос — раскрытие Минфина, не биржи; в ISS его нет
ни в одном эндпоинте (`statistics/.../bonds/auctions` отвечает 404). `demand_bn`
остаётся пустым, и тайл показывает это как «нет данных», а не как ноль.

ЧИСТЫЙ РАЗЛИЧИТЕЛЬ, которого не было ни у одного другого источника: строка доски с
нулевым объёмом = аукцион СОСТОЯЛСЯ и провалился (15.07.2026, флоатер 29028);
строки нет вовсе = аукцион в этот день НЕ ПРОВОДИЛСЯ. Минфин публикует это двумя
разными способами (итоги против «информационного сообщения о непроведении»), и оба
лежат на недоступном домене.

ТЕКУЩЕЕ СОСТОЯНИЕ РЫНКА. Аукционы приостановлены с 20.07.2026 «для стабилизации
рыночной ситуации», возобновление на 12.08.2026 не объявлено. Поэтому фетчер не
изобретает нулевые точки на плановые среды: точка появляется только там, где аукцион
реально шёл. Пауза видна не выдуманным нулём, а расстоянием от последнего аукциона —
`meta.weeks_since`.
"""

import re

from . import (FetchError, RETRO_DAYS, dates, http, incremental_start, make_meta,
               store)

ISS = "https://iss.moex.com/iss"
BOARD = "PACT"                      # «Аукцион: адресные заявки» — режим размещения ОФЗ
HIST = ISS + "/history/engines/stock/markets/bonds/boards/%s/securities.json" % BOARD
COLUMNS = ("TRADEDATE,SECID,SHORTNAME,VOLUME,VALUE,NUMTRADES,FACEVALUE,YIELDCLOSE,"
           "CLOSE,LASTTRADEDATE")
DEFAULT_START_BACK = 120            # холодный старт без затравки: четыре месяца назад
DEFAULT_FACE = 1000.0               # номинал ОФЗ, если биржа не назвала свой
MAX_DAYS = 400                      # потолок обхода: доска опрашивается по одному дню
# Сколько дней «эхо» держится на доске. После успешного размещения выпуск ещё день
# висит строкой с нулевым объёмом — и она НЕОТЛИЧИМА от провалившегося аукциона по
# объёму, числу сделок и ценам (все нули и None у обоих). Различает только
# LASTTRADEDATE: у эха последняя сделка была накануне, у настоящего провала — когда
# выпуск торговался в прошлый раз (15.07.2026: 2025-11-12, за 245 дней до).
ECHO_WINDOW_DAYS = 5

# «О проведении 19 августа 2026 года аукциона по размещению ОФЗ выпусков № …» —
# анонс выходит НАКАНУНЕ около 16:00 МСК и служит сигналом возобновления.
ANNOUNCE_RE = re.compile(r"о\s+проведении.{0,60}?аукцион\w*\s+по\s+размещению\s+ОФЗ", re.I)
ANNOUNCE_DAY = re.compile(r"проведении\s+(\d{1,2})\s+([а-яё]{3,9})\s+(20\d\d)", re.I)
_MONTHS = {"январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
           "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12}


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _day_rows(day):
    """Строки доски размещения за дату. ([] , url) — если аукциона не было.

    Эндпоинт досок принимает ТОЛЬКО `date`: `from`/`till` он молча игнорирует и
    возвращает пусто (проверено). Поэтому обход идёт по дням, а не одним запросом.
    """
    url = "%s?iss.meta=off&date=%s&history.columns=%s" % (HIST, dates.fmt_date(day), COLUMNS)
    payload = http.get_json(url)
    block = payload.get("history") or {}
    idx = {name: n for n, name in enumerate(block.get("columns") or [])}
    rows = []
    for row in block.get("data") or []:
        secid = str(row[idx["SECID"]]) if "SECID" in idx else ""
        # Только государственные выпуски: на этой же доске размещаются корпораты,
        # и без фильтра в «аукцион ОФЗ» уехал бы чужой выпуск.
        if not secid.startswith("SU"):
            continue
        rows.append({"secid": secid,
                     "name": str(row[idx["SHORTNAME"]]) if "SHORTNAME" in idx else "",
                     "volume": _num(row[idx["VOLUME"]]) if "VOLUME" in idx else None,
                     "value": _num(row[idx["VALUE"]]) if "VALUE" in idx else None,
                     "trades": _num(row[idx["NUMTRADES"]]) if "NUMTRADES" in idx else None,
                     "face": _num(row[idx["FACEVALUE"]]) if "FACEVALUE" in idx else None,
                     "yield": _num(row[idx["YIELDCLOSE"]]) if "YIELDCLOSE" in idx else None,
                     "last_trade": (str(row[idx["LASTTRADEDATE"]])
                                    if "LASTTRADEDATE" in idx and row[idx["LASTTRADEDATE"]]
                                    else None)})
    return rows, url


def is_echo(rows, day):
    """Строки-эхо: выпуск ещё висит на доске после вчерашнего размещения.

    Без этой проверки день ПОСЛЕ каждого успешного аукциона попадал в ряд нулём и
    помечался как провалившийся аукцион — то есть панель рисовала вдвое больше
    аукционов, чем было, и половину из них объявляла провалом.
    """
    if any((r.get("volume") or 0) > 0 for r in rows):
        return False                        # размещение было — это точно не эхо
    lasts = [r.get("last_trade") for r in rows]
    if not lasts or not all(lasts):
        return False                        # даты нет — не выдумываем эхо, берём день
    for raw in lasts:
        try:
            gap = (dates.parse_date(day) - dates.parse_date(raw)).days
        except (ValueError, TypeError):
            return False
        if not 0 < gap <= ECHO_WINDOW_DAYS:
            return False
    return True


def day_summary(rows):
    """Строки одного дня -> сводка аукциона по номиналу.

    Номинал берётся ИЗ СТРОКИ (FACEVALUE), а не константой: у ОФЗ-ИН он
    индексируется, у амортизируемых уменьшается, и множитель 1000 занизил бы или
    завысил день на десятки процентов.
    """
    placed = 0.0
    for row in rows:
        vol, face = row.get("volume"), row.get("face") or DEFAULT_FACE
        if vol:
            placed += vol * face
    issues = sorted({r["secid"] for r in rows})
    trades = sum(r["trades"] or 0 for r in rows)
    yields = [r["yield"] for r in rows if r.get("yield")]
    return {"placed_bln": round(placed / 1e9, 4),
            "issues": issues,
            "issue": ", ".join(issues),
            "trades": int(trades),
            # Провал: торги шли, разместить не удалось. Отличается от «дня без
            # аукциона» тем, что строки доски вообще есть.
            "failed": placed <= 0,
            "yield_close": round(max(yields), 4) if yields else None}


def next_auction(max_pages=6, max_age_days=30):
    """(дата, заголовок, url) ближайшего анонсированного аукциона или (None, …).

    Лента ISS публикует «О проведении DD месяца ГГГГ года аукциона по размещению
    ОФЗ…» накануне около 16:00 МСК. Для панели это единственный БЕСПЛАТНЫЙ признак
    того, что пауза в размещениях кончилась, — и он опережает саму сделку на день.
    """
    try:
        from . import moex_press
    except ImportError:                     # автономный запуск из каталога fetch
        try:
            import moex_press
        except ImportError:
            return None, None, None
    try:
        found = moex_press.scan_news(ANNOUNCE_RE, max_pages=max_pages,
                                     max_age_days=max_age_days)
    except FetchError as exc:
        http.LOG("анонс аукциона: %s" % exc)
        return None, None, None
    for news_id, title, published in found:
        m = ANNOUNCE_DAY.search(title)
        if not m:
            continue
        month = next((v for stem, v in _MONTHS.items()
                      if m.group(2).lower().startswith(stem)), None)
        if not month:
            continue
        try:
            day = dates.fmt_date("%s-%02d-%02d" % (m.group(3), month, int(m.group(1))))
        except ValueError:
            continue
        return day, title, "https://www.moex.com/n%d" % news_id
    return None, None, None


def auctions(series_id="ofz_auctions", start=None, end=None, bootstrap=False):
    """-> ("ofz_auctions", {дата: биржевой объём размещения, млрд руб}, meta).

    Точка создаётся ТОЛЬКО за дни, когда аукцион реально шёл. Нулей на плановые
    среды без аукциона фетчер не выдумывает: «аукциона не было» и «аукцион провалился»
    — разные состояния рынка, и склеивать их одним нулём значит стереть ровно ту
    разницу, ради которой этот ряд и ведётся.
    """
    frm = dates.parse_date(start or incremental_start(
        series_id, RETRO_DAYS,
        dates.fmt_date(dates.add_days(dates.today_msk(), -DEFAULT_START_BACK)), bootstrap))
    till = dates.parse_date(end or dates.today_msk())
    days = [d for d in dates.iter_days(frm, till) if dates.is_trading_day(d)][-MAX_DAYS:]

    points, summaries, failures = {}, {}, []
    url = HIST
    for day in days:
        try:
            rows, url = _day_rows(day)
        except FetchError as exc:
            failures.append("%s: %s" % (dates.fmt_date(day), exc))
            continue
        if not rows:
            continue                        # аукциона в этот день не было
        if is_echo(rows, day):
            continue                        # вчерашнее размещение, ещё висит на доске
        summary = day_summary(rows)
        key = dates.fmt_date(day)
        points[key] = summary["placed_bln"]
        summaries[key] = summary

    ahead, title, news_url = next_auction()
    last_day = max(summaries) if summaries else store.last_date(series_id)
    last = dict(summaries.get(last_day) or {}, date=last_day) if last_day else {}
    # Спроса у биржи нет: показываем это явным None, чтобы тайл написал «нет данных»,
    # а не унаследовал число из затравки и не выдал его за свежее.
    last.setdefault("demand_bn", None)
    if "placed_bln" in last:
        last["placed_bn"] = last.pop("placed_bln")

    weeks = None
    if last_day:
        weeks = max(0, (dates.today_msk() - dates.parse_date(last_day)).days // 7)
    status = "ok"
    note = "биржевой объём размещения (доска %s), без ДРПА; спрос биржа не раскрывает" % BOARD
    if failures and not points:
        status, note = "error", "доска %s не ответила: %s" % (BOARD, "; ".join(failures[:2]))
    elif not points:
        note = ("аукционов в окне %s..%s не было; последний — %s"
                % (dates.fmt_date(frm), dates.fmt_date(till), last_day or "неизвестно"))
    return series_id, points, make_meta(
        "moex_pact", url, points, status=status, note=note, unit="rub_bln",
        asof=last_day,
        method="moex_pact", last=last, weeks_since=weeks,
        next_auction=ahead, next_auction_title=title, next_auction_url=news_url,
        value_keys=["placed_bn", "demand_bn", "failed", "issue"],
        failures=failures or None,
        splice="история до 2026-08 собрана по файлам Минфина и включает ДРПА; "
               "новые точки — биржевые и на дни с доразмещением ниже на 5–18%")
