"""Веб-превью Telegram (`t.me/s/<канал>`) как запасной транспорт для релизов ведомств.

Зачем. `minfin.gov.ru` отвечает **503 с прод-машины** на любые заголовки, включая
полный браузерный набор (проверено 12.08.2026: с ноутбука пользователя — 200, с VPS
Hetzner — 503; TLS при этом в порядке, сертификат GlobalSign проверяется). Режет WAF по
диапазонам датацентров, и починить это заголовками нельзя. Из-за этого три ряда —
`ngd`, `budget_deficit`, `fnb` — не собрались НИ РАЗУ с самой установки конвейера.

Тот же самый текст релиза ведомство публикует в своём телеграм-канале, а канал
отдаётся по HTTPS без авторизации, с прод-машины доступен и содержит:
  * ISO-время публикации в атрибуте `<time datetime>` — не «сегодня», не «5 часов
    назад», а точная отметка, из которой считается дата периода;
  * полный текст сообщения с числами — те же формулировки, что и на сайте, поэтому
    разбор числа переиспользуется как есть (`minfin._press_number`).

Это ЗАПАСНОЙ путь, а не замена: приоритет всегда у первоисточника, зеркало включается
только когда сайт не открылся. Источник числа виден в `meta.url` (ссылка на конкретное
сообщение) и в `meta.mirror`.

Грабли, из-за которых код выглядит именно так:

1. **Перепечатки чужих новостей.** Канал Минфина вперемешку с собственными релизами
   публикует дайджесты («📰 Интерфакс: …», «📰 ТАСС: …», «📰 Frank Media: …») — и в них
   те же ключевые слова и ДРУГИЕ числа (пересказ за другой период, округление).
   Например 06.08.2026 в канале вышел дайджест про нефтегазовые доходы за июль, а
   собственный релиз Минфина про операции на август — днём раньше. Взять дайджест
   значит тихо подменить ряд чужим числом, поэтому сообщения с газетным маркером в
   начале отбрасываются (`is_reprint`).
2. **Вложенные `<div>` в тексте сообщения.** Наивное `(.*?)</div>` обрывает текст на
   первой же цитате или опросе внутри сообщения. Границы блока считаются честно.
3. **Время в превью — UTC.** `datetime="2026-08-11T16:15:12+00:00"` — это 19:15 МСК.
   Дата периода у Минфина считается по московскому дню, поэтому отметка переводится
   в МСК до того, как из неё сделают дату (иначе вечерний релиз уехал бы на сутки).
4. **Пагинация.** На странице канала ~20 последних сообщений; месячный релиз
   при трёх постах в день уходит за эту границу за неделю. Страницы листаются
   параметром `?before=<id>` — от свежих к старым, с жёстким потолком.
"""

import html as html_mod
import re
from urllib.parse import quote
from datetime import datetime, timedelta, timezone

try:                                       # прод: общий HTTP-слой (CONTRACT.md §4)
    from lib.http import get_text, FetchError
except ImportError:
    try:
        from pipeline.lib.http import get_text, FetchError
    except ImportError:                    # автономный запуск (отладка парсеров)
        class FetchError(Exception):
            pass

        def get_text(url, timeout=30, headers=None, **_kw):
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

# Превью канала отдаётся только браузерному UA: на «python-urllib» t.me присылает
# страницу-заглушку с предложением открыть приложение.
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
       "Accept-Language": "ru,en;q=0.8"}

PREVIEW = "https://t.me/s/%s"
MSK = timezone(timedelta(hours=3))
MAX_PAGES = 8            # ~160 сообщений: месячный релиз укладывается с запасом
_BLOCK = 'class="tgme_widget_message '
_TEXT_OPEN = re.compile(r'<div class="tgme_widget_message_text[^"]*"[^>]*>')
_TIME = re.compile(r'<time[^>]+datetime="([^"]+)"')
_POST = re.compile(r'data-post="([^"]+)"')
_DIV = re.compile(r"<(/?)div\b", re.I)
# Газетный маркер в начале сообщения = перепечатка чужой новости, а не релиз ведомства.
_REPRINT_HEAD = re.compile(r"^.{0,24}?📰")


