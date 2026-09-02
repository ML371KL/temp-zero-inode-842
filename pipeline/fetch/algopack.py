"""Фетчеры платного шлюза ALGOPACK (datashop) — то, чему нет бесплатного дублёра.

Отличие от fetch/iss.py::futoi принципиальное: там платный путь — ускоритель
(без ключа те же данные приходят с задержкой 14 дней), здесь без ключа данных
НЕТ ВООБЩЕ. Поэтому отсутствие ключа — не отказ и не исключение, а законный
режим «ряд не обновляется»: фетчер отдаёт статус, стор стареет, тайл молчит
(docs/CONTRACT.md §0, §7). Исключение здесь означало бы ежедневную красную
строку в журнале о том, что подписка не куплена.

HI2 — концентрация участников (индекс Херфиндаля—Хиршмана) по каждой бумаге
за день, 11 метрик (замер 02.09.2026): hhi_volume, hhi_buy/sell,
hhi_agressive(_buy/_sell), hhi_passive(_buy/_sell), hhi_netflow_buy/sell.
Эндпоинт отдаёт срез на ОДНУ дату, длинной таблицей
(tradedate, tradetime, secid, metric, value, reference, SYSTIME) страницами по
1000 строк с курсором data.cursor — ~140 бумаг × 11 метрик = 2 страницы в день,
публикация в 18:46 МСК. История доступна с 2020-01-03, но ретро-загрузка при
пустом сторе ограничена 260 торговыми днями: это ~520 запросов, и одного раза
достаточно для z по 252 дням; дальше — инкрементально с последней даты.

Точка ряда за дату — агрегаты по ВСЕМУ рынку, равновзвешенно по бумагам, где
метрика определена (аудит 02.09.2026, ряд hi2_market): усреднять «по рынку»
по капитализации нельзя — сигнал именно в том, что у мелких бумаг концентрация
нетто-потока меняется раньше, чем у тяжеловесов.

Загрузка идёт от старых дней к новым и останавливается, когда исчерпан бюджет
времени прогона (lib/runbudget.py): что успели — записано, следующий такт
продолжит с последней даты. Ретро в обратном порядке оставил бы дыру в
середине, которую инкремент не закрывает никогда (тот же урок, что у zcyc).
"""

from urllib.parse import urlencode

from ..lib import runbudget
from . import RETRO_DAYS, dates, http, make_meta, store

HOST = "apim.moex.com"
ALGOPACK = f"https://{HOST}/iss"
HI2_PATH = "datashop/algopack/eq/hi2"
PAGE = 1000                 # размер страницы datashop (data.cursor PAGESIZE)
MAX_PAGES = 20              # 20 000 строк на день — больше у биржи бумаг нет
RETRO_TRADING_DAYS = 260    # глубина первой загрузки: год торговых дней
# После скольких подряд сетевых отказов останавливаемся: висящий шлюз стоит
# ~93 с на запрос (lib/http.py), и 500 таких запросов съели бы дедлайн юнита.
MAX_NET_FAILURES = 3
SERIES_ID = "hi2_market"
SUBKEYS = ("nf", "bs", "n", "hhi_volume_mean")
UNITS = {"nf": "hhi_pts", "bs": "hhi_pts", "n": "count", "hhi_volume_mean": "hhi_pts"}
# Метрики, из которых складывается точка (имена — как отдаёт шлюз).
M_NF_BUY, M_NF_SELL = "hhi_netflow_buy", "hhi_netflow_sell"
M_AG_BUY, M_AG_SELL = "hhi_agressive_buy", "hhi_agressive_sell"
M_VOLUME = "hhi_volume"

# Пауза между запросами к шлюзу: два запроса на день × 260 дней ретро — и всё
# это к платному хосту, лимиты которого считаются на ключ. 0,3 с — «бережно»
# по договорённости аудита; futoi ходит туда же и ему эта пауза не мешает.
http.HOST_MIN_INTERVAL.setdefault(HOST, 0.3)


def series_ids():
    return [f"{SERIES_ID}_{k}" for k in SUBKEYS]


def ready():
    """Есть ли ключ подписки — читается на каждый вызов (см. iss.algopack_ready)."""
    return bool(http.auth_token(HOST))


