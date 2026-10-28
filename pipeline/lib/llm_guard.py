"""Проверка смысла ИИ-разбора и учёт его работы — ОБЩИЙ модуль панелей 842 и 843.

Файл побайтово одинаков в обоих репозиториях; правка здесь — та же правка во втором.
Тот же алгоритм на JS живёт в 837/838 блоком «5б. ПРОВЕРКА РАЗБОРА», а совпадение
поведения стережёт общий набор векторов `tests/llm_guard_vectors.json` — он тоже
одинаков во всех четырёх репозиториях.

ЗАЧЕМ. Промпт просит не выдумывать числа, не прогнозировать и не называть устройство
панели, но просьба — не гарантия. В ленте 842 до 28.10.2026 стояли «доходности уйдут
ещё на 30–50 бп», «пресс-релиз в 13:30», «RVI выше 40 — паника», «ворот открыты» и
разборы по 960 знаков при заказанных трёх фразах (аудит 28.10.2026, раздел 4).
Модуль проверяет ответ машинно:

* числа — только из входа СВОЕГО события и общей картины рынка, с допуском на
  показанную точность; отношения и разности пар чисел события тоже честны («в 1,5
  раза» из 205 и 137); год — только тот, что стоит в датах входа; дата и время —
  только из входа (дата не подтверждает «10 %» своим месяцем);
* прогнозы — по словарю вне «если …, то …»; у 843 движение курса («рубль укрепится»,
  «курс вернётся») режется и внутри «если»;
* внутренние слова панели и служебные имена полей;
* чужая письменность, латиница внутри русского слова, транслит;
* предел 600 знаков и 4 предложения;
* метки блоков ===N=== строго 0…n−1 без повторов.

Нарушившее правило предложение ВЫРЕЗАЕТСЯ, если после этого остаётся хотя бы два
предложения; иначе разбор отбрасывается целиком и событие уходит без него. Всё
отброшенное пишется в журнал с причиной и началом текста — без этого не отличить
выдумку от ложного срабатывания (у 838 так и было до 29.10.2026).

УЧЁТ. Раз в неделю в ops-канал уходит одна строка «показано N из M, отбраковано K
(причины), потрачено $X», а отказ API по доступу (401/402/403, кончились кредиты,
модель недоступна) — отдельной тревогой сразу: иначе разборы молча пропадают, а
события выглядят как обычно.

Только стандартная библиотека; модуль никогда не бросает наружу.
"""

import json
import math
import os
import re
from datetime import datetime, timedelta, timezone

MAX_CHARS = 600
MAX_SENTENCES = 4
MIN_SENTENCES = 2
LOG_CHARS = 200
SCAN_LIMIT = 4000
# Поиск повтора квадратичен по длине: 4000 знаков — около секунды на ноутбуке. Разбор
# одного события не длиннее 600 знаков, и зацикливание видно в голове ответа.
LOOP_LIMIT = 1500
MSK = timezone(timedelta(hours=3))

_L = "A-Za-zА-Яа-яЁё"
_CYR = "А-Яа-яЁё"
_NOT_L = "(?<![" + _L + "])"
_END_L = "(?![" + _L + "])"

# --------------------------------------------------------------- нормализация
_DASHES = re.compile("[−‒–—]")
_SPACES = re.compile("[    ]")


def _plain(text):
    return _SPACES.sub(" ", _DASHES.sub("-", str(text if text is not None else "")))


def _low(text):
    return _plain(text).lower().replace("ё", "е")


def short(text, n=LOG_CHARS):
    """Фраза для журнала: одна строка, не длиннее n знаков."""
    s = re.sub(r"\s+", " ", str(text if text is not None else "")).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


# --------------------------------------------------------------- метки блоков
# Метка без якорей строки: боевой ответ 842 приходил склеенным («…равновесиях.===1===»).
_MARK = re.compile(r"={2,}[ \t]*([0-9]+)[ \t]*={2,}")
_FENCE = re.compile(r"^```[a-z]*\s*|\s*```$")


def _body(text):
    return _FENCE.sub("", text.strip()).strip()


