"""What kind of result came back. Decided by code, never by the model.

Classification drives which facts get extracted and which wording the
validator will police. A model that classified its own result could report a
truncated list as a complete one and nothing downstream would notice, because
every later check would be looking for the wrong thing.
"""
from __future__ import annotations

from typing import Any

from .models import ResultType


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _operation(query_plan: Any) -> str:
    operation = _get(query_plan, "operation")
    return str(_get(operation, "value", operation) or "").lower()


def classify_result(query_result: Any, grounded_plan: Any = None,
                    query_plan: Any = None) -> ResultType:
    """The one result type this result is.

    Order matters. An aggregate that returned no rows is still an aggregate,
    and a COUNT of zero is still a COUNT -- classifying either as EMPTY_RESULT
    would throw away the number the question actually asked for.
    """
    if not _get(query_result, "success", False):
        return ResultType.ERROR_RESULT

    operation = _operation(query_plan)
    group_by = list(_get(query_plan, "group_by", []) or [])
    select = list(_get(query_plan, "select", []) or [])
    has_aggregate = any(_get(item, "aggregate") for item in select)

    if operation == "count" and not group_by:
        return ResultType.COUNT
    if group_by:
        if operation == "compare" or _is_comparison(grounded_plan):
            return ResultType.COMPARISON
        return ResultType.GROUPED_AGGREGATE
    if has_aggregate:
        return ResultType.SINGLE_AGGREGATE

    if int(_get(query_result, "row_count", 0) or 0) == 0:
        return ResultType.EMPTY_RESULT
    # Truncation outranks the row count: "two of many" is a different answer
    # from "two", and only this type makes the validator enforce the
    # difference.
    if _get(query_result, "truncated", False):
        return ResultType.TRUNCATED_RESULT
    if int(_get(query_result, "row_count", 0) or 0) == 1:
        return ResultType.SINGLE_RECORD
    return ResultType.MULTI_RECORD


def _is_comparison(grounded_plan: Any) -> bool:
    if grounded_plan is None:
        return False
    intent = str(_get(grounded_plan, "intent", "") or "").lower()
    return "compar" in intent
