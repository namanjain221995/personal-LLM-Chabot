"""The ask flow as functions (the compute part of the notebook folder's ask.py); nothing here prints.

* :func:`build` loads the knowledge files and builds the knowledge tool registry and the LLM client (from
  ``settings.model`` of a :class:`sf_assistant.config.Settings`, or the environment); with several knowledge files
  also the flow components (``settings.components_dir``, ``knowledge/components``, when it holds an ``index.json``:
  the 3 component tools and the components note of the system prompt); with the spelling help on
  (``SFA_RESOLVER=on``) also the knowledge term resolver (``registry.resolver``, start-up lines
  ``registry.resolver_lines``);
* :func:`open_record_session` opens the record session over the local snapshot (or says why records are off); with
  the spelling help on its catalog gets the full term resolver (``session.tools.catalog.resolver``);
* :func:`answer` answers one question: the route call, the knowledge step (lean or full) next to the record
  agent, the merge (by code in lean mode unless ``SFA_MERGE=model``, else the merge call) and the code checks, and
  returns an :class:`Outcome`; with the spelling help on the knowledge step gets the question's spelling block and the
  final answer states each correction in code (:func:`question_spelling`, :func:`spelling_final`);
* :func:`result_row` turns an :class:`Outcome` into the --file summary row and its JSONL record (with the answer's
  ``timeline``: :mod:`sf_assistant.agent.timeline`, ``merge_by``: which merge ran, ``record_nudge_kinds``: the prompt
  ids of the draft-check messages, and with ``SFA_TABLES=code`` ``tables_by`` and ``code_tables``: the record rows code
  wrote; with ``SFA_CHARTS=on`` ``charts``: the chart of a chart question, drawn by code in :mod:`sf_assistant.agent.
  charts`);
* :func:`run_question` = :func:`answer` + :func:`result_row`, without printing (for the web page).

The prerequisites of 2026-11-25 (no model call of their own): small talk gets a fixed line before anything else
(P6, :mod:`sf_assistant.agent.small_talk`); the display words leave the question before the knowledge and record search
and come back as flags (P3, :mod:`sf_assistant.agent.display`); a record step that ran no record tool, or whose every
call failed, after its one forced round, gets a clarifying question code writes (P2, :mod:`sf_assistant.agent.clarify`).

``sf_assistant.cli.ask`` runs answer -> print -> result_row, exactly as ask.py's ``ask()`` did. ``--no-records``
imports no record module and no duckdb: every record import below is inside a function.
"""
from __future__ import annotations

import dataclasses
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from sf_assistant import config
from sf_assistant import prompts as _prompt_files
from sf_assistant.agent import knowledge_prompts as prompts
from sf_assistant.agent import timeline
from sf_assistant.agent.knowledge_agent import QuestionSpelling, answer_question
from sf_assistant.agent.knowledge_prompts import MULTI_AGENT_SYSTEM, knowledge_system
from sf_assistant.agent.llm import LLM
from sf_assistant.agent.question import Question
from sf_assistant.config import (ResolverConfig, Settings, charts_settings, load_config, merge_settings,
                                 resolver_settings, tables_settings)
from sf_assistant.knowledge.components import ComponentError, load_components
from sf_assistant.knowledge.search import KnowledgeIndex
from sf_assistant.knowledge.store import ObjectKnowledge
from sf_assistant.tools.knowledge_tools import (MultiToolRegistry, ToolRegistry, question_spans, read_as_pairs,
                                               says_words, with_read_as_line)

KNOWLEDGE_DIR = str(config.KNOWLEDGE_DIR)   # every knowledge file the record tools read (default)


def build(knowledge_paths: list[str], settings: Settings | None = None):
    """``(cfg, object names, registry, system prompt or None, llm)`` for the knowledge files ``knowledge_paths``.

    ``cfg`` is ``settings.model`` (or, without ``settings``, :func:`load_config` from the environment) with
    ``knowledge_path`` set to the first file. Exits with the problems when a knowledge file is invalid.

    Flow components (several files only): the folder ``settings.components_dir`` (without ``settings``:
    ``config.COMPONENTS_DIR``) when it holds an ``index.json``: the registry gets the component tools
    (``registry.components``) and the system prompt the components note (:func:`~sf_assistant.agent.knowledge_prompts.
    knowledge_system`); a folder without ``index.json`` (or None) leaves both as they were. Exits with the problems
    when a component file is invalid, as for a knowledge file.

    The spelling help (``settings.resolver``, or ``SFA_RESOLVER`` / ``SFA_LEXICON`` without ``settings``): when it is
    on, the knowledge term resolver of these files and the word list (no stored value, no person name) is attached to
    the registry (``registry.resolver``) and ``registry.resolver_lines`` holds its start-up lines (on is the default);
    off (``SFA_RESOLVER=off``), ``registry.resolver`` stays None, nothing is built and nothing else changes. Never
    fatal (:func:`_knowledge_spelling`).
    """
    if settings is None:
        cfg = load_config(knowledge_path=knowledge_paths[0])
    else:
        cfg = dataclasses.replace(settings.model, knowledge_path=knowledge_paths[0])
    kns = []
    for path in knowledge_paths:
        kn = ObjectKnowledge.load(path)
        problems = [p for p in kn.validate() if not p.startswith("WARN:")]
        if problems:
            sys.exit(f"knowledge file {path} is invalid:\n  " + "\n  ".join(problems))
        kns.append(kn)
    if len(kns) == 1:
        registry, system = ToolRegistry(kns[0], KnowledgeIndex(kns[0].chunks())), None
    else:
        registry = MultiToolRegistry(kns, components=_components(settings))
        system = knowledge_system(registry)   # MULTI_AGENT_SYSTEM (+ the components note with the component tools)
    _knowledge_spelling(registry, kns, settings)
    return cfg, [k.api_name for k in kns], registry, system, LLM(cfg)


def _components(settings: Settings | None):
    """The flow components of :func:`build`: ``settings.components_dir`` (without ``settings``:
    ``config.COMPONENTS_DIR``) loaded, or None when it is None or holds no ``index.json``. Exits on invalid files."""
    folder = settings.components_dir if settings is not None else config.COMPONENTS_DIR
    try:
        return load_components(folder)
    except ComponentError as exc:
        sys.exit(str(exc))


def _knowledge_spelling(registry, kns: list[ObjectKnowledge], settings: Settings | None) -> None:
    """The knowledge term resolver of :func:`build` (docs/resolver_integration.md, "Build"): off -> nothing (no import,
    ``registry.resolver`` None, ``registry.resolver_lines`` []); on -> :func:`sf_assistant.knowledge.terms.
    knowledge_resolver` over the loaded files, attached with ``registry.attach_resolver``. Never raises."""
    rc = settings.resolver if settings is not None else resolver_settings()
    if rc.problem:   # an invalid SFA_RESOLVER: off, with one line (never fatal)
        registry.resolver_lines = [f"spelling help off: {rc.problem}"]
        return
    if not rc.enabled:
        return
    from sf_assistant.knowledge.terms import knowledge_resolver, lexicon_arg

    resolver, lines = knowledge_resolver([k.raw for k in kns], lexicon_arg(rc.lexicon))
    if resolver is not None:
        registry.attach_resolver(resolver)
    registry.resolver_lines = lines


def open_record_session(cfg, rcfg, knowledge_dir: str | None = None, resolver: ResolverConfig | None = None):
    """``(RecordSession, startup lines)``, or ``(None, ["records off: <reason>"])``. Never raises.

    ``cfg`` is the model :class:`~sf_assistant.config.Config` (from :func:`build`), ``rcfg`` the
    :class:`~sf_assistant.config.RecordsConfig` (``settings.records``); the record agent gets ``rcfg.max_steps`` rounds.

    The record tools read every knowledge file in ``knowledge_dir`` (default knowledge/objects/), whatever
    --knowledge gives the knowledge agent (the cli passes its own ``KNOWLEDGE_DIR``).

    ``resolver``: the spelling help settings (``settings.resolver``; None: ``SFA_RESOLVER`` / ``SFA_LEXICON`` now). On,
    the catalog gets the full term resolver (``session.tools.catalog.resolver``) and its lines follow the records lines
    (on is the default); off (``SFA_RESOLVER=off``), the lines and every tool are exactly as without it.

    The catalog's entity index (value spots, 2026-10-28: the stored record values a question can name) adds one line
    after them, ``value index cut: ...`` (``Catalog.entity_lines``), only when a limit cut it; uncut, the lines are as
    before.
    """
    knowledge_dir = knowledge_dir or KNOWLEDGE_DIR
    try:
        from sf_assistant.engine.types import RecordsUnavailable
        from sf_assistant.tools.open_records import open_records
        from sf_assistant.agent.record_session import RecordSession, is_local_endpoint
    except Exception as exc:  # noqa: BLE001 - records are optional
        return None, [f"records off: {type(exc).__name__}: {exc}"]
    if not is_local_endpoint(cfg.base_url):
        return None, ["records off: OKEVAL_BASE_URL is not on this machine; record data must stay local"]
    try:
        tools = open_records(rcfg, knowledge_dir, resolver)
    except RecordsUnavailable as exc:
        return None, [f"records off: {exc}"]
    except Exception as exc:  # noqa: BLE001
        return None, [f"records off: {type(exc).__name__}: {exc}"]
    try:
        session = RecordSession(tools, cfg, max_steps=rcfg.max_steps)
        status = tools.status()
    except Exception as exc:  # noqa: BLE001
        try:
            tools.close()
        except Exception:  # noqa: BLE001
            pass
        reason = str(exc) if isinstance(exc, RecordsUnavailable) else f"{type(exc).__name__}: {exc}"
        return None, [f"records off: {reason}"]
    lines = [status.line()]
    if getattr(tools, "made_private", False):
        lines.append(f"note: made {status.snapshot_path} private (mode 600)")
    warning = status.warning()
    if warning:
        lines.append(warning)
    lines += tools.resolver_lines   # the spelling help's start-up lines ([] when it is off)
    try:   # the entity index of the value spots (2026-10-28): one line only when it was cut, else none
        lines += list(tools.catalog.entity_lines())
    except Exception:  # noqa: BLE001 - a start-up line is optional
        pass
    return session, lines


