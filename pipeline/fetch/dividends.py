"""Дивидендный календарь: ближайшие отсечки по бумагам индекса МосБиржи.

Ряд `dividends`, тир `monitor`. Значение точки — сумма дивдоходностей отсечек дня
(контракт docs/SOURCES.md §3), подробности по бумагам — в `meta.items`.

ЗАЧЕМ КАЛЕНДАРЬ. Дивидендный гэп — механическая просадка индекса, которую нельзя
путать с ухудшением рынка: машина состояний считает тренд по цене IMOEX, а не по
MCFTR. Важно: валидированный сигнал `dy_trail` календаря НЕ требует — он считается
как доходность MCFTR минус доходность IMOEX за 252 дня (compute/panel.py). Здесь
речь только о тайле, и планка к источнику поэтому ниже, чем была бы для ноги ядра.

ПОЧЕМУ НЕ ISS. Датасет биржи `securities/{sec}/dividends` МЁРТВ: проверено на десяти
бумагах (SBER, LKOH, GAZP, TATN, MTSS, PHOR, SIBN, MOEX, T, X5) — у ВСЕХ записи
кончаются 2025 годом, за 2026 нет ни одной. Официальной ленты дивидендов у биржи нет.

ПОРЯДОК ИСТОЧНИКОВ: T-Invest API → smart-lab → `inputs/dividends.yml`.

**T-Invest** (`fetch/tinvest.py`) отдаёт по каждой бумаге и прошлое, и будущее:
дату закрытия реестра, последний день покупки, чистый дивиденд, доходность и цену
закрытия, от которой она посчитана. Это структурный API, а не разметка сайта.
Осторожно с диагностикой его доступности: с прод-машины запрос отвечает «код 000»,
и это ЛЕГКО принять за блокировку по IP (я так и сделал в первом заходе). На деле
падает проверка сертификата — подробности в шапке `fetch/tinvest.py`.

**smart-lab** — резерв, когда токена нет. Сторонний агрегатор: до рекомендации
совета директоров его числа прогнозные, разметка может поменяться. Поэтому шапка
таблицы проверяется явно: исчезла колонка «Дата закрытия реестра» — это отказ
источника, а не повод молча разобрать мусор.

**СОСТАВ ИНДЕКСА С ВЕСАМИ из ISS** нужен обоим путям. Веса дают то, чего в ручном
файле не было никогда: **ожидаемый гэп индекса** = Σ вес × дивдоходность. Отсечка
Сбербанка 20.07.2026 при весе 13,9% и доходности 13,49% — это 1,67% индекса, а весь
тот день с учётом VTBR, SBERP и TRNFP — около 2,5%. Без весов панель показывала бы
«13,5%» и путала механический гэп с падением рынка.

Ручной `inputs/dividends.yml` остаётся последним резервом и включается, только
когда автомат не дал ни одной записи.
"""

import re

from . import (FetchError, dates, http, make_meta, store)

CALENDAR = "https://smart-lab.ru/dividends/"
BASE_TINVEST = "https://invest-public-api.tinkoff.ru/rest"
ISS = "https://iss.moex.com/iss"
INDEX_COMP = ISS + "/statistics/engines/stock/markets/index/analytics/IMOEX.json"
SEC_INFO = ISS + "/securities/%s.json"
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
       "Accept-Language": "ru,en;q=0.8"}

# Колонки ищем по ШАПКЕ, сжатой до бес-пробельного вида. Пробелы и точки в
# заголовках — артефакт вёрстки: «Див.Дох.» приходит как «Див.<br>Дох.» и после
# снятия тегов превращается в «див. дох.». Наивный поиск подстроки «див.дох» такую
# шапку не находит, колонка доходности молча теряется, и календарь выходит без
# единого процента (поймано на первом же живом прогоне). Тот же приём — в парсере
# PDF ОРФР по той же причине.
NEED = ("тикер", "дивиденд", "датазакрытияреестра")
COLUMNS = (("ticker", "тикер"), ("amount", "дивиденд"), ("yield", "дивдох"),
           ("buy", "купитьдо"), ("ex", "датазакрытияреестра"), ("price", "ценаакции"))
