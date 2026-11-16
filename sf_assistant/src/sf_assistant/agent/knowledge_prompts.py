"""Prompt templates for question generation, paraphrasing, the answer agent and the judge.

Every template is a module-level ``str`` constant using ``{named}`` placeholders. The texts live in
``prompts/<id>.md`` (id = the constant name in lower case; :mod:`sf_assistant.prompts` loads each file once) and
are bound here at import, so the names and values are the same as before. ``CATEGORY_DEFINITIONS`` is parsed from
``prompts/question_category_definitions.md``, and ``QUESTION_GEN_SYSTEM`` is ``prompts/question_gen_system.md``
with ``<<CATEGORY_BLOCK>>`` and ``<<TOOL_BLOCK>>`` filled from it and :data:`TOOL_NAMES`. Fill the templates with
:func:`render`, which:

* replaces only ``{identifier}`` placeholders, so literal JSON braces in the templates (examples
  such as ``{"questions": [...]}``) are left untouched (format-safe, no brace doubling needed);
* fails loudly with ``KeyError`` when a placeholder has no value;
* converts non-string values: ``bool`` -> ``true``/``false``, ``list``/``dict`` -> pretty JSON;
* derives ``category_definition`` from ``category`` when it is not passed explicitly.

Templates may be edited in the notebook by assigning a new string to ``prompts.TEMPLATES[name]``;
:func:`render` always reads from ``TEMPLATES`` (the module constants keep the defaults).

Placeholders per template (see :data:`TEMPLATE_FIELDS`):

* ``QUESTION_GEN_SYSTEM``   - none
* ``QUESTION_GEN_USER``     - ``n``, ``category``, ``category_definition`` (auto), ``object_api_name``,
  ``excerpts``
* ``PARAPHRASE_SYSTEM``     - none
* ``PARAPHRASE_USER``       - ``question``, ``n``
* ``AGENT_SYSTEM``          - none (object-agnostic; the tools reveal the object)
* ``JUDGE_SYSTEM``          - none
* ``JUDGE_USER``            - ``question``, ``category``, ``answerable``, ``expected_facts``,
  ``gold_chunks``, ``tool_calls``, ``answer``
"""

from __future__ import annotations

import json
import re
from typing import Any

from sf_assistant import prompts as _prompt_files   # the texts: prompts/<id>.md

__all__ = [
    "CATEGORY_DEFINITIONS",
    "QUESTION_GEN_SYSTEM",
    "QUESTION_GEN_USER",
    "PARAPHRASE_SYSTEM",
    "PARAPHRASE_USER",
    "PARAPHRASE_CHECK_USER",
    "PREMISE_CHECK_USER",
    "AGENT_SYSTEM",
    "JUDGE_SYSTEM",
    "JUDGE_USER",
    "TEMPLATES",
    "TEMPLATE_FIELDS",
    "placeholders",
    "render",
]

# --------------------------------------------------------------------------------------------
# Question categories. Keys match okeval.questions.CATEGORIES exactly (same order).
# --------------------------------------------------------------------------------------------
def _parse_categories(block: str) -> dict[str, str]:
    """``{category: definition}`` from the ``- <category>: <definition>`` lines of
    ``prompts/question_category_definitions.md`` (in file order)."""
    out: dict[str, str] = {}
    for line in block.split("\n"):
        name, sep, definition = line[2:].partition(": ")
        if not line.startswith("- ") or not sep or not name or name in out:
            raise _prompt_files.PromptError(
                f"prompts/question_category_definitions.md: bad line {line[:60]!r} (want '- <category>: <definition>')")
        out[name] = definition
    return out


CATEGORY_DEFINITIONS: dict[str, str] = _parse_categories(_prompt_files.text("question_category_definitions"))

_CATEGORY_BLOCK = "\n".join(f"- {name}: {text}" for name, text in CATEGORY_DEFINITIONS.items())

#: Tool names exposed by okeval.tools.ToolRegistry (kept here so prompts can reference them).
TOOL_NAMES: list[str] = [
    "describe_object",
    "list_fields",
    "get_field",
    "search_fields",
    "get_picklist_values",
    "get_relationships",
    "get_child_relationships",
    "find_relationship",
    "get_temporal_semantics",
    "get_validation_rules",
    "get_automation",
    "get_business_processes",
    "search_knowledge",
    "get_provenance",
    "get_record_types",
]
_TOOL_BLOCK = ", ".join(TOOL_NAMES)

# --------------------------------------------------------------------------------------------
# Question generation
# --------------------------------------------------------------------------------------------
QUESTION_GEN_SYSTEM = _prompt_files.fill(_prompt_files.text("question_gen_system"), CATEGORY_BLOCK=_CATEGORY_BLOCK,
                                         TOOL_BLOCK=_TOOL_BLOCK)