def _rows_shown(e) -> int:
    """Rows (or groups, buckets, values...) one record query gave the model."""
    res = e.model_result if isinstance(e.model_result, dict) else {}
    if isinstance(res.get("returned"), int):
        return res["returned"]
    for key in ("rows", "groups", "buckets", "values", "matches", "changes", "segments", "objects", "references"):
        if isinstance(res.get(key), list):
            return len(res[key])
    return e.statements[0].row_count if e.statements else 0


def _record_facts(rec) -> list:
    """Facts for the JSONL sidecar: list_records rows replaced by their count."""
    from sf_assistant.agent.record_session import facts_json

    facts = json.loads(facts_json(rec.evidence, 10**9))
    for f in facts:
        res = f.get("result")
        if f.get("tool") == "list_records" and isinstance(res, dict) and isinstance(res.get("rows"), list):
            res["rows"] = {"rows_omitted": len(res["rows"])}
    return facts


def _lookup_checks(rec) -> list[dict]:
    """The lookup_check entries of the record results (JSONL sidecar), [] without records."""
    if rec is None:
        return []
    from sf_assistant.agent.record_session import lookup_checks

    return lookup_checks(rec.evidence)


def _queries(rec) -> list[dict]:
    # seconds: the tool call (its Python and its SQL); sql_seconds: the statements that ran in this question (a cached
    # or reused statement ran earlier)
    return [{"query_id": e.query_id, "tool": e.tool, "object": e.object, "ok": e.ok, "error": e.error,
             "error_code": getattr(e, "error_code", None),
             "sql": [s.sql for s in e.statements], "params": [list(s.params) for s in e.statements],
             "row_count": e.statements[0].row_count if e.statements else 0, "summary": e.summary,
             "seconds": round(float(getattr(e, "elapsed_ms", 0) or 0) / 1000, 3),
             "sql_seconds": round(sum(float(getattr(s, "elapsed_ms", 0) or 0) for s in e.statements
                                      if not getattr(s, "cached", False) and not getattr(s, "reused", False)) / 1000, 3)}
            for e in rec.evidence]


@dataclass
class Outcome:
    """One answered question: everything ``ask()`` computed before it printed (see :func:`answer`)."""

    text: str
    q: Question
    r: Any                        # AgentResult of the knowledge step, or None (records-only)
    rec: Any                      # RecordAnswer, or None (records off)
    m: Any                        # MergeResult, or None (no merge)
    mode: str | None              # knowledge step: "full", "lean", "lean+full" or None
    lean_error: str | None
    route_s: float | None
    shown_registry: Any           # the registry -v replays the knowledge tool calls through
    status: Any                   # RecordsStatus, or None
    checks: list[dict] = field(default_factory=list)   # FIELD CHECK findings passed to the merge
    k_answer: str = ""
    not_found: str = ""
    final: str = ""               # the answer with the code footer
    kind: str = "none"            # merged | records | knowledge | none
    body: str = ""                # the answer without the code footer (scored by score_records.py)
    shown: str = ""               # what is printed under the question
    rq: int = 0                   # data queries of the record agent
    used: int = 0                 # facts the answer rests on
    started_q: float = 0.0        # time.perf_counter() at the start (the row's seconds)
    records_only: bool = False
    spelling: Any = None          # QuestionSpelling (the spelling help on), else None
    footed: str | None = None     # the final answer before its "Read as" line (None: the spelling help is off)
    status_lines: list[str] = field(default_factory=list)   # the code's status lines in final (Group B item 11)
    timeline: dict = field(default_factory=dict)   # where the seconds went (agent.timeline; the JSONL "timeline")
    merge_by: str | None = None   # the merge that ran: "code" (merge.code_merge) or "model" (merge.combine); None: none
    merge_switch: str = ""        # why SFA_MERGE could not be read (the code merge was used then); "" when it could
    tables_by: str | None = None   # "code" when SFA_TABLES=code and records are on
    tables_switch: str = ""        # why SFA_TABLES could not be read
    code_tables: dict = field(default_factory=dict)   # rec.code_tables (+ "merge": restore counts)
    charts: list = field(default_factory=list)   # agent.charts items, with the picture
    charts_switch: str = ""                      # why SFA_CHARTS could not be read
    charts_error: str = ""                       # class name of a fault in charts.make
    charts_talk: bool = False                    # drop_talk removed something; no text is kept
    line_reason: str = ""          # why code answered with one fixed line: small_talk, bare_chart or display_only
    small_talk: str = ""           # the small-talk kind (agent.small_talk) of a small-talk line
    display: Any = None            # agent.display.Display of the question (None: not split, e.g. small talk)
    clarify: dict = field(default_factory=dict)   # the clarifying question's why, from, objects and choices (P2)


# ------------------------------------------------------------- the spelling help (docs/resolver_integration.md (1), (6))
#: Answer kinds of :func:`answer` whose text gets the "Read as" line.
READ_AS_KINDS = ("merged", "records", "knowledge")
#: The answer's lead sentence for a person the record tools read as another stored name (prompts/read_as_person_line.md).
READ_AS_PERSON_LINE = _prompt_files.text("read_as_person_line")


def question_spelling(text: str, registry: Any, session: Any = None) -> QuestionSpelling | None:
    """The spelling help of one question, or None when it is off (``SFA_RESOLVER=off``: no resolver on the record
    session's catalog nor on the knowledge registry).

    One ``spell_correct`` of ``text`` by the full resolver when a record session is open (``session.tools.catalog.
    resolver``: it knows the stored names, so a person's name is never read as metadata), else by the knowledge
    resolver (``registry.resolver``). Records off, the knowledge block keeps the ``corrected`` spans only: that
    resolver knows no person name, and the control sets showed its did-you-means on surnames ("may mean: Sub Params
    Count" for a person's last name), while the search still reads a misspelt word through its expander. Never
    raises."""
    tools = getattr(session, "tools", None) if session is not None else None
    full = getattr(getattr(tools, "catalog", None), "resolver", None)
    resolver = full if full is not None else getattr(registry, "resolver", None)
    if resolver is None:
        return None
    try:
        found = list(resolver.spell_correct(text))
    except Exception:  # noqa: BLE001 - the spelling help is optional
        found = []
    knowledge = question_spans(resolver, text, stored_values=False, corrections=found)
    if full is None:   # records off: no person names to tell a surname from a misspelt metadata word
        knowledge = [s for s in knowledge if s.decision == "corrected"]
    return QuestionSpelling(text, knowledge,
                            question_spans(full, text, stored_values=True, corrections=found) if full is not None
                            else [])


def _span_names(span: Any) -> list[str]:
    """The names a corrected question span stands for: its spelling and the API names, values or components of its
    targets (``Obj.Field=Value`` -> Value, ``Obj.Field`` -> Field, ``flow:Name`` -> Name)."""
    out = [str(span.used or "")]
    for kind, target in getattr(span, "targets", ()) or ():
        t = str(target)
        if kind in ("value", "record_type"):
            out.append(t.split("=", 1)[-1])
        elif kind == "component":
            out.append(t.split(":", 1)[-1])
        else:
            out.append(t.rsplit(".", 1)[-1])
    return [x for x in dict.fromkeys(out) if x.strip()]


