#!/usr/bin/env python3
"""Локальный предпросмотр панели: статика из web/ + /data/* из .state/out/.

Повторяет контракт функции Cloudflare Pages (functions/data/[[path]].js): те же
пути, тот же 503 «ещё не публиковалось», тот же Last-Modified. Иначе локально
видишь одну панель, а в проде другую — и разница вылезает уже на людях.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
OUT = Path(os.environ.get("STATE_DIR", ROOT / ".state")) / "out"
ALLOWED = {"data.json", "history/daily.json", "history/monitors.json"}
# Тот же канон сегмента, что и в functions/_middleware.js: ASCII-имя объекта, ни
# пустых сегментов, ни «..», ни процент-кодирования.
SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def is_canonical_data_path(path):
    if path in ("/data", "/data/"):
        return True
    return all(SEGMENT.match(seg) for seg in path[len("/data/"):].split("/"))


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(WEB), **kw)

    def _send_json(self, code, obj, extra=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("cache-control", "no-store")
        # Как в проде (functions/data/[[path]].js). CSP здесь сознательно НЕТ: он
        # разрешает инлайн-скрипт темы по хэшу, а рабочая копия на Windows бывает с
        # CRLF — хэш от неё не совпал бы с прод-версией, и предпросмотр ругался бы на
        # исправный код. Сходимость хэша проверяет ops/check_csp_hash.py в CI.
        self.send_header("x-content-type-options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        # Раньше здесь стоял lstrip("/"), и «//data/data.json» локально отдавал JSON,
        # а на краю Cloudflare — вёрстку главной страницы с кодом 200. Предпросмотр,
        # который ведёт себя ЛУЧШЕ прода, хуже, чем никакой: он ровно про ту разницу,
        # ради которой этот файл и написан. Ведущий слеш снимаем ровно один, а
        # неканонический путь под /data/ получает такой же JSON-404, как в проде
        # (functions/_middleware.js).
        # Путь берём из строки запроса, а не из self.path: http.server сам схлопывает
        # ведущие слеши («//data/data.json» -> «/data/data.json»), а край Cloudflare —
        # нет, и именно эта форма получается при склейке базы с конечным слешем и ключа
        # с ведущим. Схлопнув её молча, предпросмотр показал бы данные там, где прод
        # отдаёт не данные.
        raw = self.requestline.split(" ")[1] if " " in self.requestline else self.path
        path = raw.split("?", 1)[0]
        collapsed = re.sub(r"/{2,}", "/", path)
        if collapsed == "/data" or collapsed.startswith("/data/"):
            if collapsed != path or not is_canonical_data_path(path):
                return self._send_json(404, {
                    "error": "no such data object",
                    "requested": path,
                    "hint": "путь под /data/ должен быть каноническим: без пустых "
                            "сегментов, «..» и процент-кодирования"})
            key = path[len("/data/"):] if path != "/data" else ""
            if key not in ALLOWED:
                return self._send_json(404, {"error": f"unknown data path: {key}"})
            f = OUT / key
            if not f.exists():
                return self._send_json(503, {
                    "error": "snapshot has not been published yet",
                    "hint": "запусти: python pipeline/run.py --mode daily --dry-run"})
            body = f.read_bytes()
            mtime = datetime.fromtimestamp(f.stat().st_mtime, timezone.utc)
            self.send_response(200)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("cache-control", "no-store")
            self.send_header("last-modified", mtime.strftime("%a, %d %b %Y %H:%M:%S GMT"))
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            return
        return super().do_GET()

    def do_HEAD(self):  # noqa: N802
        return self.do_GET()

    def log_message(self, fmt, *args):
        if "/data/" in (args[0] if args else ""):
            super().log_message(fmt, *args)


if __name__ == "__main__":
    # Та же защита, что в run.py: консоль Windows по умолчанию cp1252, а сообщения
    # у нас русские — иначе сервер падает на собственном приветствии.
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8842)
    args = ap.parse_args()
    print(f"панель: http://127.0.0.1:{args.port}/  (данные из {OUT})")
    HTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
