from __future__ import annotations

import io
import os
import sys
import unittest
from contextlib import redirect_stderr
from email.message import Message
from unittest.mock import Mock, patch

try:
    from .support import REPO_ROOT
except ImportError:  # `unittest discover -s launcher/tests` imports top-level modules.
    from support import REPO_ROOT

from techsara_cli.bridge import MAX_REQUEST_BYTES, ModelBridge, main


class FakeWriter(io.BytesIO):
    def flush(self) -> None:
        return


def handler(
    *,
    authorization: str = "Bearer " + "k" * 32,
    content_length: str = "0",
    body: bytes = b"",
) -> ModelBridge:
    value = object.__new__(ModelBridge)
    headers = Message()
    if authorization:
        headers["Authorization"] = authorization
    if content_length:
        headers["Content-Length"] = content_length
    headers["Host"] = "untrusted.example"
    headers["Connection"] = "keep-alive"
    headers["X-Fixture"] = "preserve"
    value.headers = headers
    value.rfile = io.BytesIO(body)
    value.wfile = FakeWriter()
    value.command = "POST"
    value.path = "/v1/chat/completions"
    value.close_connection = False
    value.request_version = "HTTP/1.1"
    value.server_version = "fixture"
    value.sys_version = ""
    value.send_response = Mock()
    value.send_header = Mock()
    value.end_headers = Mock()
    value.target_host = "127.0.0.1"
    value.target_port = 18000
    value.api_key = "k" * 32
    return value


class AuthorizationAndBoundsTests(unittest.TestCase):
    def test_authorization_requires_nonempty_exact_constant_time_bearer_value(self) -> None:
        value = handler()
        with patch("techsara_cli.bridge.hmac.compare_digest", wraps=__import__("hmac").compare_digest) as compare:
            self.assertTrue(value._authorized())
        compare.assert_called_once_with("Bearer " + "k" * 32, "Bearer " + "k" * 32)

        for key, header in (
            ("", "Bearer "),
            ("k" * 32, ""),
            ("k" * 32, "Bearer wrong"),
            ("k" * 32, "Basic " + "k" * 32),
        ):
            with self.subTest(key=bool(key), header=header):
                value = handler(authorization=header)
                value.api_key = key
                self.assertFalse(value._authorized())

    def test_unauthorized_request_never_connects_to_model_backend(self) -> None:
        value = handler(authorization="Bearer wrong")
        value._error = Mock()
        with patch("techsara_cli.bridge.http.client.HTTPConnection") as connection:
            value._proxy()
        value._error.assert_called_once_with(401, b'{"error":"unauthorized"}')
        connection.assert_not_called()

    def test_invalid_negative_and_oversized_content_lengths_are_rejected_pre_read(self) -> None:
        cases = (
            ("not-a-number", 400),
            ("-1", 413),
            (str(MAX_REQUEST_BYTES + 1), 413),
        )
        for length, status in cases:
            with self.subTest(length=length):
                value = handler(content_length=length)
                value._error = Mock()
                value.rfile = Mock(side_effect=AssertionError("body must not be read"))
                with patch("techsara_cli.bridge.http.client.HTTPConnection") as connection:
                    value._proxy()
                self.assertEqual(value._error.call_args.args[0], status)
                connection.assert_not_called()

    def test_error_response_has_fixed_json_length_and_closes_connection(self) -> None:
        value = handler()
        value._error(401, b'{"error":"unauthorized"}')
        value.send_response.assert_called_once_with(401)
        headers = dict(call.args for call in value.send_header.call_args_list)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["Connection"], "close")
        self.assertEqual(int(headers["Content-Length"]), len(b'{"error":"unauthorized"}'))
        self.assertEqual(value.wfile.getvalue(), b'{"error":"unauthorized"}')

    def test_log_message_is_a_noop_even_when_given_secrets_and_prompt_paths(self) -> None:
        value = handler()
        with patch("builtins.print") as output:
            self.assertIsNone(value.log_message("Authorization: %s /prompt/%s", "secret", "content"))
        output.assert_not_called()


class FakeUpstreamResponse:
    status = 200

    def __init__(self, chunks=None) -> None:
        self.chunks = list(chunks or [b"first", b"second", b""])

    def getheaders(self):
        return [
            ("Content-Type", "text/event-stream"),
            ("Content-Length", "999"),
            ("Transfer-Encoding", "chunked"),
            ("X-Upstream", "fixture"),
        ]

    def read(self, _size):
        return self.chunks.pop(0)