def _plain(fragment):
    """HTML сообщения -> плоский текст с сохранёнными переводами строк.

    `<br>` и конец абзаца превращаются в перевод строки: заголовок релиза стоит
    первой строкой, и по ней потом ищут ключевые слова.
    """
    text = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.I)
    text = re.sub(r"</(p|div|blockquote)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    text = text.replace("\xa0", " ").replace(" ", " ").replace("​", "")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line)


def _text_block(block):
    """Текст сообщения с честным подсчётом вложенных <div>.

    Наивное `(.*?)</div>` обрывается на первом вложенном блоке (цитата, опрос,
    ссылка-превью), и число из середины релиза в текст уже не попадает.
    """
    m = _TEXT_OPEN.search(block)
    if not m:
        return ""
    depth, pos = 1, m.end()
    for tag in _DIV.finditer(block, m.end()):
        depth += -1 if tag.group(1) else 1
        if depth == 0:
            return _plain(block[pos:tag.start()])
    return _plain(block[pos:])             # блок не закрылся — берём хвост как есть


def is_reprint(text):
    """Перепечатка чужой новости («📰 ТАСС: …»), а не собственный релиз ведомства."""
    return bool(_REPRINT_HEAD.match((text or "").lstrip()))


def headline(text):
    """Первая СОДЕРЖАТЕЛЬНАЯ строка сообщения.

    Не просто `text.split(chr(10))[0]`: релизы часто начинаются со строки из одних
    эмодзи (у Минфина «📈 📈», дальше уже «О результатах размещения средств Фонда
    национального благосостояния»). Взять первую строку буквально — значит искать
    ключевые слова в двух картинках и не найти релиз никогда.
    """
    for line in (text or "").split("\n"):
        if len(re.findall(r"[А-Яа-яЁёA-Za-z]", line)) >= 3:
            return line.strip()
    return (text or "").split("\n", 1)[0].strip()


def _when(raw):
    """'2026-08-11T16:15:12+00:00' -> (datetime в МСК, '2026-08-11').

    Дата — МОСКОВСКАЯ: превью отдаёт UTC, и релиз, вышедший в 19:15 МСК, без
    перевода лёг бы в ряд датой предыдущего дня.
    """
    try:
        stamp = datetime.fromisoformat(str(raw).strip())
    except ValueError:
        return None, None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    msk = stamp.astimezone(MSK)
    return msk, msk.date().isoformat()


def parse_page(page_html, channel=""):
    """HTML превью -> [{id, url, published, at, head, text}] от старых к свежим."""
    out = []
    for block in page_html.split(_BLOCK)[1:]:
        text = _text_block(block)
        if not text:
            continue                       # сообщение без текста: фото, опрос, стикер
        m_time, m_post = _TIME.search(block), _POST.search(block)
        at, published = _when(m_time.group(1)) if m_time else (None, None)
        post = m_post.group(1) if m_post else ""
        try:
            msg_id = int(post.rsplit("/", 1)[-1])
        except (ValueError, IndexError):
            msg_id = None
        out.append({"id": msg_id,
                    "url": "https://t.me/%s" % (post or channel),
                    "published": published, "at": at,
                    "head": headline(text), "text": text})
    return out


def messages(channel, pages=1, timeout=25, query=None):
    """Сообщения канала, свежие ПЕРВЫМИ. Отказ сети -> FetchError, как у всех фетчеров.

    `pages` листает вглубь параметром `?before=`; потолок MAX_PAGES защищает от
    бесконечного обхода, если разметка t.me однажды перестанет отдавать id постов.

    `query` включает ПОИСК по каналу (`?q=`). Без него лента отдаёт только два-три
    десятка последних сообщений, и всё, что старше недели, недостижимо: у канала с
    десятком постов в день месячная новость уходит за горизонт за считанные дни.
    Поиск же индексирует историю на годы назад и листается тем же `before`.
    """
    collected, seen, before = [], set(), None
    for _ in range(max(1, min(int(pages), MAX_PAGES))):
        params = []
        if query:
            params.append("q=" + quote(str(query)))
        if before:
            params.append("before=%d" % before)
        url = PREVIEW % channel + ("?" + "&".join(params) if params else "")
        batch = parse_page(get_text(url, headers=_UA, timeout=timeout), channel)
        fresh = [m for m in batch if m["id"] is None or m["id"] not in seen]
        if not fresh:
            break
        seen.update(m["id"] for m in fresh if m["id"] is not None)
        collected = fresh + collected
        ids = [m["id"] for m in fresh if m["id"] is not None]
        if not ids:
            break                          # без id листать нечем
        before = min(ids)
    collected.sort(key=lambda m: (m["at"] is not None, m["at"]), reverse=True)
    return collected


def find(channel, keywords, pages=MAX_PAGES, skip_reprints=True):
    """Сообщения канала, у которых В ЗАГОЛОВКЕ есть все ключевые слова.

    Заголовок — первая строка сообщения: у релизов ведомств это ровно то же самое,
    что заголовок новости на сайте, а у дайджестов — имя перепечатанного издания.
    """
    keys = [k.lower() for k in keywords]
    out = []
    for msg in messages(channel, pages=pages):
        if skip_reprints and is_reprint(msg["text"]):
            continue
        if all(k in msg["head"].lower() for k in keys):
            out.append(msg)
    return out
