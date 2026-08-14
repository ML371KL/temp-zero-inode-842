"""Как панель разговаривает наружу: имена, числа, сборка сообщения.

ЗАЧЕМ ОТДЕЛЬНЫЙ МОДУЛЬ. Сообщение в телеграме читает человек, у которого перед
глазами нет ни панели, ни руководства. До 14.08.2026 туда уезжала внутренняя кухня
дословно: «Смена ячейки: bear|stress|ok → bear|stress|stress», «Ядро развернулось»,
«Облигационный флаг ВКЛЮЧЁН», «hit 0.64», «dd<−10%», «статус dead», «Лиз писателя
потерян». Каждое из этих слов имеет точный смысл ВНУТРИ проекта и никакого — снаружи.

Показательно, что промпт комментатора (`lib/commentary.py`) прямым текстом запрещает
модели употреблять «композит, ядро, ячейку, слои, мониторы». То есть у панели уже
было правило «не говорить жаргоном» — но применялось оно только к ИИ-комментарию,
а сам факт над ним писался жаргоном.

ОБРАЗЕЦ — панели 837 и 838, где этот шлюз наружу построен раньше и работает:
внутреннее имя карточки во внешний текст не попадает вовсе, вместо него идёт
нормальное имя плюс одна фраза о том, что величина значит. Здесь то же самое.
"""

import re
from datetime import date, datetime

__all__ = ["esc", "num", "pct", "signed", "ru_day", "ru_month", "plural",
           "ru_decimals", "hours_minutes", "sentence", "regime_name", "points",
           "KIND", "OPS_KIND", "cell_words", "cell_plain", "render_market",
           "render_ops", "plain_text"]

TG_LIMIT = 4000          # предел телеграма 4096; запас на служебные хвосты
MINUS = "−"         # типографский минус: в тексте рядом стоят дефисы переносов


