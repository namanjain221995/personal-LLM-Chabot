"""Pure text checks of the record agent and the merge (moved verbatim from okrec/session.py).

Thinking, reasoning and working-note cleaners (:func:`strip_thinking`, :func:`strip_reasoning`,
:func:`working_notes`, :func:`drop_working_sentences`, :func:`drop_tool_talk`), reference codes
(:func:`reference_codes`, :func:`drop_ref_codes`), numbers and the number audit (:func:`numbers_in`,
:func:`audit_numbers`, :func:`drop_unsupported`), the RECORD FACTS text (:func:`facts_json`) and tool calls written
as text (:func:`text_tool_call`). No model call, no duckdb.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, ROUND_HALF_UP
from typing import Any

from sf_assistant.agent.record_prompts import NO_RECORDS
from sf_assistant.engine.types import QueryEvidence

#: Error codes (``QueryEvidence.error_code``) of a failed call whose answer is "no such person / not recorded":
#: an unknown name (E4h, E15a), an object with no table (E1b), a documented field that is not synced (E2b).
NOT_FOUND_CODES = frozenset({"E4h", "E15a", "E1b", "E2b"})
#: List-valued result keys that facts_json may shorten (from the end).
_FACT_LIST_KEYS = ("rows", "groups", "buckets", "values", "matches", "changes", "fields", "references",
                   "system_references", "matched_records")
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
#: A line that starts the final answer after visible working text ("**Answer:**", "Final answer: 6 interviews").
_ANSWER_MARK_RE = re.compile(r"^\s*(?:#+\s*)?[*_]{0,3}\s*(?:final\s+)?answer\s*[*_]{0,3}\s*:\s*[*_]{0,3}", re.IGNORECASE)
#: A line that announces the answer at the end of working text ("Let me provide the answer:", "Here is the answer:",
#: "Let me format the answer clearly."); "Let me know ..." never. A first-person line (let me, I will) is planning by
#: itself; a "Here is ..." line counts only after planning text (:data:`_WORKING_RE`).
_ANSWER_LEAD_RE = re.compile(r"^\s*[*_]{0,3}\s*(?:now,?\s+)?(?P<who>let me(?!\s+know)|let's|i will|i'll|i can now|"
                             r"here is|here's)\b[^\n]{0,50}\b(?:final\s+)?(?:answers?|response|reply)\b[^\n.:]{0,25}"
                             r"[*_]{0,3}\s*[.:]\s*[*_]{0,3}\s*$|"
                             # "Perfect! I now have the complete breakdown:" / "Now I have all the data." (no number:
                             # the line itself is dropped)
                             r"^\s*[*_]{0,3}\s*(?:(?:perfect|great|excellent|awesome|alright|okay|ok|good)\s*[!,.]\s*)?"
                             r"(?P<have>i\s+now\s+have|now\s+i\s+have|i\s+have\s+now|i've\s+now\s+got)\b[^\n\d:]{0,80}?"
                             r"[*_]{0,3}\s*[.:!]\s*[*_]{0,3}\s*$|"
                             # "So the answer is:" / "The final answer is:" alone on its line
                             r"^\s*[*_]{0,3}\s*(?:so,?\s+)?(?:the\s+)?(?:final\s+)?answer\s+is\s*[*_]{0,3}\s*:\s*[*_]{0,3}\s*$",
                             re.IGNORECASE)
#: A first-person line that announces presenting the result ("Let me present this clearly:", "I'll format it now.").
_PRESENT_LEAD_RE = re.compile(r"^\s*[*_]{0,3}\s*(?:now,?\s+)?(?P<who>let me(?!\s+know)|let's|i will|i'll|i can now)\s+"
                              r"(?:now\s+)?(?:present|format|write|give|provide|summari[sz]e|put|state|show|lay out|list)\b"
                              r"[^\n.:]{0,40}[*_]{0,3}\s*[.:]\s*[*_]{0,3}\s*$", re.IGNORECASE)
_NUMBER_RE = re.compile(r"(?<![\w.])\d{1,3}(?:,\d{3})+(?:\.\d+)?(?![\w])|(?<![\w.])\d+(?:\.\d+)?(?![\w])")
_SMALL_INTS = frozenset(str(i) for i in range(13))
#: A value or API name as written in an answer: in backticks, in double or curly quotes, or in single quotes that are
#: no apostrophes (no letter or digit just outside them: "the recruiter's" is none).
QUOTED_SPAN_RE = re.compile(r"`[^`\n]*`|\"[^\"\n]*\"|\u201c[^\u201d\n]*\u201d|(?<![\w'])'[^'\n]{1,80}'(?![\w])")


_WRAPPED_JSON_RE = re.compile(r"^(?:```(?:json)?|<tool_call>)\s*(.*?)\s*(?:```|</tool_call>)$", re.DOTALL | re.IGNORECASE)


def text_tool_call(text: str | None, tools: list[dict] | None) -> tuple[str, dict] | None:
    """``(tool name, arguments)`` when a reply is nothing but one tool call written as text, else None.

    The reply (thinking removed, optionally in a ```json fence or <tool_call> tags) must be one JSON object:
    ``{"name": <offered tool>, "arguments": {...}}``, or bare arguments whose keys fit exactly one offered
    tool (every required key present, no unknown key). Anything else (prose, NO_RECORDS, ambiguous keys) is None.
    """
    body = strip_thinking(text)
    m = _WRAPPED_JSON_RE.match(body)
    if m:
        body = m.group(1)
    if not body.startswith("{"):
        return None
    try:
        obj = json.loads(body)
    except ValueError:
        return None
    schemas = {t["function"]["name"]: t["function"].get("parameters") or {} for t in tools or []
               if isinstance(t, dict) and isinstance(t.get("function"), dict) and t["function"].get("name")}
    if not isinstance(obj, dict) or not obj or not schemas:
        return None
    args = obj.get("arguments", obj.get("parameters"))
    name = obj.get("name")
    if isinstance(name, str) and name in schemas and isinstance(args, dict):   # a list or dict name is no call
        return name, args
    fits = [name for name, sch in schemas.items()
            if set(obj) <= set(sch.get("properties") or {}) and set(sch.get("required") or []) <= set(obj)]
    return (fits[0], obj) if len(fits) == 1 else None


class _TextToolCalls:
    """LLM wrapper for the record agent: a reply that is only a tool call written as text becomes that call.

    Qwen now and then writes the arguments of the call it means as a ```json block instead of calling the
    tool, and repeats it after the require-tools nudge, so the question ends with no record data. Calls
    without tools (route, merge) pass through unchanged.
    """

    def __init__(self, llm: Any) -> None:
        self._llm = llm
        self.recovered = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._llm, name)

    def chat(self, messages: list[dict], tools: list[dict] | None = None, **kwargs: Any) -> dict:
        resp = self._llm.chat(messages, tools=tools, **kwargs)
        if not tools or not isinstance(resp, dict) or resp.get("tool_calls"):
            return resp
        call = text_tool_call(resp.get("content"), tools)
        if call is None:
            return resp
        self.recovered += 1
        return {**resp, "content": None,
                "tool_calls": [{"id": f"text-call-{self.recovered}", "name": call[0], "arguments": call[1]}]}


def strip_thinking(text: str | None) -> str:
    """Remove ``<think>…</think>`` blocks and anything before a dangling ``</think>``; trim."""
    if not text:
        return ""
    text = _THINK_RE.sub("", text)
    if "</think>" in text:
        text = text.split("</think>", 1)[1]
    return text.strip()


def _is_source(line: str) -> bool:
    return line.strip().lstrip("*_> ").lower().startswith("source:")


#: First-person planning phrases of working notes written as the reply ("I will use ...", "Let me check", "Let's double
#: check ...", "Wait, ..."); "Let me know ..." is no planning.
_PLANNING_PATTERN = (r"\b(?:I will|I'll|I should|I need to|I am going to|Let me(?!\s+know)|Let's\s+(?:(?:now|also|first|"
                     r"quickly|just)\s+)?(?:double[- ]?check|re-?check|check|verify|confirm|look|see|think|consider|"
                     r"reconsider|make\s+sure))\b|"
                     r"(?:^|\n)\s*[*_]{0,3}\s*(?:Wait|Actually|Hmm|Final check)\b[,:]?")
#: Openers of working notes ("I now have the complete breakdown", "Perfect!", "Good — X resolves to ..."):
#: :func:`drop_working_sentences` drops them (a sentence with no number) or their opener word, so the draft check does
#: not count them alone (``working_notes(openers=False)``).
_OPENER_PATTERN = (r"\b(?:I now have|Now I have|I have now|I've now got|I have (?:all|everything|enough)(?: the)? "
                   r"(?:data|results?|information|numbers|counts|I need))\b|(?:^|\n)\s*[*_]{0,3}\s*"
                   r"(?:(?:Perfect|Great|Excellent|Awesome|Alright|Okay|OK|Good|Got it)\s*(?:!|[\u2014\u2013]|\s-\s)|"
                   r"(?:Perfect|Excellent|Awesome|Alright|Okay|Got it),)")
_PLANNING_RE = re.compile(_PLANNING_PATTERN, re.IGNORECASE)
_WORKING_RE = re.compile(_PLANNING_PATTERN + "|" + _OPENER_PATTERN, re.IGNORECASE)


#: A sentence that restates the question ("The question asks about ...", "The user wants ...").
_RESTATE_RE = re.compile(r"\bthe\s+(?:question|user)(?:\s+[\"\u201c][^\"\u201d\n]{1,200}[\"\u201d])?\s+(?:(?:most\s+)?"
                         r"(?:likely|probably|clearly)\s+)?(?:asks|is\s+asking|asked|wants|means|says|is\s+about|"
                         r"refers?\s+to)\b", re.IGNORECASE)
#: Talk about the prompt ("Per the instructions, ...", "the prompt says ...", 'the rule says "..."', "as instructed").
#: "rules" and "guidelines" only in first person ("per my rules") or quoted: "12 sessions were held following the
#: guidelines in `Session__c`" or "the validation rules require a reason" is business wording.
_PROMPT_TALK_RE = re.compile(r"\b(?:per|according\s+to|following|as\s+per|as\s+stated\s+in)\s+"
                             r"(?:the\s+|my\s+|your\s+)?(?:instructions?|system\s+prompt|prompt)\b|"
                             r"\b(?:per|according\s+to|following|as\s+per)\s+(?:my|your)\s+(?:rules?|guidelines?)\b|"
                             r"\bthe\s+(?:instructions?|prompt)\s+(?:say|says|said|tell|tells|require|requires|"
                             r"ask|asks)\b|\bthe\s+rules?\s+(?:say|says|said)\s*[:\"\u201c]|\bas\s+instructed\b",
                             re.IGNORECASE)


#: Talk about the tools or their result keys in an answer ("The lookup_check also shows ...", "the tool result").
_TOOL_TALK_RE = re.compile(r"\b(?:lookup_check|links_checked|about_people|find_references|describe_fields|get_field_values|count_records|group_records|"
                           r"list_records|search_records|any_role)\b|(?<![\w])lookup[ -]checks?\b|\bthe\s+tools?\b|"
                           r"\btool\s+results?\b", re.IGNORECASE)
#: Tool names and result keys in an answer ("(`any_role`)", "Lookup Check Counts", "the `describe_fields` result"): never
#: business wording, so one of them alone makes a reply working notes (:func:`working_notes`).
_TOOL_WORD_RE = re.compile(r"(?<![A-Za-z0-9_])(?:lookup_check|links_checked|about_people|find_references|describe_fields|find_objects|get_field_values|"
                           r"get_field_history|get_sync_status|count_records|aggregate_records|group_records|trend_records|"
                           r"compare_records|percentage_of_records|list_records|search_records|any_role|no_data_fields|"
                           r"unsynced_fits|same_target(?:_lookups)?|by_record|all_records|split_totals|"
                           r"total_across_groups|matching_values|lookup_groups|empty_lookups|not_in_warehouse)"
                           r"(?![A-Za-z0-9_])|(?<![A-Za-z0-9_])lookup[ -]checks?(?![A-Za-z0-9_])", re.IGNORECASE)


#: The merge call's own input names in an answer ("RECORD FACTS show ...", "the record answer says ...", "potentially
#: misattributed in draft"): working text of the merge, never business wording. Case matters: the inputs are named in
#: capitals, and a Salesforce value "Draft" ("3 quotes in Draft", "the Draft status") is capitalised.
_MERGE_INPUT_RE = re.compile(r"\b(?:RECORD\s+(?:FACTS|ANSWER)|KNOWLEDGE\s+ANSWER|FIELD\s+CHECK)\b|"
                             r"\b[Tt]he\s+(?:[Rr]ecord|[Kk]nowledge)\s+(?:[Aa]nswer|[Ff]acts)\b|"
                             r"\b(?:[Tt]he|[Tt]his|[Tt]hat|[Ii]n(?:\s+the)?|[Ff]rom\s+the)\s+(?:first\s+|earlier\s+|"
                             r"original\s+)?draft\b(?!\s+(?:status|stage|state|value|mode|version|flows?|records?)\b)|"
                             r"\bdraft\s+(?:answer|reply|text)\b")
#: An about_people reference code (P<n> for a listed person, R<n> for one of their records) as a word of its own.
_REF_CODE_RE = re.compile(r"(?<![\w#/-])([PR]\d{1,3})(?![\w-])")


def reference_codes(evidence: list[QueryEvidence] | None) -> dict[str, str]:
    """The reference codes the record facts use (about_people ``people`` and ``matched_records`` "ref": P<n>, R<n>) ->
    the label each stands for; "" when two results give the same code to different labels (no safe replacement)."""
    out: dict[str, str] = {}
    for e in evidence or []:
        res = getattr(e, "model_result", None)
        if not isinstance(res, dict):
            continue
        for key in ("people", "matched_records"):
            for it in res.get(key) or []:
                ref = it.get("ref") if isinstance(it, dict) else None
                if not isinstance(ref, str) or not re.fullmatch(r"[PR]\d{1,3}", ref):
                    continue
                label = " ".join(str(it.get("label") or "").split())
                out[ref] = label if out.get(ref, label) == label else ""
    return out


def drop_ref_codes(text: str | None, codes: dict[str, str]) -> str:
    """``text`` without the internal reference codes of :func:`reference_codes` (codes the facts do not hold stay):
    a code alone in brackets is removed ("Name (P1)" -> "Name", "the Lead record (R2)" -> "the Lead record"), one in a
    bracketed list loses its item ("(Employee, P1)" -> "(Employee)"), "P4/Name" reads "Name", and any other code reads
    as the label it stands for ("**P1 (Employee)**" -> "**<label> (Employee)**"). Source lines are never touched."""
    body = text or ""
    if not codes or not _REF_CODE_RE.search(body):
        return body

    def gone(m: re.Match) -> str:
        return "" if m.group(1) in codes else m.group(0)

    out: list[str] = []
    for ln in body.split("\n"):
        if not _is_source_line(ln):
            ln = re.sub(r"[ \t]*\(\s*([PR]\d{1,3})\s*\)", gone, ln)
            ln = re.sub(r"\s*,\s*([PR]\d{1,3})\s*(?=\))", gone, ln)
            ln = re.sub(r"(?<=\()\s*([PR]\d{1,3})\s*,\s*", gone, ln)
            ln = re.sub(r"(?<![\w#/-])([PR]\d{1,3})\s*/\s*(?=\w)", gone, ln)
            ln = _REF_CODE_RE.sub(lambda m: codes.get(m.group(1)) or m.group(0), ln)
        out.append(ln)
    return "\n".join(out)


def ref_codes_left(text: str | None, codes: dict[str, str], question: str = "") -> list[str]:
    """The reference codes of :func:`reference_codes` that ``text`` (Source lines aside) still names, unless the question
    names them too."""
    asked = set(_REF_CODE_RE.findall(question or ""))
    return list(dict.fromkeys(c for c in _REF_CODE_RE.findall(_body_text(text or "")) if c in codes and c not in asked))


def _is_source_line(line: str) -> bool:
    """A "Source: ..." line in any bullet or bold form ("- **Source**: ...", "**Source:** ...")."""
    return re.match(r"source\s*:", re.sub(r"[*_`]+", "", line or "").strip().lstrip("-> ").strip().lower()) is not None


def _body_text(text: str) -> str:
    """``text`` without its Source lines (in any bullet or bold form)."""
    return "\n".join(ln for ln in (text or "").split("\n") if not _is_source_line(ln))


def _is_no_records(reply: str | None) -> bool:
    """The reply is a NO_RECORDS reply: it starts with NO_RECORDS, or its first or last line (Source lines and blank
    lines left out), without markdown and quotes, is exactly NO_RECORDS (a justification in prose followed by a last
    "NO_RECORDS" line is still the agent's NO_RECORDS decision)."""
    text = (reply or "").strip()
    if not text:
        return False
    if text.lstrip("`*\"' \n").upper().startswith(NO_RECORDS):
        return True
    lines = [ln for ln in text.split("\n") if ln.strip() and not _is_source_line(ln)]
    ends = [lines[0], lines[-1]] if lines else []
    return any(re.sub(r"[`*_\"'#>.:\s-]+", "", ln).upper() == NO_RECORDS.replace("_", "") for ln in ends)


def _count_numbers(text: str) -> set[str]:
    """The numbers of ``text`` without its Source lines, dates and times (the counts it states)."""
    return set(numbers_in(_DATE_TIME_RE.sub(" ", _body_text(text))))


#: A planning sentence that announces the list after it ("Let me break this down by status:", "I'll list them
#: below."): the lead before it and the list are one answer, not two drafts. A bare "...:" ("Let me present this
#: clearly:") is no list announcement by itself; a list item right after it is (:func:`_second_draft_after`).
_ANNOUNCE_RE = re.compile(r"\b(?:break\w*\s+(?:\w+\s+){0,2}down|breakdown|list\w*|below|as\s+follows|following|"
                          r"itemi[sz]e)\b", re.IGNORECASE)
#: A list item or table row ("- Scheduled: 9", "1. ...", "| ... |").
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*\u2022+]\s|\d+[.)]\s|\|)")