def parse_blocks(text, n):
    """Ответ модели -> (список длиной n | None, причина брака | None).

    Метки обязаны идти 0, 1, … по порядку и без повторов. «===1===» у первого события
    приклеил бы разбор к чужому сообщению, поэтому любой сбой нумерации — брак всего
    ответа. Меньше меток, чем событий, — законно: недостающие остаются без разбора.
    Ответ на одно событие без меток принимается целиком.
    """
    text = str(text if text is not None else "")
    marks = list(_MARK.finditer(text))
    if not marks:
        body = _body(text)
        if n == 1 and 40 <= len(body) <= SCAN_LIMIT:
            return [body], None
        return None, "нет меток ===N==="
    idx = [int(m.group(1)) for m in marks]
    dup = sorted({i for i in idx if idx.count(i) > 1})
    if dup:
        return None, "метки повторяются: " + ", ".join(f"==={i}===" for i in dup)
    if idx != list(range(len(idx))) or len(idx) > n:
        return None, "метки " + " ".join(f"==={i}===" for i in idx) + f" вместо 0…{n - 1}"
    out = [None] * n
    for k, m in enumerate(marks):
        end = marks[k + 1].start() if k + 1 < len(marks) else len(text)
        out[k] = _body(text[m.end():end]) or None
    return out, None


# --------------------------------------------------------------- предложения
# Граница — знак конца, пробел и заглавная (цифра, кавычка, скобка, знак числа) либо
# перевод строки. «т. е. рост» не режется: за точкой строчная. Сокращение перед точкой
# («5 б.п. (сотых долей)», «−44 тыс. BTC») границей не считается, если дальше не
# русская заглавная: живые ответы 29.10.2026 резались ровно так.
_SENT_END = re.compile(r"(?<=[.!?…])[ \t]+(?=[«\"(\[А-ЯЁA-Z0-9+-])|\n+")
_ABBR = re.compile(r"(?:^|[^" + _L + r"])(?:б\.п|п\.п|тыс|млн|млрд|трлн|руб|долл|г|гг|т\.е|т\.д|т\.п|т\.н|т\.ч"
                   r"|др|см|ок|им|стр|рис|табл|прим|ср|н\.э)\.$", re.IGNORECASE)


def sentences(text):
    parts = [p.strip() for p in _SENT_END.split(str(text if text is not None else "").strip()) if p.strip()]
    out = []
    for part in parts:
        if out and _ABBR.search(out[-1]) and not re.match("[«\"]?[А-ЯЁ]", part):
            out[-1] += " " + part
        else:
            out.append(part)
    return out


# --------------------------------------------------------------- числа
_MONTHS = ("январ", "феврал", "март", "апрел", "ма", "июн", "июл", "август", "сентябр",
           "октябр", "ноябр", "декабр")
_MONTH = (r"(январ[ьяе]|феврал[ьяе]|март[ае]?|апрел[ьяе]|ма[йяе]|июн[ьяе]|июл[ьяе]|август[ае]?"
          r"|сентябр[ьяе]|октябр[ьяе]|ноябр[ьяе]|декабр[ьяе])")


def _month(word):
    w = word.lower()
    for i, stem in enumerate(_MONTHS):
        if w.startswith(stem) and (stem != "ма" or w in ("май", "мая", "мае")):
            return i + 1
    return 0


# Числа внутри имён и кодов — не данные: «S&P 500», «IMOEX2», «G20», «RUSFAR 3M».
# Так читается только ответ модели; вход читается щедро, со всеми цифрами.
_NAMED = re.compile(
    r"S&P ?500|Nasdaq[ -]?100|Russell ?2000|Euro ?Stoxx ?50|FTSE ?100|Nikkei ?225|CSI ?300"
    r"|COVID-19|H\.[0-9]+(?:\.[0-9]+)?"
    r"|(?<![" + _L + r"0-9])[A-Za-z]+[0-9]+[A-Za-z0-9]*"
    r"|(?<![" + _L + r"0-9])[0-9]+[A-Za-z]+(?![" + _L + r"])",
    re.IGNORECASE)
_NUM = re.compile(r"(?<![0-9])[0-9]{1,3}(?: [0-9]{3})+(?:[.,][0-9]+)?(?![0-9])|(?<![0-9])[0-9]+(?:[.,][0-9]+)?(?![0-9])")
_DIFF_CUE = re.compile(r"(?:^|[^" + _L + r"])на (?:(?:ещ[её]|почти|около|примерно|целых|всего|лишь|более|менее"
                       r"|чем|свыше|порядка) )*[+-]?$", re.IGNORECASE)
