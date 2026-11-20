"""Polymarket: вероятность перемирия РФ–Украина (ряд `polymarket_ceasefire`).

Слой 3 архитектуры: мониторинг без предиктивных претензий (validation/REGIME.md
§6). История коротка, событий мало — ряд копится ради будущей проверки, а не для
скоринга. Ключевая ценность на панели — ЗНАТЬ, что в цене, когда рынок дёргается
на заголовках.

ГЛАВНАЯ ГРАБЛЯ — критерий разрешения. Серии с формулировкой «ceasefire by DATE»
разрешались YES по факту КРАТКОГО перемирия (трёхдневного, пасхального и т.п.),
то есть меряли не то, что интересует рынок акций. Проверяемый факт из самих
данных API (11.08.2026): за один и тот же срок
    russia-x-ukraine-ceasefire-by-june-30-2026            -> YES (цена 1)
    russia-x-ukraine-ceasefire-agreement-by-june-30-2026  -> NO  (цена 0)
Поэтому приоритет отдаётся сериям со словом agreement (и «peace talks/deal»), а
slug и вопрос СОХРАНЯЮТСЯ вместе с ценой: без текста вопроса число бессмысленно.

Вторая грабля: в выдаче поиска попадаются шуточные рынки («Russia-Ukraine
Ceasefire before GTA VI?») — они отфильтрованы по наличию даты в вопросе и по
принадлежности к нужному событию.

ТРЕТЬЯ ГРАБЛЯ, найденная 20.08.2026: КАКОЙ горизонт показывать. Прежде брался
ближайший по сроку контракт — «рынок акций реагирует на ближний срок». На деле
у контракта на исходе жизни уровень определяется календарём: 20.08 августовский
(11 суток до экспирации, оборот $0,4 млн) стоил 1,7%, декабрьский (133 суток,
оборот $2,2 млн) — 23,5%, мартовский — 42,5%. Читатель видел «мира не будет»
там, где рынок платил почти четверть за соглашение до конца года.
Отдельно проверено, что падение последних недель НЕ было истечением срока:
за месяц декабрьский контракт потерял 12,0 п.п. (35,5 → 23,5%), октябрьский —
9,5 п.п., августовский — 5,35 п.п. Дальние, где теты почти нет, упали СИЛЬНЕЕ;
переоценка настоящая. Но уровень ближнего контракта для «новостного фона» всё
равно не годится, поэтому выбор переведён на ликвидность при горизонте ≥30 суток
(см. _pick_primary).
"""

import json
import re
from datetime import date, datetime, timezone

try:                                       # прод: общий HTTP-слой (CONTRACT.md §4)
    from lib.http import get_json, FetchError
except ImportError:
    try:
        from pipeline.lib.http import get_json, FetchError
    except ImportError:                    # автономный запуск (отладка парсеров)
        class FetchError(Exception):
            pass

        def get_json(url, timeout=45, **_kw):
            import urllib.request
            req = urllib.request.Request(url, headers={"User-Agent": "moex-radar/1.0"})
            try:
                return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
            except (OSError, ValueError) as exc:
                raise FetchError("%s: %s" % (url, exc))

SERIES_ID = "polymarket_ceasefire"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

# Порядок = приоритет. Первый слаг — серия с внятным критерием (соглашение),
# второй — историческая серия с «любым» перемирием: держим ради длины истории,
# но помечаем contaminated=True.
EVENT_SLUGS = [
    ("russia-x-ukraine-ceasefire-agreement-by", False),
    ("russia-x-ukraine-ceasefire-by", True),
]
SEARCH_URL = GAMMA + "/public-search?q=russia%20ukraine%20ceasefire&limit_per_type=20"


