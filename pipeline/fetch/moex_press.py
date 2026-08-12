"""Ежемесячный пресс-релиз МосБиржи про частных инвесторов (ряд `moex_retail`).

Что в ряду: число активных клиентов месяца (млн человек, «сделки заключали …»),
а в meta — полный расклад: приток в акции/облигации/фонды, всего клиентов, доля
физлиц в обороте и «Народный портфель» (топ-10 бумаг с долями).

Зачем. Слой 3, мониторинг. Валидация прямо предупреждает: «физики — не
константа» (VALIDATION.md §7.4) — в капитуляции 2024 розница была крупнейшим
нетто-ПРОДАВЦОМ года, а в 2026 выкупала падение. Поэтому ряд нужен как описание
состава рынка, а не как контрарианский сигнал.

Грабля: страница moex.com/ru/news рисуется скриптом (в HTML нет ни одной ссылки
на новости, проверено 11.08.2026). Тот же контент отдаёт ISS —
iss.moex.com/iss/sitenews.json (список) и /iss/sitenews/{id}.json (текст с
числами). Идём через ISS: это официальное зеркало сайта, а не сторонний агрегатор.
Вторая грабля: заголовки релиза каждый месяц разные («Частные инвесторы в 1,9
раза увеличили вложения…»), поэтому кандидатов ищем по заголовку, а подтверждаем
по ТЕЛУ (в нём обязаны быть «количество частных инвесторов» и «народный портфель»).
"""

import re
from datetime import date, datetime, timedelta, timezone

try:                                       # прод: общий HTTP-слой (CONTRACT.md §4)
    from lib.http import get_json, FetchError
except ImportError:
    try:
        from pipeline.lib.http import get_json, FetchError
    except ImportError:                    # автономный запуск (отладка парсеров)
        class FetchError(Exception):
            pass

        def get_json(url, timeout=45, **_kw):
            import json
            import urllib.request
            req = urllib.request.Request(url, headers={"User-Agent": "moex-radar/1.0"})
            try:
                return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
            except (OSError, ValueError) as exc:
                raise FetchError("%s: %s" % (url, exc))

SERIES_ID = "moex_retail"
NEWS_LIST = "https://iss.moex.com/iss/sitenews.json?start=%d"
NEWS_ITEM = "https://iss.moex.com/iss/sitenews/%d.json"
PAGE_SIZE = 100
# Глубина обхода ленты: 45 суток покрывают месячный цикл релиза с запасом, потолок
# в 20 страниц (2000 новостей) держит худший случай в разумных двух секундах.
MAX_AGE_DAYS = 45
MAX_PAGES = 20

_TITLE_RE = re.compile(r"частн\w+\s+инвестор", re.I)
_MONTHS = {"январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
           "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11,
           "декабр": 12}


def _num(raw):
    txt = re.sub(r"[\s  ]", "", str(raw)).rstrip(".,").replace(",", ".")
    try:
        return float(txt)
    except ValueError:
        return None


def _plain(html):
    body = re.sub(r"<[^>]+>", " ", str(html))
    body = (body.replace("&nbsp;", " ").replace("&ndash;", "–")
                .replace("&mdash;", "—").replace("&amp;", "&").replace("&quot;", '"'))
    return re.sub(r"[\s ]+", " ", body).strip()


