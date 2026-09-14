"""The internal attach protocol between the v1-gateway and `/v1` (2026-09-13).

WHY IT EXISTS (no-timeout design, deploy_survival). The client socket of a
long `/v1` call used to live in the process a routine deploy recreates, so an
orchestrator deploy cut every stream and every long synchronous call in
flight. The v1-gateway (gateway/) holds that socket instead, in a container
routine deploys never touch, and re-attaches to the new orchestrator when the
old one goes away. For it to do that, the two sides need a small private
vocabulary. This module is the orchestrator's half of it, and the only place
the names are spelled.

REQUEST HEADERS, written by the gateway only:

* `X-TechSara-Attempt: <uuid4>` on every request. It names one client call
  across any number of upstream attempts (pre-commit retries and post-commit
  re-attaches reuse it), so the durable layer can store it as
  `api_responses.attempt_token` and recognise the second POST as the first.
* `X-TechSara-Resume-After: <N>` on an SSE re-attach: the last internal
  sequence number the gateway relayed to its client. The answer replays from
  N+1 and never repeats `response.created`.
* `X-TechSara-Attach-Job: <key>` on an audio re-attach, with an empty body:
  the job key the orchestrator named in `X-TechSara-Run: job:<key>`.

RESPONSE HEADER, written by the orchestrator only for a TRUSTED, TAGGED
request: `X-TechSara-Run: <response id> | job:<key> | none`. The gateway reads
it as evidence that this build speaks the protocol and as the identity an
attach answer must repeat (gateway/lib/reattach.cjs `attachAnswerMatches`).
`none` means "nothing here can be re-attached": the gateway then never
re-POSTs that call after it may have been received.

SSE COMMENT, written after each data frame of a tagged stream IN THE SAME
WRITE: `: ts-seq=N`. It is a comment, so a conforming parser drops it, but it
must still never reach a caller — the gateway strips it (gateway/lib/sse.cjs),
and the Next edge strips it defensively. Never an `id:` or `retry:` line:
openai-python's SSE decoder breaks on an `id:` line followed by a comment
(measured, sdk_and_docs).

WHY TRUST IS DECIDED BY THE SOCKET PEER, NOT THE HEADER. Every one of these
headers changes what a request attaches to. From an arbitrary client, an
`X-TechSara-Attempt` copied off someone else's traffic would be a way to read
their generation, so they are honoured ONLY when the TCP peer is one of
PUBLIC_API_GATEWAY_PEERS. From anyone else they are ignored as if absent
(never a 400: a refusal would tell a scanner the names mean something). The
gateway drops any client-sent `x-techsara-*` header before it adds its own,
and its response allowlist never forwards `X-TechSara-Run`.

WHY A LIST OF ITS OWN, AND EXACT ADDRESSES ONLY (adversarial review of
T3-wire, 2026-09-14). This used to be PUBLIC_API_TRUSTED_PROXIES, the
resolver's `X-Forwarded-For` list. That list legitimately names every proxy
in front of /v1 (`.env.example` documents a whole network range) and is the
auth resolver's fallback list too, so one setting meant both "may tell me the
client's address" and "may attach to a run". Attaching to a run is the
stronger power, so it gets its own list, and that list takes single hosts
only: an entry wider than /32 (IPv4) or /128 (IPv6) is dropped with a
warning, never widened into trust. Empty trusts nobody, which turns the
protocol off and keeps requests safe. PUBLIC_API_TRUSTED_PROXIES keeps its one
meaning; the gateway's address belongs in both.

WHAT THIS MODULE DOES NOT DECIDE. Whether an attempt matches a run, what may
be replayed, and to whom, is the durable layer's (`durable.Runtime.attach_attempt`,
keyed by attempt token, key id and body sha256, called from
`router._attach_gateway_attempt` since 2026-09-14). This module parses,
validates, renders and hashes; it holds no state.
"""
from __future__ import annotations

import hashlib
import ipaddress
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, AsyncIterator, Mapping, Optional, Sequence

log = logging.getLogger(__name__)

# ------------------------------------------------------------ the names --

#: Request headers (lower case, as ASGI and Starlette present them).
ATTEMPT_HEADER = "x-techsara-attempt"
RESUME_AFTER_HEADER = "x-techsara-resume-after"
ATTACH_JOB_HEADER = "x-techsara-attach-job"

