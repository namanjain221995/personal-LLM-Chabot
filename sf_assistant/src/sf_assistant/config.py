"""Settings and paths of sf_assistant (stdlib only; imports nothing else of the package).

One :class:`Settings` object holds everything a run needs: ``model`` (:class:`Config`), ``records``
(:class:`RecordsConfig`, or None when records are off), the paths, ``limits`` (:class:`Limits`) and ``resolver``
(:class:`ResolverConfig`: the spelling help, ``SFA_RESOLVER``, on by default). Build it with
:func:`load_settings`; the cli builds it once from its flags and its folder defaults, the pipeline builds the
knowledge side from ``settings.model``, the record session from ``settings.records`` and the record catalog from
``settings.knowledge_dir``. :data:`ENV_DOC` lists every environment variable with its default.

Two settings dataclasses, moved unchanged from the notebook folder:

* :class:`Config` (was ``okeval/config.py``): the OpenAI-compatible model endpoint and the knowledge agent.
  Settings come from, in increasing order of precedence: the dataclass defaults, Colab Secrets
  (``google.colab.userdata``) when running inside Google Colab, environment variables (``OKEVAL_*``), and
  keyword overrides passed to :func:`load_config`. The API key is never printed: it is excluded from ``repr``
  and :meth:`Config.safe_dict` masks it.
* :class:`RecordsConfig` (was ``okrec/config.py``): the record tools (no duckdb import). Precedence: dataclass
  defaults, then ``OKEVAL_*`` environment variables, then keyword overrides (cli flags ``--warehouse``, ``--tz``
  and ``--today``).

Every ``OKEVAL_*`` name works as before. Each one also has an ``SFA_*`` alias (``OKEVAL_BASE_URL`` ->
``SFA_BASE_URL``, ``OKEVAL_WAREHOUSE`` -> ``SFA_WAREHOUSE`` ...) that is read only when the ``OKEVAL_*`` name is
unset or blank, so a run without ``SFA_*`` variables behaves exactly as before (:data:`ENV_ALIASES`).
``SFA_WAREHOUSE`` is also the web page's own variable (web/compose.yaml sets it to the container's read copy) with
the same meaning, the snapshot file; the web page passes that file to :func:`load_records_config` explicitly.

The spelling help (:class:`ResolverConfig`, :func:`load_resolver_config`): the typo-tolerant term resolver
(docs/resolver.md) is on by default (owner, 2026-10-26: misspelt words are read from now on); ``SFA_RESOLVER=off`` turns
it off (``on``, unset or empty: on; any other value: off with a start-up line). ``SFA_LEXICON`` gives its English word
lists (paths separated by ``os.pathsep``; default the system lists). With the switch off nothing is built and every code
path is the one without it (docs/resolver_integration.md, "Build").

The merge switch (:class:`MergeConfig`, :func:`merge_settings`; latency plan step 2, the owner's go of 2026-10-29):
``SFA_MERGE`` says how ``pipeline.answer`` merges the record answer with the lean facts block. ``code`` (unset or empty:
the default) adds the relevant field-meaning bullets by code, with no model call (``agent.merge.code_merge``);
``model`` makes the merge call of every answer, as before (``agent.merge.combine``). Read at each answer, never at
import; any other value is code, and the answer's JSONL says so (``merge_switch``). A FIELD CHECK line, the full
knowledge agent and a record answer that still plans aloud (``agent.merge.plans_aloud``) always keep the merge call.

The table switch (:class:`TablesConfig`, :func:`tables_settings`; latency plan step 3, the owner's go of 2026-10-29):
``SFA_TABLES`` says who writes the rows of a record answer. ``model`` (unset or empty: :data:`TABLES_DEFAULT`, model
until the owner's go-live) leaves them to the record agent, as before; ``code`` has the record agent write a table line
per result (``{{table q3}}``) and code write that result's rows from what the model saw (``agent.tables``). Read at each
answer, never at import (``RecordSession.ask`` reads it once per record question, ``pipeline.answer`` once per answer);
any other value is :data:`TABLES_DEFAULT`, and the answer's JSONL says so (``tables_switch``). ``web/compose.yaml``
passes no ``SFA_TABLES``, so the page and the MCP server run :data:`TABLES_DEFAULT`.

The chart switch (:class:`ChartsConfig`, :func:`charts_settings`; the charts of 2026-11-03): ``SFA_CHARTS`` says whether
a question that asks for a chart gets one. ``off`` leaves every answer, model request and listing as it is; ``on``
(unset or empty: :data:`CHARTS_DEFAULT`, on since the owner's go-live of 2026-11-04) has code draw the chart from the
record results (``agent.charts``). Read at each call, never at import (``pipeline.answer`` once per answer, ``RecordSession.triage``
and ``.ask`` per call, the MCP server once at start, because its listing is built once); any other value is
off, and the answer's JSONL says so (``charts_switch``). ``web/compose.yaml`` passes no
``SFA_CHARTS``, so the page and the MCP server run :data:`CHARTS_DEFAULT`.

Paths (all git-ignored data lives under :data:`PRIVATE_DIR`):

* :data:`HOME` - the ``sf_assistant`` folder (``SFA_HOME``; default: two levels above ``src/sf_assistant``, which
  holds for the editable install only: ``prompts/``, ``knowledge/objects/`` and ``.private/`` are not part of a
  wheel, so a plain install needs ``SFA_HOME``, and :mod:`sf_assistant.prompts` says so when the folder is missing);
* :data:`KNOWLEDGE_DIR` - the object knowledge files, ``knowledge/objects`` (``SFA_KNOWLEDGE_DIR``);
* :data:`COMPONENTS_DIR` - the component knowledge files (flows) and their ``index.json``, ``knowledge/components``
  (``SFA_COMPONENTS_DIR``): the full knowledge agent with several object files gets the component tools when the
  folder holds an ``index.json`` (:mod:`sf_assistant.knowledge.components`);
* :data:`QUESTIONS_DIR` - the question files, ``.private/questions`` (``SFA_QUESTIONS_DIR``);
* :data:`DEFAULT_WAREHOUSE` - the local snapshot, ``.private/okeval_run/warehouse.duckdb``;
* :data:`B_DIR` - the base of ``display_path`` (``.private``), so the default snapshot still shows as
  ``okeval_run/warehouse.duckdb`` in the status line and in ``get_sync_status``;
* :data:`PROMPTS_DIR` - the prompt files ``prompts/<id>.md`` (``SFA_PROMPTS_DIR``).

Limits (:data:`LIMITS`): the step, token, time and size limits of the agents and tools, with the notebook folder's
values. Config and RecordsConfig take their defaults from it, and the modules bind their constants from it
(``knowledge_agent.MAX_TOOL_RESULT_CHARS``, ``lean.LEAN_MAX_TOOL_CALLS``, ``record_base.MAX_RESULT_CHARS``,
``merge.MERGE_MAX_TOKENS`` ...). Only the RecordsConfig ones can be changed from the environment
(``OKEVAL_RECORDS_MAX_STEPS`` ...), as before. A :class:`Settings` may carry other limits for one run
(:func:`load_settings` ``limits=``): those of Config, RecordsConfig and the cli; the module constants are fixed per
process (:data:`IMPORT_BOUND_LIMITS`), and a Settings that changes one of them is refused.
"""