def _second_draft_after(plan: str, rest: str) -> bool:
    """Whether ``rest`` (the text after the planning sentence ``plan``) can be a second, whole answer: the plan
    announces no list and ``rest`` starts with a prose or label line, not a list item (a list right after a planning
    sentence belongs to the lead before it)."""
    first = next((ln for ln in (rest or "").split("\n") if ln.strip()), "")
    return bool(first) and not _ANNOUNCE_RE.search(plan.strip()) and not _LIST_ITEM_RE.match(first)


def _two_drafts(text: str, pattern: re.Pattern = _WORKING_RE) -> bool:
    """True when a planning phrase (``pattern``) splits ``text`` into a first draft and a rest that repeats all its
    numbers (the model answered, planned aloud, then answered again; :func:`_second_draft_after`)."""
    for m in pattern.finditer(text or ""):
        before = text[:m.start()]
        end = text.find("\n", m.end())
        after = text[end:] if end >= 0 else ""
        nums = _count_numbers(before)
        plan = re.split(r"(?<=[.!?])\s", text[m.start():end if end >= 0 else None], maxsplit=1)[0]
        if nums and nums <= _count_numbers(after) and _second_draft_after(plan, after):
            return True
    return False


def working_notes(text: str, openers: bool = True) -> bool:
    """True when ``text`` reads as the model's working notes rather than a final answer: it talks about the prompt
    ("Per the instructions, ...") or names a tool or result key outside its Source line (:data:`_TOOL_WORD_RE`:
    "(`any_role`)", "Lookup Check Counts") or the merge call's own inputs (:data:`_MERGE_INPUT_RE`: "RECORD FACTS show
    ...", "the record answer", "in draft"); it has first-person planning phrases and its last non-empty line is not the
    "Source: ..." line (in any bullet or bold form); or it ends with the Source line but has two or more planning
    phrases, or two of these signs: a planning phrase, a restatement of the question ("The question asks ..."), talk
    about the tools ("The lookup_check also shows ..."), a second draft that repeats the numbers written before a
    planning phrase (the working text was sent as the reply). Openers ("Perfect!", "I now have ...") count as planning
    phrases unless ``openers`` is False (the draft check: code drops them from the final answer)."""
    lines = [ln for ln in (text or "").split("\n") if ln.strip()]
    if not lines:
        return False
    body = text or ""
    if _PROMPT_TALK_RE.search(body) or _TOOL_WORD_RE.search(_body_text(body)) or _MERGE_INPUT_RE.search(_body_text(body)):
        return True
    pattern = _WORKING_RE if openers else _PLANNING_RE
    hits = len(pattern.findall(body))
    if _is_source_line(lines[-1]):
        signs = [hits > 0, _RESTATE_RE.search(body) is not None, _TOOL_TALK_RE.search(_body_text(body)) is not None,
                 hits > 0 and _two_drafts(body, pattern)]
        return hits >= 2 or sum(signs) >= 2
    return hits > 0


