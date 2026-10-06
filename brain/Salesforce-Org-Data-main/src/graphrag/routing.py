"""Turn three booleans from the extraction into exactly one route.

Deterministic and total: eight inputs, eight outputs, no model, no lookups, no
inference. The booleans are read, never adjusted -- if the extraction says a
question needs no record query, that is what routes, and correcting it belongs
upstream where the mistake was made, not here where it would be invisible.

One function, one enum, one place. A second copy of this mapping anywhere else
is how the two drift apart and a question quietly takes a path the trace says
it did not.
"""
from __future__ import annotations

from enum import Enum


class Route(str, Enum):
    """The eight paths a question can take.

    `str` mixin so a route serialises as its own name in JSON and in a trace
    without a caller having to remember to convert it.
    """

    NONE = "NONE"
    SCHEMA_ONLY = "SCHEMA_ONLY"
    DATA_DIRECT = "DATA_DIRECT"
    DATA_WITH_DISCOVERY = "DATA_WITH_DISCOVERY"
    METADATA_ONLY = "METADATA_ONLY"
    METADATA_WITH_DISCOVERY = "METADATA_WITH_DISCOVERY"
    MIXED_DIRECT = "MIXED_DIRECT"
    MIXED_WITH_DISCOVERY = "MIXED_WITH_DISCOVERY"


# (schema_discovery, record_query, metadata_context) -> Route
_ROUTES: dict[tuple[bool, bool, bool], Route] = {
    (False, False, False): Route.NONE,
    (True,  False, False): Route.SCHEMA_ONLY,
    (False, True,  False): Route.DATA_DIRECT,
    (True,  True,  False): Route.DATA_WITH_DISCOVERY,
    (False, False, True):  Route.METADATA_ONLY,
    (True,  False, True):  Route.METADATA_WITH_DISCOVERY,
    (False, True,  True):  Route.MIXED_DIRECT,
    (True,  True,  True):  Route.MIXED_WITH_DISCOVERY,
}


def derive_route(requires_schema_discovery: bool,
                 requires_record_query: bool,
                 requires_metadata_context: bool) -> Route:
    """The one route these three booleans imply.

    A missing boolean is read as False. The extraction may omit a key, and a
    None that reached the table as itself would miss every entry and raise on
    a question the pipeline could still have answered.
    """
    key = (bool(requires_schema_discovery),
           bool(requires_record_query),
           bool(requires_metadata_context))
    return _ROUTES[key]


def route_for(extraction: object) -> Route:
    """`derive_route` reading the flags off an Extraction."""
    return derive_route(
        getattr(extraction, "requires_schema_discovery", False),
        getattr(extraction, "requires_record_query", False),
        getattr(extraction, "requires_metadata_context", False),
    )