from __future__ import annotations

import dataclasses
import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = ["Settings", "load_settings", "Limits", "LIMITS", "IMPORT_BOUND_LIMITS", "ENV_DOC",
           "Config", "load_config", "ENV_VARS",
           "RecordsConfig", "load_records_config", "B_DIR", "DEFAULT_WAREHOUSE", "CONTAINER_SOURCE",
           "FORBIDDEN_SOURCE", "RECORDS_ENV_VARS", "ResolverConfig", "load_resolver_config", "resolver_settings",
           "RESOLVER_ENV_VARS", "MergeConfig", "merge_settings", "MERGE_ENV_VAR", "MERGE_MODES",
           "TablesConfig", "tables_settings", "TABLES_ENV_VAR", "TABLES_MODES", "TABLES_DEFAULT",
           "ChartsConfig", "charts_settings", "CHARTS_ENV_VAR", "CHARTS_MODES", "CHARTS_DEFAULT",
           "ENV_ALIASES", "HOME", "PRIVATE_DIR", "KNOWLEDGE_DIR", "COMPONENTS_DIR", "PROMPTS_DIR", "QUESTIONS_DIR",
           "TRANSCRIPT_DIRS"]

# ------------------------------------------------------------------------------------------------ paths

HOME = Path(os.environ.get("SFA_HOME") or Path(__file__).resolve().parents[2]).resolve()   # sf_assistant/
PRIVATE_DIR = HOME / ".private"                      # git-ignored, mode 700: real names, record data
KNOWLEDGE_DIR = Path(os.environ.get("SFA_KNOWLEDGE_DIR") or HOME / "knowledge" / "objects")
#: component knowledge (flows/<Api>.json + index.json); a folder without index.json: no component tools
COMPONENTS_DIR = Path(os.environ.get("SFA_COMPONENTS_DIR") or HOME / "knowledge" / "components")
PROMPTS_DIR = Path(os.environ.get("SFA_PROMPTS_DIR") or HOME / "prompts")   # prompts/<id>.md (sf_assistant.prompts)
QUESTIONS_DIR = Path(os.environ.get("SFA_QUESTIONS_DIR") or PRIVATE_DIR / "questions")
TRANSCRIPT_DIRS = (".private/runs", ".private/okeval_run")   # relative to HOME: where record transcripts may go

# ------------------------------------------------------------------------------------------------ limits