def spelling_pairs(sp: QuestionSpelling, kind: str, text: str = "", rec: Any = None, r: Any = None) -> list[tuple[str, str]]:
    """``(typed, used)`` of the corrections an answer of ``kind`` rests on, once per typed text: the question's
    corrected spans its agents saw (records and merged: the record agent's; knowledge and merged: the knowledge
    agent's), then the ``read_as`` of the successful record calls (``QueryEvidence.read_as``; records, merged) and
    of the knowledge tool calls (their trace records; knowledge, merged).

    A question span counts only when the answer rests on it: the answer ``text`` names its spelling, or a successful
    record or knowledge tool call has it among its arguments (``Kansas`` for a Hinglish ``kaunse`` that no call and no
    sentence used is no reading of the question, so it is never stated as one). A tool's own ``read_as`` always counts."""
    spans: list = []
    tool: list = []
    args: list = []
    if kind in ("merged", "records"):
        spans += sp.records
        for e in getattr(rec, "evidence", None) or []:
            if getattr(e, "ok", False):
                tool += list(getattr(e, "read_as", None) or [])
                args += [getattr(e, "arguments", None), getattr(e, "applied", None)]
    if kind in ("merged", "knowledge"):
        spans += sp.knowledge
        for c in getattr(r, "tool_calls", None) or []:
            if isinstance(c, dict) and c.get("ok"):
                tool += list(c.get("read_as") or [])
                args.append(c.get("arguments"))
    seen = str(text or "") + "\n" + json.dumps(args, default=str, ensure_ascii=False)
    spans = [s for s in spans if s.decision != "corrected" or any(says_words(seen, n) for n in _span_names(s))]
    return read_as_pairs(spans, tool)


def spelling_name_words(sp: QuestionSpelling, session: Any = None, rec: Any = None) -> tuple[str, ...]:
    """The one-word names of the question that the record side read as another, stored name: the person entries of
    the triage's ``spelling`` list (``rec.triage``, the typed words) and the ``asked`` spellings of
    ``session.tools.question_people()``. ``merge.with_name_note`` may then lead the merged answer with the record
    answer's sentence about them. () without either; never raises."""
    out: list[str] = []
    tri = getattr(rec, "triage", None) if rec is not None else None
    for x in (tri.get("spelling") if isinstance(tri, dict) else None) or []:
        if isinstance(x, dict) and x.get("kind") == "person" and x.get("decision") == "corrected":
            out.append(str(x.get("typed") or ""))
    try:
        people = session.tools.question_people() if session is not None else []
    except Exception:  # noqa: BLE001 - optional
        people = []
    out += [str(p.get("asked") or "") for p in people or [] if isinstance(p, dict)]
    return tuple(dict.fromkeys(w for w in out if w.strip() and len(w.split()) == 1))


def _person_reads(session: Any) -> list[tuple[str, str]]:
    """``(asked, stored)`` of the question's people the record tools read as another stored name (the spots with
    ``read_as`` of ``session.tools.question_people()``; only the spelling help makes them). [] on any problem."""
    try:
        people = session.tools.question_people() if session is not None else []
    except Exception:  # noqa: BLE001 - optional
        return []
    out = []
    for p in people or []:
        if isinstance(p, dict) and p.get("read_as") and p.get("asked"):
            out.append((" ".join(str(p["asked"]).split()), " ".join(str(p["read_as"]).split())))
    return list(dict.fromkeys(out))


def with_person_lead(text: str, reads: list[tuple[str, str]], said: str | None = None) -> str:
    """``text`` led by the code sentence "No record is named 'x'; the closest stored name is 'Y'."
    (:data:`READ_AS_PERSON_LINE`) for each ``(asked, stored)`` read whose asked spelling the text never names: a
    person correction is always stated, and first (decision B), even when the draft used the stored name silently.
    Unchanged for an empty text or when the text names every asked spelling. ``said``: the text whose words count as
    named (default ``text``; :func:`answer` passes the answer without the bullets the merge by code added, review of
    2026-10-29)."""
    if not (text or "").strip():
        return text
    seen = text if said is None else said
    lead = [_prompt_files.fill(READ_AS_PERSON_LINE, ASKED=a, STORED=u) for a, u in reads
            if a.lower() != u.lower() and not says_words(seen, a)]
    return " ".join(lead) + "\n\n" + text if lead else text


def spelling_final(sp: QuestionSpelling, text: str, kind: str, rec: Any = None, r: Any = None,
                   session: Any = None, inserted: Any = (), code_lines: Any = ()) -> str:
    """``text`` (the final answer, after the audits and the lookup footer) with the spelling help's code lines: the
    person lead sentence (:func:`with_person_lead`; records and merged answers) and the line "Read as: ..."
    (``knowledge_tools.with_read_as_line``) for :func:`spelling_pairs`; unchanged for another kind. ``inserted``: the
    lines the merge by code added (``merge.code_merge_lines``), which never count as naming an asked spelling.
    ``code_lines``: the chart's reason line, whose fixed words never do either."""
    if kind not in READ_AS_KINDS:
        return text
    seen = text
    if code_lines:
        from sf_assistant.agent.nudges import without_lines
        seen = without_lines(text, list(code_lines))
    out = with_read_as_line(text, spelling_pairs(sp, kind, seen, rec, r))
    if kind in ("merged", "records") and session is not None:
        said = None
        if inserted or code_lines:
            from sf_assistant.agent.nudges import without_lines
            said = without_lines(out, [*inserted, *code_lines])
        out = with_person_lead(out, _person_reads(session), said)
    return out


def with_footer(text: str, evidence: list, inserted: Any = ()) -> str:
    """``record_session.with_lookup_footer(text, evidence)``, with what the text already names read without the lines
    the merge by code added (``inserted``: its label and bullets, ``merge.code_merge_lines``; review of 2026-10-29: a
    bullet about the lookup, or about a field of the same name on another object, must not take away the footer that
    says what was counted). The footer line goes where ``with_lookup_footer`` puts it: before a last Source line, else
    at the end."""
    from sf_assistant.agent.nudges import without_lines
    from sf_assistant.agent.record_session import with_lookup_footer

    if not inserted:
        return with_lookup_footer(text, evidence)
    base = without_lines(text, list(inserted))
    footed = with_lookup_footer(base, evidence)
    if footed == base:
        return text
    b, f, lines = base.rstrip().split("\n"), footed.split("\n"), text.rstrip().split("\n")
    if len(f) == len(b) + 1 and f[-1] == b[-1]:   # placed before the last line (the Source line)
        if lines[-1] != b[-1]:   # the added lines end the text (never seen): the footer as before
            return with_lookup_footer(text, evidence)
        return "\n".join(lines[:-1] + [f[-2], lines[-1]])
    return "\n".join(lines + [f[-1]])


def _without_empty_label(text: str, label: str) -> str:
    """``text`` without the merge's label line (and the blank line before it) when no bullet is left under it. Runs
    before the lines code adds, so only blank and Source lines can follow an empty label."""
    from sf_assistant.agent.checks import _is_source_line

    lines = text.split("\n")
    if label not in lines:
        return text
    at = lines.index(label)
    if any(ln.strip() and not _is_source_line(ln) for ln in lines[at + 1:]):
        return text
    start = at - 1 if at and not lines[at - 1].strip() else at
    return "\n".join(lines[:start] + lines[at + 1:])


def _last_triage(session: Any, text: str) -> Any:
    """``session.last_triage(text)`` (the triage its route call kept), or None for a session without it. Never
    raises."""
    fn = getattr(session, "last_triage", None)
    try:
        return fn(text) if callable(fn) else None
    except Exception:  # noqa: BLE001 - optional: the knowledge step as before
        return None


#: The answer of a record question whose record step failed or came back empty (placement fixes, 2026-11-16): said
#: plainly, never a knowledge answer about something else in its place (:func:`choose_answer`).
RECORDS_FAILED_ANSWER = ("The figures for this question could not be computed this time: the record step gave no "
                         "usable answer. Please ask again, or ask for one part of it at a time.")
#: The label of a knowledge answer kept after :data:`RECORDS_FAILED_ANSWER` (only one about the objects the record
#: step read, :func:`_about_the_same`).
RECORDS_FAILED_NOTE_LABEL = "What the knowledge files say (field meanings only, no record figures):"


def _about_the_same(k_answer: str, rec: Any) -> bool:
    """The knowledge answer is about what the record step read: the lead (first paragraph, Source lines left out) of
    the knowledge answer names a custom object (``..__c``) or a custom field one of the record step's successful data
    calls read (its object, its ``tables``, the fields of its ``fields_used``; a field by its name, or as
    ``Object.Field`` of that object). Only successful data calls count (review of 2026-11-16: a ``describe_fields`` on
    ``Onboarding__c`` before the ``Interview__c`` calls of page ticket 3 kept an onboarding answer), a standard object
    such as Account is never matched in prose ("the candidate's Account"), and with no such call nothing is kept. A
    knowledge answer about other objects only (page ticket 3 of 2026-11-16: ``Application__c`` after record calls on
    ``Interview__c``) is no note about the question."""
    from sf_assistant.agent.checks import _body_text

    objects: set[str] = set()
    fields: set[tuple[str, str]] = set()
    for e in getattr(rec, "evidence", None) or []:
        if not (getattr(e, "ok", False) and getattr(e, "kind", None) == "data"
                and getattr(e, "tool", None) != "get_sync_status"):
            continue
        objects.update(str(o) for o in [getattr(e, "object", None), *(getattr(e, "tables", None) or [])] if o)
        for pair in getattr(e, "fields_used", None) or []:
            if isinstance(pair, (tuple, list)) and len(pair) == 2 and pair[0] and pair[1]:
                fields.add((str(pair[0]), str(pair[1])))
    lead = _body_text(k_answer).strip().split("\n\n", 1)[0]
    if any(re.search(rf"(?<!\w){re.escape(o)}(?!\w)", lead) for o in objects if o.endswith("__c")):
        return True
    return any(re.search(rf"(?<![\w.]){re.escape(f)}(?!\w)|(?<!\w){re.escape(o)}\.{re.escape(f)}(?!\w)", lead)
               for o, f in fields if f.endswith("__c"))