# Тикер МосБиржи: от одного знака («T» — Т-Технологии) до шести, буквы и цифры
# («X5»). Требование трёх букв выкидывало из календаря самые крупные бумаги.
_TICKER = re.compile(r"^[A-Z][A-Z0-9]{0,5}$")
_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S)
_TABLE = re.compile(r"<table.*?</table>", re.S)
_DATE = re.compile(r"^(\d{2})\.(\d{2})\.(20\d\d)$")
MAX_YIELD_PCT = 60.0        # санитарный потолок: выше — опечатка или спецдивиденд-выброс


def _plain(fragment):
    text = re.sub(r"<[^>]+>", " ", fragment)
    text = (text.replace("&nbsp;", " ").replace("&#160;", " ")
                .replace("&amp;", "&").replace("&quot;", '"'))
    return re.sub(r"[\s\xa0 ]+", " ", text).strip()


def _num(raw):
    """'2,7%' / '4 017,5' / '110₽' -> float. Пусто -> None."""
    txt = re.sub(r"[^\d,.\-]", "", str(raw)).replace(",", ".")
    if not txt or txt.count(".") > 1:
        return None
    try:
        return float(txt)
    except ValueError:
        return None


def _date(raw):
    m = _DATE.match(str(raw).strip())
    return "%s-%s-%s" % (m.group(3), m.group(2), m.group(1)) if m else None


def index_weights():
    """{тикер: вес в IMOEX, %}. Пустой словарь — если ISS не ответил."""
    try:
        payload = http.get_json(INDEX_COMP + "?iss.meta=off&limit=100")
    except FetchError as exc:
        http.LOG("состав индекса: %s" % exc)
        return {}
    block = payload.get("analytics") or {}
    idx = {name: n for n, name in enumerate(block.get("columns") or [])}
    out = {}
    for row in block.get("data") or []:
        ticker = str(row[idx["ticker"]]) if "ticker" in idx else None
        weight = row[idx["weight"]] if "weight" in idx else None
        if ticker and isinstance(weight, (int, float)):
            # У бумаги бывает несколько строк (разные сессии) — берём максимальный
            # вес, а не последний: последняя строка может быть неполной сессией.
            out[ticker] = max(out.get(ticker, 0.0), float(weight))
    return out


def parse_calendar(html):
    """HTML smart-lab -> [{ticker, ex_date, buy_until, amount_rub, yield_pct, price}].

    ex_date — «Дата закрытия реестра»: на МосБирже с режимом T+1 это первый день,
    когда бумага торгуется уже без дивиденда, то есть день гэпа. Именно так дата
    определена в шапке inputs/dividends.yml.
    """
    rows, seen_header = [], False
    for table in _TABLE.findall(html):
        trs = _ROW.findall(table)
        if not trs:
            continue
        head = [re.sub(r"[\s.,]+", "", _plain(c).lower()) for c in _CELL.findall(trs[0])]
        if not all(any(need in h for h in head) for need in NEED):
            continue                      # не календарь (топы по доходности и пр.)
        seen_header = True
        col = {}
        for n, name in enumerate(head):
            for key, needle in COLUMNS:
                if needle in name and key not in col:
                    col[key] = n
        for tr in trs[1:]:
            cells = [_plain(c) for c in _CELL.findall(tr)]
            if len(cells) <= max(col.values() or [0]):
                continue
            ticker = cells[col["ticker"]].upper() if "ticker" in col else ""
            ex = _date(cells[col["ex"]]) if "ex" in col else None
            if not _TICKER.match(ticker) or not ex:
                continue
            yld = _num(cells[col["yield"]]) if "yield" in col else None
            if yld is not None and not (0 < yld < MAX_YIELD_PCT):
                yld = None                # 704% из блока «топ по доходности» — не наше
            rows.append({"ticker": ticker, "ex_date": ex,
                         "buy_until": _date(cells[col["buy"]]) if "buy" in col else None,
                         "amount_rub": _num(cells[col["amount"]]) if "amount" in col else None,
                         "yield_pct": yld,
                         "price": _num(cells[col["price"]]) if "price" in col else None})
    if not seen_header:
        raise FetchError("smart-lab: таблицы календаря нет (изменилась вёрстка)",
                         url=CALENDAR)
    return rows


