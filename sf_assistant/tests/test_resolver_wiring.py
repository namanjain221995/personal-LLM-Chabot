"""The term resolver's wiring (docs/resolver_integration.md), step "Build": the SFA_RESOLVER switch, the knowledge
resolver of pipeline.build, the full resolver of open_records and their start-up lines.

The switch is on by default since 2026-10-27 (unset or empty: on). With it off (``SFA_RESOLVER=off``) nothing is
built: ``registry.resolver`` and ``catalog.resolver`` are None, the start-up lines and every tool are exactly as before
(the parity suite and the other test files cover that). These tests also run each switch-dependent expectation with
the switch on (the "flag-on twins") and check that the default is on; every test sets the switch itself (or clears it
for the default), so the result does not depend on the developer's shell. Fake data only: tests/warehouse_fixture.py,
tests/fixtures/Internal_Interview__c.json and a made-up word list.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import sf_assistant.pipeline as pipeline
from sf_assistant import config as C
from sf_assistant.config import ResolverConfig, load_records_config
from sf_assistant.knowledge.resolver import KINDS, Resolver

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_JSON = ROOT / "tests" / "fixtures" / "Internal_Interview__c.json"
META = set(KINDS) - {"person"}


@pytest.fixture(autouse=True)
def _switch_unset(monkeypatch):
    for name in C.RESOLVER_ENV_VARS.values():
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def words(tmp_path) -> Path:
    """A made-up stand-in for the English word list (generic words only)."""
    p = tmp_path / "words.txt"
    p.write_text("\n".join("assigned scheduled complete completed interview interviews status the and how many "
                           "records show list count open".split()) + "\n", encoding="utf-8")
    return p


def _on(words: Path) -> ResolverConfig:
    return ResolverConfig(enabled=True, lexicon=(str(words),))


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    pytest.importorskip("duckdb")
    from warehouse_fixture import make_knowledge, make_warehouse

    return make_warehouse(tmp_path), make_knowledge(tmp_path)


def _rcfg(wh: Path):
    return load_records_config(warehouse=str(wh), today=dt.date(2026, 10, 13))


def _key(t) -> tuple:
    return (t.kind, t.target, t.text, t.object, t.source, t.weight)


# ------------------------------------------------------------------------------------------- pipeline.build
def test_build_switch_off_builds_nothing(tmp_path, monkeypatch):
    _wh, kn = _fixture(tmp_path)
    monkeypatch.setenv("SFA_RESOLVER", "off")
    for paths in ([str(FIXTURE_JSON)], sorted(str(p) for p in kn.glob("*.json"))):
        _cfg, _objs, reg, _system, _llm = pipeline.build(paths)
        assert reg.resolver is None and reg.resolver_lines == []
        assert all(r.resolver is None for r in getattr(reg, "registries", {}).values())
        _cfg, _objs, reg, _system, _llm = pipeline.build(paths, C.load_settings(records=False))
        assert reg.resolver is None and reg.resolver_lines == []


def test_build_switch_on_gives_the_knowledge_resolver(tmp_path, words, monkeypatch):
    _wh, kn = _fixture(tmp_path)
    paths = sorted(str(p) for p in kn.glob("*.json"))
    monkeypatch.setenv("SFA_RESOLVER", "on")
    monkeypatch.setenv("SFA_LEXICON", str(words))
    settings = C.load_settings(records=False)
    assert settings.resolver == _on(words)
    for reg in (pipeline.build(paths, settings)[2], pipeline.build(paths)[2]):     # Settings, or the environment
        assert isinstance(reg.resolver, Resolver)
        assert {t.kind for t in reg.resolver.terms} <= META                          # never a person ...
        assert not any(t.source.startswith("stored") for t in reg.resolver.terms)    # ... nor a stored value
        assert all(r.resolver is reg.resolver for r in reg.registries.values())      # one resolver, shared
        assert len(reg.resolver_lines) == 1
        assert reg.resolver_lines[0].startswith("spelling help on (knowledge files): ")
        assert "word list 16 words" in reg.resolver_lines[0] and "person" not in reg.resolver_lines[0]
        assert reg.resolver.resolve_term("Interviw", kinds=["object"]).decision in ("corrected", "ambiguous")
    single = pipeline.build([str(FIXTURE_JSON)], settings)[2]
    assert isinstance(single.resolver, Resolver)
    assert {t.object for t in single.resolver.terms} == {"Internal_Interview__c"}


@pytest.mark.parametrize("raw", [None, "", "  "])
def test_build_switch_unset_or_empty_is_on_by_default(tmp_path, words, monkeypatch, raw):
    """The default since 2026-10-27: without SFA_RESOLVER (or with it empty) the knowledge resolver is built, as with
    SFA_RESOLVER=on."""
    _wh, kn = _fixture(tmp_path)
    paths = sorted(str(p) for p in kn.glob("*.json"))
    if raw is not None:
        monkeypatch.setenv("SFA_RESOLVER", raw)
    monkeypatch.setenv("SFA_LEXICON", str(words))
    settings = C.load_settings(records=False)
    assert settings.resolver == _on(words) and not settings.resolver.problem
    for reg in (pipeline.build(paths, settings)[2], pipeline.build(paths)[2]):     # Settings, or the environment
        assert isinstance(reg.resolver, Resolver) and len(reg.resolver_lines) == 1
        assert reg.resolver_lines[0].startswith("spelling help on (knowledge files): ")


def test_build_switch_problems_leave_it_off_and_say_why(tmp_path, monkeypatch):
    paths = [str(FIXTURE_JSON)]
    monkeypatch.setenv("SFA_RESOLVER", "maybe")
    reg = pipeline.build(paths)[2]
    assert reg.resolver is None and reg.resolver_lines == ["spelling help off: SFA_RESOLVER must be off or on"]
    monkeypatch.setenv("SFA_RESOLVER", "on")
    monkeypatch.setenv("SFA_LEXICON", str(tmp_path / "missing.txt"))
    reg = pipeline.build(paths)[2]
    assert reg.resolver is None and len(reg.resolver_lines) == 1
    assert reg.resolver_lines[0].startswith("spelling help off: no English word list could be read (")
    assert "missing.txt" in reg.resolver_lines[0]


# ------------------------------------------------------------------------------------------- open_records
def test_open_records_switch_off_builds_nothing(tmp_path, monkeypatch):
    from sf_assistant.tools.open_records import open_records

    wh, kn = _fixture(tmp_path)
    monkeypatch.setenv("SFA_RESOLVER", "off")
    for resolver in (None, ResolverConfig(enabled=False)):
        t = open_records(_rcfg(wh), str(kn), resolver)
        try:
            assert t.catalog.resolver is None and t.resolver_lines == []
        finally:
            t.close()


def test_open_records_switch_on_gives_the_full_resolver_through_the_guard(tmp_path, words):
    """The vocabulary is read through the warehouse's guarded connection with the catalog's policy and gives exactly
    the terms of the offline path build (its own read-only connection)."""
    from sf_assistant.engine.vocabulary import build_vocabulary
    from sf_assistant.knowledge.resolver import build
    from sf_assistant.tools.open_records import open_records

    wh, kn = _fixture(tmp_path)
    t = open_records(_rcfg(wh), str(kn), _on(words))
    try:
        r = t.catalog.resolver
        assert isinstance(r, Resolver)
        kinds = {x.kind for x in r.terms}
        assert "person" in kinds and any(x.source.startswith("stored") for x in r.terms)
        assert r.info["snapshot"]["query_errors"] == 0 and r.info["snapshot"]["truncated"] == 0
        assert len(t.resolver_lines) == 1
        line = t.resolver_lines[0]
        assert line.startswith("spelling help on (knowledge files + snapshot): ") and "person " in line
        names = {x.text for x in r.terms if x.kind == "person"}
        assert names and not any(n in line for n in names)            # counts only, never a name
        app = [_key(x) for x in r.terms]
        info = dict(r.info)
    finally:
        t.close()
    offline = build_vocabulary(str(kn), str(wh), lexicon=[str(words)])
    assert info["terms"] == offline.info["terms"]
    assert app == [_key(x) for x in build(offline).terms]


def test_open_records_reads_the_switch_from_the_environment(tmp_path, words, monkeypatch):
    from sf_assistant.tools.open_records import open_records

    wh, kn = _fixture(tmp_path)
    monkeypatch.setenv("SFA_LEXICON", str(words))
    for raw in (None, "on"):            # unset: on by default (2026-10-27), as with on
        if raw is not None:
            monkeypatch.setenv("SFA_RESOLVER", raw)
        t = open_records(_rcfg(wh), str(kn))
        try:
            assert isinstance(t.catalog.resolver, Resolver), raw
            assert len(t.resolver_lines) == 1 and t.resolver_lines[0].startswith("spelling help on "), raw
        finally:
            t.close()
    monkeypatch.setenv("SFA_RESOLVER", "off")
    t = open_records(_rcfg(wh), str(kn))
    try:
        assert t.catalog.resolver is None and t.resolver_lines == []
    finally:
        t.close()
    monkeypatch.setenv("SFA_RESOLVER", "maybe")
    t = open_records(_rcfg(wh), str(kn))
    try:
        assert t.catalog.resolver is None
        assert t.resolver_lines == ["spelling help off: SFA_RESOLVER must be off or on"]
    finally:
        t.close()


def test_mcp_server_takes_the_switch_from_its_settings_for_both_sides(tmp_path, words, monkeypatch):
    """One source of truth for the MCP server too: settings.resolver reaches its record tools at every (re)open
    and its knowledge registry (the knowledge resolver, as pipeline.build gives the knowledge agent); the env is not
    read again. Off or not given: nothing is built."""
    from sf_assistant.server import data as D
    from sf_assistant.tools import open_records as O

    wh, kn = _fixture(tmp_path)
    seen: list = []
    real = O.open_records
    monkeypatch.setattr(O, "open_records", lambda rcfg, kdir, resolver=None: (seen.append(resolver),
                                                                              real(rcfg, kdir, resolver))[1])
    monkeypatch.setenv("SFA_RESOLVER", "on")              # ignored: the settings decide
    rec = D.Records(_rcfg(wh), str(kn), resolver=ResolverConfig(enabled=False))
    try:
        assert seen == [ResolverConfig(enabled=False)] and rec.tools.catalog.resolver is None
    finally:
        rec.close()
    monkeypatch.setenv("SFA_RESOLVER", "off")             # ignored too
    rec = D.Records(_rcfg(wh), str(kn), resolver=_on(words))
    try:
        assert seen[-1] == _on(words) and isinstance(rec.tools.catalog.resolver, Resolver)
    finally:
        rec.close()
    k = D.load_knowledge(FIXTURE_JSON.parent, FIXTURE_JSON, root=str(tmp_path))
    try:
        assert getattr(k.registry, "resolver", None) is None and k.resolver_lines == []
    finally:
        k.close()
    k = D.load_knowledge(FIXTURE_JSON.parent, FIXTURE_JSON, root=str(tmp_path), resolver=_on(words))
    try:
        assert isinstance(k.registry.resolver, Resolver)
        assert k.resolver_lines[0].startswith("spelling help on (knowledge files): ")
    finally:
        k.close()
    k = D.load_knowledge(FIXTURE_JSON.parent, FIXTURE_JSON, root=str(tmp_path),
                         resolver=ResolverConfig(problem="SFA_RESOLVER must be off or on"))
    try:
        assert k.registry.resolver is None and k.resolver_lines == ["spelling help off: SFA_RESOLVER must be off or on"]
    finally:
        k.close()


def test_grading_replays_with_the_spelling_help_off_whatever_the_switch(tmp_path, monkeypatch):
    """The evaluation's reference replay (golden tool calls) never follows the switch being evaluated."""
    from sf_assistant.evaluation import runner
    from sf_assistant.tools import open_records as O

    wh, _kn = _fixture(tmp_path)
    seen: list = []
    monkeypatch.setattr(O, "open_records", lambda rcfg, kdir, resolver=None: seen.append(resolver) or None)
    for raw in (None, "on", "off"):     # unset: on by default (2026-10-27); the replay is off all the same
        if raw is None:
            monkeypatch.delenv("SFA_RESOLVER", raising=False)
        else:
            monkeypatch.setenv("SFA_RESOLVER", raw)
        seen.clear()
        con, _tools, _notes = runner.open_grading(str(wh), True, "2026-10-13")
        try:
            assert seen == [ResolverConfig(enabled=False)] and seen[0].enabled is False, raw
        finally:
            if con is not None:
                con.close()


