"""sf_assistant.config.Settings / load_settings / LIMITS / ENV_DOC: one settings object, the notebook folder's limits."""

from __future__ import annotations

import dataclasses
import datetime as dt
import os
from pathlib import Path

import pytest

from sf_assistant import config as C
from sf_assistant.agent import knowledge_agent, lean, merge, record_prompts, record_session
from sf_assistant.tools import record_base

PATH_VARS = ("SFA_HOME", "SFA_KNOWLEDGE_DIR", "SFA_COMPONENTS_DIR", "SFA_PROMPTS_DIR", "SFA_QUESTIONS_DIR")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (*C.ENV_VARS.values(), *C.RECORDS_ENV_VARS.values(), *C.ENV_ALIASES.values(),
                 *C.RESOLVER_ENV_VARS.values(), *C.MEMORY_ENV_VARS.values()):
        monkeypatch.delenv(name, raising=False)


def test_limits_are_the_notebook_values():
    """The values of okeval/config.py, okeval/agent.py, okrec/config.py, okrec/lean.py, okrec/session.py,
    okrec/tools.py, okrec/prompts.py and ask.py (notebooks/object_knowledge_eval, 2026-10-20), and the component
    tools' budgets (stage 4 of the flow build, 2026-10-24; format review A01, A02, A17), and the ask service's follow-up
    rewrite (2026-11-04)."""
    assert dataclasses.asdict(C.LIMITS) == {
        "knowledge_max_steps": 8, "model_max_tokens": 1500, "model_timeout_s": 120.0,
        "knowledge_tool_result_chars": 16_000, "lean_max_tool_calls": 3, "lean_max_model_calls": 3,
        "lean_max_tokens": 600, "records_max_steps": 12, "records_statement_timeout_s": 10.0,
        "records_call_budget_s": 20.0, "records_max_age_h": 24.0, "record_result_chars": 12_000,
        "names_max_tokens": 60, "merge_max_tokens": 1600, "merge_facts_chars": 16_000, "warmup_timeout_s": 120.0,
        "max_rows": 20, "component_search_chars": 4_000, "component_summary_chars": 3_500,
        "component_page_chars": 6_000, "component_usage_rows": 40, "component_usage_chars": 9_000,
        "followup_max_tokens": 1000}


def test_defaults_and_module_constants_come_from_the_limits():
    L = C.LIMITS
    cfg, rcfg = C.Config(), C.RecordsConfig()
    assert (cfg.max_tool_steps, cfg.max_tokens, cfg.timeout_s) == (L.knowledge_max_steps, L.model_max_tokens,
                                                                   L.model_timeout_s)
    assert (rcfg.max_steps, rcfg.timeout_s, rcfg.call_budget_s, rcfg.max_age_h) == (
        L.records_max_steps, L.records_statement_timeout_s, L.records_call_budget_s, L.records_max_age_h)
    assert knowledge_agent.MAX_TOOL_RESULT_CHARS == L.knowledge_tool_result_chars
    assert (lean.LEAN_MAX_TOOL_CALLS, lean.LEAN_MAX_MODEL_CALLS, lean.LEAN_MAX_TOKENS) == (
        L.lean_max_tool_calls, L.lean_max_model_calls, L.lean_max_tokens)
    assert (merge.MERGE_MAX_TOKENS, merge.MERGE_FACTS_CHARS) == (L.merge_max_tokens, L.merge_facts_chars)
    assert record_prompts.NAMES_MAX_TOKENS == L.names_max_tokens
    assert record_base.MAX_RESULT_CHARS == L.record_result_chars
    from sf_assistant.agent import followup

    assert followup.FOLLOWUP_MAX_TOKENS == L.followup_max_tokens
    from sf_assistant.tools import component_tools as ct

    assert (ct.SEARCH_CHARS, ct.SUMMARY_CHARS, ct.PAGE_CHARS, ct.USAGE_ROWS, ct.USAGE_CHARS) == (
        L.component_search_chars, L.component_summary_chars, L.component_page_chars, L.component_usage_rows,
        L.component_usage_chars)
    assert max(L.component_search_chars, L.component_summary_chars, L.component_page_chars,
               L.component_usage_chars) < L.knowledge_tool_result_chars   # never reaches the tool-result cut
    import inspect

    from sf_assistant import cli

    assert not hasattr(cli, "WARMUP_TIMEOUT_S")   # the cli reads settings.limits.warmup_timeout_s (#13)
    assert inspect.signature(record_session.RecordSession.__init__).parameters["max_steps"].default == L.records_max_steps
    assert inspect.signature(cli.ask).parameters["max_rows"].default == L.max_rows