_RATIO_CUE = re.compile(r"(?:[ -]?кратн| ?раз[а]?(?![" + _L + r"]))", re.IGNORECASE)


def _precision(raw):
    t = raw.replace(" ", "")
    parts = re.split(r"[.,]", t)
    if len(parts) > 1 and parts[1]:
        return 10.0 ** -len(parts[1])
    zeros = len(t) - len(t.rstrip("0"))
    return 10.0 ** max(0, min(zeros, len(t) - 2)) if len(t) >= 4 else 1.0


def tokens(text, named=True, signed=False):
    """Числа текста: даты, время, годы, месяц с годом и обычные числа.

    У обычного числа — значение, точность записи («19,5» — до десятых, «86 000» — до
    тысяч) и признак сравнения: «на 0,07» — разность, «в 1,5 раза» — отношение.
    """
    s = _plain(text)
    if named:
        s = _NAMED.sub(lambda m: " " * len(m.group(0)), s)
    out = []

    def take(item):
        def sub(m):
            got = item(m)
            if got:
                got.update(raw=m.group(0).strip(), at=m.start())
                out.append(got)
            return " " * len(m.group(0))
        return sub

    s = re.sub(r"(?<![0-9])([0-9]{4})-([0-9]{2})-([0-9]{2})(?:T[0-9:.]+Z?)?",
               take(lambda m: {"kind": "date", "d": int(m.group(3)), "m": int(m.group(2)), "y": int(m.group(1))}), s)
    s = re.sub(r"(?<![0-9.,])([0-9]{1,2})\.([0-9]{1,2})\.([0-9]{4})(?![0-9])",
               take(lambda m: {"kind": "date", "d": int(m.group(1)), "m": int(m.group(2)), "y": int(m.group(3))}), s)
    s = re.sub(r"(?<![0-9])([0-9]{1,2})(?:-го)? " + _MONTH + r"(?: ([0-9]{4}))?" + _END_L,
               take(lambda m: {"kind": "date", "d": int(m.group(1)), "m": _month(m.group(2)),
                               "y": int(m.group(3)) if m.group(3) else 0}), s, flags=re.IGNORECASE)
    s = re.sub(_NOT_L + _MONTH + r"[а-я]* ([0-9]{4})(?![0-9])",
               take(lambda m: {"kind": "myear", "m": _month(m.group(1)), "y": int(m.group(2))}), s, flags=re.IGNORECASE)
    s = re.sub(r"(?<![0-9:])([01]?[0-9]|2[0-3]):([0-5][0-9])(?![0-9:])",
               take(lambda m: {"kind": "time", "h": int(m.group(1)), "mi": int(m.group(2))}), s)
    for m in _NUM.finditer(s):
        raw = m.group(0)
        item = {"kind": "num", "raw": raw, "at": m.start(),
                "v": float(raw.replace(" ", "").replace(",", ".")), "p": _precision(raw)}
        if re.fullmatch(r"(?:19|20)[0-9]{2}", raw):
            item.update(kind="year", y=int(raw))
        dm = re.fullmatch(r"([0-9]{1,2})\.([0-9]{2})", raw)
        if dm and 1 <= int(dm.group(1)) <= 31 and 1 <= int(dm.group(2)) <= 12:
            item["dm"] = [int(dm.group(1)), int(dm.group(2))]
        before = s[m.start() - 1] if m.start() else ""
        if signed and before == "-" and not re.match("[" + _L + "0-9]", s[m.start() - 2] if m.start() > 1 else " "):
            item["v"] = -item["v"]
        if _DIFF_CUE.search(s[:m.start()]):
            item["cue"] = "diff"
        elif _RATIO_CUE.match(s[m.end():]):
            item["cue"] = "ratio"
        out.append(item)
    out.sort(key=lambda t: t["at"])
    return out


