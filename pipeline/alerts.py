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

ТРИ СУДЬБЫ СОБЫТИЯ (аудит 20.11.2026):
* обычное рыночное — телеграм и хаб, в очереди повторов сутки;
* главное (MUST_DELIVER_KINDS — смена позиции) — живёт в очереди до доставки, но не
  дольше MUST_DELIVER_MAX_DAYS и не дольше следующей смены; о недоставке дольше часа
  уходит тревога в канал отказов обвязки;
* журнальное (JOURNAL_KINDS — флаги ворот и дневной разворот оценки) — только
  журнал страницы: ни телеграма, ни хаба, ни разбора моделью.
"""

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pipeline.lib import commentary, constants, nexus, registry, telegram, wording
from pipeline.lib import dates as datelib

STATE_NAME = "alerts_state.json"
FEED_LIMIT = 20          # столько последних событий уезжает в data.json
PENDING_MAX_HOURS = 24   # старше — не повторяем: новость протухла
PENDING_MAX = 40         # длина очереди повторов; режем СТАРОЕ, а не свежее
# ГЛАВНОЕ СОБЫТИЕ — смена позиции: по нему владелец действует на живые деньги. До
# 20.11.2026 оно жило в очереди те же сутки, что и остальные, и защёлки «держать до
# доставки» у него не было: Телеграм лежит дольше суток — сообщение стёрто, а заново
# правило его не родит (снимок позиции уже обновлён). Прогон аудита на копии это
# подтвердил (scripts/agentN/n7_sim_delivery.py). Теперь такое событие ждёт доставки
# до следующей смены позиции (она его вытесняет) или MUST_DELIVER_MAX_DAYS суток.
MUST_DELIVER_KINDS = frozenset({"position_change"})
MUST_DELIVER_MAX_DAYS = 14
# Главное событие не дошло до канала рынка за это время — тревога в канал отказов
# обвязки: он на другом боте и может быть жив, когда рыночный лежит. Если лежат оба,
# остаётся строка в журнале прогона — это не лечится.
STUCK_AFTER = timedelta(hours=1)
# Отбой предупреждения уходит без звука: он отменяет ожидание, а не зовёт к действию.
SILENT_KINDS = frozenset({"position_watch_off"})
DEPOSIT_UPTICK_PP = 0.05  # декадная ставка шумит в сотых, порог отсекает дрожь
# ВСЕ поля события, которые обязаны пережить очередь повторов и ленту. Плоский
# text — свёртка для журнала витрины (контракт §3), но телеграм рендерится из
# СТРУКТУРЫ: render_ops вообще не читает text, а render_market без title заворачивает
# весь текст в одну жирную строку. Когда 14.08 события стали структурами, этот
# кортеж не расширили — и повтор из pending уходил владельцу пустым слэгом
# «842 · source_stale» без факта и «куда смотреть», ровно в аварии, ради которой
# очередь существует (аудит 18.08.2026). merged — ключи свёрнутых переходов: без
# него повтор слал бы слитую тройку заново по отдельности. causes — блок «Что за
# этим стоит» слитого поворота режима: без него повтор приходил без объяснения, тот
# же класс дефекта, что 18.08 (аудит 28.10.2026).
# exec_day / exec_missed — день исполнения решения о позиции и «его закрытие прошло
# ещё до рождения события»: по ним досылка из очереди говорит, насколько опоздала и
# успевает ли читатель к закрытию исполнения. Без них в EVENT_FIELDS повтор терял бы
# эту строку — тот же класс дефекта.
EVENT_FIELDS = ("key", "ts", "kind", "severity", "text", "comment",
                "title", "before", "after", "moves", "detail", "meaning",
                "fact", "where", "merged", "causes", "direction",
                "exec_day", "exec_missed")

# САНИТАРНЫЕ события — отказы обвязки, а не движение рынка. Они уходят в общий
# ops-канал панелей и НЕ попадают ни в журнал витрины, ни в ленту хаба.
#
# ПОЧЕМУ разделили: журнал читают как ленту рынка, а «источник moex_press: error»
# рынку ничего не сообщает. Вперемешку они гасят друг друга — за неделю отказов
# владелец перестаёт открывать журнал, и вместе с ними мимо проходит смена ячейки.
# Ровно это и случилось: в журнале из четырёх записей три были про источники.
OPS_KINDS = frozenset({
    "source_stale", "source_ok", "health_review", "lease_lost", "payload_oversize",
    "core_missing",
    # Аудит 20.11.2026: ряд решения отслеживается сам по себе, а не через семью;
    # падение расчёта; главное событие не доходит до канала рынка.
    "core_series_stale", "core_series_ok", "compute_failed", "compute_ok",
    "delivery_stuck", "delivery_ok",
    # Прежние имена (до 02.09.2026): новых событий с ними не бывает, но в очереди
    # повторов старого alerts_state.json они ещё могут лежать — и обязаны уйти в
    # ops-канал, а не в ленту рынка.
    "health_dead", "health_review_due",
})


def is_ops(event):
    return (event or {}).get("kind") in OPS_KINDS


# ЖУРНАЛЬНЫЕ события — флаги ворот. Они идут ТОЛЬКО в журнал страницы.
#
# ПОЧЕМУ (аудит 20.11.2026, §9.5). За 30 дней в канал рынка ушло 14 сообщений,
# действия требовало одно. Восемь из них — «Признаки рынка сменились»: индекс восемь
# раз пересёк 200-дневную среднюю в пределах ±1 % при неизменных воротах и позиции, и
# каждое пришло со звуком, разбором модели и тем же значком 🔔, что смена позиции.
# Читатель, которого восемь раз позвали зря, девятый раз не откроет — а девятым и
# будет смена позиции. Всё, что из состояния рынка требует действия, приходит
# событием position_change (ворота закрылись при позиции «акции» — это оно и есть);
# остальное — справка, и её место в журнале страницы.
#
# ДНЕВНОЙ РАЗВОРОТ ОЦЕНКИ (core_flip) — туда же (проверка уведомлений 20.11.2026, хвост
# аудита). Он стоит на дневной оценке с порогом ±0,1, а решение — на знаке в день
# решения с порогом ±0,2, и сам о себе пишет «позицию это само не меняет». Три довода:
# 1) с 20.11 то, что в нём было полезного — «оценка ушла за порог против позиции до дня
#    решения», — точнее говорит position_watch, с порогом и днём решения;
# 2) в день решения он противоречил соседнему сообщению: «развернулась вниз… позицию это
#    само не меняет» и тем же прогоном «Позиция: переход в деньги — оценка ушла ниже
#    −0,2»;
# 3) разворот 19.10.2026 «+0,10 (нейтрально) → −0,10 (нейтрально)» ушёл со звуком и 📉,
#    хотя страница оба числа называет нейтральным сигналом (сила сигнала, ±0,2).
# На истории он срабатывал около трёх раз в год — не шум по счёту, но ни одного раза не
# требовал действия. Запись в журнале страницы остаётся.
JOURNAL_KINDS = frozenset({"buy_window_open", "bond_flag_on", "bond_flag_off",
                           "state_cell_change", "core_flip"})
JOURNAL = "journal"      # исход доставки журнального события: в сеть не ходили


def is_journal(event):
    return (event or {}).get("kind") in JOURNAL_KINDS


def must_deliver(event):
    return (event or {}).get("kind") in MUST_DELIVER_KINDS


def state_dir():
    env = (os.environ.get("STATE_DIR") or "").strip()
    return Path(env) if env else Path(__file__).resolve().parents[1] / ".state"


def _state_path():
    return state_dir() / STATE_NAME


# 14.09 и 22.09.2026 событие о вкладах ушло со знаком наоборот: «вклад даёт на 4,5 п.п.
# МЕНЬШЕ дивидендной доходности», когда вклад давал на 4,5 п.п. больше (spread =
# дивдоходность − вклад, слово выбиралось по его знаку наоборот; аудит 03.10.2026,
# §2.3). Код исправлен 03.10.2026, но записи остались в ленте витрины — с
# комментарием модели, построенным на неверном факте. Чиним их один раз при чтении
# состояния: слово сравнения меняется на противоположное, к тексту — пометка об
# исправлении (по ней правка не повторяется), комментарий снимается. Записи после
# даты исправления написаны новым кодом и не трогаются. Уже доставленные в телеграм
# сообщения так не исправить — это только лента витрины.
DEPOSIT_SIGN_FIXED_ON = "2026-10-03"
DEPOSIT_SIGN_NOTE = "(Исправлено 03.10.2026: в исходном сообщении сравнение было перевёрнуто.)"
_DEPOSIT_SIGN_RE = re.compile(r"(Вклад теперь даёт на [^.]*?) (больше|меньше)( дивидендной доходности)")


def _fix_deposit_sign(events):
    swap = {"больше": "меньше", "меньше": "больше"}
    for ev in events:
        if ev.get("kind") != "deposit_uptick" or str(ev.get("ts") or "") >= DEPOSIT_SIGN_FIXED_ON:
            continue
        text = ev.get("text") or ""
        if DEPOSIT_SIGN_NOTE in text or not _DEPOSIT_SIGN_RE.search(text):
            continue
        for field in ("text", "meaning"):
            if isinstance(ev.get(field), str):
                ev[field] = _DEPOSIT_SIGN_RE.sub(
                    lambda m: f"{m.group(1)} {swap[m.group(2)]}{m.group(3)}", ev[field], count=1)
        ev["text"] = ev["text"].rstrip() + " " + DEPOSIT_SIGN_NOTE
        ev.pop("comment", None)
    return events


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
        _fix_deposit_sign(data[key])
    if not isinstance(data.get("last"), dict):
        data.pop("last", None)
    if not isinstance(data.get("source_down"), dict):
        data.pop("source_down", None)
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
    # direction — "up"/"down" для значка 📈/📉 (стандарт сообщений §4): у разворота
    # оценки, решения ЦБ и вкладов направление своё в каждом событии.
    for name in ("before", "after", "moves", "detail", "meaning",
                 "fact", "where", "direction", "exec_day", "exec_missed"):
        if parts.get(name):
            event[name] = parts[name]
    event["text"] = wording.plain_text(event)
    return event


def _day(value, now):
    """Дата для читателя: без года, если он тот же, что у события (стандарт §3)."""
    return wording.ru_date(value, now)


def _leg(position, leg_id):
    """Строка слагаемого оценки из verdict.position.held_by.legs — или None."""
    for row in ((position or {}).get("held_by") or {}).get("legs") or []:
        if isinstance(row, dict) and row.get("id") == leg_id:
            return row
    return None


def _oil_contrib(position):
    """(вклад нефти в рублях, число слагаемых) — для сообщения о новом месяце нефти."""
    legs = [r for r in ((position or {}).get("held_by") or {}).get("legs") or []
            if isinstance(r, dict) and isinstance(r.get("contrib"), (int, float))]
    row = _leg(position, "urals_rub_gap") or {}
    value = row.get("contrib")
    return (value if isinstance(value, (int, float)) else None), len(legs)


def snapshot(payload, now=None):
    """Что запоминаем до следующего прогона. Только то, по чему ищем переходы."""
    core = payload.get("core") or {}
    cur = (payload.get("states") or {}).get("current") or {}
    gate = (payload.get("states") or {}).get("gate")
    mons = _mons(payload)
    position = (payload.get("verdict") or {}).get("position") or {}
    oil, legs_n = _oil_contrib(position)
    return {
        "asof": payload.get("asof_trading_day"),
        "core_value": core.get("value"),
        "core_sign": core.get("sign"),
        "cell": (payload.get("verdict") or {}).get("cell_code"),
        # Позиция «акции/деньги» — слой поверх ядра и ячейки, у него своё событие.
        "position": position.get("state"),
        "position_since": position.get("since"),
        "trend": cur.get("trend"), "vol": cur.get("vol"), "bond": cur.get("bond"),
        # Флаги ВОРОТ (с гистерезисом): по ним, а не по сырым признакам, стоит
        # событие о смене состояния рынка (аудит 20.11.2026). None — в витрине блока
        # ворот нет (старый payload): переходом это не считается.
        "gate": ({k: gate.get(k) for k in ("trend", "vol", "bond", "open")}
                 if isinstance(gate, dict) else None),
        # Оценка рынка и вклад нефти прошлого расчёта — «было» в сообщении о новом
        # месяце налоговой цены нефти.
        "comp_daily": position.get("comp_daily"),
        "oil_contrib": oil, "legs_n": legs_n,
        # Признак «расчёт упал, на странице прежний вердикт» (run.py).
        "compute_error": payload.get("compute_error") or None,
        "health": (core.get("health") or {}).get("status"),
        "health_review_due": bool((core.get("health") or {}).get("review_due")),
        "key_rate": _mp(mons, "cb_meeting", "key_rate"),
        "deposit": _mp(mons, "deposit_spread", "deposit_pct"),
        "orfr_asof": (mons.get("orfr") or {}).get("asof"),
        "auction_date": _mp(mons, "ofz_auctions", "date"),
        # Статус семьи — БЕЗ рядов решения: у них свой снимок и своя тревога, иначе
        # монитор, пометивший семью устаревшей, заслонял отказ ряда решения в ней.
        "sources": {k: v.get("status") for k, v in _family_units(payload).items()},
        "core_series": {k: v.get("status") for k, v in _core_units(payload).items()},
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

    С 20.11.2026 — только запись журнала страницы (JOURNAL_KINDS, там же доводы).
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
    # Разворот здесь — по дневному значению с порогом ±0,1, а знак решения о позиции
    # меняется только в последний торговый день недели и за ±0,2 (constants.DECISION).
    # Без этой фразы читатель получал «разворот вниз», а позиция оставалась прежней, —
    # два разных знака под одним словом (аудит 03.10.2026, §3.6).
    thr = constants.DECISION["comp_threshold"]
    rule = (f"Позицию это само не меняет: знак решения пересчитывается в последний "
            f"торговый день недели и только за пределами ±{_num(thr, 1, False)}.")
    detail = (f"Знак держится с {_day(since, now)}. " if since else "") + rule
    return [_ev(f"core_flip:{sign}:{payload.get('asof_trading_day')}", "core_flip",
                # Без «на месяц вперёд»: то же число в сообщении о позиции звалось
                # «оценкой на неделю вперёд» — одно число под двумя горизонтами
                # (аудит 20.11.2026, §9.8). Имя одно: «оценка рынка».
                "Оценка рынка развернулась "
                + ("вверх" if sign > 0 else "вниз"),
                now=now, direction="up" if sign > 0 else "down",
                before=f"{_num(prev_value, 2, True)}{f' ({was_label})' if was_label else ''}",
                after=f"{_num(val, 2, True)}{f' ({_core_label(val)})' if _core_label(val) else ''}",
                detail=detail,
                meaning="Шкала — от −3 до +3: чем дальше от нуля, тем увереннее "
                        "перевес в эту сторону. Это оценка направления, "
                        "а не обещание доходности.")]


POSITION_WORD = {"long": "акции", "flat": "деньги"}


def _thr(pos):
    value = (pos or {}).get("comp_threshold")
    return value if isinstance(value, (int, float)) else constants.DECISION["comp_threshold"]


def _wd(day, now):
    """«пт 06.11» — день недели и дата (wording.ru_weekday_date)."""
    return wording.ru_weekday_date(day, now)


def exec_day_of(decision_day):
    """День исполнения решения: следующий торговый день после дня решения.

    «Исполнение — на следующем закрытии»: решение пятницы исполняется на закрытии
    понедельника, решение среды (закрылись ворота) — четверга. Календарь —
    pipeline/lib/dates.py, тот же, по которому слой решения ищет день решения.
    None — дата не разобралась: тогда сообщение дня исполнения не называет.
    """
    try:
        return datelib.next_trading_day(decision_day).isoformat()
    except (TypeError, ValueError):
        return None


def _money_word(pos):
    rate = (pos or {}).get("cash_rate")
    return "деньги" + (f" (ставка {_pct(rate, 1)})" if isinstance(rate, (int, float)) else "")


def _sign_holder(pos):
    """Слагаемое, которое держит знак оценки: без него она была бы по другую сторону нуля.

    Считается от знака САМОЙ оценки (comp_daily), а не от знака решения: в
    предупреждении перед днём решения знак решения ещё прежний, а читателю важно,
    что утянуло оценку за порог. Если таких слагаемых несколько — то, чей вклад
    больше по модулю. -> (внешнее имя, оценка без него) | None.
    """
    comp = (pos or {}).get("comp_daily")
    if not isinstance(comp, (int, float)) or comp == 0:
        return None
    best = None
    for row in ((pos or {}).get("held_by") or {}).get("legs") or []:
        if not isinstance(row, dict):
            continue
        name = wording.leg_name(row.get("id"))
        own, without = row.get("contrib"), row.get("without")
        if not name or not isinstance(own, (int, float)) or not isinstance(without, (int, float)):
            continue
        if without * comp < 0 and (best is None or abs(own) > best[0]):
            best = (abs(own), name, without)
    return (best[1], best[2]) if best else None


def _legs_line(pos):
    """«Слагаемые оценки (она — их среднее): курс доллара −0,15; …» — или пусто."""
    bits = []
    for row in ((pos or {}).get("held_by") or {}).get("legs") or []:
        name = wording.leg_name((row or {}).get("id")) if isinstance(row, dict) else ""
        if name and isinstance(row.get("contrib"), (int, float)):
            bits.append(f"{name} {_num(row['contrib'], 2, True)}")
    return ("Слагаемые оценки (она — их среднее): " + "; ".join(bits) + ".") if bits else ""


def _holder_line(pos):
    holder = _sign_holder(pos)
    if not holder:
        return ""
    return (f"Знак оценки держит {holder[0]}: без этого слагаемого она была бы "
            f"{_num(holder[1], 2, True)}.")


def _sign_agrees(pos):
    """Знак дневной оценки совпадает со знаком решения (или знак решения не известен).

    Знак решения (comp_state) ставится только в день решения и между порогами ±0,2
    держится прежним, а дневная оценка ходит каждый день. Вход в акции 15.10.2026
    случился по воротам при знаке решения «за акции» и дневной оценке −0,06: строка
    «знак оценки держит нефть, без неё было бы +0,30» объясняла бы МИНУС, тогда как
    позицию держит плюс знака решения, — два разных знака в одной фразе (проверка
    уведомлений 20.11.2026).
    """
    comp, sign = pos.get("comp_daily"), pos.get("comp_state")
    if sign not in (1, -1) or not isinstance(comp, (int, float)) or comp == 0:
        return True
    return (comp > 0) == (sign > 0)


# Ориентир «до входа/выхода по оценке рынка хватило бы одного из …» — три строки сдвигов;
# он есть на странице и во входе разбора, в факт сообщения не идёт.
_LEGS_HINT = "хватило бы одного из"


def held_lines(pos, now=None):
    """Что держит позицию и что её сменит — ФАКТ, из held_by и conditions (аудит 20.11.2026).

    Разбор модели к выходу 06.11 назвал причиной стресс облигаций и индекс ниже
    средней, хотя ворота были открыты, а вышла панель из-за оценки рынка, знак
    которой держала нефть. Правда о причине обязана стоять в фактической части
    сообщения — независимо от того, что напишет (и напишет ли) модель.
    """
    pos = pos or {}
    held = pos.get("held_by") or {}
    comp = pos.get("comp_daily")
    score = _num(comp, 2, True) if isinstance(comp, (int, float)) else ""
    thr = _num(_thr(pos), 1, False)
    agrees = _sign_agrees(pos)
    # Знак решения «за акции» при дневной оценке другого знака: называем день, с
    # которого он стоит, а число — словом «сейчас». Иначе «знак — за акции (оценка
    # −0,07)» читается как опечатка (_sign_agrees).
    since = pos.get("comp_state_since")
    sign_since = f" с {_wd(since, now)}" if since and not agrees else ""
    value = "сейчас оценка" if not agrees else "оценка"
    by = held.get("by")
    if by == "comp":
        head = ("оценка рынка" + (f" — {score} при пороге входа +{thr}" if score else "")
                + f"; {wording.GATE_WORD} открыты")
    elif by == "gate":
        head = (f"закрытые {wording.GATE_WORD}; знак оценки рынка — за акции{sign_since}"
                + (f" ({value} {score})" if score else ""))
    elif by == "both":
        head = (f"и закрытые {wording.GATE_WORD}, и оценка рынка"
                + (f" ({score} при пороге входа +{thr})" if score else ""))
    elif by == "open":
        head = (f"{wording.GATE_WORD} открыты, знак оценки рынка — за акции{sign_since}"
                + (f" ({value} {score}, порог выхода −{thr})" if score else ""))
    else:
        head = ""
    lines = []
    if head:
        holder = _holder_line(pos) if agrees else ""
        lines.append(" ".join(x for x in (f"Что держит позицию: {head}.", holder) if x))
    legs = _legs_line(pos)
    if legs:
        lines.append(legs)
    conditions = [c.strip() for c in pos.get("conditions") or []
                  if isinstance(c, str) and c.strip() and _LEGS_HINT not in c]
    if conditions:
        cond = wording.sentence(wording.ru_decimals(conditions[0]))
        line = "Что сменит позицию: " + cond[:1].lower() + cond[1:]
        # При закрытых воротах и знаке решения «за деньги» условий два, и нужны оба:
        # одно первое («ворота откроются…») обещало бы вход без оценки рынка.
        for extra in conditions[1:2]:
            second = wording.sentence(wording.ru_decimals(extra))
            line += " Нужно и второе: " + second[:1].lower() + second[1:]
        lines.append(line)
    return lines


# Причины, при которых позицию сменила ОЦЕНКА РЫНКА (а не ворота).
SCORE_REASONS = ("comp_neg", "comp_pos")

POSITION_MEANING = (
    "Позиция — итог двух условий: ворота (три признака состояния рынка, читаются "
    "ежедневно) и знак оценки рынка. Знак читается в день решения — последний "
    "торговый день недели — с порогом ±{thr}; между порогами держится прежний.")


def _position_change(payload, prev, now):
    """Смена позиции «акции ↔ деньги» между прогонами (слой решения, 02.09.2026).

    Первый прогон молчит, как core_flip: снимок без позиции (старая версия панели)
    переходом не считается. Слово «позиция» здесь — итог ворот и оценки, а не
    совет: панель говорит, где она стоит по своему правилу, и почему.

    С 20.11.2026 (аудит, §9): сообщение называет день недели и дату исполнения,
    говорит, если о решении узнали позже дня решения и прошло ли закрытие
    исполнения, и несёт в факте строки «что держит позицию» и «что её сменит».
    """
    pos = (payload.get("verdict") or {}).get("position") or {}
    new, old = pos.get("state"), prev.get("position")
    if new not in ("long", "flat") or old not in ("long", "flat") or new == old:
        return []
    word = {"long": "акции", "flat": _money_word(pos)}
    reason = wording.sentence(wording.ru_decimals(pos.get("reason_text") or ""))
    comp = pos.get("comp_daily")
    if reason and pos.get("reason") in SCORE_REASONS and isinstance(comp, (int, float)):
        # Причина называет порог, факт — само число: «ушла ниже −0,2 … : −0,31».
        reason = reason.rstrip(".") + f": {_num(comp, 2, True)}."
    since, asof = pos.get("since"), payload.get("asof_trading_day")
    exec_day = exec_day_of(since) if since else None
    close = wording.exec_close(exec_day) if exec_day else None
    exec_missed = bool(close and now >= close)
    when = []
    if since:
        when.append(f"Решение от {_wd(since, now)}, исполнять на закрытии "
                    + (f"{_wd(exec_day, now)}." if exec_day else "следующего торгового дня."))
        if asof and str(since)[:10] < str(asof)[:10]:
            # Решение датировано раньше дня расчёта: панель узнала о нём задним числом
            # (правка данных, пропущенный прогон). Молчать об этом нельзя — читатель
            # иначе исполнит «на следующем закрытии» то, что надо было исполнить давно.
            when.append(f"Панель узнала о решении позже дня решения — по расчёту за "
                        f"{_day(asof, now)}.")
    else:
        when.append("Исполнять на следующем закрытии.")
    if exec_missed:
        when.append(f"Закрытие исполнения ({_wd(exec_day, now)}) уже прошло.")
    detail = "\n".join(x for x in ([reason] if reason else []) + [" ".join(when)]
                       + held_lines(pos, now) if x)
    # Заголовок называет ДЕЙСТВИЕ, а строка ниже — движение со ставкой. Прежний
    # заголовок «Позиция: деньги → акции» дословно повторял эту строку, и лента
    # читала «Позиция: деньги → акции деньги (ставка 13,1%) → акции» (аудит B5).
    #
    # КЛЮЧ несёт момент обнаружения. Прежний «position:<день>:<куда>» гасил третью
    # смену при развороте туда-обратно в пределах одной даты вердикта (правка
    # данных в выходные: деньги → акции → деньги → акции): третье сообщение совпадало
    # ключом с первым, телеграм отвечал «уже отправляли», и последним в канале
    # оставалось «переход в деньги» при позиции «акции» (scripts/agentN/n11_flipflop.py).
    # Одна и та же смена дважды не рождается — снимок позиции двигается каждым
    # прогоном, — поэтому момент обнаружения однозначно именует смену.
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M")
    return [_ev(f"position:{asof}:{new}:{stamp}", "position_change",
                f"Позиция: переход в {'акции' if new == 'long' else 'деньги'}",
                "warn" if new == "flat" else "info", now,
                before=word[old], after=word[new],
                detail=detail[0].upper() + detail[1:],
                meaning=POSITION_MEANING.format(thr=_num(_thr(pos), 1, False)),
                exec_day=exec_day, exec_missed=exec_missed)]


# -------------------------------------------- предупреждение перед днём решения
#
# ПОЧЕМУ (аудит 20.11.2026, §9.2). Перед выходом 06.11 оценка рынка всю неделю стояла
# ниже порога (02.11 −0,24; 03.11 −0,27; 05.11 −0,31 при пороге −0,2): пятничный выход
# был виден с понедельника, и об этом не ушло ничего. Правило читает то, что слой
# решения уже кладёт в verdict.position, и само ничего не решает.

def _watch_direction(pos):
    """Куда может смениться позиция в день решения: "flat" | "long" | None."""
    comp, thr = pos.get("comp_daily"), _thr(pos)
    if not isinstance(comp, (int, float)):
        return None
    state = pos.get("state")
    if state == "long" and comp < -thr:
        return "flat"
    if state == "flat" and pos.get("gate_open") and pos.get("comp_state") != 1 and comp > thr:
        return "long"
    return None


def _position_watch(payload, prev, state, now, changed):
    """«В день решения возможна смена позиции» и отбой, если смены не случилось.

    Предупреждение — одно на пару (день решения, направление), только суточным
    прогоном и только когда день расчёта сам не является днём решения. Отбой —
    когда предупреждение ДОШЛО, день решения наступил, а позиция осталась прежней.
    `changed` — в этом прогоне позиция сменилась: тогда ответом на предупреждение
    служит само сообщение о смене.
    """
    pos = (payload.get("verdict") or {}).get("position") or {}
    out = []
    memo = state.get("watch") if isinstance(state.get("watch"), dict) else None
    if not memo:
        state.pop("watch", None)
    asof = str(pos.get("decision_day") or payload.get("asof_trading_day") or "")[:10]
    comp, thr = pos.get("comp_daily"), _thr(pos)
    if memo:
        day = str(memo.get("decision") or "")[:10]
        if changed:
            state.pop("watch", None)
            memo = None
        elif day and asof and asof >= day and pos.get("state") in ("long", "flat"):
            state.pop("watch", None)
            if memo.get("delivered") and isinstance(comp, (int, float)):
                side = (f"пороге выхода −{_num(thr, 1, False)}" if memo.get("to") == "flat"
                        else f"пороге входа +{_num(thr, 1, False)}")
                when = (f"В день решения, {_wd(day, now)}," if asof == day
                        else f"День решения был {_wd(day, now)}; на {_wd(asof, now)}")
                # Оценка прошла порог входа, а позиция осталась «деньги»: за неделю
                # закрылись ворота. Без этой оговорки отбой читается как сбой правила.
                why = (f", её держат закрытые {wording.GATE_WORD}"
                       if memo.get("to") == "long" and pos.get("state") == "flat"
                       and pos.get("gate_open") is False else "")
                out.append(_ev(f"position_watch_off:{day}:{memo.get('to')}",
                               "position_watch_off",
                               "День решения прошёл без смены позиции", "info", now,
                               detail=f"{when} оценка рынка {_num(comp, 2, True)} при "
                                      f"{side}. Позиция осталась прежней — "
                                      f"{POSITION_WORD[pos['state']]}{why}."))
            memo = None
    if payload.get("run_mode") != "daily" or pos.get("is_decision_day") or changed:
        return out
    to = _watch_direction(pos)
    day = str(pos.get("next_decision") or "")[:10]
    if to is None or not day:
        return out
    key = f"position_watch:{day}:{to}"
    if memo and memo.get("key") == key:
        return out      # об этом дне решения и этом направлении уже предупреждали
    state["watch"] = {"key": key, "decision": day, "to": to, "delivered": False}
    exec_day = exec_day_of(day)
    old_word = "акции" if to == "flat" else _money_word(pos)
    new_word = _money_word(pos) if to == "flat" else "акции"
    side = (f"ниже порога −{_num(thr, 1, False)}" if to == "flat"
            else f"выше порога +{_num(thr, 1, False)}")
    lines = [f"Оценка рынка {_num(comp, 2, True)} — {side} (расчёт за {_wd(asof, now)}).",
             f"День решения — {_wd(day, now)}: если оценка останется за порогом, позиция "
             f"сменится: {old_word} → {new_word}."
             + (f" Исполнение — на закрытии {_wd(exec_day, now)}." if exec_day else "")]
    holder = _holder_line(pos)
    if holder:
        lines.append(holder)
    return out + [_ev(key, "position_watch",
                      f"В день решения возможен переход в {POSITION_WORD[to]}",
                      "info", now, detail="\n".join(lines),
                      meaning="Это предупреждение, а не решение: знак оценки читается "
                              "только в день решения, и до него оценка может вернуться "
                              "за порог.")]


# ------------------------------------------------ новый месяц налоговой цены нефти
#
# ПОЧЕМУ (аудит 20.11.2026, §9.3). Октябрьская налоговая цена Urals пришла 05.11 и за
# сутки до дня решения сдвинула оценку на 0,06: без неё 06.11 было бы −0,197, то есть
# выхода из акций не случилось бы. Событие, решившее позицию, сообщения не дало.
#
# Память правила — ПОСЛЕДНИЙ СООБЩЁННЫЙ МЕСЯЦ (state["urals_alerted"]), а не дата
# файла или снимок прошлого прогона: смена записи в истории не событие (урок
# auction_failed 12.08.2026). Месяц назад, тот же месяц с поправленной ценой или
# месяц глубокой давности (восстановление стора) — тишина.
URALS_FRESH_DAYS = 62    # месяц выходит в первых числах следующего; два месяца — уже архив


def _month_word(month, now):
    """«октябрь» в текущем году события, «декабрь 2025» — для прошлого (стандарт §3)."""
    name = wording.ru_month(month)
    year = str(now.astimezone(wording.MSK).year)
    return name[:-len(year)].strip() if name.endswith(" " + year) else name


def _urals_visible_from(month):
    """С какого дня расчёта панель видит месяц налоговой цены: конец месяца + лаг
    доступности ряда из реестра (pub_lag_days). None — посчитать не удалось."""
    try:
        lag = (registry.SERIES.get("urals_tax") or {}).get("pub_lag_days") or 0
        return datelib.apply_pub_lag(month, lag).isoformat()
    except (TypeError, ValueError):
        return None


def _urals_month(payload, prev, state, now):
    pl = (_mons(payload).get("rub_barrel") or {}).get("payload") or {}
    month, usd = pl.get("tax_month"), pl.get("urals_usd")
    if not month or not isinstance(usd, (int, float)):
        return []
    month = str(month)[:10]
    memo = state.get("urals_alerted") if isinstance(state.get("urals_alerted"), dict) else {}
    last = str(memo.get("month") or "")[:10]
    if not last:
        state["urals_alerted"] = {"month": month, "usd": usd}   # первый раз — молча
        return []
    if month <= last:
        return []
    # Оценка пересчитывается только полным прогоном: интрадей-такт несёт вердикт
    # прошлого расчёта, и «оценка после» была бы оценкой ДО. Ждём полного прогона —
    # память при этом не двигаем, событие родится в нём.
    if payload.get("run_mode") == "intraday" or payload.get("compute_error"):
        return []
    pos = (payload.get("verdict") or {}).get("position") or {}
    # И полный прогон видит новый месяц не сразу: панель берёт значение месяца с даты
    # ДОСТУПНОСТИ (конец месяца + лаг ряда), а день расчёта — последний день с
    # закрытием. 05.11.2026 цена легла в хранилище днём, месячный прогон в 18:07 МСК
    # считал по закрытию 03.11 и октябрьской цены в оценке ещё не имел; сдвиг на 0,06
    # показал только вечерний расчёт за 05.11. Событие ждёт именно его.
    visible = _urals_visible_from(month)
    asof = str(pos.get("decision_day") or payload.get("asof_trading_day") or "")[:10]
    if visible and asof and asof < visible:
        return []
    state["urals_alerted"] = {"month": month, "usd": usd}
    age = _age_days(month, now)
    if age is None or age > URALS_FRESH_DAYS:
        return []
    comp, was = pos.get("comp_daily"), prev.get("comp_daily")
    oil, legs_n = _oil_contrib(pos)
    oil_was = prev.get("oil_contrib")
    lines = []
    if isinstance(comp, (int, float)):
        move = (f"{_num(was, 2, True)} → {_num(comp, 2, True)}"
                if isinstance(was, (int, float)) else _num(comp, 2, True))
        line = f"Оценка рынка: {move}"
        if isinstance(oil, (int, float)) and isinstance(oil_was, (int, float)) and legs_n:
            line += (f"; вклад нефти в рублях {_num(oil_was, 2, True)} → {_num(oil, 2, True)}"
                     f" — сдвиг оценки на {_num((oil - oil_was) / legs_n, 2, True)}")
        lines.append(line + ".")
        thr = _thr(pos)
        state_now = pos.get("state")
        if state_now == "long":
            gap = comp + thr
            lines.append(f"Порог выхода из акций −{_num(thr, 1, False)}: "
                         + (f"оценка ниже его на {_num(-gap, 2, False)}." if gap < 0
                            else f"до него {_num(gap, 2, False)}."))
        elif state_now == "flat" and pos.get("comp_state") != 1:
            gap = thr - comp
            lines.append(f"Порог входа в акции +{_num(thr, 1, False)}: "
                         + (f"оценка выше его на {_num(-gap, 2, False)}" if gap < 0
                            else f"до него {_num(gap, 2, False)}")
                         # При закрытых воротах одной оценки для входа мало.
                         + (f"; нужны и открытые {wording.GATE_WORD}"
                            if pos.get("gate_open") is False else "") + ".")
        elif state_now == "flat":
            lines.append(f"Знак оценки — за акции; позицию держат закрытые {wording.GATE_WORD}.")
    decision_today = bool(pos.get("is_decision_day"))
    day = pos.get("decision_day") if decision_today else pos.get("next_decision")
    if day:
        lines.append(("День решения — сегодняшний расчёт, " if decision_today
                      else "День решения — ") + f"{_wd(day, now)}.")
    was_usd = memo.get("usd")
    # В день решения «позицию это само не меняет» было бы неправдой: знак читается
    # этим же расчётом, уже с новой ценой, и смена позиции может прийти тем же прогоном.
    tail = ("Сегодня день решения: знак оценки прочитан этим же расчётом — уже с новой "
            "ценой нефти." if decision_today else
            "Позицию это само не меняет: знак оценки читается в день решения.")
    return [_ev(f"urals_month:{month}", "urals_month",
                f"Вышла налоговая цена нефти за {_month_word(month, now)}", "info", now,
                before=(f"${_num(was_usd, 2)} ({_month_word(last, now)})"
                        if isinstance(was_usd, (int, float)) else None),
                after=f"${_num(usd, 2)} за баррель Urals ({_month_word(month, now)})",
                detail="\n".join(lines) or None,
                meaning="Цена нефти входит в оценку рынка раз в месяц, поэтому сдвиг "
                        "приходит разом. " + tail)]


# ------------------------------------------------------------ флаги ворот (журнал)
#
# Семейство стоит на флагах ВОРОТ (states.gate, с гистерезисом), а не на сырых
# признаках (states.current), и идёт только в журнал страницы — JOURNAL_KINDS выше.
# До 20.11.2026 было наоборот: событие рождалось на сырой ячейке, и индекс у
# 200-дневной средней в пределах ±1 % давал «Признаки рынка сменились» восемь раз за
# месяц при неизменных воротах и позиции. Прежние оговорки «ворота этого ещё не
# читают» ушли вместе с причиной: событие теперь и есть то, что читают ворота.

GATE_FLAGS = ("trend", "vol", "bond")


def _gate_flags(block):
    """(тренд, вола, облигации) из блока ворот — или None, если чего-то нет."""
    if not isinstance(block, dict):
        return None
    flags = tuple(block.get(k) for k in GATE_FLAGS)
    return None if any(f not in (0, 1) for f in flags) else flags


def _gate_code(flags):
    return "%s|%s|%s" % ("bull" if flags[0] == 1 else "bear",
                         "stress" if flags[1] == 1 else "calm",
                         "stress" if flags[2] == 1 else "ok")


def _dist_text(payload, flag):
    """Где признак сейчас и где его снимают ВОРОТА — подробность записи журнала.

    Строка distances[].text — о сыром признаке: у облигаций «флаг снимется при −3,9%»
    — это порог включения, а ворота снимают флаг выше −3,0% (text_off). Семейство
    стоит на флагах ворот (с 20.11.2026), поэтому там, где пороги разные, к значению
    приклеивается порог ворот, а хвост о сыром пороге отрезается.
    """
    for row in (payload.get("states") or {}).get("distances") or []:
        if isinstance(row, dict) and row.get("id") == flag and row.get("text"):
            text = str(row["text"])
            off = row.get("text_off")
            if off and row.get("off_threshold") != row.get("threshold"):
                text = text.split(";")[0].strip() + "; " + str(off)
            return wording.sentence(wording.ru_decimals(text))
    return ""


def _gate_effect(payload, prev, gate):
    """Что смена флагов значит для ворот и повлияла ли она на позицию — одной фразой."""
    was_open = (prev.get("gate") or {}).get("open")
    is_open = gate.get("open")
    word = wording.GATE_WORD.capitalize()
    if is_open is None:
        head = ""
    elif was_open is None or bool(was_open) == bool(is_open):
        head = f"{word} остались {'открытыми' if is_open else 'закрытыми'}"
    else:
        head = f"{word} {'открылись' if is_open else 'закрылись'}"
    pos = (payload.get("verdict") or {}).get("position") or {}
    new, old = pos.get("state"), prev.get("position")
    tail = ""
    if new in POSITION_WORD:
        if old in POSITION_WORD and old != new:
            tail = f"позиция сменилась: {POSITION_WORD[old]} → {POSITION_WORD[new]}"
        else:
            tail = f"позиция не изменилась — {POSITION_WORD[new]}"
            comp = pos.get("comp_daily")
            by = (pos.get("held_by") or {}).get("by")
            if new == "flat" and by in ("comp", "both") and isinstance(comp, (int, float)):
                tail += (", её держит оценка рынка" if by == "comp"
                         else f", её держат и {wording.GATE_WORD}, и оценка рынка")
                tail += f" ({_num(comp, 2, True)})"
            elif new == "flat" and by == "gate":
                tail += f", её держат {wording.GATE_WORD}"
    return "; ".join(x for x in (head, tail) if x) + ("." if head or tail else "")


def _gate_events(payload, prev, now):
    """Смена флагов ворот -> события семейства (сливаются в одну запись журнала)."""
    gate = (payload.get("states") or {}).get("gate")
    new, old = _gate_flags(gate), _gate_flags(prev.get("gate"))
    if new is None or old is None or new == old:
        return []
    asof = payload.get("asof_trading_day")
    effect = _gate_effect(payload, prev, gate) or None
    moved = [f for i, f in enumerate(GATE_FLAGS) if new[i] != old[i]]
    before, after = wording.cell_words(_gate_code(old)), wording.cell_words(_gate_code(new))
    out = []
    if new[1] == 1 and new[2] == 0 and not (old[1] == 1 and old[2] == 0):
        out.append(_ev(f"buy_window:{asof}", "buy_window_open",
                       "Окно входа: паника в акциях при спокойном долге", "info", now,
                       before=before, after=after,
                       detail=" ".join(x for x in (
                           "Акции трясёт, а гособлигации держатся — редкое сочетание: "
                           "продают из-за страха, а не из-за проблем с деньгами в системе.",
                           _dist_text(payload, "vol")) if x),
                       meaning=effect))
    if "bond" in moved:
        on = new[2] == 1
        out.append(_ev(f"bond_{'on' if on else 'off'}:{asof}",
                       "bond_flag_on" if on else "bond_flag_off",
                       "Долговой рынок вошёл в стресс" if on
                       else "Долговой рынок вышел из стресса",
                       "warn" if on else "info", now,
                       before="ОФЗ под давлением" if not on else "ОФЗ спокойны",
                       after="ОФЗ под давлением" if on else "ОФЗ спокойны",
                       detail=_dist_text(payload, "bond") or None, meaning=effect))
    # Расстояния до порогов — по тем флагам, о которых не сказали события выше;
    # если сказали обо всех, запись всё равно несёт число (слияние дубль уберёт).
    told = {"bond"} | ({"vol"} if any(e["kind"] == "buy_window_open" for e in out) else set())
    rest = [f for f in moved if f not in told] or moved
    detail = " ".join(x for x in (_dist_text(payload, f) for f in rest) if x)
    out.append(_ev(f"cell:{asof}:{_gate_code(new)}", "state_cell_change",
                   "Признаки рынка для ворот сменились",
                   "info", now, before=before, after=after,
                   detail=detail or None, meaning=effect))
    return out


def _priced(pl):
    """Что рынок закладывает к заседанию — словами, а не подписью тайла.

    Подпись тайла «RUSFAR 3M у ключа — снижение НЕ в цене» уезжала в сообщение
    дословно: сокращение, капслок и ни одного числа. Вывод (снижение, повышение или
    «не ждёт») берётся из подписи — его считает тайл, — а числа из его полей.
    """
    text = str(pl.get("priced_text") or "")
    rus, spread = pl.get("rusfar3m"), pl.get("spread_pp")
    if not isinstance(rus, (int, float)) or not isinstance(spread, (int, float)):
        if "не получен" in text:
            return ""      # «сказать нельзя» — пустое, не печатаем
        return wording.sentence(wording.ru_decimals(text))
    head = f"Ставка денежного рынка на 3 месяца (RUSFAR) — {_pct(rus, 2)}"
    gap = wording.pp(abs(spread), 2, plus=False)
    if "снижение" in text and "НЕ" not in text:
        return f"{head}, на {gap} ниже ключевой: рынок закладывает снижение."
    if "ужесточение" in text:
        return f"{head}, на {gap} выше ключевой: рынок закладывает повышение."
    return f"{head}, почти вровень с ключевой: снижения рынок не ждёт."


def _cb(payload, prev, now):
    mons = _mons(payload)
    tile = mons.get("cb_meeting") or {}
    pl = tile.get("payload") or {}
    out = []
    days, nxt = pl.get("days_left"), pl.get("next_meeting")
    key_rate, cons = pl.get("key_rate"), pl.get("consensus")
    if nxt and days == 1:
        # Пустое не печатается (стандарт сообщений §1): нет консенсуса — нет и
        # фразы о нём, «консенсус в данные не внесён» читателю ничего не сообщает.
        waited = (f", аналитики ждут {_pct(cons, 2)}"
                  if isinstance(cons, (int, float)) else "")
        out.append(_ev(f"cb_reminder:{nxt}", "cb_reminder",
                       "Завтра заседание Банка России", "info", now,
                       detail=f"Сейчас ключевая ставка {_pct(key_rate, 2)}{waited}.",
                       meaning=_priced(pl)))
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
        # Без консенсуса сюрприза нет, и фразы о нём тоже нет: «сказать нечем» —
        # это молчание, а не текст (стандарт сообщений §1).
        if surprise is None:
            meaning = None
        elif surprise == 0:
            meaning = "Решение совпало с тем, чего ждали аналитики."
        else:
            meaning = (f"Это на {wording.bp(abs(surprise), plus=False)} "
                       + ("выше" if surprise > 0 else "ниже")
                       + f" ожиданий аналитиков ({_pct(cons, 2)}): рынку придётся "
                       + "переставлять свои прогнозы.")
        step = "снизил" if delta_bp < 0 else "повысил"
        out.append(_ev(f"cb_decision:{payload.get('asof_trading_day')}:{key_rate}",
                       "cb_decision", f"Банк России {step} ключевую ставку",
                       "warn" if surprise not in (0, None) else "info", now,
                       before=_pct(old_rate, 2), after=_pct(key_rate, 2),
                       detail=f"Шаг {wording.bp(delta_bp)}",  # «б.п.» уже с точкой
                       meaning=meaning, direction="down" if delta_bp < 0 else "up"))

    # СОХРАНЕНИЕ СТАВКИ — тоже решение, и до 12.09.2026 панель о нём молчала:
    # cb_decision срабатывает только на ИЗМЕНЕНИЕ ряда key_rate. Владелец получал
    # «Завтра заседание Банка России», а дальше — тишину, неотличимую от «ничего
    # не произошло» (заседание 11.09.2026: ЦБ сохранил 14,00%, панель промолчала).
    #
    # ПОЧЕМУ НЕ В ДЕНЬ ЗАСЕДАНИЯ. Решение попадает в ряд ЦБ на СЛЕДУЮЩИЙ рабочий
    # день (замер по всем пяти сменам 2026 года: ровно +1 торговый день, пятница →
    # понедельник). В день заседания «ставка не изменилась» означает лишь «ещё не
    # дошло», и объявить это сохранением было бы догадкой. Ждём CB_HOLD_CONFIRM_DAYS
    # календарных суток: для пятничного заседания это вторник — с запасом на
    # выходные и на праздничный понедельник.
    meeting, since = pl.get("last_meeting"), pl.get("days_since_last_meeting")
    was = pl.get("rate_at_last_meeting")
    if (meeting and isinstance(since, int) and since >= CB_HOLD_CONFIRM_DAYS
            and isinstance(key_rate, (int, float)) and isinstance(was, (int, float))
            and abs(key_rate - was) < 1e-9):
        cons = pl.get("last_consensus")
        surprise = round((key_rate - cons) * 100) if isinstance(cons, (int, float)) else None
        if surprise is None:
            meaning = None
        elif surprise == 0:
            meaning = "Решение совпало с тем, чего ждали аналитики."
        else:
            meaning = (f"Аналитики ждали {_pct(cons, 2)} — решение на "
                       f"{wording.bp(abs(surprise), plus=False)} "
                       + ("выше" if surprise > 0 else "ниже") + " ожиданий.")
        out.append(_ev(f"cb_hold:{meeting}", "cb_hold",
                       "Банк России сохранил ключевую ставку",
                       "warn" if surprise not in (0, None) else "info", now,
                       # Без строки «было → стало»: у сохранения обе половины
                       # одинаковы, и «14,00% → 14,00%» выглядит как сбой вёрстки.
                       # Число уходит в факт, где оно и читается.
                       detail=f"Ставка осталась {_pct(key_rate, 2)}. "
                              f"Заседание {_day(meeting, now)}.",
                       meaning=meaning))
    return out


# «Публикация ОРФР» — про СВЕЖИЙ релиз. Данные месячные, выходят в середине
# следующего месяца: 45 суток покрывают самый поздний релиз с запасом.
# Сколько суток ждать, прежде чем называть неизменную ставку СОХРАНЕНИЕМ. Решение
# доходит до ряда ЦБ на следующий рабочий день (все пять смен 2026 года: +1
# торговый день), заседания проходят по пятницам — четырёх суток хватает, чтобы
# пережить выходные и праздничный понедельник. Верхнюю границу держит сам тайл:
# last_meeting гаснет через CB_DECISION_FRESH_DAYS = 7 суток.
CB_HOLD_CONFIRM_DAYS = 4

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
    return [_ev(f"orfr:{asof}", "orfr_published",
                "Банк России опубликовал, кто покупал и продавал акции", "info", now,
                detail=_orfr_month(tile, asof),
                meaning=(_orfr_pressure(pl) or
                         "Это единственный официальный ответ на вопрос, чьи деньги "
                         "двигали рынок в прошлом месяце."))]


# «ДУ» — сокращение из таблицы ЦБ (НФО в доверительном управлении). Читатель телеграма
# его не знает, а в ленте оно стояло без расшифровки.
_DU = re.compile(r"\bДУ\b")


def _orfr_month(tile, asof):
    """«Сентябрь 2026: доверительные управляющие −7,1 млрд ₽, физлица +6,8 млрд ₽.»

    Из чисел тайла, а не из его подписи: подпись сама начинается с месяца, и
    склейка давала «Сентябрь 2026: сентябрь 2026: ДУ −7,1 млрд» (аудит B5).
    """
    month = wording.ru_month(asof)
    last = (tile.get("payload") or {}).get("last") or {}
    bits = []
    for key, name in (("nfo_du", "доверительные управляющие"), ("fiz", "физлица")):
        if isinstance(last.get(key), (int, float)):
            bits.append(f"{name} {wording.bn_rub(last[key], plus=True)}")
    if bits:
        return f"{month.capitalize()}: " + ", ".join(bits) + "."
    head = wording.ru_decimals(tile.get("headline") or "").strip().rstrip(".")
    if head.lower().startswith(month.lower()):
        head = head[len(month):].lstrip(" :")
    head = _DU.sub("доверительные управляющие", head)
    return f"{month.capitalize()}: {head}." if head else ""


def _orfr_pressure(pl):
    """Сальдо управляющих за квартал против прошлого — из чисел, иначе подпись тайла."""
    se = pl.get("seller_exhaustion") or {}
    s3, p3 = se.get("sum_3m_nfo_du"), se.get("prev_3m")
    if isinstance(s3, (int, float)):
        head = f"Сальдо доверительных управляющих за 3 месяца {wording.bn_rub(s3, plus=True)}"
        if s3 >= 0:
            return head + " — продавец ушёл."
        if isinstance(p3, (int, float)):
            return (head + f" против {wording.bn_rub(p3, plus=True)} кварталом ранее — "
                    + ("давление слабеет." if s3 > p3 else "давление растёт."))
        return head + "."
    text = wording.ru_decimals(se.get("text") or "")
    return wording.sentence(_DU.sub("управляющие", text)) if text else ""


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
    tail = (f", при спросе {wording.bn_rub(demand)}"
            if isinstance(demand, (int, float)) else "; спрос биржа не раскрывает")
    return [_ev(f"auction_failed:{date_}", "auction_failed",
                "Аукцион ОФЗ не состоялся", "warn", now,
                detail=f"{_day(date_, now)}: размещено "
                       f"{wording.bn_rub(pl.get('placed_bn'))}{tail}.",
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
        # spread_pp тайла — дивдоходность МИНУС вклад. Отрицательный спред значит, что
        # вклад платит БОЛЬШЕ дивидендов. До 03.10.2026 слова стояли наоборот, и
        # 14.09/22.09.2026 в ленту ушло «вклад даёт на 4,5 п.п. меньше дивидендов»
        # при вкладе 12,95% против дивидендов 8,5% (аудит 03.10.2026, §2.3).
        if abs(spread) < 0.005:
            compare = "столько же, сколько дивидендная доходность акций."
        else:
            compare = (f"на {wording.pp(abs(spread), 1, plus=False)}"
                       + (" меньше" if spread > 0 else " больше")
                       + " дивидендной доходности акций.")
        meaning = (f"Вклад теперь даёт {compare} Чем выгоднее вклад, тем дольше "
                   "деньги не пойдут с депозитов на биржу.")
    return [_ev(f"deposit_uptick:{pl.get('deposit_asof')}", "deposit_uptick",
                "Банки подняли ставки по вкладам", "info", now,
                before=_pct(old, 2), after=_pct(new, 2),
                detail=f"Прибавка {wording.pp(step)}",  # «п.п.» уже с точкой
                meaning=meaning, direction="up")]


def _against(payload, prev, state, now):
    """«Рынок растёт вопреки оценке» — одно событие на эпизод (аудит 10.11.2026, этап 2).

    Условие считает конвейер (compute/skills.against): позиция «деньги» исполнена, а
    индекс полной доходности за 20 сессий вырос больше чем на 10 %. Эпизод — сигналы
    не дальше 30 дней друг от друга, его начало — ключ дедупа: сообщаем один раз за
    эпизод, сколько бы дней подряд условие ни держалось. Это повод посмотреть, а не
    сигнал: позиция по правилу от него не меняется.
    """
    a = (payload.get("verdict") or {}).get("against") or {}
    start = a.get("episode_start")
    if not a.get("active") or not start or state.get("against_alerted") == start:
        return []
    state["against_alerted"] = start
    rise = a.get("rise_20_pct")
    st = a.get("stats") or {}
    hist = ""
    if st.get("with_fwd"):
        hist = (f"На истории с {str(a.get('since') or '')[:4]} года таких эпизодов "
                f"{st.get('episodes')}; через {a.get('fwd_sessions')} сессий рынок был выше в "
                f"{st.get('higher')} из {st.get('with_fwd')}, в среднем "
                f"{_pct(st.get('fwd_avg_pct'), 1, plus=True)} против "
                f"{_pct(st.get('flat_fwd_avg_pct'), 1, plus=True)} по всем дням в деньгах.")
    return [_ev(f"against:{start}", "against_rally", "Рынок растёт вопреки оценке", "info", now,
                detail=(f"Индекс полной доходности за {a.get('window')} сессий "
                        f"{_pct(rise, 1, plus=True)}, а позиция — деньги. " + hist).strip(),
                meaning="Повод посмотреть, а не сигнал: позиция по правилу не меняется.",
                direction="up")]


# ВЫДЕРЖКА санитарной тревоги об источнике — только на интрадей-такте.
#
# С 02.10 по 28.10.2026 в ops-канал ушло ≈20 санитарных сообщений 842, и почти
# половина — пары «протух → снова отвечает» интрадей-такта через 2–10 минут:
# 13.10 (ISS, один такт), 20.10 (ЦБ, один такт), 21.10 (Polymarket, два такта).
# За месяц у интрадей-такта всего 4 неудачных прогона, самый длинный сбой — два
# такта подряд. Такая пара ничего не просит у владельца: следующий такт уже
# починил сам, а панель между ними считала по кэшу. Выдержка — та же, что у
# ops-моста (dash-notify, 15 минут): сбой короче — тишина, дольше — одна тревога
# с исходным временем начала и одна отбивка.
#
# ПОЧЕМУ НЕ ПО lag_min. fetched_at пишется и при ОШИБКЕ опроса (fetch.make_meta
# кладёт его в meta всегда), поэтому у источника в ошибке lag_min ≈ 0 на каждом
# такте. Выдержка «lag_min ≥ 15» заглушила бы долгий отказ навсегда. Начало сбоя
# своё — `source_down` в состоянии алертов: оно переживает перезапуск процесса и
# не зависит от того, что источник пишет о себе.
#
# ПОЧЕМУ ТОЛЬКО ИНТРАДЕЙ. Интрадей идёт раз в 5 минут, и следующий такт сам
# скажет, был ли сбой коротким. У суточного (16:05, 20:55 UTC), месячного (три
# раза в сутки) и недельного тактов следующий прогон — через 5 часов и больше:
# выдержка там — это часы немоты, поэтому тревога сразу.
#
# Такт плавает на ±20 с (RandomizedDelaySec), а `now` берётся в начале прогона:
# четвёртый подряд неудачный такт приходит через 14:40–15:20 после первого.
# Допуск в минуту делает границу устойчивой: сбой в три такта (до 10 минут) —
# тишина, четвёртый неудачный такт — тревога.
SOURCE_HOLD = timedelta(minutes=15)
SOURCE_HOLD_SLACK = timedelta(minutes=1)
BAD_SOURCE = ("stale", "error")


def _parse_ts(text):
    try:
        moment = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


# РЯДЫ РЕШЕНИЯ — те, от которых зависит позиция. Тревога об источнике до 20.11.2026
# была одна на СЕМЬЮ, а статус семьи — статус её худшего ряда. Пока семья биржевых
# рядов помечена устаревшей из-за доходности ВДО (монитор, в решение не входит),
# отказ индекса, RGBI или кривой ОФЗ в той же семье тревоги не давал: правило видело
# «семья уже плохая — уже сообщали» (аудит 20.11.2026, §9.7; так обстояло дело с
# 19.11). То же у семьи Минфина (налоговая цена нефти рядом с дефицитом бюджета) и
# ЦБ. Теперь у каждого ряда решения свой снимок, своя выдержка и своя тревога, а
# статус семьи для семейной тревоги считается БЕЗ них.
#
# Какие ряды — решает реестр (pipeline/lib/registry.py): роль «ядро» — слагаемые
# оценки рынка (в том числе налоговая цена нефти, хотя она и не помечена
# обязательной), и обязательные ряды ролей «состояние» и «сигнал».
DECISION_ROLES = ("core", "state", "signal")
BAD_CORE = ("stale", "error", "missing")
_BAD_RANK = {"stale": 1, "missing": 2, "error": 3}


def is_decision_series(spec):
    """Ряд реестра, от которого зависит решение о позиции."""
    spec = spec or {}
    role = spec.get("role")
    return role == "core" or (role in DECISION_ROLES and bool(spec.get("required")))


def _family_units(payload):
    """{семья: строка} — статус семьи БЕЗ рядов решения.

    Витрина старого вида (без поля bad_series) отдаётся как есть: там семья — это
    худший ряд, и правило работает по-прежнему.
    """
    out = {}
    for name, meta in (payload.get("sources") or {}).items():
        if not isinstance(meta, dict):
            continue
        bad = meta.get("bad_series")
        if not isinstance(bad, list) or meta.get("status") not in BAD_SOURCE:
            out[name] = meta
            continue
        rest = [r for r in bad if isinstance(r, dict) and not r.get("core")
                and r.get("status") in BAD_SOURCE]
        if not rest:
            # Плохи только ряды решения: о них скажет своя тревога, семья для
            # семейного правила здорова.
            out[name] = dict(meta, status="ok", series=None, asof=None, stale_reason=None)
            continue
        worst = max(rest, key=lambda r: _BAD_RANK.get(r.get("status"), 0))
        out[name] = dict(meta, **{k: worst.get(k) for k in
                                  ("status", "series", "asof", "lag_min", "stale_reason")})
    return out


def _core_units(payload):
    """{ряд решения: строка с полем family} — из sources[семья].core_series."""
    out = {}
    for family, meta in (payload.get("sources") or {}).items():
        rows = meta.get("core_series") if isinstance(meta, dict) else None
        if not isinstance(rows, dict):
            continue
        for sid, row in rows.items():
            if isinstance(row, dict):
                out[sid] = dict(row, family=family)
    return out


def _down_records(state, key, current):
    """Записи «сломан с …» для тех, кто ещё есть в сводке."""
    raw = state.get(key)
    if not isinstance(raw, dict):
        return {}
    return {name: dict(rec) for name, rec in raw.items()
            if name in current and isinstance(rec, dict) and _parse_ts(rec.get("since"))}


def _unit_events(units, old, down, payload, now, core=False):
    """Тревоги и отбивки по набору «единиц»: семьям источников или рядам решения.

    Логика одна — переход в плохой статус, выдержка на интрадей-такте, отбивка о
    выздоровлении; различаются виды событий и слова. `down` мутируется.
    """
    out = []
    hold = payload.get("run_mode") == "intraday"
    bad_set = BAD_CORE if core else BAD_SOURCE
    # Журнал того такта, что заметил сбой: интрадей-отказ в журнале суточного не найти.
    unit = "moex-radar-" + (payload.get("run_mode") if payload.get("run_mode") in
                            ("intraday", "daily", "weekly", "monthly", "manual") else "daily")
    for name, meta in sorted(units.items()):
        st = meta.get("status")
        was_bad = old.get(name) in bad_set
        series = meta.get("series")
        who = f"{name} ({series})" if series and series != name else name
        day = _day(meta.get("asof") or payload.get("asof_trading_day"), now)
        label = str(meta.get("label") or "").strip()
        family = meta.get("family")
        if core:
            # «Индекс гособлигаций» (ряд rgbi, источник iss): данные от 17.11.
            head = ((f"«{label}» " if label else "")
                    + f"(ряд {who}" + (f", источник {family}" if family else "") + "): ")
            # У пустого ряда своей даты нет: день вердикта на её месте спорил бы с
            # заголовком «в хранилище нет ни одной точки».
            data_from = head + (f"данные от {day}." if meta.get("asof")
                                else "своих точек в хранилище нет.")
        else:
            data_from = ((f"Ряд {series}: данные от " if series and series != name
                          else "Данные в панели от ") + f"{day}.")
        if st not in bad_set:
            # Источник в порядке: запись о сбое больше не нужна. Если сбой был
            # коротким и тревога выдерживалась, снимок держал прежний «ok», и
            # was_bad здесь ложно — отбивки без тревоги не бывает.
            rec = down.pop(name, None)
            since = _parse_ts((rec or {}).get("since"))
            # ОТБИВКА О ВЫЗДОРОВЛЕНИИ. Её здесь не было, и молчание после тревоги
            # оказывалось двусмысленным: «уже починилось» и «до сих пор лежит»
            # выглядят одинаково — никак. Оплачено 08.09.2026: ISS отвалился на
            # десять минут, в 15:00 следующий прогон прошёл начисто, а владелец
            # ещё через семнадцать часов считал источник лежащим и написал
            # «раньше никогда так надолго не отваливалось».
            #
            # Тревога при этом НЕ была обезоружена: старое состояние переписывается
            # на «ok», и следующий отказ сообщит о себе снова. Молчал не сторож —
            # молчал конец истории.
            #
            # Тот же урок уже оплачен в стороже панелей (dash-watch, репо 839,
            # ключ quotes-agent, август 2026): ключ без ветки выздоровления
            # превращает канал в источник тревоги без разрядки.
            if was_bad:
                # Сутки и больше — в сутках: «187 ч» никто не считает в уме (у месячного
                # ряда налоговой цены нефти сбой меряется днями).
                lasted = (f" Сбой длился {wording.days_hours((now - since).total_seconds() / 60)}"
                          f" (с {wording.msk_when(since, now)})."
                          if since and now > since else "")
                # «Смотреть» — туда, что на странице есть. Раздела «Источники» на ней
                # нет с переделки витрины (fa614dc): о ряде решения говорит плашка
                # вверху страницы, о мониторе — точка статуса на его карточке.
                if core:
                    out.append(_ev(f"core_series_ok:{name}:{_stamp(since or now)}",
                                   "core_series_ok", f"ряд решения {name} снова обновляется",
                                   "info", now, fact=data_from + lasted,
                                   meaning="Решение о позиции снова считается по свежим "
                                           "числам этого ряда.",
                                   where="Смотреть: страницу панели — плашка об источнике "
                                         "вверху должна исчезнуть."))
                else:
                    out.append(_ev(f"source_ok:{name}:{payload.get('asof_trading_day')}",
                                   "source_ok", f"источник {who} снова отвечает",
                                   "info", now, fact=data_from + lasted,
                                   meaning="Жёлтая точка с карточек этого источника снята — "
                                           "панель снова считает по свежим числам.",
                                   where="Смотреть: карточки этого источника на панели."))
            continue
        if was_bad:
            continue  # уже сообщали, повторять каждый прогон нельзя
        # Начало сбоя — из записи прошлых тактов, иначе этот прогон. Время из
        # будущего (часы машины перевели назад) считаем началом сейчас: иначе
        # выдержка не кончилась бы никогда.
        since = _parse_ts((down.get(name) or {}).get("since")) or now
        since = min(since, now)
        held = hold and now - since < SOURCE_HOLD - SOURCE_HOLD_SLACK
        down[name] = {"since": _iso(since), "status": st, "held": held}
        if held:
            continue  # выдержка: снимок держит прежний статус (_snapshot_keeping_undelivered)
        # «Последний удачный опрос» — только у протухшего по возрасту: у источника в
        # ошибке fetched_at свежий (make_meta пишет его и при отказе), и «0 мин назад»
        # про неудачный опрос читалось как «всё хорошо».
        age = meta.get("lag_min")
        age_txt = (f" Последний удачный опрос {wording.days_hours(age)} назад."
                   if isinstance(age, (int, float)) and st == "stale" else "")
        waited = now - since
        if waited >= timedelta(minutes=1):
            age_txt = (f" Сбой с {wording.msk_when(since, now)}, длится "
                       f"{wording.days_hours(waited.total_seconds() / 60)}.") + age_txt
        # Две разные протухшести чинятся в разных местах: «не опрашивался» — это
        # конвейер или расписание, «отдаёт старое» — сам источник. Одно слово на
        # обе посылало владельца искать не там.
        if st == "error":
            word = "не отвечает"
        elif st == "missing":
            word = "пуст: в хранилище нет ни одной точки"
        elif meta.get("stale_reason") == "data":
            word = "отвечает, но отдаёт старые данные"
        else:
            word = "не опрашивался вовремя"
        # Статус семьи — это статус ХУДШЕГО ряда в ней (run.py), и его имя семья
        # знает (meta.series). Без него сообщение зовёт проверять весь источник:
        # «источник iss отдаёт устаревшие данные» при двадцати живых рядах ISS и
        # одном сломанном — это адрес, по которому нечего чинить. Дата в факте по
        # той же причине принадлежит РЯДУ, а не источнику (оплачено 20.08.2026:
        # ISS сломал расчёт доходности ВДО, панель сказала «данные от 13.08», а
        # индекс был свежий).
        # Время события — НАЧАЛО сбоя, а не конец выдержки: тревога, дождавшаяся
        # четвёртого такта, говорит о том, что сломалось пятнадцать минут назад.
        # Раздела «Источники» на странице нет (см. отбивку выше): у ряда решения на
        # ней горит плашка вверху, у монитора — точка статуса на карточке.
        where = (f"Смотреть: journalctl -u {unit} -n 50"
                 + ("; на странице — плашка об источнике вверху." if core
                    else " и точку статуса на карточках этого источника."))
        if core:
            # Ключ — по НАЧАЛУ сбоя, а не по дню вердикта: второй отказ того же ряда
            # в тот же день — новая тревога, а не «уже отправляли».
            out.append(_ev(f"core_series_stale:{name}:{_stamp(since)}",
                           "core_series_stale", f"ряд решения {name} {word}", "warn", since,
                           fact=data_from + age_txt,
                           meaning="От этого ряда зависит решение о позиции: пока он не "
                                   "обновится, панель считает позицию по его последнему "
                                   "удачному числу.",
                           where=where))
        else:
            out.append(_ev(f"source_stale:{name}:{payload.get('asof_trading_day')}",
                           "source_stale", f"источник {who} {word}", "warn", since,
                           fact=data_from + age_txt,
                           meaning="Панель считает по последним удачным числам и выглядит "
                                   "рабочей — карточки этого источника помечены жёлтой точкой.",
                           where=where))
    return out


def _stamp(moment):
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M")


def _sources(payload, prev, state, now):
    """Семьи источников (без рядов решения) и ряды решения — каждый своим правилом."""
    families = _family_units(payload)
    down = _down_records(state, "source_down", families)
    out = _unit_events(families, prev.get("sources") or {}, down, payload, now)
    state["source_down"] = down
    series = _core_units(payload)
    core_down = _down_records(state, "core_down", series)
    out += _unit_events(series, prev.get("core_series") or {}, core_down, payload, now,
                        core=True)
    state["core_down"] = core_down
    return out


def _held(state, key):
    return sorted(name for name, rec in (state.get(key) or {}).items()
                  if isinstance(rec, dict) and rec.get("held"))


def held_sources(state):
    """Источники, тревога о которых сейчас выдерживается."""
    return _held(state, "source_down")


def held_core_series(state):
    """Ряды решения, тревога о которых сейчас выдерживается."""
    return _held(state, "core_down")


# ----------------------------------------------------------------- падение расчёта
#
# Упавший расчёт суточного прогона публиковал вчерашний вердикт со свежим временем:
# юнит зелёный, событий нет, страница выглядит обновлённой (аудит 20.11.2026, §9.7).
# run.py кладёт в витрину поле compute_error («ТипОшибки @ время»), а это правило
# даёт тревогу в канал отказов — с защёлкой до доставки (LATCH_FIELDS) и отбивкой,
# когда расчёт снова прошёл.

def _compute_failed(payload, prev, now, crash=None, state=None):
    err, was = payload.get("compute_error"), prev.get("compute_error")
    mode = payload.get("run_mode") if payload.get("run_mode") in (
        "intraday", "daily", "weekly", "monthly", "manual") else "daily"
    asof = payload.get("asof_trading_day")
    day = now.astimezone(wording.MSK).strftime("%Y-%m-%d")
    state = state if state is not None else {}
    # НАЧАЛО сбоя. Каждый упавший полный прогон пишет в compute_error СВОЁ время, и
    # снимок к отбивке помнит последнее: «не проходил с 07:30 МСК» о расчёте, который
    # лежал полтора дня (суточный такт теперь и утром). Первое время держим отдельно.
    if err and not state.get("compute_down_since"):
        first = _parse_ts(str(err).split(" @ ")[-1].strip()) or now
        state["compute_down_since"] = _iso(min(first, now))
    if err and not was:
        kind = str(err).split(" @ ")[0].strip() or "ошибка"
        note = str(crash or "").strip()
        what = note if note.startswith(kind) else (f"{kind}: {note}" if note else kind)
        state_now = ((payload.get("verdict") or {}).get("position") or {}).get("state")
        tail = (f", позиция — {POSITION_WORD[state_now]}" if state_now in POSITION_WORD else "")
        return [_ev(f"compute_failed:{day}", "compute_failed",
                    "расчёт упал: на странице прежний вердикт", "warn", now,
                    fact=f"Прогон {wording.msk_when(now, now)}: {wording.sentence(what[:200])} "
                         f"Опубликован вердикт от {_day(asof, now)}{tail}.",
                    meaning="Оценка и позиция на странице — от прошлого удачного расчёта, "
                            "хотя время обновления свежее. Сообщения о смене позиции не "
                            "будет, пока расчёт не пройдёт.",
                    where=f"Смотреть: journalctl -u moex-radar-{mode} -n 80.")]
    if not err and payload.get("run_mode") != "intraday":
        first = state.pop("compute_down_since", None)
    else:
        first = None
    if was and not err and payload.get("run_mode") != "intraday":
        since = first or str(was).split(" @ ")[-1].strip()
        return [_ev(f"compute_ok:{_stamp(now)}", "compute_ok",
                    "расчёт снова проходит", "info", now,
                    fact=f"Вердикт пересчитан, расчёт за {_day(asof, now)}."
                         + (f" Расчёт не проходил с {wording.msk_when(since, now)}."
                            if _parse_ts(since) else ""),
                    meaning="Оценка и позиция на странице снова от текущего расчёта.",
                    where="Смотреть: страницу панели.")]
    return []


def _health(payload, prev, now):
    """Регламентный порог плановой ревалидации: IC ниже нуля 12 месяцев подряд.

    Одно событие вместо двух прежних (health_dead + health_review_due, аудит
    02.09.2026). health_dead срабатывал на ПЕРВЫЙ месяц ниже нуля — при окне 24 месяца
    интервал шириной ±0,41, и значение −0,08 накрывает ноль втрое; владелец получал
    приговор модели там, где честный диагноз — «на этом окне информации нет»
    (оплачено 01.09.2026). Теперь тревога одна и приходит в момент, который что-то
    значит: статус review = порог ×12 = review_due, и это один переход, а не два.

    Сообщение не говорит «меняй состав» и не говорит «сокращай позицию»: протокол по
    порогу — реколибровка, а вторая половина условия (механизм у кандидата) —
    работа человека, панель её не измеряет.
    """
    health = ((payload.get("core") or {}).get("health") or {})
    st = health.get("status")
    if st != "review" or prev.get("health") == "review":
        return []
    ci = health.get("ic_ci95") or []
    span = (f" (95% интервал от {_num(ci[0], 2, True)} до {_num(ci[1], 2, True)})"
            if len(ci) == 2 and ci[0] is not None else "")
    months = health.get("below_zero_months") or 0
    since = health.get("below_since")
    return [_ev(f"health_review:{since or payload.get('asof_trading_day')}", "health_review",
                "достигнут порог пересмотра модели", "warn", now,
                fact=f"Связь оценки с рынком держится ниже нуля {months} "
                     f"{wording.plural(months, 'месяц', 'месяца', 'месяцев')} подряд"
                     + (f" (с {wording.ru_month(since, nominative=False)})" if since else "")
                     + f", сейчас {_num(health.get('ic_24m'), 2, True)}{span}.",
                meaning="Это плановая ревалидация состава по регламенту §7 — "
                        "реколибровка, а не сокращение позиции: оценка продолжает "
                        "считаться, позиция ведётся по прежнему правилу. Второе условие "
                        "регламента, наличие механизма у кандидата, панель проверить "
                        "не может — это работа человека.",
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
# С 20.11.2026 семейство стоит на флагах ворот и идёт только в журнал страницы
# (JOURNAL_KINDS = это же семейство), но смысл слияния прежний: один поворот — одна
# запись журнала с самым предметным заголовком, остальное подпунктами.
#
# Порядок — ОТ КОНКРЕТНОГО К ОБЩЕМУ: заголовком становится самый предметный факт,
# остальное уходит подпунктами в «Что за этим стоит».
#
# Окно входа впереди всех: самый редкий режим за 22 года (8–10 месяцев) и
# единственный, который панель считает поводом добавлять риск.
# Дальше облигационный флаг, и лишь потом смена режима. Это не произвол: «Долговой
# рынок вошёл в стресс» — конкретное событие с понятным следствием, а «Признаки
# рынка сменились» — обобщение над ним. Заголовок обязан нести предметное, обобщение
# читается подпунктом. Обратный порядок первой редакции давал заголовки, из которых
# нельзя было понять, ЧТО ИМЕННО произошло.
#
# core_flip СЮДА НЕ ВХОДИТ и не должен: наклон и ворота — разные слои модели
# («сначала ворота, потом наклон», docs/ARCHITECTURE.md). Они меняются независимо, и
# слияние спрятало бы одно за другим. position_change — тоже отдельно: позиция есть
# ИТОГ ворот и наклона, и смена режима без смены позиции (или наоборот) — две разные
# новости.
REGIME_FAMILY = ("buy_window_open", "bond_flag_on", "bond_flag_off", "state_cell_change")
# Семейство режима живёт только в журнале; core_flip — тоже, но в семейство не сливается.
assert frozenset(REGIME_FAMILY) | {"core_flip"} == JOURNAL_KINDS, \
    "журнальные виды: семейство режима и дневной разворот оценки"

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
        if detail and detail != str(main.get("detail") or "").strip().rstrip(".")                 and detail not in causes:
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


def detect(payload, state, now=None, merge=True, crash=None):
    """Все переходы этого прогона. state мутируется (гистерезис знака ядра).

    `crash` — текст исключения упавшего расчёта (run.py): уходит только в тревогу
    канала отказов, в публичную витрину попадает лишь тип ошибки и время.

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
    changed = _position_change(payload, prev, now)
    events += changed
    events += _position_watch(payload, prev, state, now, bool(changed))
    events += _urals_month(payload, prev, state, now)
    events += _gate_events(payload, prev, now)
    events += _cb(payload, prev, now)
    events += _orfr(payload, prev, now)
    events += _auction(payload, prev, now)
    events += _deposit(payload, prev, state, now)
    events += _against(payload, prev, state, now)
    events += _sources(payload, prev, state, now)
    events += _health(payload, prev, now)
    events += _core_missing(payload, prev, now)
    events += _compute_failed(payload, prev, now, crash, state)
    return merge_regime(events) if merge else events