def test_a_cut_snapshot_statement_is_counted_and_warned_about(tmp_path, words, monkeypatch):
    import sf_assistant.tools.open_records as O

    wh, kn = _fixture(tmp_path)
    monkeypatch.setattr(O, "VOCAB_MAX_ROWS", 1)
    t = O.open_records(_rcfg(wh), str(kn), _on(words))
    try:
        n = t.catalog.resolver.info["snapshot"]["truncated"]
        assert n > 0
        assert t.resolver_lines[1] == (f"WARNING spelling help: {n} snapshot statement(s) hit the row or size limit; "
                                       "some stored values or names are left out")
    finally:
        t.close()


def test_a_failing_build_leaves_the_resolver_off_and_the_records_on(tmp_path, words, monkeypatch):
    import sf_assistant.engine.vocabulary as V
    from sf_assistant.tools.open_records import open_records

    wh, kn = _fixture(tmp_path)

    def boom(*_a, **_k):
        raise RuntimeError("boom\nsecond line")

    monkeypatch.setattr(V, "build_vocabulary", boom)
    t = open_records(_rcfg(wh), str(kn), _on(words))
    try:
        assert t.catalog.resolver is None
        assert t.resolver_lines == ["spelling help off: the record vocabulary could not be built: RuntimeError: boom"]
        res = t.call("count_records", {"object": "Interview__c"})
        assert res["ok"], res
    finally:
        t.close()
    monkeypatch.undo()
    t = open_records(_rcfg(wh), str(kn), ResolverConfig(enabled=True, lexicon=(str(tmp_path / "none.txt"),)))
    try:
        assert t.catalog.resolver is None and len(t.resolver_lines) == 1
        assert t.resolver_lines[0].startswith("spelling help off: no English word list could be read (")
    finally:
        t.close()


