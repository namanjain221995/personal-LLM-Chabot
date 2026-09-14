"""Which contract features the target has BUILT, and how the suite knows.

WHY DETECTION FROM THE SERVER'S OWN SCHEMA (2026-09-13). The programme is
landing six models, a 1,000,000-token output ceiling and a Files API in
parallel waves, so on any given day a deployment has some of them. A test for
a feature that is not built yet must say "not built yet" — XFAIL(strict) —
rather than fail the release or, worse, pass silently.

The evidence is `GET /v1/openapi.json`, which CONTRACT-3 §7 says is the
public schema and which `publicapi/openapi.py` builds from the same modules
the routes read. A feature counts as BUILT when that document declares it.
Three outcomes follow, all of them honest:

* declared and working           -> PASS
* declared and broken            -> FAIL
* not declared, and not working  -> XFAIL  "not built yet: <feature> — <evidence>"
* not declared, but working      -> FAIL   (XPASS(strict): the server does the
                                            thing its schema does not describe,
                                            which is a documentation defect)

A release manager can override any row with `--feature NAME=built|planned`
(repeatable) or TECHSARA_FEATURES="a=built,b=planned" — e.g. to insist a
feature is built on the release candidate, turning an XFAIL into a FAIL.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

BUILT = "built"
PLANNED = "planned"

Detector = Callable[[Mapping[str, Any], str], Tuple[bool, str]]


def _schema_props(doc: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    schema = (doc.get("components") or {}).get("schemas", {}).get(name) or {}
    return schema.get("properties") or {}


def _has_operation(path_pattern: str, method: str) -> Detector:
    regex = re.compile(path_pattern)

    def detect(doc: Mapping[str, Any], _text: str) -> Tuple[bool, str]:
        for path, ops in (doc.get("paths") or {}).items():
            if regex.fullmatch(path) and method in (ops or {}):
                return True, f"{method.upper()} {path} is in /v1/openapi.json"
        return False, f"no {method.upper()} {path_pattern} in /v1/openapi.json"

    return detect


def _schema_has(schema: str, prop: str) -> Detector:
    def detect(doc: Mapping[str, Any], _text: str) -> Tuple[bool, str]:
        if prop in _schema_props(doc, schema):
            return True, f"schema {schema} declares `{prop}`"
        return False, f"schema {schema} has no `{prop}` in /v1/openapi.json"

    return detect


def _text_has(needle: str, what: str) -> Detector:
    def detect(_doc: Mapping[str, Any], text: str) -> Tuple[bool, str]:
        if needle in text:
            return True, f"/v1/openapi.json mentions {what}"
        return False, f"/v1/openapi.json never mentions {what}"

    return detect


def _response_description_has(path: str, method: str, status: str, needle: str) -> Detector:
    def detect(doc: Mapping[str, Any], _text: str) -> Tuple[bool, str]:
        op = ((doc.get("paths") or {}).get(path) or {}).get(method) or {}
        description = str(((op.get("responses") or {}).get(status) or {}).get("description") or "")
        if needle in description:
            return True, f"{method.upper()} {path} {status} is described as {needle!r}"
        return False, f"{method.upper()} {path} {status} description does not say {needle!r}"

    return detect


def _no_429_anywhere(doc: Mapping[str, Any], _text: str) -> Tuple[bool, str]:
    offenders = [
        f"{method.upper()} {path}"
        for path, ops in (doc.get("paths") or {}).items()
        for method, op in (ops or {}).items()
        if isinstance(op, Mapping) and "429" in (op.get("responses") or {})
    ]
    if offenders:
        return False, f"429 still documented on {len(offenders)} operations (limits enforced)"
    return True, "no operation documents a 429 (limits off)"


@dataclass(frozen=True)
class Feature:
    name: str
    summary: str
    contract: str
    detect: Detector
    #: Scopes the MAIN key needs for this feature's tests (CONTRACT-3 §7).
    #: Only scopes the contract names: the Files API scopes are still a
    #: design proposal (files.read / files.write), so files features require
    #: none until CONTRACT.md lists them — add them here when it does.
    scopes: Tuple[str, ...] = field(default=())


FEATURES: Dict[str, Feature] = {
    f.name: f
    for f in [
        Feature(
            "model_catalogue",
            "GET /v1/models lists every configured model with kind, capabilities, endpoints and ceilings",
            "CONTRACT-3 §7, §15",
            _schema_has("Model", "kind"),
        ),
        Feature(
            "output_ceiling",
            "max_output_tokens up to 1,000,000, clamped not refused; applied value + incomplete_details on the wire",
            "CONTRACT-3 §8.3, §9, §10",
            _schema_has("Response", "incomplete_details"),
        ),
        Feature(
            "max_completion_tokens",
            "chat.completions accepts max_completion_tokens as an alias of max_tokens",
            "CONTRACT-3 §8.2",
            _schema_has("ChatCompletionRequest", "max_completion_tokens"),
        ),
        Feature(
            "idempotency_in_flight_409",
            "the same Idempotency-Key + body while the first is running is 409 with Retry-After (was 429)",
            "CONTRACT-3 §13",
            _response_description_has("/v1/responses", "post", "409", "still running"),
        ),
        Feature(
            "limits_off",
            "no usage limits: no 429 for volume and no RateLimit / RateLimit-Policy headers",
            "CONTRACT-3 §12.1",
            _no_429_anywhere,
        ),
        Feature(
            "capacity_gates",
            "per-engine public capacity gates answer 503 model_unavailable 'at capacity' with Retry-After",
            "CONTRACT-3 §12.3",
            _response_description_has("/v1/responses", "post", "503", "at capacity"),
        ),
        Feature(
            "image_input",
            "input_image / image_url data-URL parts on /v1/responses and /v1/chat/completions; vision and OCR models",
            "CONTRACT-3 §8.1, §8.2",
            _text_has('"input_image"', "the `input_image` content part"),
        ),
        Feature(
            "embeddings",
            "POST /v1/embeddings with techsara-embed",
            "CONTRACT-3 §8.4",
            _has_operation(r"/v1/embeddings", "post"),
            scopes=("embeddings.write",),
        ),
        Feature(
            "rerank",
            "POST /v1/rerank with techsara-rerank",
            "CONTRACT-3 §8.5",
            _has_operation(r"/v1/rerank", "post"),
            scopes=("rerank.write",),
        ),
        Feature(
            "audio_transcriptions",
            "POST /v1/audio/transcriptions with techsara-whisper (multipart)",
            "CONTRACT-3 §8.6",
            _has_operation(r"/v1/audio/transcriptions", "post"),
            scopes=("audio.write",),
        ),
        Feature(
            "files",
            "POST/GET/DELETE /v1/files and GET /v1/files/{id}/content",
            "Files API design 2026-09-13, OpenAI parity plan A.1-A.5",
            _has_operation(r"/v1/files", "post"),
        ),
        Feature(
            "uploads",
            "POST /v1/uploads, /parts, /complete, /cancel (OpenAI Uploads, chunked)",
            "Files API design 2026-09-13, OpenAI parity plan A.6-A.9",
            _has_operation(r"/v1/uploads", "post"),
        ),
        Feature(
            "uploads_resume",
            "GET /v1/uploads/{id} lists received parts; part_number makes a part retry idempotent",
            "Files API design 2026-09-13, parity plan C.1-C.3",
            _has_operation(r"/v1/uploads/\{[^}/]+\}", "get"),
        ),
        Feature(
            "files_model_input",
            "an uploaded file used as model input (input_file / input_image with file_id)",
            "Files API design 2026-09-13, parity plan A.10-A.11",
            _text_has('"input_file"', "the `input_file` content part"),
        ),
    ]
}


@dataclass(frozen=True)
class Resolution:
    name: str
    state: str
    evidence: str


def parse_overrides(values: Iterable[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for chunk in values:
        for item in str(chunk).split(","):
            item = item.strip()
            if not item:
                continue
            name, _, state = item.partition("=")
            name, state = name.strip(), state.strip().lower()
            if name not in FEATURES:
                raise ValueError(f"unknown feature {name!r}; known: {', '.join(sorted(FEATURES))}")
            if state not in (BUILT, PLANNED, "auto"):
                raise ValueError(f"feature {name}: state must be built, planned or auto, not {state!r}")
            out[name] = state
    return out


def resolve(
    doc: Optional[Mapping[str, Any]], fetch_error: Optional[str], overrides: Mapping[str, str]
) -> Dict[str, Resolution]:
    text = json.dumps(doc, sort_keys=True) if doc is not None else ""
    out: Dict[str, Resolution] = {}
    for name, feature in FEATURES.items():
        forced = overrides.get(name, "auto")
        if forced in (BUILT, PLANNED):
            out[name] = Resolution(name, forced, "forced on the command line / TECHSARA_FEATURES")
            continue
        if doc is None:
            out[name] = Resolution(name, PLANNED, f"could not read /v1/openapi.json ({fetch_error})")
            continue
        built, evidence = feature.detect(doc, text)
        out[name] = Resolution(name, BUILT if built else PLANNED, evidence)
    return out


def missing_scopes(name: str, held: Optional[frozenset]) -> List[str]:
    """The scopes feature `name` needs that a key holding `held` lacks
    (empty when the key's scopes are unknown)."""
    if held is None:
        return []
    return [scope for scope in FEATURES[name].scopes if scope not in held]


def table(resolutions: Mapping[str, Resolution]) -> List[str]:
    width = max(len(n) for n in resolutions)
    return [f"  {r.name:<{width}}  {r.state:<7}  {r.evidence}" for r in resolutions.values()]