def _json_number(value):
    """Число из JSON входа -> (значение, точность записи).

    Точность — по кратчайшей записи значения, одинаковой в Python и JS: целое — до
    единиц, 2391.72 — до сотых, 1e-07 — до десятимиллионных. «14.0» в JSON читается
    как целое: JS различать 14 и 14.0 не умеет, а наборы векторов общие.
    """
    v = float(value)
    if v == int(v):
        return v, 1.0
    mant, _, exp = repr(v).lower().partition("e")
    decimals = len(mant.partition(".")[2]) - (int(exp) if exp else 0)
    return v, 10.0 ** -max(decimals, 0)


class Evidence:
    """Что модели дали про ОДНО событие: его поля и общий контекст, без чужих событий.

    Вход — JSON, который ушёл модели: события лежат в списке `events` (номер — в `i`),
    всё остальное (сегодня, market_now, контекст решения) общее для пачки.
    """

    def __init__(self, user_input, index, extra=None):
        self.nums, self.own, self.dates, self.times, self.years = [], [], [], set(), set()
        data = user_input
        if isinstance(user_input, str):
            try:
                data = json.loads(user_input)
            except ValueError:
                data = user_input
        self._walk(data, index, False)
        # Предметные числа самого промпта («около половины», «с июня 2024 года») модели
        # тоже даны: их называть не выдумка.
        if extra:
            self._text(extra, False)

    def _text(self, text, own):
        for t in tokens(text, named=False, signed=True):
            kind = t["kind"]
            if kind == "date":
                self.dates.append((t["d"], t["m"], t["y"]))
                if t["y"]:
                    self.years.add(t["y"])
            elif kind == "myear":
                self.years.add(t["y"])
            elif kind == "time":
                self.times.add((t["h"], t["mi"]))
            else:
                if kind == "year":
                    self.years.add(t["y"])
                if "dm" in t:
                    self.dates.append((t["dm"][0], t["dm"][1], 0))
                self._num(t["v"], t["p"], own)

    def _num(self, v, p, own):
        self.nums.append((v, p))
        if own:
            self.own.append((v, p))

    def _walk(self, node, index, own):
        if isinstance(node, dict):
            for key, val in node.items():
                if key == "events" and isinstance(val, list) and not own:
                    for pos, ev in enumerate(val):
                        num = ev.get("i", pos) if isinstance(ev, dict) else pos
                        if num == index:
                            self._walk({k: v for k, v in ev.items() if k != "i"} if isinstance(ev, dict) else ev,
                                       index, True)
                    continue
                self._text(key, own)
                self._walk(val, index, own)
        elif isinstance(node, list):
            for val in node:
                self._walk(val, index, own)
        elif isinstance(node, bool) or node is None:
            return
        elif isinstance(node, (int, float)):
            if math.isfinite(node):
                self._num(*_json_number(node), own)
        else:
            self._text(node, own)


# Кратность: «86 тыс.» при 86 198, «1,2 трлн» при 1 200 млрд.
_SCALES = (1.0, 1e3, 1e6, 1e9, 1e12)


def _found(v, p, pool):
    tol = p / 2 + abs(v) * 1e-9
    av = abs(v)
    for y, _ in pool:
        ay = abs(y)
        for k in _SCALES:
            if abs(av - ay * k) <= tol or abs(av - ay / k) <= tol:
                return True
    return False


def _derived(v, p, cue, own):
    """Число из пары чисел СВОЕГО события: отношение, разность или изменение в %."""
    av = abs(v)
    for i, (a, pa) in enumerate(own):
        for j, (b, pb) in enumerate(own):
            if i == j or b == 0:
                continue
            if cue == "ratio":
                if a == 0:
                    continue
                r = abs(a) / abs(b)
                if abs(av - r) <= p / 2 + r * (pa / 2 / abs(a) + pb / 2 / abs(b)):
                    return True
            else:
                if abs(av - abs(a - b)) <= p / 2 + (pa + pb) / 2:
                    return True
                c = abs(a - b) / abs(b) * 100
                if abs(av - c) <= p / 2 + 100 * (pa / 2 + pb / 2 * abs(a) / abs(b)) / abs(b):
                    return True
    return False


