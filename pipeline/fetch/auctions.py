"""Аукционы ОФЗ: итог дня по Минфину, биржевая доска как сверка, анонс ближайшего.

Ряд `ofz_auctions`. Тир `monitor` — предиктивность не доказана, ряд читается глазами
как индикатор фискальной премии в длинном конце.

С 20.11.2026 число дня — ОФИЦИАЛЬНЫЙ ИТОГ МИНФИНА (аукционы плюс доразмещение) из
его телеграм-канала; биржевая доска решает, был ли аукцион, и остаётся сверкой и
резервом (см. блок «официальный итог» ниже). Всё, что написано дальше про доску,
по-прежнему верно — и объясняет, почему одной доски мало.

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
               store, tg)

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


# ------------------------------------------------ официальный итог: канал Минфина
#
# Биржевая доска занижает день на 4–29 % (аудит 20.11.2026): в неё не попадает
# доразмещение после аукциона (ДРПА), а иногда и часть самого аукциона.
#     11.11.2026  Минфин 88,87 (из них ДРПА 23,18)   доска 62,70
#     18.11.2026  Минфин 73,742                      доска 70,64
#     21.10.2026  Минфин 177,128                     доска 136,24
#     07.10.2026  Минфин 80,996 (ДРПА 41,87)         доска 38,07
# Сайт Минфина с прод-машины недоступен (503), но тот же текст итогов ведомство
# публикует в своём телеграм-канале вечером дня аукциона — и конвейер этот канал уже
# читает ради бюджета (fetch/tg.py). Пост устроен одинаково из недели в неделю:
#
#     Минфин России на аукционе разместил два выпуска ОФЗ на общую сумму 73,742 млрд
#     рублей по номиналу
#     🔹 На аукционе по размещению ОФЗ-ПД выпуска № 26233RMFS … было продано бумаг на
#     общую сумму 27,918 млрд рублей. …
#     🔹 Минфин России информирует о результатах проведения 18 ноября 2026 г.
#     дополнительного размещения после аукциона (ДРПА): … размещенный объем выпуска –
#     2 877,000 млн рублей; …
#
# ЧУЖОЕ ЧИСЛО В РЯД ПОПАСТЬ НЕ ДОЛЖНО, поэтому пост принимается, только если:
#   1) это собственный релиз, а не перепечатка («📰 ТАСС: …» отсекает tg.find);
#   2) заголовок — слово в слово итог аукциона ОФЗ «по номиналу» (RESULT_HEAD);
#   3) сумма по выпускам плюс ДРПА сходится с числом заголовка: заголовок бывает
#      округлён («около 140 млрд», «1 трлн»), поэтому в ряд идёт сумма частей, а
#      заголовок её только подтверждает;
#   4) пост привязан к дню: вышел в день аукциона, а если в нём есть ДРПА — дата
#      ДРПА в тексте та же;
#   5) число не меньше биржевого (OFFICIAL_FLOOR): официальный итог включает всё,
#      что видела доска.
# Не прошёл хоть один пункт — день остаётся биржевым, и тайл говорит это прямо.
TG_CHANNEL = "minfin"
TG_QUERY = "на аукционе разместил"          # поиск по каналу: лента глубже недели не видна
TG_KEYS = ("на аукцион", "разместил", "офз", "по номиналу")
_NUM = r"(\d[\d ]*(?:,\d+)?)"
RESULT_HEAD = re.compile(
    r"^Минфин России на аукцион(?:е|ах) разместил\b[^.\n]{0,60}?\bОФЗ\b[^.\n]{0,20}?"
    r"на (?:общую )?сумму (?:около )?" + _NUM + r" (млрд|трлн) рублей по номиналу", re.I)
SOLD_RE = re.compile(
    r"На аукционе по размещению ОФЗ\S*\s+выпуска\s+№\s*(\w+)[^🔹]{0,120}?"
    r"было продано бумаг на общую сумму\s+" + _NUM + r"\s+млрд рублей", re.I)
DRPA_DAY_RE = re.compile(
    r"результатах проведения\s+(\d{1,2})\s+([а-яё]+)\s+(20\d\d)\s*г\w*\.?\s+"
    r"дополнительного размещения", re.I)
DRPA_VOL_RE = re.compile(
    r"размещ[её]нный объ[её]м выпуска\s*[–—-]\s*" + _NUM + r"\s+млн рублей", re.I)
# Заголовок против суммы частей: «около 140» при 139,993 и «1 трлн» при 1000,000.
HEAD_TOL_BN, HEAD_TOL_SHARE = 0.6, 0.01
# Официальный итог не может быть меньше биржевого; три процента — на округление.
OFFICIAL_FLOOR = 0.97
# Расхождение, меньше которого старую точку не переписываем (история до 2026-08
# собрана по файлам Минфина и уже совпадает с итогом до копеек).
BACKFILL_MIN_DIFF = 0.05
OFFICIAL_DAYS_KEEP = 80


def _ru_number(raw):
    return float(str(raw).replace(" ", "").replace("\xa0", "").replace(",", "."))


def parse_result(text, published):
    """Пост канала Минфина -> итог аукционов дня или None.

    {date, total_bn, auction_bn, drpa_bn, issues, head_bn}: total_bn — всё
    размещённое за день по номиналу (аукционы + ДРПА), млрд руб.
    """
    head = RESULT_HEAD.match(tg.headline(text or ""))
    if not head or not published:
        return None
    head_bn = _ru_number(head.group(1)) * (1000.0 if head.group(2).lower() == "трлн" else 1.0)
    sold = [(m.group(1), _ru_number(m.group(2))) for m in SOLD_RE.finditer(text)]
    if not sold:
        return None                         # состава нет — заголовку одному не верим
    auction_bn = sum(v for _, v in sold)
    drpa_bn = sum(_ru_number(m.group(1)) for m in DRPA_VOL_RE.finditer(text)) / 1000.0
    total = auction_bn + drpa_bn
    if abs(total - head_bn) > max(HEAD_TOL_BN, HEAD_TOL_SHARE * head_bn):
        return None                         # части не сходятся с заголовком
    day = str(published)[:10]
    drpa_day = DRPA_DAY_RE.search(text)
    if drpa_day:
        month = next((v for stem, v in _MONTHS.items()
                      if drpa_day.group(2).lower().startswith(stem)), None)
        stated = (None if not month else
                  "%s-%02d-%02d" % (drpa_day.group(3), month, int(drpa_day.group(1))))
        if stated != day:
            return None                     # пост про другой день
    return {"date": day, "total_bn": round(total, 3), "auction_bn": round(auction_bn, 3),
            "drpa_bn": round(drpa_bn, 3), "issues": [s for s, _ in sold],
            "head_bn": round(head_bn, 3)}


def minfin_results(pages=1):
    """{день: итог} по постам канала Минфина. Отказ сети -> FetchError.

    Одна страница поиска — двадцать постов, то есть около пяти месяцев аукционов:
    хватает и на текущий день, и на то, чтобы досчитать прошлые биржевые точки.
    """
    out = {}
    for msg in tg.find(TG_CHANNEL, TG_KEYS, pages=pages, query=TG_QUERY):
        got = parse_result(msg.get("text"), msg.get("published"))
        if got and got["date"] not in out:  # сообщения идут от свежих к старым
            out[got["date"]] = dict(got, url=msg.get("url"))
    return out


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
    data = block.get("data") or []
    # Схема блоков у ISS «плавает между эндпоинтами» (уже ловлено в ленте новостей):
    # пропавшая SECID без этой проверки выбрасывала ВСЕ строки фильтром startswith,
    # и переименование колонки навсегда превращало ряд в «аукционов не было» со
    # status=ok — при реальной паузе это месяцами неотличимо от правды.
    if data and not {"SECID", "VOLUME"} <= set(idx):
        raise FetchError("доска %s сменила схему: нет колонок %s (есть: %s)"
                         % (BOARD, sorted({"SECID", "VOLUME"} - set(idx)),
                            sorted(idx)), url=url)
    rows = []
    for row in data:
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
        # Анонс живёт в ленте и ПОСЛЕ аукциона: со среды-вечера до следующего
        # вторника свежайший подходящий анонс — прошлый, и тайл 5 дней из 7 писал
        # «Назначен следующий: 19.08» про уже прошедшее размещение. Следующий —
        # это не раньше сегодняшнего дня.
        if day < dates.fmt_date(dates.today_msk()):
            continue
        return day, title, "https://www.moex.com/n%d" % news_id
    return None, None, None


def auctions(series_id="ofz_auctions", start=None, end=None, bootstrap=False):
    """-> ("ofz_auctions", {дата: размещено за день по номиналу, млрд руб}, meta).

    Точка создаётся ТОЛЬКО за дни, когда аукцион реально шёл (есть строки доски).
    Нулей на плановые среды без аукциона фетчер не выдумывает: «аукциона не было» и
    «аукцион провалился» — разные состояния рынка, и склеивать их одним нулём значит
    стереть ровно ту разницу, ради которой этот ряд и ведётся.

    ЧИСЛО ДНЯ — официальный итог Минфина (аукционы + доразмещение) из его канала;
    биржевая доска говорит, БЫЛ ли аукцион, и служит сверкой и резервом. Оба числа
    лежат в meta.last, источник числа — в meta.last.source.
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
            # СТОП, а не continue: ретро-окно (5 суток) уже недельного шага ряда.
            # Запиши мы более поздний аукцион в этом же прогоне — last_date уехал
            # бы за сбойный день, и тот выпал бы из всех будущих окон НАВСЕГДА.
            # Остановка держит last_date до сбоя: следующий прогон переспросит.
            failures.append("%s: %s (обход остановлен, дальше — следующим прогоном)"
                            % (dates.fmt_date(day), exc))
            break
        if not rows:
            continue                        # аукциона в этот день не было
        if is_echo(rows, day):
            continue                        # вчерашнее размещение, ещё висит на доске
        summary = day_summary(rows)
        key = dates.fmt_date(day)
        points[key] = summary["placed_bln"]
        summaries[key] = summary

    # Официальные итоги. Отказ канала — не отказ ряда: день остаётся биржевым.
    official, official_note = {}, None
    try:
        official = minfin_results()
    except FetchError as exc:
        official_note = "канал Минфина не прочитан (%s)" % str(exc)[:120]
        http.LOG("%s: %s" % (series_id, official_note))
    stored = store.load_series(series_id) or {}
    old_points = stored.get("points") or {}
    old_meta = stored.get("meta") or {}
    official_days = {str(d)[:10] for d in (old_meta.get("official_days") or [])}
    rejected = []
    for key, summary in summaries.items():
        board = summary["placed_bln"]
        summary["placed_board_bn"] = board
        off = official.get(key)
        if off and off["total_bn"] >= board * OFFICIAL_FLOOR:
            points[key] = off["total_bn"]
            official_days.add(key)
            summary.update(source="minfin_tg", placed_bln=off["total_bn"],
                           placed_official_bn=off["total_bn"],
                           auction_bn=off["auction_bn"], drpa_bn=off["drpa_bn"],
                           official_url=off.get("url"), failed=off["total_bn"] <= 0)
        else:
            official_days.discard(key)
            summary.update(source="moex_pact", placed_official_bn=None,
                           auction_bn=None, drpa_bn=None, official_url=None)
            if off:
                rejected.append([key, off["total_bn"], board])
    # Прошлые дни ряда, записанные биржевым числом: если итог Минфина за этот день
    # нашёлся и он не меньше записанного — в ряд идёт итог. Так шов «биржа с
    # августа» затягивается сам, без ручной пересборки. Дней, которых в ряду нет,
    # пост не создаёт: был ли аукцион, решает доска.
    backfilled = []
    for key, off in sorted(official.items()):
        old = old_points.get(key)
        if key in summaries or not isinstance(old, (int, float)):
            continue
        if off["total_bn"] < old * OFFICIAL_FLOOR:
            rejected.append([key, off["total_bn"], old])
            continue
        official_days.add(key)
        if abs(off["total_bn"] - old) >= BACKFILL_MIN_DIFF:
            points[key] = off["total_bn"]
            backfilled.append([key, old, off["total_bn"]])
    if backfilled:
        http.LOG("%s: %d прошлых дней переведены с биржевого числа на итог Минфина "
                 "(последний %s: %s -> %s)" % ((series_id, len(backfilled)) + tuple(backfilled[-1])))

    ahead, title, news_url = next_auction()
    # Если в окне аукционов не было, «последним аукционом» остаётся тот, что фетчер
    # уже разбирал, а НЕ последняя точка ряда: в затравке дни отменённых аукционов
    # записаны нулями, и брать их за последний аукцион значит объявить аукционом день,
    # когда его не проводили («Аукцион 05.08 не состоялся» при паузе с 20.07).
    last = dict(summaries[max(summaries)], date=max(summaries)) if summaries else {}
    if not last:
        prev = old_meta.get("last")
        if isinstance(prev, dict) and prev.get("date"):
            last = dict(prev)
    # ТОЛЬКО из собственного разбора (свежего или запомненного в meta.last): в
    # затравке дни отменённых аукционов записаны нулями, и store.last_date выдавал
    # нулевую точку за «последний аукцион» — тайл объявлял несостоявшимся аукцион
    # дня, которого не проводили. Нет памяти — честное «неизвестно».
    last_day = last.get("date")
    # Спроса нет ни у биржи, ни в посте Минфина: показываем это явным None, чтобы
    # тайл написал «нет данных», а не унаследовал число из затравки.
    last.setdefault("demand_bn", None)
    if "placed_bln" in last:
        last["placed_bn"] = last.pop("placed_bln")

    weeks = None
    if last_day:
        weeks = max(0, (dates.today_msk() - dates.parse_date(last_day)).days // 7)
    status = "ok"
    by_official = last.get("source") == "minfin_tg"
    if by_official:
        note = ("размещено за день по номиналу — итог Минфина (канал t.me/%s), с "
                "доразмещением после аукциона; биржевая доска %s — сверка"
                % (TG_CHANNEL, BOARD))
    else:
        note = ("биржевой объём размещения (доска %s), без доразмещения после аукциона: "
                "итог Минфина за последний аукцион не получен" % BOARD)
    if failures and not points:
        status, note = "error", "доска %s не ответила: %s" % (BOARD, "; ".join(failures[:2]))
    elif not summaries:
        note = ("аукционов в окне %s..%s не было; последний — %s"
                % (dates.fmt_date(frm), dates.fmt_date(till), last_day or "неизвестно"))
    if official_note:
        note = "%s; %s" % (note, official_note)
    return series_id, points, make_meta(
        "minfin_tg+moex_pact", url, points, status=status, note=note, unit="rub_bln",
        asof=last_day,
        method=last.get("source") or "moex_pact", last=last, weeks_since=weeks,
        next_auction=ahead, next_auction_title=title, next_auction_url=news_url,
        value_keys=["placed_bn", "demand_bn", "failed", "issue"],
        failures=failures or None,
        official_days=sorted(official_days)[-OFFICIAL_DAYS_KEEP:],
        official_backfilled=backfilled or None,
        official_rejected=rejected or None,
        official_error=official_note,
        splice="дни из meta.official_days — итог Минфина с доразмещением (как и история "
               "до 2026-08 по файлам Минфина); остальные точки с 2026-08 — биржевая "
               "доска, без доразмещения и ниже итога на 4–29%")
