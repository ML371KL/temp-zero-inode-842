"""«Обзор рисков финансовых рынков» ЦБ: нетто-покупки акций по категориям участников.

Ряд `orfr_flows` (registry: subkeys fiz/nfo_du/nfo_own/szko/other_banks/nonres,
pub_lag_days=15, poll_window 5–17 числа). Валидация относит его к слою 3 —
мониторинг без предиктивных претензий: «исчерпание продавца ДУ» имеет 5 событий
и мощность в единицы процентов (VALIDATION.md §5.8). Ряд ведём ради накопления
истории и чтения того, КТО двигает рынок, а не ради сигнала.

Почему свой извлекатель PDF. Внешних библиотек нет по конституции проекта
(CONTRACT.md §0), а числа ЦБ называет прямо в тексте («розничные инвесторы
(нетто-покупки – 15,7 млрд руб.)»), поэтому достаточно вытащить текстовый слой:
потоки FlateDecode распаковываются zlib, текст лежит в операторах Tj/TJ, коды
переводятся в Unicode по /ToUnicode CMap шрифта (а при её отсутствии — по
/Encoding /Differences). Полноценный PDF-парсер здесь не нужен и не пишется.

Грабли, оплаченные отладкой на ORFR_2026-2.pdf:
1. /Contents страницы — это МАССИВ потоков, и шрифт, выбранный оператором Tf в
   одном фрагменте, действует в следующих. Разбирать фрагменты по отдельности
   (сбрасывая текущий шрифт) — половина текста превращается в кракозябры,
   сдвинутые ровно на 0x3C0: это cp1251-чтение кодов чужого шрифта.
2. Имена шрифтов (/C2_1) НЕ глобальны: на разных страницах один и тот же /C2_1
   указывает на разные объекты. Карту строим на КАЖДУЮ страницу отдельно.
3. Тире перед числом в тексте ЦБ — разделитель, а не минус («нетто-покупки –
   15,7 млрд руб.» = ПОКУПКИ на 15,7). Знак определяем словом (приобрели /
   реализовали), пунктуацию игнорируем.
4. В обзоре куча чисел «млрд руб» про валюту, ОФЗ и корпоративные облигации.
   Тему числа определяем ТОЛЬКО по тексту ВЫШЕ него (_topic_is_equity): в
   ORFR_2025-11 абзац про облигации упирается прямо в заголовок «рынок акций»,
   и «ближайшее слово в обе стороны» уводит 143 млрд СЗКО не в тот ряд.
5. Пробелы в тексте PDF — не разделители, а вёрстка: «нетто-покупателями» может
   приехать как «нетт о-п окупателями», «156» — как «1 5 6». Поэтому и текст, и
   ключевые слова сжимаются (см. _norm/_squeeze), а не «чистятся» точечно.

Проверено на четырёх выпусках (числа сошлись с текстом до 0,1 млрд):
2026-02, 2025-11, 2025-09, 2025-07.
"""

import re
import zlib
from datetime import date, datetime, timedelta, timezone

try:                                       # прод: общий HTTP-слой (CONTRACT.md §4)
    from lib.http import get_text, get_bytes, FetchError
except ImportError:
    try:
        from pipeline.lib.http import get_text, get_bytes, FetchError
    except ImportError:                    # автономный запуск (отладка парсера)
        class FetchError(Exception):
            pass

        def get_bytes(url, timeout=90, headers=None, **_kw):
            import urllib.request
            req = urllib.request.Request(url, headers=headers or _UA)
            try:
                return urllib.request.urlopen(req, timeout=timeout).read()
            except OSError as exc:
                raise FetchError("%s: %s" % (url, exc))

        def get_text(url, encoding="utf-8", **kw):
            return get_bytes(url, **kw).decode(encoding, "replace")

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
       "Accept-Language": "ru,en;q=0.8"}

SERIES_ID = "orfr_flows"
INDEX_URL = "https://www.cbr.ru/analytics/finstab/orfr/"
BASE = "https://www.cbr.ru"

CATEGORIES = ("fiz", "nfo_du", "nfo_own", "szko", "other_banks", "nonres")