def stray_numbers(sentence, ev):
    """Числа предложения, которых нет во входе своего события."""
    bad = []
    for t in tokens(sentence):
        kind = t["kind"]
        if kind == "date":
            ok = any(d == t["d"] and m == t["m"] and (not t["y"] or not y or y == t["y"]) for d, m, y in ev.dates)
        elif kind == "myear":
            ok = t["y"] in ev.years
        elif kind == "time":
            ok = (t["h"], t["mi"]) in ev.times
        elif kind == "year":
            ok = t["y"] in ev.years or _found(t["v"], 1.0, ev.nums)
        else:
            ok = _found(t["v"], t["p"], ev.nums)
            if not ok and "dm" in t:
                ok = any(d == t["dm"][0] and m == t["dm"][1] for d, m, _ in ev.dates)
            if not ok and t.get("cue"):
                ok = _derived(t["v"], t["p"], t["cue"], ev.own)
        if not ok:
            bad.append(t["raw"])
    return bad


# --------------------------------------------------------------- слова
# Устройство панелей и служебные имена полей входа. Читатель про них не знает, а
# «ворот открыты» и «якоря_в_работе» в ленту уже протекали.
_JARGON = re.compile(
    _NOT_L + r"(?:композит[а-я]*|балл(?:а|ов|ы|ам|ами|ах|е)?|ядр(?:о|а|у|ом|е)|ячейк[а-я]*|ячеек"
    r"|ворот(?:а|ам|ами|ах)?|якор(?:ь|я|ей|ю|ям|ями|ях|ем|е)|веер[а-я]*|детектор[а-я]*|дашборд[а-я]*"
    r"|панел(?:ь|и|ью|ей|ям|ями|ях)|монитор(?:ы|ов|ам|ами|ах)?)" + _END_L +
    r"|[" + _L + r"]+_[" + _L + r"0-9_]+",
    re.IGNORECASE)

_VERBS = (r"вырастет|вырастут|подрастет|подрастут|упадет|упадут|снизится|снизятся|понизится|понизятся"
          r"|повысится|повысятся|подорожает|подорожают|подешевеет|подешевеют|укрепится|укрепятся|ослабнет"
          r"|ослабнут|ослабеет|развернется|развернутся|отскочит|отскочат|обвалится|обвалятся|просядет"
          r"|просядут|восстановится|восстановятся")
# Уверенность о будущем рынка. Ловит узнаваемое, а не всякий прогноз: словарь — сеть с
# крупной ячейкой, остальное держит промпт.
_FORECAST = re.compile(
    _NOT_L + r"(?:скорее всего|наверняка|неизбежно|непременно|неминуемо|гарантированно|маловероятно|несомненно"
    r"|дно будет|не разворот|разворота не будет"
    r"|(?:рост|отскок|разворот|подъем|снижение|падение) начнется|начнется (?:устойчивый |настоящий )?(?:рост|отскок"
    r"|разворот|подъем|снижение|падение)"
    r"|(?:" + _VERBS + r")"
    r"|продолжи(?:т|м|ть)? (?:рост|падение|снижение|укрепление|ослабление|расти|падать|снижаться|дорожать"
    r"|дешеветь|укрепляться|слабеть)|продолжат (?:рост|падение|снижение|расти|падать|снижаться|дорожать|дешеветь)"
    r"|(?:пойдет|пойдут|уйдет|уйдут) (?:вверх|вниз|выше|ниже)"
    r"|(?:будет|будут) (?:расти|падать|снижаться|дорожать|дешеветь|укрепляться|слабеть)"
    r"|станет началом|даст (?:короткий |мощный |сильный )?(?:отскок|рост|импульс))" + _END_L)
# Курс числом и направлением — для 843 запрет без исключений, даже внутри «если».
_FORECAST_RATE = re.compile(
    _NOT_L + r"(?:курс|рубл|доллар|юан)[а-я]* (?:(?:скорее всего|вероятно|наверняка|точно|снова|быстро) )?"
    r"(?:пойдет|вернется|уйдет|составит|достигнет|будет стоить|укрепится|ослабнет|подешевеет"
    r"|подорожает)" + _END_L)
_IF = re.compile(_NOT_L + r"(?:если|в случае|при условии)" + _END_L)

_FOREIGN = re.compile("[؀-ۿ֐-׿一-鿿぀-ヿ가-힯]")
_MIXED = re.compile("[" + _CYR + "][A-Za-z]|[A-Za-z][" + _CYR + "]")
_TRANSLIT = re.compile(r"(?<![A-Za-z])[a-z]{5,}(?![A-Za-z])")
_TRANSLIT_OK = {"bitcoin", "ethereum", "nasdaq", "treasuries", "futures", "options", "hedge", "spread",
                "basis", "carry", "funding", "stablecoin"}

