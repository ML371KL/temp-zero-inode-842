#!/usr/bin/env python3
"""Проверка, что каждый режим конвейера кто-то запускает, а каждый ряд кто-то опрашивает.

Зачем отдельная проверка. Режим, под который не создан юнит, отказывает МОЛЧА и
неотличимо от исправной работы: ряд стоит на значении с последнего ручного прогона,
а тайл при этом зелёный — свежесть считается от `fetched_at` против SLA, и «никто не
спрашивал» выглядит там ровно как «источник только что ответил». Так режимы `weekly`
и `manual` не запускались ни одним таймером с самой установки: пять рядов из тридцати
шести (cpi_weekly, ofz_auctions, cb_consensus, events_registry, dividends) не
обновлялись никогда, тайл дивидендов стоял пустым, а ветка «сюрприз к консенсусу» в
алертах была недостижима.

Источник правды здесь — ops/*.service (то, что реально запустится на машине), а НЕ
registry.MODES: список режимов в коде и без юнитов выглядит полным.

Запуск: python ops/check_schedule.py        (0 — покрыто всё, 1 — есть дыры)
"""

import glob
import os
import re
import sys

# Кириллица в выводе на Windows: консоль cp1252 роняет print с русским текстом
# (UnicodeEncodeError), и падает не логика, а диагностика — грабля №5 docs/DEPLOY.md.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pipeline.lib import registry, schedule  # noqa: E402  (после sys.path)

# Режимы, у которых юнита нет и быть не должно: bootstrap запускают руками один раз при
# установке, selftest — из установщика сразу после неё.
MANUAL_ONLY_MODES = {"bootstrap", "selftest"}

# Режимы обвязки, которые НЕ собирают данные: у них нет рядов, и требовать
# от них покрытия реестра бессмысленно. Таймер им при этом всё равно нужен —
# проверка №1 ниже за этим следит.
NON_PIPELINE_MODES = {"recalibrate"}

EXEC_START = re.compile(r"^ExecStart=.*?/moex-radar\s+(\w+)\s*$", re.M)
ON_CALENDAR = re.compile(r"^OnCalendar=(.+?)\s*$", re.M)
START_LIMIT_BURST = re.compile(r"^StartLimitBurst=(\d+)\s*$", re.M)
START_LIMIT_INTERVAL = re.compile(r"^StartLimitIntervalSec=(\d+)\s*$", re.M)


def start_limit_problems(ops_dir):
    """Ограничитель стартов не имеет права отбивать ПЛАНОВОЕ срабатывание таймера.

    systemd считает ВСЕ старты юнита, включая приходящие от таймера, — а не только
    повторы по `Restart=`. Поэтому пара (StartLimitIntervalSec, StartLimitBurst)
    обязана вмещать все плановые такты своего окна, иначе лишние отбиваются с
    «Start request repeated too quickly», и это НЕ ВИДНО НИКАК: юнит не падает,
    прогона просто не происходит, а витрина остаётся с прошлыми числами и честным
    «ok» у всех источников.

    Оплачено продом: у витринного такта стояло burst=2 на окно 900 с — верно для
    прежнего шага 15 минут и неверно для нынешних 5. За 13.08.2026 молча пропало
    73 такта из 169, и в их числе закрывающий 21:00 UTC — каждый день, потому что
    20:50 и 20:55 выбирали лимит целиком.
    """
    problems = []
    for path in sorted(glob.glob(os.path.join(ops_dir, "*.timer"))):
        base = os.path.basename(path)[: -len(".timer")]
        service = os.path.join(ops_dir, base + ".service")
        if not os.path.exists(service):
            continue
        with open(path, encoding="utf-8") as fh:
            calendar = ON_CALENDAR.findall(fh.read())
        with open(service, encoding="utf-8") as fh:
            unit = fh.read()
        burst = START_LIMIT_BURST.search(unit)
        interval = START_LIMIT_INTERVAL.search(unit)
        if not burst or not interval:
            continue
        burst, interval = int(burst.group(1)), int(interval.group(1))
        planned = schedule.max_starts_in_window(
            schedule.starts_of_day(calendar), interval / 60.0)
        if burst < planned:
            problems.append(
                f"{base}.service: StartLimitBurst={burst} при окне {interval} с, "
                f"а таймер даёт до {planned} плановых срабатываний за это окно — "
                f"лишние systemd отобьёт молча (нужно не меньше {planned})")
    return problems


def modes_with_units(ops_dir):
    """-> {режим: [имена юнитов]} по тому, что реально стоит в ExecStart."""
    found = {}
    for path in sorted(glob.glob(os.path.join(ops_dir, "*.service"))):
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        for mode in EXEC_START.findall(text):
            found.setdefault(mode, []).append(os.path.basename(path))
    return found


def main(argv=None):
    args = (argv if argv is not None else sys.argv)[1:]
    ops_dir = args[0] if args else os.path.join(ROOT, "ops")
    scheduled = modes_with_units(ops_dir)
    problems = []

    # 1. Юнит без таймера — тот же молчаливый отказ: файл есть, запускать некому.
    for mode, units in sorted(scheduled.items()):
        for unit in units:
            timer = os.path.join(ops_dir, unit[: -len(".service")] + ".timer")
            if not os.path.exists(timer):
                problems.append(f"{unit}: режим {mode} есть, а таймера рядом нет")

    # 2. Каждый режим из MODES обязан кем-то запускаться.
    for mode in sorted(registry.MODES):
        if mode in MANUAL_ONLY_MODES or mode in scheduled:
            continue
        rows = ", ".join(sorted(registry.MODES[mode])) or "—"
        problems.append(f"режим {mode} не стоит ни в одном ops/*.service; "
                        f"его ряды не опрашивает никто: {rows}")

    # 3. И обратная сторона: ряд, не попавший ни в один ЗАПУСКАЕМЫЙ режим. Проверка
    # именно по юнитам, а не по MODES: ряд может быть перечислен в режиме, который
    # никто не запускает, — это и есть исходный дефект.
    covered = set()
    for mode in scheduled:
        if mode in NON_PIPELINE_MODES:
            continue
        covered |= {sid for sid, _ in registry.series_for_mode(mode)}
    orphans = sorted(set(registry.SERIES) - covered)
    if orphans:
        problems.append("рядов без расписания: %d — %s" % (len(orphans), ", ".join(orphans)))

    # 4. Ограничитель стартов против плотности таймера.
    problems += start_limit_problems(ops_dir)

    print(f"режимы с юнитами: {', '.join(sorted(scheduled)) or '—'}")
    print(f"рядов покрыто: {len(covered)} из {len(registry.SERIES)}")
    if problems:
        print("", file=sys.stderr)
        for line in problems:
            print(f"ОШИБКА: {line}", file=sys.stderr)
        return 1
    print("расписание покрывает всё")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