def _issue_size(ticker):
    """Число акций выпуска или None. Нужно, чтобы перевести рубли на акцию в млрд."""
    try:
        payload = http.get_json((SEC_INFO % ticker) + "?iss.meta=off&iss.only=description")
    except FetchError:
        return None
    block = payload.get("description") or {}
    idx = {name: n for n, name in enumerate(block.get("columns") or [])}
    for row in block.get("data") or []:
        if str(row[idx.get("name", 0)]).upper() == "ISSUESIZE":
            value = row[idx.get("value", 2)] if "value" in idx else None
            try:
                return float(str(value).replace(" ", ""))
            except (TypeError, ValueError):
                return None
    return None


def from_tinvest(weights, back_days=180, ahead_days=200):
    """Календарь из T-Invest по бумагам индекса. [] — если токена нет или API молчит.

    Запрос идёт ТОЛЬКО по бумагам, у которых в справочнике стоит флаг выплаты
    дивидендов: из 46 бумаг индекса их около трёх десятков, и лишние запросы к
    заведомо бездивидендным (VTBR, OZON…) — это секунды на каждом прогоне впустую.
    """
    from . import tinvest
    if not tinvest.ready():
        return [], "нет токена T-Invest"
    try:
        book = tinvest.shares()
    except FetchError as exc:
        return [], "справочник T-Invest: %s" % exc
    frm = dates.fmt_date(dates.add_days(dates.today_msk(), -abs(back_days)))
    till = dates.fmt_date(dates.add_days(dates.today_msk(), abs(ahead_days)))
    rows, failed = [], []
    for ticker in sorted(weights):
        info = book.get(ticker)
        if not info or not info.get("pays_dividends"):
            continue
        try:
            found = tinvest.dividends(info["uid"], frm, till)
        except FetchError as exc:
            failed.append("%s: %s" % (ticker, exc))
            continue
        for rec in found:
            rows.append({"ticker": ticker,
                         # ex_date — день, с которого бумага торгуется без дивиденда.
                         # На МосБирже в режиме T+1 это и есть дата закрытия реестра
                         # (SBER: последний день покупки 17.07, реестр 20.07).
                         "ex_date": rec["record_date"],
                         "buy_until": rec["last_buy_date"],
                         "amount_rub": rec["amount_rub"],
                         "yield_pct": rec["yield_pct"],
                         "price": rec["price"],
                         "payment_date": rec["payment_date"],
                         "source": "tinvest"})
    if failed:
        http.LOG("T-Invest: отказов по бумагам %d (%s)" % (len(failed), failed[0][:60]))
    return rows, ("отказов по бумагам: %d" % len(failed)) if failed else None