def test_load_settings_reads_both_configs(monkeypatch):
    monkeypatch.setenv("OKEVAL_MODEL", "m-env")
    monkeypatch.setenv("SFA_BASE_URL", "http://127.0.0.1:9/v1")   # alias: OKEVAL_BASE_URL is unset
    monkeypatch.setenv("OKEVAL_RECORDS_TODAY", "2026-10-13")
    monkeypatch.setenv("OKEVAL_RECORDS_MAX_STEPS", "15")
    s = C.load_settings(tz="America/New_York", warehouse=None)
    assert (s.model.model, s.model.base_url) == ("m-env", "http://127.0.0.1:9/v1")
    assert s.records == C.load_records_config(tz="America/New_York")
    assert (s.records.today, s.records.max_steps, s.records.tz) == (dt.date(2026, 10, 13), 15, "America/New_York")
    assert s.model == C.load_config() and s.limits is C.LIMITS
    assert (s.home, s.knowledge_dir, s.prompts_dir, s.questions_dir, s.private_dir) == (
        C.HOME, C.KNOWLEDGE_DIR, C.PROMPTS_DIR, C.QUESTIONS_DIR, C.PRIVATE_DIR)
    assert s.components_dir == C.COMPONENTS_DIR
    assert C.load_settings(model="m-flag").model.model == "m-flag"


def test_without_records_a_bad_record_setting_does_not_stop_the_run(monkeypatch):
    monkeypatch.setenv("OKEVAL_RECORDS_TZ", "Mars/Base")
    assert C.load_settings(records=False).records is None
    with pytest.raises(ValueError, match="unknown time zone 'Mars/Base' in OKEVAL_RECORDS_TZ"):
        C.load_settings()
    with pytest.raises(TypeError):
        C.load_settings(records=False, colour="red")


def test_safe_dict_masks_the_api_key(monkeypatch):
    monkeypatch.setenv("OKEVAL_API_KEY", "secret-key-123")
    d = C.load_settings(records=False).safe_dict()
    assert d["model"]["api_key"] == "***" and "secret-key-123" not in repr(d) and d["records"] is None
    assert d["limits"]["records_max_steps"] == 12


def test_env_doc_lists_every_environment_variable_with_its_default():
    names = [n for n, _, _ in C.ENV_DOC]
    assert len(names) == len(set(names))
    assert set(names) == {*C.ENV_VARS.values(), *C.RECORDS_ENV_VARS.values(), *PATH_VARS,
                          *C.RESOLVER_ENV_VARS.values(), C.MERGE_ENV_VAR, C.TABLES_ENV_VAR, C.CHARTS_ENV_VAR,
                          *C.MEMORY_ENV_VARS.values(), C.ASK_LOG_DAYS_ENV_VAR}
    doc = {n: d for n, d, _ in C.ENV_DOC}
    assert doc["SFA_RESOLVER"] == "on" and "/usr/share/dict/american-english" in doc["SFA_LEXICON"]
    assert doc["SFA_MERGE"] == "code" == C.MergeConfig().by   # the merge switch (2026-10-29): code by default
    # the table switch (latency plan step 3, 2026-10-29): its default is the one constant TABLES_DEFAULT (derived here,
    # so the go-live stays a one-line change; "ships as model" is a deploy checklist item, docs/runbook.md)
    assert doc["SFA_TABLES"] == C.TABLES_DEFAULT == C.TablesConfig().by and C.TABLES_DEFAULT in C.TABLES_MODES
    # the chart switch (2026-11-03): its default is the one constant CHARTS_DEFAULT, as above
    assert doc["SFA_CHARTS"] == C.CHARTS_DEFAULT == C.ChartsConfig().mode and C.CHARTS_DEFAULT in C.CHARTS_MODES
    assert C.CHARTS_DEFAULT == "on"   # the owner's go-live of 2026-11-04: going back is the owner's word, here as well
    # conversation memory (design v2 of 2026-11-25 §15.1): shadow after the deploy; "on" is the owner's word
    assert doc["SFA_MEMORY"] == C.MEMORY_DEFAULT == C.MemoryConfig().mode == "shadow"
    assert (doc["SFA_MEMORY_SHORTCUTS"], doc["SFA_MEMORY_PATCH_KNOWLEDGE"], doc["SFA_MEMORY_MAX_CALLS"],
            doc["SFA_MEMORY_MAX_AGE"], doc["SFA_MEMORY_TOKENS"], doc["SFA_ASK_LOG_DAYS"]) == (
        "on", "off", "3", "2", "12000", "90")
    assert doc["OKEVAL_BASE_URL"] == C.Config.base_url and doc["OKEVAL_MODEL"] == C.Config.model
    assert doc["OKEVAL_RECORDS_MAX_STEPS"] == "12" and doc["OKEVAL_RECORDS_TIMEOUT_S"] == "10.0"
    assert set(C.ENV_ALIASES) == {*C.ENV_VARS.values(), *C.RECORDS_ENV_VARS.values()}


