#!/usr/bin/env python3
"""eBay Marketplace Account Deletion endpoint.

eBay only enables a Production keyset once this answers its challenge:
  GET  <endpoint>?challenge_code=X  ->  {"challengeResponse": sha256(X + token + endpoint)}
  POST <endpoint>                   ->  204 (a deletion notice; acknowledged)

Standalone on purpose (stdlib only, no repo imports): it is the one part of
Snipe Hunt reachable from the internet, so run it as a throwaway user
with only its own two settings.
"""
import hashlib
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

TOKEN = os.environ["EBAY_VERIFICATION_TOKEN"]
ENDPOINT = os.environ["EBAY_DELETION_ENDPOINT"]
PATH = urlsplit(ENDPOINT).path
PORT = int(os.environ.get("EBAY_DELETION_PORT", "8765"))
MAX_BODY = 64 * 1024


def challenge_response(code: str) -> str:
    return hashlib.sha256((code + TOKEN + ENDPOINT).encode()).hexdigest()


class Handler(BaseHTTPRequestHandler):
    server_version = "snipe-hunt"
    sys_version = ""

    def _send(self, status: int, body: bytes = b"") -> None:
        self.send_response(status)
        if body:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        code = parse_qs(url.query).get("challenge_code", [""])[0]
        if url.path != PATH or not code or len(code) > 512:
            return self._send(404)
        print("challenge answered", flush=True)
        self._send(200, json.dumps({"challengeResponse": challenge_response(code)}).encode())

    def do_POST(self) -> None:
        if urlsplit(self.path).path != PATH:
            return self._send(404)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._send(400)
        if length < 0 or length > MAX_BODY:
            return self._send(413)
        raw = self.rfile.read(length)
        try:
            note_id = json.loads(raw)["notification"]["notificationId"]
        except (ValueError, KeyError, TypeError):
            note_id = "unparsed"
        # Log the notice id only, never the user it names.
        print(f"deletion notice {str(note_id)[:80]}", flush=True)
        self._send(204)

    def log_message(self, *args) -> None:  # the lines above are the log
        pass


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--answer":
        print(challenge_response(sys.argv[2]))
    else:
        ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
