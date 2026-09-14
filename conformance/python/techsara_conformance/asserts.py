"""The error envelope of CONTRACT-3 §9, asserted one way everywhere."""
from __future__ import annotations

from typing import Any, Mapping, Optional

ENVELOPE_KEYS = {"message", "type", "code", "param", "request_id"}

#: A well-formed key that cannot exist (all-zero public id, filler secret).
#: Built at runtime so no key-shaped literal sits in a public repository for
#: a secret scanner to trip on (2026-09-13).
FAKE_KEY = "tsk_test_" + "0" * 16 + "_" + "x" * 43 + "000000"

#: CONTRACT-3 §9: the closed code table and each code's HTTP status.
STATUS_FOR = {
    "invalid_request_error": 400,
    "context_length_exceeded": 400,
    "invalid_api_key": 401,
    "insufficient_scope": 403,
    "origin_not_allowed": 403,
    "model_not_found": 404,
    "response_not_found": 404,
    "idempotency_conflict": 409,
    "request_too_large": 413,
    "rate_limit_error": 429,
    "quota_exceeded": 429,
    "concurrency_limit_exceeded": 429,
    "model_recovering": 503,
    "model_unavailable": 503,
    "timeout": 504,
    "internal_error": 500,
}

_LEAKS = ("Traceback", "/home/", "/app/", "vllm", "Qwen/", "psycopg", "SELECT ")


def envelope(status: int, body: Any, headers: Mapping[str, str], *, code: str, param: Optional[str] = "any") -> Mapping[str, Any]:
    """Assert `body` is the §9 envelope for `code` and return the inner error."""
    assert isinstance(body, Mapping) and set(body) == {"error"}, f"not the §9 envelope: {body!r}"
    error = body["error"]
    assert set(error) == ENVELOPE_KEYS, f"envelope keys {sorted(error)} != {sorted(ENVELOPE_KEYS)}"
    assert error["code"] == code, f"code {error['code']!r} (message {error['message']!r}), expected {code!r}"
    assert status == STATUS_FOR[code], f"HTTP {status} for {code}, contract says {STATUS_FOR[code]}"
    assert isinstance(error["message"], str) and error["message"], "empty message"
    assert error["request_id"] and error["request_id"] == headers.get("x-request-id"), (
        f"request_id {error['request_id']!r} != X-Request-Id {headers.get('x-request-id')!r}"
    )
    if param != "any":
        assert error["param"] == param, f"param {error['param']!r}, expected {param!r}"
    for leak in _LEAKS:
        assert leak not in error["message"], f"internal detail {leak!r} in an error message: {error['message']!r}"
    return error


def sdk_error(exc: Any, *, code: str, param: Optional[str] = "any") -> Mapping[str, Any]:
    """The same assertion for an `openai.APIStatusError`."""
    response = exc.response
    try:
        body = response.json()
    except ValueError:  # pragma: no cover — a non-JSON error is itself the failure
        raise AssertionError(f"HTTP {response.status_code} error body is not JSON: {response.text[:200]!r}")
    error = envelope(response.status_code, body, response.headers, code=code, param=param)
    assert exc.code == code, f"the SDK read code {exc.code!r} from the envelope, expected {code!r}"
    return error
