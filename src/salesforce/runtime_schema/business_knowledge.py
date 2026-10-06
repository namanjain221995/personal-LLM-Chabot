"""Hand-reviewed business vocabulary -> manual aliases in the runtime schema.

The org's own metadata says what things are CALLED. It cannot say what the
team MEANS: that "candidate" is an Account, or that an interview's "offer
status" lives in Interview_Outcome__c even though Interview_Status__c lists
"Offer Received" too. That knowledge is written by hand in one file,
`brain/lexicon/curated.yaml`, which the discovery lexicon already reads. This
loads the same file into the runtime schema, so schema linking sees the same
vocabulary instead of a second, drifting copy.

Every entry is checked against the schema being built. A term that points at an
object or field that does not exist is dropped and reported -- a stale alias
that grounds a question to nothing is worse than no alias. Entries still marked
`needs_review: true` are not loaded, which is the file's own rule.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Any

from .models import Alias, SchemaBundle

log = logging.getLogger(__name__)

SOURCE = "business_knowledge"
_CONFIDENCE = {"high": 0.98, "medium": 0.9, "low": 0.8}


@dataclass
class LoadReport:
    loaded: int = 0
    skipped_review: list[str] = dataclass_field(default_factory=list)
    rejected: list[str] = dataclass_field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"loaded": self.loaded, "skipped_review": self.skipped_review,
                "rejected": self.rejected}


def _terms(path: Path) -> dict[str, dict[str, Any]]:
    try:
        import yaml
    except ImportError:
        log.warning("business knowledge needs PyYAML; none loaded")
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {str(term).strip().lower(): spec for term, spec in
            (raw.get("terms") or {}).items() if isinstance(spec, dict)}


def load_business_aliases(path: str | Path | None,
                          bundle: SchemaBundle) -> LoadReport:
    """Append verified manual aliases to `bundle`. Never raises."""
    report = LoadReport()
    if not path:
        return report
    path = Path(path)
    if not path.is_file():
        log.warning("business knowledge file not found at %s", path)
        return report

    objects = {o.api_name for o in bundle.objects}
    fields = {(f.object_api_name, f.api_name) for f in bundle.fields}

    try:
        terms = _terms(path)
    except Exception as exc:                            # noqa: BLE001
        log.warning("business knowledge unreadable (%s): %s", path, exc)
        return report

    for term, spec in terms.items():
        if spec.get("needs_review") is True:
            report.skipped_review.append(term)
            continue
        target = str(spec.get("target") or "")
        confidence = _CONFIDENCE.get(str(spec.get("confidence", "high")).lower(), 0.9)
        kind, _, ident = target.partition(":")
        if kind == "object" and ident in objects:
            bundle.object_aliases.append(Alias(
                object_api_name=ident, alias=term, source=SOURCE,
                confidence=confidence, is_manual=True))
            report.loaded += 1
        elif kind == "field" and "." in ident:
            obj, _, fld = ident.partition(".")
            if (obj, fld) in fields:
                bundle.field_aliases.append(Alias(
                    object_api_name=obj, field_api_name=fld, alias=term,
                    source=SOURCE, confidence=confidence, is_manual=True))
                report.loaded += 1
            else:
                report.rejected.append(f"{term} -> {target}")
        else:
            report.rejected.append(f"{term} -> {target}")

    if report.rejected:
        log.warning("business knowledge: %d term(s) point at nothing in this "
                    "schema: %s", len(report.rejected), report.rejected)
    return report


def load_business_notes(path: str | Path | None) -> dict[str, dict[str, Any]]:
    """Target component -> the human notes about it, for the semantic linker.

    Notes are what makes a term's meaning explicit ("offer status lives in
    Interview_Outcome__c"). They are evidence handed to the model, never a
    decision taken on the model's behalf.
    """
    if not path or not Path(path).is_file():
        return {}
    try:
        terms = _terms(Path(path))
    except Exception:                                   # noqa: BLE001
        return {}
    notes: dict[str, dict[str, Any]] = {}
    for term, spec in terms.items():
        if spec.get("needs_review") is True:
            continue
        target = str(spec.get("target") or "")
        entry = notes.setdefault(target, {"terms": [], "notes": []})
        entry["terms"].append(term)
        note = " ".join(str(spec.get("note") or "").split())
        if note and note not in entry["notes"]:
            entry["notes"].append(note[:400])
        if spec.get("filter"):
            entry.setdefault("implied_filter", str(spec["filter"]))
    return notes