# ------------------------------------------------------------------ доставка

def render(event, now=None):
    """Текст для телеграма. Санитарные — в формате общего мостика панелей.

    Слово «Внимание.» перед тревогой убрано намеренно: его роль теперь несёт значок
    вида события в заголовке, а как ПЕРВОЕ слово сообщения оно съедало место, в
    котором читатель ищет суть. Ровно по той же причине его нет у 837/838.
    """
    if is_ops(event):
        return wording.render_ops(event, now=now)
    # now — момент отправки: событие из очереди повторов, ушедшее заметно позже,
    # чем случилось, получает строку ⏳ под заголовком (стандарт сообщений §4).
    return wording.render_market(event, now=now)


def dispatch(events, dry_run=False, enabled=True, now=None):
    for ev in events:
        if not enabled:
            ev["delivered"], ev["skip"] = False, "алерты выключены"
        elif dry_run:
            ev["delivered"], ev["skip"] = False, "dry-run"
        elif is_journal(ev):
            # Журнальное: только лента страницы (её пишет _feed_add). Ни телеграма,
            # ни хаба — в сеть не ходим вовсе, и повторять здесь нечего.
            ev["channel"], ev["outcome"], ev["delivered"] = JOURNAL, JOURNAL, True
        elif is_ops(ev):
            # Санитарное: только в ops-канал. Ни зеркала в хаб, ни ленты — это
            # сообщение для того, кто чинит, а не для того, кто читает рынок.
            ev["channel"] = "ops"
            outcome = telegram.deliver(ev["key"], render(ev, now), channel="ops")
            ev["outcome"] = outcome
            # OFF («ERROR_* не заданы») здесь НЕ доставка. Санитарное событие больше
            # никуда не идёт — ни в ленту витрины, ни в хаб, — поэтому непрочитанным
            # оно исчезает совсем. Защёлка ниже (_snapshot_keeping_undelivered) держит
            # состояние ровно на этом флаге: пока не доставлено, правило родит событие
            # заново, и находка дождётся настроенного канала. Повторов при этом не
            # видно никому и в сеть не ходит ни один такт: config() отвечает сразу.
            ev["delivered"] = outcome in (telegram.SENT, telegram.DUP)
        else:
            outcome = telegram.deliver(ev["key"], render(ev, now),
                                       silent=ev.get("kind") in SILENT_KINDS)
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
            # уже уехало в хаб строкой ниже; считать его недоставленным значит вечно
            # держать в очереди pending и переотправлять каждые 5 минут. Дублей это не
            # даёт — хаб схлопывает повтор по (source, eventId), см. lib/nexus.py, —
            # но очередь, где висит вечный «недоставленный», вытесняет живые события
            # (прогон без TELEGRAM_*, README называет их необязательными).
            ev["nexus"] = nexus.deliver(ev)
            ev["delivered"] = (outcome in (telegram.SENT, telegram.DUP, telegram.OFF)
                               and ev["nexus"] in (nexus.SENT, nexus.OFF))
    return events


