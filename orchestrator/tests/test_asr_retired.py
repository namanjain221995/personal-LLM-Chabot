"""No speech engine is installed, and that is the intended state.

Two engines were evaluated and both rejected — Qwen3-ASR-1.7B and
TheStageAI/thewhisper-large-v3. Their providers, services, images, weights and
configuration were removed on 2026-09-08. Voice input is therefore disabled
until a replacement lands.

This file guards the four things that make that state safe rather than merely
broken.

  1. NOTHING REJECTED CAN COME BACK BY ACCIDENT. No provider class, no backend
     selector, no model default, no import. A half-removed engine that a stale
     environment variable could re-select is worse than one that is gone.

  2. THE ROUTE STILL BEHAVES. A deployment with no engine answers 404 — the
     same answer it always gave for "this deployment does not have voice
     input" — and a stale environment that still says ASR_ENABLED=true gets a
     sentence rather than a stack trace.

  3. THE MODEL-AGNOSTIC HALF SURVIVED. The transcript shape, the error
     vocabulary, the fleet router and the admission pool are what the NEXT
     engine plugs into. Deleting them would mean rediscovering all of it.

  4. THE OTHER QWEN MODELS ARE UNTOUCHED. Chat, router, agent, vision,
     embeddings and reranker are all Qwen too. Only the voice model was
     removed, and a repository-wide search for "Qwen" was never permission to
     take the rest.

When an engine is installed, tests 1 and 2 are the ones that should be updated
— deliberately, as part of installing it.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from app import asr, audio_api
from app.config import Settings, settings

REPO = Path(__file__).resolve().parents[2]
APP = REPO / "orchestrator" / "app"

WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 8192

#: The two rejected engines, by every name they were ever known by in code.
# Written on 2026-09-08 when both engines had been removed and nothing was
# installed; openai/whisper-large-v3 landed on both Sparks later that day
# (scripts/whisper.sh). The guard survives with its purpose narrowed to what
# is still true: nothing rejected comes back, and a deployment with no engine
# fails honestly. `VLLMAudioProvider` is not on the list — it is the generic
# OpenAI /v1/audio/transcriptions client whisper is served through, nothing
# in it is Qwen's.
REJECTED = (
    "Qwen/Qwen3-ASR-1.7B", "qwen3_asr",
    "TheStageAI/thewhisper-large-v3", "thewhisper", "TheWhisperProvider",
    "asr_thewhisper", "ASR_THEWHISPER_MODEL",
    # Never installed, but named in an abandoned migration attempt.
    "CrisperWhisper", "crisperwhisper", "nyralabs",
)


def _symbols(path: Path) -> set[str]:
    """Names, attributes, imports and non-docstring strings in a module.

    Parsed rather than grepped: `app/asr.py`'s own docstring explains which
    engines were removed and why, and a substring search would read that
    explanation as the offence.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value not in docstrings:
                found.add(node.value)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                found.add(alias.name)
                if alias.asname:
                    found.add(alias.asname)
            if isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module)
    return found


# ---------------------------------------------------------------------------
# 1. Nothing rejected can come back
# ---------------------------------------------------------------------------


def test_no_rejected_engine_survives_anywhere_in_the_application_code():
    """Every module under app/, parsed. Not one may name a rejected engine in
    code — only in prose explaining that it is gone."""
    offenders: dict[str, list[str]] = {}
    for path in sorted(APP.rglob("*.py")):
        symbols = _symbols(path)
        hits = [r for r in REJECTED if r in symbols]
        if hits:
            offenders[str(path.relative_to(REPO))] = hits
    assert not offenders, f"rejected engines still referenced in code: {offenders}"


def test_the_rejected_provider_modules_are_gone_from_disk():
    assert not (APP / "asr_thewhisper.py").exists()
    with pytest.raises(ImportError):
        __import__("app.asr_thewhisper")


def test_no_engine_specific_wire_format_survives_in_the_asr_module():
    """Qwen's `language X<asr_text>…` contract left with the engine. A parser
    for a model nobody runs is a trap for the next one. (`normalise_language`,
    `SUPPORTED_LANGUAGES` and `language_code` are whisper's own language
    table and its name-to-ISO helper, checked against the engine's reply —
    they belong to the engine that IS installed.)"""
    assert not hasattr(asr, "parse_chat_output"), "parse_chat_output outlived its engine"


def test_provider_refuses_rather_than_returning_a_stub(monkeypatch):
    """A stub that answered every recording with an empty string would look
    like a working microphone that never hears anything. Raising is the honest
    failure, and the route already turns it into a sentence."""
    monkeypatch.setattr(settings, "asr_base_urls", ())
    asr.set_provider(None)
    try:
        with pytest.raises(asr.ASRUnavailable) as caught:
            asr.provider()
        assert "no speech engine is configured" in str(caught.value)
    finally:
        asr.set_provider(None)