def calendar(series_id="dividends", with_amounts=True):
    """-> ("dividends", {ex_date: сумма дивдоходностей, %}, meta).

    Порядок источников: T-Invest (структурный API с ценой закрытия и собственной
    доходностью) → smart-lab (скрейп) → inputs/dividends.yml (руками).

    В ряд идут только бумаги ИНДЕКСА: календари перечисляют всех эмитентов, а тайл
    описывает просадку индекса. Без фильтра в сумму дня попадал бы третий эшелон,
    которого в IMOEX нет.
    """
    weights = index_weights()
    if not weights:
        return _fallback("состав индекса не получен — фильтровать нечем")

    rows, note = from_tinvest(weights)
    origin = "tinvest"
    if not rows:
        origin = "smartlab"
        try:
            rows = parse_calendar(http.get_text(CALENDAR, headers=_UA, timeout=25,
                                                retries=2))
        except FetchError as exc:
            return _fallback("T-Invest: %s; smart-lab: %s" % (note or "пусто", exc))

    items, skipped = [], []
    for row in rows:
        weight = weights.get(row["ticker"])
        if weight is None:
            skipped.append(row["ticker"])
            continue
        items.append(dict(row, weight_pct=round(weight, 3),
                          index_drag_pct=(round(weight * row["yield_pct"] / 100.0, 4)
                                          if row["yield_pct"] is not None else None)))
    if not items:
        return _fallback("в календаре нет ни одной бумаги индекса (строк всего %d)"
                         % len(rows))

    if with_amounts:
        sizes = {}
        for it in items:
            if it["amount_rub"] is None:
                continue
            size = sizes.get(it["ticker"]) or _issue_size(it["ticker"])
            sizes[it["ticker"]] = size
            if size:
                it["amount_bn"] = round(it["amount_rub"] * size / 1e9, 3)

    points, drag = {}, {}
    for it in items:
        if it["yield_pct"] is not None:
            points[it["ex_date"]] = round(points.get(it["ex_date"], 0.0) + it["yield_pct"], 4)
        if it.get("index_drag_pct") is not None:
            drag[it["ex_date"]] = round(drag.get(it["ex_date"], 0.0) + it["index_drag_pct"], 4)

    today = dates.fmt_date(dates.today_msk())
    ahead = sorted(d for d in drag if d >= today)
    where = {"tinvest": ("T-Invest API", BASE_TINVEST),
             "smartlab": ("календарь smart-lab", CALENDAR)}[origin]
    return series_id, points, make_meta(
        origin, where[1], points, unit="pct",
        # asof = день, КОГДА КАЛЕНДАРЬ ПРОЧИТАН, а не последняя точка ряда.
        #
        # make_meta по умолчанию берёт максимальную дату точек, и для обычного ряда
        # это верно: последнее наблюдение и есть возраст данных. Здесь точки —
        # БУДУЩИЕ отсечки, поэтому умолчание давало asof на два месяца вперёд
        # (2026-10-12 при витрине от 12.08). Фронт такую дату в подпись не пускает
        # намеренно — дату из будущего он заменяет на «нет данных», — и тайл с
        # полностью исправными числами выглядел пустым. Та же дата уезжала в блок
        # sources витрины, где означала бы «данные свежее сегодняшнего дня».
        #
        # Возраст календаря = когда мы его в последний раз прочитали: ни T-Invest,
        # ни smart-lab не сообщают, когда сам календарь обновлялся.
        asof=today,
        note="%s, отфильтрован по составу IMOEX (%d бумаг)%s" % (
            where[0], len(weights),
            "; до рекомендации СД числа прогнозные" if origin == "smartlab" else ""),
        items=sorted(items, key=lambda r: r["ex_date"]),
        index_drag_pct=drag,
        index_drag_ahead_pct=round(sum(drag[d] for d in ahead), 3) if ahead else 0.0,
        weights_source="iss_imoex_analytics",
        origin=origin, origin_note=note,
        skipped_non_index=sorted(set(skipped)))


def _fallback(reason):
    """Ручной ввод inputs/dividends.yml — резерв, когда автомат ничего не дал."""
    try:
        from . import manual as manual_mod
    except ImportError:                    # автономный запуск из каталога fetch
        try:
            from fetch import manual as manual_mod
        except ImportError:
            import manual as manual_mod
    sid, points, meta = manual_mod.dividends()
    meta = dict(meta or {})
    # Тайл читает meta["items"], а ручной загрузчик кладёт записи в "records":
    # из-за этого расхождения календарь из файла НИКОГДА не показывал тикеры —
    # тайл сваливался в фолбэк и писал «?» вместо бумаги.
    if "items" not in meta and isinstance(meta.get("records"), list):
        meta["items"] = meta["records"]
    meta["note"] = "%s; взято из inputs/dividends.yml" % reason
    meta["status"] = "manual_needed" if points else "error"
    meta["fallback_reason"] = reason
    return sid, points, meta
