"""The Fast time-to-first-token tunables live in Settings (2026-09-13).

The performance work of 2026-09-13 (Fast/chat TTFT p50 1.46 s / p95 6.16 s
against an engine TTFT of 0.05-0.2 s) added ten tunables, and each module
read its own with `os.environ` and a private copy of config.py's parsing
helpers. That left the operator surface split: `Settings` did not list them,
and a module could change a default or a parse rule without config.py
noticing. These tests hold the two together:

1. every tunable is a `Settings` attribute with the default and type it ships;
2. the module that reads it, while it still parses the environment itself,
   ships the same default and parses every raw value exactly as `Settings`;
3. the module's getter returns the `Settings` value when the module's own
   default (its import-time constant, or the environment it reads per call)
   says something else — so a monkeypatched or operator-set `settings`
   attribute is the one that takes effect.

Rule 3 is not yet true for every module. `living_knowledge.py` already
prefers a `Settings` attribute of the same name; `health.py`,
`memory_semantic.py`, `sse.py` and `main.py` still read only the module
constant or the environment. Those cases (NOT_YET_WIRED) are strict xfails
keyed on the module's source naming the attribute: the moment the owner wires
the getter (and so names the attribute), the xfail lifts by itself and the
case becomes a hard requirement, and a getter that names it without honouring
it fails. The living_knowledge getters are not in that set, so un-wiring one
fails outright.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import re
from dataclasses import dataclass
from typing import Callable, Optional

import pytest

from app import health, main, memory_semantic, sse
from app import living_knowledge as lk
from app.config import Settings, settings


@dataclass(frozen=True)
class Tunable:
    env: str
    attr: str
    kind: type
    default: object
    #: The module that reads it, and the helper it parses with (None when
    #: the module parses inline, as sse.py does).
    module: object
    helper: Optional[str]


TUNABLES = (
    Tunable("KNOWLEDGE_FAST_TOPICAL_DEADLINE_S", "knowledge_fast_topical_deadline_s", float, 0.0, lk, "_env_float"),
    Tunable("KNOWLEDGE_FAST_TOPICAL_HIT_BUDGET_S", "knowledge_fast_topical_hit_budget_s", float, 0.0, lk, "_env_float"),
    Tunable("KNOWLEDGE_FAST_TOPICAL_PRECHECK", "knowledge_fast_topical_precheck", bool, True, lk, "_env_bool"),
    Tunable("FRESHNESS_FAST_SKIP_ROUTER", "freshness_fast_skip_router", bool, True, lk, "_env_bool"),
    Tunable("KNOWLEDGE_FAST_CONCURRENT_RETRIEVE", "knowledge_fast_concurrent_retrieve", bool, True, lk, "_env_bool"),
    Tunable("HEALTH_DEPENDENCY_CACHE_S", "health_dependency_cache_s", float, 4.0, health, "_env_float"),
    Tunable("CROSS_CHAT_EMBEDDINGS_CACHE_S", "cross_chat_embeddings_cache_s", float, 60.0, memory_semantic, "_env_float"),
    Tunable("CONTEXT_CONCURRENT_READS", "context_concurrent_reads", bool, True, main, "_env_bool"),
    Tunable("CONTEXT_READS_CONCURRENCY", "context_reads_concurrency", int, 6, main, "_env_int"),
    Tunable("CONTEXT_READS_PROCESS_LIMIT", "context_reads_process_limit", int, 6, main, "_env_int"),
    Tunable("CONTEXT_READS_CONCURRENT_MAX_TURNS", "context_reads_concurrent_max_turns", int, 0, main, "_env_int"),
    Tunable("SSE_COALESCE_MS", "sse_coalesce_ms", float, 25.0, sse, None),
)
BY_ENV = {t.env: t for t in TUNABLES}

#: Raw environment values each parse rule must agree on, including the ones
#: config.py treats specially (blank means the default) and ones it rejects.
RAW = {
    float: ["", "   ", "0", "0.25", "7", " 1.5 ", "-1", "abc"],
    bool: ["", " ", "1", "true", "TRUE", " yes ", "on", "0", "false", "off", "no", "maybe"],
    int: ["", " ", "8", " 3 ", "0", "-2", "2.5", "x"],
}


def _same(a, b) -> bool:
    """Equal, and of the same kind: floats within rounding (sse.py works in
    seconds, Settings in milliseconds), everything else exactly."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    return a == pytest.approx(b)