# ------------------------------------------------------------------------------------- open_record_session
def _steady(line: str) -> str:
    """A start-up line without what changes from one open (or run) to the next: the snapshot's age and the build time
    that ends a ``spelling help on`` line."""
    return re.sub(r", \d+\.\d s$", ", <s> s", re.sub(r"\d+\.\d h\b", "<age> h", line))


def test_record_session_lines_gain_the_spelling_lines_only_with_the_switch_on(tmp_path, words, monkeypatch):
    wh, kn = _fixture(tmp_path)
    monkeypatch.setenv("SFA_RESOLVER", "off")
    cfg = C.load_config(base_url="http://localhost:8000/v1")

    def opened(**kw):   # one session at a time: DuckDB allows one configuration per file and process
        session, lines = pipeline.open_record_session(cfg, _rcfg(wh), str(kn), **kw)
        assert session is not None, lines
        try:   # the snapshot's age and the spelling help's build time change at each open: compared without them
            return ([_steady(x) for x in lines], session.tools.catalog.resolver,
                    [_steady(x) for x in session.tools.resolver_lines])
        finally:
            session.close()

    opened()            # the first open makes the fixture file private (a "note: made ... private" line)
    lines_off, r_off, _ = opened()
    assert lines_off[0].startswith("records: ") and r_off is None
    assert not any("spelling help" in x for x in lines_off)
    assert opened(resolver=ResolverConfig(enabled=False)) == (lines_off, None, [])
    lines_on, r_on, spelling = opened(resolver=_on(words))
    assert isinstance(r_on, Resolver) and len(spelling) == 1
    assert lines_on == lines_off + spelling
    monkeypatch.delenv("SFA_RESOLVER")                    # unset: on by default (2026-10-27)
    monkeypatch.setenv("SFA_LEXICON", str(words))
    lines_default, r_default, spelling_default = opened()
    assert isinstance(r_default, Resolver) and (lines_default, spelling_default) == (lines_on, spelling)