def esc(value):
    """Экранирование под parse_mode=HTML. Экранируются ДАННЫЕ, не наша разметка."""
    return (str("" if value is None else value)
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def num(value, digits=1, plus=False, dash="н/д"):
    """Число по-русски: запятая, неразрывный пробел в тысячах, типографский минус.

    До 14.08.2026 сообщения писали «+0.66» и «14.00%» — точкой, тогда как сама
    панель и соседние 837/838 пишут «+0,66». В одном сообщении рядом оказывались
    «+1.4%/мес» и «+1,4%/мес», и это читается как две разные величины.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return dash
    text = f"{value:{'+' if plus else ''},.{digits}f}"
    return (text.replace(",", " ").replace(".", ",")
                .replace("-", MINUS).replace("+", "+"))


def pct(value, digits=1, plus=False, dash="н/д"):
    out = num(value, digits, plus, dash)
    return out if out == dash else out + "%"


def signed(value, digits=2, dash="н/д"):
    return num(value, digits, plus=True, dash=dash)


_DEC = re.compile(r"(?<=\d)\.(?=\d)")


def ru_decimals(text):
    """Числа в ЧУЖОЙ строке к русскому виду: 12.3 -> 12,3, -5 -> −5.

    Нужна там, где в сообщение вклеивается готовая подпись тайла или подсказка
    состояния: их собирает свой код, и без нормализации в одном сообщении рядом
    оказываются «12.3 млрд» и «−37,9 млрд».
    """
    out = _DEC.sub(",", str(text or ""))
    return re.sub(r"(?<![\w\u2212])-(?=\d)", MINUS, out)


def ru_day(value, with_year=True):
    """'2026-07-15' -> '15.07.2026'. Дату в сообщении читает человек, а не машина."""
    raw = str(value or "")[:10]
    try:
        when = date.fromisoformat(raw)
    except ValueError:
        return raw or "н/д"
    return when.strftime("%d.%m.%Y" if with_year else "%d.%m")


_MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля",
           "августа", "сентября", "октября", "ноября", "декабря")
_MONTHS_NOM = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль",
               "август", "сентябрь", "октябрь", "ноябрь", "декабрь")


def ru_month(value, nominative=True):
    """'2026-07' или '2026-07-31' -> 'июль 2026'."""
    raw = str(value or "")
    match = re.match(r"(\d{4})-(\d{2})", raw)
    if not match:
        return raw or "н/д"
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        return raw
    names = _MONTHS_NOM if nominative else _MONTHS
    return f"{names[month - 1]} {year}"


def plural(count, one, few, many):
    """Русское склонение. Мелочь, без которой текст выглядит машинным переводом."""
    try:
        n = abs(int(count))
    except (TypeError, ValueError):
        return many
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def hours_minutes(minutes):
    """95 -> '1 ч 35 мин'. Возраст в минутах читается плохо начиная с часа."""
    try:
        total = int(round(float(minutes)))
    except (TypeError, ValueError):
        return "н/д"
    if total < 60:
        return f"{total} мин"
    return f"{total // 60} ч {total % 60} мин"


# --------------------------------------------------------------- виды событий
#
# Эмодзи несёт вид события, а не эмоцию: читатель на телефоне отличает их раньше,
# чем прочитает заголовок. Набор и роль — те же, что у 837/838.
KIND = {
    "core_flip":         {"emoji": "🎯", "label": "смена оценки рынка"},
    "state_cell_change": {"emoji": "🔀", "label": "смена режима рынка"},
    "bond_flag_on":      {"emoji": "⚠️", "label": "сигнал риска"},
    "bond_flag_off":     {"emoji": "✅", "label": "сигнал риска снят"},
    "buy_window_open":   {"emoji": "🎯", "label": "окно входа"},
    "cb_reminder":       {"emoji": "📅", "label": "событие календаря"},
    "cb_decision":       {"emoji": "📊", "label": "решение Банка России"},
    "orfr_published":    {"emoji": "📊", "label": "вышли новые данные"},
    "auction_failed":    {"emoji": "⚠️", "label": "сигнал риска"},
    "deposit_uptick":    {"emoji": "📊", "label": "вышли новые данные"},
}
DEFAULT_KIND = {"emoji": "📊", "label": "событие"}

# Санитарные виды: те же три состояния, что у общего мостика панелей
# (`/usr/local/sbin/dash-notify`): 🔴 сломалось, 🟡 подозрительно, 🟢 починилось.
OPS_KIND = {
    "source_stale":      "🟡",
    "health_dead":       "🔴",
    "health_review_due": "🟡",
    "core_missing":      "🔴",
    "lease_lost":        "🟡",
    "payload_oversize":  "🟡",
}


# ------------------------------------------------------- имена состояний рынка
#
# Ключ ячейки — три бита: тренд, волатильность, облигации. Внутри он пишется
# «bear|stress|stress», и в таком виде уезжал в телеграм. Снаружи это набор букв.
BITS = {
    "trend": {1: "растущий рынок", 0: "падающий рынок"},
    "vol": {1: "нервная торговля", 0: "спокойная торговля"},
    "bond": {1: "ОФЗ под давлением", 0: "ОФЗ спокойны"},
}
_CODE_WORDS = {"bull": ("trend", 1), "bear": ("trend", 0),
               "stress": (None, 1), "calm": ("vol", 0), "ok": (None, 0)}


def cell_words(code):
    """'bear|stress|stress' -> 'падающий рынок · нервная торговля · ОФЗ под давлением'."""
    parts = str(code or "").split("|")
    if len(parts) != 3:
        return str(code or "н/д")
    trend = BITS["trend"][1] if parts[0] == "bull" else BITS["trend"][0]
    vol = BITS["vol"][1] if parts[1] == "stress" else BITS["vol"][0]
    bond = BITS["bond"][1] if parts[2] == "stress" else BITS["bond"][0]
    return f"{trend} · {vol} · {bond}"


def sentence(text):
    """Строка сообщения — самостоятельное предложение: с большой буквы и с точкой.

    Куски, приходящие из тайлов и подсказок состояния, писались как продолжение
    чужой фразы («просадка RGBI −1,2% от максимума», «продавец выдыхается»). Стоя
    отдельной строкой, они выглядят обрывками.
    """
    out = str(text or "").strip()
    if not out:
        return ""
    out = out[0].upper() + out[1:]
    return out if out[-1] in ".!?…:" else out + "."


# Внешние имена восьми режимов. Тот же приём, что словарь HUMAN у 837: наружу идёт
# не подпись из таблицы модели, а нормальное имя. Внутри состояния называются
# ЯЧЕЙКАМИ таблицы, и у одного слово попало прямо в подпись («токсичная ячейка») —
# читатель телеграма про таблицу не знает. Правило пополнения то же: новый режим —
# новая строка здесь, иначе наружу уедет внутренняя подпись.
REGIME_NAMES = {
    "рабочий режим": "обычный рабочий режим",
    "бык с долговой тенью": "рост акций при слабых ОФЗ",
    "окно входа": "окно входа",
    "перегрев на стрессе": "перегрев на стрессе",
    "вялый медведь": "вялое снижение",
    "медведь с долговой тенью": "снижение акций при слабых ОФЗ",
    "токсичная ячейка": "худшее сочетание из возможных",
}


def regime_name(label):
    """Подпись режима человеческим языком."""
    out = str(label or "").strip()
    if not out:
        return ""
    known = REGIME_NAMES.get(out.lower())
    if known:
        return known
    for word in (" ячейка", " ячейку", " ячейки"):
        if out.endswith(word):
            out = out[: -len(word)]
    return out.strip()


def points(value, digits=1):
    """«0,40 процентного пункта» / «3 процентных пункта» — с верным склонением.

    У дробных чисел русский требует родительного единственного («8,1 процентного
    пункта»), а не множественного, которое даёт счёт по целой части.
    """
    body = num(abs(value), digits)
    if abs(value - round(value)) > 1e-9:
        return f"{body} процентного пункта"
    return f"{body} " + plural(round(abs(value)), "процентного пункта",
                               "процентных пункта", "процентных пунктов")


def cell_plain(stats):
    """Что режим значил в прошлом — словами и без внутренних сокращений.

    Раньше сюда уезжало «исторически −2.9%/мес (n=25, hit 0.56)». «n» и «hit» —
    обозначения из таблицы, а среднее по ячейке ещё и хвостовая величина: читатель
    принимал его за прогноз на месяц. Поэтому наружу идут медиана (типичный месяц),
    доля плюсовых и худший случай — теми же словами, что на самой панели.
    """
    if not isinstance(stats, dict) or not stats:
        return ""
    median = stats.get("median_fwd1m_pct")
    worst = stats.get("worst_pct")
    hit = stats.get("hit")
    count = stats.get("n")
    bits = []
    if isinstance(median, (int, float)):
        bits.append(f"типичный месяц {pct(median, 1, plus=True)}")
    if isinstance(hit, (int, float)):
        bits.append(f"в плюс закрывались {round(hit * 100)}%")
    if isinstance(worst, (int, float)):
        bits.append(f"худший {pct(worst, 1, plus=True)}")
    if not bits:
        return ""
    tail = (f" ({count} {plural(count, 'месяц', 'месяца', 'месяцев')} истории)"
            if isinstance(count, int) else "")
    return "Так было раньше: " + ", ".join(bits) + tail + "."


# ------------------------------------------------------------- сборка сообщения

def _cut(text, limit=TG_LIMIT):
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render_market(event):
    """Рыночное событие -> HTML для телеграма.

    Порядок блоков тот же, что у 837/838, и он не случаен: сначала ЧТО (заголовок),
    потом НАСКОЛЬКО (было → стало), потом подробности, и только затем разбор модели
    за отбивкой. Читатель, пролиставший ленту, получает смысл из первой строки.
    """
    kind = KIND.get(event.get("kind"), DEFAULT_KIND)
    title = event.get("title") or event.get("text") or ""
    lines = [f"{kind['emoji']} <b>{esc(title)}</b>"]

    before, after = event.get("before"), event.get("after")
    if before or after:
        lines.append(f"{esc(before or '—')} → <b>{esc(after or '—')}</b>")

    for move in event.get("moves") or []:
        row = f"• {esc(move.get('name'))}: {esc(move.get('before'))} → <b>{esc(move.get('after'))}</b>"
        if move.get("note"):
            row += f" <i>({esc(move['note'])})</i>"
        lines.append(row)

    if event.get("detail"):
        lines.append(esc(event["detail"]))
    if event.get("meaning"):
        lines.append(esc(event["meaning"]))

    # Разбор модели отбивается ПУСТОЙ СТРОКОЙ и меткой 💬 — ровно как у 837/838,
    # чтобы в общей ленте хаба разбор у всех панелей выглядел одинаково.
    comment = (event.get("comment") or "").strip()
    if comment:
        lines += ["", "💬 " + esc(comment)]
    return _cut("\n".join(lines))


def render_ops(event, panel="842"):
    """Санитарное событие -> HTML в том же виде, что шлёт общий мостик панелей.

    Формат подсмотрен не на глаз, а взят у `/usr/local/sbin/dash-notify`, через
    который об отказах сообщают 837, 838 и 839: значок состояния, жирный заголовок
    вида «842 · что сломалось», затем тело из трёх строк — ФАКТ, ЧТО ЭТО ЗНАЧИТ и
    КУДА СМОТРЕТЬ. Последняя строка обязательна: сообщение о поломке без адреса
    поломки заставляет искать заново каждый раз.
    """
    icon = OPS_KIND.get(event.get("kind"), "🟡")
    title = event.get("title") or event.get("kind") or "отказ"
    lines = [f"{icon} <b>{esc(panel)} · {esc(title)}</b>"]
    for key in ("fact", "meaning", "where"):
        if event.get(key):
            lines.append(esc(event[key]))
    return _cut("\n".join(lines))


def plain_text(event):
    """Тот же факт без разметки — для журнала витрины и ленты хаба.

    Журнал показывает событие как строку ленты, поэтому разметка ему не нужна, а
    вот полнота нужна: заголовок и подробности склеиваются в одну фразу.
    """
    parts = [event.get("title") or ""]
    before, after = event.get("before"), event.get("after")
    if before or after:
        parts.append(f"{before or '—'} → {after or '—'}.")
    for move in event.get("moves") or []:
        parts.append(f"{move.get('name')}: {move.get('before')} → {move.get('after')}.")
    # Санитарные поля тоже: у отказов обвязки нет before/after, весь факт лежит
    # в `fact`, и без него плоский текст выходил без единого числа.
    for key in ("fact", "detail", "meaning", "where"):
        if event.get(key):
            parts.append(event[key])
    text = " ".join(p.strip() for p in parts if p)
    return re.sub(r"\s+", " ", text).strip()