def _as_list(raw):
    """outcomePrices/clobTokenIds приходят строкой с JSON внутри."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw.strip().startswith("["):
        try:
            return json.loads(raw)
        except ValueError:
            return []
    return []


def _yes_price(market):
    """Цена исхода YES. Порядок outcomes не гарантирован — ищем по названию."""
    outcomes = [str(o).lower() for o in _as_list(market.get("outcomes"))]
    prices = _as_list(market.get("outcomePrices"))
    if outcomes and prices and len(outcomes) == len(prices):
        for name, price in zip(outcomes, prices):
            if name.startswith("yes"):
                try:
                    return float(price)
                except (TypeError, ValueError):
                    return None
    last = market.get("lastTradePrice")
    try:
        return float(last)
    except (TypeError, ValueError):
        return None


def _yes_token(market):
    """Токен исхода YES — по ИМЕНИ исхода, как в _yes_price, а не tokens[0].

    Порядок outcomes API не обещан (сам модуль говорит это строчкой выше), и
    рынок с порядком [No, Yes] превращал бы ВСЮ историю цен в 1−p: тайл
    «Вероятность перемирия» показывал бы 95% вместо 5% со статусом ok. Если
    сопоставить токен исходу нельзя (длины разошлись) — честный None, а не
    угаданный первый.
    """
    tokens = _as_list(market.get("clobTokenIds"))
    outcomes = [str(o).lower() for o in _as_list(market.get("outcomes"))]
    if tokens and outcomes and len(tokens) == len(outcomes):
        for name, token in zip(outcomes, tokens):
            if name.startswith("yes"):
                return str(token)
        return None  # исхода Yes в рынке нет — чужой токен брать нельзя
    return str(tokens[0]) if tokens and not outcomes else None


def _meta(status, url, note=None, extra=None):
    meta = {"source": "polymarket", "url": url, "status": status, "note": note,
            "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    if extra:
        meta.update(extra)
    return meta


def _collect_markets():
    """[(запись рынка, contaminated)] по всем известным сериям. Пусто — если API молчит."""
    out, errors = [], []
    for slug, contaminated in EVENT_SLUGS:
        try:
            events = get_json("%s/events?slug=%s" % (GAMMA, slug))
        except FetchError as exc:
            errors.append("%s: %s" % (slug, exc))
            continue
        for event in events if isinstance(events, list) else []:
            for market in event.get("markets") or []:
                out.append((market, contaminated, event.get("slug")))
    return out, errors


def _horizon_days(market, now=None):
    """Суток до экспирации рынка или None, если срок не назван."""
    raw = str(market.get("endDate") or "")[:10]
    try:
        end = datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None
    today = (now or datetime.now(timezone.utc)).date()
    return (end - today).days


_MONTHS_EN = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
              "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
              "november": 11, "december": 12}
_QDATE = re.compile(r"(?:^|\s)by\s+([A-Za-z]+)\s+(\d{1,2}),?\s+(20\d\d)", re.I)


def _question_date(question):
    """Дата разрешения ИЗ ТЕКСТА вопроса (ISO) или None.

    endDate у Polymarket — момент расчёта, а не дата из вопроса: у «by December 31,
    2026» там стоит 2027-01-01T04:59Z (полночь по Нью-Йорку плюс буфер). Показывать
    читателю 01.01.2027 рядом с вопросом «by December 31, 2026» — значит спорить с
    самим собой на одной плитке, что и уехало в прод 20.08.2026.
    """
    m = _QDATE.search(str(question or ""))
    if not m:
        return None
    month = _MONTHS_EN.get(m.group(1).lower())
    if not month:
        return None
    try:
        return date(int(m.group(3)), month, int(m.group(2))).isoformat()
    except ValueError:
        return None


def _volume(market):
    try:
        return float(market.get("volume") or 0.0)
    except (TypeError, ValueError):
        return 0.0


# Минимальный горизонт: у контракта на грани экспирации цена механически сходится
# к нулю или единице, и её УРОВЕНЬ несопоставим во времени. 30 суток — не про
# «тету», а про сопоставимость: ниже этого срока число говорит о календаре
# столько же, сколько о переговорах.
MIN_HORIZON_DAYS = 30


def _pick_primary(markets, now=None):
    """Самый ЛИКВИДНЫЙ незакрытый рынок из чистой серии с горизонтом ≥30 суток.

    До 20.08.2026 брался БЛИЖАЙШИЙ по сроку, и это оказалось ошибкой. Замер того
    дня: ближний контракт (до экспирации 11 суток, оборот $0,4 млн) стоил 1,7% и
    показывал «мира не будет», тогда как самый торгуемый контракт той же серии
    (133 суток, оборот $2,2 млн) стоил 23,5%, а мартовский — 42,5%. Плитка при
    этом называется «новостной фон»: уровень, который зависит от того, сколько
    дней осталось до произвольной календарной даты, для этого не годится.
    Ликвидность выбрана мерой потому, что деньги стоят там, где идёт торговля:
    на дальнем краю серии висят рынки с оборотом в три тысячи долларов, и брать
    их «потому что горизонт длиннее» значило бы менять шум на шум.
    Порог 30 суток заодно делает перекат естественным: когда торгуемый контракт
    подходит к сроку, выбор сам уезжает на следующий.
    """
    live = [(m, c, ev) for m, c, ev in markets
            if not m.get("closed") and _yes_price(m) is not None
            and re.search(r"\b(20\d\d)\b", str(m.get("question") or ""))]
    if not live:
        return None
    clean = [x for x in live if not x[1]] or live
    far = [x for x in clean if (_horizon_days(x[0], now) or 0) >= MIN_HORIZON_DAYS]
    if not far:
        # Всё вот-вот истекает: берём самый дальний, а не самый ликвидный —
        # сопоставимость уровня здесь важнее оборота, и тайл назовёт горизонт.
        return max(clean, key=lambda x: str(x[0].get("endDate") or ""))
    return max(far, key=lambda x: (_volume(x[0]), str(x[0].get("endDate") or "")))


DAY_SECONDS = 86400
GRID_SLACK_SECONDS = 300


def _history(token, limit_days=400):
    """Суточные срезы цены YES -> ({дата: вероятность}, живая точка | None).

    API отдаёт цену на суточной сетке (у декабрьского контракта 2026 — в 00:00
    UTC каждого дня) и ПОСЛЕДНЕЙ строкой — цену «прямо сейчас», вне сетки:

        … (19.11 00:00, 0,115) (20.11 00:00, 0,095) (20.11 05:28, 0,095)

    До 20.11.2026 всё это шло в ряд подряд, и последняя точка дня оказывалась ценой
    в момент прогона, а назавтра переписывалась срезом того же дня: значение
    прошедшего дня менялось задним числом на величину до 2 п.п. (19.11: записано
    0,095, срез дня — 0,115). Теперь ряд — только срезы сетки; живая точка
    возвращается отдельно и в ряд не идёт.
    """
    url = "%s/prices-history?market=%s&interval=max&fidelity=1440" % (CLOB, token)
    data = get_json(url)
    rows = []
    for row in (data or {}).get("history", [])[-limit_days:]:
        try:
            rows.append((int(row["t"]), round(float(row["p"]), 4)))
        except (KeyError, TypeError, ValueError):
            continue
    rows.sort()
    live = None
    if len(rows) >= 2:
        step = (rows[-1][0] - rows[-2][0]) % DAY_SECONDS
        if GRID_SLACK_SECONDS < step < DAY_SECONDS - GRID_SLACK_SECONDS:
            live = rows.pop()               # вне суточной сетки — это «сейчас»
    points = {}
    for stamp, price in rows:
        day = datetime.fromtimestamp(stamp, timezone.utc).date().isoformat()
        points.setdefault(day, price)       # первый срез дня; сетка даёт один на день
    if live is not None:
        live = {"at": datetime.fromtimestamp(live[0], timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ"), "price": live[1]}
    return points, live


def _stored():
    """(точки, meta) ряда из стора; пусто, если стора нет (автономный запуск)."""
    try:
        try:
            from lib import store
        except ImportError:
            from pipeline.lib import store
        series = store.load_series(SERIES_ID) or {}
    except Exception:  # noqa: BLE001 — стор не обязателен для разбора API
        return {}, {}
    return (series.get("points") or {}), (series.get("meta") or {})


def ceasefire(now=None):
    """-> ("polymarket_ceasefire", {дата: вероятность YES}, meta).

    В meta: slug и вопрос выбранного рынка (без них число нечитаемо), список всех
    живых рынков серии с ценами и признак contaminated для «старого» критерия.

    СМЕНА КОНТРАКТА (аудит 20.11.2026). Выбор контракта сам переезжает на
    следующий, когда до расчёта текущего остаётся меньше 30 суток: около 2–3
    декабря 2026 — с декабрьского (цена ~0,095) на мартовский 2027 (~0,285).
    Прежний код при этом доливал в ряд ВСЮ историю нового контракта и переписывал
    прошлое с 19.08: уровень прыгал задним числом, изменения «за неделю» и «за
    месяц» у стыка становились ложными. Теперь:
      * история прежнего контракта не переписывается — новый пишется только с
        даты смены (meta.contract_since);
      * идентификатор выбранного контракта и его срок лежат в meta (slug,
        end_date, resolves_on), прежний — в meta.contract_prev;
      * цена в момент прогона — в meta.price_now, а не точкой ряда.
    """
    moment = now or datetime.now(timezone.utc)
    markets, errors = _collect_markets()
    if not markets:
        return SERIES_ID, {}, _meta("error", GAMMA,
                                    "; ".join(errors) or "серии не найдены")
    primary = _pick_primary(markets, moment)
    if primary is None:
        return SERIES_ID, {}, _meta("stale", GAMMA,
                                    "все рынки серий закрыты — нужен новый slug",
                                    {"errors": errors})
    market, contaminated, event_slug = primary
    board = [{"slug": m.get("slug"), "question": m.get("question"),
              "price_yes": _yes_price(m), "end_date": m.get("endDate"),
              "closed": bool(m.get("closed")), "contaminated": c,
              "volume": m.get("volume")}
             for m, c, _ in markets if not m.get("closed")]
    board.sort(key=lambda r: str(r["end_date"] or "9999"))
    price = _yes_price(market)
    token = _yes_token(market)
    history, live, note = {}, None, None
    if token:
        try:
            history, live = _history(token)
        except FetchError as exc:
            note = "история цен не отдалась (%s), взята текущая котировка" % exc
    if price is None and live:
        price = live["price"]
    if not history and price is None:
        return SERIES_ID, {}, _meta("error", GAMMA, "нет ни истории, ни цены",
                                    {"errors": errors})

    # Тот же контракт, что в ряду, или новый?
    slug = market.get("slug")
    today = moment.date().isoformat()
    old_points, old_meta = _stored()
    prev_slug = old_meta.get("slug")
    since = old_meta.get("contract_since")
    prev = old_meta.get("contract_prev")
    if old_points and prev_slug and slug and prev_slug != slug:
        since = today
        prev = {"slug": prev_slug, "question": old_meta.get("question"),
                "resolves_on": old_meta.get("resolves_on"),
                "end_date": old_meta.get("end_date"),
                "until": max((d for d in old_points if d < today), default=None)}
    points = {d: v for d, v in history.items() if not since or d >= str(since)[:10]}
    stamp = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    return SERIES_ID, points, _meta(
        "ok", "https://polymarket.com/event/%s" % (event_slug or ""),
        note or market.get("question"),
        {"slug": slug, "event_slug": event_slug,
         "question": market.get("question"), "end_date": market.get("endDate"),
         "horizon_days": _horizon_days(market, moment), "volume": _volume(market) or None,
         "resolves_on": _question_date(market.get("question")),
         "contract_since": since, "contract_prev": prev,
         "price_now": round(price, 4) if price is not None else None,
         "price_now_at": stamp if price is not None else None,
         # asof ряда — день последнего СРЕЗА; в день смены контракта срезов нового
         # ещё может не быть, и asof честно остаётся прежним (store считает сам).
         "contaminated": contaminated, "markets": board, "errors": errors,
         "criteria_note": "серии без слова agreement разрешались YES по краткому "
                          "перемирию — сравнивать их с «соглашением» нельзя",
         "splice": "до 20.08.2026 ряд шёл по БЛИЖАЙШЕМУ контракту серии и в конце "
                   "его жизни съезжал к нулю по календарю, а не по новостям; с "
                   "20.08.2026 берётся самый торгуемый с горизонтом ≥30 суток. "
                   "Точки ряда — суточные срезы цены; при смене контракта прошлое "
                   "не переписывается (meta.contract_since)"})


def search_events(query="russia ukraine ceasefire"):
    """Подсказка оператору: какие серии живы сейчас (когда EVENT_SLUGS протухли).

    В прогоне не участвует — вызывается руками при обновлении списка слагов.
    """
    url = "%s/public-search?q=%s&limit_per_type=20" % (
        GAMMA, query.replace(" ", "%20"))
    data = get_json(url)
    out = []
    for event in (data or {}).get("events", []):
        out.append({"slug": event.get("slug"), "title": event.get("title"),
                    "closed": bool(event.get("closed")),
                    "markets": [m.get("slug") for m in event.get("markets") or []]})
    return out
