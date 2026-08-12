#!/usr/bin/env python3
"""Квартальная реколибровка (docs/ARCHITECTURE.md §7): пересчёт опор панели.

ЗАЧЕМ. Все опорные числа заморожены в `pipeline/lib/constants.py` и в
`validation/data/*.csv`: статистика восьми ячеек, условные IC второго слоя,
эталон walk-forward. История при этом растёт каждый месяц, а числа — нет. Дрейф
происходит МОЛЧА: 12.08.2026 внеплановый аудит нашёл среднее токсичной ячейки
−3,14% против замороженных −2,94% и `hit` 0,54, который при n=25 арифметически
недостижим. Этот скрипт делает такую сверку регулярной и воспроизводимой.

ЧЕГО ОН НЕ ДЕЛАЕТ. Не меняет ни одного числа и не предлагает состав ядра.
Автоматический пересмотр состава по свежей результативности — доказанный провал
(REGIME.md §4: адаптивный отбор дал OOS IC −0,018 против +0,227 у фиксированного
композита), поэтому решение остаётся человеку, а отчёт — только диффом.

ГДЕ ЗАПУСКАТЬ. На VPS, где стор уже есть:
    sudo -u dash bash -c 'set -a; . /usr/local/etc/moex-radar/env; set +a; \
        cd /srv/dash/repo-842 && PYTHONPATH=. python3 ops/recalibrate.py'
На пустом раннере GitHub Actions стор восстанавливается из зеркала R2 по
манифесту raw/_index.json (его пишет publish._mirror_index).
"""

import argparse
import csv
import hashlib
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from pipeline.compute import core, panel as panel_mod, states as states_mod  # noqa: E402
from pipeline.lib import calc, constants, r2, store  # noqa: E402

REFERENCE = ROOT / "validation" / "data" / "walkforward_results.csv"
OOS_START = "2010-01-01"          # окно walk-forward исследования (REGIME.md §4)

# Порог дрейфа ячейки — ОТНОСИТЕЛЬНЫЙ, в долях её собственной ошибки среднего.
#
# Первая редакция ставила абсолютные 0,15 п.п. — и это был детектор, обречённый
# кричать вечно: средняя токсичной ячейки известна с точностью ±2,9 п.п. (n=24,
# разброс месяцев от −30% до +18%), а у самой точной ячейки ошибка 0,6 п.п. На таком
# фоне «расхождение» в 0,20 п.п. — это 0,07 стандартной ошибки, то есть числа
# −2,85, −2,94 и −3,14 статистически НЕРАЗЛИЧИМЫ.
#
# Половина ошибки среднего масштабируется правильно сама: у ячейки с n=110 порог
# выходит 0,30 п.п., у ячейки с n=8 — около 1,1 п.п. Плюс два жёстких триггера,
# которые от размера выборки не зависят: смена знака и переход через −1,5%, по
# которому фронт красит ячейку в критический тон (web/app.js).
DRIFT_SE_RATIO = 0.5
TONE_CRIT_PCT = -1.5


# --------------------------------------------------------------- восстановление

def restore_from_r2(target):
    """Скачать зеркало raw/ в пустой стор. -> (сколько рядов, чего не хватило)."""
    if not r2.configured():
        raise SystemExit("стор пуст, а R2 не сконфигурирован — нечем восстанавливать")
    index = r2.get("raw/_index.json")
    if not index:
        raise SystemExit(
            "в бакете нет raw/_index.json — манифест пишет publish._mirror_index, "
            "он появится после первого прогона конвейера новой версии. "
            "До тех пор запускайте реколибровку на VPS, где стор есть локально.")
    ids = json.loads(index.decode("utf-8")).get("series") or []
    target.mkdir(parents=True, exist_ok=True)
    missing = []
    for sid in ids:
        body = r2.get(f"raw/{sid}.json")
        if body is None:
            missing.append(sid)
            continue
        (target / f"{sid}.json").write_bytes(body)
    return len(ids) - len(missing), missing