# Реперы из задания (ORFR за июль 2026): используются только для самопроверки,
# в данные НЕ подставляются. Расхождение уходит в meta["selfcheck"].
SELF_CHECK = {"2026-07": {"fiz": 24.4, "nfo_du": -37.9, "szko": -22.7, "nonres": 15.3}}


# ------------------------------------------------------------ извлечение PDF

_OBJ_RE = re.compile(rb"(\d+)\s+\d+\s+obj\b(.*?)\bendobj", re.S)
_NAME_REF = re.compile(rb"/([A-Za-z0-9#+._-]+)\s+(\d+)\s+\d+\s+R")
_TOKEN = re.compile(
    rb"/([A-Za-z0-9#+._-]+)\s+[-\d.]+\s+Tf"      # выбор шрифта
    rb"|\((?:\\.|[^\\()])*\)"                     # литеральная строка
    rb"|<([0-9A-Fa-f\s]*)>"                       # hex-строка
    rb"|\bT[dDm]\b|\bT\*\b|\bET\b",               # переводы строки/конец текста
    re.S)

# Кириллица в /Differences встречается двумя именами глифов: uniXXXX и afiiNNNNN.
_AFII_UP = "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"   # afii10017..afii10049
_AFII_LOW = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"  # afii10065..afii10097


class _Pdf:
    """Минимальная объектная модель: номер объекта -> тело, распакованные потоки."""

    def __init__(self, data):
        self.objs = {}
        for m in _OBJ_RE.finditer(data):
            self.objs.setdefault(int(m.group(1)), m.group(2))
        self.streams = {}
        for num, body in self.objs.items():
            raw, head = self._raw_stream(body)
            if raw is None or b"FlateDecode" not in head:
                continue
            dec = self._inflate(raw)
            if dec is not None:
                self.streams[num] = dec
        self._cmap_cache = {}

    @staticmethod
    def _raw_stream(body):
        i = body.find(b"stream")
        if i < 0:
            return None, body
        head, j = body[:i], i + len(b"stream")
        if body[j:j + 2] == b"\r\n":
            j += 2
        elif body[j:j + 1] in (b"\n", b"\r"):
            j += 1
        k = body.rfind(b"endstream")
        return (body[j:k] if k > j else None), head

    @staticmethod
    def _inflate(raw):
        try:
            return zlib.decompress(raw)
        except zlib.error:
            try:  # битый хвост встречается — берём то, что распаковалось
                return zlib.decompressobj().decompress(raw)
            except zlib.error:
                return None

    @staticmethod
    def _dict_slice(body, key):
        """Содержимое <<…>> после ключа с учётом вложенности (ищем /Font, /XObject)."""
        i = body.find(key)
        if i < 0:
            return None
        j = body.find(b"<<", i)
        if j < 0:
            return None
        depth, k = 0, j
        while k < len(body) - 1:
            if body[k:k + 2] == b"<<":
                depth += 1
                k += 2
            elif body[k:k + 2] == b">>":
                depth -= 1
                k += 2
                if depth == 0:
                    return body[j + 2:k - 2]
            else:
                k += 1
        return None

    def _resources(self, body):
        """/Resources бывает прямым словарём и ссылкой на объект."""
        res = self._dict_slice(body, b"/Resources")
        if res is not None:
            return res
        ref = re.search(rb"/Resources\s+(\d+)\s+\d+\s+R", body)
        return self.objs.get(int(ref.group(1)), b"") if ref else b""

    def font_map(self, body):
        res = self._resources(body)
        fdict = self._dict_slice(res, b"/Font")
        if fdict is None:
            fdict = self._dict_slice(body, b"/Font") or b""
        return {m.group(1).decode("latin-1"): self._font_cmap(int(m.group(2)))
                for m in _NAME_REF.finditer(fdict)}

    def _font_cmap(self, fobj):
        if fobj in self._cmap_cache:
            return self._cmap_cache[fobj]
        body = self.objs.get(fobj, b"")
        table = {}
        m = re.search(rb"/ToUnicode\s+(\d+)\s+\d+\s+R", body)
        if m:
            table = _parse_cmap(self.streams.get(int(m.group(1)), b""))
        if not table:                       # шрифт без ToUnicode — читаем Differences
            enc = self._dict_slice(body, b"/Encoding")
            if enc is None:
                ref = re.search(rb"/Encoding\s+(\d+)\s+\d+\s+R", body)
                enc = self.objs.get(int(ref.group(1)), b"") if ref else b""
            table = _parse_differences(enc or b"")
        self._cmap_cache[fobj] = table
        return table

    def page_chunks(self):
        """[(байты контента, карта шрифтов)] — страницы и их формы-XObject."""
        out = []
        for num, body in self.objs.items():
            if not re.search(rb"/Type\s*/Page\b", body):
                continue
            fonts = self.font_map(body)
            refs = re.findall(rb"(\d+)\s+\d+\s+R", self._contents_field(body))
            # ВАЖНО: фрагменты склеиваем — Tf из одного действует в следующих
            joined = b"\n".join(self.streams.get(int(r), b"") for r in refs)
            if joined:
                out.append((joined, fonts))
            xdict = self._dict_slice(self._resources(body), b"/XObject") or b""
            for m in _NAME_REF.finditer(xdict):
                xnum = int(m.group(2))
                dec = self.streams.get(xnum)
                if not dec or b"BT" not in dec:
                    continue
                xfonts = self.font_map(self.objs.get(xnum, b"")) or fonts
                out.append((dec, xfonts))
        return out

    @staticmethod
    def _contents_field(body):
        m = re.search(rb"/Contents\s+(\d+\s+\d+\s+R)", body)
        if m:
            return m.group(1)
        arr = re.search(rb"/Contents\s*\[(.*?)\]", body, re.S)
        return arr.group(1) if arr else b""