def records_failed(rec: Any) -> bool:
    """The record step of a question that needs record data failed or came back empty: ``rec.error`` is set and the
    route call said RECORDS (``triage["route"]`` True), or gave no reading (None: the call failed or its reply was
    unclear) while the record agent ran all the same and a data call of it read records (review of 2026-11-16), or a
    chart ask sent it to the records (``overruled_by`` "chart"). A stored value's overrule (route False: the route
    call judged a design question), a NO_RECORDS route and a route with no reading whose record agent read no
    records keep the knowledge answer, as before."""
    if rec is None or not getattr(rec, "error", None) or getattr(rec, "routed_out", False):
        return False
    if getattr(rec, "named_people", None):   # the people path says it in its own words ("could not be read")
        return False
    tri = getattr(rec, "triage", None) or {}
    if tri.get("route") is True or tri.get("overruled_by") == "chart":
        return True
    # no reading of the route call (it failed or was unclear) and the record agent ran all the same: a failure once a
    # data call of it read records (the question was a record question); with none, the knowledge answer as before (a
    # design question whose record agent only failed its calls, or made none)
    return bool(tri) and tri.get("route") is None and any(
        getattr(e, "kind", None) == "data" and getattr(e, "ok", False) and getattr(e, "tool", None) != "get_sync_status"
        for e in getattr(rec, "evidence", None) or [])


def choose_answer(m: Any, rec: Any, not_found: str, k_answer: str, records_only: bool = False,
                  status: Any = None) -> tuple[str, str]:
    """``(final, kind)``: the answer :func:`answer` shows (evaluation fix 2, 2026-10-22), in this order: the merged
    answer when the merge was accepted; the record answer (the merge's fallback, or the record agent's answer when
    nothing was merged); the code-written people answer (``agent.people_answer.people_answer_from``, with a Source
    line) when the record side failed or said nothing (``rec.error``), the record agent used about_people, its last
    result covers every named person of the question (``rec.named_people``, or a misspelt one's stored spelling,
    ``rec.named_spellings``; review of 2026-10-22: a result for one of two people is no answer about both, the
    knowledge answer with its warning is) and the question's names are stored
    (the triage's ``people_data``); the code's not-found answer; :data:`RECORDS_FAILED_ANSWER` (kind "none") when the
    record step of a record question failed or came back empty (:func:`records_failed`; 2026-11-16: never the
    knowledge answer in its place, which answered another question on the page); the knowledge answer
    (records-only: none). An answer with no text besides its Source line and the lines code writes around it
    (``agent.nudges.answer_text``) counts as none, so an empty answer is never shown: ``final`` is "" and ``kind``
    "none" when nothing is left. Records off (``rec`` None): the knowledge answer as before, and no record module is
    imported."""
    if rec is None:
        return k_answer, "knowledge" if k_answer.strip() else "none"
    from sf_assistant.agent.nudges import answer_text, with_source_line
    from sf_assistant.agent.people_answer import people_answer_from

    if m is not None and m.merged and answer_text(m.text):
        return m.text, "merged"
    record = m.text if m is not None and not m.merged else ("" if rec.no_records else rec.answer)
    if answer_text(record):
        return record, "records"
    if rec.error and (getattr(rec, "triage", None) or {}).get("people_data", True):
        people = people_answer_from(rec.evidence, list(getattr(rec, "named_people", None) or []),
                                    dict(getattr(rec, "named_spellings", None) or {}))
        if people:
            return with_source_line(people, rec.evidence, status), "records"
    if not_found:
        return not_found, "records"
    if records_only:
        return "", "none"
    if records_failed(rec):   # the knowledge answer never stands in for the figures; a note on the same thing may follow
        if answer_text(k_answer) and _about_the_same(k_answer, rec):   # its own Source line is left out
            from sf_assistant.agent.checks import _body_text
            return f"{RECORDS_FAILED_ANSWER}\n\n{RECORDS_FAILED_NOTE_LABEL}\n{_body_text(k_answer).strip()}", "none"
        return RECORDS_FAILED_ANSWER, "none"
    if not answer_text(k_answer):
        return "", "none"
    return k_answer, "knowledge"


def _line_outcome(text, q, registry, session, started_q, records_only, merge_cfg, tables_cfg, charts_cfg, line: str,
                  reason: str, small_talk: str = "", display: Any = None) -> Outcome:
    """The :class:`Outcome` of a question code answers with one fixed ``line`` and no model call: ``reason``
    ``bare_chart`` (a chart question that names nothing to chart), ``small_talk`` (agent.small_talk; ``small_talk`` is
    its kind) or ``display_only`` (agent.display: only how to show, or a capability ask). With a session ``rec`` has
    the shape of a NO_RECORDS route in records-only mode, which every reader of an outcome knows."""
    t_assemble = time.perf_counter()
    rec = None
    if session is not None:
        from sf_assistant.agent.record_session import RecordAnswer
        rec = RecordAnswer(question=text, answer="", no_records=True, used_data=False, routed_out=True)
    timeline.add("stage", t_assemble, stage="assemble")   # one entry: an answer of this day always has a timeline
    return Outcome(text=text, q=q, r=None, rec=rec, m=None, mode=None, lean_error=None, route_s=None,
                   shown_registry=registry, status=session.status() if session is not None else None,
                   final=line, kind="none", body="", shown=line, started_q=started_q, records_only=records_only,
                   status_lines=[line], timeline=timeline.end(), merge_switch=merge_cfg.problem,
                   tables_switch=tables_cfg.problem, charts_switch=charts_cfg.problem, line_reason=reason,
                   small_talk=small_talk, display=display)


def _bare_chart_outcome(text, q, registry, session, started_q, records_only, merge_cfg, tables_cfg, charts_cfg,
                        display: Any = None):
    """The :class:`Outcome` of a chart question that names nothing to chart: one line and no model call
    (:func:`_line_outcome`, reason ``bare_chart``)."""
    from sf_assistant.agent import charts as _charts
    return _line_outcome(text, q, registry, session, started_q, records_only, merge_cfg, tables_cfg, charts_cfg,
                         _charts.bare_line(), "bare_chart", display=display)


def _with_display(fn: Any, display: Any, typed: str | None = None) -> dict:
    """``{"display": display}`` when the session method ``fn`` takes it (``RecordSession.route`` and ``.ask``), else {}:
    a session of another shape (a test stub, an older caller) is called as before. ``typed`` (the question as typed,
    for ``RecordSession.ask`` only: the record agent's user message keeps the display words, pre-release fix of
    2026-11-25) goes as ``typed`` when ``fn`` takes it too."""
    import inspect

    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return {}
    any_kw = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    out = {"display": display} if "display" in params or any_kw else {}
    if out and typed is not None and ("typed" in params or any_kw):
        out["typed"] = typed
    return out


#: A sentence of a knowledge answer that echoes the agent's require-tools nudge ("You're right, I should not answer from
#: memory."): the prompt's own wording, never shown (review fix E-22 of the prerequisites, 2026-11-25; the L63 shape).
_NUDGE_ECHO_RE = re.compile(
    r"\b(?:should(?:\s+not|n'?t)|must(?:\s+not|n'?t)|will\s+not|won'?t|cannot|can'?t|do\s+not|don'?t)\s+answer\s+from\s+memory\b"
    r"|\banswer(?:ed|ing)?\s+from\s+memory\b"
    r"|\b(?:you'?re|you\s+are)\s+right\b[^.!?\n]{0,80}\b(?:memory|tools?|look\s+(?:it\s+)?up)\b"
    r"|\bwithout\s+(?:calling|using)\s+(?:any|a|the)\s+tools?\b", re.IGNORECASE)


def drop_nudge_echo(text: str) -> str:
    """``text`` without the sentences that echo the require-tools nudge (:data:`_NUDGE_ECHO_RE`); as it was when none
    does. Never raises."""
    try:
        if not text or not _NUDGE_ECHO_RE.search(text):
            return text
        lines = []
        for line in text.split("\n"):
            parts = re.split(r"(?<=[.!?])\s+", line)
            kept = [p for p in parts if not _NUDGE_ECHO_RE.search(p)]
            if kept or not line.strip():
                lines.append(" ".join(kept) if len(kept) != len(parts) else line)
        return "\n".join(lines).strip()
    except Exception:  # noqa: BLE001 - the answer as it was
        return text