def _fresh(ev, now):
    """Имеет ли смысл повторять событие из очереди.

    Обычное — сутки: дальше новость протухла. Главное (смена позиции) —
    MUST_DELIVER_MAX_DAYS: оно не протухает, пока позиция та же, а при следующей
    смене позиции его вытеснит новое такое же событие (_supersede).
    """
    try:
        ts = datetime.fromisoformat(str(ev.get("ts")).replace("Z", "+00:00"))
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    if ev.get("kind") == "position_watch":
        # Предупреждение о дне решения, прошедшем по Москве, — уже не предупреждение:
        # ответ на него (смена позиции или отбой) пришёл или придёт сам. Общие сутки
        # очереди пропускали «в пт возможен переход…» досылкой в субботу (проверка
        # уведомлений 20.11.2026). День решения — в ключе: position_watch:<день>:<куда>.
        parts = str(ev.get("key") or "").split(":")
        if len(parts) > 2 and now.astimezone(wording.MSK).strftime("%Y-%m-%d") > parts[1]:
            return False
    limit = (timedelta(days=MUST_DELIVER_MAX_DAYS) if must_deliver(ev)
             else timedelta(hours=PENDING_MAX_HOURS))
    return now - ts < limit


def _supersede(pending, events, now):
    """Новая смена позиции вытесняет недоставленные прежние.

    Доставлять обе незачем: читателю нужна позиция, которая стоит сейчас. Но о том,
    что прежнее сообщение не дошло, новое обязано сказать — иначе «переход в деньги»
    приходит человеку, который не знал, что панель была в акциях.
    """
    fresh = [e for e in events if must_deliver(e)]
    lost = [e for e in pending if must_deliver(e)]
    if not fresh or not lost:
        return pending
    newest = fresh[-1]
    # В очереди событие лежит и тогда, когда телеграм его ПРИНЯЛ, а не принял только
    # хаб (доставлено = оба канала). Маркер телеграма есть — сообщение до канала
    # дошло, и «не дошло» в новом сообщении было бы неправдой.
    notes = [f"Сообщение о предыдущей смене ({str(e.get('title') or '').split(': ')[-1]}, "
             f"{wording.msk_when(e.get('ts'), now)}) до канала не дошло." for e in lost
             if not _telegram_got(e)]
    if notes:
        newest["detail"] = "\n".join([newest.get("detail") or ""] + notes).strip()
        newest["text"] = wording.plain_text(newest)
    return [e for e in pending if not must_deliver(e)]