def draft_needs_final_form(reply: str) -> bool:
    """Whether a record draft gets ``RECORDS_FINAL_FORM_MESSAGE``: it reads as working notes (:func:`working_notes`)
    after the rewrite code applies to every final answer anyway (:func:`drop_tool_talk`; openers are left to
    :func:`drop_working_sentences`), or the fully cleaned text still does. A tool word or an opener that code removes
    costs no model call."""
    if not working_notes(reply):
        return False
    return (working_notes(drop_tool_talk(reply), openers=False)
            or working_notes(drop_tool_talk(drop_working_sentences(reply))))


#: A first-person planning sentence at the end of a line ("... Let me verify this is complete.").
_PLAN_TAIL_RE = re.compile(r"(?:^|(?<=[.!?:])\s+)[*_]{0,3}\s*(?:now,?\s+|so,?\s+)?(?:let\s+me(?!\s+know)|let's|i\s+will|i'll|"
                           r"i\s+need\s+to|i\s+should|i\s+am\s+going\s+to|i'm\s+going\s+to)\b[^.!?\n]*[.!?:]?[*_\s]*$",
                           re.IGNORECASE)


def _repeat_cut(lines: list[str], i: int) -> bool:
    """Line ``i`` ends with a planning sentence, the lines after it are a whole answer (they end with the Source line
    and hold no planning phrase) and that answer repeats every number written before the planning sentence (a first
    draft): cut before it. Never when the planning sentence announces a list or the lines after it start with a list
    item (:func:`_second_draft_after`: "... (`Interviewer__c`). Let me break this down by status:" then bullets)."""
    m = _PLAN_TAIL_RE.search(lines[i])
    rest = [x for x in lines[i + 1:] if x.strip()]
    if not m or not rest or not _is_source_line(rest[-1]) or _WORKING_RE.search("\n".join(rest)):
        return False
    if not _second_draft_after(m.group(0), "\n".join(rest)):
        return False
    nums = _count_numbers("\n".join(lines[:i] + [lines[i][:m.start()]]))
    return bool(nums) and nums <= _count_numbers("\n".join(rest))