def _unset_all(monkeypatch) -> None:
    for t in TUNABLES:
        monkeypatch.delenv(t.env, raising=False)


def _env_call_defaults(module, env: str) -> list:
    """The default argument of every `_env_*("ENV", default)` call in the
    module's source — the module's own shipped default, read from the code
    rather than from its import-time value (which the environment moves)."""
    tree = ast.parse(inspect.getsource(module))
    return [
        ast.literal_eval(node.args[1])
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id.startswith("_env_")
        and len(node.args) >= 2
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == env
    ]


def _module_parse(t: Tunable, monkeypatch):
    """The module's own reading of the environment as it stands, in Settings
    units. Raises what the module raises."""
    if t.helper is not None:
        defaults = _env_call_defaults(t.module, t.env)
        if not defaults:
            pytest.skip(f"{t.module.__name__} no longer parses {t.env} itself; Settings is the only reader")
        return getattr(t.module, t.helper)(t.env, defaults[0])
    # sse.py parses inline in `_coalesce_seconds`, in seconds. Without a
    # Settings attribute to prefer, its fallback is that parse.
    monkeypatch.delattr(settings, t.attr, raising=False)
    return sse._coalesce_seconds() * 1000.0


# ── 1. Settings carries every tunable ────────────────────────────────────────


@pytest.mark.parametrize("env", list(BY_ENV))
def test_every_fast_ttft_tunable_is_a_settings_attribute_with_its_shipped_default_and_type(monkeypatch, env):
    t = BY_ENV[env]
    _unset_all(monkeypatch)
    fresh = Settings()
    assert hasattr(fresh, t.attr), f"Settings has no {t.attr} for {t.env}"
    value = getattr(fresh, t.attr)
    assert type(value) is t.kind, f"{t.attr} is {type(value).__name__}, the module reads a {t.kind.__name__}"
    assert value == t.default
    # The process-wide singleton carries it too (it is what getters read).
    assert hasattr(settings, t.attr)


@pytest.mark.parametrize("env", list(BY_ENV))
def test_the_module_that_reads_a_tunable_ships_the_same_default_as_settings(monkeypatch, env):
    t = BY_ENV[env]
    _unset_all(monkeypatch)
    module_default = _module_parse(t, monkeypatch)
    assert _same(module_default, getattr(Settings(), t.attr))
    assert _same(module_default, t.default)


# ── 2. the same parse rule ───────────────────────────────────────────────────


@pytest.mark.parametrize("env", list(BY_ENV))
def test_settings_parses_each_tunable_exactly_like_the_module_that_reads_it(monkeypatch, env):
    # One test per tunable, every raw value inside it: each test in this
    # suite pays the shared database fixture (~1 s), and 88 separate cases
    # measured 131 s on 2026-09-13.
    t = BY_ENV[env]
    for raw in RAW[t.kind]:
        _unset_all(monkeypatch)
        monkeypatch.setenv(t.env, raw)
        try:
            expected = _module_parse(t, monkeypatch)
        except ValueError:
            with pytest.raises(ValueError):
                Settings()
            continue
        got = getattr(Settings(), t.attr)
        assert type(got) is t.kind, f"{t.env}={raw!r}: Settings gave a {type(got).__name__}"
        assert _same(got, expected), f"{t.env}={raw!r}: Settings {got!r}, {t.module.__name__} {expected!r}"


# ── 3. a Settings value wins over the module's own default ───────────────────
#
# Each probe sets the module's own default (its constant and the environment)
# to `module_value`, the Settings attribute to `settings_value`, and returns
# the value the module actually USES, observed through its behaviour where
# there is no getter to call.


def _lk_getter(const: str, getter: str):
    def probe(monkeypatch, t, settings_value, module_value):
        monkeypatch.setattr(lk, const, module_value)
        return getattr(lk, getter)()

    return probe