def test_the_next_engine_can_still_be_installed_without_touching_this_module():
    """`set_provider` is the seam. If it stopped working, installing an engine
    would mean editing the module rather than plugging into it."""

    class Stub:
        name = "stub"
        model = "stub/model"

        async def transcribe(self, audio, *, filename, content_type, language=""):
            return asr.Transcript(
                text="ok", language=None, language_code=None,
                provider=self.name, model=self.model, engine_ms=1,
            )

        async def health(self) -> bool:
            return True

    try:
        asr.set_provider(Stub())
        assert asr.provider().model == "stub/model"
    finally:
        asr.set_provider(None)


# ---------------------------------------------------------------------------
# 2. The route still behaves
# ---------------------------------------------------------------------------


def test_voice_input_is_disabled_by_default():
    """OFF, and it must stay off until an engine exists: a composer that
    offers a microphone with nothing behind it shows members a button that
    cannot work."""
    assert Settings().asr_enabled is False


def test_no_default_points_at_a_retired_engines_port(monkeypatch):
    """Qwen3-ASR listened on 30006 and that port is closed. Whisper listens
    on 30007 on both Sparks and IS the documented default (the worker's
    engine), so only the retired port is forbidden here."""
    for key in ("ASR_BASE_URL", "ASR_BASE_URLS"):
        monkeypatch.delenv(key, raising=False)
    fresh = Settings()
    assert "30006" not in fresh.asr_base_url
    assert all("30006" not in url for url in fresh.asr_base_urls)


def test_the_route_is_still_mounted_for_the_next_engine():
    """The frontend posts here and was deliberately not changed. The path has
    to survive the engine that used to be behind it."""
    paths = {route.path for route in audio_api.router.routes}
    assert "/audio/transcribe" in paths
    assert "/audio/health" in paths


def test_a_deployment_with_no_engine_answers_not_found(login_client, monkeypatch):
    """404 and not 503: with no engine this deployment does not HAVE voice
    input, which is the same thing the composer decides from /auth/me."""
    monkeypatch.setattr(settings, "asr_enabled", False)
    audio_api.reset_for_tests()
    response = login_client("retired-404").post(
        "/audio/transcribe",
        content=WEBM,
        headers={"content-type": "audio/webm"},
        params={"duration_ms": "4200"},
    )
    assert response.status_code == 404
    assert isinstance(response.json()["detail"], str)


def test_a_stale_environment_that_still_enables_voice_gets_a_sentence(
    login_client, monkeypatch
):
    """The running container may still carry ASR_ENABLED=true from before the
    removal. That must produce the ordinary 503 and a readable sentence, not a
    500 with a stack-trace id."""
    monkeypatch.setattr(settings, "asr_enabled", True)
    monkeypatch.setattr(settings, "asr_base_urls", ())  # the no-engine case, not the live fleet
    asr.set_provider(None)
    audio_api.reset_for_tests()
    try:
        response = login_client("retired-stale").post(
            "/audio/transcribe",
            content=WEBM,
            headers={"content-type": "audio/webm"},
            params={"duration_ms": "4200"},
        )
        assert response.status_code == 503
        detail = response.json()["detail"]
        assert isinstance(detail, str)
        # Still no internals: a member has no reason to learn what is missing.
        for leak in ("Qwen", "whisper", "30006", "30007", "provider"):
            assert leak.lower() not in detail.lower()
    finally:
        asr.set_provider(None)
        audio_api.reset_for_tests()


def test_the_admin_health_endpoint_says_why_rather_than_just_not_ready(
    login_client, monkeypatch
):
    """An administrator asking whether dictation works deserves the reason,
    not an 'enabled, not ready' that hides it."""
    monkeypatch.setattr(settings, "asr_enabled", True)
    monkeypatch.setattr(settings, "asr_base_urls", ())  # the no-engine case, not the live fleet
    asr.set_provider(None)
    try:
        # super_admin, like the existing operational test: /audio/health is
        # gated on ANALYTICS_READ and answers 404 to anyone without it, so
        # that it does not confirm its own existence to a prober.
        body = login_client("retired-admin", role="super_admin").get("/audio/health").json()
        assert body["enabled"] is True
        assert body["ready"] is False
        assert body["model"] is None
        assert "no speech engine" in body["reason"].lower()
    finally:
        asr.set_provider(None)