def strip_reasoning(text: str | None) -> str:
    """:func:`strip_thinking`, then drop working text the model wrote before its final answer in the visible reply.

    Four shapes are cut: a reply with two or more "Source: ..." lines keeps only the part after the second-to-last
    one (the working ended with a first answer, then the real one follows); a line that announces the answer after
    working text ("Let me provide the answer:", "Let me format the answer clearly.", or "Here is the answer:" after
    planning phrases) keeps the reply after the last such line; a line that ends with a planning sentence ("... Let
    me verify this is complete.") followed by a whole answer that repeats every number written before it keeps that
    answer; a line that starts with an answer marker
    ("**Answer:**", "Final answer: ...") after other text keeps the reply from that line on (its text after the
    marker kept). A bare marker line left at the top is dropped. Replies without these shapes are unchanged.
    """
    body = strip_thinking(text)
    if not body:
        return ""
    lines = body.split("\n")
    src = [i for i, ln in enumerate(lines) if _is_source(ln)]
    if len(src) >= 2 and any(ln.strip() for ln in lines[src[-2] + 1:src[-1]]):
        lines = lines[src[-2] + 1:]
    leads = []
    for i, ln in enumerate(lines):
        answer_line = _ANSWER_LEAD_RE.match(ln)
        m = answer_line or _PRESENT_LEAD_RE.match(ln)
        if not m:   # a planning sentence that ends a line of a first draft, then the whole answer again
            if _repeat_cut(lines, i):
                leads.append(i)
            continue
        if not any(x.strip() for x in lines[:i]) or not any(x.strip() for x in lines[i + 1:]):
            continue
        before, after = "\n".join(lines[:i]), "\n".join(lines[i + 1:])
        rest = [x for x in lines[i + 1:] if x.strip()]
        complete = ln.rstrip(" *_").endswith(":") or _is_source(rest[-1])   # "...clearly." needs a whole answer after it
        if answer_line:   # "Let me provide the answer:" is planning by itself; "Here is the answer:" after planning
            who = answer_line.group("who") or answer_line.group("have") or ""
            working = not who.lower().startswith("here") or _WORKING_RE.search(before)
        else:             # "Let me present this clearly:" only after planning or after a first draft it repeats
            nums = set(numbers_in(before))
            working = _WORKING_RE.search(before) or (bool(nums) and nums <= set(numbers_in(after)))
        if complete and working:
            leads.append(i)
    if leads:
        lines = lines[leads[-1] + 1:]   # the last announcement after working text: the final answer follows it
    for i, ln in enumerate(lines):
        if not _ANSWER_MARK_RE.match(ln):
            continue
        head = _ANSWER_MARK_RE.sub("", ln, count=1).strip().strip("*_ ").strip()
        if any(x.strip() for x in lines[:i]):
            lines = ([head] if head else []) + lines[i + 1:]
        elif not head:
            lines = lines[i + 1:]   # a bare "**Answer:**" line on top
        break   # only the first marker; an answer that opens with "Answer: <text>" stays as it is
    out = "\n".join(lines).strip()
    return out or body


#: A sentence that is working text in a final answer: first-person planning of the next step ("Let me check ...", "I
#: should answer for ...", "I will now verify ...") or "Wait, ...". Statements ("I will not count the legacy field",
#: "Let me clarify: ...") and "Let me know ..." are kept.
_PLAN_VERBS = (r"(?:now\s+|also\s+|first\s+|then\s+|quickly\s+)*(?:check|verify|confirm|look|see|query|count|run|call|"
               r"use|try|search|find|answer|report|provide|present|format|write|give|list|consider|re-?check|double[- ]?check|"
               r"make\s+sure|focus|start|re-?run|re-?query)\b")
