"""Authenticated, no-content-logging bridge for host-native model servers."""

from __future__ import annotations

import argparse
import hmac
import http.client
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

MAX_REQUEST_BYTES = 256 * 1024 * 1024
HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}


class ModelBridge(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    target_host = "127.0.0.1"
    target_port = 0
    api_key = ""

    def log_message(self, format: str, *args: object) -> None:
        # Never log paths, prompts, authorization headers, or model responses.
        return

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        expected = f"Bearer {self.api_key}"
        return bool(self.api_key) and hmac.compare_digest(header, expected)

    def _error(self, status: int, message: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(message)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(message)

    def _proxy(self) -> None:
        if not self._authorized():
            self._error(401, b'{"error":"unauthorized"}')
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            self._error(400, b'{"error":"invalid content length"}')
            return
        if length < 0 or length > MAX_REQUEST_BYTES:
            self._error(413, b'{"error":"request too large"}')
            return
        body = self.rfile.read(length) if length else None
        headers = {
            key: value for key, value in self.headers.items()
            if key.lower() not in HOP_HEADERS and key.lower() not in {"host", "authorization", "content-length"}
        }
        if body is not None:
            headers["Content-Length"] = str(len(body))
        connection = http.client.HTTPConnection(self.target_host, self.target_port, timeout=600)
        try:
            connection.request(self.command, self.path, body=body, headers=headers)
            response = connection.getresponse()
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() not in HOP_HEADERS and key.lower() != "content-length":
                    self.send_header(key, value)
            self.send_header("Connection", "close")
            self.end_headers()
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # Normal streaming cancellation; closing tears down the upstream.
            pass
        except OSError:
            if not self.wfile.closed:
                try:
                    self._error(502, b'{"error":"model backend unavailable"}')
                except OSError:
                    pass
        finally:
            connection.close()
            self.close_connection = True

    do_GET = _proxy
    do_POST = _proxy


def main() -> int:
    parser = argparse.ArgumentParser(description="TechSara authenticated model bridge")
    parser.add_argument("--listen-host", default="0.0.0.0")
    parser.add_argument("--listen-port", type=int, required=True)
    parser.add_argument("--target", required=True)
    args = parser.parse_args()
    target = urlsplit(args.target)
    if target.scheme != "http" or target.hostname not in {"127.0.0.1", "localhost"} or not target.port:
        parser.error("--target must be an explicit loopback HTTP endpoint")
    if not 1024 <= args.listen_port <= 65535:
        parser.error("--listen-port must be between 1024 and 65535")
    key = os.environ.get("TECHSARA_MODEL_API_KEY", "")
    if len(key) < 32:
        parser.error("TECHSARA_MODEL_API_KEY is missing or too short")
    ModelBridge.target_host = "127.0.0.1"
    ModelBridge.target_port = target.port
    ModelBridge.api_key = key
    server = ThreadingHTTPServer((args.listen_host, args.listen_port), ModelBridge)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
