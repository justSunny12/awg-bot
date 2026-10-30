"""«РФ-сайт» стенда для сценария транзита (s08_rf_transit): HTTP в пространстве `lab-rf`
на всех его адресах, каждый запрос — строкой в журнал JSONL (кто пришёл и на какой адрес).

- `GET /ip` — в ответе адрес, с которого сайт видит запрос (`src`);
- `GET /blob?n=<байт>` — столько байт нулей (скачивание через транзит, MSS);
- остальное — `ok` (зонды живости слотов ходят TCP-коннектом на тот же порт).

Строка журнала: {"ts", "src", "dst", "path"}; путь журнала — аргумент `--journal`.

    ip netns exec lab-rf python -m tests.lab.rfsite --port 80 --journal $PL/rf.jsonl
"""
from __future__ import annotations

import argparse
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

_LOCK = threading.Lock()


def make_handler(journal: str):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, fmt, *args):            # noqa: N802 — журнал свой
            return

        def do_GET(self):                              # noqa: N802
            u = urlparse(self.path)
            src = self.client_address[0]
            dst = self.connection.getsockname()[0]
            with _LOCK, open(journal, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": time.time(), "src": src, "dst": dst, "path": u.path}) + "\n")
            if u.path == "/blob":
                n = int((parse_qs(u.query).get("n") or ["1000000"])[0])
                self.send_response(200)
                self.send_header("Content-Length", str(n))
                self.end_headers()
                chunk = b"\0" * 65536
                left = n
                while left > 0:
                    self.wfile.write(chunk[:min(left, len(chunk))])
                    left -= min(left, len(chunk))
                return
            body = (src if u.path == "/ip" else "ok").encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    return Handler


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=80)
    p.add_argument("--journal", required=True)
    a = p.parse_args()
    ThreadingHTTPServer.address_family = socket.AF_INET
    ThreadingHTTPServer.allow_reuse_address = True
    srv = ThreadingHTTPServer(("0.0.0.0", a.port), make_handler(a.journal))
    srv.serve_forever()


if __name__ == "__main__":
    main()
