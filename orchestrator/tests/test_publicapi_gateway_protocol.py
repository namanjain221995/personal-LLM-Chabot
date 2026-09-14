"""The orchestrator's half of the internal attach protocol (2026-09-13).

The v1-gateway (gateway/) tags every request with `X-TechSara-Attempt`, reads
`X-TechSara-Run` off the answer, strips `: ts-seq=N` comments out of a stream
and re-POSTs with `X-TechSara-Resume-After` when the orchestrator goes away.
What this suite pins:

* the headers are honoured ONLY from a PUBLIC_API_GATEWAY_PEERS peer — from
  anyone else they are ignored as if absent, with no refusal to learn from —
  and that list is NOT PUBLIC_API_TRUSTED_PROXIES: a proxy trusted to name a
  client's address is not thereby trusted to attach to a run, and the list
  takes exact addresses only;
* a trusted, tagged request gets `X-TechSara-Run` on every answer, and a
  stream gets `: ts-seq=N` after every data frame in the same write, numbered
  like the events' own `sequence_number` (chat: the chunk ordinal,
  `[DONE]` included);
* until generations launch through the durable layer, a generation's run is
  `none` and a re-attach is a 404 that never reaches the engine — a second
  generation must never be spliced onto a client's first;
* the internal names never reach a browser (`Access-Control-Expose-Headers`).
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app import db, llm
from app.config import settings
from app.publicapi import events, gateway_protocol as gp, registry
from app.publicapi import router as public_router
from tests.test_publicapi_routes import (  # noqa: F401 - fixtures
    TOKENS,
    _bare_app,
    _pepper,
    platform,
)
from tests.test_publicapi_sync_commit import body_of, serve, timed_post

ATTEMPT = "3f0c2b1e-5a6d-4c7b-9e8f-0a1b2c3d4e5f"


# ------------------------------------------------------------ parsing --


def test_the_headers_from_an_untrusted_peer_are_ignored_whatever_they_say():
    headers = {gp.ATTEMPT_HEADER: ATTEMPT, gp.RESUME_AFTER_HEADER: "7", gp.ATTACH_JOB_HEADER: "abc"}
    assert gp.parse(headers, "203.0.113.9", gateway_peers=("10.231.231.2",)) == gp.UNTAGGED
    assert gp.parse(headers, "10.1.2.3", gateway_peers=()) == gp.UNTAGGED  # empty trusts nobody
    assert gp.parse(headers, None, gateway_peers=("10.231.231.2",)) == gp.UNTAGGED
    assert gp.parse(headers, "testclient", gateway_peers=("10.231.231.2",)) == gp.UNTAGGED


def test_a_gateway_peer_is_matched_as_one_exact_address_including_its_ipv4_mapped_form():
    headers = {gp.ATTEMPT_HEADER: ATTEMPT, gp.RESUME_AFTER_HEADER: "7"}
    tag = gp.parse(headers, "::ffff:10.231.231.2", gateway_peers=("10.231.231.2",))
    assert tag == gp.GatewayTag(trusted=True, attempt=ATTEMPT, resume_after=7)
    assert tag.tagged and tag.reattach
    assert gp.parse(headers, "10.231.231.2", gateway_peers=("10.231.231.2/32", "not-an-address")).tagged
    assert gp.parse(headers, "fd00::7", gateway_peers=("fd00::7/128",)).tagged
    assert gp.parse(headers, "10.231.231.3", gateway_peers=("10.231.231.2",)) == gp.UNTAGGED


def test_a_network_in_the_gateway_list_is_refused_rather_than_widened_into_trust(caplog):
    """A range can never be the gateway: only exact addresses are trusted to
    attach (2026-09-14)."""
    headers = {gp.ATTEMPT_HEADER: ATTEMPT, gp.RESUME_AFTER_HEADER: "7"}
    gp._WARNED_ENTRIES.clear()
    with caplog.at_level("WARNING", logger=gp.__name__):
        for peer in ("10.231.231.2", "172.18.0.6", "172.18.0.9"):
            assert gp.parse(headers, peer, gateway_peers=("10.231.231.0/28", "172.18.0.2/31", "172.18.0.8/29")) == gp.UNTAGGED
        assert gp.parse_gateway_peers(("10.231.231.0/28", "fd00::/64", "10.231.231.2")) == (
            gp.ipaddress.ip_address("10.231.231.2"),
        )
    warned = [r.getMessage() for r in caplog.records]
    assert any("10.231.231.0/28" in m and "a network" in m for m in warned)
    assert sum("10.231.231.0/28" in m for m in warned) == 1  # once per process, not per request


def test_a_proxy_trusted_for_x_forwarded_for_is_not_trusted_to_attach(monkeypatch):
    """The two trust roots are separate settings: a peer trusted for
    X-Forwarded-For (the documented range form, plus the gateway) is not
    thereby trusted to attach."""
    headers = {gp.ATTEMPT_HEADER: ATTEMPT, gp.RESUME_AFTER_HEADER: "7"}
    monkeypatch.setattr(
        settings, "public_api_trusted_proxies", ("172.18.0.2/31", "172.18.0.4/30", "172.18.0.8/29", "10.231.231.2")
    )
    monkeypatch.setattr(settings, "public_api_gateway_peers", ("10.231.231.2",), raising=False)
    assert gp.parse(headers, "10.231.231.2").reattach
    for container in ("172.18.0.6", "172.18.0.9"):  # two other peers inside the proxy range
        assert gp.parse(headers, container) == gp.UNTAGGED
    monkeypatch.setattr(settings, "public_api_gateway_peers", None, raising=False)
    monkeypatch.setenv("PUBLIC_API_GATEWAY_PEERS", " 10.231.231.2 , ")
    assert gp.parse(headers, "10.231.231.2").reattach
    monkeypatch.delenv("PUBLIC_API_GATEWAY_PEERS")
    assert gp.parse(headers, "10.231.231.2") == gp.UNTAGGED  # unset: nobody, whatever the proxy list says


def test_a_malformed_value_from_a_trusted_peer_is_dropped_on_its_own():
    trusted = ("127.0.0.1",)
    bad_resume = gp.parse({gp.ATTEMPT_HEADER: ATTEMPT, gp.RESUME_AFTER_HEADER: "-1"}, "127.0.0.1", gateway_peers=trusted)
    assert bad_resume.attempt == ATTEMPT and bad_resume.resume_after is None and not bad_resume.reattach
    huge = gp.parse({gp.ATTEMPT_HEADER: ATTEMPT, gp.RESUME_AFTER_HEADER: str(2**60)}, "127.0.0.1", gateway_peers=trusted)
    assert huge.resume_after is None
    bad_job = gp.parse({gp.ATTEMPT_HEADER: ATTEMPT, gp.ATTACH_JOB_HEADER: "a b\r\nx"}, "127.0.0.1", gateway_peers=trusted)
    assert bad_job.attach_job is None
    for attempt in ("short", "x" * 65, "uuid with spaces", "-leading-hyphen"):
        assert gp.parse({gp.ATTEMPT_HEADER: attempt}, "127.0.0.1", gateway_peers=trusted).attempt is None


def test_a_resume_point_without_an_attempt_identifies_nothing():
    tag = gp.parse({gp.RESUME_AFTER_HEADER: "3", gp.ATTACH_JOB_HEADER: "k1"}, "127.0.0.1", gateway_peers=("127.0.0.1",))
    assert tag == gp.GatewayTag(trusted=True)
    assert not tag.tagged and not tag.reattach


def test_the_run_header_names_a_response_a_job_or_nothing():
    assert gp.run_header_value(response_id="resp_0123456789abcdef01234567") == "resp_0123456789abcdef01234567"
    assert gp.run_header_value(job_key="a" * 64) == "job:" + "a" * 64
    assert gp.run_header_value() == "none"
    assert gp.run_header_value(response_id="resp x\r\n") == "none"
    assert gp.run_header_value(response_id="job:spoof") == "none"
    assert gp.run_headers(gp.UNTAGGED, response_id="resp_1") == {}
    assert gp.run_headers(gp.GatewayTag(trusted=True), response_id="resp_1") == {}
    assert gp.run_headers(gp.GatewayTag(trusted=True, attempt=ATTEMPT), response_id="resp_1") == {"X-TechSara-Run": "resp_1"}


def test_the_body_hash_is_over_the_raw_bytes_so_one_changed_byte_is_a_different_body():
    a = gp.body_sha256(b'{"model":"m","input":"hi"}')
    assert a == gp.body_sha256(b'{"model":"m","input":"hi"}')
    assert a != gp.body_sha256(b'{"model": "m","input":"hi"}')
    assert len(a) == 64


def test_every_data_frame_is_followed_by_its_sequence_in_the_same_chunk_and_comments_are_not_numbered():
    frames = [": ping\n\n", "event: a\ndata: {}\n\n", events.queued_comment(), "data: {}\n\n", "data: [DONE]\n\n"]
    closed = []

    async def source():
        try:
            for frame in frames:
                yield frame
        finally:
            closed.append(True)

    async def collect(start_after=0):
        return [chunk async for chunk in gp.tag_frames(source(), start_after=start_after)]

    out = asyncio.run(collect())
    assert out == [
        ": ping\n\n",
        "event: a\ndata: {}\n\n: ts-seq=1\n\n",
        ": queued\n\n",
        "data: {}\n\n: ts-seq=2\n\n",
        "data: [DONE]\n\n: ts-seq=3\n\n",
    ]
    assert asyncio.run(collect(start_after=41))[1].endswith(": ts-seq=42\n\n")
    assert closed == [True, True]
    with pytest.raises(ValueError):
        gp.seq_comment(0)


# ---------------------------------------------------- over the routes --


class _Engine:
    def __init__(self, pieces=("Hello", " world")):
        self.pieces = pieces
        self.calls = 0

    def __call__(self, messages, **kwargs):
        self.calls += 1
        return self._run()

    async def _run(self):
        for piece in self.pieces:
            await asyncio.sleep(0)
            yield ("token", piece)


@pytest.fixture()
def served(platform, monkeypatch):
    engine = _Engine()
    monkeypatch.setattr(llm, "stream_chat_events", engine)
    monkeypatch.setattr(llm, "reset_usage", lambda: None)
    monkeypatch.setattr(llm, "get_usage", lambda: None)
    app = _bare_app()
    public_router.install_error_handlers(app)
    with serve(app) as port:
        yield port, engine


def _trust_this_machine(monkeypatch):
    monkeypatch.setattr(settings, "public_api_gateway_peers", ("127.0.0.1",), raising=False)


def _headers(**extra):
    headers = {"Authorization": f"Bearer {TOKENS['live']}"}
    headers.update(extra)
    return headers


RESPONSES_STREAM = {"model": registry.TECHSARA_35B, "input": "hi", "stream": True}
CHAT_STREAM = {"model": registry.TECHSARA_35B, "stream": True, "messages": [{"role": "user", "content": "hi"}]}


def test_an_untrusted_peer_gets_no_run_header_and_no_sequence_comments(served, monkeypatch):
    port, _engine = served
    monkeypatch.setattr(settings, "public_api_gateway_peers", ("10.254.0.1",), raising=False)
    monkeypatch.setattr(settings, "public_api_trusted_proxies", ("127.0.0.1",))  # a proxy, not the gateway
    tagged = _headers(**{gp.ATTEMPT_HEADER: ATTEMPT})
    response, chunks, _ = timed_post(port, "/v1/responses", json_body=RESPONSES_STREAM, headers=tagged)
    assert response.status_code == 200
    assert "x-techsara-run" not in response.headers
    assert b"ts-seq" not in body_of(chunks)
    sync, _, _ = timed_post(port, "/v1/responses", json_body={"model": registry.TECHSARA_35B, "input": "hi"}, headers=tagged)
    assert sync.status_code == 200 and "x-techsara-run" not in sync.headers


def test_a_tagged_responses_stream_numbers_every_event_with_its_own_sequence_number(served, monkeypatch):
    port, _engine = served
    _trust_this_machine(monkeypatch)
    response, chunks, _ = timed_post(
        port, "/v1/responses", json_body=RESPONSES_STREAM, headers=_headers(**{gp.ATTEMPT_HEADER: ATTEMPT})
    )
    assert response.status_code == 200
    assert response.headers["x-techsara-run"] == "none"
    text = body_of(chunks).decode()
    blocks = [b for b in text.split("\n\n") if b.strip()]
    seen = []
    for index, block in enumerate(blocks):
        if block.startswith("event: "):
            number = json.loads(block.split("data: ", 1)[1])["sequence_number"]
            assert blocks[index + 1] == f": ts-seq={number}", (block, blocks[index + 1])
            seen.append(number)
    assert seen == list(range(1, len(seen) + 1)) and len(seen) >= 5
    assert text.count("ts-seq=") == len(seen)
    assert events.reserved_field_lines(text) == []


def test_a_tagged_chat_stream_numbers_its_chunks_and_the_done_sentinel(served, monkeypatch):
    port, _engine = served
    _trust_this_machine(monkeypatch)
    response, chunks, _ = timed_post(
        port, "/v1/chat/completions", json_body=CHAT_STREAM, headers=_headers(**{gp.ATTEMPT_HEADER: ATTEMPT})
    )
    text = body_of(chunks).decode()
    assert text.startswith(": ping\n\n")
    data_blocks = [b for b in text.split("\n\n") if b.startswith("data: ")]
    assert data_blocks[-1] == "data: [DONE]"
    assert text.endswith(f"data: [DONE]\n\n: ts-seq={len(data_blocks)}\n\n")
    assert [int(b.split("=", 1)[1]) for b in text.split("\n\n") if b.startswith(": ts-seq=")] == list(
        range(1, len(data_blocks) + 1)
    )
    assert response.headers["x-techsara-run"] == "none"


def test_a_re_attach_this_build_cannot_serve_is_a_404_that_never_reaches_the_engine(served, platform, monkeypatch):
    port, engine = served
    _trust_this_machine(monkeypatch)
    for extra in ({gp.RESUME_AFTER_HEADER: "4"}, {gp.ATTACH_JOB_HEADER: "k" * 64}):
        response, chunks, _ = timed_post(
            port, "/v1/responses", json_body=RESPONSES_STREAM, headers=_headers(**{gp.ATTEMPT_HEADER: ATTEMPT}, **extra)
        )
        assert response.status_code == 404
        assert response.headers["x-techsara-run"] == "none"
        assert json.loads(body_of(chunks))["error"]["code"] == "response_not_found"
    assert engine.calls == 0
    assert db.list_api_responses(platform["project"]["id"]) == []


def test_the_same_re_attach_from_an_untrusted_peer_is_an_ordinary_fresh_request(served, monkeypatch):
    port, engine = served
    monkeypatch.setattr(settings, "public_api_gateway_peers", (), raising=False)
    response, _chunks, _ = timed_post(
        port,
        "/v1/responses",
        json_body=RESPONSES_STREAM,
        headers=_headers(**{gp.ATTEMPT_HEADER: ATTEMPT, gp.RESUME_AFTER_HEADER: "4"}),
    )
    assert response.status_code == 200 and engine.calls == 1


def test_a_trusted_refusal_and_a_tagged_read_route_still_name_no_run(served, monkeypatch):
    port, _engine = served
    _trust_this_machine(monkeypatch)
    tagged = {gp.ATTEMPT_HEADER: ATTEMPT}
    with httpx.Client() as client:
        models = client.get(f"http://127.0.0.1:{port}/v1/models", headers=_headers(**tagged))
        unknown = client.post(
            f"http://127.0.0.1:{port}/v1/responses",
            json={"model": "no-such-model", "input": "hi"},
            headers=_headers(**tagged),
        )
        browser = client.post(
            f"http://127.0.0.1:{port}/v1/responses",
            json={"model": registry.TECHSARA_35B, "input": "hi"},
            headers=_headers(**tagged, Origin="https://app.example"),
        )
    assert models.status_code == 200 and models.headers["x-techsara-run"] == "none"
    assert unknown.status_code == 404 and unknown.headers["x-techsara-run"] == "none"
    assert "techsara" not in browser.headers.get("access-control-expose-headers", "").lower()


def test_the_trust_decision_is_made_once_and_a_route_cannot_read_the_raw_header():
    # A structural pin: the router reads the tag from request.state, and the
    # only reader of the raw names is gateway_protocol itself.
    import inspect

    source = inspect.getsource(public_router)
    for name in (gp.ATTEMPT_HEADER, gp.RESUME_AFTER_HEADER, gp.ATTACH_JOB_HEADER):
        assert name not in source
    assert "gateway_protocol.from_request(request)" in source