def _telegram_got(event):
    """Уходило ли событие в канал рынка (маркер дедупа телеграма). Не бросает."""
    try:
        return telegram.already_sent(event.get("key"))
    except Exception:  # noqa: BLE001 — сомнение трактуем как «не дошло»: так было раньше
        return False


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
    left = [{k: e[k] for k in EVENT_FIELDS if k in e}
            for e in _dedup(batch) if not e.get("delivered")]
    # Главное событие срез очереди не трогает: потерять его из-за сорока мелких
    # повторов — та же потеря смены позиции, только другим путём.
    main = [e for e in left if must_deliver(e)]
    room = max(PENDING_MAX - len(main), 0)
    rest = [e for e in left if not must_deliver(e)]
    keep = {id(e) for e in main} | {id(e) for e in (rest[-room:] if room else [])}
    return [e for e in left if id(e) in keep]


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
# Поле-защёлка у каждого своё: у health_review — строка статуса (и флаг review_due
# рядом: это один и тот же порог), у core_missing — пара «оценка + режим» (правило
# сравнивает обе).
# До 18.08.2026 защёлка покрывала только два health-вида, и это было хуже, чем
# казалось: незащищённые source_stale и core_missing при недоставке умирали в
# pending по TTL 24 ч и больше не рождались НИКОГДА — снимок уже зафиксировал
# «error»/«None», и правило считало, что уже сообщало. Стор частично теряется —
# панель публикует «нет данных» поверх рабочего вердикта, watchdog видит свежий
# Last-Modified и молчит, владелец не узнаёт об аварии вовсе.
# lease_lost и payload_oversize защищены отдельно (after_publish, lease_ok).
LATCH_FIELDS = {
    "health_review": ("health", "health_review_due"),
    "core_missing": ("core_value", "cell"),
    # Падение расчёта: пока тревога не доставлена, снимок помнит «расчёт проходил»,
    # и следующий такт (он несёт то же поле compute_error) родит её заново.
    "compute_failed": ("compute_error",),
}


