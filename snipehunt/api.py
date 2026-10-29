"""Local API for the Home Assistant integration.

Home Assistant polls GET /api/snapshot for everything it shows, and posts to
/api/action (buttons) and /api/settings (the settings entities). Every request
needs the bearer token in `API_TOKEN`. Nothing here can bid, buy or change a
snipe: the only things it can do are the actions listed in actions.py.
"""
from __future__ import annotations

import hmac
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class QuietServer(ThreadingHTTPServer):
    """A client that hangs up mid-answer (Home Assistant restarting) isn't an error worth a traceback."""

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)

from . import actions, config, db, demo, settings, snapshot

MAX_BODY = 64 * 1024
CACHE_SECONDS = 15   # several dashboards polling at once share one snapshot

_lock = threading.Lock()   # one snapshot or action at a time
_cache: dict = {"at": 0.0, "body": b""}


def demo_mode() -> bool:
    return (config.get("SNIPEHUNT_DEMO") or "").lower() in ("1", "true", "yes", "on")


def _snapshot_body(fresh: bool = False) -> bytes:
    if demo_mode():
        return json.dumps(demo.build(), ensure_ascii=False).encode()
    if fresh or time.time() - _cache["at"] > CACHE_SECONDS:
        con = db.connect()
        try:
            body = json.dumps(snapshot.snapshot(con), ensure_ascii=False).encode()
        finally:
            con.close()
        _cache.update(at=time.time(), body=body)
    return _cache["body"]


def act(data: dict) -> tuple[int, bytes]:
    """Run one of the button actions; the outcome is kept for the dashboard."""
    if demo_mode():
        return 200, _reply(True, "Demo mode: nothing was changed.")
    con = db.connect()
    try:
        ok, message = actions.handle(con, data)
    finally:
        con.close()
    print(f"api: {data.get('action')}: {message}", file=sys.stderr)
    return 200, _reply(ok, message)


def change_settings(data: dict) -> tuple[int, bytes]:
    if demo_mode():
        return 200, _reply(True, "Demo mode: settings aren't saved.")
    con = db.connect()
    try:
        settings.update(con, data)
    except settings.SettingsError as exc:
        return 400, json.dumps({"ok": False, "message": str(exc)}).encode()
    finally:
        con.close()
    print(f"api: settings changed: {', '.join(sorted(data))}", file=sys.stderr)
    return 200, _reply(True, "Saved.")


def _reply(ok: bool, message: str) -> bytes:
    """The outcome plus a fresh snapshot, so Home Assistant updates at once."""
    return (b'{"ok": ' + (b"true" if ok else b"false") + b', "message": '
            + json.dumps(message).encode() + b', "snapshot": '
            + _snapshot_body(fresh=True) + b"}")


POST = {"/api/action": act, "/api/settings": change_settings}


class Handler(BaseHTTPRequestHandler):
    server_version = "SnipeHunt/1"
    token = ""

    def _send(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code: int, message: str) -> None:
        self._send(code, json.dumps({"ok": False, "message": message}).encode())

    def _allowed(self) -> bool:
        given = self.headers.get("Authorization", "")
        if hmac.compare_digest(given.encode(), f"Bearer {self.token}".encode()):
            return True
        self._error(401, "Bad or missing token")
        return False

    def _guarded(self, fn) -> None:
        try:
            with _lock:
                code, body = fn()
        except Exception as exc:
            print(f"api: {self.path} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            code, body = 500, json.dumps({
                "ok": False, "message": f"{type(exc).__name__}: {exc}"[:250]}).encode()
        self._send(code, body)

    def do_GET(self) -> None:   # noqa: N802
        if not self._allowed():
            return
        if self.path != "/api/snapshot":
            return self._error(404, "Not found")
        self._guarded(lambda: (200, _snapshot_body()))

    def do_POST(self) -> None:   # noqa: N802
        if not self._allowed():
            return
        if self.path not in POST:
            return self._error(404, "Not found")
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_BODY:
            return self._error(413, "Body missing or too large")
        try:
            data = json.loads(self.rfile.read(length))
        except ValueError:
            return self._error(400, "Body isn't JSON")
        if not isinstance(data, dict):
            return self._error(400, "Body must be a JSON object")
        self._guarded(lambda: POST[self.path](data))

    def log_message(self, fmt: str, *args) -> None:
        pass   # a poll every few seconds would flood the journal


def serve() -> None:
    (Handler.token,) = config.require("API_TOKEN")
    bind, port = config.get("API_BIND"), int(config.get("API_PORT"))
    server = QuietServer((bind, port), Handler)
    server.daemon_threads = True
    print(f"api: serving the Home Assistant integration on {bind}:{port}", file=sys.stderr)
    server.serve_forever()


if __name__ == "__main__":
    serve()