KINDS = ("числа", "прогноз", "внутренние слова", "язык", "длина", "метки", "брак текста")


def _sentence_fault(sentence, ev, strict):
    """-> (вид, причина) | None."""
    low = _low(sentence)
    if _FOREIGN.search(sentence):
        return "язык", "чужая письменность"
    m = _MIXED.search(sentence)
    if m:
        word = re.search("[" + _L + "]*" + re.escape(m.group(0)) + "[" + _L + "]*", sentence)
        return "язык", "латиница внутри слова: " + (word.group(0) if word else m.group(0))
    tr = [w for w in _TRANSLIT.findall(sentence) if w not in _TRANSLIT_OK]
    if tr:
        return "язык", "транслит: " + ", ".join(tr[:3])
    j = _JARGON.search(sentence)
    if j:
        return "внутренние слова", "внутреннее слово: " + j.group(0)
    # Курс направлением при strict режется и внутри «если»; общий словарь у всех панелей —
    # только вне «если …, то …»: условие с признаком и есть сценарий, о котором просит промпт.
    f = _FORECAST_RATE.search(low) if strict else None
    if not f:
        f = _FORECAST.search(low)
        if f and _IF.search(low):
            f = None
    if f:
        return "прогноз", "прогноз: " + f.group(0)
    stray = stray_numbers(sentence, ev) if ev is not None else []
    if stray:
        return "числа", "числа не из входа: " + ", ".join(stray[:4])
    return None


def _whole_fault(text):
    t = str(text if text is not None else "").strip()
    if len(t) < 40:
        return "слишком короткий ответ"
    head = t[:SCAN_LIMIT]
    if re.search(r"([^\n]{12,}?)\1{2,}", t[:LOOP_LIMIT]):
        return "текст зациклился"
    if "```" in t or t.startswith("{"):
        return "вместо текста разметка или JSON"
    cyr = len(re.findall("[" + _CYR + "]", head))
    lat = len(re.findall("[A-Za-z]", head))
    if cyr + lat >= 40 and cyr / (cyr + lat) < 0.5:
        return f"ответ не на русском (кириллицы {int(100 * cyr / (cyr + lat) + 0.5)}%)"
    return None


def check(text, user_input=None, index=0, strict=False, extra=None):
    """Проверить разбор одного события.

    user_input — вход модели (JSON-строка или объект), index — номер события в нём,
    strict — запрет прогноза курса без исключения для «если» (843), extra — текст
    промпта до раздела формата: его предметные числа модели тоже даны.
    -> {"text": итог | None, "cut": [[фраза, причина, вид]], "reject": причина | None,
        "kind": вид брака | None}
    """
    res = {"text": None, "cut": [], "reject": None, "kind": None}
    try:
        whole = _whole_fault(text)
        if whole:
            res.update(reject=whole, kind="язык" if "русском" in whole else "брак текста")
            return res
        ev = Evidence(user_input, index, extra) if user_input is not None else None
        kept = []
        for s in sentences(text):
            fault = _sentence_fault(s, ev, strict)
            if fault:
                res["cut"].append([short(s), fault[1], fault[0]])
            else:
                kept.append(s)
        if res["cut"] and len(kept) < MIN_SENTENCES:
            kinds = [c[2] for c in res["cut"]]
            res.update(kind=max(KINDS, key=lambda k: (kinds.count(k), -KINDS.index(k))),
                       reject=f"после вырезания осталось предложений: {len(kept)} ("
                              + "; ".join(c[1] for c in res["cut"][:3]) + ")")
            return res
        over = []
        while kept and (len(kept) > MAX_SENTENCES or len(" ".join(kept)) > MAX_CHARS):
            over.insert(0, kept.pop())
        for s in over:
            res["cut"].append([short(s), "сверх предела длины", "длина"])
        if over and len(kept) < MIN_SENTENCES:
            res.update(kind="длина", reject=f"длиннее предела: {len(' '.join(kept + over))} знаков, "
                                            f"{len(kept + over)} предложений")
            return res
        res["text"] = " ".join(kept)
        return res
    except Exception as exc:  # noqa: BLE001 — проверка не роняет разбор: сломалась — разбора нет
        res.update(text=None, reject=f"проверка упала: {type(exc).__name__}: {short(exc, 120)}", kind="брак текста")
        return res


