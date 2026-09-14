"""A tiny contract-shaped `/v1` target for the suite's own end-to-end selftests.

WHY (2026-09-13): the adversarial review proved four suite defects by pointing
the real suite at a local mock — a hidden 429 passing as limits-off, a paced
request read as stream silence, a contract-correct rerank id failing, and a
ceiling refusal passing for the wrong reason. The selftests keep those proofs
runnable without a server, a key or engine time. It implements only what the
selftests call, in the shapes CONTRACT-3 §7-§10 and §16 document; it is not a
reference server.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional


@dataclass
class Behaviour:
    #: Document a 429 on /v1/models (limits enforced) or not (limits off).
    documents_429: bool = False
    #: Declare Response.incomplete_details (output_ceiling built).
    declares_output_ceiling: bool = False
    #: The first N GET /v1/models answer 429 rate_limit_error.
    models_429_first: int = 0
    #: Add a RateLimit header to 404 answers only.
    ratelimit_on_errors: bool = False
    #: Advertise this max_output_tokens on the chat model (None = absent).
    advertised_max_output_tokens: Optional[int] = None
    #: Refuse max_output_tokens above this with 400 (the pre-ceiling build).
    refuse_above: int = 8192
    #: POST /v1/responses with max_output_tokens >= this is 503 "at capacity".
    at_capacity_from: Optional[int] = None
    #: Seconds of `: ping` heartbeats before the first stream event.
    stream_prefill_s: float = 0.0
    ping_every_s: float = 1.0
    calls: Dict[str, int] = field(default_factory=dict)


def _doc(b: Behaviour) -> Dict[str, Any]:
    models_responses: Dict[str, Any] = {"200": {"description": "ok"}}
    if b.documents_429:
        models_responses["429"] = {"description": "rate_limit_error"}
    response_props: Dict[str, Any] = {"id": {"type": "string"}}
    if b.declares_output_ceiling:
        response_props["incomplete_details"] = {"type": "object"}
    return {
        "openapi": "3.1.0",
        "paths": {
            "/v1/models": {"get": {"responses": models_responses}},
            "/v1/responses": {"post": {"responses": {"200": {"description": "ok"}}}},
            "/v1/rerank": {"post": {"responses": {"200": {"description": "ok"}}}},
        },
        "components": {"schemas": {"Response": {"properties": response_props}}},
    }


def _model(b: Behaviour) -> Dict[str, Any]:
    out: Dict[str, Any] = {"id": "techsara-35b", "object": "model", "created": 1, "owned_by": "techsara"}
    if b.advertised_max_output_tokens is not None:
        out["max_output_tokens"] = b.advertised_max_output_tokens
    return out


def _error(code: str, message: str, param: Optional[str] = None) -> Dict[str, Any]:
    kind = "rate_limit_error" if code == "rate_limit_error" else "invalid_request_error"
    return {"error": {"message": message, "type": kind, "code": code, "param": param}}


def _handler(b: Behaviour):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:  # quiet
            pass

        def _count(self, key: str) -> int:
            b.calls[key] = b.calls.get(key, 0) + 1
            return b.calls[key]

        def _json(self, status: int, obj: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> None:
            request_id = "req_" + uuid.uuid4().hex
            if "error" in obj:
                obj["error"]["request_id"] = request_id
            payload = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("x-request-id", request_id)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _not_found(self, code: str = "model_not_found") -> None:
            extra = {"ratelimit": '"requests";r=50;t=30'} if b.ratelimit_on_errors else None
            self._json(404, _error(code, "The model does not exist."), extra)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/v1/openapi.json":
                return self._json(200, _doc(b))
            if self.path == "/v1/models":
                n = self._count("models")
                if n <= b.models_429_first:
                    return self._json(
                        429, _error("rate_limit_error", "The rate limit for this project has been reached."), {"retry-after": "1"}
                    )
                return self._json(200, {"object": "list", "data": [_model(b)]})
            if self.path == "/v1/models/techsara-35b":
                return self._json(200, _model(b))
            return self._not_found()

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("content-length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/v1/rerank":
                return self._rerank(body)
            if self.path != "/v1/responses":
                return self._not_found()
            if body.get("model") != "techsara-35b":
                return self._not_found()
            tokens = int(body.get("max_output_tokens") or 8192)
            if b.at_capacity_from is not None and tokens >= b.at_capacity_from:
                return self._json(
                    503,
                    {"error": {"message": "The model is at capacity. Retry after 60 seconds.", "type": "server_error",
                               "code": "model_unavailable", "param": None}},
                    {"retry-after": "60"},
                )
            if tokens > b.refuse_above:
                return self._json(
                    400,
                    _error("invalid_request_error", f"max_output_tokens must be between 1 and {b.refuse_above} for this model.",
                           "max_output_tokens"),
                )
            if body.get("stream"):
                return self._stream(tokens)
            return self._json(200, self._response("completed", tokens, usage=True))

        def _response(self, status: str, tokens: int, *, usage: bool) -> Dict[str, Any]:
            out_tokens = min(tokens, 8)
            text = "1\n" * out_tokens if status == "completed" else ""
            return {
                "id": "resp_" + "a" * 24, "object": "response", "created_at": 1, "model": "techsara-35b", "status": status,
                "output": [{"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                            "content": [{"type": "output_text", "text": text, "annotations": []}]}] if text else [],
                "usage": {"input_tokens": 5, "output_tokens": out_tokens, "total_tokens": 5 + out_tokens} if usage else None,
                "max_output_tokens": tokens, "incomplete_details": None,
            }

        def _stream(self, tokens: int) -> None:
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("cache-control", "no-store, no-cache, no-transform")
            self.send_header("x-request-id", "req_" + uuid.uuid4().hex)
            self.send_header("connection", "close")
            self.end_headers()
            deadline = time.monotonic() + b.stream_prefill_s
            while time.monotonic() < deadline:
                self.wfile.write(b": ping\n\n")
                self.wfile.flush()
                time.sleep(b.ping_every_s)
            base = self._response("in_progress", tokens, usage=False)
            done = self._response("completed", tokens, usage=True)
            count = done["usage"]["output_tokens"]
            events = [("response.created", {"response": base}), ("response.in_progress", {"response": base})]
            events += [("response.output_text.delta", {"delta": "1\n", "item_id": "msg_1", "output_index": 0, "content_index": 0})
                       for _ in range(count)]
            events += [("response.output_text.done", {"text": "1\n" * count, "item_id": "msg_1", "output_index": 0, "content_index": 0}),
                       ("response.completed", {"response": done})]
            for number, (name, data) in enumerate(events, 1):
                data = dict(data, type=name, sequence_number=number)
                self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())
                self.wfile.flush()
            self.close_connection = True

        def _rerank(self, body: Dict[str, Any]) -> None:
            documents = body.get("documents") or []
            scored = [{"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.1}, {"index": 2, "relevance_score": 0.05}]
            top = scored[: body.get("top_n") or len(scored)]
            if body.get("return_documents"):
                for result in top:
                    doc = documents[result["index"]]
                    result["document"] = {"text": doc if isinstance(doc, str) else doc["text"]}
            self._json(200, {"id": "rrk_" + uuid.uuid4().hex[:24], "object": "rerank", "model": "techsara-rerank",
                             "results": top, "usage": {"input_tokens": 88, "total_tokens": 88}})

    return Handler


class MockTarget:
    def __init__(self, behaviour: Behaviour) -> None:
        self.behaviour = behaviour
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(behaviour))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def __enter__(self) -> "MockTarget":
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._server.shutdown()
        self._server.server_close()