def _meta(status, url, note=None, extra=None):
    meta = {"source": "moex_iss_sitenews", "url": url, "status": status, "note": note,
            "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    if extra:
        meta.update(extra)
    return meta


def _month_end(year, month):
    nxt = (year + 1, 1) if month == 12 else (year, month + 1)
    return (date(nxt[0], nxt[1], 1) - timedelta(days=1)).isoformat()


def _period(text, published):
    """'…в июле 2026 года…' -> ('2026-07-31', '2026-07').

    Если месяц в тексте без года (частый случай в заголовке), год берём из даты
    публикации, а при январском релизе за декабрь откатываем год назад.
    """
    low = text.lower()
    m = re.search(r"в\s+([а-яё]{3,9})\s+(20\d\d)\s*г", low) or \
        re.search(r"([а-яё]{3,9})\s+(20\d\d)\s*года", low)
    year = month = None
    if m:
        month, year = _month_num(m.group(1)), int(m.group(2))
    if month is None:
        m2 = re.search(r"(?:по итогам|в|за)\s+([а-яё]{3,9})", low)
        month = _month_num(m2.group(1)) if m2 else None
        if month is not None and published:
            year = int(published[:4])
            if month == 12 and published[5:7] == "01":
                year -= 1
    if not month or not year:
        return None, None
    return _month_end(year, month), "%04d-%02d" % (year, month)


def _month_num(word):
    word = (word or "").lower()
    for stem, num in _MONTHS.items():
        if word.startswith(stem):
            return num
    return None


def _find_number(text, patterns):
    for pattern in patterns:
        m = re.search(pattern, text, re.I)
        if m:
            value = _num(m.group(1))
            if value is not None:
                return value
    return None


def parse_retail(text):
    """Текст релиза -> словарь показателей (None там, где формулировка изменилась)."""
    out = {
        "inflow_total_bln": _find_number(text, [
            r"инвестиции физических лиц[^.]{0,120}?составили\s*([\d\s,.]+?)\s*млрд"]),
        "inflow_equity_bln": _find_number(text, [
            r"вложения[^.]{0,60}?в акции[^.]{0,80}?составили\s*([\d\s,.]+?)\s*млрд",
            r"в акции[^.]{0,60}?составил[аи]?\s*([\d\s,.]+?)\s*млрд"]),
        "inflow_bonds_bln": _find_number(text, [
            r"в облигации[^.]{0,80}?составил\D{0,20}?([\d\s,.]+?)\s*млрд"]),
        "inflow_funds_bln": _find_number(text, [
            r"в паевые инвестиционные фонды\D{0,20}?([\d\s,.]+?)\s*млрд"]),
        "clients_total_mln": _find_number(text, [
            r"количество частных инвесторов[^.]{0,140}?составило\s*([\d\s,.]+?)\s*млн"]),
        "clients_added_k": _find_number(text, [
            r"\(\+\s*([\d\s,.]+?)\s*тыс"]),
        "active_mln": _find_number(text, [
            r"сделки[^.]{0,60}?заключали[^.]{0,40}?([\d\s,.]+?)\s*млн"]),
        "share_equity_pct": _find_number(text, [
            r"доля физлиц в объеме торгов акциями[^.]{0,60}?составила\s*([\d\s,.]+?)\s*%",
            r"в объеме торгов акциями[^.]{0,40}?([\d\s,.]+?)\s*%"]),
        "portfolio": parse_portfolio(text),
    }
    return out


def parse_portfolio(text):
    """«Народный портфель»: [{name, share_pct}] в порядке упоминания.

    У Сбербанка две доли в одной скобке («31,2% и 7,1% соответственно») — вторая
    относится к привилегированным акциям, поэтому пара разворачивается в две
    записи, иначе топ-10 превратится в топ-9.
    """
    m = re.search(r"народн\w+\s+портфел\w+(.{0,700}?)(?:\.|$)", text, re.I | re.S)
    if not m:
        return []
    chunk = m.group(1)
    out = []
    for item in re.finditer(r"([^,()]{2,60}?)\s*\(([\d,.]+)\s*%(?:\s*и\s*([\d,.]+)\s*%)?",
                            chunk):
        # Отрезаем хвост предыдущей фразы: имя эмитента — это последние слова с
        # заглавной («…вошли обыкновенные и привилегированные акции Сбербанка»).
        tokens = item.group(1).strip(" –—-").split()
        while tokens and not (tokens[0][:1].isupper() or tokens[0][:1].isdigit()):
            tokens.pop(0)
        name = " ".join(tokens)
        first = _num(item.group(2))
        if name and first is not None:
            out.append({"name": name, "share_pct": first})
            if item.group(3):
                second = _num(item.group(3))
                if second is not None:
                    out.append({"name": name + " (прив.)", "share_pct": second})
    return out[:12]


def _candidates(max_pages=MAX_PAGES, max_age_days=MAX_AGE_DAYS):
    """id новостей-кандидатов (свежие сверху) + ошибки списка.

    Глубина обхода считается ПО ДАТАМ, а не по числу страниц, и вот почему. Раньше
    здесь стояло `scan_pages=3` — триста новостей. МосБиржа публикует около сотни
    новостей в СУТКИ (риск-параметры, регистрации выпусков, депозитные аукционы),
    то есть три страницы — это примерно трое суток ленты. Релиз про частных
    инвесторов месячный: в окно он попадал только если прогон случался в те самые
    двое-трое суток после публикации, а в остальные дни фетчер честно докладывал
    «релизов не нашлось» и ряд не собрался ни разу (замер 12.08.2026: в ленте лежит
    релиз от 08.07, до него 15 страниц).

    Потолок max_pages остаётся: если ISS однажды начнёт отдавать даты мусором,
    обход обязан кончиться, а не листать ленту до 2014 года.
    """
    ids, errors = [], []
    newest = None          # верх ленты: от него, а не от «сегодня», считается возраст
    for page in range(max(1, int(max_pages))):
        try:
            data = get_json(NEWS_LIST % (page * PAGE_SIZE))
        except FetchError as exc:
            errors.append(str(exc))
            break
        block = (data.get("sitenews") or {})
        rows = block.get("data") or []
        if not rows:
            break
        # Колонки ищем по ИМЕНИ: у ISS схема блоков плавает между эндпоинтами, и
        # позиционный доступ однажды тихо подставит дату вместо заголовка.
        idx = {name: n for n, name in enumerate(block.get("columns") or [])}
        pos_id = idx.get("id", 0)
        pos_title = idx.get("title", 2)
        pos_date = idx.get("published_at", 3)
        oldest = None
        for row in rows:
            title = str(row[pos_title]) if len(row) > pos_title else ""
            published = str(row[pos_date])[:10] if len(row) > pos_date else None
            newest = newest or published
            oldest = published or oldest
            if _TITLE_RE.search(title):
                try:
                    ids.append((int(row[pos_id]), title, published))
                except (TypeError, ValueError):
                    continue
        if _age_days(newest, oldest) > max_age_days:
            break
    return ids, errors


def _age_days(newest, oldest):
    """Насколько лента уже отлистана назад, в сутках. -1, если дат нет.

    Точка отсчёта — САМАЯ СВЕЖАЯ новость ленты, а не «сегодня»: тест с замороженной
    фикстурой иначе был бы зелёным ровно до того дня, когда перестанет им быть
    (правило набора №1, tests/__init__.py).
    """
    if not newest or not oldest:
        return -1
    try:
        return (date.fromisoformat(newest) - date.fromisoformat(oldest)).days
    except ValueError:
        return -1


def retail(scan_pages=MAX_PAGES, max_open=4):
    """-> ("moex_retail", {последний день месяца: активных клиентов, млн}, meta).

    Скаляром в ряду — активные клиенты (сколько людей реально торговали): это
    единственная величина релиза, которая описывает участие, а не накопленный
    маркетинговый счёт открытых счетов. Всё остальное — в meta["payload"].
    """
    ids, errors = _candidates(scan_pages)
    if not ids:
        return SERIES_ID, {}, _meta("error", NEWS_LIST % 0,
                                    "; ".join(errors) or "релизов про частных "
                                    "инвесторов не нашлось в ленте ISS")
    tried = []
    for news_id, title, published in ids[:max_open]:
        try:
            data = get_json(NEWS_ITEM % news_id)
        except FetchError as exc:
            tried.append("%s: %s" % (news_id, exc))
            continue
        block = (data.get("content") or {})
        rows = block.get("data") or []
        if not rows:
            continue
        columns = [c.lower() for c in block.get("columns") or []]
        row = rows[0]
        body = _plain(row[columns.index("body")] if "body" in columns else row[-1])
        low = body.lower()
        if "количество частных инвесторов" not in low or "народн" not in low:
            tried.append("%s: не месячный релиз (%s)" % (news_id, title[:60]))
            continue
        payload = parse_retail(body)
        day, month = _period(title + " " + body, published)
        if not day:
            tried.append("%s: не определился месяц" % news_id)
            continue
        value = payload.get("active_mln")
        points = {day: value} if value is not None else {}
        status = "ok" if points else "stale"
        return SERIES_ID, points, _meta(
            status, "https://www.moex.com/n%d" % news_id, title,
            {"asof": month, "published": published, "payload": payload,
             "news_id": news_id, "tried": tried,
             "value_note": "точка = активные клиенты месяца, млн человек"})
    return SERIES_ID, {}, _meta("error", NEWS_LIST % 0,
                                "; ".join(tried + errors) or "релиз не разобрался")