def review(text, user_input, n, strict=False, say=None, extra=None):
    """Весь ответ модели -> (список комментариев длиной n с None на месте брака, сводка).

    Сводка — для недельного учёта: {"shown", "cut", "rejected": {вид: число}}. say —
    журнал: каждое вырезанное и отброшенное уходит туда с причиной и началом текста.
    """
    def log(msg):
        if say:
            try:
                say(msg)
            except Exception:  # noqa: BLE001 — журнал не роняет проверку
                pass

    summary = {"shown": 0, "cut": 0, "rejected": {}}
    blocks, err = parse_blocks(text, n)
    if blocks is None:
        log(f"ответ отброшен — {err}; начало: «{short(text)}»")
        summary["rejected"]["метки"] = n
        return [None] * n, summary
    out = [None] * n
    for i, block in enumerate(blocks):
        if block is None:
            log(f"событие {i}: разбора в ответе нет")
            summary["rejected"]["метки"] = summary["rejected"].get("метки", 0) + 1
            continue
        res = check(block, user_input, i, strict, extra)
        for phrase, why, _kind in res["cut"]:
            log(f"событие {i}: вырезано — {why}: «{phrase}»")
        summary["cut"] += len(res["cut"]) if res["text"] else 0
        if res["text"]:
            out[i] = res["text"]
            summary["shown"] += 1
        else:
            log(f"событие {i}: разбор отброшен — {res['reject']}; текст: «{short(block)}»")
            summary["rejected"][res["kind"]] = summary["rejected"].get(res["kind"], 0) + 1
    return out, summary


# --------------------------------------------------------------- недельный учёт
# Отказ API по доступу: разборы тогда пропадают все, а события уходят как обычно —
# заметить это по ленте нельзя. Ключ отозван (401), кончились кредиты или беда с
# оплатой (402; у Anthropic это бывает и 400 «credit balance is too low»), у ключа нет
# прав (403), модель снята (404 по model).
ACCESS_WORDS = {401: "ключ не принят: отозван или неверен",
                402: "отказ по оплате: кончились кредиты или не прошёл платёж",
                403: "у ключа нет доступа к модели",
                404: "модель недоступна",
                400: "кончились кредиты"}


def access_problem(status, message=""):
    """Отказ API, при котором разборов не будет, пока человек не вмешается."""
    msg = str(message or "").lower()
    if status in (401, 402, 403):
        return True
    if status == 400 and ("credit" in msg or "billing" in msg):
        return True
    return status == 404 and "model" in msg


def week_of(now):
    """ISO-неделя по Москве: «2026-W44»."""
    y, w, _ = now.astimezone(MSK).isocalendar()
    return f"{y}-W{w:02d}"


def _week_span(week):
    y, w = week.split("-W")
    monday = datetime.fromisocalendar(int(y), int(w), 1)
    return f"{monday:%d.%m}–{monday + timedelta(days=6):%d.%m}"


def _empty(week):
    return {"week": week, "asked": 0, "shown": 0, "cut": 0, "rejected": {}, "failed": {}, "usd": 0.0}


