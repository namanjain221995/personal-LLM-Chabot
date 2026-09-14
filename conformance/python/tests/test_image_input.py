"""Image input on /v1/responses and /v1/chat/completions (CONTRACT-3 §8.1, §8.2).
Planned 2026-09-13.

The image is a generated solid-red square, and the model is asked its
colour: an answer naming red proves the pixels reached the model, which a
200 alone does not (a silently dropped image still answers 200)."""
from __future__ import annotations

import openai
import pytest

from techsara_conformance import asserts, media

pytestmark = pytest.mark.feature("image_input")
QUESTION = "What single colour fills this image? Answer with one word."


@pytest.fixture(scope="module")
def red_png_url() -> str:
    return media.data_url(media.solid_png(rgb=(220, 20, 20)), "image/png")


@pytest.fixture(scope="module")
def valid_image_accepted(make_module_client, target, red_png_url):
    """A refusal test only proves something when the SAME request with a valid
    data-URL image is accepted: a server that refuses every content-part list
    would otherwise "pass" all the refusals below (observed on the 2026-09-13
    e2e build, where `content` must be a string). One 1-token generation."""
    client = make_module_client()
    try:
        client.responses.create(model=target.models["chat"], input=_responses_input(red_png_url), max_output_tokens=1)
    except openai.APIStatusError as exc:
        return False, f"HTTP {exc.status_code} {exc.body!r}"
    return True, ""


def _require_valid_image_accepted(valid_image_accepted) -> None:
    ok, detail = valid_image_accepted
    assert ok, f"a VALID data-URL image is refused too ({detail}), so this refusal proves nothing"


def _responses_input(image_url: str, text: str = QUESTION):
    return [{"role": "user", "content": [{"type": "input_text", "text": text}, {"type": "input_image", "image_url": image_url, "detail": "auto"}]}]


@pytest.mark.parametrize("role", ["chat", "vision"])
def test_a_data_url_image_reaches_the_vision_model_through_responses(client, target, red_png_url, role):
    response = client.responses.create(
        model=target.models[role], input=_responses_input(red_png_url), max_output_tokens=target.small_output_tokens, temperature=0
    )
    assert response.status == "completed"
    assert "red" in response.output_text.lower(), f"{target.models[role]} answered {response.output_text!r}"


def test_a_data_url_image_reaches_the_model_through_chat_completions(client, target, red_png_url):
    completion = client.chat.completions.create(
        model=target.models["chat"],
        messages=[{"role": "user", "content": [{"type": "text", "text": QUESTION}, {"type": "image_url", "image_url": {"url": red_png_url, "detail": "auto"}}]}],
        max_tokens=target.small_output_tokens,
        temperature=0,
    )
    assert "red" in (completion.choices[0].message.content or "").lower(), completion.choices[0].message.content


def test_ocr_takes_exactly_one_image_and_answers_without_a_prompt(client, target, red_png_url):
    response = client.responses.create(
        model=target.models["ocr"],
        input=[{"role": "user", "content": [{"type": "input_image", "image_url": red_png_url}]}],
        max_output_tokens=64,
    )
    assert response.status == "completed"
    with pytest.raises(openai.BadRequestError) as caught:
        client.responses.create(model=target.models["ocr"], input="no image here", max_output_tokens=16)
    asserts.sdk_error(caught.value, code="invalid_request_error")


@pytest.mark.parametrize("url", ["http://127.0.0.1/x.png", "https://example.com/x.png", "file:///etc/hosts"], ids=["http", "https", "file"])
def test_a_non_data_image_url_is_refused_before_any_engine_sees_it(client, target, url, valid_image_accepted):
    _require_valid_image_accepted(valid_image_accepted)
    with pytest.raises(openai.BadRequestError) as caught:
        client.responses.create(model=target.models["chat"], input=_responses_input(url), max_output_tokens=4)
    asserts.sdk_error(caught.value, code="invalid_request_error")


def test_an_image_whose_bytes_do_not_match_its_declared_type_is_refused(client, target, valid_image_accepted):
    _require_valid_image_accepted(valid_image_accepted)
    lying = media.data_url(media.solid_png(), "image/jpeg")
    with pytest.raises(openai.BadRequestError) as caught:
        client.responses.create(model=target.models["chat"], input=_responses_input(lying), max_output_tokens=4)
    asserts.sdk_error(caught.value, code="invalid_request_error")


def test_an_image_on_a_non_user_message_is_refused(client, target, red_png_url, valid_image_accepted):
    _require_valid_image_accepted(valid_image_accepted)
    with pytest.raises(openai.BadRequestError) as caught:
        client.responses.create(
            model=target.models["chat"],
            input=[
                {"role": "system", "content": [{"type": "input_image", "image_url": red_png_url}]},
                {"role": "user", "content": QUESTION},
            ],
            max_output_tokens=4,
        )
    asserts.sdk_error(caught.value, code="invalid_request_error")
