"""Бюджет времени на сбор данных: публикация важнее последнего ряда.

Контракт прогона — «панель обязана обновляться и с половиной источников»
(pipeline/run.py). До 18.08.2026 он выполнялся только при БЫСТРЫХ отказах:
источник, который не отвечает отказом, а ВИСИТ до сокет-таймаута, стоит ~93 с на
ряд (30 с × 3 попытки + бэкофф, lib/http.py), fetch_all строго последователен, и
дедлайн юнита (300 с интрадей, 900 с суточный) пробивался ДО публикации — timeout
убивал прогон, data.json не обновлялся вовсе, свежие ряды с других источников
даже не запрашивались. Blackhole у одного ISS замораживал панель целиком.

Правило: обвязка выдаёт python-у бюджет чуть меньше своего дедлайна
(RADAR_FETCH_BUDGET_S в ops/moex-radar.sh); когда он исчерпан, оставшиеся ряды
пропускаются с пометкой в журнале, а прогон идёт дальше — считать и публиковать
по кэшу. Пропуск громкий (status=skip в отчёте фетча), но это штатная деградация,
а не отказ.

Модульное состояние, а не параметр через все сигнатуры: бюджет один на прогон,
а спрашивают его и fetch_all, и длинные фетчеры изнутри (breadth — 45
последовательных историй, каждая может висеть свои 93 с).
"""

import os
import time

__all__ = ["arm", "arm_from_env", "disarm", "exhausted", "remaining"]

_deadline = None


def arm(seconds):
    """Взвести бюджет: столько секунд от текущего момента."""
    global _deadline
    _deadline = time.monotonic() + max(0.0, float(seconds))


def arm_from_env():
    """Бюджет из RADAR_FETCH_BUDGET_S; мусор и пустота = бюджета нет (не бросает)."""
    raw = (os.environ.get("RADAR_FETCH_BUDGET_S") or "").strip()
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    arm(value)
    return True


def disarm():
    global _deadline
    _deadline = None


def exhausted():
    return _deadline is not None and time.monotonic() >= _deadline


def remaining():
    """Секунд до дедлайна; None — бюджет не взведён (значит, не ограничены)."""
    return None if _deadline is None else max(0.0, _deadline - time.monotonic())
