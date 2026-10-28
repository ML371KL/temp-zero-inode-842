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
from datetime import date, datetime, timedelta, timezone

from pipeline.lib import constants

__all__ = ["esc", "num", "pct", "signed", "ru_day", "ru_month", "plural",
           "ru_decimals", "hours_minutes", "msk_when", "sentence", "regime_name", "points",
           "KIND", "OPS_KIND", "cell_words", "cell_plain", "render_market",
           "render_ops", "plain_text", "pp", "bp", "ru_date", "emoji", "footer",
           "PANEL_LABEL", "PANEL_URL"]

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
    # Единый стандарт сообщений (29.10.2026): разряды — неразрывным пробелом с
    # 10 000, четырёхзначные слитно («2391,72», а не «2 391,72»). Порог — по
    # округлённому числу: 9999,96 с одним знаком — это уже «10 000,0».
    rounded = round(float(value), digits)
    if rounded == 0:
        rounded = 0.0          # «−0,00» после округления читается как падение
    group = "," if abs(rounded) >= 10000 else ""
    text = f"{rounded:{'+' if plus else ''}{group}.{digits}f}"
    return text.replace(",", NBSP).replace(".", ",").replace("-", MINUS)


NBSP = "\u00a0"          # неразрывный пробел: телеграм не рвёт по нему число и единицу


def pct(value, digits=1, plus=False, dash="н/д"):
    out = num(value, digits, plus, dash)
    return out if out == dash else out + "%"


def pp(value, digits=2, plus=True, dash="н/д"):
    """Процентные пункты: «+0,06 п.п.» — знак у изменения всегда, единица через
    неразрывный пробел."""
    out = num(value, digits, plus, dash)
    return out if out == dash else out + NBSP + "п.п."


def bn_rub(value, digits=1, plus=False, dash="н/д"):
    """Миллиарды рублей: «−7,1 млрд ₽» — единицы через неразрывный пробел."""
    out = num(value, digits, plus, dash)
    return out if out == dash else out + NBSP + "млрд" + NBSP + "₽"


def bp(value, plus=True, dash="н/д"):
    """Базисные пункты: «−25 б.п.»."""
    out = num(value, 0, plus, dash)
    return out if out == dash else out + NBSP + "б.п."


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


def ru_date(value, ref=None):
    """Дата для сообщения: «15.10» в год `ref` (момента события), «15.10.2025» — для
    прошлых лет. Без `ref` год пишется всегда: угадывать «текущий» по часам машины
    модуль не берётся."""
    raw = str(value or "")[:10]
    try:
        when = date.fromisoformat(raw)
    except ValueError:
        return raw or "н/д"
    if isinstance(ref, datetime):
        ref_local = (ref if ref.tzinfo else ref.replace(tzinfo=timezone.utc)).astimezone(MSK)
        if ref_local.year == when.year:
            return when.strftime("%d.%m")
    return when.strftime("%d.%m.%Y")


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
    if total % 60 == 0:
        return f"{total // 60} ч"
    return f"{total // 60} ч {total % 60} мин"


MSK = timezone(timedelta(hours=constants.MSK_OFFSET_HOURS))