# ---------------------------------------------------------------- stage 2 (#13): every Settings field is read or checked
def test_load_settings_folders_and_limits_reach_the_run(monkeypatch, tmp_path):
    s = C.load_settings(knowledge_dir=str(tmp_path), questions_dir=tmp_path / "q", transcript_dirs=[".private/x"],
                        components_dir=str(tmp_path / "c"))
    assert (s.knowledge_dir, s.questions_dir, s.transcript_dirs) == (tmp_path, tmp_path / "q", (".private/x",))
    assert s.components_dir == tmp_path / "c" and s.safe_dict()["components_dir"] == str(tmp_path / "c")
    lim = dataclasses.replace(C.LIMITS, knowledge_max_steps=3, model_max_tokens=99, records_max_steps=5,
                              records_call_budget_s=7.0, records_max_age_h=2.0, max_rows=4, warmup_timeout_s=9.0)
    s = C.load_settings(limits=lim)
    assert (s.model.max_tool_steps, s.model.max_tokens, s.model.timeout_s) == (3, 99, C.LIMITS.model_timeout_s)
    assert (s.records.max_steps, s.records.call_budget_s, s.records.max_age_h, s.records.timeout_s) == (
        5, 7.0, 2.0, C.LIMITS.records_statement_timeout_s)
    assert s.limits is lim
    monkeypatch.setenv("OKEVAL_RECORDS_MAX_STEPS", "15")          # a variable wins over a limit
    monkeypatch.setenv("SFA_RECORDS_MAX_AGE_H", "3")              # its SFA_* alias too
    s = C.load_settings(limits=lim, max_tokens=50)                # and a model override
    assert (s.records.max_steps, s.records.max_age_h, s.model.max_tokens) == (15, 3.0, 50)
    with pytest.raises(ValueError, match="limits.records_max_steps must be an integer between 2 and 30"):
        monkeypatch.delenv("OKEVAL_RECORDS_MAX_STEPS")
        C.load_settings(limits=dataclasses.replace(C.LIMITS, records_max_steps=99))


def test_default_settings_equal_the_configs_own_defaults():
    s = C.load_settings()
    assert s == C.Settings(model=C.load_config(), records=C.load_records_config())
    assert C.load_settings(limits=C.Limits()) == s     # equal limits: nothing applied


@pytest.mark.parametrize("field", C.IMPORT_BOUND_LIMITS)
def test_a_limit_the_modules_bind_at_import_is_refused(field):
    lim = dataclasses.replace(C.LIMITS, **{field: getattr(C.LIMITS, field) + 1})
    with pytest.raises(ValueError, match=f"Settings.limits.{field} = .*cannot change per run"):
        C.load_settings(records=False, limits=lim)


@pytest.mark.parametrize("field", ["home", "private_dir", "prompts_dir"])
def test_a_folder_bound_at_import_is_refused(field, tmp_path):
    s = C.load_settings(records=False)
    with pytest.raises(ValueError, match=f"Settings.{field} .* is bound when sf_assistant is imported"):
        dataclasses.replace(s, **{field: tmp_path})


def test_cli_builds_one_settings_from_its_flags_and_folders(monkeypatch, tmp_path):
    from sf_assistant import cli

    seen = {}

    def load(**kw):
        seen.update(kw)
        raise SystemExit(0)   # stop before the knowledge files are read: no model, no snapshot

    monkeypatch.setattr(cli, "load_settings", load)
    monkeypatch.setattr(cli, "KNOWLEDGE_DIR", str(tmp_path))
    with pytest.raises(SystemExit):
        cli.main(["--no-records", "--max-rows", "3", "q"])
    assert seen == {"records": False, "warehouse": None, "tz": None, "today": None, "knowledge_dir": str(tmp_path),
                    "transcript_dirs": cli.PRIVATE_DIRS}
    home = Path(cli.HERE)
    path, note = cli.transcript_path(None, "q.txt", records=True, dirs=(".private/x",))   # the run's folders
    assert Path(path).parent == home / ".private" / "x" and note is None
    inside = str(home / ".private" / "x" / "a.txt")
    assert cli.transcript_path(inside, "q.txt", records=True, dirs=(".private/x",)) == (os.path.realpath(inside), None)


