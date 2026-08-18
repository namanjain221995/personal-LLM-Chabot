"""The Salesforce brain: org knowledge as DATA, dropped in by the SF team.

`org_brief.py` proved the pattern — business rules, canonical metrics and
domain vocabulary injected per-question keep the model from writing plausible
but wrong queries. But org_brief is hand-authored Python: every new piece of
knowledge needs a code change. The Salesforce developers produce knowledge as
TEXT (see brain/sources/ in the repo — field dictionaries, formula semantics,
flow behaviour), and they will keep producing more of it.

This module closes that gap. A "pack" is one YAML/JSON file in BRAIN_DIR
describing one subject area, with the same knowledge shapes org_brief already
injects:

    name:        pack identity (defaults to the filename)
    triggers:    words/phrases that mean a question is about this subject
    tables:      warehouse tables to pin into the schema slice when triggered
    rules:       the trap-avoiding rules block, injected when triggered
    metrics:     canonical measure definitions (same shape as org_brief.METRICS)
    glossary:    term -> meaning, injected when the term appears
    field_notes: object -> field -> help text, merged into the sf dictionary
                 (enrich-only: a note for a field the org lacks is dropped)
    knowledge:   prose chunks retrieved for conceptual "how does X work"
                 questions that no SQL can answer

Packs are re-read automatically when a file changes (mtime scan per call —
the directory holds a handful of small files), so dropping a new pack in
needs no restart. Everything here is best-effort and never fatal: a missing
directory or a malformed pack means less knowledge, not a broken request.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..config import settings

log = logging.getLogger("brain")

#: Metric entries must carry these to be usable by org_brief.metric_hint.
_METRIC_REQUIRED = ("name", "table", "definition", "sql")

#: Per-block caps. The prompt already carries the schema slice, the dictionary
#: hint and the org brief; brain blocks must not crowd those out (prefill
#: latency on the 35B model is roughly linear in prompt size).
#: Per-pack rules budget. Raised from 4000 (2026-08-18) because the
#: internal-interview pack legitimately reached it and the overflow was being
#: cut mid-word — see `_capped_rules`. Still well under `_RULES_TOTAL_CAP`, so
#: one large pack cannot crowd every other one out of a multi-domain question.
_RULES_CAP = 5000
_KNOWLEDGE_CHUNK_CAP = 3200  # was 1400 (2026-08-18)
_GLOSSARY_CAP = 3200  # was 1200 (2026-08-18)

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]+")
#: Same list as sf_dictionary — words that match everything rank nothing.
_STOP = {
    "the", "and", "for", "with", "from", "how", "many", "what", "which", "show",
    "give", "list", "all", "our", "are", "is", "in", "of", "to", "me", "data",
    "record", "records", "salesforce", "object", "objects", "field", "fields",
    "count", "total", "last", "this", "that", "get", "please", "name", "names",
    "does", "work", "works", "mean", "means", "explain", "why", "when", "who",
}


def _stem(word: str) -> str:
    """Crude singularisation; same rule as sf_dictionary and org_brief."""
    if len(word) > 3 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _tokens(text: str) -> set:
    words = {w.lower() for w in _WORD_RE.findall(text or "")} - _STOP
    return {_stem(w) for w in words}


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

_PACK_SUFFIXES = (".yaml", ".yml", ".json")

#: (directory used, file fingerprint) -> parsed packs. One entry only; the
#: fingerprint check is what makes dropped-in packs live without a restart.
_cache: Optional[Tuple[str, Tuple, List[Dict[str, Any]]]] = None


def _candidate_dirs() -> List[Path]:
    """Where packs may live: the configured dir, then the repo checkout.

    The container mounts brain/packs at BRAIN_DIR; a developer checkout has
    no /data, so fall back to the packs directory relative to this file
    (repo/brain/packs) — which simply does not exist inside the image.
    """
    dirs = [Path(settings.brain_dir)]
    try:
        dirs.append(Path(__file__).resolve().parents[3] / "brain" / "packs")
    except IndexError:  # shallow filesystem layout; nothing to add
        pass
    return dirs


def _fingerprint(directory: Path) -> Tuple:
    files = []
    try:
        for entry in sorted(directory.iterdir()):
            if entry.suffix.lower() in _PACK_SUFFIXES and entry.is_file():
                stat = entry.stat()
                files.append((str(entry), stat.st_mtime_ns, stat.st_size))
    except OSError:
        return ()
    return tuple(files)


def _parse_pack(path: Path) -> Optional[Dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".json":
            data = json.loads(text)
        else:
            import yaml  # lazy: not every deployment ships packs

            data = yaml.safe_load(text)
    except Exception as exc:  # noqa: BLE001 — one bad pack must not kill the rest
        log.warning("brain pack %s unreadable: %s", path.name, str(exc)[:200])
        return None
    if not isinstance(data, dict):
        log.warning("brain pack %s is not a mapping; ignored", path.name)
        return None
    data.setdefault("name", path.stem)
    return _normalise(data, path.name)


def _capped_rules(rules: str, filename: str) -> str:
    """Trim a pack's rules to the per-pack budget WITHOUT cutting mid-sentence.

    This used to be `rules[:_RULES_CAP]`, which is precisely what the comment on
    `_RULES_TOTAL_CAP` says must never happen: "a truncated rule reads as a
    complete one, which is worse than a missing one". A pack that grew past the
    cap had its last rule sliced mid-word — the internal-interview pack's rule
    about which object an interviewer lives on ended at "a CANDIDATE or an
    INTERVIEW", and the model was left to finish the sentence itself.

    So the cut lands on a rule boundary (a line starting "- "), and it is
    logged: a pack quietly losing knowledge is how grounding rots.
    """
    if len(rules) <= _RULES_CAP:
        return rules
    kept: List[str] = []
    used = 0
    for line in rules.split("\n"):
        if line.startswith("- ") and used + len(line) > _RULES_CAP and kept:
            break
        kept.append(line)
        used += len(line) + 1
    dropped = len(rules.split("\n")) - len(kept)
    log.warning(
        "brain pack %s: rules are %d chars over the %d cap; dropped the last "
        "%d line(s) at a rule boundary. Shorten the pack rather than relying "
        "on this.",
        filename, len(rules) - _RULES_CAP, _RULES_CAP, dropped,
    )
    return "\n".join(kept).rstrip()


def _normalise(pack: Dict[str, Any], filename: str) -> Dict[str, Any]:
    """Coerce a pack into predictable shapes, dropping what cannot be used."""
    pack["triggers"] = [str(t).strip().lower() for t in pack.get("triggers") or [] if str(t).strip()]
    pack["tables"] = [str(t).strip() for t in pack.get("tables") or [] if str(t).strip()]
    pack["rules"] = _capped_rules(str(pack.get("rules") or "").strip(), filename)

    metrics = []
    for metric in pack.get("metrics") or []:
        if not isinstance(metric, dict):
            continue
        if any(not str(metric.get(k) or "").strip() for k in _METRIC_REQUIRED):
            log.warning(
                "brain pack %s: metric %r lacks one of %s; skipped",
                filename, metric.get("name", "?"), _METRIC_REQUIRED,
            )
            continue
        metric.setdefault("aliases", [])
        metrics.append(metric)
    pack["metrics"] = metrics

    glossary = pack.get("glossary") or {}
    pack["glossary"] = (
        {str(k).strip(): str(v).strip() for k, v in glossary.items() if str(v).strip()}
        if isinstance(glossary, dict) else {}
    )

    notes = pack.get("field_notes") or {}
    pack["field_notes"] = notes if isinstance(notes, dict) else {}

    chunks = []
    for chunk in pack.get("knowledge") or []:
        if isinstance(chunk, str):
            chunk = {"text": chunk}
        if not isinstance(chunk, dict) or not str(chunk.get("text") or "").strip():
            continue
        chunk["text"] = str(chunk["text"]).strip()
        chunk["title"] = str(chunk.get("title") or "").strip()
        chunk["keywords"] = [str(k).lower() for k in chunk.get("keywords") or []]
        chunks.append(chunk)
    pack["knowledge"] = chunks
    return pack


def load() -> List[Dict[str, Any]]:
    """All packs, cached until a pack file changes. Never raises."""
    global _cache
    if not settings.brain_enabled:
        return []
    directory = next((d for d in _candidate_dirs() if d.is_dir()), None)
    if directory is None:
        return []
    fingerprint = _fingerprint(directory)
    if _cache and _cache[0] == str(directory) and _cache[1] == fingerprint:
        return _cache[2]
    packs = []
    for entry, _mtime, _size in fingerprint:
        pack = _parse_pack(Path(entry))
        if pack is not None:
            packs.append(pack)
    _cache = (str(directory), fingerprint, packs)
    if packs:
        log.info(
            "brain: %d pack(s) loaded from %s: %s",
            len(packs), directory, ", ".join(p["name"] for p in packs),
        )
    return packs


def available() -> bool:
    return bool(load())


#: (pack-cache identity) -> the word set. Rebuilt only when a pack file
#: changes, because the identity is the same fingerprint `load()` caches on.
_vocab_cache: Optional[Tuple[Tuple, frozenset]] = None


def vocabulary() -> frozenset:
    """Every word the packs teach: triggers, glossary terms, table names.

    This is the spelling authority for `sf_intel/interpret.py`. A question is
    only grounded when its words reach the trigger matcher, and the matcher is
    exact — so "mock" pulls the internal-interview rules and "moks" pulls
    nothing at all. Handing the same list to a bounded edit-distance repair is
    what turns the second one back into the first, without anybody maintaining
    a table of misspellings.

    Multi-word triggers contribute their parts as well as the whole phrase:
    "question bank" should let both halves be repaired.
    """
    global _vocab_cache
    packs = load()
    # `load()` leaves `_cache` untouched when packs are switched off, so the
    # flag rides along in the identity — otherwise disabling and re-enabling
    # the brain would serve the empty vocabulary from the enabled fingerprint.
    identity = (bool(packs), _cache[1] if _cache else ())
    if _vocab_cache and _vocab_cache[0] == identity:
        return _vocab_cache[1]
    words: set = set()

    def add(raw: str) -> None:
        for word in _WORD_RE.findall(str(raw or "").lower()):
            if len(word) >= 3:
                words.add(word)

    for pack in packs:
        for trigger in pack["triggers"]:
            add(trigger)
        for term in pack["glossary"]:
            add(term)
        for table in pack["tables"]:
            # Interview_Type__c -> {interview, type}. The API spelling itself
            # is never a repair target; users do not type it.
            add(table.replace("__c", " ").replace("_", " "))
        for metric in pack["metrics"]:
            add(metric.get("name", ""))
            for alias in metric.get("aliases") or []:
                add(alias)
    _vocab_cache = (identity, frozenset(words))
    return _vocab_cache[1]


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def _trigger_hits(question: str, pack: Dict[str, Any]) -> int:
    """How many DISTINCT triggers of this pack the question contains."""
    text = " " + (question or "").lower() + " "
    tokens = _tokens(question)
    hits = 0
    for trigger in pack["triggers"]:
        if " " in trigger:
            if re.search(r"\b" + re.escape(trigger) + r"\b", text):
                hits += 1
        elif _stem(trigger) in tokens:
            hits += 1
    return hits


def _triggered(question: str, pack: Dict[str, Any]) -> bool:
    return _trigger_hits(question, pack) > 0


def matched_packs(question: str) -> List[Dict[str, Any]]:
    return [p for p in load() if _triggered(question, p)]


# ---------------------------------------------------------------------------
# What the prompts consume
# ---------------------------------------------------------------------------

#: Total budget for pack RULES in one prompt. With a dozen-plus packs loaded, a
#: multi-domain question ("unpaid invoices for dropped candidates with failed
#: mocks") matched enough packs to stack 22KB of grounding. Whole blocks only,
#: strongest trigger matches first — a truncated rule reads as a complete one,
#: which is worse than a missing one.
_RULES_TOTAL_CAP = 24000  # was 9000 (2026-08-18)
#: A multi-domain question ("unpaid invoices for dropped candidates with failed
#: mocks") matches several packs, and at 9000 the later ones were dropped
#: whole — the question that needed the most knowledge got the least.


def rules_for(question: str) -> str:
    """The rule blocks this question earns, strongest matches first, capped."""
    scored = [
        (hits, pack)
        for pack in load()
        if pack["rules"] and (hits := _trigger_hits(question, pack)) > 0
    ]
    scored.sort(key=lambda pair: (-pair[0], pair[1]["name"]))
    blocks: List[str] = []
    budget = _RULES_TOTAL_CAP
    for _hits, pack in scored:
        if len(pack["rules"]) > budget:
            continue
        budget -= len(pack["rules"])
        blocks.append(pack["rules"])
    return "\n\n".join(blocks)


def extra_metrics() -> List[Dict[str, Any]]:
    """Pack metrics, for org_brief.match_metrics to score alongside its own."""
    metrics: List[Dict[str, Any]] = []
    for pack in load():
        metrics.extend(pack["metrics"])
    return metrics


def tables_for(question: str) -> List[str]:
    """Tables the triggered packs want pinned into the schema slice."""
    seen: List[str] = []
    for pack in matched_packs(question):
        for table in pack["tables"]:
            if table not in seen:
                seen.append(table)
    return seen


def glossary_for(question: str) -> str:
    """Definitions for pack glossary terms the question actually uses."""
    text = " " + (question or "").lower() + " "
    tokens = _tokens(question)
    lines: List[str] = []
    seen = set()
    for pack in load():
        for term, meaning in pack["glossary"].items():
            key = term.lower()
            if key in seen:
                continue
            hit = (
                re.search(r"\b" + re.escape(key) + r"\b", text)
                if " " in key or "-" in key
                else _stem(key) in tokens
            )
            if hit:
                seen.add(key)
                lines.append(f"- {term}: {meaning}")
    block = "\n".join(lines)
    if not block:
        return ""
    return "What this org's own terms mean:\n" + block[:_GLOSSARY_CAP]


def knowledge_for(question: str, limit: int = 3) -> str:
    """The prose chunks most relevant to this question, or "".

    Lexical scoring, same as every other retrieval in this codebase: cheap,
    offline, and predictable. Title and keyword hits outweigh body hits so a
    chunk ABOUT the asked thing beats a chunk that merely mentions it.
    """
    tokens = _tokens(question)
    if not tokens:
        return ""
    scored = []
    for pack in load():
        for chunk in pack["knowledge"]:
            score = 3 * len(tokens & (_tokens(chunk["title"]) | {_stem(k) for k in chunk["keywords"]}))
            score += len(tokens & _tokens(chunk["text"]))
            if score >= 2:
                scored.append((score, chunk))
    if not scored:
        return ""
    scored.sort(key=lambda pair: (-pair[0], pair[1]["title"]))
    parts = []
    budget = settings.brain_max_chars
    for _score, chunk in scored[:limit]:
        text = chunk["text"][:_KNOWLEDGE_CHUNK_CAP]
        block = f"### {chunk['title']}\n{text}" if chunk["title"] else text
        if len(block) > budget:
            break
        budget -= len(block)
        parts.append(block)
    if not parts:
        return ""
    return (
        "Internal documentation about how this org's processes actually work "
        "(authoritative — written by the Salesforce team):\n" + "\n\n".join(parts)
    )


def grounding_extras(question: str) -> str:
    """Glossary + retrieved knowledge, appended to org_brief.grounding_for.

    The rules/metrics/tables shapes are folded into org_brief's own layers
    (domain_rules_for, match_metrics, tables_for); these two are the shapes
    org_brief has no equivalent for.
    """
    parts = [p for p in (glossary_for(question), knowledge_for(question)) if p]
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Dictionary overlay
# ---------------------------------------------------------------------------

def field_overlay() -> Dict[str, Any]:
    """field_notes reshaped for sf_dictionary.merge (enrich-only).

    Only `help` is contributed: the base dictionary was built from the org the
    platform actually queries, so its names, types and picklists win. A note
    for a field the org lacks is silently dropped by the merge — knowledge
    files often come from sandboxes that run ahead of production.
    """
    objects: Dict[str, Any] = {}
    for pack in load():
        for obj_api, fields in pack["field_notes"].items():
            if not isinstance(fields, dict):
                continue
            entry = objects.setdefault(
                str(obj_api), {"api": str(obj_api), "label": str(obj_api), "fields": []}
            )
            for field_api, help_text in fields.items():
                if str(help_text or "").strip():
                    entry["fields"].append(
                        {
                            "api": str(field_api),
                            "label": str(field_api),
                            "help": " ".join(str(help_text).split()),
                        }
                    )
    return {"objects": objects} if objects else {}