_OPENER = r"(?:perfect|great|excellent|awesome|alright|all\s+right|okay|ok|good|got\s+it)"
_DROP_SENTENCE_RE = re.compile(r"^[*_\s]*(?:(?:now|so|okay|ok|first|next|then|perfect|great|excellent|alright),?\s+)?"
                               r"(?:(?:let\s+me|let's|i\s+will|i'll|"
                               r"i\s+should|i\s+need\s+to|i\s+am\s+going\s+to|i'm\s+going\s+to|i\s+can\s+now|i\s+must)\s+"
                               + _PLAN_VERBS + r"|wait\b|hmm\b|"
                               # an opener alone ("Perfect!") and "I now have the complete breakdown."
                               + _OPENER + r"\s*[!.]?[*_\s]*$|(?:i\s+now\s+have|now\s+i\s+have|i\s+have\s+now|"
                               r"i've\s+now\s+got|i\s+have\s+(?:all|everything|enough))\b)", re.IGNORECASE)
#: A sentence that only restates the question ("The question asks about mock interviews she supported."): dropped when
#: it names no API name and says nothing negative (a "does not record ..." clause is content).
_RESTATE_SENTENCE_RE = re.compile(r"^[*_\s]*the\s+(?:question|user)(?:\s+[\"\u201c][^\"\u201d\n]{1,200}[\"\u201d])?\s+"
                                  r"(?:(?:most\s+)?(?:likely|probably|clearly)\s+)?(?:asks|is\s+asking|wants|"
                                  r"refers?\s+to)\b", re.IGNORECASE)
_CONTENT_RE = re.compile(r"`|__c\b|\bnot\b|n't\b|\bno\b|\bnever\b|\bblank\b|\blegacy\b", re.IGNORECASE)
#: Content in a planning-form sentence: an API name, a negation, "same name", "different record".
_PLAN_CONTENT_RE = re.compile(_CONTENT_RE.pattern + r"|\bsame\s+name\b|\bdifferent\s+(?:record|person|employee|"
                              r"candidate|account)s?\b", re.IGNORECASE)
#: A planning sentence that reports a fact ("I need to report that no close spelling exists in `Recruiter__c`.").
_PLAN_REPORT_RE = re.compile(r"\b(?:report|answer|say|state|note|mention|explain)\s+that\b", re.IGNORECASE)


def _plan_statement(sent: str) -> str | None:
    """The content a planning-form sentence carries, else None (the sentence is dropped): the whole sentence when it
    reports a fact with content ("I need to report that no close spelling exists in `Recruiter__c` either."), else
    its clause after ";" when that clause has content and is no plan itself ("I should answer for the Employee record;
    the Account record with the same name is a different record." keeps "The Account record with the same name is a
    different record."). A plan that only says what to do next ("I will use the sync time ...", "I should answer for
    the Employee with `Interviewer__c` first, then ...") carries none."""
    if _PLAN_REPORT_RE.search(sent) and _PLAN_CONTENT_RE.search(sent):
        return sent
    _head, sep, tail = sent.partition(";")
    tail = tail.strip()
    if sep and tail and _PLAN_CONTENT_RE.search(tail) and not _DROP_SENTENCE_RE.match(tail):
        return tail[:1].upper() + tail[1:]
    return None


#: "Per the instructions, " at the start of a sentence: dropped, the rest of the sentence kept.
_PROMPT_LEAD_RE = re.compile(r"^(?P<pre>[*_\s]*)(?:per|according\s+to|following|as\s+per)\s+(?:the\s+|my\s+|your\s+)?"
                             r"(?:instructions?|system\s+prompt|prompt|rules?|guidelines?)\s*,\s*", re.IGNORECASE)
#: "Based on the query results:" / "According to the data," at the start of a sentence (nothing named after it): the
#: lead-in is dropped, the rest of the sentence kept. "Based on the data from `X__c`:" names something and stays.
_RESULTS_LEAD_RE = re.compile(r"^(?P<pre>[*_\s]*)(?:based\s+on|according\s+to|from|looking\s+at)\s+(?:the\s+|these\s+|"
                              r"my\s+)?(?:(?:query|tool|record)\s+)?(?:results?|data|findings|output|counts?|quer(?:y|ies))"
                              r"(?:\s+above)?[*_]*\s*[:,][*_]*\s*", re.IGNORECASE)
#: An opener word with a dash or comma at the start of a sentence ("Good \u2014 X resolves to ...", "Great, the count
#: ..."): dropped, the rest of the sentence kept (an opener alone is a :data:`_DROP_SENTENCE_RE` sentence).
_OPENER_LEAD_RE = re.compile(r"^(?P<pre>[*_\s]*)(?:" + _OPENER + r"\s*(?:[\u2014\u2013]|\s-)|(?:perfect|excellent|awesome|"
                             r"alright|all\s+right|okay|got\s+it),)\s*(?=\S)", re.IGNORECASE)
#: A sentence that says which field the model picked ("`X__c` has 9 distinct values and is the correct field for
#: 'type'"): working text. Its numbers are metadata (distinct values) or repeated elsewhere, see
#: :func:`drop_working_sentences`.
_FIELD_CHOICE_RE = re.compile(r"\b(?:is|are)\s+the\s+(?:correct|right|appropriate|proper|best|relevant)\s+"
                              r"(?:field|lookup|object|column|one)s?\s+(?:for|to\s+(?:use|count|group))\b", re.IGNORECASE)
#: A number in a field-choice sentence that counts values or fields, not records ("9 distinct values").
_META_COUNT_RE = re.compile(r"(?<![\w.,])(\d[\d,]*)\s+(?:(?:distinct|different|picklist|stored)\s+)?"
                            r"(?:values?|fields?|lookups?|options?)\b", re.IGNORECASE)
#: A markdown rule line ("---", "***", "___") at the top or the bottom of an answer (before its Source line).
_RULE_LINE_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")


def _planning_count_sentence(sent: str) -> bool:
    """A planning or question-restating sentence whose numbers only count people, lookups or roles ("The question asks
    '...' - since there are 4 people named X, I should report per person."): working text, dropped although it has a
    number (no record count is lost: :data:`_NOT_RECORD_COUNT_RE`, at most 12)."""
    if not (_DROP_SENTENCE_RE.match(sent) or _RESTATE_SENTENCE_RE.match(sent)) or not _PLANNING_RE.search(sent):
        return False
    nums = numbers_in(sent)
    counted = {n for m in _NOT_RECORD_COUNT_RE.finditer(sent) for n in numbers_in(m.group(1))}
    return bool(nums) and all(n in counted and n in _SMALL_INTS for n in nums)


