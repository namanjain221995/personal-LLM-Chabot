"""Score schema discovery, which is ranked retrieval rather than one answer.

The existing evaluators ask "is the object correct?" and record pass or fail.
That question misreads what discovery produces. Discovery returns a ranked
shortlist, and a query that puts the right object second has not failed in the
way a query that never retrieved it has. Collapsing both to `failed` throws
away the distinction that tells you whether to fix retrieval or fix ranking.

So these checks report recall@k and the rank that achieved it, and they treat a
clarifying question as a legitimate outcome. The placement regression in this
dataset exists precisely because the old system did NOT ask when "placed" was
ambiguous; a runner that scores asking as a failure would push the system back
toward the behaviour the dataset was written to catch.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Sequence

from .common import CheckResult, failed, normalized_set, not_evaluable, passed

# Retrieval is a shortlist handed to a planner, not a final answer. Five is
# what fits in a prompt without crowding it.
DEFAULT_K = 5


def _rank_of(required: Iterable[str], ranked: Sequence[str]) -> Dict[str, int | None]:
    """1-based rank of each required name, or None when it never appears."""
    lookup = {normalized_name: index + 1
              for index, normalized_name in enumerate(
                  normalized_name for normalized_name in
                  (_norm(item) for item in ranked))}
    return {item: lookup.get(_norm(item)) for item in required}


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _recall_check(stage: str, required: set[str], forbidden: set[str],
                  ranked: List[str], k: int) -> CheckResult:
    if not required and not forbidden:
        return not_evaluable(stage, {"required": [], "forbidden": []},
                             "case declares no required or forbidden names")
    # A case asking for six fields cannot be judged on a top-five list: at
    # k=5 it is marked failed however good retrieval is. The bar is "did the
    # top N contain the N things needed", so k floors at the number required.
    k = max(k, len(required))
    top_k = ranked[:k]
    ranks = _rank_of(required, ranked)
    missing = sorted(name for name, rank in ranks.items()
                     if rank is None or rank > k)
    present_forbidden = sorted(normalized_set(forbidden) & normalized_set(top_k))
    actual = {
        "top_k": top_k,
        "k": k,
        "ranks": {name: rank for name, rank in sorted(ranks.items())},
        "recall_at_k": round(
            (len(required) - len(missing)) / len(required), 3) if required else None,
        "k_effective": k,
    }
    expected = {"required": sorted(required), "forbidden": sorted(forbidden)}
    if missing or present_forbidden:
        reasons = []
        if missing:
            # Separating "never retrieved" from "retrieved but ranked too low"
            # is the point: they are different bugs with different fixes.
            absent = [n for n in missing if ranks[n] is None]
            low = [f"{n}@{ranks[n]}" for n in missing if ranks[n] is not None]
            if absent:
                reasons.append(f"not retrieved at all: {', '.join(absent)}")
            if low:
                reasons.append(f"retrieved below k={k}: {', '.join(low)}")
        if present_forbidden:
            reasons.append(f"forbidden present: {', '.join(present_forbidden)}")
        return failed(stage, expected, actual, "; ".join(reasons))
    return passed(stage, expected, actual)


def objects(expected: Dict[str, Any], discovery: Dict[str, Any],
            k: int = DEFAULT_K) -> CheckResult:
    block = (expected.get("entities") or {}).get("objects") or {}
    ranked = [item.get("api_name") or item.get("component_id", "")
              for item in discovery.get("objects") or []]
    return _recall_check("discovery_objects",
                         set(block.get("required") or []),
                         set(block.get("forbidden") or []),
                         ranked, k)


def fields(expected: Dict[str, Any], discovery: Dict[str, Any],
           k: int = DEFAULT_K) -> CheckResult:
    block = (expected.get("entities") or {}).get("fields") or {}
    required = set(block.get("required") or [])
    ranked: List[str] = []
    for item in discovery.get("candidate_fields") or []:
        qualified = str(item.get("component_id", "")).split(":", 1)[-1]
        ranked.append(qualified)
        # Cases name fields both bare (`IsPersonAccount`) and qualified
        # (`Account.IsPersonAccount`), so both spellings have to match.
        if "." in qualified:
            ranked.append(qualified.split(".", 1)[1])
    return _recall_check("discovery_fields", required,
                         set(block.get("forbidden") or []), ranked, k)


def clarification(expected: Dict[str, Any], discovery: Dict[str, Any]) -> CheckResult:
    """Did it ask, and was asking the right call?

    A case may declare `expected_clarification` — a list of surfaces a correct
    system SHOULD query. Where the dataset says nothing, asking is reported but
    never counted against the run: silence is not evidence that a question was
    wrong.
    """
    asked = sorted(r.get("surface", "") for r in discovery.get("questions") or [])
    wanted = expected.get("expected_clarification")
    if wanted is None:
        return not_evaluable(
            "discovery_clarification", {"expected_clarification": None},
            f"case declares no expectation; system asked about {asked or 'nothing'}")
    wanted_set = normalized_set(wanted)
    asked_set = normalized_set(asked)
    actual = {"asked": asked, "missing": sorted(wanted_set - asked_set),
              "unexpected": sorted(asked_set - wanted_set)}
    if wanted_set - asked_set:
        return failed("discovery_clarification", sorted(wanted_set), actual,
                      "did not ask about an ambiguous term the case requires")
    return passed("discovery_clarification", sorted(wanted_set), actual)


def evaluate_discovery(case: Dict[str, Any], discovery: Dict[str, Any],
                       k: int = DEFAULT_K) -> List[CheckResult]:
    expected = case.get("expected") or {}
    return [objects(expected, discovery, k),
            fields(expected, discovery, k),
            clarification(expected, discovery)]


def summarise(results: List[Dict[str, Any]], k: int = DEFAULT_K) -> Dict[str, Any]:
    """Aggregate metrics: recall@k and MRR, not just a pass count."""
    def collect(stage: str) -> Dict[str, Any]:
        rows = [c for r in results for c in r["checks"] if c["stage"] == stage]
        scored = [c for c in rows if c["status"] in ("passed", "failed")]
        recalls = [c["actual"]["recall_at_k"] for c in scored
                   if isinstance(c.get("actual"), dict)
                   and c["actual"].get("recall_at_k") is not None]
        reciprocal: List[float] = []
        for check in scored:
            ranks = [r for r in (check.get("actual") or {}).get("ranks", {}).values()
                     if r is not None]
            reciprocal.append(1.0 / min(ranks) if ranks else 0.0)
        return {
            "evaluated": len(scored),
            "not_evaluable": len(rows) - len(scored),
            "passed": sum(1 for c in scored if c["status"] == "passed"),
            f"mean_recall_at_{k}": round(sum(recalls) / len(recalls), 3) if recalls else None,
            "mrr": round(sum(reciprocal) / len(reciprocal), 3) if reciprocal else None,
        }

    return {
        "k": k,
        "cases": len(results),
        "objects": collect("discovery_objects"),
        "fields": collect("discovery_fields"),
        "clarification": collect("discovery_clarification"),
    }