def _keep_prev_source(snap, prev_last, name, section="sources"):
    prev_sources = prev_last.get(section) or {}
    if name in prev_sources:
        snap.setdefault(section, {})[name] = prev_sources[name]
    else:
        # Источник впервые появился уже сломанным: раньше его в снимке не
        # было — убираем и сейчас, чтобы переход случился заново.
        (snap.get(section) or {}).pop(name, None)


def _snapshot_keeping_undelivered(payload, prev_last, batch, now, held=(), held_core=()):
    """Снимок текущего состояния, но защёлки недоставленных событий откатываются назад.

    `held` — источники на выдержке: о сбое ещё не сообщали, поэтому снимок держит их
    прежний статус. Иначе следующий такт увидел бы «уже сломан» и промолчал как
    «уже сообщали», а выздоровление прислало бы отбивку без тревоги. `held_core` —
    то же для рядов решения.
    """
    snap = snapshot(payload, now)
    for name in held:
        _keep_prev_source(snap, prev_last, name)
    for name in held_core:
        _keep_prev_source(snap, prev_last, name, "core_series")
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
            if name:
                _keep_prev_source(snap, prev_last, name)
        if kind == "core_series_stale":
            parts = str(event.get("key") or "").split(":")
            name = parts[1] if len(parts) > 2 else None
            if name:
                _keep_prev_source(snap, prev_last, name, "core_series")
    return snap


