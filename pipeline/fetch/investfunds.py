"""СЧА фондов денежного рынка (investfunds.ru) — «сколько денег сидит в кэше».

Ряд `lqdt_aum` (registry: cadence daily, pub_lag 1 день, SLA 4 суток).

Зачем. Ротация из фондов денежного рынка в акции — событие, которое НЕ случалось
ни разу за наблюдаемую историю (validation/VALIDATION.md §6), поэтому ряд ведётся
как ожидание первого срабатывания, а не как сигнал. Соответственно и требования
к нему мягкие: важен УРОВЕНЬ и его перелом, а не третий знак после запятой.

Грабли:
1. Состав корзины фондов — часть определения ряда. Добавили фонд — уровень
   прыгнул, и «отток» на графике окажется правкой состава. Поэтому FUNDS меняем
   только вместе с пересборкой истории (или заводим новый series_id).
2. Прирост СЧА ≠ приток. СЧА растёт сама на доходность пая (~15–17% годовых, то
   есть ~0,05% в день). Чистый приток считается через цену пая:
   flow ≈ ΔСЧА − СЧА_вчера × (пай_сегодня/пай_вчера − 1) — см. estimate_flow().
3. Счётчик на moex.com/ru/moneyfunds («1,8 трлн+») — это ВЕСЬ рынок и он
   округлён до десятых триллиона. Класть его в тот же ряд, где сумма по нашим
   фондам, нельзя: получится ступенька на ровном месте. Он живёт в meta.
4. Поиск на investfunds.ru отрабатывает скриптом (параметр srch в HTML-ответе
   игнорируется, проверено 11.08.2026), поэтому id фондов — константы с датой
   проверки, а не результат парсинга поиска.
"""

import re
from datetime import datetime, timezone

try:                                       # прод: общий HTTP-слой (CONTRACT.md §4)
    from lib.http import get_text, FetchError
except ImportError:
    try:
        from pipeline.lib.http import get_text, FetchError
    except ImportError:                    # автономный запуск (отладка парсеров)
        class FetchError(Exception):
            pass

        def get_text(url, timeout=45, headers=None, **_kw):
            import gzip
            import urllib.request
            req = urllib.request.Request(url, headers=headers or _UA)
            try:
                resp = urllib.request.urlopen(req, timeout=timeout)
                raw = resp.read()
            except OSError as exc:
                raise FetchError("%s: %s" % (url, exc))
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return raw.decode("utf-8", "replace")

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
       "Accept-Language": "ru,en;q=0.8"}

SERIES_ID = "lqdt_aum"
FUND_URL = "https://investfunds.ru/funds/%d/"
MOEX_MONEYFUNDS = "https://www.moex.com/ru/moneyfunds"

# id investfunds.ru → тикер. Проверено вручную 11.08.2026 открытием страницы.
# LQDT (БПИФ «Ликвидность», ВИМ) — крупнейший фонд денежного рынка; на его долю
# приходится большая часть рынка, поэтому корзина из одного фонда уже
# репрезентативна по ДИНАМИКЕ. Кандидаты на добавление (id подтверждены ссылками
# с карточки LQDT, но состав ряда без пересборки истории не меняем — грабля 1):
#   6423  — ВИМ «Денежный рынок. Рубли» (ОПИФ, тот же управляющий)
#   12851 — «Яндекс Пэй. Фонд денежного рынка. Рубли»
# Тикеры SBMM (Первая) и AKMM (Альфа-Капитал) на investfunds имеют другие id —
# их нужно один раз найти руками (поиск на сайте скриптовый) и внести сюда.
FUNDS = {"LQDT": 5973}


def _num(raw):
    """'736 214 358 102.65' -> float. Пробелы — разделитель разрядов."""
    txt = re.sub(r"[\s  ]", "", raw).replace(",", ".")
    try:
        return float(txt)
    except ValueError:
        return None


def _plain(html):
    body = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S)
    body = re.sub(r"<[^>]+>", " ", body)
    body = body.replace("&nbsp;", " ").replace("&#160;", " ").replace("&amp;", "&")
    return re.sub(r"[\s ]+", " ", body).strip()