def _field_choice_sentence(sent: str, body: str, first: bool) -> bool:
    """A sentence that says which field is the correct one (:data:`_FIELD_CHOICE_RE`) as the answer's opener (``first``)
    or with a count of values or fields (:data:`_META_COUNT_RE`: "has 9 distinct values and is the correct field for
    'type'"), whose numbers count values or fields or appear elsewhere in the answer ``body``: working text. A later
    sentence without such a count ("`Interviewer__c` is the correct lookup for interviews conducted.") is kept."""
    if not _FIELD_CHOICE_RE.search(sent):
        return False
    meta = {n for m in _META_COUNT_RE.finditer(sent) for n in numbers_in(m.group(1))}
    if not (first or meta):
        return False
    rest = set(numbers_in(body.replace(sent, " ")))
    return all(n in meta or n in rest for n in numbers_in(sent))


def drop_working_sentences(text: str | None) -> str:
    """``text`` without sentences that are working text (:data:`_DROP_SENTENCE_RE`: planning such as "Let me verify
    this is complete." or "I should answer for the Employee first.", "Wait, ..."; a sentence that only restates the
    question, :data:`_RESTATE_SENTENCE_RE`; the content a planning sentence carries is kept, :func:`_plan_statement`)
    and
    without a "Per the instructions, " or "Based on the query results:" opening (the rest of that sentence kept), a
    markdown rule line at the top or just before the Source line ("---"). Sentences with a number are kept (a count
    must never be lost), except a planning or restating sentence whose numbers only count people or lookups
    (:func:`_planning_count_sentence`) and a sentence that says which field is the correct one when its numbers count
    values or are repeated elsewhere (:func:`_field_choice_sentence`); Source lines are kept; a line left empty is
    removed. A text that would lose everything is returned unchanged. The last safety net for the final record answer
    and the merged answer, after :func:`strip_reasoning` and the working-notes nudge."""
    body = text or ""
    if not body.strip():
        return body
    out_lines: list[str] = []
    changed = False
    lines = body.split("\n")
    content = [i for i, ln in enumerate(lines) if ln.strip() and not _is_source_line(ln)]
    rules = {i for i in content if _RULE_LINE_RE.match(lines[i])}
    edge: set[int] = set()   # rule lines before the first content line or after the last one
    for seq in (content, content[::-1]):
        for i in seq:
            if i not in rules:
                break
            edge.add(i)
    for idx, ln in enumerate(lines):
        if idx in edge:
            changed = True
            continue
        if not ln.strip() or _is_source_line(ln):
            out_lines.append(ln)
            continue
        m = re.match(r"^(\s*(?:[-*+]|\d+[.)])\s+)", ln)   # keep a bullet's marker
        prefix, rest = (m.group(1), ln[m.end():]) if m else ("", ln)
        kept: list[str] = []
        for sent in re.split(r"(?<=[.!?])\s+", rest):
            if not sent.strip():
                continue
            pm = _PROMPT_LEAD_RE.match(sent) or _RESULTS_LEAD_RE.match(sent) or _OPENER_LEAD_RE.match(sent)
            if pm:   # "Per the instructions, X" -> "X"; "Based on the query results: X" -> "X"; "Good - X" -> "X"
                tail = sent[pm.end():]
                sent = pm.group("pre") + tail[:1].upper() + tail[1:]
                changed = True
                if not tail.strip(" *_"):
                    continue
            first = not any(x.strip() and not _is_source_line(x) for x in out_lines) and not kept
            if _planning_count_sentence(sent) or _field_choice_sentence(sent, body, first):
                changed = True
                continue
            if not numbers_in(sent) and _DROP_SENTENCE_RE.match(sent):
                content = _plan_statement(sent)
                if content != sent:
                    changed = True
                if content:
                    kept.append(content)
                continue
            if not numbers_in(sent) and _RESTATE_SENTENCE_RE.match(sent) and not _CONTENT_RE.search(sent):
                changed = True
                continue
            kept.append(sent)
        if kept:
            out_lines.append(prefix + " ".join(kept))
        elif ln.strip():
            changed = True
    if not changed:
        return body
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(out_lines)).strip()
    return out if out and any(x.strip() and not _is_source_line(x) for x in out.split("\n")) else body


_TOOL_NAMES = (r"lookup_check|lookup[ -]checks?|links_checked|about_people|find_references|describe_fields|find_objects|get_field_values|"
               r"get_field_history|count_records|aggregate_records|group_records|trend_records|compare_records|"
               r"percentage_of_records|list_records|search_records")
#: A tool word alone in brackets: "(`any_role`)", "(lookup_check)", "(from describe_fields)", "(per the lookup check)",
#: also with trailing words and no number ("(from `lookup_check` on the same query)": ``tail``).
_TOOL_PAREN_RE = re.compile(r"[ \t]*\(\s*(?:(?:from|via|per|in|by)\s+)?(?:the\s+)?[`*]*(?P<word>any_role|" + _TOOL_NAMES
                            + r")[`*]*(?:\s+(?:result|results|output|data|count|scan))?(?P<tail>[\s,;:][^()\n\d]{0,80}?)?"
                            r"\s*\)", re.IGNORECASE)
#: A "Lookup check" label of a line ("- **Lookup check**:", "**Lookup Check:**"): it reads "Other lookups".
_TOOL_LABEL_RE = re.compile(r"^(?P<pre>\s*(?:(?:[-*+\u2022]|\d+[.)])\s+)?[*_]*)lookup[ _-]checks?(?P<post>[*_]*\s*:)",
                            re.IGNORECASE)
#: "the `find_references` query" (a data tool named as the source): it reads "the records".
_TOOL_QUERY_RE = re.compile(r"\bthe\s+[`*]*(?:about_people|find_references|get_field_values|get_field_history|count_records|"
                            r"aggregate_records|group_records|trend_records|compare_records|percentage_of_records|"
                            r"list_records|search_records)[`*]*\s+(?:query|queries|call|calls)\b", re.IGNORECASE)
#: The line already says "in any role" / "in any capacity" (an "(any_role)" after it adds nothing).
_ANY_ROLE_SAID_RE = re.compile(r"\bany\s+(?:role|capacity)\b", re.IGNORECASE)
#: A lead-in that names a tool or its result ("The `describe_fields` result shows that ", "the lookup check also shows ",
#: "According to the lookup_check, "): dropped, the content after it kept.
_TOOL_LEAD_RE = re.compile(r"(?:\bthe\s+)?[`*]*(?<![A-Za-z0-9_])(?:" + _TOOL_NAMES + r")[`*]*\s+(?:(?:result|results|output|"
                           r"call|scan|tool|data)\s+)?(?:also\s+)?(?:shows?|says|confirms?|indicates?|lists?|gives?|"
                           r"reports?|found|finds)\s+(?:that\s+)?|\b(?:per|according\s+to|from)\s+the\s+[`*]*(?:"
                           + _TOOL_NAMES + r")[`*]*(?:\s+(?:result|results|output|data))?\s*,\s*", re.IGNORECASE)