QUESTION_GEN_USER = _prompt_files.text("question_gen_user")

# --------------------------------------------------------------------------------------------
# Paraphrasing
# --------------------------------------------------------------------------------------------
PARAPHRASE_SYSTEM = _prompt_files.text("paraphrase_system")

PARAPHRASE_USER = _prompt_files.text("paraphrase_user")

# --------------------------------------------------------------------------------------------
# Answer agent
# --------------------------------------------------------------------------------------------
AGENT_SYSTEM = _prompt_files.text("agent_system")

# --------------------------------------------------------------------------------------------
# Judge
# --------------------------------------------------------------------------------------------
JUDGE_SYSTEM = _prompt_files.text("judge_system")

JUDGE_USER = _prompt_files.text("judge_user")

PARAPHRASE_CHECK_USER = _prompt_files.text("paraphrase_check_user")

PREMISE_CHECK_USER = _prompt_files.text("premise_check_user")

# --------------------------------------------------------------------------------------------
# Registry + rendering
# --------------------------------------------------------------------------------------------
_TEMPLATE_NAMES = (
    "QUESTION_GEN_SYSTEM",
    "QUESTION_GEN_USER",
    "PARAPHRASE_SYSTEM",
    "PARAPHRASE_USER",
    "PARAPHRASE_CHECK_USER",
    "PREMISE_CHECK_USER",
    "AGENT_SYSTEM",
    "JUDGE_SYSTEM",
    "JUDGE_USER",
)

#: name -> template text. Edit entries here to change prompts at runtime.
TEMPLATES: dict[str, str] = {name: globals()[name] for name in _TEMPLATE_NAMES}

_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def placeholders(name_or_template: str) -> list[str]:
    """Return the unique ``{named}`` placeholders of a template (by name or raw text), in order."""
    text = TEMPLATES.get(name_or_template.upper(), name_or_template)
    seen: list[str] = []
    for key in _PLACEHOLDER_RE.findall(text):
        if key not in seen:
            seen.append(key)
    return seen


#: name -> placeholders it requires (computed from the default templates).
TEMPLATE_FIELDS: dict[str, list[str]] = {name: placeholders(name) for name in _TEMPLATE_NAMES}


def _to_text(value: Any) -> str:
    """Convert a value for insertion into a prompt."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value, indent=2, ensure_ascii=False, default=str)
    if value is None:
        return "null"
    return str(value)


def render(name: str, **kw: Any) -> str:
    """Fill template ``name`` (e.g. ``"JUDGE_USER"``) with keyword values.

    Unused keywords are ignored. ``category_definition`` is filled from
    :data:`CATEGORY_DEFINITIONS` when the template needs it and only ``category`` was given.

    Raises:
        KeyError: unknown template name, or one or more placeholders without a value.
    """
    key = name.upper()
    if key not in TEMPLATES:
        raise KeyError(f"unknown template {name!r}; known: {sorted(TEMPLATES)}")
    template = TEMPLATES[key]
    needed = placeholders(template)
    if "category_definition" in needed and "category_definition" not in kw and "category" in kw:
        kw["category_definition"] = CATEGORY_DEFINITIONS.get(
            str(kw["category"]), "(no definition available for this category)"
        )
    missing = [p for p in needed if p not in kw]
    if missing:
        raise KeyError(f"template {key} is missing value(s) for: {missing}")
    return _PLACEHOLDER_RE.sub(lambda m: _to_text(kw[m.group(1)]), template)


# -------------------------------------------------------------------------------------------- several objects
# moved from okeval/multi.py: the knowledge agent's system prompt when several objects are loaded

MULTI_AGENT_SYSTEM = _prompt_files.text("multi_agent_system")

# -------------------------------------------------------------------------------------------- flow components
# new in the flow components wiring (stage 4 of the flow build, 2026-10-24): appended to MULTI_AGENT_SYSTEM when the
# registry has the component tools (sf_assistant.pipeline.build, the MCP server's answer_from_knowledge); not a
# notebook prompt, so it is not in __all__ or TEMPLATES (they stay the notebook's).

KNOWLEDGE_COMPONENTS_NOTE = _prompt_files.text("knowledge_components_note")


def knowledge_system(registry: Any) -> str:
    """The full knowledge agent's system prompt for ``registry`` (several objects): :data:`MULTI_AGENT_SYSTEM`, plus
    :data:`KNOWLEDGE_COMPONENTS_NOTE` when the registry offers the component tools (``registry.components``)."""
    if getattr(registry, "components", None) is not None:
        return MULTI_AGENT_SYSTEM + KNOWLEDGE_COMPONENTS_NOTE
    return MULTI_AGENT_SYSTEM
