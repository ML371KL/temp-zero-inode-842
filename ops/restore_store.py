#!/usr/bin/env python3
"""Восстановить стор из зеркала raw/ в R2 — шаг фолбэк-писателя перед прогоном.

Зачем отдельный шаг, а не логика внутри run.py: восстановление уместно РОВНО там,
где стор пуст по построению (раннер GHA с STATE_DIR во временном каталоге). На VPS
стор локальный и живой — тихое «дочитывание» из бакета там маскировало бы порчу
локальных файлов свежей копией и ломало бы расследования.

Код возврата ненулевой в двух случаях, и оба — «фолбэку нельзя публиковать»:
  * зеркала нет вовсе (нет манифеста / R2 не сконфигурирован);
  * не восстановился ряд ядра или обязательный ряд — модель без него ДРУГАЯ
    (композит из двух ног), и публиковать её значит скормить читателю ложный
    «разворот ядра». Старая витрина честнее неверной свежей.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):  # pragma: no cover — не у всех потоков есть
        pass

from pipeline.lib import mirror, store  # noqa: E402


def main():
    raw = Path(store.raw_dir())
    try:
        restored, missing = mirror.restore_from_r2(raw)
    except mirror.RestoreError as exc:
        print(f"восстановление невозможно: {exc}", file=sys.stderr)
        return 1
    if missing:
        print(f"в зеркале не нашлось {len(missing)} рядов: {', '.join(missing[:8])}",
              file=sys.stderr)
    critical = mirror.missing_critical(raw)
    if critical:
        print(f"не восстановились критичные ряды ({', '.join(critical)}) — "
              f"публиковать усечённую модель нельзя", file=sys.stderr)
        return 1
    print(f"восстановлено рядов: {restored}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