def _parse_cmap(dec):
    table = {}
    for blk in re.findall(rb"beginbfchar(.*?)endbfchar", dec, re.S):
        for src, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk):
            table[int(src, 16)] = _utf16(dst)
    for blk in re.findall(rb"beginbfrange(.*?)endbfrange", dec, re.S):
        for lo, hi, dst in re.findall(
                rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk):
            a, b, base = int(lo, 16), int(hi, 16), int(dst, 16)
            for code in range(a, min(b, a + 4096) + 1):   # защита от мусорных диапазонов
                table[code] = chr(base + code - a)
    return table


def _utf16(hexstr):
    try:
        return bytes.fromhex(hexstr.decode()).decode("utf-16-be", "replace")
    except ValueError:
        return ""


def _parse_differences(enc):
    """/Differences[1/uni0436 2/afii10033 …] -> {код: символ}."""
    m = re.search(rb"/Differences\s*\[(.*?)\]", enc, re.S)
    if not m:
        return {}
    table, code = {}, 0
    for tok in re.findall(rb"(\d+)|/([A-Za-z0-9._]+)", m.group(1)):
        if tok[0]:
            code = int(tok[0])
            continue
        name = tok[1].decode("latin-1")
        ch = _glyph_char(name)
        if ch:
            table[code] = ch
        code += 1
    return table


def _glyph_char(name):
    m = re.match(r"^uni([0-9A-Fa-f]{4})$", name)
    if m:
        return chr(int(m.group(1), 16))
    m = re.match(r"^afii(\d{5})$", name)
    if m:
        num = int(m.group(1))
        if 10017 <= num <= 10049:
            return _AFII_UP[num - 10017]
        if 10065 <= num <= 10097:
            return _AFII_LOW[num - 10065]
        return ""
    return {"space": " ", "periodcentered": "·", "endash": "–", "emdash": "—",
            "quotedblleft": "«", "quotedblright": "»", "percent": "%"}.get(name, "")