#: "Lookup Check Counts" (a heading or a phrase).
_TOOL_COUNTS_RE = re.compile(r"(?<![A-Za-z0-9_])lookup[ _-]checks?\s+counts?\b", re.IGNORECASE)
_ANY_ROLE_RE = re.compile(r"[`*]*(?<![A-Za-z0-9_])any_role(?![A-Za-z0-9_])[`*]*", re.IGNORECASE)


def drop_tool_talk(text: str | None) -> str:
    """``text`` with the tool talk an answer must not show rewritten (:data:`_TOOL_WORD_RE`): a tool word in brackets,
    alone or with trailing words that hold no number, is removed ("456 in any capacity (`any_role`)" -> "456 in any
    capacity"; "456 (`any_role`)" -> "456 (in any role)"; "(from `lookup_check` on the same query)" -> ""), a "Lookup
    check:" label reads "Other lookups:", "the `find_references` query" reads "the records", a lead-in naming a tool or
    its result is dropped with the content after it kept ("The `describe_fields` result shows `X` is blank" -> "`X` is
    blank"), "Lookup Check Counts" reads "Counts through other lookups" and "any_role" reads "any role". Source lines and
    numbers are never touched; other tool words stay (a merged text that still has one is rejected as working notes,
    and a record draft gets the final-form message)."""
    body = text or ""
    if not _TOOL_WORD_RE.search(_body_text(body)):
        return body
    out: list[str] = []
    for ln in body.split("\n"):
        if _is_source_line(ln):
            out.append(ln)
            continue
        def paren(m: re.Match, line: str = ln) -> str:
            if m.group("word").lower() != "any_role":
                return ""
            if m.group("tail"):   # "(any_role: creator and owner not included)": the words stay ("any role" below)
                return m.group(0)
            return "" if _ANY_ROLE_SAID_RE.search(line) else " (in any role)"

        ln = _TOOL_PAREN_RE.sub(paren, ln)
        ln = _TOOL_LABEL_RE.sub(lambda m: m.group("pre") + "Other lookups" + m.group("post"), ln)
        ln = _TOOL_QUERY_RE.sub("the records", ln)
        ln = _TOOL_LEAD_RE.sub("\x00", ln)
        ln = re.sub(r"(^|[.!?:]\s+|^\s*(?:[-*+]|\d+[.)])\s+|^[*_]+\s*)\x00([`*_]*)([a-z])",
                    lambda m: m.group(1) + m.group(2) + m.group(3).upper(), ln).replace("\x00", "")
        ln = _TOOL_COUNTS_RE.sub(lambda m: ("C" if m.group(0)[:1].isupper() else "c") + "ounts through other lookups", ln)
        ln = _ANY_ROLE_RE.sub("any role", ln)
        out.append(ln)
    return "\n".join(out)


# ------------------------------------------------------------------------------------- numbers

def _norm_number(token: str) -> str | None:
    try:
        d = Decimal(token.replace(",", ""))
    except InvalidOperation:
        return None
    return format(d.normalize(), "f")


def numbers_in(text: str) -> list[str]:
    """Number tokens of ``text``, normalised ('2,562' -> '2562', '6.80' -> '6.8', '05' -> '5').

    Digits glued to letters or underscores are ignored (``Q2``, ``Ref_1_Email__c``); in ``REC-00024``
    the part after the hyphen counts.
    """
    out: list[str] = []
    for tok in _NUMBER_RE.findall(text or ""):
        n = _norm_number(tok)
        if n is not None:
            out.append(n)
    return out


def _allowed(sources: list[str]) -> set[str]:
    allowed = set(_SMALL_INTS)
    for src in sources:
        for tok in numbers_in(src or ""):
            allowed.add(tok)
            if "." in tok:
                d = Decimal(tok)
                places = len(tok.split(".", 1)[1])
                for k in range(places):
                    q = Decimal(1).scaleb(-k)
                    for mode in (ROUND_HALF_UP, ROUND_HALF_EVEN):
                        allowed.add(format(d.quantize(q, rounding=mode).normalize(), "f"))
    return allowed


def quoted_spans(text: str | None) -> list[str]:
    """The quoted values and API names of ``text`` (:data:`QUOTED_SPAN_RE`), in order."""
    return QUOTED_SPAN_RE.findall(text or "")


def audit_numbers(text: str, sources: list[str]) -> list[str]:
    """Numbers of ``text`` found in none of ``sources`` (unique, in order of appearance).

    Allowed: every number of every source, every rounding of a source decimal to fewer places,
    and the integers 0 to 12.
    """
    allowed = _allowed(sources)
    out: list[str] = []
    for tok in numbers_in(text):
        if tok not in allowed and tok not in out:
            out.append(tok)
    return out


def _label_only(line: str) -> bool:
    """A label or heading line with no number ("**Total interviews supported (client + internal):**", "### Totals")."""
    s = re.sub(r"[*_`#>]+", "", line or "").strip()
    return bool(s) and not numbers_in(s) and (s.endswith(":") or (line or "").lstrip().startswith("#"))


def drop_unsupported(text: str | None, sources: list[str]) -> tuple[str, list[str]]:
    """``text`` without the parts that hold a number found in none of ``sources`` (:func:`audit_numbers`), and the
    numbers removed. A list item or table row with such a number is removed whole ("- **Total: 257 interviews**"), in
    other lines the sentence that holds it; a label line ("**Totals:**") whose list items were all removed goes too.
    Source lines are never touched. The record agent's last answer is checked by no model call when its tool budget
    ran out (``okeval.agent.answer_question`` checks a final answer only while steps remain), so code removes what no
    result backs: a sum the model added up ("251 + 6 = 257"). Unchanged (the numbers stay reported) when nothing
    would be left that states a number, or when there is nothing to remove."""
    body = text or ""
    bad = set(audit_numbers(body, sources))
    if not bad:
        return body, []
    lines = body.split("\n")
    out: list[str | None] = []
    removed: list[str] = []
    for ln in lines:
        if not ln.strip() or _is_source_line(ln):
            out.append(ln)
            continue
        hits = [n for n in numbers_in(ln) if n in bad]
        if not hits:
            out.append(ln)
            continue
        if _LIST_ITEM_RE.match(ln):
            out.append(None)
            removed += hits
            continue
        m = re.match(r"^(\s*(?:[-*+]|\d+[.)])\s+)", ln)
        prefix, rest = (m.group(1), ln[m.end():]) if m else ("", ln)
        kept = []
        for sent in re.split(r"(?<=[.!?])\s+", rest):
            sent_bad = [n for n in numbers_in(sent) if n in bad]
            if sent_bad:
                removed += sent_bad
            elif sent.strip():
                kept.append(sent)
        out.append(prefix + " ".join(kept) if kept else None)
    # a label line whose list items were all removed
    for i, ln in enumerate(out):
        if ln is None or not _label_only(ln):
            continue
        j, items, gone = i + 1, 0, 0
        while j < len(lines) and lines[j].strip() and (_LIST_ITEM_RE.match(lines[j]) or out[j] is None) \
                and not _label_only(lines[j]):
            items += 1
            gone += out[j] is None
            j += 1
        if items and gone == items:
            out[i] = None
    result = re.sub(r"\n{3,}", "\n\n", "\n".join(x for x in out if x is not None)).strip()
    if not removed or not numbers_in(_DATE_TIME_RE.sub(" ", _body_text(result))):
        return body, []
    return result, list(dict.fromkeys(removed))


