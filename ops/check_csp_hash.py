#!/usr/bin/env python3
"""Сверка хэшей инлайн-скриптов web/index.html с теми, что разрешает CSP.

Зачем. Строгий CSP (`functions/_middleware.js`) пускает инлайн-скрипт только по хэшу
его содержимого. Правка того скрипта на один пробел делает хэш недействительным, и
браузер молча перестаёт его исполнять: страница работает, но тема ставится уже не
до отрисовки, а вместе с app.js — ночью это заметное моргание белым, ради избавления
от которого скрипт и написан. Ошибка не видна ни в тестах, ни в логах сервера, только
в консоли браузера, куда никто не смотрит. Поэтому — проверка в CI.

Перевод строк нормализуется в LF намеренно: в репозитории файлы лежат с LF
(.gitattributes), а рабочая копия на Windows бывает с CRLF — хэш от CRLF-версии не
совпадёт с тем, что посчитает браузер на выложенном сайте. Считать надо ровно те
байты, которые выложит раннер Linux.

Запуск: python ops/check_csp_hash.py        (0 — сходится, 1 — нет)
"""

import base64
import hashlib
import os
import re
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "web", "index.html")
MIDDLEWARE = os.path.join(ROOT, "functions", "_middleware.js")

INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)
DECLARED_HASH = re.compile(r"THEME_SCRIPT_HASH\s*=\s*\"(sha256-[A-Za-z0-9+/=]+)\"")


def read_lf(path):
    with open(path, "rb") as fh:
        return fh.read().replace(b"\r\n", b"\n").decode("utf-8")


def sha256_csp(text):
    return "sha256-" + base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode()


def main():
    page = read_lf(PAGE)
    scripts = INLINE_SCRIPT.findall(page)
    if len(scripts) != 1:
        print(f"ОШИБКА: в web/index.html инлайн-скриптов {len(scripts)}, а CSP знает про один. "
              f"Добавьте хэши остальных в script-src или вынесите их в отдельный файл.",
              file=sys.stderr)
        return 1

    actual = sha256_csp(scripts[0])
    declared = DECLARED_HASH.search(read_lf(MIDDLEWARE))
    if not declared:
        print("ОШИБКА: в functions/_middleware.js не нашлось THEME_SCRIPT_HASH — "
              "CSP больше не про хэш? Проверьте руками.", file=sys.stderr)
        return 1

    if declared.group(1) != actual:
        print(f"ОШИБКА: инлайн-скрипт темы изменился, а CSP разрешает старый хэш.\n"
              f"  в functions/_middleware.js: {declared.group(1)}\n"
              f"  по содержимому web/index.html: {actual}\n"
              f"Замените строку на:\n"
              f'const THEME_SCRIPT_HASH = "{actual}";', file=sys.stderr)
        return 1

    print(f"хэш инлайн-скрипта сходится: {actual}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