def _unescape(s):
    out, i = bytearray(), 0
    escapes = {0x6E: 10, 0x72: 13, 0x74: 9, 0x62: 8, 0x66: 12}
    while i < len(s):
        ch = s[i]
        if ch == 0x5C and i + 1 < len(s):
            nxt = s[i + 1]
            if nxt in escapes:
                out.append(escapes[nxt])
                i += 2
                continue
            if 0x30 <= nxt <= 0x37:                       # восьмеричный код \ddd
                k = 0
                while k < 3 and i + 1 + k < len(s) and 0x30 <= s[i + 1 + k] <= 0x37:
                    k += 1
                out.append(int(s[i + 1:i + 1 + k].decode(), 8) & 0xFF)
                i += 1 + k
                continue
            out.append(nxt)
            i += 2
            continue
        out.append(ch)
        i += 1
    return bytes(out)


def _decode_str(raw, cmap):
    """Строка PDF -> текст. Ширину кода (1 или 2 байта) выбираем по попаданиям в CMap."""
    if not cmap:
        return raw.decode("cp1251", "replace")
    two = [int.from_bytes(raw[i:i + 2], "big") for i in range(0, len(raw) - 1, 2)]
    one = list(raw)
    hit2 = sum(1 for c in two if c in cmap)
    hit1 = sum(1 for c in one if c in cmap)
    codes = two if hit2 * 2 >= hit1 else one
    return "".join(cmap.get(c, "") for c in codes)


def pdf_text(data):
    """Байты PDF -> текст (по строкам). Пустая строка = текстового слоя нет."""
    pdf = _Pdf(data)
    lines = []
    for content, fonts in pdf.page_chunks():
        cur, buf = {}, []
        for m in _TOKEN.finditer(content):
            tok = m.group(0)
            if m.group(1):
                cur = fonts.get(m.group(1).decode("latin-1"), {})
            elif tok.startswith(b"("):
                buf.append(_decode_str(_unescape(tok[1:-1]), cur))
            elif tok.startswith(b"<"):
                hexs = re.sub(rb"\s", b"", tok[1:-1])
                if len(hexs) % 2:
                    hexs += b"0"
                try:
                    buf.append(_decode_str(bytes.fromhex(hexs.decode()), cur))
                except ValueError:
                    pass
            elif buf:
                lines.append("".join(buf))
                buf = []
        if buf:
            lines.append("".join(buf))
    return "\n".join(lines)


# ------------------------------------------------------------ разбор чисел
#
# ВСЁ сопоставление идёт по тексту БЕЗ пробелов (см. _norm): пробел в PDF —
# артефакт вёрстки, а не разделитель. В ORFR_2025-7.pdf «нетто-покупателями»
# приезжает как «нетт о-п окупателями», в ORFR_2025-9.pdf «156» — как «1 5 6».
# Сжатие снимает оба класса поломок разом, поэтому ключевые слова тоже сжаты.

def _squeeze(text):
    return re.sub(r"[\s  ]+", "", text)


def _keys(*words):
    return tuple(_squeeze(w) for w in words)


_CAT_KEYS = {
    "fiz": _keys("розничные инвестор", "розничных инвестор", "физические лица",
                 "физических лиц", "физлиц", "население"),
    "nfo_du": _keys("доверительного управления", "доверительном управлении",
                    "доверительному управлению"),
    "nfo_own": _keys("собственных средств", "собственными средствами",
                     "собственные средства", "счет собственных", "счёт собственных"),
    "szko": _keys("сзко", "системно значимые", "системно значимых"),
    "other_banks": _keys("прочие банки", "прочих банков", "прочими банками",
                         "прочие кредитные"),
    "nonres": _keys("нерезидент",),
}
# Знак берём из глагола, а не из пунктуации (грабля 3 в шапке модуля).
# «покупк» нужен отдельным корнем: ЦБ пишет и «нетто-покупки», и «совершившие
# покупки на 7,9 млрд» — без него фраза досталась бы соседнему «нетто-продавцами».
_BUY_WORDS = _keys("покупк", "покупател", "покупал", "приобрет", "приобрел",
                   "приобрёл", "купил", "выкупил", "вложил", "нарастил")
_SELL_WORDS = _keys("продаж", "продавц", "продавал", "продал", "реализовал",
                    "сократил", "избавля")