@dataclass(frozen=True)
class Limits:
    """Step, token, time and size limits of one run (the notebook folder's values; not read from the environment
    except through the RecordsConfig settings, see :data:`ENV_DOC`)."""

    # knowledge agent (Config defaults; agent/knowledge_agent.py)
    knowledge_max_steps: int = 8                 # model calls of the knowledge agent (Config.max_tool_steps)
    model_max_tokens: int = 1500                 # max_tokens of a knowledge agent reply (Config.max_tokens)
    model_timeout_s: float = 120.0               # one model call attempt (Config.timeout_s)
    knowledge_tool_result_chars: int = 16_000    # a tool result is cut to this before the model sees it
    # lean knowledge step (agent/lean.py, merged mode, RECORDS_SPEC §21.19)
    lean_max_tool_calls: int = 3
    lean_max_model_calls: int = 3
    lean_max_tokens: int = 600
    # record agent and record tools (RecordsConfig defaults; tools/record_base.py)
    records_max_steps: int = 12                  # record agent tool rounds (RecordsConfig.max_steps, 2-30)
    records_statement_timeout_s: float = 10.0    # one SQL statement (RecordsConfig.timeout_s, 1-120)
    records_call_budget_s: float = 20.0          # one tool call, all its statements (RecordsConfig.call_budget_s)
    records_max_age_h: float = 24.0              # snapshot age warning (RecordsConfig.max_age_h)
    record_result_chars: int = 12_000            # a record tool result's JSON (record_base.MAX_RESULT_CHARS)
    names_max_tokens: int = 60                   # the names call (record_prompts.NAMES_MAX_TOKENS)
    # merge call (agent/merge.py)
    merge_max_tokens: int = 1600
    merge_facts_chars: int = 16_000              # RECORD FACTS block of the merge call
    # cli
    warmup_timeout_s: float = 120.0              # interactive mode: wait for the snapshot to open
    max_rows: int = 20                           # --max-rows default: rows printed per statement
    # component tools (tools/component_tools.py; knowledge/components), each well under knowledge_tool_result_chars
    component_search_chars: int = 4_000          # one search_components result
    component_summary_chars: int = 3_500         # get_component detail=summary
    component_page_chars: int = 6_000            # one page of get_component detail=logic / fields / caveats
    component_usage_rows: int = 40               # find_usages rows before "more"
    component_usage_chars: int = 9_000           # one find_usages result
    # follow-up rewrite of the ask service (agent/followup.py)
    followup_max_tokens: int = 1000              # one question of at most 1,000 characters, echoed back whole


LIMITS = Limits()

#: The limits the modules bind at import (module constants, see each field's comment): fixed per process. The other
#: limits are run settings: Config and RecordsConfig defaults (:func:`load_settings`) and the cli's.
IMPORT_BOUND_LIMITS = ("knowledge_tool_result_chars", "lean_max_tool_calls", "lean_max_model_calls", "lean_max_tokens",
                       "record_result_chars", "names_max_tokens", "merge_max_tokens", "merge_facts_chars",
                       "component_search_chars", "component_summary_chars", "component_page_chars",
                       "component_usage_rows", "component_usage_chars", "followup_max_tokens")
#: Config attribute -> the Limits field that gives its default.
_CONFIG_LIMITS = {"max_tool_steps": "knowledge_max_steps", "max_tokens": "model_max_tokens",
                  "timeout_s": "model_timeout_s"}
#: RecordsConfig attribute -> the Limits field that gives its default.
_RECORDS_LIMITS = {"timeout_s": "records_statement_timeout_s", "call_budget_s": "records_call_budget_s",
                   "max_steps": "records_max_steps", "max_age_h": "records_max_age_h"}

# ------------------------------------------------------------------------------------------------ model endpoint
# (was okeval/config.py)

#: Config attribute -> environment variable / Colab secret name.
ENV_VARS: dict[str, str] = {
    "base_url": "OKEVAL_BASE_URL",
    "api_key": "OKEVAL_API_KEY",
    "model": "OKEVAL_MODEL",
    "judge_model": "OKEVAL_JUDGE_MODEL",
}


@dataclass
class Config:
    """All tunable settings for one evaluation run."""

    base_url: str = "http://localhost:8000/v1"  # any OpenAI-compatible endpoint
    api_key: str = field(default="EMPTY", repr=False)  # never shown in repr / logs
    model: str = "Qwen/Qwen3.6-35B-A3B-NVFP4"
    judge_model: str | None = None  # None -> same as model
    temperature: float = 0.0
    max_tokens: int = LIMITS.model_max_tokens          # 1500
    timeout_s: float = LIMITS.model_timeout_s          # 120.0
    enable_thinking: bool = False  # sent as extra_body chat_template_kwargs for vLLM/Qwen
    max_tool_steps: int = LIMITS.knowledge_max_steps   # 8
    knowledge_path: str = "knowledge/Internal_Interview__c.json"
    seed: int = 7

    @property
    def effective_judge_model(self) -> str:
        """Model used for judging: ``judge_model`` when set, otherwise ``model``."""
        return self.judge_model or self.model

    @property
    def is_openai(self) -> bool:
        """True when the endpoint is the official OpenAI API (no vLLM-only extras allowed)."""
        return "api.openai.com" in (self.base_url or "")

    def safe_dict(self) -> dict[str, Any]:
        """Return the settings as a dict with the API key masked (safe to print or save)."""
        d = dataclasses.asdict(self)
        d["api_key"] = "***" if self.api_key and self.api_key != "EMPTY" else self.api_key
        return d


def _colab_secret_getter() -> Callable[[str], str | None] | None:
    """Return a getter for Colab Secrets, or None outside Colab.

    ``userdata.get`` raises when a secret is missing or notebook access was not granted, so every
    lookup is wrapped and turned into ``None``.
    """
    try:
        from google.colab import userdata  # type: ignore[import-not-found]
    except Exception:  # not in Colab (ImportError) or Colab internals unavailable
        return None

    def _get(name: str) -> str | None:
        try:
            value = userdata.get(name)
        except Exception:  # SecretNotFoundError, NotebookAccessError, TimeoutException, ...
            return None
        return value or None

    return _get


