#!/usr/bin/env python3
"""Проверка shell-обёрток на ловушку «комментарий внутри переноса строки».

Строка, оканчивающаяся на «\\», означает «команда продолжается ниже». Если
следующая строка — комментарий, bash НЕ пропускает его: он передаёт текст
комментария как АРГУМЕНТЫ команды. Так обёртка скормила `timeout` слова из
комментария, тот вышел с кодом 125 («Try 'timeout --help'»), systemd после
двух рестартов заглушил юнит — и витринный такт молча умер на живой машине.

Ошибку не ловит ни `bash -n` (синтаксис корректен), ни чтение глазами:
выглядит как обычный комментарий на своём месте.

Запуск: python ops/lint_sh.py ops/*.sh        (0 — чисто, 1 — нашлось)
"""

import glob
import sys

# Консоль Windows по умолчанию cp1252, и русский вывод роняет линтер с
# UnicodeEncodeError ещё до того, как он успеет сказать «чисто»: код возврата 1
# неотличим от «нашлись проблемы», а трейсбек читается как поломка обёрток. Ловушка
# та же, что в pipeline/run.py и ops/seed_store.py (грабля №5 в docs/DEPLOY.md), и на
# раннере Ubuntu она невидима — там локаль UTF-8, и линтер всегда зелёный.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def find_bad_continuations(text):
    """-> [(номер строки, текст)] для комментариев сразу после переноса.

    Номера строк — с единицы, как в редакторе и в сообщениях bash.
    """
    bad = []
    lines = text.split("\n")
    for i in range(1, len(lines)):
        prev = lines[i - 1].rstrip("\r")
        cur = lines[i]
        # Перенос — это ровно один «\» в конце строки. Экранированный «\\» в конце
        # переносом не является: это литеральный обратный слэш.
        if not prev.endswith("\\"):
            continue
        trailing = len(prev) - len(prev.rstrip("\\"))
        if trailing % 2 == 0:
            continue
        if cur.lstrip().startswith("#"):
            bad.append((i + 1, cur.strip()))
    return bad


def main(argv):
    patterns = argv[1:] or ["ops/*.sh"]
    files = []
    for pat in patterns:
        files.extend(sorted(glob.glob(pat)))
    if not files:
        print("нечего проверять: файлы не найдены", file=sys.stderr)
        return 1
    problems = 0
    for path in files:
        with open(path, encoding="utf-8") as fh:
            for lineno, line in find_bad_continuations(fh.read()):
                problems += 1
                print(f"{path}:{lineno}: комментарий после переноса строки уедет "
                      f"в аргументы команды -> {line}")
    if problems:
        print(f"\nнайдено проблем: {problems}", file=sys.stderr)
        return 1
    print(f"проверено файлов: {len(files)}; переносов с комментариями нет")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