def load_stats(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_stats(path, stats):
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(stats, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


def _roll(stats, now):
    """Текущая неделя; закончившаяся уходит в очередь отчёта."""
    week = week_of(now)
    cur = stats.get("cur") if isinstance(stats.get("cur"), dict) else None
    if cur is None:
        stats["cur"] = _empty(week)
    elif cur.get("week") != week:
        stats["report"] = cur
        stats["cur"] = _empty(week)
    return stats["cur"]


def record(stats, now, asked=0, summary=None, failed=None, usd=0.0, access=None, ok=False):
    """Дописать в учёт итог одного обращения к модели."""
    try:
        cur = _roll(stats, now)
        cur["asked"] = int(cur.get("asked") or 0) + int(asked)
        if summary:
            cur["shown"] = int(cur.get("shown") or 0) + int(summary.get("shown") or 0)
            cur["cut"] = int(cur.get("cut") or 0) + int(summary.get("cut") or 0)
            for kind, cnt in (summary.get("rejected") or {}).items():
                cur["rejected"][kind] = int(cur["rejected"].get(kind) or 0) + int(cnt)
        if failed:
            cur["failed"][failed] = int(cur["failed"].get(failed) or 0) + int(asked or 1)
        cur["usd"] = round(float(cur.get("usd") or 0.0) + float(usd or 0.0), 6)
        if access:
            if not (stats.get("access") or {}).get("status") == access.get("status"):
                stats["access"] = dict(access, since=now.isoformat(), alerted=False)
        elif ok and stats.get("access"):
            stats["recovered"] = dict(stats.pop("access"), at=now.isoformat())
    except Exception:  # noqa: BLE001 — учёт не роняет разбор
        pass
    return stats


def _money(usd):
    return f"${usd:.2f}".replace(".", ",")


def week_line(panel, rec):
    """Строка недельного отчёта."""
    head = f"📊 {panel} · ИИ-разборы за {_week_span(rec['week'])}: "
    asked = int(rec.get("asked") or 0)
    if not asked:
        return head + f"событий для разбора не было; потрачено {_money(float(rec.get('usd') or 0))}"
    parts = [f"показано {int(rec.get('shown') or 0)} из {asked}"]
    rej = {k: v for k, v in (rec.get("rejected") or {}).items() if v}
    if rej:
        parts.append(f"отбраковано {sum(rej.values())} ("
                     + ", ".join(f"{k} {v}" for k, v in sorted(rej.items(), key=lambda kv: -kv[1])) + ")")
    else:
        parts.append("отбраковано 0")
    if rec.get("cut"):
        parts.append(f"вырезано фраз {int(rec['cut'])}")
    fail = {k: v for k, v in (rec.get("failed") or {}).items() if v}
    if fail:
        parts.append("без ответа " + ", ".join(f"{k} {v}" for k, v in sorted(fail.items(), key=lambda kv: -kv[1])))
    parts.append("потрачено " + _money(float(rec.get("usd") or 0)))
    return head + "; ".join(parts)


def _esc(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def access_alert(panel, acc, where):
    status = acc.get("status")
    what = ACCESS_WORDS.get(status, "отказ по доступу")
    return (f"🔴 <b>{_esc(panel)} · ИИ-разбор не работает</b>\n"
            f"API Anthropic ответил {status} {_esc(acc.get('kind') or '')}: {_esc(what)}. "
            f"{_esc(short(acc.get('message') or '', 160))}\n"
            "События уходят без разбора, сама рассылка работает.\n"
            f"Смотреть: console.anthropic.com → API Keys и Billing; {_esc(where)}")


def recovery_line(panel, acc):
    return (f"🟢 <b>{_esc(panel)} · ИИ-разбор снова работает</b>\n"
            f"API Anthropic отвечает; отказ {acc.get('status')} длился с "
            f"{_esc(_msk(acc.get('since')))} до {_esc(_msk(acc.get('at')))} МСК.")


def _msk(iso):
    try:
        return datetime.fromisoformat(str(iso)).astimezone(MSK).strftime("%d.%m %H:%M")
    except ValueError:
        return "?"


def ops_tick(stats, now, panel, send, where):
    """Раз в прогон: тревога о доступе, «снова работает» и строка за прошлую неделю.

    send(text) -> bool; отправленное помечается только после успеха, поэтому сбой
    Telegram повторится следующим прогоном. -> список того, что ушло (для журнала).
    """
    sent = []
    try:
        _roll(stats, now)
        acc = stats.get("access")
        if acc and not acc.get("alerted") and send(access_alert(panel, acc, where)):
            acc["alerted"] = True
            sent.append(f"тревога: API {acc.get('status')}")
        rec = stats.get("recovered")
        if rec:
            if not rec.get("alerted") or send(recovery_line(panel, rec)):
                stats.pop("recovered", None)
                if rec.get("alerted"):
                    sent.append("снова работает")
        rep = stats.get("report")
        if rep and send(week_line(panel, rep)):
            stats.pop("report", None)
            sent.append(f"неделя {rep.get('week')}")
    except Exception:  # noqa: BLE001 — учёт не роняет прогон
        pass
    return sent