def _meta(status, url, note=None, extra=None):
    meta = {"source": "investfunds", "url": url, "status": status, "note": note,
            "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    if extra:
        meta.update(extra)
    return meta


# Снимок карточки: «10.08.2026 Пай 2.06 СЧА 736 214 358 102.65».
# Группы СЧА/пая ЖАДНЫЕ: разряды отделены пробелами, и ленивый квантификатор
# обрывает число на первом же пробеле (736 вместо 736 214 358 102,65).
_SNAPSHOT = re.compile(
    r"(\d{2})\.(\d{2})\.(20\d\d)\s*Пай\s*([\d\s ,.]+?)\s*СЧА\s*([\d\s ,.]+)",
    re.I)


def parse_fund_page(html):
    """Страница фонда -> (дата ISO, цена пая, СЧА в рублях).

    Блок «Динамика стоимости пая и СЧА DD.MM.YYYY Пай X СЧА Y» дублируется на
    странице дважды (десктоп/мобильная вёрстка) — берём первое совпадение.
    """
    text = _plain(html)
    m = _SNAPSHOT.search(text)
    if not m:
        return None
    pai = _num(m.group(4))
    nav = _num(m.group(5))
    if pai is None or nav is None:
        return None
    day = "%s-%s-%s" % (m.group(3), m.group(2), m.group(1))
    return day, pai, nav


def moex_total_bln():
    """Грубый счётчик суммарной СЧА рынка с moex.com. -> (млрд руб | None, текст)."""
    try:
        text = _plain(get_text(MOEX_MONEYFUNDS, headers=_UA))
    except FetchError as exc:
        return None, str(exc)
    m = re.search(r"([\d,.]+)\s*(трлн|млрд)[^.]{0,40}?СЧА", text)
    if not m:
        m = re.search(r"СЧА[^.]{0,40}?([\d,.]+)\s*(трлн|млрд)", text)
    if not m:
        return None, "счётчик на странице не найден"
    value = _num(m.group(1))
    if value is None:
        return None, "счётчик не парсится: %r" % m.group(0)[:60]
    return (value * 1000.0 if m.group(2) == "трлн" else value), m.group(0).strip()


def money_funds():
    """-> ("lqdt_aum", {дата: суммарная СЧА, млрд руб}, meta).

    Суммируем только фонды, у которых СОВПАЛА дата снимка: смешивать вчерашнюю
    СЧА одного фонда с сегодняшней другого — рисовать несуществующий отток.
    """
    snaps, failed = {}, []
    for ticker, fund_id in sorted(FUNDS.items()):
        url = FUND_URL % fund_id
        try:
            parsed = parse_fund_page(get_text(url, headers=_UA))
        except FetchError as exc:
            failed.append("%s: %s" % (ticker, exc))
            continue
        if not parsed:
            failed.append("%s: блок «Пай … СЧА …» не найден" % ticker)
            continue
        day, pai, nav = parsed
        snaps[ticker] = {"day": day, "pai": pai, "nav_rub": nav}
    moex_bln, moex_note = moex_total_bln()
    if not snaps:
        return SERIES_ID, {}, _meta("error", FUND_URL % FUNDS.get("LQDT", 0),
                                    "; ".join(failed) or "фонды не прочитались",
                                    {"moex_total_bln": moex_bln,
                                     "moex_note": moex_note})
    day = max(s["day"] for s in snaps.values())
    used = {t: s for t, s in snaps.items() if s["day"] == day}
    skipped = [t for t in snaps if t not in used]
    total_bln = round(sum(s["nav_rub"] for s in used.values()) / 1e9, 3)
    note = "фондов в корзине: %d (%s)" % (len(used), ", ".join(sorted(used)))
    if skipped:
        note += "; пропущены с другой датой: %s" % ", ".join(sorted(skipped))
    return SERIES_ID, {day: total_bln}, _meta(
        "ok", FUND_URL % FUNDS["LQDT"], note,
        {"asof": day, "funds": snaps, "failed": failed,
         "moex_total_bln": moex_bln, "moex_note": moex_note,
         "coverage_note": "ряд = сумма по FUNDS, а не весь рынок; счётчик МосБиржи "
                          "приведён отдельно и в ряд НЕ подмешивается"})


def money_funds_all():
    """[(series_id, points, meta)] — СЧА и средневзвешенная цена пая.

    Цена пая нужна, чтобы отделить приток от начисленной доходности
    (estimate_flow). В registry этого ряда пока нет — это TODO интеграции.
    """
    sid, points, meta = money_funds()
    out = [(sid, points, meta)]
    funds = meta.get("funds") or {}
    day = meta.get("asof")
    if day and funds:
        nav_sum = sum(f["nav_rub"] for f in funds.values() if f["day"] == day)
        if nav_sum > 0:
            weighted = sum(f["pai"] * f["nav_rub"] for f in funds.values()
                           if f["day"] == day) / nav_sum
            out.append(("lqdt_pai", {day: round(weighted, 6)},
                        _meta(meta["status"], meta["url"],
                              "средневзвешенная по СЧА цена пая", {"asof": day})))
    return out


def estimate_flow(nav_prev, nav_now, pai_prev, pai_now):
    """Чистый приток за день, в тех же единицах, что и СЧА.

    Вычитаем доходность самого фонда: без этого спокойный день с нулевым притоком
    выглядит как приток в 0,05% активов, а за месяц набегает «+1%» из воздуха.
    """
    if None in (nav_prev, nav_now, pai_prev, pai_now) or pai_prev == 0:
        return None
    return round(nav_now - nav_prev * (pai_now / pai_prev), 4)