# Число + «млрд руб» уже в сжатом тексте.
_NUM_RE = re.compile(r"(\d[\d.,]*?)млрдруб")
# Вариант без «руб»: в перечислениях единица пишется один раз на конце
# («на 29,8 млрд и 17,6 млрд руб. соответственно»).
_NUM_ANY = re.compile(r"(\d[\d.,]*?)млрд")

# Контексты, в которых число — НЕ месячный нетто-поток категории.
_VETO = _keys("объем торгов", "объём торгов", "объем продаж", "среднедневн",
              "оборот", "размещени", "в среднем", "с начала года", "с начала лета",
              "с начала месяца", "в месяц", "за год", "год к году",
              # «суммарно на 47,4 млрд» — склейка двух категорий: разложить нельзя,
              # а записать в одну из них — соврать
              "суммарно", "совокупно", "в сумме", "в общей сложности")
_MONTH_STEMS = {1: ("январ",), 2: ("феврал",), 3: ("март",), 4: ("апрел",),
                5: ("мае", "мая", "май"), 6: ("июн",), 7: ("июл",), 8: ("август",),
                9: ("сентябр",), 10: ("октябр",), 11: ("ноябр",), 12: ("декабр",)}
# Сокращения, после которых точка — не конец предложения.
# «рублей.» сюда НЕ добавлять: это конец предложения, а не сокращение — иначе
# фраза склеивается с предыдущей и ловит вету по чужому месяцу.
_ABBR = ("руб", "млрд", "млн", "тыс", "г", "гг", "п", "пп", "рис",
         "коп", "проц", "шт", "долл")

_FAR = 10 ** 6
_MAX_DIST = 220          # дальше этого слово к числу уже не относится
_BACK, _FWD = 260, 100


def _norm(text):
    """Текст PDF -> одна строка в нижнем регистре и БЕЗ пробелов."""
    return _squeeze(text.replace("‑", "-")).lower()


def _nearest(hay, needles, at):
    """Ближайшее к позиции at вхождение любого из needles.

    Расстояние ВЗВЕШЕННОЕ: слово перед числом почти всегда его подлежащее, слово
    после — чаще подлежащее следующего числа. Без этой асимметрии фраза
    «в рамках ДУ реализовали акции на 16,8 млрд руб., за счет собственных
    средств – на 11,7 млрд» приписывает 16,8 собственным средствам.
    -> (взвешенное расстояние, слово).
    """
    lo, hi = max(0, at - _BACK), min(len(hay), at + _FWD)
    window = hay[lo:hi]
    best = (_FAR, None)
    for needle in needles:
        start = 0
        while True:
            idx = window.find(needle, start)
            if idx < 0:
                break
            raw = lo + idx - at
            weighted = -raw if raw < 0 else 20 + 3 * raw
            if weighted < best[0]:
                best = (weighted, needle)
            start = idx + 1
    return best


def _sentence_bounds(low, at):
    """Границы предложения вокруг позиции.

    Веты считаем в пределах предложения: соседняя фраза про другой месяц не должна
    отменять верное число. Точку после «руб.», «млрд», «г.» концом не считаем.
    """
    start, end = 0, len(low)
    for m in re.finditer(r"[.•]", low[:at]):
        tail = low[max(0, m.start() - 8):m.start()]
        if any(tail.endswith(a) for a in _ABBR):
            continue
        if m.end() < len(low) and low[m.end()].isdigit():   # «1.5» — не конец фразы
            continue
        start = m.end()
    for m in re.finditer(r"[.•]", low[at:]):
        pos = at + m.start()
        tail = low[max(0, pos - 8):pos]
        if any(tail.endswith(a) for a in _ABBR):
            continue
        if pos + 1 < len(low) and low[pos + 1].isdigit():
            continue
        end = pos
        break
    return start, end