def load_config(**overrides: Any) -> Config:
    """Build a :class:`Config` from Colab Secrets, ``OKEVAL_*`` env vars and keyword overrides.

    Args:
        **overrides: any :class:`Config` attribute, applied last. ``None`` values are applied as
            given (useful for ``judge_model=None``).

    Raises:
        TypeError: if an override names an unknown setting.
    """
    valid = {f.name for f in dataclasses.fields(Config)}
    unknown = sorted(set(overrides) - valid)
    if unknown:
        raise TypeError(f"unknown Config setting(s): {unknown}; valid: {sorted(valid)}")

    values: dict[str, Any] = {}

    # 2. Colab Secrets (lowest of the external sources).
    colab_get = _colab_secret_getter()
    if colab_get is not None:
        for attr, name in ENV_VARS.items():
            secret = colab_get(name)
            if secret:
                values[attr] = secret.strip()

    # 3. Environment variables win over Colab Secrets (lets a cell set os.environ explicitly).
    for attr, name in ENV_VARS.items():
        env_val, _source = _env(name)   # OKEVAL_* name, or its SFA_* alias when that is unset or blank
        if env_val:
            values[attr] = env_val.strip()

    # 4. Explicit keyword overrides win over everything.
    values.update(overrides)
    return Config(**values)


# ------------------------------------------------------------------------------------------------ record tools
# (was okrec/config.py)

B_DIR = PRIVATE_DIR                                    # display_path() base (was notebooks/object_knowledge_eval)
DEFAULT_WAREHOUSE = B_DIR / "okeval_run" / "warehouse.duckdb"
CONTAINER_SOURCE = "/data/warehouse.read.duckdb"       # constant, not configurable
FORBIDDEN_SOURCE = "/data/warehouse.duckdb"            # the sync-worker's writer file: never touched

#: RecordsConfig attribute -> environment variable.
RECORDS_ENV_VARS: dict[str, str] = {
    "warehouse": "OKEVAL_WAREHOUSE",
    "container": "OKEVAL_WAREHOUSE_CONTAINER",
    "tz": "OKEVAL_RECORDS_TZ",
    "today": "OKEVAL_RECORDS_TODAY",
    "timeout_s": "OKEVAL_RECORDS_TIMEOUT_S",
    "max_steps": "OKEVAL_RECORDS_MAX_STEPS",
    "max_age_h": "OKEVAL_RECORDS_MAX_AGE_H",
}

_CONTAINER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@dataclass(frozen=True)
class RecordsConfig:
    """Where the snapshot is, how today and the business time zone are found, and query limits."""

    warehouse: str = str(DEFAULT_WAREHOUSE)
    container: str = "sf-local-ai-orchestrator-1"
    tz: str | None = None            # None -> Organization.TimeZoneSidKey -> "UTC"
    today: date | None = None        # pins "today" (reproducible runs)
    timeout_s: float = LIMITS.records_statement_timeout_s   # 10.0, per SQL statement
    call_budget_s: float = LIMITS.records_call_budget_s     # 20.0, per tool call (all its statements)
    max_steps: int = LIMITS.records_max_steps               # 12, record agent tool rounds (nudges, E4h and
    #                                                         find_references use rounds)
    max_age_h: float = LIMITS.records_max_age_h             # 24.0, staleness warning


def _check_tz(value: str, source: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValueError(f"unknown time zone '{value}' in {source}") from None
    return value


def _coerce(attr: str, value: Any, source: str) -> Any:
    """Validate and convert one setting; ``source`` names the env var or 'override'."""
    if attr == "warehouse":
        return str(value)
    if attr == "container":
        if not _CONTAINER_RE.match(str(value)):
            raise ValueError(f"{source} is not a container name")
        return str(value)
    if attr == "tz":
        return None if value in (None, "") else _check_tz(str(value), source)
    if attr == "today":
        if value in (None, "") or isinstance(value, date):
            return value or None
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value).strip()):
                raise ValueError
            return date.fromisoformat(str(value).strip())
        except ValueError:
            raise ValueError(f"{source} must be YYYY-MM-DD") from None
    if attr in ("timeout_s", "call_budget_s"):
        try:
            v = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{source} must be a number between 1 and 120") from None
        if not 1 <= v <= 120:
            raise ValueError(f"{source} must be a number between 1 and 120")
        return v
    if attr == "max_steps":
        try:
            v = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{source} must be an integer between 2 and 30") from None
        if not 2 <= v <= 30:
            raise ValueError(f"{source} must be an integer between 2 and 30")
        return v
    if attr == "max_age_h":
        try:
            v = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{source} must be a number greater than 0") from None
        if v <= 0:
            raise ValueError(f"{source} must be a number greater than 0")
        return v
    return value


def load_records_config(**overrides: Any) -> RecordsConfig:
    """Build a :class:`RecordsConfig` from defaults, ``OKEVAL_*`` env vars and keyword overrides.

    ``None`` overrides are skipped (an unset CLI flag), so they never hide an env value.

    Raises:
        TypeError: an override names an unknown setting.
        ValueError: a value is invalid (the message names the env var or override).
    """
    valid = {f.name for f in dataclasses.fields(RecordsConfig)}
    unknown = sorted(set(overrides) - valid)
    if unknown:
        raise TypeError(f"unknown RecordsConfig setting(s): {unknown}; valid: {sorted(valid)}")
    values: dict[str, Any] = {}
    for attr, name in RECORDS_ENV_VARS.items():
        raw, source = _env(name)   # OKEVAL_* name, or its SFA_* alias when that is unset or blank
        if raw is not None and raw.strip():
            values[attr] = _coerce(attr, raw.strip(), source)
    for attr, value in overrides.items():
        if value is None:  # an unset CLI flag: keep the env / default value
            continue
        values[attr] = _coerce(attr, value, f"--{attr.replace('_', '-')}" if attr in ("tz", "today", "warehouse") else attr)
    return RecordsConfig(**values)


