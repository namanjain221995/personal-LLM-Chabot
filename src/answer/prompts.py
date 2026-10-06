"""The main model's instructions, in one place.

One builder, not prompt fragments scattered through the application. A rule
that exists in two prompts drifts into two rules, and the grounding validator
enforces only one of them.
"""
from __future__ import annotations

import json
from typing import Any

from .models import GroundingReport, InterpretedResult, ResultType

SYSTEM_PROMPT = """You are generating the final response from verified database results.

Use only facts contained in the supplied grounded result context.

Do not invent records, fields, dates, counts, reasons, causal explanations, statuses, or conclusions.

Do not assume facts that are not explicitly present.

Do not reinterpret database results beyond the supplied deterministic facts.

Do not perform arithmetic. Every sum, difference, percentage, ratio, average, minimum and maximum you are allowed to state is already in the context. If a number is not in the context, do not state it.

If information required to fully answer the user's question is missing, state that the returned data does not contain that information.

Use the user's business terminology rather than internal Salesforce or database identifiers, unless the user explicitly requests technical details.

Preserve all numeric values exactly.

Respect truncation metadata. `returned_records` is how many rows came back; `matching_records` is how many exist. If `matching_records` is absent, you do not know the total, so say the result is a partial view rather than stating a total.

The database is a synchronised local copy of Salesforce, not live Salesforce. Never say Salesforce "currently" shows something. If `freshness.include_in_answer` is false, do not mention synchronisation or data age at all.

Return concise, clear, business-friendly language.

First interpret the result, then answer. Reply with JSON only, in exactly this shape:

{
  "interpretation": {
    "answer_type": "record_list | count | aggregate | comparison | ranking | percentage | trend | exists | schema | operational | history | empty | unsupported",
    "primary_facts": ["the context keys, groups or records that directly answer the question"],
    "important_context": ["other context worth stating: totals, truncation, periods, filters"],
    "relation": "how several results combine (sides of a comparison, part and whole), or null"
  },
  "answer_type": "record_list | count | aggregate | comparison | empty | summary",
  "summary": "one sentence answering the question",
  "details": [{"text": "one line per record or group"}],
  "notes": ["only when something material must be qualified"],
  "freshness_note": "a sentence about data age, or null"
}

Write no prose outside the JSON."""


_TYPE_GUIDANCE: dict[ResultType, str] = {
    ResultType.EMPTY_RESULT:
        "No records matched. State that plainly. Do not suggest or speculate "
        "about why nothing matched -- the result establishes only that the "
        "count is zero, never a reason.",
    ResultType.SINGLE_RECORD:
        "One record matched. Answer with its fields, nothing more.",
    ResultType.MULTI_RECORD:
        "Several records matched. Give one detail line per record.",
    ResultType.COUNT:
        "The answer is a single verified count. State that number exactly "
        "and do not list records -- none were returned.",
    ResultType.SINGLE_AGGREGATE:
        "The answer is one aggregate value, already computed. State it "
        "exactly.",
    ResultType.GROUPED_AGGREGATE:
        "The answer is a set of groups with values, already computed. Give "
        "one detail line per group and do not combine or re-total them.",
    ResultType.DUPLICATE_GROUPS:
        "Every returned group is a verified duplicate key and its occurrence "
        "count. Give one detail line per group; do not call missing values "
        "duplicates or infer why duplication happened.",
    ResultType.COMPARISON:
        "The comparison figures in derived_facts were computed already. You "
        "may state them. Do not compute any others.",
    ResultType.PERCENTAGE:
        "The percentage, its numerator and its denominator were computed by "
        "code. State the percentage and, if useful, the two counts behind it. "
        "Do not recompute anything.",
    ResultType.RANKING:
        "The groups are already ranked, first to last. Keep that order and "
        "state each value exactly.",
    ResultType.TREND:
        "Each group is one time period, in order. Describe the values period "
        "by period; only mention an increase or decrease that derived_facts "
        "states.",
    ResultType.EXISTS:
        "The answer is yes or no, stated in verified_facts.exists, with the "
        "matching count. Say it plainly.",
    ResultType.SCHEMA_FACTS:
        "These are facts about the org's STRUCTURE (objects, fields, types, "
        "picklist values), not records. Report them as given. API names are "
        "appropriate here: the user asked about the schema.",
    ResultType.OPERATIONAL_FACTS:
        "These are facts about data synchronisation and system state. Report "
        "them as given, with their timestamps. When a fact says something is "
        "not recorded or unknown, say exactly that -- never claim it did or "
        "did not happen.",
    ResultType.HISTORY_TIMELINE:
        "These are recorded field changes, newest first: which field, old and "
        "new value, when. Report only what is listed.",
    ResultType.UNSUPPORTED:
        "The question needs a capability or data source this system does not "
        "have yet, named in verified_facts. Say so plainly and do not attempt "
        "an answer.",
    ResultType.TRUNCATED_RESULT:
        "The database returned more rows than were kept. Say how many are "
        "shown. State a total only if `matching_records` is present; "
        "otherwise say further records may exist.",
}


def build_answer_prompt(interpreted: InterpretedResult,
                        context: dict[str, Any]) -> list[dict[str, str]]:
    """The two messages the main model receives.

    The question sits inside the context as a field, labelled as the question.
    It is presentation intent, not evidence: what the user asked does not make
    anything true, and a question that assumes a fact must not be able to
    smuggle it into the answer.
    """
    guidance = _TYPE_GUIDANCE.get(interpreted.result_type, "")
    user = (f"{guidance}\n\n"
            f"GROUNDED RESULT CONTEXT (the only evidence you may use):\n"
            f"{json.dumps(context, indent=2, sort_keys=True, default=str)}")
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user}]


def build_regeneration_prompt(interpreted: InterpretedResult,
                              context: dict[str, Any],
                              previous: Any,
                              report: GroundingReport) -> list[dict[str, str]]:
    """The second and final attempt, told exactly what was wrong.

    Naming the offending values rather than repeating "be accurate" is the
    difference between a correction and another guess.
    """
    faults = "\n".join(
        f"- {violation.code.value}: {violation.detail}"
        for violation in report.violations)
    messages = build_answer_prompt(interpreted, context)
    messages.append({"role": "assistant",
                     "content": json.dumps(previous.as_dict(), default=str)})
    messages.append({"role": "user", "content": (
        "That answer was rejected by grounding validation. Each problem below "
        "is a claim the context does not support:\n\n"
        f"{faults}\n\n"
        "Write the answer again. Remove every unsupported claim. Do not "
        "replace it with a different guess -- say less instead. Use only "
        "values that appear in the context above, copied exactly. Reply with "
        "the same JSON shape and nothing else.")})
    return messages