def _url(day, start=0):
    query = {"date": dates.fmt_date(day), "iss.meta": "off"}
    if start:
        query["start"] = int(start)
    return f"{ALGOPACK}/{HI2_PATH}.json?{urlencode(query)}"


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _cursor_total(payload):
    cur = payload.get("data.cursor") or {}
    try:
        idx = {c: i for i, c in enumerate(cur["columns"])}
        return int(cur["data"][0][idx["TOTAL"]])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def fetch_day(day):
    """{secid: {metric: value}} за день + число страниц. Кидает FetchError.

    Строки с чужим tradedate отбрасываются: у соседнего эндпоинта zcyc биржа на
    нерабочий день молча отдаёт ближайший более ранний срез, и одно значение
    размазалось бы по выходным.
    """
    key = dates.fmt_date(day)
    by_ticker, start, pages = {}, 0, 0
    for _ in range(MAX_PAGES):
        payload = http.get_json(_url(key, start))
        pages += 1
        block = payload.get("data") or {}
        cols = list(block.get("columns") or [])
        rows = block.get("data") or []
        idx = {c: i for i, c in enumerate(cols)}
        i_day, i_sec, i_met, i_val = (idx.get("tradedate"), idx.get("secid"),
                                      idx.get("metric"), idx.get("value"))
        if None in (i_sec, i_met, i_val):
            break  # схема не та: строк не разобрать, но это не отказ HTTP
        for row in rows:
            if i_day is not None and str(row[i_day])[:10] != key:
                continue
            value = _num(row[i_val])
            if value is None:
                continue
            by_ticker.setdefault(str(row[i_sec]), {})[str(row[i_met])] = value
        if len(rows) < PAGE:
            break
        start += len(rows)
        total = _cursor_total(payload)
        if total is not None and start >= total:
            break
    return by_ticker, pages


def aggregate(by_ticker):
    """Агрегаты дня по всему рынку или None, если ни у одной бумаги нет нетто-потока.

    Равновзвешенно и только по бумагам, где обе стороны метрики определены:
    разность «покупка есть, продажа None» дала бы концентрацию, которой не было.
    """
    nf, bs, vol = [], [], []
    for metrics in by_ticker.values():
        a, b = metrics.get(M_NF_BUY), metrics.get(M_NF_SELL)
        if a is not None and b is not None:
            nf.append(a - b)
        a, b = metrics.get(M_AG_BUY), metrics.get(M_AG_SELL)
        if a is not None and b is not None:
            bs.append(a - b)
        v = metrics.get(M_VOLUME)
        if v is not None:
            vol.append(v)
    if not nf:
        return None
    mean = lambda xs: round(sum(xs) / len(xs), 3) if xs else None  # noqa: E731
    return {"nf": mean(nf), "bs": mean(bs), "n": float(len(nf)),
            "hhi_volume_mean": mean(vol)}


def _trading_days(frm, till):
    """Дни для опроса: эвристика календаря ПЛЮС дни, когда индекс реально торговался
    (субботние сессии с 2025 — см. iss.zcyc, та же грабля)."""
    traded = {d for d, v in ((store.load_series("imoex") or {}).get("points") or {}).items()
              if v is not None}
    return [d for d in dates.iter_days(frm, till)
            if dates.is_trading_day(d) or dates.fmt_date(d) in traded]


def _window(start, end, bootstrap):
    """(дни к опросу, откуда взялось окно). Ретро — не глубже RETRO_TRADING_DAYS."""
    till = dates.parse_date(end) if end else dates.today_msk()
    known = [store.last_date(sid) for sid in series_ids()]
    have = [d for d in known if d]
    if start:
        frm = dates.parse_date(start)
        return _trading_days(frm, till), "start"
    if have and not bootstrap:
        # По САМОМУ отстающему подряду: run.py пишет ряды по одному, и падение на
        # середине оставляет часть id позади (та же логика, что у futoi).
        frm = dates.add_days(min(have), -RETRO_DAYS)
        return _trading_days(frm, till), "incremental"
    # Пустой стор или bootstrap: последние RETRO_TRADING_DAYS торговых дней. Берём
    # календарный запас с избытком и режем по числу торговых дней, а не по дате —
    # праздники и субботние сессии не дают посчитать «260 торговых» календарём.
    frm = dates.add_days(till, -int(RETRO_TRADING_DAYS * 1.6) - 30)
    days = _trading_days(frm, till)
    return days[-RETRO_TRADING_DAYS:], "retro"