# ------------------------------------------------------------------------------------------------ spelling help
# (the term resolver, docs/resolver.md; how it is built and used: docs/resolver_integration.md)

#: ResolverConfig attribute -> environment variable. sf_assistant's own names (no OKEVAL_* name, so no alias).
RESOLVER_ENV_VARS: dict[str, str] = {"enabled": "SFA_RESOLVER", "lexicon": "SFA_LEXICON"}


@dataclass(frozen=True)
class ResolverConfig:
    """The spelling help: the typo-tolerant term resolver (docs/resolver.md). On by default (owner, 2026-10-26);
    off (``SFA_RESOLVER=off``), no resolver is built and every code path is the one without it.

    ``enabled``: ``SFA_RESOLVER`` (``on``, unset or empty: True; ``off``: False). ``lexicon``: the English word-list
    files (``SFA_LEXICON``, paths separated by ``os.pathsep``); None: the system lists
    ``/usr/share/dict/american-english`` and ``british-english`` (the image installs them: web/Dockerfile). Without a
    readable word list the spelling help stays off, because ordinary words would be taken for typos. ``problem``: why
    the switch could not be read (:func:`resolver_settings`: an ``SFA_RESOLVER`` other than off, on or empty); the
    spelling help is then off (``enabled`` False, even when the caller left it at the default) and its start-up line
    says so, and nothing else stops (docs/resolver_integration.md, "Build": never fatal). A path that must stay exact
    whatever the switch says (the evaluation's reference replay) passes ``ResolverConfig(enabled=False)``."""

    enabled: bool = True
    lexicon: tuple[str, ...] | None = None
    problem: str = ""

    def __post_init__(self) -> None:
        if self.problem and self.enabled:   # a switch that could not be read is off, never on by the default
            object.__setattr__(self, "enabled", False)


def load_resolver_config(**overrides: Any) -> ResolverConfig:
    """Build a :class:`ResolverConfig` from ``SFA_RESOLVER`` / ``SFA_LEXICON`` and keyword overrides (``enabled``,
    ``lexicon``; a None override is skipped, so it never hides an env value).

    Raises:
        TypeError: an override names an unknown setting.
        ValueError: ``SFA_RESOLVER`` (or ``enabled``) is neither off nor on.
    """
    valid = {f.name for f in dataclasses.fields(ResolverConfig)}
    unknown = sorted(set(overrides) - valid)
    if unknown:
        raise TypeError(f"unknown ResolverConfig setting(s): {unknown}; valid: {sorted(valid)}")
    values: dict[str, Any] = {}
    raw = os.environ.get(RESOLVER_ENV_VARS["enabled"])
    if raw is not None and raw.strip():
        if raw.strip().lower() not in ("off", "on"):
            raise ValueError(f"{RESOLVER_ENV_VARS['enabled']} must be off or on")
        values["enabled"] = raw.strip().lower() == "on"
    raw = os.environ.get(RESOLVER_ENV_VARS["lexicon"])
    if raw is not None and raw.strip():
        values["lexicon"] = tuple(p.strip() for p in raw.split(os.pathsep) if p.strip())
    for attr, value in overrides.items():
        if value is None:
            continue
        if attr == "enabled":
            if not isinstance(value, bool):
                raise ValueError("enabled must be True or False (SFA_RESOLVER off or on)")
            values[attr] = value
        else:
            values[attr] = (value,) if isinstance(value, (str, Path)) else tuple(str(p) for p in value)
    return ResolverConfig(**values)


def resolver_settings() -> ResolverConfig:
    """:func:`load_resolver_config` for an entry point (:func:`load_settings`, ``open_records`` and
    ``pipeline.build`` without settings): an invalid ``SFA_RESOLVER`` never stops a run, a page or the MCP server; it
    gives the switched-off config with ``problem`` set, and the start-up line ``spelling help off: <problem>``."""
    try:
        return load_resolver_config()
    except ValueError as exc:
        return ResolverConfig(enabled=False, problem=str(exc))


# ------------------------------------------------------------------------------------------------ merge switch
# (latency plan step 2, the owner's go of 2026-10-29: the lean-mode merge by code; docs/runbook.md "How answers are
# merged")

#: The merge switch's environment variable: sf_assistant's own name (no OKEVAL_* name, so no alias).
MERGE_ENV_VAR = "SFA_MERGE"
#: Its values: code (the default) and model (the merge call, as before 2026-10-29).
MERGE_MODES = ("code", "model")


@dataclass(frozen=True)
class MergeConfig:
    """How ``pipeline.answer`` merges a lean answer (the record answer and the lean facts block).

    ``by``: ``code`` (the default) adds the relevant field-meaning bullets of the facts block to the record answer by
    code, with no model call (``agent.merge.code_merge``); ``model`` makes the merge call (``agent.merge.combine``), as
    before. A FIELD CHECK line, the full knowledge agent and a record answer that still plans aloud always get the
    merge call. ``problem``: why ``SFA_MERGE`` could not be read (:func:`merge_settings`); ``by`` is then code and
    nothing stops."""

    by: str = "code"
    problem: str = ""