#: The response header. Spelled in the gateway's case for readability; HTTP
#: header names are case-insensitive and the gateway reads it lower-cased.
RUN_HEADER = "X-TechSara-Run"

#: Every internal header name, for a test (or an edge) that must prove none of
#: them crosses to a caller.
INTERNAL_HEADERS = (ATTEMPT_HEADER, RESUME_AFTER_HEADER, ATTACH_JOB_HEADER, RUN_HEADER.lower())

#: `X-TechSara-Run` for a call nothing can re-attach to (store:false, the
#: pooling routes, a build without the durable layer).
RUN_NONE = "none"

#: The prefix of a `X-TechSara-Run` that names an audio job rather than a
#: response (gateway/lib/reattach.cjs `parseRun`).
RUN_JOB_PREFIX = "job:"

#: The gateway mints a uuid4 (36 characters). Accepted a little more broadly —
#: letters, digits and hyphens, 8 to 64 — so a future gateway can move to
#: another opaque id without a lockstep deploy; anything else is ignored,
#: because the value is stored in a unique index and echoed in no body.
_ATTEMPT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{7,63}$")

#: Job keys are sha256 hex today (audio_jobs.AudioSource.key); the alphabet
#: allows the `job:` form's colon-free key and nothing that could break a
#: header or a log line.
_JOB_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

#: A sequence number is a JSON integer the gateway parsed from `: ts-seq=N`
#: (gateway/lib/sse.cjs accepts 1-15 digits). Bounded to what a JavaScript
#: number carries exactly.
_MAX_SEQUENCE = 2**53 - 1
_SEQUENCE_RE = re.compile(r"^[0-9]{1,16}$")

#: The SSE comment grammar the gateway parses (gateway/lib/sse.cjs TS_SEQ).
_TS_SEQ_TEMPLATE = ": ts-seq={n}\n\n"


# ------------------------------------------------------------- the tag --


@dataclass(frozen=True)
class GatewayTag:
    """What the gateway said about one request, after the trust decision.

    `trusted` is whether the socket peer is a configured proxy. The three
    values are None unless it is AND the header was present and well formed —
    so code that reads `tag.attempt` never has to remember the trust rule.
    """

    trusted: bool = False
    attempt: Optional[str] = None
    resume_after: Optional[int] = None
    attach_job: Optional[str] = None

    @property
    def tagged(self) -> bool:
        """A trusted request the gateway numbered: the only kind that gets
        `X-TechSara-Run` and `: ts-seq` comments."""
        return self.trusted and self.attempt is not None

    @property
    def reattach(self) -> bool:
        """A re-POST after the gateway had already relayed something: an SSE
        resume point or an audio job key. A fresh launch never carries one."""
        return self.tagged and (self.resume_after is not None or self.attach_job is not None)


UNTAGGED = GatewayTag()


#: The setting that names the gateway (module docstring).
GATEWAY_PEERS_SETTING = "PUBLIC_API_GATEWAY_PEERS"

#: Entries already warned about, so a bad value logs once per process rather
#: than once per request.
_WARNED_ENTRIES: set = set()


def _warn_once(entry: str, why: str) -> None:
    if entry in _WARNED_ENTRIES:
        return
    _WARNED_ENTRIES.add(entry)
    log.warning("%s: ignoring %r (%s)", GATEWAY_PEERS_SETTING, entry, why)


def _normalised(address: Any) -> Any:
    """An IPv4-mapped IPv6 address is its IPv4 address (the resolver's rule:
    a dual-stack socket reports `::ffff:10.0.0.2`)."""
    mapped = getattr(address, "ipv4_mapped", None)
    return mapped if mapped is not None else address


def parse_gateway_peers(entries: Sequence[str]) -> tuple:
    """The exact addresses in `entries`; anything else is dropped and logged.

    A bare address and a /32 (or /128) are the same host. A wider network is
    refused, not narrowed: `10.231.231.0/28` most likely means the operator
    meant "the relay network", and trusting any one address of it would be a
    guess."""
    peers = []
    for raw in entries or ():
        entry = str(raw).strip()
        if not entry:
            continue
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            _warn_once(entry, "not an IP address")
            continue
        if network.prefixlen != network.max_prefixlen:
            _warn_once(entry, "a network, not one address: name the gateway's own address")
            continue
        peers.append(_normalised(network.network_address))
    return tuple(peers)