# ------------------------------------------------------------------------------------------------- the cli
_CLI = textwrap.dedent("""
    import contextlib, io, json, sys
    import sf_assistant.cli as mod
    import sf_assistant.pipeline
    from sf_assistant.agent.llm import LLM
    from sf_assistant.agent.mock_llm import MockClient
    sf_assistant.pipeline.LLM = lambda cfg: LLM(cfg, client=MockClient())
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = mod.main(["--no-records", "--knowledge", sys.argv[1], "What values can Status__c take?"])
    mods = sorted(m for m in sys.modules if m.startswith("sf_assistant."))
    print(json.dumps({"code": code, "out": buf.getvalue(), "modules": mods, "duckdb": "duckdb" in sys.modules}))
""")


def _cli(tmp_path: Path, **env: str) -> subprocess.CompletedProcess:
    e = {k: v for k, v in os.environ.items() if k not in C.RESOLVER_ENV_VARS.values()}
    e.update(OKEVAL_BASE_URL="http://localhost:8000/v1", OKEVAL_API_KEY="test-key", **env)
    return subprocess.run([sys.executable, "-c", _CLI, str(FIXTURE_JSON)], capture_output=True, text=True, env=e,
                          cwd=str(tmp_path), timeout=120)


def _run_cli(tmp_path: Path, **env: str) -> dict:
    res = _cli(tmp_path, **env)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])