def _stuck_events(batch, state, now, log=None):
    """Тревога «главное событие не доходит» и отбивка, когда оно дошло.

    Главное событие (смена позиции) ждёт доставки в очереди, но ждать молча нельзя:
    владелец не знает, что ему не пришло то, по чему он действует. Канал отказов
    обвязки ходит другим ботом и может быть жив, когда рыночный лежит, — тревога
    уходит туда и сама несёт суть решения («было → стало», день исполнения). Если
    лежат оба канала, остаётся строка в журнале прогона: это не лечится.
    """
    stuck = state.get("stuck") if isinstance(state.get("stuck"), dict) else {}
    out = []
    for ev in batch:
        if not must_deliver(ev):
            continue
        key = ev.get("key")
        ts = _parse_ts(ev.get("ts"))
        waited = (now - ts) if ts else timedelta(0)
        late = wording.days_hours(waited.total_seconds() / 60)
        title = str(ev.get("title") or "").strip().rstrip(".")
        if ev.get("delivered"):
            if key in stuck:
                stuck.pop(key, None)
                out.append(_ev(f"delivery_ok:{key}", "delivery_ok",
                               "сообщение о смене позиции доставлено", "info", now,
                               fact=f"«{title}» дошло до канала рынка с опозданием {late}.",
                               meaning="Очередь главных сообщений пуста.",
                               where="Смотреть: канал рынка."))
            continue
        if ev.get("outcome") != telegram.FAIL or waited < STUCK_AFTER:
            continue
        if log:
            log(f"ГЛАВНОЕ СОБЫТИЕ НЕ ДОСТАВЛЕНО: {key} ждёт {late} — канал рынка не принимает")
        if key in stuck:
            continue        # тревога уже ушла: повторять её каждым тактом нельзя
        move = " → ".join(x for x in (ev.get("before"), ev.get("after")) if x)
        when = ""
        if ev.get("exec_day"):
            close = wording.exec_close(ev["exec_day"])
            when = (" Исполнять на закрытии " + _wd(ev["exec_day"], now)
                    + (" — оно уже прошло." if close and now >= close else " — оно ещё впереди."))
        out.append(_ev(f"delivery_stuck:{key}", "delivery_stuck",
                       "смена позиции не доходит до канала рынка", "warn", now,
                       fact=f"«{title}»" + (f": {move}" if move else "") + "."
                            + when
                            + f" Событие от {wording.msk_when(ts, now)}, не доставлено {late}.",
                       meaning="Канал рынка в Телеграме не принимает сообщения. Панель "
                               "повторяет отправку каждым прогоном — до доставки, но не "
                               f"дольше {MUST_DELIVER_MAX_DAYS} суток и не дольше следующей "
                               "смены позиции.",
                       where="Смотреть: страницу панели (позиция там уже новая) и "
                             "journalctl -u moex-radar-intraday -n 50."))
    state["stuck"] = stuck
    return out


