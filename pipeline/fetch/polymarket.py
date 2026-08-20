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
from datetime import datetime, timezone

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


def _history(token, limit_days=400):
    """Дневная история цены YES -> {дата: вероятность}."""
    url = "%s/prices-history?market=%s&interval=max&fidelity=1440" % (CLOB, token)
    data = get_json(url)
    points = {}
    for row in (data or {}).get("history", [])[-limit_days:]:
        try:
            day = datetime.fromtimestamp(int(row["t"]), timezone.utc).date().isoformat()
            points[day] = round(float(row["p"]), 4)
        except (KeyError, TypeError, ValueError):
            continue
    return points


def ceasefire():
    """-> ("polymarket_ceasefire", {дата: вероятность YES}, meta).

    В meta: slug и вопрос выбранного рынка (без них число нечитаемо), список всех
    живых рынков серии с ценами и признак contaminated для «старого» критерия.
    """
    markets, errors = _collect_markets()
    if not markets:
        return SERIES_ID, {}, _meta("error", GAMMA,
                                    "; ".join(errors) or "серии не найдены")
    primary = _pick_primary(markets)
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
    points, note = {}, None
    if token:
        try:
            points = _history(token)
        except FetchError as exc:
            note = "история цен не отдалась (%s), взята текущая котировка" % exc
    today = datetime.now(timezone.utc).date().isoformat()
    if price is not None:
        points[today] = round(price, 4)     # свежая котировка поверх истории
    if not points:
        return SERIES_ID, {}, _meta("error", GAMMA, "нет ни истории, ни цены",
                                    {"errors": errors})
    return SERIES_ID, points, _meta(
        "ok", "https://polymarket.com/event/%s" % (event_slug or ""),
        note or market.get("question"),
        {"slug": market.get("slug"), "event_slug": event_slug,
         "question": market.get("question"), "end_date": market.get("endDate"),
         "horizon_days": _horizon_days(market), "volume": _volume(market) or None,
         "contaminated": contaminated, "markets": board, "errors": errors,
         "criteria_note": "серии без слова agreement разрешались YES по краткому "
                          "перемирию — сравнивать их с «соглашением» нельзя",
         "splice": "до 20.08.2026 ряд шёл по БЛИЖАЙШЕМУ контракту серии и в конце "
                   "его жизни съезжал к нулю по календарю, а не по новостям; с "
                   "20.08.2026 берётся самый торгуемый с горизонтом ≥30 суток"})


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