def _pack(points, meta):
    return [(sid, points[sid], dict(meta, unit=UNITS[k]))
            for k, sid in zip(SUBKEYS, series_ids())]


def hi2_market(start=None, end=None, bootstrap=False):
    """Концентрация участников по всему рынку: 4 подряда hi2_market_{nf,bs,n,hhi_volume_mean}.

    Возвращает статус, а не исключение, во всех трёх «нет данных»:
      * ключа нет            — stale, «ряд не обновляется» (законный режим);
      * шлюз отверг ключ     — error, «проверьте подписку» (это про оплату);
      * шлюз не отвечает     — error, «чинить доступ» (подписка ни при чём).
    Разные слова нарочно: у futoi неделя ушла на то, что проблема сети выглядела
    проблемой оплаты (fetch/iss.py::_futoi_window).
    """
    ids = series_ids()
    empty = {sid: {} for sid in ids}
    url = _url(end or dates.today_msk())
    if not ready():
        had = any(store.last_date(sid) for sid in ids)
        note = ("ключа ALGOPACK нет (env MOEX_ALGOPACK_TOKEN) — HI2 без подписки не "
                "отдаётся, бесплатного дублёра нет; ряд не обновляется")
        return _pack(empty, make_meta("algopack", url, {}, status="stale" if had else "missing",
                                      note=note, algopack=False))

    days, origin = _window(start, end, bootstrap)
    points = {sid: {} for sid in ids}
    asked = failed = 0
    net_streak = 0
    stopped = None
    paid_fail = None
    for n, day in enumerate(days, 1):
        if runbudget.exhausted():
            stopped = f"бюджет времени фетча исчерпан на {n - 1}/{len(days)} дней"
            break
        key = dates.fmt_date(day)
        url = _url(key)
        asked += 1
        try:
            by_ticker, _pages = fetch_day(day)
        except http.FetchError as e:
            failed += 1
            auth = (getattr(e, "status", None) in (401, 403)
                    or "HTTP 401" in str(e) or "HTTP 403" in str(e))
            http.LOG(f"hi2 {key}: {e}")
            if auth:
                # Отвергнутый ключ отвергнется и на следующих 259 днях — не
                # тратим на это полчаса и лимиты шлюза.
                paid_fail = "auth"
                stopped = "шлюз отверг ключ — загрузка остановлена"
                break
            paid_fail = paid_fail or "net"
            net_streak += 1
            if net_streak >= MAX_NET_FAILURES:
                stopped = f"{net_streak} сетевых отказа подряд — загрузка остановлена"
                break
            continue
        net_streak = 0
        agg = aggregate(by_ticker)
        if agg is None:
            continue  # день без публикации (ещё нет 18:46 МСК или выходной)
        for k, sid in zip(SUBKEYS, ids):
            if agg[k] is not None:
                points[sid][key] = agg[k]
        if n % 50 == 0:
            http.LOG(f"hi2: {n}/{len(days)} дней")

    filled = sum(len(v) for v in points.values())
    notes = [f"окно {origin}: дней опрошено {asked}, с данными {len(points[ids[0]])}"]
    if failed:
        notes.append(f"отказов {failed}")
    if stopped:
        notes.append(stopped)
    status = "ok"
    if paid_fail == "auth":
        status = "error"
        notes.append("шлюз ALGOPACK отверг ключ (HTTP 401/403) — проверьте подписку")
    elif paid_fail == "net" and not filled:
        status = "error"
        notes.append("платный шлюз ALGOPACK недоступен (DNS/сеть/TLS) — подписка ни "
                     "при чём, чинить доступ с машины")
    elif not filled and origin == "retro" and asked > 5:
        # Ключ живой, отказов нет, но за 260 дней ни одной строки: стучимся не туда
        # (сменилась схема/путь) — молчать нельзя, иначе ряд навсегда останется пустым
        # со status=ok.
        status = "error"
        notes.append("шлюз ответил, но не отдал ни одной строки HI2 — проверить путь/схему")
    meta = make_meta("algopack", url, points[ids[0]], status=status, note="; ".join(notes),
                     algopack=True, days_asked=asked, fetch_failed=failed,
                     retro_days=RETRO_TRADING_DAYS if origin == "retro" else None)
    return _pack(points, meta)