# ------------------------------------------------------------- the spelling help switch (docs/resolver_integration.md)
def test_resolver_switch_is_on_by_default_and_read_from_the_environment(monkeypatch, tmp_path):
    """On by default (owner, 2026-10-26): unset or empty is on; off still turns it off, on keeps it on."""
    assert C.load_resolver_config() == C.ResolverConfig() == C.ResolverConfig(enabled=True, lexicon=None)
    assert C.load_settings(records=False).resolver == C.ResolverConfig() == C.resolver_settings()
    assert C.Settings(model=C.load_config()).resolver.enabled is True
    assert C.load_settings(records=False).safe_dict()["resolver"] == {"enabled": True, "lexicon": None}
    for raw, on in (("on", True), (" ON ", True), ("off", False), ("Off", False), (" OFF ", False), ("", True),
                    ("  ", True)):
        monkeypatch.setenv("SFA_RESOLVER", raw)
        assert C.load_resolver_config().enabled is on, raw
        assert C.resolver_settings().enabled is on and C.load_settings(records=False).resolver.enabled is on, raw
    monkeypatch.setenv("SFA_RESOLVER", "off")
    s = C.load_settings(records=False)
    assert s.resolver == C.ResolverConfig(enabled=False) and not s.resolver.problem
    assert s.safe_dict()["resolver"] == {"enabled": False, "lexicon": None}
    assert C.load_resolver_config(enabled=None).enabled is False          # a None override keeps the env's off
    monkeypatch.setenv("SFA_RESOLVER", "on")
    s = C.load_settings(records=False)
    assert s.resolver.enabled and s.safe_dict()["resolver"] == {"enabled": True, "lexicon": None}
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    monkeypatch.setenv("SFA_LEXICON", f"{a}{os.pathsep} {b} {os.pathsep}")
    assert C.load_resolver_config().lexicon == (str(a), str(b))
    assert C.load_settings(records=False).safe_dict()["resolver"]["lexicon"] == [str(a), str(b)]
    monkeypatch.setenv("SFA_LEXICON", " ")
    assert C.load_resolver_config().lexicon is None                          # blank: the system lists


def test_resolver_switch_rejects_other_values_and_takes_overrides(monkeypatch, tmp_path):
    for raw in ("1", "true", "yes", "maybe"):
        monkeypatch.setenv("SFA_RESOLVER", raw)
        with pytest.raises(ValueError, match="SFA_RESOLVER must be off or on"):
            C.load_resolver_config()
        # an entry point never stops for it (never fatal): off, with the reason for the start-up line (never on by
        # the default)
        bad = C.ResolverConfig(enabled=False, problem="SFA_RESOLVER must be off or on")
        assert C.resolver_settings() == bad and C.load_settings(records=False).resolver == bad
        assert C.resolver_settings().enabled is False and C.load_settings(records=False).resolver.enabled is False
        assert C.load_settings(records=False).safe_dict()["resolver"] == {
            "enabled": False, "lexicon": None, "problem": "SFA_RESOLVER must be off or on"}
    assert C.ResolverConfig(problem="x").enabled is False                   # a config with a problem is off
    assert C.ResolverConfig(problem="x") == C.ResolverConfig(enabled=False, problem="x")
    monkeypatch.setenv("SFA_RESOLVER", "off")
    assert C.load_resolver_config(enabled=True, lexicon=str(tmp_path)) == C.ResolverConfig(True, (str(tmp_path),))
    monkeypatch.delenv("SFA_RESOLVER")
    assert C.load_resolver_config(enabled=False) == C.ResolverConfig(enabled=False)   # the override turns it off
    assert C.load_resolver_config(enabled=None, lexicon=["x", tmp_path]).lexicon == ("x", str(tmp_path))
    with pytest.raises(ValueError, match="enabled must be True or False"):
        C.load_resolver_config(enabled="on")
    with pytest.raises(TypeError, match="unknown ResolverConfig setting"):
        C.load_resolver_config(colour="red")