def _vetoed(low, at, period):
    """Число описывает не месячный нетто-поток категории — пропускаем.

    Так ЦБ ломает наивный парсер: «(в августе объем продаж … 9,8 млрд руб., а в
    рамках ДУ НФО были покупателями акций на 2,4 млрд руб.)» — скобка про ПРОШЛЫЙ
    месяц внутри абзаца про текущий. Смотрим ТОЛЬКО то, что стоит перед числом в
    его предложении (плюс 12 символов после): придаточное после числа обычно
    относится к следующему числу.
    """
    start, end = _sentence_bounds(low, at)
    before = low[start:at]
    after = low[at:min(end, at + 12)]
    if any(word in before or word in after for word in _VETO):
        return True
    if not period:
        return False
    # Решает ПОСЛЕДНЕЕ упоминание месяца перед числом: в одном предложении легко
    # уживаются «в сентябре … (в августе … 2,4 млрд руб.)», и ближе к числу август.
    last_month, last_pos = None, -1
    for num, stems in _MONTH_STEMS.items():
        for stem in stems:
            pos = before.rfind(stem)
            if pos > last_pos:
                last_month, last_pos = num, pos
    return last_month is not None and last_month != int(period[5:7])


def _topic_is_equity(low, at):
    """Число относится к рынку АКЦИЙ, а не к ОФЗ/корпоративным облигациям?

    Смотрим ТОЛЬКО НАЗАД: тему задаёт заголовок или подлежащее выше по тексту.
    Грабля ORFR_2025-11: фраза про облигации заканчивается вплотную перед
    заголовком «рынок акций», и при взгляде вперёд 143 млрд СЗКО по облигациям
    уезжают в потоки по акциям.
    """
    lo = max(0, at - 360)
    pos_eq = low.rfind("акци", lo, at)
    if pos_eq < 0:
        return False
    d_eq = at - pos_eq
    d_bond = _FAR
    for word in ("офз", "облигаци"):
        pos = low.rfind(word, lo, at)
        if pos >= 0:
            d_bond = min(d_bond, at - pos)
    return d_eq < d_bond


def _to_float(raw):
    """«15,7» / «8823» -> 15.7 / 8823.0 (пробелы уже сняты в _norm)."""
    txt = raw.rstrip(".,").replace(",", ".")
    if txt.count(".") > 1:                  # «1.234.5» — мусор, лучше пропустить
        return None
    try:
        return float(txt)
    except ValueError:
        return None


def _pair_respectively(low, period):
    """Разбор конструкции «A и B … на X млрд и Y млрд руб. соответственно».

    Без неё месячные обзоры теряют разбивку НФО: ЦБ регулярно пишет две категории
    и два числа списком. Пары получают приоритет (cat_dist=0) — это прямая
    атрибуция, а не соседство в абзаце.
    """
    out = []
    for m in re.finditer("соответственно", low):
        seg_start, _ = _sentence_bounds(low, m.start())
        seg = low[seg_start:m.start()]
        hits = []
        for name, keys in _CAT_KEYS.items():
            for key in keys:
                start = 0
                while True:
                    idx = seg.find(key, start)
                    if idx < 0:
                        break
                    hits.append((idx, name))
                    start = idx + 1
        hits.sort()
        cats = []
        for _, name in hits:
            if not cats or cats[-1] != name:
                cats.append(name)
        nums = [(mm.start(), _to_float(mm.group(1))) for mm in _NUM_ANY.finditer(seg)]
        nums = [(pos, val) for pos, val in nums if val is not None]
        if len(cats) < 2 or len(cats) != len(nums):
            continue
        if hits and nums[0][0] < hits[-1][0]:      # числа должны идти ПОСЛЕ перечисления
            continue
        at = seg_start + nums[0][0]
        if _vetoed(low, at, period) or _vetoed(low, seg_start + nums[-1][0], period):
            continue
        if not _topic_is_equity(low, at):
            continue
        d_buy, w_buy = _nearest(low, _BUY_WORDS, at)
        d_sell, w_sell = _nearest(low, _SELL_WORDS, at)
        if min(d_buy, d_sell) > _MAX_DIST:
            continue
        sign = 1.0 if d_buy <= d_sell else -1.0
        for cat, (pos, val) in zip(cats, nums):
            out.append({"cat": cat, "value": round(sign * val, 2), "cat_dist": 0,
                        "word": (w_buy if sign > 0 else w_sell) + " (перечисление)",
                        "context": low[max(0, seg_start + pos - 120):seg_start + pos + 30]})
    return out


