"""Route -> the stages that route runs.

A second table beside `graphrag.routing`, not a second copy of it. That one
answers "which route do these three booleans mean?"; this one answers "what
does that route actually do?". Keeping them apart is what lets the route stay a
statement about the question while this stays a statement about the system.

Deterministic and total, for the same reason routing is: eight routes, eight
entries, no inference. A route with no entry would be a question the pipeline
silently did nothing about.
"""
from __future__ import annotations

from enum import Enum


class Stage(str, Enum):
    """The four things a question can cause to happen."""

    DISCOVERY = "DISCOVERY"              # business words -> candidate components
    SCHEMA_LINKING = "SCHEMA_LINKING"    # candidates -> verified identifiers
    RECORD_QUERY = "RECORD_QUERY"        # identifiers -> rows from DuckDB
    METADATA = "METADATA"                # describe what the org configured
    ANSWER = "ANSWER"                    # verified facts -> the main model


# Route value -> the stages it runs, in order.
#
# Every data route includes SCHEMA_LINKING, DATA_DIRECT included. "Direct"
# means no discovery pass, not no grounding: the record layer refuses any plan
# not marked schema_grounded, and linking is the only thing that marks one. A
# DATA_DIRECT that skipped linking would have nothing to execute.
#
# Every data route ends in ANSWER, and ANSWER is always last. A route that
# returned rows without it would be a second answer path, and a system with two
# answer paths has two voices while only one of them is ever evaluated.
# Metadata-only routes have no query result to interpret, so they have no
# ANSWER stage -- describing a component is not answering a question about
# records.
_STAGES: dict[str, tuple[Stage, ...]] = {
    "NONE": (),
    "SCHEMA_ONLY": (Stage.DISCOVERY,),
    "DATA_DIRECT": (Stage.SCHEMA_LINKING, Stage.RECORD_QUERY, Stage.ANSWER),
    "DATA_WITH_DISCOVERY": (Stage.DISCOVERY, Stage.SCHEMA_LINKING,
                            Stage.RECORD_QUERY, Stage.ANSWER),
    "METADATA_ONLY": (Stage.METADATA,),
    "METADATA_WITH_DISCOVERY": (Stage.DISCOVERY, Stage.METADATA),
    "MIXED_DIRECT": (Stage.SCHEMA_LINKING, Stage.RECORD_QUERY, Stage.METADATA,
                     Stage.ANSWER),
    "MIXED_WITH_DISCOVERY": (Stage.DISCOVERY, Stage.SCHEMA_LINKING,
                             Stage.RECORD_QUERY, Stage.METADATA, Stage.ANSWER),
}


class UnknownRoute(KeyError):
    pass


def stages_for(route: object) -> tuple[Stage, ...]:
    """The stages this route runs.

    Accepts a `graphrag.routing.Route` or its plain name. Route is a `str`
    enum, so one lookup serves both and this package needs no import from the
    knowledge bundle -- which matters because the two live in different source
    trees and are mounted into different containers.
    """
    key = getattr(route, "value", route)
    if not isinstance(key, str):
        raise UnknownRoute(f"{route!r} is not a route")
    try:
        return _STAGES[key]
    except KeyError as exc:
        raise UnknownRoute(f"no stage plan for route {key!r}") from exc


def routes() -> tuple[str, ...]:
    return tuple(_STAGES)


def runs(route: object, stage: Stage) -> bool:
    return stage in stages_for(route)