def _mark_stuck(extra, state):
    """Тревога о недоставке ушла (или уже уходила) — запомнить, чтобы не повторять."""
    stuck = state.get("stuck") if isinstance(state.get("stuck"), dict) else {}
    for ev in extra:
        if ev.get("kind") == "delivery_stuck" and ev.get("delivered"):
            stuck[str(ev.get("key"))[len("delivery_stuck:"):]] = ev.get("ts")
    state["stuck"] = stuck


def prepare(payload, dry_run=False, enabled=True, now=None, seed_payload=None, log=None,
            crash=None):
    """Первая половина цикла: найти переходы, поднять очередь, спросить разбор.

    Ничего не отправляет и состояния не пишет. -> (batch, ctx): batch уже годится
    для ленты страницы (payload_events), ctx отдаётся в deliver.

    ПОЧЕМУ ЦИКЛ РАЗРЕЗАН (20.11.2026). Сообщение уходило раньше публикации
    страницы: ссылка «Открыть панель» из сообщения о смене позиции вела на
    страницу, где позиция ещё прежняя. Теперь порядок в run.py такой: prepare →
    лента в payload → публикация → deliver. Событие должно попасть в ленту ДО
    публикации (и с тем же разбором, что уйдёт в телеграм) — поэтому правила и
    разбор остаются до неё, а после неё только отправка. Причины прежнего порядка,
    кроме этой, в истории нет (порядок стоит с первой редакции, 013f25e).
    Обрыв между публикацией и deliver безопасен: состояние ещё не записано, и
    следующий прогон найдёт те же переходы заново.
    """
    now = now or datetime.now(timezone.utc)
    state = load_state()
    if seed_payload is not None:
        seed_from_payload(state, seed_payload, now)
    prev_last = dict(state.get("last") or {})
    events = detect(payload, state, now, crash=crash)
    pending = [e for e in (state.get("pending") or []) if _fresh(e, now)]
    pending = _supersede(pending, events, now)
    batch = _dedup(pending + events)
    # Комментарий проставляется ДО отправки и ДО записи в ленту: он должен уехать
    # одинаковым во все три места — телеграм, хаб и журнал витрины. В dry-run в сеть
    # не ходим: прогон «на посмотреть» не должен зависеть от чужого провайдера.
    #
    # СПРАШИВАЕМ ТОЛЬКО ПРО НОВОЕ. Часть правил рождает событие заново на КАЖДОМ такте
    # (cb_reminder — все 169 тактов в канун заседания), и телеграм гасит их дедупом уже
    # после того, как комментатор сходил к модели. Модель отвечает 150–200 с, а разбор
    # стоит ДО публикации при дедлайне такта 300 с — то есть лишний запрос платит не
    # деньгами, а риском не обновить витрину. Событие, чей ключ уже лежит в ленте, своё
    # уже получило: второй разбор ему не нужен и второй раз он никуда не уедет.
    #
    # Журнальным (флаги ворот) разбор не заказывается вовсе: они не уходят читателю
    # сообщением, и платить моделью за строку журнала незачем.
    if enabled and not dry_run:
        known = {e.get("key") for e in (state.get("feed") or [])}
        fresh = [e for e in batch if e.get("key") not in known and not is_journal(e)]
        try:
            commentary.annotate(fresh, payload, log=log)
        except Exception as exc:  # noqa: BLE001 — украшение не имеет права сорвать доставку
            if log:
                log(f"комментатор упал: {type(exc).__name__}: {exc}")
    ctx = {"state": state, "prev_last": prev_last, "events": events, "payload": payload,
           "now": now, "dry_run": dry_run, "enabled": enabled, "log": log}
    return batch, ctx