def chart_reading(chart_ask: Any, d: Any) -> tuple[Any, Any]:
    """``(chart_ask, display)`` read the same way (``SFA_CHARTS`` on; prerequisite P3): a chart the question turns down
    (``"chart"`` in ``display.refused``) is no chart ask; a visual word, or a chart word ``charts.chart_ask`` does not
    read, is a chart ask with no named type; a chart ask the split missed sets the chart flag. The split refuses every
    refusal the chart grammar reads and keeps its skill words (review fix R1 of 2026-11-25: no string the chart tests
    pin as no chart ask becomes one here)."""
    from sf_assistant.agent import charts as _charts

    if "chart" in (getattr(d, "refused", None) or ()):
        return None, (dataclasses.replace(d, chart=False) if d.chart else d)
    if chart_ask is None and d.chart:
        return _charts.ChartAsk(type=None, bare=False), d
    if chart_ask is not None and not d.chart:
        return chart_ask, dataclasses.replace(d, chart=True)
    return chart_ask, d


def _clarify_due(rec: Any) -> bool:
    """The record step of a record question ran no record tool, or every call failed, after its forced round
    (``rec.no_data``, prerequisite P2) and no model fault ended it: code asks which question was meant."""
    if rec is None or not getattr(rec, "no_data", None) or not records_failed(rec):
        return False
    agent = getattr(rec, "agent", None)
    return agent is not None and getattr(agent, "error", None) is None