def merge_settings() -> MergeConfig:
    """The merge switch now (``SFA_MERGE``, read at each call, so a test or an evaluation run sets it per run):
    ``code``, unset or empty: code; ``model``: the merge call. Any other value never stops a run, a page or the MCP
    server: it gives code with ``problem`` set ("SFA_MERGE must be code or model"; the value itself is not repeated)."""
    raw = (os.environ.get(MERGE_ENV_VAR) or "").strip().lower()
    if not raw:
        return MergeConfig()
    if raw not in MERGE_MODES:
        return MergeConfig(problem=f"{MERGE_ENV_VAR} must be {' or '.join(MERGE_MODES)}")
    return MergeConfig(by=raw)


# ------------------------------------------------------------------------------------------------ table switch
# (latency plan step 3, the owner's go of 2026-10-29: the rows of a record answer written by code; docs/runbook.md "How
# record rows are written")

#: The table switch's environment variable: sf_assistant's own name (no OKEVAL_* name, so no alias).
TABLES_ENV_VAR = "SFA_TABLES"
#: Its values: model (the record agent types the rows, as before 2026-10-29) and code (agent.tables writes them).
TABLES_MODES = ("model", "code")
#: The default (unset, empty or invalid). It stays model until the owner's go; the go-live is this one line ("code").
TABLES_DEFAULT = "model"


@dataclass(frozen=True)
class TablesConfig:
    """Who writes the rows of a record answer (``RecordSession.ask``, ``pipeline.answer``).

    ``by``: ``model`` (:data:`TABLES_DEFAULT` until the owner's go-live) leaves the rows to the record agent, as
    before; ``code`` has the record agent write a table line per result and code write that result's rows from what
    the model saw (``agent.tables``). ``problem``: why ``SFA_TABLES`` could not be read (:func:`tables_settings`);
    ``by`` is then :data:`TABLES_DEFAULT` and nothing stops."""

    by: str = TABLES_DEFAULT
    problem: str = ""


def tables_settings() -> TablesConfig:
    """The table switch now (``SFA_TABLES``, read at each call, so a test or an evaluation run sets it per run):
    unset or empty: :data:`TABLES_DEFAULT`; ``model`` or ``code``. Any other value never stops a run, a page or the
    MCP server: it gives :data:`TABLES_DEFAULT` with ``problem`` set ("SFA_TABLES must be model or code"; the value
    itself is not repeated)."""
    raw = (os.environ.get(TABLES_ENV_VAR) or "").strip().lower()
    if not raw:
        return TablesConfig()
    if raw not in TABLES_MODES:
        return TablesConfig(problem=f"{TABLES_ENV_VAR} must be {' or '.join(TABLES_MODES)}")
    return TablesConfig(by=raw)


# ------------------------------------------------------------------------------------------------ chart switch
# (the charts of 2026-11-03: a question that asks for a chart gets one, drawn by code; docs/runbook.md "Charts
# (SFA_CHARTS)")

#: The chart switch's environment variable: sf_assistant's own name (no OKEVAL_* name, so no alias).
CHARTS_ENV_VAR = "SFA_CHARTS"
#: Its values: off (no chart, every answer as before) and on (agent.charts draws them).
CHARTS_MODES = ("off", "on")
#: The default (unset or empty): on since the owner's go-live of 2026-11-04. Back to "off" is this line and its pin in
#: tests/test_settings.py.
CHARTS_DEFAULT = "on"


@dataclass(frozen=True)
class ChartsConfig:
    """Whether a question that asks for a chart gets one (``pipeline.answer``, ``RecordSession``, ``SfaServer``).

    ``mode``: ``off`` changes nothing; ``on`` (:data:`CHARTS_DEFAULT` since the owner's go-live of 2026-11-04) has code
    draw the chart from the record results (``agent.charts``). ``problem``: why ``SFA_CHARTS`` could not be read
    (:func:`charts_settings`); ``mode`` is then ``off`` and nothing stops."""

    mode: str = CHARTS_DEFAULT
    problem: str = ""

    @property
    def on(self) -> bool:
        return self.mode == "on"


def charts_settings() -> ChartsConfig:
    """The chart switch now (``SFA_CHARTS``, read at each call, so a test or an evaluation run sets it per run): unset
    or empty: :data:`CHARTS_DEFAULT`; ``off`` or ``on``. Any other value never stops a run, a page or the MCP server: it
    gives ``off`` with ``problem`` set ("SFA_CHARTS must be off or on"; the value itself is not
    repeated)."""
    raw = (os.environ.get(CHARTS_ENV_VAR) or "").strip().lower()
    if not raw:
        return ChartsConfig()
    if raw not in CHARTS_MODES:
        return ChartsConfig(mode="off", problem=f"{CHARTS_ENV_VAR} must be {' or '.join(CHARTS_MODES)}")
    return ChartsConfig(mode=raw)


# ------------------------------------------------------------------------------------------------ SFA_* aliases

#: OKEVAL_* name -> its SFA_* alias (read only when the OKEVAL_* name is unset or blank).
ENV_ALIASES: dict[str, str] = {name: "SFA_" + name[len("OKEVAL_"):]
                               for name in (*ENV_VARS.values(), *RECORDS_ENV_VARS.values())}