def msk_when(moment, ref=None):
    """Момент по Москве для читателя: «17:05 МСК» в тот же день, что `ref`, иначе
    «13.10, 17:05 МСК»; год — только у прошлых лет. Принимает datetime или ISO-строку."""
    if not isinstance(moment, datetime):
        try:
            moment = datetime.fromisoformat(str(moment).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return "н/д"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    local = moment.astimezone(MSK)
    clock = local.strftime("%H:%M") + " МСК"
    if isinstance(ref, datetime):
        ref_local = (ref if ref.tzinfo else ref.replace(tzinfo=timezone.utc)).astimezone(MSK)
        if ref_local.date() == local.date():
            return clock
        day = local.strftime("%d.%m" if ref_local.year == local.year else "%d.%m.%Y")
    else:
        day = local.strftime("%d.%m")
    return f"{day}, {clock}"


# --------------------------------------------------------------- виды событий
#
# Эмодзи — одна в начале заголовка, по словарю единого стандарта сообщений
# (TELEGRAM-STYLE-2026-10-29, §4): 🔔 смена позиции или режима — главное событие,
# 📈/📉 движение показателя строго по направлению, ⚠️ риск, 🗓 календарь и выход
# статистики. Направление у 📈/📉 несёт поле события `direction` ("up"/"down"):
# у разворота оценки, решения ЦБ и вкладов оно своё в каждом событии.
KIND = {
    # Позиция — итог ворот и наклона, отдельный слой поверх обоих: своё событие,
    # в семейство режима не сливается (alerts.REGIME_FAMILY).
    "position_change":   {"emoji": "🔔", "label": "смена позиции"},
    "core_flip":         {"emoji": "📈", "label": "смена оценки рынка"},
    "state_cell_change": {"emoji": "🔔", "label": "смена признаков рынка"},
    "bond_flag_on":      {"emoji": "⚠️", "label": "сигнал риска"},
    # Снятие облигационного стресса — подъём RGBI над порогом: направление вверх.
    "bond_flag_off":     {"emoji": "📈", "label": "сигнал риска снят"},
    "buy_window_open":   {"emoji": "🔔", "label": "окно входа"},
    "cb_reminder":       {"emoji": "🗓", "label": "событие календаря"},
    "cb_decision":       {"emoji": "📈", "label": "решение Банка России"},
    # Сохранение ставки — то же заседание, но без движения: значок календаря.
    "cb_hold":           {"emoji": "🗓", "label": "решение Банка России"},
    "orfr_published":    {"emoji": "🗓", "label": "вышли новые данные"},
    "auction_failed":    {"emoji": "⚠️", "label": "сигнал риска"},
    "deposit_uptick":    {"emoji": "📈", "label": "вышли новые данные"},
}
DEFAULT_KIND = {"emoji": "ℹ️", "label": "событие"}
DIRECTION_EMOJI = {"up": "📈", "down": "📉"}

# Пометка панели — первым словом заголовка в каждом сообщении (стандарт §1): 837 и
# 838 пишут одним ботом, ops-канал общий у всех панелей. Имя — как на карточке хаба.
PANEL_LABEL = "Рынок РФ"
PANEL_URL = "https://tzi-842.pages.dev"
# Досылка из очереди повторов: событие старше этого к моменту отправки помечается ⏳.
LATE_AFTER_MIN = 30


_FIRST_NUM = re.compile(r"[+−-]?\d+(?:[.,]\d+)?")


def _first_num(text):
    match = _FIRST_NUM.search(str(text or ""))
    if not match:
        return None
    try:
        return float(match.group(0).replace("−", "-").replace(",", "."))
    except ValueError:
        return None


def emoji(event):
    """Значок события: направление, если оно у события есть, иначе значок вида.

    У события без поля `direction` (лежало в очереди повторов до 29.10.2026)
    направление выводится из «было → стало»: значок обязан совпадать с движением,
    «📈» у снижения ставки — брак (стандарт §4).
    """
    kind = KIND.get(event.get("kind"), DEFAULT_KIND)
    if kind["emoji"] not in DIRECTION_EMOJI.values():
        return kind["emoji"]
    direction = event.get("direction")
    if direction not in DIRECTION_EMOJI:
        old, new = _first_num(event.get("before")), _first_num(event.get("after"))
        if old is not None and new is not None and old != new:
            direction = "up" if new > old else "down"
    return DIRECTION_EMOJI.get(direction, kind["emoji"])

# Санитарные виды: те же три состояния, что у общего мостика панелей
# (`/usr/local/sbin/dash-notify`): 🔴 сломалось, 🟡 подозрительно, 🟢 починилось.
OPS_KIND = {
    "source_stale":      "🟡",
    # Разрядка тревоги. Своим значком, а не жёлтым по умолчанию: отбивка,
    # выглядящая как предупреждение, не снимает тревогу, а добавляет её.
    "source_ok":         "🟢",
    # health_review заменил health_dead + health_review_due (аудит 02.09.2026): это
    # не поломка, а достигнутый порог плановой ревалидации — поэтому жёлтый.
    "health_review":     "🟡",
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
    # Выборка называется та, на которой посчитаны сами числа. Медиана, доля
    # плюсовых и худший месяц берутся по ЗАКРЫТЫМ месяцам (n_closed), а `n` —
    # число пар исследования: у токсичной ячейки это 24 против 25, и «25 месяцев
    # истории» рядом с долей, посчитанной по 24, — ровно та подмена выборки,
    # из-за которой долю однажды уже переписали неверно (constants.CELL_STATS).
    count = stats.get("n_closed", stats.get("n"))
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
#
# Каркас — единый стандарт сообщений всех панелей (TELEGRAM-STYLE-2026-10-29, §1):
#
#   <эмодзи> <b>Рынок РФ · Заголовок</b>
#   было → <b>стало</b>
#   подробность, что это значит
#
#   <b>Что за этим стоит</b>  • …
#
#   💬 разбор
#
#   <a href="…">Открыть панель</a> · 28.10, 14:05 МСК
#
# Подвал несёт время СОБЫТИЯ по Москве, а не время отправки: досылка из очереди
# повторов помечается строкой ⏳ под заголовком, иначе вчерашний поворот читался бы
# как сегодняшний. Пустые разделы не печатаются вовсе — ни «—», ни «нет данных».

def _cap(text):
    text = str(text or "").strip()
    return text[:1].upper() + text[1:]


def footer(event):
    """«Открыть панель · 28.10, 14:05 МСК» — ссылка и время события по Москве."""
    link = f'<a href="{PANEL_URL}">Открыть панель</a>'
    when = msk_when(event.get("ts")) if event.get("ts") else ""
    return f"{link} · {when}" if when and when != "н/д" else link


def _late_line(event, now):
    """⏳ — событие пришло из очереди повторов заметно позже, чем случилось."""
    if not isinstance(now, datetime) or not event.get("ts"):
        return None
    try:
        ts = datetime.fromisoformat(str(event["ts"]).replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    if (now - ts).total_seconds() < LATE_AFTER_MIN * 60:
        return None
    return f"⏳ <i>Досылка: событие от {esc(msk_when(ts, now))}</i>"


def _assemble(lines, tail, limit=TG_LIMIT):
    """Сообщение не длиннее предела: режется тело по границе строки, подвал остаётся.

    Резать посреди строки — значит рвать тег или число; режем целыми строками с
    конца, а если не влезает и первая строка тела — её одну, с многоточием.
    """
    tail_text = "\n\n" + tail if tail else ""
    room = limit - len(tail_text)
    body = list(lines)
    while len(body) > 1 and len("\n".join(body)) > room:
        body.pop()
    text = "\n".join(body)
    if len(text) > room:
        text = text[: room - 1] + "…"
    elif len(body) < len(lines):
        text = text + "\n…"
        if len(text) > room:
            text = text[: room - 1] + "…"
    return text + tail_text


def _move_line(before, after):
    if before and after:
        return f"{esc(before)} → <b>{esc(after)}</b>"
    if after:
        return f"<b>{esc(after)}</b>"
    return esc(before) if before else None


def render_market(event, now=None):
    """Рыночное событие -> HTML для телеграма.

    Порядок блоков тот же, что у 837/838, и он не случаен: сначала ЧТО (заголовок),
    потом НАСКОЛЬКО (было → стало), потом подробности, и только затем разбор модели
    за отбивкой. Читатель, пролиставший ленту, получает смысл из первой строки.
    """
    title = event.get("title") or event.get("text") or ""
    lines = [f"{emoji(event)} <b>{esc(PANEL_LABEL)} · {esc(_cap(title))}</b>"]
    late = _late_line(event, now)
    if late:
        lines.append(late)

    move = _move_line(event.get("before"), event.get("after"))
    if move:
        lines.append(move)

    for item in event.get("moves") or []:
        row = f"• {esc(item.get('name'))}: {_move_line(item.get('before'), item.get('after'))}"
        if item.get("note"):
            row += f" <i>({esc(item['note'])})</i>"
        lines.append(row)

    if event.get("detail"):
        lines.append(esc(event["detail"]))
    if event.get("meaning"):
        lines.append(esc(event["meaning"]))

    # «Что за этим стоит» — блок 837 слово в слово. Туда уходят переходы, которые
    # описывают ТО ЖЕ движение с другой стороны: снятый облигационный флаг и смена
    # режима — это один поворот рынка, а не три новости. Пустая строка перед
    # заголовком блока делается первым элементом, а не «\n» внутри строки: так
    # отбивка видна в исходнике и не теряется при склейке.
    causes = [c for c in (event.get("causes") or []) if str(c).strip()]
    if causes:
        lines += ["", "<b>Что за этим стоит</b>"]
        lines += [f"• {esc(cause)}" for cause in causes]

    # Разбор модели отбивается ПУСТОЙ СТРОКОЙ и меткой 💬 — ровно как у 837/838,
    # чтобы в общей ленте хаба разбор у всех панелей выглядел одинаково.
    comment = (event.get("comment") or "").strip()
    if comment:
        lines += ["", "💬 " + esc(comment)]
    return _assemble(lines, footer(event))


def render_ops(event, panel=None, now=None):
    """Санитарное событие -> HTML в виде общего мостика панелей.

    Форма — как у `/usr/local/sbin/dash-notify`: значок состояния (🔴 🟡 🟢), жирный
    заголовок «Рынок РФ · что сломалось», затем ФАКТ, ЧТО ЭТО ЗНАЧИТ и КУДА
    СМОТРЕТЬ. Последняя строка тела обязательна: сообщение о поломке без адреса
    поломки заставляет искать заново каждый раз. Время в подвале — время события:
    у тревоги об источнике это начало сбоя.
    """
    icon = OPS_KIND.get(event.get("kind"), "🟡")
    title = event.get("title") or event.get("kind") or "отказ"
    lines = [f"{icon} <b>{esc(panel or PANEL_LABEL)} · {esc(_cap(title))}</b>"]
    for key in ("fact", "meaning", "where"):
        if event.get(key):
            lines.append(esc(event[key]))
    return _assemble(lines, footer(event))


def plain_text(event):
    """Тот же факт без разметки — для журнала витрины и ленты хаба.

    Каждая часть — самостоятельное предложение: заголовок с точкой, «было → стало»
    отдельной фразой. Раньше части склеивались пробелом, и лента читала
    «Позиция: деньги → акции деньги (ставка 13,1%) → акции» — заголовок налезал на
    строку движения (аудит 28.10.2026, B5).
    """
    parts = [event.get("title") or ""]
    before, after = event.get("before"), event.get("after")
    if before and after:
        parts.append(f"{before} → {after}")
    elif before or after:
        parts.append(before or after)
    for move in event.get("moves") or []:
        parts.append(f"{move.get('name')}: {move.get('before')} → {move.get('after')}")
    # Санитарные поля тоже: у отказов обвязки нет before/after, весь факт лежит
    # в `fact`, и без него плоский текст выходил без единого числа.
    for key in ("fact", "detail", "meaning", "where"):
        if event.get(key):
            parts.append(event[key])
    for cause in event.get("causes") or []:
        if str(cause).strip():
            parts.append(cause)
    text = " ".join(sentence(p) for p in parts if str(p or "").strip())
    return re.sub(r"\s+", " ", text).strip()