class ProxyTests(unittest.TestCase):
    def test_proxy_strips_credentials_and_hop_headers_and_streams_response(self) -> None:
        body = b'{"messages":[{"role":"user","content":"fixture"}]}'
        value = handler(content_length=str(len(body)), body=body)
        connection = Mock()
        connection.getresponse.return_value = FakeUpstreamResponse()
        with patch("techsara_cli.bridge.http.client.HTTPConnection", return_value=connection) as factory:
            value._proxy()
        factory.assert_called_once_with("127.0.0.1", 18000, timeout=600)
        method, path = connection.request.call_args.args[:2]
        kwargs = connection.request.call_args.kwargs
        self.assertEqual((method, path), ("POST", "/v1/chat/completions"))
        self.assertEqual(kwargs["body"], body)
        self.assertEqual(kwargs["headers"]["X-Fixture"], "preserve")
        self.assertEqual(kwargs["headers"]["Content-Length"], str(len(body)))
        lowered = {key.lower() for key in kwargs["headers"]}
        self.assertNotIn("authorization", lowered)
        self.assertNotIn("host", lowered)
        self.assertNotIn("connection", lowered)
        response_headers = {call.args[0].lower(): call.args[1] for call in value.send_header.call_args_list}
        self.assertEqual(response_headers["content-type"], "text/event-stream")
        self.assertEqual(response_headers["x-upstream"], "fixture")
        self.assertNotIn("content-length", response_headers)
        self.assertNotIn("transfer-encoding", response_headers)
        self.assertEqual(response_headers["connection"], "close")
        self.assertEqual(value.wfile.getvalue(), b"firstsecond")
        self.assertTrue(value.close_connection)
        connection.close.assert_called_once()

    def test_upstream_failure_returns_generic_502_without_backend_detail(self) -> None:
        value = handler()
        value._error = Mock()
        connection = Mock()
        connection.request.side_effect = OSError("secret backend path")
        with patch("techsara_cli.bridge.http.client.HTTPConnection", return_value=connection):
            value._proxy()
        value._error.assert_called_once_with(502, b'{"error":"model backend unavailable"}')
        self.assertNotIn("secret", repr(value._error.call_args))
        connection.close.assert_called_once()

    def test_client_disconnect_is_normal_and_still_closes_upstream(self) -> None:
        value = handler()
        value.wfile = Mock()
        value.wfile.write.side_effect = BrokenPipeError
        connection = Mock()
        connection.getresponse.return_value = FakeUpstreamResponse([b"frame", b""])
        with patch("techsara_cli.bridge.http.client.HTTPConnection", return_value=connection):
            value._proxy()
        connection.close.assert_called_once()
        self.assertTrue(value.close_connection)


class BridgeMainTests(unittest.TestCase):
    def test_main_rejects_non_loopback_https_missing_port_bad_listen_port_and_short_key(self) -> None:
        cases = (
            (["bridge", "--listen-port", "18001", "--target", "http://example.com:18000"], "k" * 32),
            (["bridge", "--listen-port", "18001", "--target", "https://localhost:18000"], "k" * 32),
            (["bridge", "--listen-port", "18001", "--target", "http://localhost"], "k" * 32),
            (["bridge", "--listen-port", "80", "--target", "http://localhost:18000"], "k" * 32),
            (["bridge", "--listen-port", "18001", "--target", "http://localhost:18000"], "short"),
        )
        for argv, key in cases:
            with self.subTest(argv=argv, key_length=len(key)), patch.object(sys, "argv", argv), patch.dict(
                os.environ, {"TECHSARA_MODEL_API_KEY": key}, clear=False
            ), patch("techsara_cli.bridge.ThreadingHTTPServer") as server, redirect_stderr(
                io.StringIO()
            ), self.assertRaises(SystemExit):
                main()
            server.assert_not_called()

    def test_main_binds_requested_listener_to_loopback_target_and_closes_server(self) -> None:
        server = Mock()
        server.serve_forever.side_effect = KeyboardInterrupt
        argv = [
            "bridge",
            "--listen-host",
            "0.0.0.0",
            "--listen-port",
            "18001",
            "--target",
            "http://localhost:18000",
        ]
        with patch.object(sys, "argv", argv), patch.dict(
            os.environ, {"TECHSARA_MODEL_API_KEY": "k" * 32}, clear=False
        ), patch("techsara_cli.bridge.ThreadingHTTPServer", return_value=server) as factory:
            self.assertEqual(main(), 0)
        factory.assert_called_once_with(("0.0.0.0", 18001), ModelBridge)
        self.assertEqual(ModelBridge.target_host, "127.0.0.1")
        self.assertEqual(ModelBridge.target_port, 18000)
        self.assertEqual(ModelBridge.api_key, "k" * 32)
        server.serve_forever.assert_called_once_with(poll_interval=0.25)
        server.server_close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