# ------------------------------------------------------------------- статистика

def rank(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    out = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            out[order[k]] = (i + j) / 2.0 + 1
        i = j + 1
    return out


def corr(xs, ys):
    n = len(xs)
    if n < 8:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if not sx or not sy:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


def spearman(xs, ys):
    return corr(rank(xs), rank(ys))


def ci(ic, n):
    """Грубый 95% интервал рангового IC: без него «+0,05» читается как результат."""
    if ic is None or n < 4:
        return None, None
    se = 1 / math.sqrt(n - 1)
    return ic - 1.96 * se, ic + 1.96 * se


def pc(x):
    return (math.exp(x) - 1) * 100


def ann_sharpe_dd(rets):
    n = len(rets)
    m = sum(rets) / n
    sd = math.sqrt(sum((r - m) ** 2 for r in rets) / (n - 1)) if n > 1 else 0
    peak = cum = worst = 0.0
    for r in rets:
        cum += r
        peak = max(peak, cum)
        worst = min(worst, cum - peak)
    return pc(m * 12), (m / sd * math.sqrt(12) if sd else 0), pc(worst)


# ---------------------------------------------------------------------- разделы

def section_invariant(labels, comp, out):
    ref = {}
    if REFERENCE.exists():
        with open(REFERENCE, encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                try:
                    ref[(row[""] or "")[:7]] = float(row["M1_fixed"])
                except (TypeError, ValueError, KeyError):
                    pass
    mine = {labels[i][:7]: comp[i] for i in range(len(labels)) if comp[i] is not None}
    both = sorted(set(ref) & set(mine))
    worst = max((abs(mine[m] - ref[m]) for m in both), default=None)
    out.append("## 1. Инвариант композита")
    out.append("")
    out.append(f"Сопоставлено месяцев: **{len(both)}** из {len(ref)} эталонных "
               f"(`validation/data/walkforward_results.csv`).")
    if worst is None:
        out.append("")
        out.append("⚠️ Пересечения с эталоном нет — сверять не с чем.")
        return False
    ok = worst < 1e-9
    out.append(f"Максимальное расхождение: **{worst:.12f}** — "
               f"{'совпадает побитово' if ok else '⚠️ РАСХОЖДЕНИЕ'}.")
    out.append("")
    if not ok:
        out.append("Композит панели разошёлся с числом, на котором считалась вся "
                   "валидация. Это не повод менять состав — это повод найти, какой "
                   "ряд переписали, и понять, стало ли лучше.")
        out.append("")
    return ok


def section_health(health, out):
    out.append("## 2. Здоровье ядра")
    out.append("")
    lo, hi = ci(health.get("ic_24m"), health.get("n") or 0)
    span = f"[{lo:+.2f}; {hi:+.2f}]" if lo is not None else "—"
    out.append(f"| величина | значение |")
    out.append(f"|---|---|")
    out.append(f"| скользящий IC за {health.get('n')} мес | **{health.get('ic_24m'):+.3f}** |")
    out.append(f"| 95% интервал | {span} |")
    out.append(f"| статус | **{health.get('status')}** |")
    out.append(f"| месяцев ниже нуля подряд | {health.get('below_zero_months')} "
               f"(порог регламента {health.get('review_months')}) |")
    out.append(f"| порог пересмотра достигнут | "
               f"{'ДА' if health.get('review_due') else 'нет'} |")
    out.append("")
    if lo is not None and lo <= 0 <= hi:
        out.append("Интервал накрывает ноль: на этом окне модель статистически "
                   "неотличима от монетки. Это не поломка и не разрешение — это "
                   "отсутствие информации.")
        out.append("")
    if health.get("review_due"):
        out.append("**Порог здоровья из §7 достигнут.** Вторая половина условия — "
                   "механизм у кандидата — проверяется человеком; без неё состав "
                   "не меняют.")
        out.append("")


def cell_flagged(mean, ref_mean, se_pp):
    """Новость ли расхождение ячейки. -> (флаг, причина).

    Три вопроса в одном, и они разные по природе. «Сместилась ли оценка» — вопрос
    статистический, и мерить его надо в единицах ошибки самой ячейки. «Сменился ли
    знак» и «перешла ли она порог, по которому фронт красит ячейку в критический
    тон» — вопросы редакционные: они меняют то, ЧТО читатель видит, и от размера
    выборки не зависят вовсе.
    """
    if ref_mean is None or mean is None:
        return False, None
    if (mean > 0) != (ref_mean > 0):
        return True, "смена знака"
    if (mean <= TONE_CRIT_PCT) != (ref_mean <= TONE_CRIT_PCT):
        return True, "переход через порог тона"
    if not se_pp:
        # n=1: ошибку среднего оценить нечем, но и «дрейфа» на одном наблюдении нет.
        return False, None
    ratio = abs(mean - ref_mean) / se_pp
    return (ratio > DRIFT_SE_RATIO), (f"{ratio:.1f} ст.ош." if ratio > DRIFT_SE_RATIO else None)


def section_cells(labels, fwd, cells, out):
    out.append("## 3. Статистика ячеек против `constants.CELL_STATS`")
    out.append("")
    out.append("| ячейка | n сейчас / в коде | средн сейчас / в коде | Δ в ст.ош. | медиана | худший |")
    out.append("|---|---|---|---|---|---|")
    BITS = {"bull": 1, "bear": 0, "calm": 0, "stress": 1, "ok": 0}
    rows = {}
    for i in range(len(labels) - 2):
        code = cells.get(labels[i])
        if code and fwd[i] is not None:
            rows.setdefault(code, []).append(fwd[i])
    drift = []
    for code in sorted(rows, key=lambda c: -len(rows[c])):
        v = sorted(rows[code])
        n = len(v)
        raw_mean = sum(v) / n
        mean = pc(raw_mean)
        med = pc(v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2)
        sd = math.sqrt(sum((x - raw_mean) ** 2 for x in v) / (n - 1)) if n > 1 else 0.0
        se_pp = pc(sd / math.sqrt(n)) if n > 1 else 0.0
        p = code.split("|")
        ref = constants.CELL_STATS.get((BITS[p[0]], BITS[p[1]], BITS[p[2]])) or {}
        ref_mean = ref.get("mean_fwd1m_pct")
        ratio = abs(mean - (ref_mean or 0)) / se_pp if se_pp else 0.0
        flagged, why = cell_flagged(mean, ref_mean, se_pp)
        mark = " ⚠️" if flagged else ""
        if flagged:
            # Имя находки — сама ячейка: её числа дрейфуют, а проблема та же.
            drift.append((f"cell:{code}",
                          f"{code}: {mean:+.2f}% против {ref_mean:+.2f}% ({why})"))
        out.append(f"| {code} | {n} / {ref.get('n')} | {mean:+.2f}% / "
                   f"{ref_mean:+.2f}%{mark} | {ratio:.2f} | {med:+.2f}% | {pc(v[0]):+.1f}% |")
    out.append("")
    out.append(f"Порог тревоги — {DRIFT_SE_RATIO} собственной ошибки среднего ячейки, а не "
               f"абсолютные проценты: у ячейки с n=24 эта ошибка около 2,9 п.п., и "
               f"расхождение в 0,2 п.п. там означает 0,07 ошибки — то есть одно и то же "
               f"число, а не дрейф. Отдельно и независимо от выборки ловятся смена знака и "
               f"переход через {TONE_CRIT_PCT}% (по нему фронт красит ячейку).")
    out.append("")
    if drift:
        out.append("Разошлись: " + "; ".join(text for _, text in drift) + ".")
        out.append("")
        out.append("Числа в коде менять только целиком и осознанно: они калибровались "
                   "разом, и правка одной строки рассогласует таблицу с руководством "
                   "и с правилами дня.")
        out.append("")
    return drift


def section_strategies(labels, comp, fwd, cells, out):
    out.append("## 4. Слои: что даёт каждый")
    out.append("")
    out.append("| выборка | вариант | год.дох | Шарп | макс.просадка |")
    out.append("|---|---|---|---|---|")
    usable_all = [i for i in range(len(labels) - 2)
                  if fwd[i] is not None and comp[i] is not None and cells.get(labels[i])]
    for start, name in ((labels[0], "вся история"), (OOS_START, "с 2010")):
        idxs = [i for i in usable_all if labels[i] >= start]
        if len(idxs) < 36:
            continue
        variants = (
            ("buy & hold", lambda i: True),
            ("слой 1 (композит>0)", lambda i: comp[i] > 0),
            ("слой 2 (ворота)", lambda i: cells.get(labels[i]) != "bear|stress|stress"),
            ("оба слоя", lambda i: comp[i] > 0 and cells.get(labels[i]) != "bear|stress|stress"),
        )
        for vname, rule in variants:
            ann, sh, dd = ann_sharpe_dd([fwd[i] if rule(i) else 0.0 for i in idxs])
            out.append(f"| {name} (n={len(idxs)}) | {vname} | {ann:+.1f}% | {sh:.2f} | {dd:.1f}% |")
    out.append("")


BITS_OF_CODE = {"bull": ("trend", 1), "bear": ("trend", 0),
                "calm": ("vol", 0), "stress": ("vol", 1)}


def bits_of(code):
    """Код ячейки «bear|stress|stress» -> {'trend':0,'vol':1,'bond':1}."""
    parts = (code or "").split("|")
    if len(parts) != 3:
        return {}
    return {"trend": 1 if parts[0] == "bull" else 0,
            "vol": 1 if parts[1] == "stress" else 0,
            "bond": 1 if parts[2] == "stress" else 0}


def rate_phase_by_day(panel):
    """Фаза ставки на каждый день: знак последнего изменения ключевой (REGIME §2).

    Ворота switch_spread стоят именно на ней, и из кода ячейки её не достать —
    без этого сигнал пришлось бы считать по всей выборке и получить другое число.
    """
    kr = panel["cols"].get("key_rate") or []
    phase, prev, cur = {}, None, 0
    for i, day in enumerate(panel["dates"]):
        v = kr[i] if i < len(kr) else None
        if v is not None:
            if prev is not None and v != prev:
                cur = 1 if v > prev else -1
            prev = v
        phase[day] = cur
    return phase


def signal_on(sig, code, phase):
    """Включён ли сигнал в этой ячейке — по тем же `when`/`alt_when`, что и в проде."""
    bits = bits_of(code)
    if not bits:
        return False

    def match(spec):
        if not spec:
            return False
        for key, want in spec.items():
            got = phase if key == "rate_phase" else bits.get(key)
            if got != want:
                return False
        return True

    return match(sig.get("when")) or match(sig.get("alt_when"))


def gate_text(sig):
    words = {"trend": {0: "медведь", 1: "бык"}, "vol": {0: "спокойно", 1: "вола-стресс"},
             "bond": {0: "ОФЗ спокойны", 1: "ОФЗ в стрессе"},
             "rate_phase": {-1: "смягчение", 1: "ужесточение", 0: "пауза"}}
    out = []
    for spec in (sig.get("when"), sig.get("alt_when")):
        if not spec:
            continue
        out.append(" и ".join(words.get(k, {}).get(v, f"{k}={v}") for k, v in spec.items()))
    return " либо ".join(out) or "всегда"


def section_second_layer(labels, fwd, cells, panel, out):
    out.append("## 5. Второй слой: условные IC против того, что записано в коде")
    out.append("")
    out.append("| сигнал | ворота | n | IC сейчас | заявлено в `constants` |")
    out.append("|---|---|---|---|---|")
    idx_of = {d: i for i, d in enumerate(panel["dates"])}
    phases = rate_phase_by_day(panel)
    for sig in constants.SECOND_LAYER:
        col = sig.get("id")
        series = panel["cols"].get(col)
        if not series:
            out.append(f"| {sig.get('label', col)} | {gate_text(sig)} | — | "
                       f"нет колонки в панели | {sig.get('why', '')} |")
            continue
        xs, ys = [], []
        for i in range(len(labels) - 2):
            j = idx_of.get(labels[i])
            if j is None or fwd[i] is None or series[j] is None:
                continue
            if not signal_on(sig, cells.get(labels[i]), phases.get(labels[i], 0)):
                continue
            # Знак ноги — тот же, что применяет панель: иначе таблица покажет
            # «перевёрнутый» IC у контрарианских сигналов и это прочтут как поломку.
            xs.append(series[j] * sig.get("sign", 1))
            ys.append(fwd[i])
        ic = spearman(xs, ys) if len(xs) >= 8 else None
        shown = f"{ic:+.3f}" if ic is not None else "мало данных"
        out.append(f"| {sig.get('label', col)} | {gate_text(sig)} | {len(xs)} | "
                   f"{shown} | {sig.get('why', '')} |")
    out.append("")
    out.append("IC в колонке «сейчас» посчитан со ЗНАКОМ, который применяет панель: "
               "у контрарианских ног (`sign: −1`) положительное число означает, что "
               "сигнал работает в заявленную сторону.")
    out.append("")


# ------------------------------------------------------------------------ main

QUARTER_MONTHS = (1, 4, 7, 10)


def notify(verdicts, health, mode, out_path, now=None):
    """Одна строка в ops-канал. Молчим, когда сказать нечего.

    ПОЧЕМУ НЕ КАЖДЫЙ РАЗ: отчёт «расхождений нет» ежемесячно за год приучает не
    открывать сообщения от панели вовсе — и вместе с ними мимо проходит настоящее
    расхождение. Поэтому тревога уходит СРАЗУ, а рутинное подтверждение — только в
    квартальные месяцы, чтобы раз в квартал было видно: проверка жива и прошла.

    ПОЧЕМУ КЛЮЧ ИЗ ТОЖДЕСТВА НАХОДКИ, А НЕ ИЗ ЕЁ ТЕКСТА И НЕ ИЗ ДАТЫ. Находка живёт
    МЕСЯЦАМИ: пока константы не пересчитаны целиком, расхождение ячейки верно и в
    сентябре, и в октябре. Ключ по дате слал бы его заново каждый месяц — «состояние
    вместо перехода», против чего построен весь остальной алертинг панели.
    Ключ по ТЕКСТУ не спасает: в тексте живут числа, и они дрейфуют. У порога §7 в
    тексте вообще счётчик («ниже нуля 7 мес подряд»), который растёт ежемесячно по
    построению, — сообщение приходило бы каждый месяц до самой реколибровки.
    Поэтому у каждой находки есть неизменное имя (`cell:bear|stress|stress`,
    `invariant`, `health_review`), и хеш считается по НАБОРУ ИМЁН: пока состав
    проблем тот же — тишина, появилась или ушла проблема — сообщение.
    """
    if mode == "never":
        return "выключено"
    from pipeline.lib import telegram

    now = now or datetime.now(timezone.utc)
    findings = [(i, t) for i, t in verdicts]
    if health.get("review_due"):
        findings.append(("health_review",
                         f"здоровье ниже нуля {health.get('below_zero_months')} мес подряд — "
                         f"порог §7 на пересмотр состава достигнут"))
    quarter = now.month in QUARTER_MONTHS
    if mode == "auto" and not findings and not quarter:
        return "молчу: расхождений нет, месяц не квартальный"

    head = "Реколибровка: есть расхождения" if findings else "Реколибровка: расхождений нет"
    lines = [head] + [f"• {text}" for _, text in findings]
    if out_path:
        lines.append(f"Полный отчёт: {out_path}")

    if findings:
        idents = "\n".join(sorted(ident for ident, _ in findings))
        digest = hashlib.sha256(idents.encode("utf-8")).hexdigest()[:16]
        key = f"recalibrate:{digest}"
    else:
        # Подтверждение «всё чисто» — не находка, а сердцебиение: ровно одно на квартал.
        key = f"recalibrate-ok:{now.year}Q{(now.month - 1) // 3 + 1}"
    outcome = telegram.deliver(key, "\n".join(lines), channel="ops")
    return f"telegram({outcome})"


def main():
    ap = argparse.ArgumentParser(description="Квартальная реколибровка панели")
    ap.add_argument("--out", help="куда положить markdown-отчёт")
    ap.add_argument("--restore", action="store_true",
                    help="скачать стор из зеркала R2 (для пустого раннера)")
    ap.add_argument("--notify", choices=("auto", "always", "never"), default="never",
                    help="строка вердикта в ops-канал: auto — только когда есть что сказать")
    args = ap.parse_args()

    raw = Path(store.raw_dir())
    have = len(list(raw.glob("*.json"))) if raw.exists() else 0
    restored = None
    if args.restore or have == 0:
        restored, missing = restore_from_r2(raw)
        if missing:
            print(f"ПРЕДУПРЕЖДЕНИЕ: в зеркале не нашлось {len(missing)} рядов: "
                  f"{', '.join(missing[:8])}", file=sys.stderr)
        have = len(list(raw.glob("*.json")))

    panel = panel_mod.build_panel(store)
    mf = core.monthly_frame(panel)
    labels, comp, fwd = mf["dates"], mf["composite"], mf["fwd1m"]
    cells = dict(states_mod.compute_states(panel).get("series") or [])
    health = core.compute_core(panel)["health"]

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = [f"# Реколибровка панели MOEX Radar — {now}", "",
           f"Стор: {have} рядов" + (f" (восстановлено из R2: {restored})" if restored else "")
           + f"; панель {panel['dates'][0]} … {panel['dates'][-1]}; "
             f"месячных меток {len(labels)}.", "",
           "Отчёт ничего не меняет. Он показывает, где замороженные числа разошлись "
           "с историей, и оставляет решение человеку: автоматический пересмотр состава "
           "по свежей результативности — доказанный провал (REGIME.md §4).", ""]

    invariant_ok = section_invariant(labels, comp, out)
    section_health(health, out)
    drift = section_cells(labels, fwd, cells, out)
    section_strategies(labels, comp, fwd, cells, out)
    section_second_layer(labels, fwd, cells, panel, out)

    out.append("## Что делать с этим отчётом")
    out.append("")
    if not invariant_ok:
        out.append("1. **Инвариант разошёлся** — разобраться в первую очередь: "
                   "композит больше не тот, на котором считалась валидация.")
    if drift:
        out.append("1. Числа ячеек разошлись — правку делать целиком, вместе с "
                   "`web/guide.html` (его сверяет `tests/test_guide.py`).")
    if health.get("review_due"):
        out.append("1. **Достигнут порог §7** — искать кандидата с механизмом; "
                   "трейлинг-IC для отбора не использовать.")
    if invariant_ok and not drift and not health.get("review_due"):
        out.append("Расхождений выше порогов нет. Состав ядра не трогать.")
    out.append("")

    text = "\n".join(out)
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"отчёт: {path}")
    else:
        print(text)

    verdicts = list(drift)
    if not invariant_ok:
        verdicts.insert(0, ("invariant", "композит разошёлся с эталоном исследования"))
    print("уведомление:", notify(verdicts, health, args.notify, args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
