"""The PUBLIC_API_* settings the six-model and security waves read are
declared on `Settings` (integration, 2026-09-13).

Before the declaration every reader fell back to os.environ with config.py's
own `_int` / `_float` rule, so what is pinned here is that declaring them
changed nothing an operator could observe:

1. a fresh `Settings()` on a clean environment has each attribute, with the
   exact type and the default the design and CONTRACT §12.4 give;
2. every reader in `app/` that names the variable ships that same default
   (read from its source with `ast`, so a reader changed on its own fails here);
3. the declared attribute is what the readers now return;
4. the environment is parsed by config.py's rule: blank is the default, a
   value is taken, and a malformed value fails at start-up.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from app import config as config_module
from app.apiplatform.webhooks import queue as webhook_queue
from app.config import Settings, settings
from app.publicapi import capacity, endpoint_models, models, planning, registry

APP = Path(__file__).resolve().parents[1] / "app"

#: name -> (type, default). Dynamic defaults are resolved in `_expected`.
#:
#: The settings the no-timeout design retires (config.RETIRED_PUBLIC_API_SETTINGS:
#: the wall-clock formula, the gate waits, the audio length cap) were declared
#: here by PR #65 and are deliberately absent since the merge of 2026-09-14 —
#: `test_retired_settings_are_not_declared` pins that.
DECLARED: Dict[str, Tuple[type, Any]] = {
    "PUBLIC_API_MAX_OUTPUT_TOKENS": (int, 1_000_000),
    "PUBLIC_API_MAIN_SOLO_OUTPUT_TOKENS": (int, 800_000),
    "PUBLIC_API_MAIN_LONG_MAX_CONCURRENT": (int, 1),
    "PUBLIC_API_MAIN_EXTENDED_OUTPUT_TOKENS": (int, "public_api_default_max_output_tokens"),
    "PUBLIC_API_MAIN_EXTENDED_MAX_CONCURRENT": (int, 2),
    "PUBLIC_API_YIELD_TO_CHAT_MAX_WAIT_S": (float, 10.0),
    "PUBLIC_API_ROUTER_CONTEXT_TOKENS": (int, 24_576),
    "PUBLIC_API_ROUTER_MAX_CONCURRENT": (int, 4),
    "PUBLIC_API_ROUTER_KV_BUDGET_TOKENS": (int, 24_576),
    "PUBLIC_API_OCR_CONTEXT_TOKENS": (int, 8192),
    "PUBLIC_API_OCR_MAX_CONCURRENT": (int, 2),
    "PUBLIC_API_EMBED_CONTEXT_TOKENS": (int, 4096),
    "PUBLIC_API_EMBED_MAX_CONCURRENT": (int, 2),
    "PUBLIC_API_EMBED_KV_BUDGET_TOKENS": (int, 8192),
    "PUBLIC_API_EMBED_MAX_INPUTS": (int, 2048),
    "PUBLIC_API_RERANK_CONTEXT_TOKENS": (int, 4096),
    "PUBLIC_API_RERANK_MAX_CONCURRENT": (int, 2),
    "PUBLIC_API_RERANK_KV_BUDGET_TOKENS": (int, 8192),
    "PUBLIC_API_RERANK_MAX_DOCUMENTS": (int, 1000),
    "PUBLIC_API_ASR_MAX_CONCURRENT": (int, 1),
    "PUBLIC_API_MAX_AUDIO_BYTES": (int, 93_323_264),
    "PUBLIC_API_MAX_AUDIO_BODY_BYTES": (int, 94_371_840),
    "PUBLIC_API_MAX_MEDIA_BODY_BYTES": (int, 20_971_520),
    "PUBLIC_API_MAX_IMAGE_BYTES": (int, 10_485_760),
    "PUBLIC_API_WEBHOOK_DELIVERY_RETENTION_DAYS": (int, 30),
    "PUBLIC_API_WEBHOOK_DISABLED_GRACE_SECONDS": (float, 3600.0),
    "PUBLIC_API_WEBHOOK_MAX_PENDING_PER_ENDPOINT": (int, 1000),
}

#: Readers whose default is computed rather than a literal: the literal the
#: expression stands for on a clean environment.
_DYNAMIC_READER_DEFAULTS = {
    ("PUBLIC_API_MAIN_EXTENDED_OUTPUT_TOKENS", "registry.public_default_max_output_tokens()"): 8192,
}


def _clean_env(monkeypatch) -> None:
    for name in list(DECLARED) + ["PUBLIC_API_DEFAULT_MAX_OUTPUT_TOKENS"]:
        monkeypatch.delenv(name, raising=False)


def _expected(fresh: Settings, default: Any) -> Any:
    return getattr(fresh, default) if isinstance(default, str) else default


def test_a_fresh_settings_declares_each_public_api_setting_with_its_type_and_default(monkeypatch):
    _clean_env(monkeypatch)
    fresh = Settings()
    for name, (kind, default) in DECLARED.items():
        attribute = name.lower()
        assert hasattr(fresh, attribute), name
        value = getattr(fresh, attribute)
        assert type(value) is kind, (name, type(value))
        assert value == _expected(fresh, default), (name, value)


def test_a_dynamic_default_follows_the_setting_it_is_defined_by(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("PUBLIC_API_DEFAULT_MAX_OUTPUT_TOKENS", "4096")
    fresh = Settings()
    assert fresh.public_api_main_extended_output_tokens == 4096


def _module_constants(tree: ast.Module) -> Dict[str, Any]:
    constants: Dict[str, Any] = {}
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if value is None:
                continue
            try:
                evaluated = eval(compile(ast.Expression(value), "<const>", "eval"), {"__builtins__": {}}, dict(constants))
            except Exception:  # noqa: BLE001 - not a constant expression
                continue
            if isinstance(evaluated, (int, float)):
                for target in targets:
                    if isinstance(target, ast.Name):
                        constants[target.id] = evaluated
    return constants


def _reader_defaults() -> Dict[str, List[Tuple[str, Any]]]:
    """name -> [(file:line, default)] for every call in app/ that passes the
    variable name as a string followed by a default."""
    found: Dict[str, List[Tuple[str, Any]]] = {name: [] for name in DECLARED}
    for path in APP.rglob("*.py"):
        if path.name == "config.py" and path.parent == APP:
            continue
        source = path.read_text("utf-8")
        if "PUBLIC_API_" not in source:
            continue
        tree = ast.parse(source)
        constants = _module_constants(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            args = list(node.args)
            for index, arg in enumerate(args):
                if not (isinstance(arg, ast.Constant) and arg.value in DECLARED):
                    continue
                if index + 1 >= len(args):
                    continue
                name, default_node = arg.value, args[index + 1]
                where = f"{path.relative_to(APP)}:{node.lineno}"
                segment = ast.unparse(default_node)
                if (name, segment) in _DYNAMIC_READER_DEFAULTS:
                    found[name].append((where, _DYNAMIC_READER_DEFAULTS[(name, segment)]))
                    continue
                try:
                    value = eval(
                        compile(ast.Expression(default_node), "<default>", "eval"),
                        {"__builtins__": {}},
                        dict(constants),
                    )
                except Exception:  # noqa: BLE001
                    found[name].append((where, f"<unresolved {segment}>"))
                    continue
                found[name].append((where, value))
    return found


def test_every_reader_in_the_app_ships_the_default_config_py_declares(monkeypatch):
    _clean_env(monkeypatch)
    fresh = Settings()
    readers = _reader_defaults()
    for name, (_, default) in DECLARED.items():
        expected = _expected(fresh, default)
        assert readers[name], f"{name} is declared but nothing in app/ reads it"
        for where, value in readers[name]:
            assert value == expected, (name, where, value, expected)


def test_the_readers_return_the_declared_attribute(monkeypatch):
    checks = [
        ("public_api_max_output_tokens", 123_456, registry.max_output_tokens_setting),
        ("public_api_yield_to_chat_max_wait_s", 1.25, capacity.yield_to_chat_max_wait_s),
        ("public_api_main_extended_output_tokens", 16_384, planning.main_extended_output_tokens),
        ("public_api_main_solo_output_tokens", 900_000, planning.main_solo_output_tokens),
        ("public_api_max_media_body_bytes", 5 * 1024 * 1024, models.max_media_body_bytes),
        ("public_api_max_image_bytes", 2 * 1024 * 1024, models.max_image_bytes),
        ("public_api_max_audio_body_bytes", 9 * 1024 * 1024, models.max_audio_body_bytes),
        ("public_api_embed_max_inputs", 17, endpoint_models.embed_max_inputs),
        ("public_api_rerank_max_documents", 9, endpoint_models.rerank_max_documents),
        ("public_api_max_audio_bytes", 1024 * 1024, endpoint_models.max_audio_bytes),
        ("public_api_webhook_delivery_retention_days", 7, webhook_queue.delivery_retention_days),
        ("public_api_webhook_disabled_grace_seconds", 60.0, webhook_queue.disabled_grace_seconds),
        ("public_api_webhook_max_pending_per_endpoint", 25, webhook_queue.max_pending_per_endpoint),
    ]
    for attribute, value, reader in checks:
        monkeypatch.setattr(settings, attribute, value)
        # The variable no longer decides once the attribute is declared.
        monkeypatch.setenv(attribute.upper(), "1")
        assert reader() == value, attribute
    monkeypatch.setattr(settings, "public_api_router_max_concurrent", 3)
    monkeypatch.setattr(settings, "public_api_router_kv_budget_tokens", 12_000)
    monkeypatch.setattr(settings, "public_api_main_long_max_concurrent", 2)
    monkeypatch.setattr(settings, "public_api_asr_max_concurrent", 2)
    assert capacity._config(capacity.GATE_ROUTER).max_concurrent == 3
    assert capacity._config(capacity.GATE_ROUTER).budget_tokens == 12_000
    assert capacity._config(capacity.GATE_MAIN_LONG).max_concurrent == 2
    assert capacity._config(capacity.GATE_ASR).max_concurrent == 2


@pytest.mark.parametrize("name", sorted(DECLARED))
def test_the_environment_is_parsed_by_config_pys_own_rule(monkeypatch, name):
    _clean_env(monkeypatch)
    kind, default = DECLARED[name]
    attribute = name.lower()
    monkeypatch.setenv(name, "  ")
    blank = Settings()
    assert getattr(blank, attribute) == _expected(blank, default)
    monkeypatch.setenv(name, "42")
    assert getattr(Settings(), attribute) == kind(42)
    helper = config_module._int if kind is int else config_module._float
    assert getattr(Settings(), attribute) == helper(name, 0)
    monkeypatch.setenv(name, "lots")
    with pytest.raises(ValueError):
        Settings()


def test_retired_settings_are_not_declared(monkeypatch):
    """The no-timeout design retires these; declaring one would make a value in
    .env silently honoured instead of warned about (config.warn_retired_settings)."""
    _clean_env(monkeypatch)
    fresh = Settings()
    for name in config_module.RETIRED_PUBLIC_API_SETTINGS:
        if name == "PUBLIC_API_IDEMPOTENCY_IN_FLIGHT_LEASE_SECONDS":
            # Declared before either release, and read by idempotency.py as an
            # attribute; STILL_READ_RETIRED_SETTINGS names it.
            continue
        assert not hasattr(fresh, name.lower()), name