def _health_ttl(monkeypatch, t, settings_value, module_value):
    """0.0 when every call probes, 60.0 when the second call is cached."""
    monkeypatch.setattr(health, "HEALTH_DEPENDENCY_CACHE_S", module_value)
    calls = []

    async def uncached():
        calls.append(1)
        return [], {}, []

    monkeypatch.setattr(health, "_dependency_probe_uncached", uncached)
    health.reset_dependency_cache()

    async def twice():
        await health._dependency_probe()
        await health._dependency_probe()

    try:
        asyncio.run(twice())
    finally:
        health.reset_dependency_cache()
    return 0.0 if len(calls) == 2 else 60.0


def _memory_ttl(monkeypatch, t, settings_value, module_value):
    """0.0 when every load reads the database, 60.0 when the second is cached."""
    monkeypatch.setattr(memory_semantic, "CROSS_CHAT_EMBEDDINGS_CACHE_S", module_value)
    fetches = []

    def fetch(user_id, model_id, exclude, limit):
        fetches.append(1)
        return [{"content": "an earlier answer"}]

    monkeypatch.setattr(memory_semantic.db, "fetch_message_embeddings", fetch)
    monkeypatch.setattr(memory_semantic, "_embeddings_fingerprint", lambda u, m, exclude="": (1, 1, 0))
    memory_semantic.invalidate_message_embeddings()
    try:
        memory_semantic._load_candidates(987654, "embed-model", None, 500)
        memory_semantic._load_candidates(987654, "embed-model", None, 500)
    finally:
        memory_semantic.invalidate_message_embeddings()
    return 0.0 if len(fetches) == 2 else 60.0


def _sse_window_ms(monkeypatch, t, settings_value, module_value):
    return sse._coalesce_seconds() * 1000.0


def _concurrent_reads(monkeypatch, t, settings_value, module_value):
    return main._context_concurrent_reads_enabled()


def _reads_in_flight(monkeypatch, t, settings_value, module_value):
    """The most context reads one turn had in flight at once, of eight."""

    async def run():
        reads = main._ContextReads(True)
        state = {"live": 0, "top": 0}

        async def read():
            state["live"] += 1
            state["top"] = max(state["top"], state["live"])
            await asyncio.sleep(0.01)
            state["live"] -= 1
            return 1

        for i in range(8):
            reads.start(f"r{i}", read)
        for i in range(8):
            await reads.get(f"r{i}", read)
        return state["top"]

    return asyncio.run(run())


def _reads_in_flight_across_turns(monkeypatch, t, settings_value, module_value):
    """The most reads running ahead at once across three turns of four reads."""
    monkeypatch.setattr(settings, "context_reads_concurrency", 8)

    async def run():
        state = {"live": 0, "top": 0}

        async def read():
            state["live"] += 1
            state["top"] = max(state["top"], state["live"])
            await asyncio.sleep(0.02)
            state["live"] -= 1
            return 1

        turns = [main._ContextReads(True) for _ in range(3)]
        for n, reads in enumerate(turns):
            for i in range(4):
                reads.start(f"t{n}r{i}", read)
        await asyncio.sleep(0.005)
        top = state["top"]
        for reads in turns:
            reads.close()
        return top

    return asyncio.run(run())


def _turns_reading_ahead_limit(monkeypatch, t, settings_value, module_value):
    """How many of four turns opened at once got to read ahead."""
    for leftover in list(main._turns_reading_ahead):
        leftover.close()
    turns = [main._ContextReads(True) for _ in range(4)]
    ahead = sum(1 for reads in turns if reads.concurrent)
    for reads in turns:
        reads.close()
    return ahead