def gateway_peers() -> tuple:
    """PUBLIC_API_GATEWAY_PEERS, read on every call (a monkeypatch or a
    restart takes effect without a re-import): from `settings` once config.py
    names it, from the environment until then. Comma-separated."""
    from ..config import settings

    configured = getattr(settings, GATEWAY_PEERS_SETTING.lower(), None)
    if configured is None:
        configured = os.environ.get(GATEWAY_PEERS_SETTING, "")
    if isinstance(configured, str):
        configured = configured.split(",")
    return parse_gateway_peers(tuple(configured or ()))


def peer_is_trusted(peer: Optional[str], gateway_peers_: Optional[Sequence[str]] = None) -> bool:
    """Whether the TCP peer is one of PUBLIC_API_GATEWAY_PEERS (or of
    `gateway_peers_`, the same form, for a test). An IPv4-mapped IPv6 peer is
    its IPv4 address; an empty list trusts nobody: the fail-closed default."""
    peers = gateway_peers() if gateway_peers_ is None else parse_gateway_peers(gateway_peers_)
    if not peer or not peers:
        return False
    try:
        address = _normalised(ipaddress.ip_address(str(peer).strip()))
    except ValueError:
        return False
    return address in peers


def _header(headers: Mapping[str, str], name: str) -> Optional[str]:
    value = headers.get(name)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def parse(
    headers: Mapping[str, str],
    peer: Optional[str],
    *,
    gateway_peers: Optional[Sequence[str]] = None,
) -> GatewayTag:
    """The tag for a request with these headers from this socket peer.

    From an untrusted peer the headers are IGNORED — the result is `UNTAGGED`
    whatever they say, and nothing is logged at more than debug: a client
    probing for the names learns nothing, and a noisy log would be a way to
    fill the disk. From a trusted peer a malformed value is dropped on its
    own (a bad Resume-After does not discard a good attempt id) and logged,
    because a trusted peer sending garbage is a gateway bug worth seeing.
    """
    names_present = any(headers.get(name) is not None for name in INTERNAL_HEADERS[:3])
    if not peer_is_trusted(peer, gateway_peers):
        if names_present:
            log.debug("ignoring x-techsara-* headers from an untrusted peer")
        return UNTAGGED
    attempt = _header(headers, ATTEMPT_HEADER)
    if attempt is not None and not _ATTEMPT_RE.match(attempt):
        log.warning("a trusted peer sent a malformed %s; ignored", ATTEMPT_HEADER)
        attempt = None
    resume_raw = _header(headers, RESUME_AFTER_HEADER)
    resume_after: Optional[int] = None
    if resume_raw is not None:
        if _SEQUENCE_RE.match(resume_raw) and int(resume_raw) <= _MAX_SEQUENCE:
            resume_after = int(resume_raw)
        else:
            log.warning("a trusted peer sent a malformed %s; ignored", RESUME_AFTER_HEADER)
    job = _header(headers, ATTACH_JOB_HEADER)
    if job is not None and not _JOB_KEY_RE.match(job):
        log.warning("a trusted peer sent a malformed %s; ignored", ATTACH_JOB_HEADER)
        job = None
    if attempt is None:
        # Resume-After and Attach-Job name a point in an ATTEMPT. Without the
        # attempt they identify nothing, and honouring them alone would let a
        # re-POST attach by job key to a call it did not make.
        return GatewayTag(trusted=True)
    return GatewayTag(trusted=True, attempt=attempt, resume_after=resume_after, attach_job=job)


def from_request(request: Any, *, gateway_peers: Optional[Sequence[str]] = None) -> GatewayTag:
    """`parse` for a Starlette request (its headers and its socket peer)."""
    client = getattr(request, "client", None)
    peer = getattr(client, "host", None) if client is not None else None
    return parse(request.headers, peer, gateway_peers=gateway_peers)


# ---------------------------------------------------- the response side --