# --------------------------------------------------------------------------------------- facts

def _error_code(e: QueryEvidence) -> str | None:
    """``QueryEvidence.error_code`` (None for evidence built before the field existed)."""
    return getattr(e, "error_code", None)


def _not_available(e: QueryEvidence) -> bool:
    """A failed call whose error says the person, field or object is not there (NOT_FOUND_CODES)."""
    return not e.ok and _error_code(e) in NOT_FOUND_CODES


def _fact_entries(evidence: list[QueryEvidence]) -> list[dict]:
    ok = [e for e in evidence if e.ok and e.model_result is not None]
    entries = [{"query_id": e.query_id, "tool": e.tool, "object": e.object, "result": e.model_result,
                "_kind": e.kind} for e in ok if e.kind == "data"]
    # failed calls that say "not there": no counts, only the error text (the merge must say "not recorded")
    entries += [{"query_id": e.query_id, "tool": e.tool, "object": e.object,
                 "not_available": " ".join(str(e.error or "").split())[:300], "_kind": "not_available"}
                for e in evidence if _not_available(e)]
    entries += [{"query_id": e.query_id, "tool": e.tool, "object": e.object, "result": e.model_result,
                 "_kind": e.kind} for e in ok if e.kind != "data"]
    return entries


def _dump(entries: list[dict]) -> str:
    return json.dumps([{k: v for k, v in e.items() if k != "_kind"} for e in entries], ensure_ascii=False,
                      default=str)


def _trim_largest_list(entries: list[dict]) -> bool:
    """Halve the longest trimmable list among the results. False when nothing is left to trim."""
    best: tuple[int, dict, str] | None = None
    for e in entries:
        res = e.get("result")
        if not isinstance(res, dict):
            continue
        for key in _FACT_LIST_KEYS:
            lst = res.get(key)
            if isinstance(lst, list) and lst:
                size = len(json.dumps(lst, default=str))
                if best is None or size > best[0]:
                    best = (size, e, key)
    if best is None:
        return False
    _size, e, key = best
    res = dict(e["result"])
    lst = res[key]
    res[key] = lst[:len(lst) // 2]
    res["trimmed"] = True
    e["result"] = res
    return True


#: The detail keys the Group B people fixes add to record results (docs/people.md), per list key of a result (and at
#: the top): facts_json drops them, with their notes (``tools.record_base.DETAIL_NOTE_MARKS``), before anything else.
#: A shared record's system work (``matched_records[].system_work``, review of the evaluation fixes, 2026-10-22: the
#: work a person's ``system_work`` held before) is a detail like the person's.
_DETAIL_KEYS: dict[str, tuple[str, ...]] = {
    "lookup_check": ("paths", "any_path", "related", "copy_source"),
    "references": ("distinct", "via_parents", "any_path", "who"),
    "system_references": ("who",),
    "people": ("linked", "system_work", "more_references"),
    "matched_records": ("system_work",),
    "links": ("others", "by_status", "holder"),
}
_DETAIL_TOP = ("also_held", "child_people", "child_copy")


def _strip_details(entry: dict) -> bool:
    """``entry``'s result without the Group B detail keys and notes (a copy; the evidence is never changed). True when
    something was removed."""
    from sf_assistant.tools.record_base import DETAIL_NOTE_MARKS

    res = entry.get("result")
    if not isinstance(res, dict):
        return False
    out = dict(res)
    changed = False
    for key in _DETAIL_TOP:
        if key in out:
            out.pop(key)
            changed = True
    for key, details in _DETAIL_KEYS.items():
        lst = out.get(key)
        if not isinstance(lst, list):
            continue
        new = []
        for x in lst:
            if isinstance(x, dict) and any(d in x for d in details):
                x = {k: v for k, v in x.items() if k not in details}
                changed = True
            new.append(x)
        out[key] = new
    notes = out.get("notes")
    if isinstance(notes, list):
        kept = [n for n in notes if not (isinstance(n, str) and any(m in n for m in DETAIL_NOTE_MARKS))]
        if len(kept) != len(notes):
            out["notes"] = kept
            changed = True
    if changed:
        entry["result"] = out
    return changed


def facts_json(evidence: list[QueryEvidence], max_chars: int = 8000) -> str:
    """JSON list of the successful results the model saw: data tools first, then orientation tools.

    Failed calls whose ``error_code`` is in :data:`NOT_FOUND_CODES` add ``{"query_id", "tool", "object",
    "not_available": <error, at most 300 characters>}`` after the data entries (no counts). Over
    ``max_chars``: the detail keys and notes of the Group B people fixes (:data:`_DETAIL_KEYS`) go first, one entry
    at a time from the last (so they never push out what the facts had without them), then orientation entries, then
    the not_available entries, are dropped (from the end), then the longest list-valued keys (rows, groups, buckets,
    values, matches, changes, fields, references, system_references, matched_records) are shortened from the end;
    trimmed results get ``"trimmed": true``. The first entry is always kept.
    """
    entries = _fact_entries(evidence)
    text = _dump(entries)
    if len(text) <= max_chars:
        return text
    for e in reversed(entries):
        if _strip_details(e):
            text = _dump(entries)
            if len(text) <= max_chars:
                return text
    while len(text) > max_chars and len(entries) > 1 and entries[-1]["_kind"] != "data":
        entries.pop()
        text = _dump(entries)
    while len(text) > max_chars and _trim_largest_list(entries):
        text = _dump(entries)
    while len(text) > max_chars and len(entries) > 1:
        entries.pop()
        text = _dump(entries)
    return text
#: Dates and times of a draft (resolved period bounds, the sync time): their numbers are never counts.
_DATE_TIME_RE = re.compile(r"\b\d{4}-\d{1,2}-\d{1,2}(?:[T ]\d{1,2}:\d{2}(?::\d{2})?)?|\b\d{1,2}:\d{2}(?::\d{2})?\b")
#: A number that counts lookups, roles or fields, not records ("3 other roles", "2 lookups"): never extra records.
_NOT_RECORD_COUNT_RE = re.compile(r"(?<![\w.,])(\d[\d,]*)\s+(?:(?:other|more|additional|different|separate|distinct|"
                                  r"further|extra)\s+)?(?:lookups?|roles?|fields?|people|persons?|columns?|objects?|"
                                  r"capacit(?:y|ies)|ways?|types?|categories)\b", re.IGNORECASE)