def test_no_records_run_switch_off_imports_no_resolver_module(tmp_path):
    data = _run_cli(tmp_path, SFA_RESOLVER="off")
    assert data["code"] == 0 and "spelling help" not in data["out"]
    assert not {"sf_assistant.knowledge.resolver", "sf_assistant.knowledge.terms"} & set(data["modules"])
    assert not any(m.startswith("sf_assistant.engine") for m in data["modules"]) and data["duckdb"] is False


def test_no_records_run_switch_on_builds_the_knowledge_resolver_without_engine_code(tmp_path, words):
    """Flag-on twin of test_pipeline_cli.py::test_no_records_imports_neither_record_modules_nor_duckdb (decision A1:
    a records-off run gets the spelling help, and still loads no engine module and no duckdb)."""
    data = _run_cli(tmp_path, SFA_RESOLVER="on", SFA_LEXICON=str(words))
    assert data["code"] == 0
    out = data["out"].splitlines()
    assert out[0].startswith("objects: Internal_Interview__c   model: ")
    assert out[1].startswith("spelling help on (knowledge files): ") and "word list 16 words" in out[1]
    assert {"sf_assistant.knowledge.resolver", "sf_assistant.knowledge.terms"} <= set(data["modules"])
    assert not any(m.startswith("sf_assistant.engine") for m in data["modules"]) and data["duckdb"] is False
    assert "\n-- 2 tool call(s), 3 model call(s)" in data["out"]     # the answer itself is unchanged
    for empty in ({}, {"SFA_RESOLVER": ""}):            # unset or empty: on by default (2026-10-27)
        default = _run_cli(tmp_path, SFA_LEXICON=str(words), **empty)
        assert default["code"] == 0, empty
        assert [_steady(x) for x in default["out"].splitlines()[:2]] == [_steady(x) for x in out[:2]], empty
        assert {"sf_assistant.knowledge.resolver", "sf_assistant.knowledge.terms"} <= set(default["modules"])
    bad = _run_cli(tmp_path, SFA_RESOLVER="maybe")  # a bad switch never stops a run: off, with one start-up line
    assert bad["code"] == 0 and bad["out"].splitlines()[1] == "spelling help off: SFA_RESOLVER must be off or on"
    assert "sf_assistant.knowledge.resolver" not in bad["modules"]


# ------------------------------------------------------------------------------------- live (opt-in, counts only)
_LIVE = Path(os.environ.get("SFA_LIVE_SNAPSHOT") or C.DEFAULT_WAREHOUSE)


@pytest.mark.skipif(os.environ.get("OKEVAL_RECORDS_LIVE") != "1" or not _LIVE.is_file()
                    or not Path("/usr/share/dict/american-english").is_file(),
                    reason="live: set OKEVAL_RECORDS_LIVE=1 with the local snapshot and the system word lists")
def test_live_vocabulary_through_the_guard_equals_the_offline_build():
    """The plan's in-app check (docs/resolver_integration.md, "(7)"): on the real snapshot the vocabulary read through
    the guarded warehouse equals the offline build term for term, no statement fails or is cut. Nothing is printed."""
    from sf_assistant.engine.vocabulary import build_vocabulary
    from sf_assistant.knowledge.resolver import build
    from sf_assistant.tools.open_records import open_records

    t = open_records(load_records_config(warehouse=str(_LIVE), today=dt.date(2026, 10, 13), tz="America/New_York"),
                     str(C.KNOWLEDGE_DIR), ResolverConfig(enabled=True))
    try:
        r = t.catalog.resolver
        assert isinstance(r, Resolver), t.resolver_lines
        assert r.info["snapshot"]["query_errors"] == 0 and r.info["snapshot"]["truncated"] == 0
        assert len(t.resolver_lines) == 1 and "WARNING" not in t.resolver_lines[0]
        app, counts = [_key(x) for x in r.terms], dict(r.info["terms"])
    finally:
        t.close()
    offline = build_vocabulary(str(C.KNOWLEDGE_DIR), str(_LIVE))
    assert counts == offline.info["terms"]
    assert app == [_key(x) for x in build(offline).terms]