def answer(text: str, cfg, registry, system, llm, n: int, session=None, records_only: bool = False,
           lean_knowledge: bool = True, context_objects: Any = ()) -> Outcome:
    """Answer one question (no printing). The compute part of ask.py's ``ask()``, unchanged.

    ``session`` is a ``sf_assistant.agent.record_session.RecordSession`` (records on) or None (records off).
    ``lean_knowledge`` (merged mode only): make the route call first and, when it says RECORDS, run the lean
    knowledge step (``sf_assistant.agent.lean``, RECORDS_SPEC §21.19) instead of the full knowledge agent. False
    (--full-knowledge) keeps the full knowledge agent for every question, with the route call inside the record
    session. A question that a stored record value sent to the records against a NO_RECORDS route (the triage's
    ``overruled_by`` "value", :meth:`RecordSession.last_triage`; value spots, 2026-10-28) keeps the full knowledge
    agent: the route call judged it a design question, so its knowledge answer is what it got before, and the merge
    adds the record facts. People overrules keep the lean step, as before.

    The spelling help (``SFA_RESOLVER=on``: a resolver on ``session.tools.catalog`` or on ``registry``,
    docs/resolver_integration.md (1), (6)): the knowledge step gets the question followed by its spelling block (the
    misspelt metadata names; no person name, no stored value), ``q``, ``text`` and the JSONL keep the user's words,
    and after the audits and the lookup footer ``final`` (never ``body``) gets the line "Read as: ..." for the
    corrections the answer rests on, and a lead sentence for a person the record tools read as another stored name
    when the answer does not say so (:func:`spelling_final`); the merge's name note also covers a one-word name the
    record tools corrected.
    Off, ``spelling`` is None and nothing of this runs.

    After the lookup footer, ``final`` (never ``body``) of a record or merged answer gets the status lines of
    ``agent.nudges.status_lines`` (Group B item 11, docs/people.md): the status split, with the values no record
    has, of the person's records a completion verb of the question is about, when the answer names none of those
    values (``Outcome.status_lines``; the JSONL gets ``status_lines`` when there are any, and its ``lookup_footer``
    flag still says only whether the lookup footer was added).

    Which answer is shown: :func:`choose_answer` (evaluation fix 2: merged, record, code-written people answer,
    not-found answer, knowledge answer; an answer with nothing but its Source line is never shown). With records on,
    an answer to a question that does not ask for contact details then loses every e-mail address and phone number,
    whatever its kind (phase 1, 2026-10-26: :func:`sf_assistant.engine.contact.drop_contact_details`; a number the
    record tool results hold as a whole value stays), before the footer and the lines. A records-off run reads no
    record and imports no record code (``sf_assistant.engine``, tests/test_pipeline_cli.py): its answers come from the
    knowledge files, which hold no e-mail address and no phone number (kb_build masks them;
    tests/test_contact_rule.py checks the files), so it needs no check (review F7).

    The merge (latency plan step 2, the owner's go of 2026-10-29): in lean mode without a FIELD CHECK line (``checks``
    empty) code adds the relevant field-meaning bullets of the lean facts block to the record answer, with no model
    call (``agent.merge.code_merge``: at most 3, each about a field the answer used or its object and passing the
    merge's checks; none kept: the record answer, kind "records"). ``SFA_MERGE=model`` (:func:`sf_assistant.config.
    merge_settings`, read here at each call) keeps the merge call for these answers too, as before; the full knowledge
    agent (mode "full" or "lean+full"), a FIELD CHECK line and a record answer that still plans aloud
    (``agent.merge.plans_aloud``: "Let me compile ...", which the merge call rewrote away) always get it
    (``agent.merge.combine``, unchanged). ``Outcome.merge_by`` says which merge ran and ``Outcome.merge_switch`` why an
    invalid ``SFA_MERGE`` was read as code (:func:`result_row` writes both to the JSONL). The timeline's ``merge`` stage
    covers either (about 0 s for code). What follows the merge (the answer choice, the contact rule, the footer, the
    status and Read-as lines) is the same for both, except that the lines the merge by code added
    (``agent.merge.code_merge_lines``) never count as said: the lookup footer (:func:`with_footer`), the status lines
    and the person lead (:func:`with_person_lead`) read the answer without them (review of 2026-10-29). Step-2 fixes of
    2026-10-30 (docs/runbook.md): ``code_merge`` gets the record catalog's objects as a dict (name -> ObjectSpec) for
    its bullet checks, and the lines it added are read for every code-merge result, also an unmerged answer that got
    only its counted line; ``plans_aloud`` also sends thinking aloud, tool talk and Hinglish first-person planning to
    the merge call.

    Latency logging (2026-10-29): the answer's timeline (:mod:`sf_assistant.agent.timeline`) is opened here and closed
    into ``Outcome.timeline``; :func:`result_row` writes it as the JSONL ``timeline``. It only observes: the same
    calls run in the same order on the same threads.

    Rows by code (latency plan step 3; ``SFA_TABLES``, :func:`sf_assistant.config.tables_settings`, read here once per
    answer; ``model`` is the default): with ``code`` the record answer holds rows code wrote (``rec.table_blocks``).
    The merge call then gets the record answer with those rows as table lines (``agent.tables.marked``, with
    ``COMBINE_TABLES_NOTE`` appended to its system prompt), and code puts them back into an accepted merged text
    (``agent.tables.restore``; a failure gives the record answer); ``plans_aloud`` reads the record answer with its
    rows as table lines, and the merge by code keeps them verbatim. As a last resort, ``final`` (and so ``body``)
    loses any table line left. ``Outcome.tables_by``, ``code_tables`` and ``tables_switch`` go to the JSONL
    (:func:`result_row`); ``code_tables`` counts ``kept`` and ``left`` in the final answer, and ``merge`` says whether
    the merge call that got table lines was used (``merged``) and how the rows came back. With ``model`` (or records
    off) nothing of this runs.

    Charts (``SFA_CHARTS``, :func:`sf_assistant.config.charts_settings`, read here once per answer; ``on`` is the
    default): with ``on``, a question that asks for a chart (:func:`sf_assistant.agent.charts.chart_ask`) gets a chart
    code draws from the record results. A question that names nothing to chart gets one line and no model call
    (:func:`_bare_chart_outcome`). A NO_RECORDS route on any other chart question is overruled in
    :meth:`RecordSession.triage` (``overruled_by`` "chart") and keeps the full knowledge agent, as a value overrule
    does; when its merge is rejected or fails, the knowledge answer stays in the shown text, before the record answer.
    Chart talk leaves the record answer before the merge decision (``charts.drop_talk``), and ``charts.make`` runs
    after the contact rule. When it edits a merge by code's text, the lines that merge added are read anew, and a label
    with no bullet left goes. Its reason line goes after the status lines, in ``final`` only, so ``body``, the lookup
    footer flag and the Read-as line stay right. ``Outcome.charts``, ``charts_switch``, ``charts_error`` and
    ``charts_talk`` go to the JSONL (:func:`result_row`). Off, nothing of this runs and the charts module is not
    imported. On, every answer imports it, also a records-off run, which still loads no record module; its chart
    question gets the ``records_off`` line through
    ``agent.nudges`` (``engine.types`` only). No duckdb and no warehouse either way.

    Small talk (prerequisite P6 of 2026-11-25): a greeting, thanks, an acknowledgement, a goodbye, a presence check, an
    empty ask or a help ask (:func:`sf_assistant.agent.small_talk.small_talk`, a full match) gets its fixed line first,
    with no model call (:func:`_line_outcome`, ``line_reason`` "small_talk").

    Display words (prerequisite P3): :func:`sf_assistant.agent.display.split` takes the display words out of the
    question (with a session, :class:`~sf_assistant.agent.display.SessionGuards` keeps stored values and names whole and
    drops the split when the contact rule or the term contracts would read it otherwise). The search text feeds the
    spelling help, the knowledge question, the route, names and record calls, the value and people spots and the record
    hints; with ``SFA_CHARTS`` on, the record agent's user message keeps the typed text (``RecordSession.ask(...,
    typed=text)``: with the search text "plot X of last 6 months" read as "show X ..." and the model listed records
    instead of a monthly trend, pre-release check of 2026-11-25; off, it gets the search text as before); the flags
    feed the chart note, the chart overrule and the record agent's display note. ``Outcome.text``, ``q``, the merge,
    the contact rule, ``charts.make``
    and the JSONL keep the typed text. A question of display words only gets one line and no model call (``line_reason``
    "display_only": the capability line for "can you do visualizations ?", else the bare line); a chart question that
    names nothing keeps its bare chart line. A visual word (artifact, dashboard) or a chart word ``charts.chart_ask``
    does not read is a chart ask with no named type. A file ask gets the ``file`` line after the status lines (``final``
    only) when the answer used record data. The JSONL gets ``display`` (flags, words, kept) when a display word was read.

    Clarifying question (prerequisite P2): when the record step of a record question ran no record tool, or every call
    failed, after its forced round (``rec.no_data``) and no model fault ended it, ``final`` is the clarifying question
    of :func:`sf_assistant.agent.clarify.build` instead of :data:`RECORDS_FAILED_ANSWER`, kind "none", no chart and no
    line (``Outcome.clarify``; the JSONL ``clarify``). ``context_objects``: the objects the last answer named (the ask
    service and the page's follow-up path; only the clarifying question reads them).
    """
    started_q = time.perf_counter()
    timeline.begin()   # where the seconds go: stage blocks here, model, tool and check entries below (Outcome.timeline)
    merge_cfg = merge_settings()   # SFA_MERGE now: code (the default) or model; an invalid value is code, logged
    tables_cfg = tables_settings()   # SFA_TABLES now: model (the default) or code; an invalid value is model, logged
    charts_cfg = charts_settings()   # SFA_CHARTS now: on (the default) or off; an invalid value is off, logged
    q = Question(qid=f"ask-{n:03d}", category="cross_section", question=text, expected_facts=[],
                 expected_tools=["search_knowledge"], answerable=True, source="ask", gold_chunk_ids=[])
    from sf_assistant.agent import display as _display
    from sf_assistant.agent.small_talk import line as _talk_line, small_talk as _small_talk

    talk = _small_talk(text)   # P6: one fixed line, no model call
    if talk:
        return _line_outcome(text, q, registry, session, started_q, records_only, merge_cfg, tables_cfg, charts_cfg,
                             _talk_line(talk), "small_talk", small_talk=talk)
    chart_ask = None
    if charts_cfg.on:
        from sf_assistant.agent import charts as _charts
        chart_ask = _charts.chart_ask(text)
    # P3: what to fetch, apart from how to show it (code only; the spots of SessionGuards run only for a display word)
    d = _display.split(text, _display.SessionGuards(session) if session is not None else None)
    if d.empty and d.capability:   # "can you do visualizations ?": what the answers can show, no model call
        return _line_outcome(text, q, registry, session, started_q, records_only, merge_cfg, tables_cfg, charts_cfg,
                             _display.line(_display.capability_key(d, charts_cfg.on)), "display_only", display=d)
    if chart_ask is not None and chart_ask.bare:   # names nothing to chart: one line, no model call
        return _bare_chart_outcome(text, q, registry, session, started_q, records_only, merge_cfg, tables_cfg,
                                   charts_cfg, display=d)
    if d.empty:   # only how to show, nothing to fetch: one line, no model call
        return _line_outcome(text, q, registry, session, started_q, records_only, merge_cfg, tables_cfg, charts_cfg,
                             _display.line(_display.bare_key(charts_cfg.on)), "display_only", display=d)
    if charts_cfg.on:
        chart_ask, d = chart_reading(chart_ask, d)   # one reading for both steps
    search = d.search or text   # what to fetch: every search below reads it
    # the record agent's user message: the typed text, display words kept (pre-release fix of 2026-11-25); with
    # SFA_CHARTS off the search text, as before (no chart word reaches a model that can draw none)
    agent_text = text if charts_cfg.on else None
    sq = q if search == text else dataclasses.replace(q, question=search)
    with timeline.stage("spelling"):
        spelling = question_spelling(search, registry, session)   # None: the spelling help is off
    kq = spelling.knowledge_question(sq) if spelling is not None else sq   # the knowledge step's question
    r = rec = m = None
    mode = None          # knowledge step: "full", "lean", "lean+full" or None (records-only: no knowledge agent)
    lean_error = None    # the lean step raised (it should not): the full knowledge agent answered instead
    route_s = None       # seconds of the route call made here (merged mode with lean_knowledge)
    shown_registry = registry
    if session is None:
        with timeline.stage("knowledge"):
            r = answer_question(kq, llm, registry, cfg, system=system, require_tools=True)
        mode = "full"
    elif records_only:
        with timeline.stage("records"):
            rec = session.ask(search, **_with_display(session.ask, d, agent_text))
    else:
        from sf_assistant.agent.record_prompts import KNOWLEDGE_RECORDS_NOTE

        noted = (system or prompts.AGENT_SYSTEM) + KNOWLEDGE_RECORDS_NOTE
        early = lean_knowledge and callable(getattr(session, "route", None))
        routed = None
        by_value = False   # a stored record value or a chart ask overruled a NO_RECORDS route (value spots, 2026-10-28)
        if early:  # the route call picks the knowledge step: RECORDS -> lean (field meanings only)
            t_route = time.perf_counter()
            routed = session.route(search, **_with_display(session.route, d))
            route_s = time.perf_counter() - t_route
            by_value = getattr(_last_triage(session, search), "overruled_by", None) in ("value", "chart")
        mode = "lean" if routed is True and not by_value else "full"
        pool = ThreadPoolExecutor(max_workers=1)  # the record agent runs while the knowledge agent answers
        try:
            shown = _with_display(session.ask, d, agent_text)   # the record agent reads the typed text
            pending = (pool.submit(timeline.run_in, "records", session.ask, search, routed=routed, **shown) if early
                       else pool.submit(timeline.run_in, "records", session.ask, search, **shown))
            if mode == "lean":
                from sf_assistant.agent.lean import LeanRegistry, lean_knowledge_answer

                t_lean = time.perf_counter()
                try:
                    with timeline.stage("lean"):
                        r = lean_knowledge_answer(kq, llm, registry, cfg)
                    shown_registry = LeanRegistry(registry, max_calls=10**6)  # -v shows results as the step saw them
                except Exception as exc:  # noqa: BLE001 - it never raises; if it does, the full agent answers
                    lean_error = f"{type(exc).__name__}: {exc}"[:300]
                    lean_s = time.perf_counter() - t_lean
                    with timeline.stage("knowledge"):
                        r = answer_question(kq, llm, registry, cfg, system=noted, require_tools=True)
                    r.latency_s = round(r.latency_s + lean_s, 3)
                    mode = "lean+full"
            else:
                with timeline.stage("knowledge"):
                    r = answer_question(kq, llm, registry, cfg, system=noted, require_tools=True)
            try:
                with timeline.stage("wait_records"):   # the main thread idle until the record agent ends
                    rec = pending.result()
            except Exception as exc:  # noqa: BLE001 - RecordSession.ask never raises
                from sf_assistant.agent.record_session import RecordAnswer
                rec = RecordAnswer(question=search, answer="", no_records=True, used_data=False,
                                   error=f"{type(exc).__name__}: {exc}")
        finally:
            pool.shutdown(wait=False)
        if mode == "lean" and rec is not None and rec.no_records and not rec.error and not rec.routed_out:
            # the route said RECORDS, but the record agent replied NO_RECORDS: a metadata question after all, so it
            # gets the full knowledge answer (never seen in the runs so far; costs the full agent only here)
            lean_s = r.latency_s if r is not None else 0.0
            with timeline.stage("knowledge"):
                r = answer_question(kq, llm, registry, cfg, system=noted, require_tools=True)
            r.latency_s = round(r.latency_s + lean_s, 3)
            mode, shown_registry = "lean+full", registry
    if r is not None and r.answer:   # review fix E-22: no sentence that echoes the require-tools nudge is shown
        r.answer = drop_nudge_echo(r.answer)
    status = session.status() if session is not None else None
    charts_talk = False
    # "Let me plot" forces no merge call; a people answer code wrote holds no talk, and a name can hold a chart word
    if chart_ask is not None and rec is not None and rec.answer and not getattr(rec, "code_answer", False):
        kept = _charts.drop_talk(rec.answer, getattr(rec, "table_blocks", None))
        charts_talk, rec.answer = kept != rec.answer, kept
    checks: list[dict] = []  # FIELD CHECK findings passed to the merge
    merge_by = None          # the merge that ran: "code" (merge.code_merge), "model" (merge.combine) or None
    inserted: list[str] = []   # the lines the merge by code added (its label and bullets): no part of what is said
    tables_merge = None      # SFA_TABLES=code: how the merge call's text got the code rows back (restore counts)
    if rec is not None and r is not None and not rec.no_records:
        from sf_assistant.agent.record_session import combine, field_checks
        checks = field_checks(r.answer or "", rec.evidence)
        availability = session.availability() if hasattr(session, "availability") else None
        # a lean facts block carries no record numbers: the audit does not take numbers from its prose (§21.19)
        names = spelling_name_words(spelling, session, rec) if spelling is not None else ()   # one-word names read
        with timeline.stage("merge"):
            from sf_assistant.agent import tables as _tables
            from sf_assistant.agent.merge import code_merge, code_merge_lines, plans_aloud

            blocks = dict(getattr(rec, "table_blocks", None) or {})   # SFA_TABLES=code: the record answer's code rows
            # ...as table lines: the merge call never retypes them, and a cell is never read as a plan
            marked = _tables.marked(rec.answer, blocks) if blocks else ""
            # latency plan step 2: no merge call; a record answer that still plans aloud gets it (review of 2026-10-29)
            if merge_cfg.by == "code" and mode == "lean" and not checks and not plans_aloud(marked or rec.answer):
                # the record catalog's objects (name -> ObjectSpec): the bullet checks of the step-2 fixes read them
                catalog = getattr(getattr(session, "tools", None), "catalog", None)
                m, merge_by = code_merge(text, r.answer, rec, status, availability=availability,
                                         objects=getattr(catalog, "objects", None) or {}), "code"
                inserted = code_merge_lines(m.text, rec.answer)   # also the counted line of an unmerged answer
            else:
                # latency plan step 3: the merge call gets the code rows as table lines and code puts them back
                extra = {"record_text": marked, "table_note": _tables.COMBINE_TABLES_NOTE} if marked else {}
                m, merge_by = combine(llm, text, r.answer, rec, status, field_check=[c["line"] for c in checks],
                                      availability=availability, knowledge_numbers=mode != "lean", **extra,
                                      **({"name_words": names} if names else {})), "model"
                if marked and m.merged:
                    back = _tables.restore(m.text, blocks)
                    m = (dataclasses.replace(m, text=back[0]) if back is not None else
                         dataclasses.replace(m, text=rec.answer, merged=False,
                                             error="the record answer's rows could not be put back"))
                    tables_merge = {"merged": 1, **back[1]} if back is not None else {"merged": 0, "error": 1}
                elif marked:   # the merge call got table lines but its text was not used (merge_error says why)
                    tables_merge = {"merged": 0}

    t_assemble = time.perf_counter()   # the answer shown: choose_answer, contact rule, footer, status and Read-as lines
    k_answer = (r.answer or "") if r is not None else ""
    if rec is not None and rec.no_records and rec.error and getattr(rec, "named_people", None) and k_answer.strip():
        # the people tool itself failed: the knowledge answer must not read as an answer about these people
        k_answer = ("Record data about " + ", ".join(f"'{w}'" for w in rec.named_people)
                    + " could not be read in this run.\n\n" + k_answer)
    # the record agent's reply was dropped (a count after a name was not found): code's not-found answer, never the raw
    # knowledge answer, which does not say that the name is not in the records
    not_found = (getattr(rec, "not_found_answer", "") or "") if rec is not None and rec.no_records else ""
    final, kind = choose_answer(m, rec, not_found, k_answer, records_only, status)
    clarify: dict = {}
    if kind == "none" and _clarify_due(rec):   # P2: a question back with 2-3 choices, never the failed-figures line
        from sf_assistant.agent import clarify as _clarify
        final, clarify = _clarify.build(search, d, rec, session, tuple(context_objects or ()), charts_on=charts_cfg.on)
    design_kept = False
    if chart_ask is not None and kind == "records" and m is not None and not m.merged and \
            (getattr(rec, "triage", None) or {}).get("overruled_by") == "chart":
        from sf_assistant.agent.nudges import answer_text
        if answer_text(k_answer):   # decision 4: a question the route calls design keeps its design answer
            final, design_kept = k_answer.rstrip() + "\n\n" + final, True
    if tables_cfg.by == "code" and rec is not None:   # the last resort: a user never sees a table line (body inherits)
        from sf_assistant.agent import tables as _tables
        final = _tables.strip_markers(final)
    if rec is not None and final:   # phase 1 (2026-10-26): contact details only when the question asks for them
        from sf_assistant.engine.contact import asks_for_contact, drop_contact_details, result_numbers
        if not asks_for_contact(text):
            evidence = list(getattr(rec, "evidence", None) or [])
            final = drop_contact_details(final, result_numbers(getattr(e, "model_result", None) for e in evidence
                                                               if getattr(e, "ok", False)))[0]
    chart_items, chart_lines, charts_error = [], [], ""
    if chart_ask is not None and not clarify:   # never raises; after the contact rule (no chart under a question)
        catalog = getattr(getattr(session, "tools", None), "catalog", None)
        before = final
        # a kept design answer reads as a full merge does: it keeps its sentences, and the record text is clean already
        final, chart_items, chart_lines, charts_error = _charts.make(chart_ask, text, final,
                                                                     "merged" if design_kept else kind, mode, rec,
                                                                     catalog, getattr(status, "today", None))
        charts_talk = charts_talk or final != before
        if final != before and merge_by == "code":   # a bullet went, so its label may be empty; the lines are read anew
            from sf_assistant.agent.merge import CODE_MERGE_LABEL, code_merge_lines
            final = _without_empty_label(final, CODE_MERGE_LABEL)
            inserted = code_merge_lines(final, rec.answer)
    body = final  # the answer without the code footer (scored by score_records.py)
    status_added: list[str] = []   # the status lines (Group B item 11) added after the lookup footer
    if kind in ("merged", "records") and rec is not None:  # after the audit; the footer's numbers are record facts
        from sf_assistant.agent.nudges import status_lines, with_lines, without_lines
        # what the answer already says is read without the bullets the merge by code added (review of 2026-10-29)
        final = with_footer(final, rec.evidence, inserted)
        status_added = status_lines(without_lines(final, inserted), rec.evidence, text,
                                    getattr(session, "status_of", None), getattr(session, "lookup_directions", None))
        final = with_lines(final, status_added)
    if d.file and kind in ("merged", "records") and final.strip():   # P3: no file is made; the figures are here
        chart_lines = chart_lines + [_display.line("file")]
    if chart_lines:   # not in body; status_added keeps lookup_footer right
        from sf_assistant.agent.nudges import with_lines
        final = with_lines(final, chart_lines) if final.strip() else "\n".join(chart_lines)
        status_added = status_added + chart_lines
    footed = None
    if spelling is not None:   # after the audits and the footer, in final only (body is what score_records scores)
        footed = final
        final = spelling_final(spelling, final, kind, rec, r, session, inserted,
                               **({"code_lines": chart_lines} if chart_lines else {}))
    if not final.strip():
        if records_only and rec is not None and rec.no_records and not rec.error:
            shown = "(no record data is needed for this question)"
        elif records_only:
            shown = "(no answer from the records)"
        else:
            shown = "(no answer)"
    else:
        shown = final
    rq = rec.data_queries if rec is not None else 0
    used = rq or (sum(1 for e in rec.evidence if e.ok) if rec is not None else 0)  # facts the answer rests on
    # SFA_TABLES=code: the record answer's counts, read against the final answer (review of 2026-10-30): "kept" the
    # blocks it holds whole, "left" any "{{" or "{table" it still holds (a table line no pattern reads)
    code_tables = dict(getattr(rec, "code_tables", None) or {})
    if code_tables:
        from sf_assistant.agent import tables as _tables
        code_tables["kept"] = sum(1 for b in (getattr(rec, "table_blocks", None) or {}).values()
                                  if _tables.holds(final, b))
        code_tables["left"] = _tables.left_over(final)
    if tables_merge is not None:
        code_tables["merge"] = tables_merge
    timeline.add("stage", t_assemble, stage="assemble")
    return Outcome(text=text, q=q, r=r, rec=rec, m=m, mode=mode, lean_error=lean_error, route_s=route_s,
                   shown_registry=shown_registry, status=status, checks=checks, k_answer=k_answer,
                   not_found=not_found, final=final, kind=kind, body=body, shown=shown, rq=rq, used=used,
                   started_q=started_q, records_only=records_only, spelling=spelling, footed=footed,
                   status_lines=status_added, timeline=timeline.end(), merge_by=merge_by,
                   merge_switch=merge_cfg.problem,
                   tables_by="code" if tables_cfg.by == "code" and rec is not None else None,
                   tables_switch=tables_cfg.problem, code_tables=code_tables, charts=chart_items,
                   charts_switch=charts_cfg.problem, charts_error=charts_error, charts_talk=charts_talk,
                   display=d, clarify=clarify)


