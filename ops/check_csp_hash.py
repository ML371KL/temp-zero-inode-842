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
WEB = os.path.join(ROOT, "web")
MIDDLEWARE = os.path.join(ROOT, "functions", "_middleware.js")

INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)
DECLARED_HASH = re.compile(r"\"(sha256-[A-Za-z0-9+/=]+)\"")


def read_lf(path):
    with open(path, "rb") as fh:
        return fh.read().replace(b"\r\n", b"\n").decode("utf-8")


def sha256_csp(text):
    return "sha256-" + base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode()


def main():
    # ВСЕ страницы, а не только index.html: middleware ставит один CSP на каждый
    # ответ, значит хэш каждого инлайн-скрипта каждой страницы обязан быть в
    # списке. Урок 18.08.2026: страж читал только index.html, и тема руководства
    # стояла заблокированной политикой в проде при зелёном CI.
    actual = {}  # хэш -> откуда
    for name in sorted(os.listdir(WEB)):
        if not name.endswith(".html"):
            continue
        for script in INLINE_SCRIPT.findall(read_lf(os.path.join(WEB, name))):
            actual[sha256_csp(script)] = f"web/{name}"
    if not actual:
        print("ОШИБКА: в web/*.html не нашлось ни одного инлайн-скрипта — "
              "стражу нечего сверять, проверьте регэксп.", file=sys.stderr)
        return 1

    block = re.search(r"THEME_SCRIPT_HASHES\s*=\s*\[(.*?)\]", read_lf(MIDDLEWARE), re.S)
    if not block:
        print("ОШИБКА: в functions/_middleware.js не нашлось THEME_SCRIPT_HASHES — "
              "CSP больше не про хэши? Проверьте руками.", file=sys.stderr)
        return 1
    declared = set(DECLARED_HASH.findall(block.group(1)))

    missing = {h: src for h, src in actual.items() if h not in declared}
    stale = declared - set(actual)
    if missing or stale:
        lines = ["ОШИБКА: хэши инлайн-скриптов разошлись с CSP."]
        for h, src in sorted(missing.items()):
            lines.append(f"  скрипт из {src} не разрешён: {h}")
        for h in sorted(stale):
            lines.append(f"  разрешён хэш, которому не соответствует ни один скрипт: {h}")
        lines.append("Список THEME_SCRIPT_HASHES должен быть ровно таким:")
        for h, src in sorted(actual.items(), key=lambda kv: kv[1]):
            lines.append(f'  "{h}", // {src}')
        print("\n".join(lines), file=sys.stderr)
        return 1

    print("хэши инлайн-скриптов сходятся: "
          + "; ".join(f"{src} {h}"
                      for h, src in sorted(actual.items(), key=lambda kv: kv[1])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