def parse_flows(text, period=None):
    """Текст обзора -> ({категория: млрд руб со знаком}, [кандидаты для аудита]).

    period ('YYYY-MM') нужен, чтобы отбросить числа про соседние месяцы.
    """
    low = _norm(text)
    best, audit = {}, []
    for m in _NUM_RE.finditer(low):
        at = m.start()
        if _vetoed(low, at, period):
            continue
        if not _topic_is_equity(low, at):
            continue
        cat, cat_dist = None, _FAR
        for name, keys in _CAT_KEYS.items():
            dist, _ = _nearest(low, keys, at)
            if dist < cat_dist:
                cat, cat_dist = name, dist
        if cat is None or cat_dist > _MAX_DIST:
            continue
        d_buy, w_buy = _nearest(low, _BUY_WORDS, at)
        d_sell, w_sell = _nearest(low, _SELL_WORDS, at)
        if min(d_buy, d_sell) > _MAX_DIST:
            continue
        sign = 1.0 if d_buy <= d_sell else -1.0
        value = _to_float(m.group(1))
        if value is None:
            continue
        rec = {"cat": cat, "value": round(sign * value, 2), "cat_dist": cat_dist,
               "word": w_buy if sign > 0 else w_sell,
               "context": low[max(0, at - 120):at + 30]}
        audit.append(rec)
        # побеждает кандидат с самой близкой категорией: прямое «X купил на N»
        # сильнее, чем число, случайно попавшее в тот же абзац
        if cat not in best or cat_dist < best[cat]["cat_dist"]:
            best[cat] = rec
    for rec in _pair_respectively(low, period):
        audit.append(rec)
        if rec["cat"] not in best or best[rec["cat"]]["cat_dist"] > 0:
            best[rec["cat"]] = rec
    return {cat: rec["value"] for cat, rec in best.items()}, audit


# ------------------------------------------------------------- поиск свежего PDF

_MONTHS_RU = {"январ": 1, "феврал": 2, "март": 3, "апрел": 4, "мая": 5, "май": 5,
              "июн": 6, "июл": 7, "август": 8, "сентябр": 9, "октябр": 10,
              "ноябр": 11, "декабр": 12}


def _period_from_name(url):
    """'/File/59728/ORFR_2026-2.pdf' -> '2026-02'. Склейки вида
    'ORFR_2025-12_2026-01' относим к ПОСЛЕДНЕМУ месяцу периода."""
    names = re.findall(r"(\d{4})-(\d{1,2})", url.rsplit("/", 1)[-1])
    if not names:
        return None
    year, month = names[-1]
    return "%04d-%02d" % (int(year), int(month))


def latest_pdf(html):
    """HTML страницы обзора -> (абсолютный URL, период 'YYYY-MM') самого свежего PDF."""
    best = None
    for href in re.findall(r'href="([^"]+\.pdf)"', html, re.I):
        if "orfr" not in href.lower():
            continue
        period = _period_from_name(href)
        if not period:
            continue
        if best is None or period > best[1]:
            best = (href if href.startswith("http") else BASE + href, period)
    return best if best else (None, None)


def _month_end(period):
    year, month = int(period[:4]), int(period[5:7])
    nxt = (year + 1, 1) if month == 12 else (year, month + 1)
    return (date(nxt[0], nxt[1], 1) - timedelta(days=1)).isoformat()


def _get_pdf(url):
    """PDF — бинарь, поэтому get_bytes, а не get_text."""
    return get_bytes(url, headers=_UA, timeout=120)