def deliver(batch, ctx):
    """Вторая половина цикла: отправить, записать состояние. -> batch с исходами."""
    state, now, payload = ctx["state"], ctx["now"], ctx["payload"]
    dry_run, enabled, log = ctx["dry_run"], ctx["enabled"], ctx.get("log")
    dispatch(batch, dry_run=dry_run, enabled=enabled, now=now)
    if enabled and not dry_run:
        extra = _stuck_events(batch, state, now, log=log)
        if extra:
            dispatch(extra, dry_run=dry_run, enabled=enabled, now=now)
            _mark_stuck(extra, state)
            batch += extra
        # Отбой предупреждения имеет смысл, только если само предупреждение дошло.
        watch = state.get("watch") if isinstance(state.get("watch"), dict) else None
        if watch and any(e.get("key") == watch.get("key") and e.get("delivered")
                         for e in batch):
            watch["delivered"] = True

    _feed_add(state, ctx["events"])
    state["last"] = _snapshot_keeping_undelivered(payload, ctx["prev_last"], batch, now,
                                                  held=held_sources(state),
                                                  held_core=held_core_series(state))
    state["pending"] = _requeue(batch)
    if not dry_run:
        # В dry-run состояние НЕ трогаем: иначе прогон «на посмотреть» съедает
        # переход, и настоящий прогон о нём уже не сообщит.
        save_state(state)
    return batch


def run(payload, dry_run=False, enabled=True, now=None, seed_payload=None, log=None,
        crash=None):
    """Полный цикл одним вызовом: определить переходы, повторить недоставленное,
    отправить, сохранить. Боевой прогон (run.py) зовёт половины порознь, с
    публикацией страницы между ними; этот вызов — для проверок и ручных прогонов."""
    batch, ctx = prepare(payload, dry_run=dry_run, enabled=enabled, now=now,
                         seed_payload=seed_payload, log=log, crash=crash)
    return deliver(batch, ctx)


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
    dispatch(events, dry_run=dry_run, enabled=enabled, now=now)
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
