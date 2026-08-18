"""События в телеграм и в ленту хаба NEXUS (docs/CONTRACT.md §6): ТОЛЬКО переходы,
не состояния.

ПОЧЕМУ только переходы: панель и так показывает состояние; уведомление ценно ровно
в момент смены. Ежедневное «сегодня по-прежнему токсичная ячейка» читатель выключает
через неделю, и вместе с ним выключает настоящий алерт.

ПОЧЕМУ состояние прошлого прогона лежит в файле, а не выводится из data.json: часть
переходов (доставлен ли алерт, о каком знаке ядра уже сообщали) в публикуемый payload
не попадает — иначе гистерезис ядра пришлось бы восстанавливать по истории каждый раз.

ПОЧЕМУ pending: телеграм лежит регулярно. Недоставленное событие остаётся в очереди
и повторяется на следующем прогоне (сутки), маркер дедупа двигает только успех.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pipeline.lib import commentary, constants, nexus, telegram, wording

STATE_NAME = "alerts_state.json"
FEED_LIMIT = 20          # столько последних событий уезжает в data.json
PENDING_MAX_HOURS = 24   # старше — не повторяем: новость протухла
PENDING_MAX = 40         # длина очереди повторов; режем СТАРОЕ, а не свежее
DEPOSIT_UPTICK_PP = 0.05  # декадная ставка шумит в сотых, порог отсекает дрожь
# ВСЕ поля события, которые обязаны пережить очередь повторов и ленту. Плоский
# text — свёртка для журнала витрины (контракт §3), но телеграм рендерится из
# СТРУКТУРЫ: render_ops вообще не читает text, а render_market без title заворачивает
# весь текст в одну жирную строку. Когда 14.08 события стали структурами, этот
# кортеж не расширили — и повтор из pending уходил владельцу пустым слэгом
# «842 · source_stale» без факта и «куда смотреть», ровно в аварии, ради которой
# очередь существует (аудит 18.08.2026). merged — ключи свёрнутых переходов: без
# него повтор слал бы слитую тройку заново по отдельности.
EVENT_FIELDS = ("key", "ts", "kind", "severity", "text", "comment",
                "title", "before", "after", "moves", "detail", "meaning",
                "fact", "where", "merged")

# САНИТАРНЫЕ события — отказы обвязки, а не движение рынка. Они уходят в общий
# ops-канал панелей и НЕ попадают ни в журнал витрины, ни в ленту хаба.
#
# ПОЧЕМУ разделили: журнал читают как ленту рынка, а «источник moex_press: error»
# рынку ничего не сообщает. Вперемешку они гасят друг друга — за неделю отказов
# владелец перестаёт открывать журнал, и вместе с ними мимо проходит смена ячейки.
# Ровно это и случилось: в журнале из четырёх записей три были про источники.
OPS_KINDS = frozenset({
    "source_stale", "health_dead", "health_review_due", "lease_lost", "payload_oversize",
    "core_missing",
})


def is_ops(event):
    return (event or {}).get("kind") in OPS_KINDS


def state_dir():
    env = (os.environ.get("STATE_DIR") or "").strip()
    return Path(env) if env else Path(__file__).resolve().parents[1] / ".state"


def _state_path():
    return state_dir() / STATE_NAME


def load_state():
    """Состояние прошлого прогона. Значения неверных типов выбрасываем.

    ПОЧЕМУ проверяем типы: файл переживает падения прогона, ручные правки и откаты
    версий. Один раз попавший в pending мусор (строка вместо списка событий) валил
    весь прогон в alerts.run ДО публикации — панель не обновлялась из-за алерта.
    """
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    for key in ("pending", "feed"):
        data[key] = [e for e in data.get(key) or [] if isinstance(e, dict) and e.get("key")] \
            if isinstance(data.get(key), list) else []
    if not isinstance(data.get("last"), dict):
        data.pop("last", None)
    return data


def save_state(state):
    path = _state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        return True
    except OSError:
        return False


def _iso(dt=None):
    return (dt or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _num(v, d=1, plus=False):
    """Число так, как его пишет сама панель и соседние 837/838: запятой."""
    return wording.num(v, d, plus)


def _pct(v, d=1, plus=False):
    return wording.pct(v, d, plus)


def _mons(payload):
    return {t.get("id"): t for t in (payload.get("monitors") or []) if isinstance(t, dict)}


def _mp(mons, tid, key, default=None):
    return ((mons.get(tid) or {}).get("payload") or {}).get(key, default)


def _ev(key, kind, title, severity="info", now=None, **parts):
    """Событие как СТРУКТУРА, а не готовая строка.

    До 14.08.2026 правило сразу склеивало текст, и один и тот же кусок уезжал и в
    телеграм, и в журнал витрины, и в ленту хаба — а им нужен разный вид: телеграму
    жирный заголовок и разметка, журналу простая фраза, хабу заголовок отдельной
    строкой. Отсюда поля: `title` — о чём событие, `before`/`after` — движение,
    `detail` — подробность, `meaning` — что это значит для читателя. Плоский `text`
    собирается из них один раз (`lib/wording.plain_text`) и остаётся в контракте §3.
    """
    event = {"key": key, "kind": kind, "severity": severity,
             "title": str(title).strip(), "ts": _iso(now)}
    for name in ("before", "after", "moves", "detail", "meaning",
                 "fact", "where"):
        if parts.get(name):
            event[name] = parts[name]
    event["text"] = wording.plain_text(event)
    return event


def snapshot(payload, now=None):
    """Что запоминаем до следующего прогона. Только то, по чему ищем переходы."""
    core = payload.get("core") or {}
    cur = (payload.get("states") or {}).get("current") or {}
    mons = _mons(payload)
    return {
        "asof": payload.get("asof_trading_day"),
        "core_value": core.get("value"),
        "core_sign": core.get("sign"),
        "cell": (payload.get("verdict") or {}).get("cell_code"),
        "trend": cur.get("trend"), "vol": cur.get("vol"), "bond": cur.get("bond"),
        "health": (core.get("health") or {}).get("status"),
        "health_review_due": bool((core.get("health") or {}).get("review_due")),
        "key_rate": _mp(mons, "cb_meeting", "key_rate"),
        "deposit": _mp(mons, "deposit_spread", "deposit_pct"),
        "orfr_asof": (mons.get("orfr") or {}).get("asof"),
        "auction_date": _mp(mons, "ofz_auctions", "date"),
        "sources": {k: (v or {}).get("status") for k, v in (payload.get("sources") or {}).items()
                    if isinstance(v, dict)},
        "updated_at": _iso(now),
    }


# -------------------------------------------------------------------- правила

def _core_label(value):
    """Словесная подпись значения оценки — та же лестница, что на панели."""
    if not isinstance(value, (int, float)):
        return ""
    for low, high, word in constants.CORE_LABELS:
        if low <= value < high:
            return word
    return ""


def _core_flip(payload, prev, state, now):
    """Разворот ядра с гистерезисом: сообщаем, только когда |value| ушёл за порог.

    Без гистерезиса композит, болтающийся около нуля, шлёт по алерту в день —
    ровно это и заставило зафиксировать CORE_FLIP_HYSTERESIS в валидации.
    """
    core = payload.get("core") or {}
    val = core.get("value")
    if not isinstance(val, (int, float)):
        return []
    hyst = constants.CORE_FLIP_HYSTERESIS
    sign = 1 if val > hyst else (-1 if val < -hyst else 0)
    if sign == 0:
        return []
    prev_sign = state.get("core_sign_alerted")
    # «Было X» берём из ЗАФИКСИРОВАННОГО знака, а не из вчерашнего снимка: путь
    # 0.66 → 0.02 → −0.02 → −0.66 давал текст «развернулось: −0.66, было −0.02» —
    # оба числа одного знака, сообщение выглядит ошибочным ровно там, ради чего
    # гистерезис и введён.
    prev_value = state.get("core_value_alerted")
    state["core_sign_alerted"] = sign
    state["core_value_alerted"] = val
    if prev_sign is None or prev_sign == sign:
        # Первый прогон запоминает знак молча: иначе установка пайплайна начинается
        # с алерта «разворот», которого не было.
        return []
    if not isinstance(prev_value, (int, float)):
        prev_value = prev.get("core_value")  # состояние от старой версии без core_value_alerted
    label = (payload.get("verdict") or {}).get("core_label") or ""
    was_label = _core_label(prev_value)
    since = core.get("sign_since")
    return [_ev(f"core_flip:{sign}:{payload.get('asof_trading_day')}", "core_flip",
                "Оценка рынка на месяц вперёд развернулась "
                + ("вверх" if sign > 0 else "вниз"),
                now=now,
                before=f"{_num(prev_value, 2, True)}{f' ({was_label})' if was_label else ''}",
                after=f"{_num(val, 2, True)}{f' ({_core_label(val)})' if _core_label(val) else ''}",
                detail=(f"Знак держится с {wording.ru_day(since)}." if since else ""),
                meaning="Шкала — от −3 до +3: чем дальше от нуля, тем увереннее "
                        "перевес в эту сторону. Это оценка направления на месяц, "
                        "а не обещание доходности.")]


def _cell_change(payload, prev, now):
    verdict = payload.get("verdict") or {}
    cell = verdict.get("cell_code")
    old = prev.get("cell")
    if not cell or not old or cell == old:
        return []
    stats = verdict.get("cell_stats") or {}
    label = verdict.get("cell_label") or ""
    sev = "warn" if (stats.get("mean_fwd1m_pct") or 0) < 0 else "info"
    return [_ev(f"cell:{payload.get('asof_trading_day')}:{cell}", "state_cell_change",
                "Режим рынка сменился"
                + (f": {wording.regime_name(label)}" if wording.regime_name(label) else ""),
                sev, now,
                before=wording.cell_words(old),
                after=wording.cell_words(cell),
                meaning=wording.cell_plain(stats))]


def _bond_flag(payload, prev, now):
    cur = (payload.get("states") or {}).get("current") or {}
    new, old = cur.get("bond"), prev.get("bond")
    if new is None or old is None or new == old:
        return []
    dist = ""
    for row in (payload.get("states") or {}).get("distances") or []:
        if row.get("id") == "bond" and row.get("text"):
            dist = wording.sentence(wording.ru_decimals(row["text"]))
            break
    asof = payload.get("asof_trading_day")
    if new == 1:
        return [_ev(f"bond_on:{asof}", "bond_flag_on",
                    "Долговой рынок вошёл в стресс", "warn", now,
                    before="ОФЗ спокойны", after="ОФЗ под давлением",
                    detail=dist,
                    meaning="Индекс гособлигаций RGBI ушёл заметно ниже своего "
                            "годового максимума. Пока так, покупка просадок в акциях "
                            "не работает: на истории в такие месяцы она приносила "
                            "убыток, а не прибыль.")]
    return [_ev(f"bond_off:{asof}", "bond_flag_off",
                "Долговой рынок вышел из стресса", "info", now,
                before="ОФЗ под давлением", after="ОФЗ спокойны",
                detail=dist,
                meaning="Гособлигации отыграли просадку. Покупка просадок в акциях "
                        "снова имеет смысл: на спокойном долге такие месяцы в "
                        "среднем закрывались в плюс примерно в двух случаях из трёх.")]


def _buy_window(payload, prev, now):
    cur = (payload.get("states") or {}).get("current") or {}
    vol, bond = cur.get("vol"), cur.get("bond")
    if vol != 1 or bond != 0:
        return []
    # Та же защита, что в _bond_flag: неизвестное прошлое — не переход. Состояния
    # приходят с None после дня, когда панель не посчиталась (нет RGBI), и без
    # этой строки следующий прогон зовёт «Окно входа» о ячейке, которая не менялась.
    if prev.get("vol") is None or prev.get("bond") is None:
        return []
    if prev.get("vol") == 1 and prev.get("bond") == 0:
        return []
    trend = cur.get("trend")
    prev_code = "%s|%s|%s" % ("bull" if prev.get("trend") == 1 else "bear",
                             "stress" if prev.get("vol") == 1 else "calm",
                             "stress" if prev.get("bond") == 1 else "ok")
    stats = constants.CELL_STATS.get((trend, 1, 0)) or {}
    return [_ev(f"buy_window:{payload.get('asof_trading_day')}", "buy_window_open",
                "Окно входа: паника в акциях при спокойном долге", "info", now,
                before=wording.cell_words(prev_code), after=wording.cell_words(
                    f"{'bull' if trend == 1 else 'bear'}|stress|ok"),
                detail="Акции трясёт, а гособлигации держатся — редкое сочетание: "
                       "продают из-за страха, а не из-за проблем с деньгами в системе.",
                meaning=wording.cell_plain(stats))]


def _cb(payload, prev, now):
    mons = _mons(payload)
    tile = mons.get("cb_meeting") or {}
    pl = tile.get("payload") or {}
    out = []
    days, nxt = pl.get("days_left"), pl.get("next_meeting")
    key_rate, cons = pl.get("key_rate"), pl.get("consensus")
    if nxt and days == 1:
        priced = wording.ru_decimals(pl.get("priced_text") or "")
        waited = (f", аналитики ждут {_pct(cons, 2)}"
                  if isinstance(cons, (int, float))
                  else ", консенсус аналитиков в данные не внесён")
        out.append(_ev(f"cb_reminder:{nxt}", "cb_reminder",
                       "Завтра заседание Банка России", "info", now,
                       detail=f"Сейчас ключевая ставка {_pct(key_rate, 2)}{waited}.",
                       meaning=wording.sentence(priced)))
    old_rate = prev.get("key_rate")
    if isinstance(key_rate, (int, float)) and isinstance(old_rate, (int, float)) \
            and abs(key_rate - old_rate) > 1e-9:
        delta_bp = round((key_rate - old_rate) * 100)
        # Сюрприз меряется от консенсуса ТОЛЬКО ЧТО ПРОШЕДШЕГО заседания, а не
        # ближайшего будущего. Новая ставка попадает в ряд key_rate на 1–3 рабочих
        # дня позже решения (16 из 17 смен с 2023 — ровно +3 дня), и к этому моменту
        # pl['consensus'] уже смотрит на СЛЕДУЮЩЕЕ заседание: сюрприз считался бы от
        # чужих ожиданий, а при пустой строке будущего — «не внесён» при внесённом.
        # Поле last_consensus тайл кладёт ровно для этого (monitors._t_cb_meeting) и
        # сам гасит его через неделю после заседания. Фолбэка на pl['consensus'] нет
        # НАМЕРЕННО: честное «сказать нечем» лучше сюрприза от чужих ожиданий.
        cons = pl.get("last_consensus")
        surprise = round((key_rate - cons) * 100) if isinstance(cons, (int, float)) else None
        if surprise is None:
            meaning = ("Консенсус аналитиков в данные не внесён — сказать, совпало ли "
                       "решение с ожиданиями, нечем.")
        elif surprise == 0:
            meaning = "Решение совпало с тем, чего ждали аналитики."
        else:
            meaning = (f"Это на {_num(abs(surprise), 0)} базисных пунктов "
                       + ("выше" if surprise > 0 else "ниже")
                       + f" ожиданий аналитиков ({_pct(cons, 2)}): рынку придётся "
                       + "переставлять свои прогнозы.")
        step = "снизил" if delta_bp < 0 else "повысил"
        out.append(_ev(f"cb_decision:{payload.get('asof_trading_day')}:{key_rate}",
                       "cb_decision", f"Банк России {step} ключевую ставку",
                       "warn" if surprise not in (0, None) else "info", now,
                       before=_pct(old_rate, 2), after=_pct(key_rate, 2),
                       detail=f"Шаг {_num(delta_bp, 0, True)} базисных пунктов.",
                       meaning=meaning))
    return out


# «Публикация ОРФР» — про СВЕЖИЙ релиз. Данные месячные, выходят в середине
# следующего месяца: 45 суток покрывают самый поздний релиз с запасом.
ORFR_FRESH_DAYS = 45


def _orfr(payload, prev, now):
    tile = _mons(payload).get("orfr") or {}
    asof = tile.get("asof")
    if not asof or not prev.get("orfr_asof") or asof == prev.get("orfr_asof"):
        return []
    # Смена asof НАЗАД или на глубокую древность — переезд источника либо
    # восстановление стора, а не публикация: подписчикам уходила бы новость о
    # потоках трёхмесячной давности, поданная как сегодняшняя. Тот же класс, что
    # инцидент аукционов 12.08 (там уже стоит проверка возраста).
    if asof < prev.get("orfr_asof") or (_age_days(asof, now) or 0) > ORFR_FRESH_DAYS:
        return []
    pl = tile.get("payload") or {}
    exhaust = wording.ru_decimals((pl.get("seller_exhaustion") or {}).get("text") or "")
    head = wording.ru_decimals(tile.get("headline") or "").rstrip(".")
    return [_ev(f"orfr:{asof}", "orfr_published",
                "Банк России опубликовал, кто покупал и продавал акции", "info", now,
                detail=(f"{wording.ru_month(asof).capitalize()}: {head}." if head else ""),
                meaning=(wording.sentence(exhaust) if exhaust else
                         "Это единственный официальный ответ на вопрос, чьи деньги "
                         "двигали рынок в прошлом месяце."))]


# Тревога об аукционе — про СЕГОДНЯШНИЙ провал. Аукционы идут по средам, четырёх
# суток хватает и на вечернюю публикацию, и на пропущенный такт.
AUCTION_FRESH_DAYS = 4


def _age_days(day, now):
    """Сколько суток назад была дата тайла. None — если дата не разобралась."""
    try:
        when = datetime.strptime(str(day)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return (now - when).days


def _auction(payload, prev, now):
    tile = _mons(payload).get("ofz_auctions") or {}
    pl = tile.get("payload") or {}
    date_ = pl.get("date")
    if not pl.get("failed") or not date_ or date_ == prev.get("auction_date"):
        return []
    # Дата тайла меняется НЕ ТОЛЬКО когда прошёл новый аукцион: переезд на другой
    # источник, починка ряда или восстановление стора сдвигают её задним числом.
    # 12.08.2026 так и вышло — ряд переехал на биржевую доску, «последним» стал
    # реальный аукцион 15.07 вместо нуля затравки от 05.08, и правило разослало
    # «аукцион провален» про день четырёхнедельной давности. Смена записи в истории
    # не событие рынка; событие — свежий провал.
    age = _age_days(date_, now)
    if age is None or age > AUCTION_FRESH_DAYS:
        return []
    # Спрос бывает пустым штатно: биржа его не раскрывает. «при спросе н/д млрд» —
    # единица, приклеенная к отсутствующему числу (та же ошибка, что уже правили на
    # тайле), поэтому про молчание говорим словами.
    demand = pl.get("demand_bn")
    tail = (f", при спросе {_num(demand, 1)} млрд рублей"
            if isinstance(demand, (int, float)) else "; спрос биржа не раскрывает")
    return [_ev(f"auction_failed:{date_}", "auction_failed",
                "Аукцион ОФЗ не состоялся", "warn", now,
                detail=f"{wording.ru_day(date_)}: размещено "
                       f"{_num(pl.get('placed_bn'), 1)} млрд рублей{tail}.",
                meaning="Минфин не захотел занимать дороже и отказался давать премию "
                        "к рынку. Занимать всё равно придётся — позже и, скорее всего, "
                        "длинными выпусками: давление переезжает на дальний конец кривой.")]


def _deposit(payload, prev, state, now):
    """Рост ставок по вкладам — от ПОСЛЕДНЕГО ОБЪЯВЛЕННОГО уровня, не от вчерашнего.

    Якорь в снимке двигался каждый прогон, и ползучий цикл повышений по 0,03–0,04
    п.п. за декаду (+1 п.п. за квартал) не давал события НИКОГДА — каждый шаг
    порознь меньше порога шума. Та же ошибка якоря уже чинилась у _core_flip
    (core_value_alerted): сравниваем с уровнем, о котором СООБЩАЛИ, а снижение
    опускает якорь ко дну, чтобы следующий цикл мерялся от него.
    """
    tile = _mons(payload).get("deposit_spread") or {}
    pl = tile.get("payload") or {}
    new = pl.get("deposit_pct")
    if not isinstance(new, (int, float)):
        return []
    old = state.get("deposit_alerted")
    if not isinstance(old, (int, float)):
        state["deposit_alerted"] = new   # первый раз запоминаем молча
        return []
    if new < old:
        state["deposit_alerted"] = new
        return []
    if new - old < DEPOSIT_UPTICK_PP:
        return []
    state["deposit_alerted"] = new
    spread = pl.get("spread_pp")
    step = round(new - old, 2)
    meaning = ""
    if isinstance(spread, (int, float)):
        meaning = (f"Вклад теперь даёт на {wording.points(spread)}"
                   + (" больше" if spread > 0 else " меньше")
                   + " дивидендной доходности акций. Чем выгоднее вклад, тем дольше "
                     "деньги не пойдут с депозитов на биржу.")
    return [_ev(f"deposit_uptick:{pl.get('deposit_asof')}", "deposit_uptick",
                "Банки подняли ставки по вкладам", "info", now,
                before=_pct(old, 2), after=_pct(new, 2),
                detail=f"Прибавка {wording.points(step, 2)}.",
                meaning=meaning)]


def _sources(payload, prev, now):
    out = []
    old = prev.get("sources") or {}
    for name, meta in sorted((payload.get("sources") or {}).items()):
        if not isinstance(meta, dict):
            continue
        st = meta.get("status")
        if st not in ("stale", "error"):
            continue
        if old.get(name) in ("stale", "error"):
            continue  # уже сообщали, повторять каждый прогон нельзя
        age = meta.get("lag_min")
        age_txt = (f" Последний удачный опрос {wording.hours_minutes(age)} назад."
                   if isinstance(age, (int, float)) else "")
        word = "не отвечает" if st == "error" else "отдаёт устаревшие данные"
        out.append(_ev(f"source_stale:{name}:{payload.get('asof_trading_day')}",
                       "source_stale", f"источник {name} {word}", "warn", now,
                       fact=f"Данные в панели от "
                            f"{wording.ru_day(meta.get('asof') or payload.get('asof_trading_day'))}."
                            + age_txt,
                       meaning="Панель считает по последним удачным числам и выглядит "
                               "рабочей — тайлы этого источника помечены жёлтой точкой.",
                       where=f"Смотреть: journalctl -u moex-radar-daily -n 50 "
                             f"и раздел «Источники» на панели."))
    return out


def _health(payload, prev, now):
    health = ((payload.get("core") or {}).get("health") or {})
    st = health.get("status")
    if st != "dead" or prev.get("health") == "dead":
        return []
    return [_ev(f"health_dead:{payload.get('asof_trading_day')}", "health_dead",
                "модель перестала работать на свежей истории", "warn", now,
                fact=f"Связь оценки с последующим движением рынка за последние "
                     f"{health.get('n')} {wording.plural(health.get('n'), 'месяц', 'месяца', 'месяцев')}"
                     f" — {_num(health.get('ic_24m'), 2, True)} при норме выше нуля.",
                meaning="Пока так, знаку оценки на панели доверять нельзя: она "
                        "продолжает считаться и выглядит исправной.",
                where="Смотреть: карточку «Здоровье модели» на панели и "
                      "docs/ARCHITECTURE.md §7.")]


def _health_review(payload, prev, now):
    """Регламентный порог пересмотра состава: IC ниже нуля два квартала подряд.

    ПОЧЕМУ ОТДЕЛЬНО ОТ health_dead: тот срабатывает на ПЕРВЫЙ месяц ниже нуля и
    больше не возвращается, а регламент (docs/ARCHITECTURE.md §7) требует шести
    месяцев подряд. Между этими двумя моментами полгода, и без своего события
    порог наступал молча — ровно то, ради чего регламент и писали.

    Сообщение не говорит «меняй состав»: вторая половина условия (механизм у
    кандидата) человеческая, панель её не измеряет.
    """
    health = ((payload.get("core") or {}).get("health") or {})
    if not health.get("review_due") or prev.get("health_review_due"):
        return []
    months = health.get("below_zero_months")
    return [_ev(f"health_review_due:{health.get('below_since')}", "health_review_due",
                "достигнут порог пересмотра модели", "warn", now,
                fact=f"Связь оценки с рынком держится ниже нуля {months} "
                     f"{wording.plural(months, 'месяц', 'месяца', 'месяцев')} подряд "
                     f"(с {wording.ru_month(health.get('below_since'))}), сейчас "
                     f"{_num(health.get('ic_24m'), 2, True)}.",
                meaning="Регламент требует пересмотра состава модели — но это работа "
                        "человека: второе условие, наличие механизма у кандидата, "
                        "панель проверить не может.",
                where="Смотреть: отчёт реколибровки в state/recalibration/ "
                      "и docs/ARCHITECTURE.md §7.")]


def _core_missing(payload, prev, now):
    """Вчера был вердикт, сегодня «нет данных» — это авария, а не состояние рынка.

    Ловит частичную потерю стора (восстановление из неполной копии, оборванная
    запись, ручная чистка raw/): фетч при этом проходит зелёным, источники ok,
    health смотрит только на 'dead', — молчали ВСЕ правила, а панель писала
    «нет данных» поверх рабочего вердикта.
    """
    core = payload.get("core") or {}
    cell = (payload.get("verdict") or {}).get("cell_code")
    lost_core = core.get("value") is None and isinstance(prev.get("core_value"), (int, float))
    lost_cell = not cell and bool(prev.get("cell"))
    if not lost_core and not lost_cell:
        return []
    what = ("оценку и режим рынка" if (lost_core and lost_cell)
            else ("оценку рынка" if lost_core else "режим рынка"))
    was = (_num(prev.get("core_value"), 2, True) if lost_core
           else wording.cell_words(prev.get("cell")))
    return [_ev(f"core_missing:{payload.get('asof_trading_day')}", "core_missing",
                f"панель потеряла {what}", "warn", now,
                fact=f"Вчера было {was}, сегодня «нет данных».",
                meaning="Похоже на неполный набор рядов: публиковать такую панель "
                        "нельзя, читатель увидит прочерки вместо чисел.",
                where="Смотреть: journalctl -u moex-radar-daily -n 50 и полноту "
                      "стора в /var/lib/moex-radar/raw.")]


# Семейство «машина состояний»: разные стороны ОДНОГО поворота рынка. Ячейка — это
# и есть три её признака вместе, поэтому снятие облигационного флага НЕИЗБЕЖНО меняет
# ячейку, а окно входа — частный случай той же смены. Прогон 14.08.2026 показал, чем
# это оборачивалось: переход «ОФЗ вышли из стресса» рождал ТРИ сообщения подряд про
# одно движение.
#
# Порядок — ОТ КОНКРЕТНОГО К ОБЩЕМУ: заголовком становится самый предметный факт,
# остальное уходит подпунктами в «Что за этим стоит».
#
# Окно входа впереди всех: самый редкий режим за 22 года (8–10 месяцев) и
# единственный, который панель считает поводом добавлять риск.
# Дальше облигационный флаг, и лишь потом смена режима. Это не произвол: «Долговой
# рынок вошёл в стресс» — конкретное событие с понятным следствием, а «Режим рынка
# сменился» — обобщение над ним. Заголовок обязан нести предметное, обобщение
# читается подпунктом. Обратный порядок первой редакции давал заголовки, из которых
# нельзя было понять, ЧТО ИМЕННО произошло.
#
# core_flip СЮДА НЕ ВХОДИТ и не должен: наклон и ворота — разные слои модели
# («сначала ворота, потом наклон», docs/ARCHITECTURE.md). Они меняются независимо, и
# слияние спрятало бы одно за другим.
REGIME_FAMILY = ("buy_window_open", "bond_flag_on", "bond_flag_off", "state_cell_change")

SEVERITY_RANK = {"info": 0, "warn": 1}
CAUSES_MAX = 4          # пять строк подряд читаются как шум (тот же предел у 837)


def _cause_line(event, main):
    """Событие как подпункт — или None, если оно ничего не добавляет к заголовку.

    Свёрнутое событие описывает то же движение, что и главное, поэтому его
    «было → стало» СПЛОШЬ И РЯДОМ совпадает с уже напечатанным во второй строке.
    Первая редакция печатала его всё равно, и подпункт дословно повторял строку
    выше — блок «Что за этим стоит» не объяснял, а дублировал.
    """
    title = (event.get("title") or "").strip()
    before, after = event.get("before"), event.get("after")
    same = (before == main.get("before") and after == main.get("after"))
    if not (before or after) or same:
        # Движение то же — остаётся один заголовок, и то лишь если он говорит
        # что-то сверх главного.
        return None if same else (title or None)
    joiner = " — " if ":" in title else ": "
    return f"{title}{joiner}{before or '—'} → {after or '—'}"


def merge_regime(events):
    """Совпавшие переходы машины состояний -> одно событие с подпунктами."""
    family = [e for e in events if e.get("kind") in REGIME_FAMILY]
    if len(family) < 2:
        return events
    order = {kind: i for i, kind in enumerate(REGIME_FAMILY)}
    family.sort(key=lambda e: order[e["kind"]])
    main, folded = family[0], family[1:]

    causes = list(main.get("causes") or [])
    for event in folded:
        line = _cause_line(event, main)
        if line:
            causes.append(line)
        # Подробность свёрнутого события (например, расстояние RGBI до порога) —
        # тоже объяснение, и терять её вместе с сообщением незачем. Дубль с уже
        # напечатанной подробностью главного не берём.
        detail = str(event.get("detail") or "").strip().rstrip(".")
        if detail and detail != str(main.get("detail") or "").strip().rstrip("."):
            causes.append(detail)
        # СМЫСЛОВАЯ часть свёрнутого события — самое ценное, что в нём было
        # («покупка просадок снова в силе», историческая статистика режима).
        # Терять её вместе с отдельным сообщением нельзя: ради неё событие и есть.
        meaning = str(event.get("meaning") or "").strip()
        main_meaning = str(main.get("meaning") or "").strip()
        # Не только дословный дубль: две строки «Так было раньше: …» подряд с разными
        # числами читаются как противоречие, хотя описывают один и тот же режим.
        same_opening = bool(meaning and main_meaning
                            and meaning[:20] == main_meaning[:20])
        if meaning and meaning != main_meaning and not same_opening:
            causes.append(meaning)
    main["causes"] = causes[:CAUSES_MAX]
    main["severity"] = max([main.get("severity", "info")]
                           + [e.get("severity", "info") for e in folded],
                           key=lambda s: SEVERITY_RANK.get(s, 0))
    # Ключи свёрнутых событий переезжают в главное: дедуп телеграма и лента хаба
    # работают по ключу, и без этого повтор из очереди прислал бы их заново
    # по отдельности.
    main["merged"] = [e["key"] for e in folded]
    main["text"] = wording.plain_text(main)
    keep = {id(e) for e in folded}
    return [e for e in events if id(e) not in keep]


def detect(payload, state, now=None, merge=True):
    """Все переходы этого прогона. state мутируется (гистерезис знака ядра).

    `merge=False` отдаёт СЫРОЙ выход правил, без слияния семейства режима. Нужен
    проверкам, которые обходят все виды событий: после слияния часть видов в одном
    прогоне не появляется по построению, и «вид недостижим» стало бы неотличимо от
    «правило сломалось».
    """
    now = now or datetime.now(timezone.utc)
    prev = state.get("last") or {}
    events = []
    events += _core_flip(payload, prev, state, now)
    if not prev:
        # Первый прогон на чистой машине запоминает мир молча. Иначе установка
        # пайплайна начинается с пачки «событий» о том, что случилось до него:
        # проваленный на прошлой неделе аукцион и протухший с вечера источник.
        return events
    events += _cell_change(payload, prev, now)
    events += _bond_flag(payload, prev, now)
    events += _buy_window(payload, prev, now)
    events += _cb(payload, prev, now)
    events += _orfr(payload, prev, now)
    events += _auction(payload, prev, now)
    events += _deposit(payload, prev, state, now)
    events += _sources(payload, prev, now)
    events += _health(payload, prev, now)
    events += _health_review(payload, prev, now)
    events += _core_missing(payload, prev, now)
    return merge_regime(events) if merge else events


# ------------------------------------------------------------------ доставка

def render(event):
    """Текст для телеграма. Санитарные — в формате общего мостика панелей.

    Слово «Внимание.» перед тревогой убрано намеренно: его роль теперь несёт значок
    вида события в заголовке, а как ПЕРВОЕ слово сообщения оно съедало место, в
    котором читатель ищет суть. Ровно по той же причине его нет у 837/838.
    """
    if is_ops(event):
        return wording.render_ops(event)
    return wording.render_market(event)


def dispatch(events, dry_run=False, enabled=True):
    for ev in events:
        if not enabled:
            ev["delivered"], ev["skip"] = False, "алерты выключены"
        elif dry_run:
            ev["delivered"], ev["skip"] = False, "dry-run"
        elif is_ops(ev):
            # Санитарное: только в ops-канал. Ни зеркала в хаб, ни ленты — это
            # сообщение для того, кто чинит, а не для того, кто читает рынок.
            ev["channel"] = "ops"
            outcome = telegram.deliver(ev["key"], render(ev), channel="ops")
            ev["outcome"] = outcome
            # OFF («ERROR_* не заданы») здесь НЕ доставка. Санитарное событие больше
            # никуда не идёт — ни в ленту витрины, ни в хаб, — поэтому непрочитанным
            # оно исчезает совсем. Защёлка ниже (_snapshot_keeping_undelivered) держит
            # состояние ровно на этом флаге: пока не доставлено, правило родит событие
            # заново, и находка дождётся настроенного канала. Повторов при этом не
            # видно никому и в сеть не ходит ни один такт: config() отвечает сразу.
            ev["delivered"] = outcome in (telegram.SENT, telegram.DUP)
        else:
            outcome = telegram.deliver(ev["key"], render(ev))
            # DUP («такой ключ уже уходил») — это ДОСТАВЛЕНО. Считать его провалом
            # значит вечно держать событие в очереди повторов: cb_reminder рождается
            # на каждом из 56 интрадей-тактов и один вытеснял из очереди всё живое.
            ev["outcome"] = outcome
            # Копия события уходит в ленту хаба NEXUS. Доставленным событие
            # считается, только когда прошли ОБА канала: недоставленное ложится в
            # pending и повторяется, а телеграм на повторе отвечает DUP и второй
            # раз в канал не пишет. Отдельная очередь для зеркала не нужна.
            # OFF (канал не настроен) блокировать доставку не имеет права — иначе
            # прогон без NEXUS_* копил бы вечную очередь повторов.
            # А ЗДЕСЬ telegram.OFF — доставка, и по обратной причине. Рыночное событие
            # уже уехало в хаб строкой выше; считать его недоставленным значит класть
            # в очередь и повторять — и каждый повтор снова постит его в хаб (телеграм
            # на повторе молчит дедупом, у зеркала своего дедупа нет). Прогон без
            # TELEGRAM_* (README называет их необязательными) дублировал бы так каждое
            # событие раз в 5 минут сутки подряд.
            ev["nexus"] = nexus.deliver(ev)
            ev["delivered"] = (outcome in (telegram.SENT, telegram.DUP, telegram.OFF)
                               and ev["nexus"] in (nexus.SENT, nexus.OFF))
    return events


def _fresh(ev, now):
    try:
        ts = datetime.fromisoformat(str(ev.get("ts")).replace("Z", "+00:00"))
    except ValueError:
        return False
    return now - ts < timedelta(hours=PENDING_MAX_HOURS)


def payload_events(new_events=None, state=None):
    """Лента для data.json: сохранённый хвост + события этого прогона (CONTRACT §3).

    Санитарные сюда не попадают — ни новые, ни осевшие в state от прежних версий.
    """
    state = state if state is not None else load_state()
    feed = [e for e in (state.get("feed") or []) if not is_ops(e)]
    seen = {e.get("key") for e in feed}
    for ev in new_events or []:
        if is_ops(ev) or ev.get("key") in seen:
            continue
        feed.append({k: ev[k] for k in EVENT_FIELDS if k in ev})
        seen.add(ev.get("key"))
    feed = feed[-FEED_LIMIT:]
    return [{"ts": e.get("ts"), "kind": e.get("kind"), "severity": e.get("severity"),
             "text": e.get("text"), "comment": e.get("comment") or None} for e in feed]


def _dedup(events):
    """По одному событию на ключ; при повторе остаётся ПОСЛЕДНЯЯ (свежая) копия."""
    out = {}
    for ev in events:
        if isinstance(ev, dict) and ev.get("key"):
            out[ev["key"]] = ev
    return list(out.values())


def _requeue(batch):
    """Очередь повторов: только недоставленное, без дублей, режем СТАРОЕ.

    Срез с головы ([:40]) выбрасывал самое свежее событие — а именно свежее и есть
    новость. Плюс дедуп: без него одна и та же напоминалка занимала всю очередь.
    """
    return [{k: e[k] for k in EVENT_FIELDS if k in e}
            for e in _dedup(batch) if not e.get("delivered")][-PENDING_MAX:]


def _feed_add(state, events):
    """Лента копится только из рыночных событий: санитарные живут в ops-канале."""
    feed = [e for e in (state.get("feed") or []) if not is_ops(e)]
    seen = {e.get("key") for e in feed}
    for ev in events:
        if is_ops(ev) or ev.get("key") in seen:
            continue
        feed.append({k: ev[k] for k in EVENT_FIELDS if k in ev})
        seen.add(ev.get("key"))
    state["feed"] = feed[-FEED_LIMIT:]
    return state


def seed_from_payload(state, payload, now=None):
    """Восстановить снимок «прошлого прогона» из ОПУБЛИКОВАННОГО data.json.

    ПОЧЕМУ нужно: на чистой машине state пуст, а detect на пустом prev молчит по
    определению (иначе установка началась бы с пачки «событий» о прошлой неделе).
    Фолбэк-раннер GHA поднимается с пустым STATE_DIR КАЖДЫЙ раз — то есть ровно в
    аварии VPS, когда владелец ждёт уведомлений, канал был гарантированно глухим.
    Витрины хватает на все правила, кроме гистерезиса знака ядра.
    """
    if state.get("last") or not isinstance(payload, dict):
        return False
    snap = snapshot(payload, now)
    if snap.get("core_value") is None and not snap.get("cell"):
        return False  # витрина сама пустая — восстанавливать нечего
    state["last"] = snap
    # Журнал витрины тоже восстанавливается: без этого фолбэк публиковал data.json
    # с лентой из одних событий своего прогона (обычно пустой) поверх журнала,
    # который читатели видели минуту назад, — авария VPS выглядела бы как «панель
    # забыла всё, что рассказывала». Санитарного в опубликованной ленте не бывает
    # по построению (payload_events фильтрует), но фильтр повторяем: витрина —
    # внешний вход, а не доверенный.
    if not state.get("feed"):
        state["feed"] = [{k: e[k] for k in EVENT_FIELDS if k in e}
                         for e in (payload.get("events") or [])
                         if isinstance(e, dict) and not is_ops(e)][-FEED_LIMIT:]
    val = snap.get("core_value")
    if isinstance(val, (int, float)) and abs(val) > constants.CORE_FLIP_HYSTERESIS:
        # Знак из опубликованного ядра, иначе первый прогон на фолбэке объявит
        # «разворот» просто потому, что раньше о знаке никто не сообщал.
        state["core_sign_alerted"] = 1 if val > 0 else -1
        state["core_value_alerted"] = val
    return True


# Санитарные события живут защёлкой в снимке: правило смотрит на прошлое состояние и
# молчит, пока оно не изменилось. Снимок при этом писался БЕЗУСЛОВНО — то есть
# недоставленное событие исчезало навсегда: в ленту витрины и в хаб санитарные не идут
# (OPS_KINDS), очередь повторов живёт сутки, а заново правило его не породит, пока
# держится та же серия. Ops-канал по умолчанию не настроен (ops/env.example оставляет
# ERROR_* пустыми), так что путь этот не гипотетический.
#
# Поле-защёлка у каждого своё: у health_review_due — одноимённый флаг, у health_dead —
# строка статуса, у core_missing — пара «оценка + режим» (правило сравнивает обе).
# До 18.08.2026 защёлка покрывала только два health-вида, и это было хуже, чем
# казалось: незащищённые source_stale и core_missing при недоставке умирали в
# pending по TTL 24 ч и больше не рождались НИКОГДА — снимок уже зафиксировал
# «error»/«None», и правило считало, что уже сообщало. Стор частично теряется —
# панель публикует «нет данных» поверх рабочего вердикта, watchdog видит свежий
# Last-Modified и молчит, владелец не узнаёт об аварии вовсе.
# lease_lost и payload_oversize защищены отдельно (after_publish, lease_ok).
LATCH_FIELDS = {
    "health_review_due": ("health_review_due",),
    "health_dead": ("health",),
    "core_missing": ("core_value", "cell"),
}


def _snapshot_keeping_undelivered(payload, prev_last, batch, now):
    """Снимок текущего состояния, но защёлки недоставленных событий откатываются назад."""
    snap = snapshot(payload, now)
    for event in batch:
        if event.get("delivered"):
            continue
        kind = event.get("kind")
        for field in LATCH_FIELDS.get(kind, ()):
            if field in prev_last:
                snap[field] = prev_last[field]
        if kind == "source_stale":
            # Состояние этого правила — статус КОНКРЕТНОГО источника внутри словаря
            # sources; имя достаётся из ключа события (source_stale:<имя>:<день>).
            parts = str(event.get("key") or "").split(":")
            name = parts[1] if len(parts) > 2 else None
            prev_sources = prev_last.get("sources") or {}
            if name and name in prev_sources:
                snap.setdefault("sources", {})[name] = prev_sources[name]
            elif name:
                # Источник впервые появился уже сломанным: раньше его в снимке не
                # было — убираем и сейчас, чтобы переход случился заново.
                (snap.get("sources") or {}).pop(name, None)
    return snap


def run(payload, dry_run=False, enabled=True, now=None, seed_payload=None, log=None):
    """Полный цикл: определить переходы, повторить недоставленное, отправить, сохранить."""
    now = now or datetime.now(timezone.utc)
    state = load_state()
    if seed_payload is not None:
        seed_from_payload(state, seed_payload, now)
    prev_last = dict(state.get("last") or {})
    events = detect(payload, state, now)
    pending = [e for e in (state.get("pending") or []) if _fresh(e, now)]
    batch = _dedup(pending + events)
    # Комментарий проставляется ДО отправки и ДО записи в ленту: он должен уехать
    # одинаковым во все три места — телеграм, хаб и журнал витрины. В dry-run в сеть
    # не ходим: прогон «на посмотреть» не должен зависеть от чужого провайдера.
    #
    # СПРАШИВАЕМ ТОЛЬКО ПРО НОВОЕ. Часть правил рождает событие заново на КАЖДОМ такте
    # (cb_reminder — все 169 тактов в канун заседания), и телеграм гасит их дедупом уже
    # после того, как комментатор сходил к модели. Модель отвечает 150–200 с, а alerts.run
    # стоит ДО публикации при дедлайне такта 300 с — то есть лишний запрос платит не
    # деньгами, а риском не обновить витрину. Событие, чей ключ уже лежит в ленте, своё
    # уже получило: второй разбор ему не нужен и второй раз он никуда не уедет.
    if enabled and not dry_run:
        known = {e.get("key") for e in (state.get("feed") or [])}
        fresh = [e for e in batch if e.get("key") not in known]
        try:
            commentary.annotate(fresh, payload, log=log)
        except Exception as exc:  # noqa: BLE001 — украшение не имеет права сорвать доставку
            if log:
                log(f"комментатор упал: {type(exc).__name__}: {exc}")
    dispatch(batch, dry_run=dry_run, enabled=enabled)

    _feed_add(state, events)
    state["last"] = _snapshot_keeping_undelivered(payload, prev_last, batch, now)
    state["pending"] = _requeue(batch)
    if not dry_run:
        # В dry-run состояние НЕ трогаем: иначе прогон «на посмотреть» съедает
        # переход, и настоящий прогон о нём уже не сообщит.
        save_state(state)
    return batch


def needs_seed():
    """Нужен ли внешний снимок: состояния прошлого прогона на машине нет."""
    return not load_state().get("last")


def after_publish(publish_result, dry_run=False, enabled=True, now=None):
    """Санитарные события, которые видны только после попытки записи."""
    now = now or datetime.now(timezone.utc)
    res = publish_result or {}
    state = load_state()
    had = bool(state.get("lease_ok", True))
    have = bool(res.get("lease_ok", True))
    events, lease_events = [], []
    if had and not have:
        lease_events.append(_ev(f"lease_lost:{now.strftime('%Y-%m-%d')}", "lease_lost",
                                "публикация пропущена: писала другая машина", "warn", now,
                                fact=f"Право на запись занято: {res.get('lease_reason')}.",
                                meaning="Витрину обновил кто-то другой, этот прогон "
                                        "свои числа не опубликовал. Один раз — норма "
                                        "при пересменке, подряд — нет.",
                                where="Смотреть: journalctl -u moex-radar-daily -n 50 "
                                      "и объект lease в бакете."))
    events += lease_events
    if res.get("oversize"):
        # Лестница обрезки прошла целиком, а payload всё равно за лимитом: это
        # дефект лестницы (раздулся блок, которого в ней нет), и молчать о нём
        # нельзя — иначе о нарушении контракта §3 узнаёт только тот, кто читает
        # journald руками.
        events.append(_ev(f"payload_oversize:{now.strftime('%Y-%m-%d')}",
                          "payload_oversize", "витрина не помещается в свой лимит",
                          "warn", now,
                          fact=f"Файл витрины {_num(res.get('bytes'), 0)} байт при "
                               f"пределе {_num(res.get('limit'), 0)}; обрезка уже "
                               f"прошла целиком (вырезано: "
                               f"{', '.join(res.get('trimmed') or []) or 'нечего'}).",
                          meaning="Раздулся блок, которого в лестнице обрезки нет. "
                                  "Панель откроется, но на мобильной сети заметно "
                                  "медленнее.",
                          where="Смотреть: pipeline/publish.py, лестница fit_size."))
    dispatch(events, dry_run=dry_run, enabled=enabled)
    if not dry_run:
        _feed_add(state, [e for e in events if e.get("delivered")])
        # Недоставленное уезжает в ту же очередь повторов, что и обычные события:
        # раньше lease_lost умирал молча при первом же сетевом чихе, а теряется
        # лиз как раз в инфраструктурной аварии, когда телеграм тоже нестабилен.
        state["pending"] = _requeue(list(state.get("pending") or []) + events)
        # Флаг двигаем только после доставки: иначе следующий прогон уже не увидит
        # перехода had→have и повторять будет нечего.
        if all(e.get("delivered") for e in lease_events):
            state["lease_ok"] = have
        save_state(state)
    return events