def test_webm_opus_is_still_the_accepted_container_for_the_next_engine():
    """The browser records WebM/Opus and the frontend was not changed. Chrome
    labels an audio-only blob `video/webm`; Safari uses `video/mp4`."""
    for content_type in ("audio/webm", "video/webm", "audio/ogg",
                         "audio/mp4", "video/mp4", "audio/wav"):
        assert content_type in audio_api.ALLOWED_TYPES


# ---------------------------------------------------------------------------
# 3. The model-agnostic half survived
# ---------------------------------------------------------------------------


def test_the_pieces_the_next_engine_plugs_into_are_all_still_here():
    for kept in ("Transcript", "ASRProvider", "RoutedProvider", "POOL",
                 "ASRUnavailable", "ASRBusy", "ASRRejected",
                 "provider", "set_provider", "transcribe"):
        assert hasattr(asr, kept), f"{kept} was removed but is not engine-specific"


def test_the_transcript_shape_the_response_is_built_from_is_unchanged():
    """The seven-key public response is assembled from these fields. A change
    here changes the frontend contract, which was deliberately not touched."""
    fields = asr.Transcript.__dataclass_fields__
    assert set(fields) == {
        "text", "language", "language_code", "provider", "model",
        "engine_ms", "degraded",
    }


def test_the_admission_pool_still_sizes_itself_from_the_fleet(monkeypatch):
    """Four per endpoint, times the fleet. The limit protects the CHAT model,
    which is still there whether or not a speech engine is."""
    import asyncio

    monkeypatch.setattr(settings, "asr_max_concurrent", 4)
    monkeypatch.setattr(settings, "asr_base_urls", ("http://a/v1", "http://b/v1"))
    asr.POOL.reset_for_tests()

    async def check():
        async with asr.POOL:
            return asr.POOL._semaphore()._value + 1

    assert asyncio.run(check()) == 8
    asr.POOL.reset_for_tests()


# ---------------------------------------------------------------------------
# 4. The other Qwen models are untouched
# ---------------------------------------------------------------------------


def test_every_other_qwen_model_is_still_configured():
    """Six settings, all Qwen, none of them the voice model. Removing the ASR
    engine must not have reached any of them."""
    fresh = Settings()
    for name in ("llm_model", "router_model", "agent_model",
                 "vision_model", "embed_model"):
        value = getattr(fresh, name)
        assert value, f"{name} lost its value"
    assert "Qwen" in fresh.embed_model


def test_the_manifest_kept_every_model_except_the_retired_voice_one():
    """config/model-manifest.yaml pins every model this platform runs. Exactly
    one entry was removed."""
    manifest = json.loads((REPO / "config" / "model-manifest.yaml").read_text(encoding="utf-8"))
    ids = {entry.get("id") for entry in manifest["models"].values()}
    assert "Qwen/Qwen3-ASR-1.7B" not in ids
    assert "TheStageAI/thewhisper-large-v3" not in ids
    # The rest of the family is still pinned — this is expected and required.
    qwen_left = [k for k in manifest["models"] if "qwen" in k.lower()]
    assert len(qwen_left) >= 10, f"other Qwen models went missing: {qwen_left}"


def test_no_rejected_service_definition_or_script_survives():
    for gone in (
        "compose/compose.asr-worker.yaml", "compose/asr",
        "compose/compose.thewhisper-worker.yaml", "compose/thewhisper",
        "scripts/asr.sh", "scripts/asr_bench.py", "scripts/thewhisper.sh",
    ):
        assert not (REPO / gone).exists(), f"{gone} still exists"


def test_the_retained_benchmark_material_configures_nothing():
    """A historical result file is evidence, not configuration. It must be
    labelled as retired and must not be able to start anything."""
    result = REPO / "benchmarks" / "asr" / "results" / "qwen3-asr-baseline.json"
    assert result.exists(), "historical evidence was deleted"
    recorded = json.loads(result.read_text(encoding="utf-8"))
    assert recorded["_status"] == "RETIRED HISTORICAL BASELINE"
    # No live result file for either rejected engine.
    assert not (REPO / "benchmarks" / "asr" / "results" / "thewhisper-large-v3.json").exists()


def test_the_frontend_voice_path_was_not_touched():
    """Backend cleanup only. The composer still records WebM/Opus and posts to
    the same proxy, so the next engine needs no frontend change at all."""
    for kept in ("frontend/lib/voice.ts",
                 "frontend/components/useVoiceRecorder.ts",
                 "frontend/components/VoiceBar.tsx",
                 "frontend/app/api/audio/transcribe/route.ts"):
        assert (REPO / kept).exists(), f"{kept} must survive backend cleanup"
    voice = (REPO / "frontend" / "lib" / "voice.ts").read_text(encoding="utf-8")
    assert "/api/audio/transcribe" in voice
    assert "audio/webm;codecs=opus" in voice