def _meta(status, url, note=None, extra=None):
    meta = {"source": "cbr_orfr", "url": url, "status": status, "note": note,
            "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    if extra:
        meta.update(extra)
    return meta


def _manual_fallback(reason, url):
    """Ручной ввод из inputs/orfr.yml. Импорт ленивый: имя пакета зависит от того,
    как run.py кладёт pipeline в sys.path."""
    try:
        from . import manual as manual_mod
    except ImportError:
        try:
            from fetch import manual as manual_mod
        except ImportError:
            import manual as manual_mod
    points, path, sources = manual_mod.orfr_manual()
    status = "manual_needed" if points else "error"
    note = reason + ("; взято из %s" % path if points else "; inputs/orfr.yml пуст")
    meta = _meta(status, url, note, {"manual_source": path, "manual_refs": sources})
    return _flatten(points, meta)


def _flatten(points, meta):
    """{дата: {cat: v}} -> [("orfr_flows_fiz", {дата: v}, meta), …].

    Стор по контракту §1 хранит ЧИСЛА, а не словари: так же уже разложены КБД
    (zcyc_y1, zcyc_y2, …) и открытые позиции (futoi_mx_pos, …). Разнобой стоил бы
    отдельной ветки в сторе, в панели и в мониторах — а мониторы и без того ищут
    именно плоские `orfr_flows_<категория>`.
    """
    out = []
    for cat in CATEGORIES:
        vals = {day: row[cat] for day, row in points.items()
                if isinstance(row, dict) and row.get(cat) is not None}
        out.append(("%s_%s" % (SERIES_ID, cat), vals, dict(meta)))
    return out


def flows(url=None):
    """-> [("orfr_flows_<категория>", {последний день месяца: млрд руб}, meta), …].

    Статусы: ok (разобрали ≥2 категории), manual_needed (взяли inputs/orfr.yml),
    error (нет ни того, ни другого). Исключений наружу не кидаем: провал одного
    источника не валит прогон (CONTRACT.md §0).
    """
    pdf_url, period = url, (_period_from_name(url) if url else None)
    if pdf_url is None:
        try:
            html = get_text(INDEX_URL, headers=_UA)
        except FetchError as exc:
            return _manual_fallback("страница обзора не открылась: %s" % exc, INDEX_URL)
        pdf_url, period = latest_pdf(html)
        if not pdf_url:
            return _manual_fallback("на странице обзора не нашлось ссылок на PDF",
                                    INDEX_URL)
    try:
        data = _get_pdf(pdf_url)
    except FetchError as exc:
        return _manual_fallback(str(exc), pdf_url)
    try:
        text = pdf_text(data)
    except (zlib.error, ValueError, UnicodeError) as exc:
        return _manual_fallback("PDF не разобрался: %s" % exc, pdf_url)
    if len(text) < 2000:
        return _manual_fallback("в PDF нет текстового слоя (%d симв.)" % len(text),
                                pdf_url)
    found, audit = parse_flows(text, period)
    if len(found) < 2:
        return _manual_fallback("в тексте нашлось %d категорий из %d"
                                % (len(found), len(CATEGORIES)), pdf_url)
    key = _month_end(period) if period else None
    if key is None:
        return _manual_fallback("не определился период выпуска", pdf_url)
    check = _selfcheck(period, found)
    meta = _meta("ok", pdf_url, "разобрано категорий: %d" % len(found),
                 {"asof": period, "selfcheck": check, "candidates": audit[:20],
                  "text_chars": len(text)})
    return _flatten({key: dict(found)}, meta)


def _selfcheck(period, found):
    """Сверка с реперами задания. Не правит данные — только сообщает расхождение."""
    ref = SELF_CHECK.get(period)
    if not ref:
        return "нет репера на %s" % period
    bad = ["%s: %.1f против %.1f" % (k, found[k], v)
           for k, v in ref.items() if k in found and abs(found[k] - v) > 0.15]
    missing = [k for k in ref if k not in found]
    if not bad and not missing:
        return "ok"
    return "расхождения: %s; не найдено: %s" % ("; ".join(bad) or "нет",
                                                ", ".join(missing) or "нет")


def split_categories(points):
    """{дата: {cat: v}} -> {'orfr_flows.fiz': {дата: v}, …} для плоского хранения."""
    out = {}
    for day, row in points.items():
        for cat, value in row.items():
            if cat in CATEGORIES:
                out.setdefault("%s.%s" % (SERIES_ID, cat), {})[day] = value
    return out