#: env -> (probe, (settings_value, module_value) pairs, both directions).
PROBES: dict = {
    "KNOWLEDGE_FAST_TOPICAL_DEADLINE_S": (
        _lk_getter("_FAST_TOPICAL_DEADLINE_S", "fast_topical_deadline_s"), [(1.25, 0.3), (0.0, 5.0)]
    ),
    "KNOWLEDGE_FAST_TOPICAL_HIT_BUDGET_S": (
        _lk_getter("_FAST_TOPICAL_HIT_BUDGET_S", "fast_topical_hit_budget_s"), [(0.0, 1.5), (2.5, 0.0)]
    ),
    "KNOWLEDGE_FAST_TOPICAL_PRECHECK": (
        _lk_getter("_FAST_TOPICAL_PRECHECK", "fast_topical_precheck"), [(False, True), (True, False)]
    ),
    "FRESHNESS_FAST_SKIP_ROUTER": (_lk_getter("_FAST_SKIP_ROUTER", "fast_skip_router"), [(False, True), (True, False)]),
    "KNOWLEDGE_FAST_CONCURRENT_RETRIEVE": (
        _lk_getter("_FAST_CONCURRENT_RETRIEVE", "fast_concurrent_retrieve"), [(False, True), (True, False)]
    ),
    "HEALTH_DEPENDENCY_CACHE_S": (_health_ttl, [(0.0, 60.0), (60.0, 0.0)]),
    "CROSS_CHAT_EMBEDDINGS_CACHE_S": (_memory_ttl, [(0.0, 60.0), (60.0, 0.0)]),
    "CONTEXT_CONCURRENT_READS": (_concurrent_reads, [(False, True), (True, False)]),
    "CONTEXT_READS_CONCURRENCY": (_reads_in_flight, [(2, 6), (6, 2)]),
    "CONTEXT_READS_PROCESS_LIMIT": (_reads_in_flight_across_turns, [(2, 6), (6, 2)]),
    "CONTEXT_READS_CONCURRENT_MAX_TURNS": (_turns_reading_ahead_limit, [(1, 3), (3, 1)]),
    "SSE_COALESCE_MS": (_sse_window_ms, [(40.0, 10.0), (10.0, 40.0)]),
}


#: The modules that did NOT yet prefer the Settings attribute on 2026-09-13.
#: Only these may xfail; the living_knowledge getters already did, so a
#: regression there fails outright instead of turning into an xfail.
NOT_YET_WIRED = frozenset({
    "HEALTH_DEPENDENCY_CACHE_S",
    "CROSS_CHAT_EMBEDDINGS_CACHE_S",
    "CONTEXT_CONCURRENT_READS",
    "CONTEXT_READS_CONCURRENCY",
    "SSE_COALESCE_MS",
})


def _env_text(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _getter_cases():
    cases = []
    for t in TUNABLES:
        probe, pairs = PROBES[t.env]
        # Wired = the module names the attribute as `settings.<attr>` or as a
        # quoted name (a getattr). A bare substring is not enough: main.py's
        # `_context_concurrent_reads_enabled` contains `context_concurrent_reads`.
        wired = re.search(
            rf"settings\.{t.attr}\b|[\"']{t.attr}[\"']", inspect.getsource(t.module)
        ) is not None
        marks = (
            []
            if wired or t.env not in NOT_YET_WIRED
            else [
                pytest.mark.xfail(
                    strict=True,
                    reason=(
                        f"2026-09-13: {t.module.__name__} reads {t.env} only from its own "
                        f"constant/environment; its getter must prefer settings.{t.attr}"
                    ),
                )
            ]
        )
        for settings_value, module_value in pairs:
            cases.append(
                pytest.param(t.env, probe, settings_value, module_value, marks=marks,
                             id=f"{t.env}-settings={settings_value}-module={module_value}")
            )
    return cases


def test_every_tunable_has_a_getter_probe():
    assert set(PROBES) == set(BY_ENV)


@pytest.mark.parametrize("env,probe,settings_value,module_value", _getter_cases())
def test_a_module_getter_returns_the_settings_value_when_the_module_default_differs(
    monkeypatch, env, probe: Callable, settings_value, module_value
):
    t = BY_ENV[env]
    assert settings_value != module_value
    monkeypatch.setenv(t.env, _env_text(module_value))
    # raising=True: without the Settings attribute this is an AttributeError,
    # not a silently created one.
    monkeypatch.setattr(settings, t.attr, settings_value)
    used = probe(monkeypatch, t, settings_value, module_value)
    assert _same(used, settings_value), (
        f"{t.module.__name__} used {used!r} for {t.env}; settings.{t.attr} is {settings_value!r}, "
        f"its own default {module_value!r}"
    )
