"""Local web server for the configurator UI (standard library only)."""
from __future__ import annotations

import json
import threading
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from .checker import ConfigurationChecker
from .explorer import explore
from .extractor import ExtractionError

WEB_DIR = Path(__file__).resolve().parent / "web"
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}


class AppState:
    """Holds the current design space; `reload` re-runs the extractor (after editing the model)."""

    def __init__(self, extract: Callable[[], dict], toolkit):
        self._extract = extract
        self._toolkit = toolkit
        self._lock = threading.Lock()
        self.design_space: dict = {}
        self.checker: ConfigurationChecker
        self.reload()

    def reload(self) -> None:
        ds = self._extract()
        with self._lock:
            self.design_space = ds
            self.checker = ConfigurationChecker(self._toolkit, ds)


def make_handler(state: AppState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # keep the console quiet
            pass

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload, status: int = 200) -> None:
            self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/api/design-space":
                return self._json(state.design_space)
            name = "index.html" if path in ("/", "") else path.lstrip("/")
            file = (WEB_DIR / name).resolve()
            if WEB_DIR not in file.parents or not file.is_file():
                return self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
            self._send(200, file.read_bytes(), CONTENT_TYPES.get(file.suffix, "application/octet-stream"))

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                pl = int(body.get("productLine", 0))
                selection = body.get("selection") or {}
                if self.path == "/api/check":
                    result = state.checker.check(pl, selection)
                    result["snippet"] = state.checker.snippet(pl, selection, body.get("name") or "meineKonfiguration")
                    return self._json(result)
                if self.path == "/api/alternatives":
                    return self._json(state.checker.alternatives(pl, selection))
                if self.path == "/api/explore":
                    return self._json(explore(state.checker, pl, body.get("mode", "random"),
                                              max(1, min(int(body.get("count", 100)), 500)),
                                              seed=int(body.get("seed", 0)), offset=int(body.get("offset", 0)),
                                              around=body.get("around") or [], known=body.get("known") or [],
                                              selections=body.get("selections") or []))
                if self.path == "/api/reload":
                    state.reload()
                    return self._json(state.design_space)
                return self._json({"error": "unknown endpoint"}, HTTPStatus.NOT_FOUND)
            except ExtractionError as err:
                return self._json({"error": str(err), "diagnostics": err.diagnostics}, HTTPStatus.UNPROCESSABLE_ENTITY)
            except Exception as err:  # report to the UI instead of dropping the connection
                traceback.print_exc()
                return self._json({"error": f"{type(err).__name__}: {err}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    return Handler


def serve(state: AppState, host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(state))
