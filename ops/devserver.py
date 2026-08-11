#!/usr/bin/env python3
"""Локальный предпросмотр панели: статика из web/ + /data/* из .state/out/.

Повторяет контракт функции Cloudflare Pages (functions/data/[[path]].js): те же
пути, тот же 503 «ещё не публиковалось», тот же Last-Modified. Иначе локально
видишь одну панель, а в проде другую — и разница вылезает уже на людях.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
OUT = Path(os.environ.get("STATE_DIR", ROOT / ".state")) / "out"
ALLOWED = {"data.json", "history/daily.json", "history/monitors.json"}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(WEB), **kw)

    def _send_json(self, code, obj, extra=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("cache-control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0].lstrip("/")
        if path.startswith("data/"):
            key = path[len("data/"):]
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