def result_row(o: Outcome, session=None) -> dict:
    """The --file summary row and its JSONL record (``row["jsonl"]``) of one answered question.

    ``row["seconds"]`` is measured here, as ask.py measured it (after the answer was printed). The JSONL gets
    ``merge_by`` ("code" or "model", ``Outcome.merge_by``) when a merge function ran and ``merge_switch`` (the problem,
    ``Outcome.merge_switch``) when ``SFA_MERGE`` could not be read; each key is left out otherwise. With
    ``SFA_TABLES=code`` and records on it also gets ``tables_by`` ("code") and ``code_tables`` (the counts of the
    table lines and code rows, ``kept`` and ``left`` read in the final answer, with ``merge`` when the merge call got
    table lines), and ``tables_switch`` when ``SFA_TABLES`` could not be read; in model mode none of the three. In
    every mode ``record_nudge_kinds`` holds the prompt id of each draft-check message (``[]`` without records or a
    message; step-3 fix T5): the one key model mode gains. With ``SFA_CHARTS=on`` the row gets ``charts`` (the items
    with their picture, for the page) and the JSONL ``charts`` (the same items with no picture), ``charts_error`` (a
    class name) and ``charts_talk`` (true) when each has something to say; ``charts_switch`` holds the problem when
    ``SFA_CHARTS`` could not be read. Each key is left out otherwise. The prerequisites of 2026-11-25 add, each only
    when it has something to say: ``line_reason`` (and ``small_talk``, the kind) for a fixed line code wrote,
    ``display`` (the display flags, the removed display words and the (word, guard) pairs a guard kept), ``clarify``
    (the clarifying question's why, from, objects and choices), ``record_no_data`` and ``record_tool_retry`` (the
    forced round of the record step).
    """
    text, r, rec, m, kind, rq = o.text, o.r, o.rec, o.m, o.kind, o.rq
    final, body, k_answer, checks, mode = o.final, o.body, o.k_answer, o.checks, o.mode
    lean_error, route_s, started_q = o.lean_error, o.route_s, o.started_q
    # ---- summary row and JSONL record (--file)
    if session is None:
        records = "off"
    elif rec.no_records:
        records = "error" if rec.error else "not needed"
    elif rq:
        n_rows = sum(_rows_shown(e) for e in rec.evidence if e.ok and e.kind == "data")
        records = f"{rq} q, {n_rows} row{'s' if n_rows != 1 else ''}"
    else:
        records = "none"
    unsupported = list(rec.unsupported_numbers) if (rec is not None and kind == "records") else []
    audit = "ok" if kind == "merged" else (f"!{len(unsupported)}" if unsupported else "ok") if kind == "records" else "-"
    record_error = None
    if records == "error":
        failed = [e.error for e in rec.evidence if not e.ok and e.error]
        record_error = (failed[-1] if failed else rec.error) or "unknown error"
        record_error = " ".join(str(record_error).split())[:150]
    row = {"question": text, "seconds": time.perf_counter() - started_q, "answer": kind, "records": records,
           "rq": rq, "audit": audit, "ktools": len(r.tool_calls) if r is not None else 0,
           "rtools": len(rec.agent.tool_calls) if rec is not None and rec.agent is not None else 0,
           "error": r.error if r is not None else None, "record_error": record_error}
    row["jsonl"] = {
        "question": text, "answer": final, "answer_kind": kind, "answer_body": body,
        "record_answer": rec.answer if rec is not None else None, "knowledge_answer": k_answer if r is not None else None,
        "record_facts": _record_facts(rec) if rec is not None else [],
        "queries": _queries(rec) if rec is not None else [],
        "unsupported_numbers": unsupported, "rejected_numbers": list(m.rejected_numbers) if m is not None else [],
        "record_unsupported_numbers": list(rec.unsupported_numbers) if rec is not None else [],
        "record_dropped_numbers": list(getattr(rec, "dropped_numbers", None) or []) if rec is not None else [],
        "record_error": rec.error if rec is not None else None,
        "record_code_answer": bool(getattr(rec, "code_answer", False)) if rec is not None else False,
        "triage": dict(getattr(rec, "triage", None) or {}) if rec is not None else {},
        "lookup_checks": _lookup_checks(rec),
        "field_check": [c["line"] for c in checks],
        "lookup_footer": _without_status(o.footed if o.footed is not None else final, o) != body,
        "merge_error": m.error if m is not None else None,
        "record_nudges": [str(x)[:160] for x in getattr(rec, "nudges", None) or []] if rec is not None else [],
        "record_nudge_kinds": list(getattr(rec, "nudge_kinds", None) or []) if rec is not None else [],
        "record_dropped_answer": (getattr(rec, "dropped_answer", "") or None) if rec is not None else None,
        "knowledge_mode": mode, "knowledge_seconds": round(r.latency_s, 2) if r is not None else None,
        "lean_error": lean_error,
        "knowledge_model_calls": r.steps if r is not None else 0,
        "route_seconds": round(route_s, 2) if route_s is not None else None,
        "record_seconds": round(rec.seconds, 2) if rec is not None else None,
        "merge_seconds": round(m.seconds, 2) if m is not None else None,
        "seconds": round(row["seconds"], 2)}
    if o.merge_by is not None:   # the merge that ran (latency plan step 2, SFA_MERGE): code or model
        row["jsonl"]["merge_by"] = o.merge_by
    if o.merge_switch:   # an invalid SFA_MERGE, read as code: the problem only, never text from the question
        row["jsonl"]["merge_switch"] = o.merge_switch
    if o.tables_by is not None:   # the rows by code (latency plan step 3, SFA_TABLES=code); nothing in model mode
        row["jsonl"]["tables_by"] = o.tables_by
    if o.code_tables:   # the counts of the table lines and code rows: ints, fixed words, query ids, tool names
        row["jsonl"]["code_tables"] = dict(o.code_tables)
    if o.tables_switch:   # an invalid SFA_TABLES, read as the default: the problem only, never text from the question
        row["jsonl"]["tables_switch"] = o.tables_switch
    if o.charts:   # the page reads row["charts"]; the sidecar gets no picture
        from sf_assistant.agent import charts as _charts
        row["charts"] = o.charts
        row["jsonl"]["charts"] = _charts.for_jsonl(o.charts)
    if o.charts_switch:   # an invalid SFA_CHARTS, read as off: the problem only, never text from the question
        row["jsonl"]["charts_switch"] = o.charts_switch
    if o.charts_error:   # a class name, never the fault's text
        row["jsonl"]["charts_error"] = o.charts_error
    if o.charts_talk:
        row["jsonl"]["charts_talk"] = True
    if getattr(rec, "dropped_raw", ""):   # an empty record answer's raw text (2026-11-16): which cleaner emptied it
        row["jsonl"]["record_dropped_raw"] = rec.dropped_raw
    if o.status_lines:   # Group B item 11: the code's status lines (the key is left out when there is none)
        row["jsonl"]["status_lines"] = list(o.status_lines)
    if o.spelling is not None:   # the spelling help (SFA_RESOLVER=on): the question's misspelt names, the code line
        row["jsonl"]["spelling"] = o.spelling.rows()
        row["jsonl"]["read_as_line"] = o.footed is not None and final != o.footed
    if o.timeline:   # where the seconds went (agent.timeline): names, numbers and fixed words only, no text
        row["jsonl"]["timeline"] = o.timeline
    if o.line_reason:   # the prerequisites of 2026-11-25: a fixed line code wrote (small talk, display only, bare chart)
        row["jsonl"]["line_reason"] = o.line_reason
    if o.small_talk:
        row["jsonl"]["small_talk"] = o.small_talk
    # fixed vocabulary, and the user's own words a guard kept; only when a word was removed or refused (review fix R8:
    # a guard that only kept a word, "details of X", "reports to", adds no key to an ordinary row)
    if o.display is not None and (o.display.words or o.display.refused):
        row["jsonl"]["display"] = o.display.as_log()
    if o.clarify:
        row["jsonl"]["clarify"] = dict(o.clarify)
    if rec is not None and getattr(rec, "no_data", None):
        row["jsonl"]["record_no_data"] = rec.no_data
    if rec is not None and getattr(rec, "tool_retry", None):
        row["jsonl"]["record_tool_retry"] = dict(rec.tool_retry)
    return row


def _without_status(text: str, o: Outcome) -> str:
    """``text`` without the outcome's status lines: the JSONL ``lookup_footer`` flag says only whether the lookup
    footer was added."""
    if not o.status_lines:
        return text
    if o.line_reason and text == "\n".join(o.status_lines):   # one fixed line and nothing else: no record module
        return o.body
    from sf_assistant.agent.nudges import without_lines

    return without_lines(text, o.status_lines)


def run_question(text: str, cfg, registry, system, llm, n: int, session=None, records_only: bool = False,
                 lean_knowledge: bool = True, context_objects: Any = ()) -> dict:
    """:func:`answer` + :func:`result_row` without printing: the summary row with its JSONL record."""
    return result_row(answer(text, cfg, registry, system, llm, n, session, records_only=records_only,
                             lean_knowledge=lean_knowledge, context_objects=context_objects), session)