def _env(name: str) -> tuple[str | None, str]:
    """``(raw value, source name)`` of one setting: ``os.environ[name]``, or its SFA_* alias when ``name`` is unset
    or blank and the alias is set. Without SFA_* variables this is exactly ``(os.environ.get(name), name)``."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        alias = ENV_ALIASES.get(name)
        alt = os.environ.get(alias) if alias else None
        if alt is not None and alt.strip():
            return alt, alias
    return raw, name


# ------------------------------------------------------------------------------------------------ one Settings object

@dataclass(frozen=True)
class Settings:
    """Everything one run needs. ``records`` is None when records are off (``--no-records``).

    What reads each field:

    * ``model``: the knowledge agent, the LLM client and the record session (:func:`sf_assistant.pipeline.build`);
    * ``records``: the record session (``pipeline.open_record_session``; None = records off);
    * ``knowledge_dir``: the cli's default knowledge files and the folder the record catalog reads;
    * ``components_dir``: the component knowledge (flows and ``index.json``) that :func:`sf_assistant.pipeline.build`
      gives the knowledge agent of several object files (the component tools); None, or a folder without
      ``index.json``: no component tools;
    * ``transcript_dirs``: where the cli may write --file transcripts with record data (relative to ``home``);
    * ``questions_dir``: the default question folder of the evaluation tools (the ``questions/...`` set files of
      ``evaluation/sets.yaml`` and ``score_records --golden``);
    * ``limits``: the defaults :func:`load_settings` gives :class:`Config` and :class:`RecordsConfig` (the run reads
      ``model`` / ``records``), the cli's ``max_rows`` (--max-rows default) and ``warmup_timeout_s``;
    * ``resolver``: the spelling help (:class:`ResolverConfig`): :func:`sf_assistant.pipeline.build` builds the
      knowledge resolver and ``pipeline.open_record_session`` the full one only when ``resolver.enabled``.

    Bound at import, so fixed per process and checked here (a Settings that changes one raises ValueError):
    ``home``, ``private_dir`` and ``prompts_dir`` (``SFA_HOME``, ``SFA_PROMPTS_DIR``; the prompt texts, the
    snapshot path and the display base are derived from them at import) and the :data:`IMPORT_BOUND_LIMITS`.
    """

    model: Config
    records: RecordsConfig | None = None
    home: Path = HOME
    private_dir: Path = PRIVATE_DIR
    knowledge_dir: Path = KNOWLEDGE_DIR
    components_dir: Path | None = COMPONENTS_DIR
    prompts_dir: Path = PROMPTS_DIR
    questions_dir: Path = QUESTIONS_DIR
    transcript_dirs: tuple[str, ...] = TRANSCRIPT_DIRS
    limits: Limits = LIMITS
    resolver: ResolverConfig = ResolverConfig()      # the spelling help: on unless SFA_RESOLVER=off

    def __post_init__(self) -> None:
        for name, bound in (("home", HOME), ("private_dir", PRIVATE_DIR), ("prompts_dir", PROMPTS_DIR)):
            if Path(getattr(self, name)) != bound:
                raise ValueError(f"Settings.{name} {getattr(self, name)} is not {bound}: it is bound when sf_assistant "
                                 "is imported (set SFA_HOME / SFA_PROMPTS_DIR before the import)")
        for name in IMPORT_BOUND_LIMITS:
            if getattr(self.limits, name) != getattr(LIMITS, name):
                raise ValueError(f"Settings.limits.{name} = {getattr(self.limits, name)!r}: the modules bind it at "
                                 f"import ({getattr(LIMITS, name)!r}), so it cannot change per run")

    def safe_dict(self) -> dict[str, Any]:
        """The settings as a dict with the API key masked (safe to print or save)."""
        return {"model": self.model.safe_dict(),
                "records": dataclasses.asdict(self.records) if self.records is not None else None,
                "home": str(self.home), "private_dir": str(self.private_dir), "knowledge_dir": str(self.knowledge_dir),
                "components_dir": str(self.components_dir) if self.components_dir is not None else None,
                "prompts_dir": str(self.prompts_dir), "questions_dir": str(self.questions_dir),
                "transcript_dirs": list(self.transcript_dirs), "limits": dataclasses.asdict(self.limits),
                "resolver": {"enabled": self.resolver.enabled,
                             "lexicon": list(self.resolver.lexicon) if self.resolver.lexicon is not None else None,
                             **({"problem": self.resolver.problem} if self.resolver.problem else {})}}


def load_settings(*, records: bool = True, warehouse: str | None = None, tz: str | None = None,
                  today: Any = None, knowledge_dir: str | Path | None = None, questions_dir: str | Path | None = None,
                  transcript_dirs: tuple[str, ...] | None = None, limits: Limits | None = None,
                  components_dir: str | Path | None = None, **model_overrides: Any) -> Settings:
    """Build the :class:`Settings` of one run.

    ``records``: load :class:`RecordsConfig` (``OKEVAL_*`` env vars, then the ``warehouse``/``tz``/``today`` flags;
    a None flag keeps the env or default value); False leaves ``records`` None, so a bad record setting cannot stop a
    run without records. ``model_overrides`` go to :func:`load_config` (Colab Secrets, then ``OKEVAL_*``).
    The record settings are read first, as the cli always did.

    ``knowledge_dir``, ``questions_dir``, ``transcript_dirs``, ``components_dir``: the run's folders (None:
    :data:`KNOWLEDGE_DIR`, :data:`QUESTIONS_DIR`, :data:`TRANSCRIPT_DIRS`, :data:`COMPONENTS_DIR`). ``limits``
    (None: :data:`LIMITS`) gives the defaults of the Config and RecordsConfig settings it holds (an ``OKEVAL_*``
    variable, a flag or a model override still wins) and the cli's; with the default limits the result is exactly the one of the configs' own defaults. ``resolver``
    comes from ``SFA_RESOLVER`` / ``SFA_LEXICON`` (:func:`resolver_settings`), read last; an invalid
    ``SFA_RESOLVER`` leaves it off with ``resolver.problem`` set (never an error).

    Raises:
        ValueError: a record setting is invalid (the message names the env var or flag), or ``limits`` changes one
            of the :data:`IMPORT_BOUND_LIMITS`.
        TypeError: an override names an unknown setting.
    """
    lim = LIMITS if limits is None else limits
    rcfg = load_records_config(warehouse=warehouse, tz=tz, today=today) if records else None
    if rcfg is not None:   # a limit that differs from LIMITS is the default: an OKEVAL_* (or SFA_*) value wins over it
        own = {}
        for attr, name in _RECORDS_LIMITS.items():
            env = RECORDS_ENV_VARS.get(attr)
            if getattr(lim, name) != getattr(LIMITS, name) and not (env and (_env(env)[0] or "").strip()):
                own[attr] = _coerce(attr, getattr(lim, name), f"limits.{name}")
        if own:
            rcfg = dataclasses.replace(rcfg, **own)
    model_defaults = {attr: getattr(lim, name) for attr, name in _CONFIG_LIMITS.items()
                      if getattr(lim, name) != getattr(LIMITS, name) and attr not in model_overrides}
    return Settings(model=load_config(**model_defaults, **model_overrides), records=rcfg,
                    knowledge_dir=KNOWLEDGE_DIR if knowledge_dir is None else Path(knowledge_dir),
                    components_dir=COMPONENTS_DIR if components_dir is None else Path(components_dir),
                    questions_dir=QUESTIONS_DIR if questions_dir is None else Path(questions_dir),
                    transcript_dirs=TRANSCRIPT_DIRS if transcript_dirs is None else tuple(transcript_dirs),
                    limits=lim, resolver=resolver_settings())


#: Every environment variable: ``(name, default, what it sets)``. Each ``OKEVAL_*`` name also reads its ``SFA_*``
#: alias (:data:`ENV_ALIASES`) when it is unset or blank. The paths are read once, at import.
ENV_DOC: tuple[tuple[str, str, str], ...] = (
    ("OKEVAL_BASE_URL", Config.base_url, "OpenAI-compatible endpoint; records need one on this machine"),
    ("OKEVAL_API_KEY", Config.api_key, "API key (never printed)"),
    ("OKEVAL_MODEL", Config.model, "model of every call"),
    ("OKEVAL_JUDGE_MODEL", "(same as OKEVAL_MODEL)", "judge model of the evaluation"),
    ("OKEVAL_WAREHOUSE", ".private/okeval_run/warehouse.duckdb", "local warehouse snapshot (--warehouse)"),
    ("OKEVAL_WAREHOUSE_CONTAINER", RecordsConfig.container, "container of --refresh-warehouse"),
    ("OKEVAL_RECORDS_TZ", "(the org's Organization.TimeZoneSidKey, else UTC)", "business time zone (--tz)"),
    ("OKEVAL_RECORDS_TODAY", "(today)", "pins today for record periods, YYYY-MM-DD (--today)"),
    ("OKEVAL_RECORDS_TIMEOUT_S", str(RecordsConfig.timeout_s), "one SQL statement, 1-120 s"),
    ("OKEVAL_RECORDS_MAX_STEPS", str(RecordsConfig.max_steps), "record agent tool rounds, 2-30"),
    ("OKEVAL_RECORDS_MAX_AGE_H", str(RecordsConfig.max_age_h), "snapshot age warning, hours > 0"),
    ("SFA_HOME", "(the sf_assistant folder)", "base of every default path"),
    ("SFA_KNOWLEDGE_DIR", "knowledge/objects", "object knowledge files"),
    ("SFA_COMPONENTS_DIR", "knowledge/components", "component knowledge (flows/*.json and index.json; no index.json: "
                                                   "no component tools)"),
    ("SFA_PROMPTS_DIR", "prompts", "prompt files prompts/<id>.md"),
    ("SFA_QUESTIONS_DIR", ".private/questions", "question files"),
    ("SFA_RESOLVER", "on", "spelling help (the typo-tolerant term resolver): off or on"),
    ("SFA_LEXICON", "(the system lists /usr/share/dict/american-english and british-english)",
     "English word lists of the spelling help, paths separated by ':'; none readable: the spelling help stays off"),
    (MERGE_ENV_VAR, MergeConfig.by, "merge of a lean answer without a FIELD CHECK line: code (the relevant "
                                    "field-meaning bullets added by code, no model call) or model (the merge call, as "
                                    "before 2026-10-29); another value: code, and the JSONL says merge_switch"),
    (TABLES_ENV_VAR, TablesConfig.by, "who writes a record answer's rows: model (the record agent types them, as "
                                      "before 2026-10-29) or code (the record agent writes a table line per result "
                                      "and code writes that result's rows from what the model saw); another value: "
                                      "model, and the JSONL says tables_switch"),
    (CHARTS_ENV_VAR, ChartsConfig.mode, "a chart for a question that asks for one, drawn by code from the record "
                                        "results: off (no chart, as before 2026-11-03) or on; another value: off, and "
                                        "the JSONL says charts_switch"),
)
