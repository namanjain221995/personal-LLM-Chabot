"""JSON mode on /v1 (2026-10-27).

- `text.format` on /v1/responses and `response_format` on /v1/chat/completions
  reach the engine as its chat-completions `response_format`, and `text` (or
  nothing) sends no key at all;
- every malformed shape is a 400 naming the field the caller sent;
- a model other than techsara-35b refuses JSON mode instead of answering
  unconstrained;
- `chat_template_kwargs` is accepted only as `{"enable_thinking": false}`;
- the spec survives the durable store, and a JSON-mode run is never continued.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List

import pytest

from app import llm
from app.publicapi import durable, errors, models, planning, registry, router as public_router, streaming
from tests.test_llm_public_stream_kwargs import world  # noqa: F401 - fixture
from tests.test_publicapi_routes import (  # noqa: F401 - fixtures
    _auth,
    _pepper,
    api,
    platform,
)

SCHEMA = {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}
ENGINE_SCHEMA = {"type": "json_schema", "json_schema": {"name": "place", "schema": SCHEMA, "strict": True}}


def _chat(**fields: Any) -> Dict[str, Any]:
    body: Dict[str, Any] = {"model": registry.TECHSARA_35B, "messages": [{"role": "user", "content": "hi"}]}
    body.update(fields)
    return body


def _chat_format(**fields: Any):
    request, _ = public_router._from_chat_completions(_chat(**fields))
    return request.response_format()


def _refusal(call) -> errors.ApiError:
    with pytest.raises(errors.ApiError) as raised:
        call()
    assert raised.value.status == 400
    return raised.value


# ------------------------------------------------------------ translation --


def test_chat_json_object_and_json_schema_become_the_engine_response_format():
    assert _chat_format(response_format={"type": "json_object"}) == {"type": "json_object"}
    sent = {"type": "json_schema", "json_schema": {"name": "place", "schema": SCHEMA, "strict": True}}
    assert _chat_format(response_format=sent) == ENGINE_SCHEMA


def test_responses_text_format_becomes_the_same_engine_response_format():
    request = models.parse_responses_request(
        {
            "model": registry.TECHSARA_35B,
            "input": "hi",
            "text": {"format": {"type": "json_schema", "name": "place", "schema": SCHEMA, "strict": True}},
        }
    )
    assert request.response_format() == ENGINE_SCHEMA


def test_text_or_no_format_sends_nothing():
    assert _chat_format() is None
    assert _chat_format(response_format={"type": "text"}) is None
    request = models.parse_responses_request(
        {"model": registry.TECHSARA_35B, "input": "hi", "text": {"format": {"type": "text"}}}
    )
    assert request.response_format() is None


@pytest.mark.parametrize(
    "response_format, param",
    [
        ("json", "response_format"),
        ({"type": "xml"}, "response_format.type"),
        ({"type": "json_object", "json_schema": {}}, "response_format"),
        ({"type": "json_schema"}, "response_format.json_schema"),
        ({"type": "json_schema", "json_schema": {"schema": SCHEMA}}, "response_format"),
        ({"type": "json_schema", "json_schema": {"name": "bad name", "schema": SCHEMA}}, "response_format"),
        ({"type": "json_schema", "json_schema": {"name": "place"}}, "response_format"),
        ({"type": "json_schema", "json_schema": {"name": "place", "schema": SCHEMA, "extra": 1}},
         "response_format.json_schema"),
        ({"type": "json_schema", "json_schema": {"name": "place", "schema": {"d": "y" * 70_000}}},
         "response_format"),
    ],
)
def test_a_malformed_chat_response_format_is_a_400_naming_it(response_format, param):
    refusal = _refusal(lambda: _chat_format(response_format=response_format))
    assert refusal.param == param


def test_a_malformed_text_format_is_a_400_naming_it():
    for fmt in ({"type": "json_object", "name": "place"}, {"type": "json_schema", "schema": SCHEMA}):
        refusal = _refusal(
            lambda: models.parse_responses_request(
                {"model": registry.TECHSARA_35B, "input": "hi", "text": {"format": fmt}}
            )
        )
        assert refusal.param == "text.format"


def test_a_model_other_than_the_main_one_refuses_json_mode():
    vision = registry.resolve_public_model(registry.TECHSARA_8B_VISION, allowed=None, overrides=None)
    request, _ = public_router._from_chat_completions(
        _chat(model=vision.id, response_format={"type": "json_object"})
    )
    refusal = _refusal(
        lambda: planning.plan_generation(request, vision, endpoint=registry.ENDPOINT_CHAT_COMPLETIONS)
    )
    assert refusal.param == "response_format"
    plain, _ = public_router._from_chat_completions(_chat(model=vision.id))
    assert planning.plan_generation(plain, vision, endpoint=registry.ENDPOINT_CHAT_COMPLETIONS).response_format is None


def test_the_plan_and_the_spec_carry_the_format():
    main = registry.resolve_public_model(registry.TECHSARA_35B, allowed=None, overrides=None)
    request, _ = public_router._from_chat_completions(_chat(response_format={"type": "json_object"}))
    plan = planning.plan_generation(request, main, endpoint=registry.ENDPOINT_CHAT_COMPLETIONS)
    spec = streaming.spec_from_plan(plan, response_id="resp_json", created_at=1789200000)
    assert spec.response_format == {"type": "json_object"}


# --------------------------------------------------- chat_template_kwargs --


def test_chat_template_kwargs_is_accepted_only_as_thinking_off():
    public_router._from_chat_completions(_chat(chat_template_kwargs={"enable_thinking": False}))
    public_router._from_chat_completions(_chat(chat_template_kwargs={}))
    cases = [
        ({"enable_thinking": True}, "chat_template_kwargs.enable_thinking"),
        ({"enable_thinking": 0}, "chat_template_kwargs.enable_thinking"),
        ({"reasoning_effort": "high"}, "chat_template_kwargs"),
        ("off", "chat_template_kwargs"),
    ]
    for value, param in cases:
        refusal = _refusal(lambda: public_router._from_chat_completions(_chat(chat_template_kwargs=value)))
        assert refusal.param == param


def test_any_other_unknown_chat_field_is_still_refused():
    refusal = _refusal(lambda: public_router._from_chat_completions(_chat(top_p=0.9)))
    assert refusal.param == "top_p"


# ------------------------------------------------------------- the engine --


def _spec(**fields: Any) -> streaming.GenerationSpec:
    return streaming.GenerationSpec(
        response_id="resp_json",
        model=registry.TECHSARA_35B,
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=64,
        temperature=0.2,
        created_at=1789200000,
        item_id="msg_fixed",
        **fields,
    )


def _drain(spec: streaming.GenerationSpec) -> None:
    async def run():
        return "".join([frame async for frame in streaming.responses_sse(spec)])

    asyncio.run(run())


def test_the_engine_is_sent_the_response_format(world):
    _drain(_spec(response_format=ENGINE_SCHEMA))
    assert world.engine.calls, "the real stream_chat_events never reached the engine"
    assert world.engine.calls[0]["response_format"] == ENGINE_SCHEMA


def test_without_json_mode_the_engine_request_has_no_response_format(world):
    _drain(_spec())
    assert world.engine.calls
    assert "response_format" not in world.engine.calls[0]


def test_a_json_mode_run_is_never_continued():
    generation = streaming.Generation(
        _spec(response_format={"type": "json_object"}),
        messages=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": '{"ci'}],
        continue_final_message=True,
    )
    with pytest.raises(streaming.ContinuationUnsupported):
        generation._open_engine_stream()


def test_the_durable_spec_keeps_the_format_and_an_old_row_loads_without_it():
    stored, _ = durable.spec_to_json(_spec(response_format=ENGINE_SCHEMA))
    assert durable.spec_from_json(stored).response_format == ENGINE_SCHEMA
    stored.pop("response_format")
    assert durable.spec_from_json(stored).response_format is None


# ------------------------------------------------------------- the routes --


class _Recorder:
    """`llm.stream_chat_events`, stubbed, keeping what each call was given."""

    def __init__(self) -> None:
        self.kwargs: List[Dict[str, Any]] = []

    def __call__(self, messages, **kwargs):
        self.kwargs.append(kwargs)
        return self._run()

    async def _run(self):
        await asyncio.sleep(0)
        yield ("token", '{"city": "Paris"}')


@pytest.fixture()
def recorder(monkeypatch):
    fake = _Recorder()
    monkeypatch.setattr(llm, "stream_chat_events", fake)
    monkeypatch.setattr(llm, "reset_usage", lambda: None)
    monkeypatch.setattr(llm, "get_usage", lambda: None)
    return fake


def test_a_chat_request_in_json_mode_reaches_the_engine_with_its_format(api, recorder):
    body = _chat(
        response_format={"type": "json_object"},
        chat_template_kwargs={"enable_thinking": False},
        store=False,
    )
    response = api.post("/v1/chat/completions", json=body, headers=_auth())
    assert response.status_code == 200, response.text
    assert response.json()["choices"][0]["message"]["content"] == '{"city": "Paris"}'
    assert recorder.kwargs[-1]["response_format"] == {"type": "json_object"}


def test_a_responses_request_in_json_mode_reaches_the_engine_with_its_format(api, recorder):
    body = {
        "model": registry.TECHSARA_35B,
        "input": "hi",
        "store": False,
        "text": {"format": {"type": "json_schema", "name": "place", "schema": SCHEMA, "strict": True}},
    }
    response = api.post("/v1/responses", json=body, headers=_auth())
    assert response.status_code == 200, response.text
    assert recorder.kwargs[-1]["response_format"] == ENGINE_SCHEMA


def test_a_plain_request_sends_no_format(api, recorder):
    response = api.post("/v1/chat/completions", json=_chat(store=False), headers=_auth())
    assert response.status_code == 200, response.text
    assert "response_format" not in recorder.kwargs[-1]


def test_thinking_on_is_a_400_on_the_route(api, recorder):
    body = _chat(chat_template_kwargs={"enable_thinking": True})
    response = api.post("/v1/chat/completions", json=body, headers=_auth())
    assert response.status_code == 400
    assert response.json()["error"]["param"] == "chat_template_kwargs.enable_thinking"
    assert recorder.kwargs == []