def run_header_value(*, response_id: Optional[str] = None, job_key: Optional[str] = None) -> str:
    """`X-TechSara-Run`'s value: the response id, `job:<key>`, or `none`.

    A value that could not round-trip through the gateway's `parseRun` is
    rendered as `none` rather than sent: an unmatchable run name would make
    every re-attach a `run_mismatch` cut, which is worse than not attaching.
    """
    if response_id:
        text = str(response_id)
        if _JOB_KEY_RE.match(text) and not text.startswith(RUN_JOB_PREFIX):
            return text
        return RUN_NONE
    if job_key:
        text = str(job_key)
        if _JOB_KEY_RE.match(text):
            return RUN_JOB_PREFIX + text
    return RUN_NONE


def run_headers(tag: GatewayTag, *, response_id: Optional[str] = None, job_key: Optional[str] = None) -> dict:
    """The response headers for a tagged request, and NOTHING for any other.

    An untagged request never learns the header exists — not from a direct
    LAN caller, and not through the Next edge, whose allowlist would drop it
    anyway.
    """
    if not tag.tagged:
        return {}
    return {RUN_HEADER: run_header_value(response_id=response_id, job_key=job_key)}


def seq_comment(sequence_number: int) -> str:
    """`: ts-seq=N` as one complete SSE comment frame."""
    number = int(sequence_number)
    if number < 1 or number > _MAX_SEQUENCE:
        raise ValueError(f"an internal sequence number must be 1..2^53-1, not {number}")
    return _TS_SEQ_TEMPLATE.format(n=number)


def is_data_frame(frame: str) -> bool:
    """Whether an SSE frame carries an event (any non-comment field line).

    A heartbeat (`: ping`), a `: queued` note and a bare blank line are not
    events: numbering them would make the gateway hold a comment for a marker
    that never refers to anything a client could miss.
    """
    for line in str(frame).split("\n"):
        stripped = line.rstrip("\r")
        if stripped and not stripped.startswith(":"):
            return True
    return False


async def tag_frames(
    frames: AsyncIterator[str],
    *,
    start_after: int = 0,
) -> AsyncIterator[str]:
    """`frames`, with `: ts-seq=N` appended to every data frame in the SAME
    chunk (so one ASGI send, one TCP write).

    WHY THE SAME WRITE. The gateway holds a data frame until its marker
    arrives (gateway/lib/relay.cjs `onFrame`), and releases it unconfirmed
    after V1_GATEWAY_SEQ_HOLD_S. Two writes would let a crash fall between
    them and turn a frame the client already has into one the re-attach sends
    again.

    NUMBERING. N counts data frames from `start_after + 1`. On a Responses
    stream that is exactly each event's `sequence_number` (1-based, one per
    event, comments excluded — events.SequencedEvents); on a Chat Completions
    stream it is the chunk ordinal, `data: [DONE]` included, which is what a
    replay from N+1 needs. A stream re-attached with Resume-After N passes
    `start_after=N` together with frames that begin at N+1.

    WHERE IT IS STILL USED (2026-09-14). Only by NON-durable streams
    (`store: false`), which answer `X-TechSara-Run: none` and are never
    re-attached. A durable stream is rendered from its write-ahead log by
    `durable.sse_frames(tagged=True)`, which appends the LOG RECORD's number
    instead — on a Chat Completions stream that is not the chunk ordinal (the
    lifecycle records render no chunk), and it is the number a re-attach with
    Resume-After N replays from. Wrapping a durable stream here as well would
    number it twice (`router._SlotStream(number_frames=False)`).

    The wrapped iterator is closed with this one: `async for` has no teardown
    of its own, and the stream it wraps holds the engine generator.
    """
    number = int(start_after)
    try:
        async for frame in frames:
            if is_data_frame(frame):
                number += 1
                yield f"{frame}{seq_comment(number)}"
            else:
                yield frame
    finally:
        closer = getattr(frames, "aclose", None)
        if closer is not None:
            await closer()


# ------------------------------------------------------------ the body --


def body_sha256(raw: bytes) -> str:
    """sha256 over the RAW request body bytes, hex.

    The raw bytes and not the parsed JSON: the gateway re-POSTs exactly the
    bytes it buffered, and an attach must refuse a body that differs by a
    single byte (a re-POST whose body sha differs is not attached — the
    design's T3 test 4). Canonicalising the JSON first would call two
    different requests the same one.
    """
    return hashlib.sha256(bytes(raw or b"")).hexdigest()
