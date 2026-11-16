"""Reporting: result DataFrames, per-category summary, knowledge coverage, gaps, export.

``results_frame`` joins questions, agent results and grades on ``qid`` into one flat
DataFrame (one row per question). Every other function works from that frame, except
``coverage`` which needs the knowledge and the raw agent traces.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any, Iterable

import pandas as pd

if TYPE_CHECKING:
    from sf_assistant.agent.knowledge_agent import AgentResult
    from sf_assistant.agent.question import Question
    from sf_assistant.knowledge.store import ObjectKnowledge

    from .grading import Grade

# Columns that hold lists/dicts; JSON-encoded when written to CSV.
_LIST_COLUMNS = ("expected_facts", "facts_missing", "forbidden_found", "expected_tools", "gold_chunk_ids",
                 "tools_called", "chunk_ids", "tool_calls", "judge")


def _judge_get(judge: dict | None, key: str) -> Any:
    """Read a judge field, ``None`` when the judge is absent or errored."""
    if not isinstance(judge, dict) or "error" in judge:
        return None
    return judge.get(key)


def results_frame(questions: Iterable["Question"], results: Iterable["AgentResult"],
                  grades: Iterable["Grade"]) -> pd.DataFrame:
    """One row per question with its answer, trace summary and grade.

    Rows follow question order; a question without a result/grade still gets a row (with
    ``passed=False``) so nothing silently disappears from the report.
    """
    res = {r.qid: r for r in results}
    grd = {g.qid: g for g in grades}
    rows = []
    for q in questions:
        r, g = res.get(q.qid), grd.get(q.qid)
        calls = list(r.tool_calls) if r else []
        chunk_ids: list[str] = []
        for tc in calls:
            for cid in tc.get("chunk_ids") or []:
                if cid not in chunk_ids:
                    chunk_ids.append(cid)
        judge = g.judge if g else None
        usage = (r.usage if r else None) or {}
        rows.append({
            "qid": q.qid,
            "category": q.category,
            "source": q.source,
            "answerable": q.answerable,
            "question": q.question,
            "answer": r.answer if r else "",
            "expected_facts": list(q.expected_facts or []),
            "facts_missing": list(g.facts_missing) if g else list(q.expected_facts or []),
            "fact_recall": g.fact_recall if g else 0.0,
            "expected_tools": list(q.expected_tools or []),
            "tools_called": [tc.get("name") for tc in calls],
            "n_tool_calls": len(calls),
            "n_tool_errors": sum(1 for tc in calls if not tc.get("ok")),
            "tool_hit": g.tool_hit if g else False,
            "refusal_correct": g.refusal_correct if g else None,
            "passed": bool(g.passed) if g else False,
            "forbidden_found": list(getattr(g, "forbidden_found", None) or []) if g else [],
            "judge_correctness": _judge_get(judge, "correctness"),
            "judge_completeness": _judge_get(judge, "completeness"),
            "judge_groundedness": _judge_get(judge, "groundedness"),
            "judge_hallucination": _judge_get(judge, "hallucination"),
            "judge_missing_knowledge": _judge_get(judge, "missing_knowledge"),
            "judge_notes": _judge_get(judge, "notes") or (judge.get("error") if isinstance(judge, dict) else None),
            "gold_chunk_ids": list(q.gold_chunk_ids or []),
            "chunk_ids": chunk_ids,
            "steps": r.steps if r else 0,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "latency_s": r.latency_s if r else None,
            "error": (r.error if r else "no agent result"),
            "tool_calls": calls,
            "judge": judge,
        })
    return pd.DataFrame(rows)


def summary(df: pd.DataFrame) -> pd.DataFrame:
    """Per-category metrics plus an ``ALL`` row.

    Columns: ``category, n, pass_rate, fact_recall, tool_hit, avg_latency_s, errors`` and
    ``judge_correctness`` (mean, NaN when no judge ran).
    """
    cols = ["category", "n", "pass_rate", "fact_recall", "tool_hit", "avg_latency_s", "errors", "judge_correctness"]
    if df.empty:
        return pd.DataFrame(columns=cols)

    def _agg(g: pd.DataFrame, name: str) -> dict:
        jc = pd.to_numeric(g.get("judge_correctness"), errors="coerce") if "judge_correctness" in g else pd.Series(dtype=float)
        return {
            "category": name,
            "n": int(len(g)),
            "pass_rate": round(float(g["passed"].astype(bool).mean()), 3),
            "fact_recall": round(float(pd.to_numeric(g["fact_recall"], errors="coerce").mean()), 3),
            "tool_hit": round(float(g["tool_hit"].astype(bool).mean()), 3),
            "avg_latency_s": round(float(pd.to_numeric(g["latency_s"], errors="coerce").mean()), 2),
            "errors": int(g["error"].notna().sum()) if "error" in g else 0,
            "judge_correctness": round(float(jc.mean()), 3) if jc.notna().any() else float("nan"),
        }

    rows = [_agg(g, str(cat)) for cat, g in df.groupby("category", sort=True)]
    out = pd.DataFrame(rows, columns=cols).sort_values(["pass_rate", "category"]).reset_index(drop=True)
    return pd.concat([out, pd.DataFrame([_agg(df, "ALL")], columns=cols)], ignore_index=True)


#: A tool call that returns more chunks than this (an unfiltered field list, a broad search)
#: does not count as touching them, except the question's own gold chunks.
MAX_CHUNKS_PER_CALL = 12


def _attribute_leaves(knowledge: "ObjectKnowledge") -> list[tuple[str, str, str, str]]:
    """Content leaves of the knowledge JSON a question can assert.

    Returns ``(leaf_id, kind, chunk_id, text)`` for field meanings, field properties
    (precision/scale/length), picklist value meanings, relationship meanings and direction
    semantics, automation purposes and related objects, validation formulas / error meanings,
    and process meanings.
    """
    raw = knowledge.raw
    out: list[tuple[str, str, str, str]] = []
    for f in raw.get("fields") or []:
        api = f.get("api_name")
        if not api:
            continue
        if f.get("business_meaning"):
            out.append((f"field:{api}.business_meaning", "field_meaning", f"field:{api}", f["business_meaning"]))
        for prop in ("precision", "scale", "length"):
            if f.get(prop) is not None:
                out.append((f"field:{api}.{prop}", "field_property", f"field:{api}", str(f[prop])))
        values = (f.get("picklist") or {}).get("values")
        for v in values if isinstance(values, list) else []:
            if isinstance(v, dict) and v.get("business_meaning"):
                out.append((f"picklist:{api}.{v.get('value')}.business_meaning", "picklist_value_meaning",
                            f"picklist:{api}", v["business_meaning"]))
    for r in raw.get("relationships") or []:
        fld = r.get("field")
        if r.get("business_meaning"):
            out.append((f"relationship:{fld}.business_meaning", "relationship_meaning", f"relationship:{fld}",
                        r["business_meaning"]))
        if r.get("direction_semantics"):
            out.append((f"relationship:{fld}.direction_semantics", "direction_semantics", f"relationship:{fld}",
                        " ".join(str(x) for x in (r["direction_semantics"] or {}).values())))
    auto = raw.get("automation") or {}
    for kind, key in (("flow", "flows"), ("apex", "apex")):
        for a in auto.get(key) or []:
            name = a.get("name")
            if a.get("purpose"):
                out.append((f"{kind}:{name}.purpose", "automation_purpose", f"{kind}:{name}", a["purpose"]))
            if a.get("related_objects"):
                out.append((f"{kind}:{name}.related_objects", "automation_related_objects", f"{kind}:{name}",
                            " ".join(a["related_objects"])))
    for r in raw.get("validation_rules") or []:
        for prop in ("formula", "error_meaning"):
            if r.get(prop):
                out.append((f"validation:{r.get('name')}.{prop}", f"validation_{prop}", f"validation:{r.get('name')}",
                            str(r[prop])))
    for p in raw.get("business_process_usage") or []:
        if p.get("meaning"):
            out.append((f"process:{p.get('process_id')}.meaning", "process_meaning", f"process:{p.get('process_id')}",
                        p["meaning"]))
    return out


def _leaf_has_fact(fact: str, text: str) -> bool:
    """``fact`` was taken from ``text``: word-bounded normalised substring, or a long
    descriptive fact whose content words mostly occur in the text."""
    from .grading import fact_in_answer, normalize

    nf = normalize(fact)
    if not nf:
        return False
    if f" {nf} " in f" {normalize(text)} ":
        return True
    return len(nf.split()) >= 3 and fact_in_answer(fact, text)


def coverage(knowledge: "ObjectKnowledge", results: Iterable["AgentResult"], *,
             questions: Iterable["Question"] | None = None, grades: Iterable["Grade"] | None = None,
             max_chunks_per_call: int = MAX_CHUNKS_PER_CALL) -> dict:
    """How much of the knowledge file the run exercised.

    Always returns ``{"touched", "never_touched", "ratio"}`` (tool coverage, in
    ``knowledge.section_ids()`` order): a chunk counts as touched when a tool call returned it
    and that call returned at most ``max_chunks_per_call`` chunks, or it is a gold chunk of
    the question being answered. One unfiltered ``list_fields`` call therefore no longer
    marks every field as touched.

    With ``questions`` (and ``grades``) it also returns:

    * ``question_coverage`` - ``{"covered", "never_covered", "untested", "ratio"}``: chunks that
      are gold chunks of at least one *passed* question whose expected facts are not all
      stated in its own text (unanswerable questions count when they passed); ``untested``
      lists chunks that no question names as gold at all;
    * ``attribute_coverage`` - ``{"by_kind": {kind: {"covered", "total"}}, "uncovered": [...],
      "ratio"}``: content leaves of the JSON (field meanings, precision/scale/length, picklist
      value meanings, automation purposes, validation formulas, process meanings, ...) from
      which at least one expected fact of a question on that chunk was taken;
    * ``knowledge_inconsistencies`` - ``knowledge.consistency_warnings()``.
    """
    all_ids = list(knowledge.section_ids())
    qs = list(questions or [])
    gold_by_qid = {q.qid: set(q.gold_chunk_ids or []) for q in qs}
    seen: set[str] = set()
    for r in results:
        gold = gold_by_qid.get(r.qid, set())
        for tc in r.tool_calls or []:
            ids = list(tc.get("chunk_ids") or [])
            if len(ids) <= max_chunks_per_call:
                seen.update(ids)
            else:
                seen.update(cid for cid in ids if cid in gold)
    touched = [cid for cid in all_ids if cid in seen]
    never = [cid for cid in all_ids if cid not in seen]
    ratio = round(len(touched) / len(all_ids), 4) if all_ids else 0.0
    out: dict[str, Any] = {"touched": touched, "never_touched": never, "ratio": ratio,
                           "max_chunks_per_call": max_chunks_per_call}
    if not qs:
        return out

    from .grading import fact_in_question

    passed = {g.qid: bool(g.passed) for g in grades or []}
    covered: set[str] = set()
    named: set[str] = set()
    for q in qs:
        named.update(q.gold_chunk_ids or [])
        if grades is not None and not passed.get(q.qid):
            continue
        facts = [f for f in q.expected_facts or [] if str(f).strip()]
        if q.answerable and (not facts or all(fact_in_question(f, q.question, knowledge) for f in facts)):
            continue  # tautological: passing it says nothing about the chunk
        covered.update(q.gold_chunk_ids or [])
    qc = [cid for cid in all_ids if cid in covered]
    out["question_coverage"] = {
        "covered": qc,
        "never_covered": [cid for cid in all_ids if cid not in covered],
        "untested": [cid for cid in all_ids if cid not in named],
        "ratio": round(len(qc) / len(all_ids), 4) if all_ids else 0.0,
        "counts_passed_only": grades is not None,
    }

    by_kind: dict[str, dict[str, int]] = {}
    uncovered: list[str] = []
    facts_by_chunk: dict[str, list[str]] = {}
    for q in qs:
        for cid in q.gold_chunk_ids or []:
            facts_by_chunk.setdefault(cid, []).extend(str(f) for f in q.expected_facts or [])
    for leaf_id, kind, cid, text in _attribute_leaves(knowledge):
        stat = by_kind.setdefault(kind, {"covered": 0, "total": 0})
        stat["total"] += 1
        if any(_leaf_has_fact(f, text) for f in facts_by_chunk.get(cid, [])):
            stat["covered"] += 1
        else:
            uncovered.append(leaf_id)
    total = sum(v["total"] for v in by_kind.values())
    out["attribute_coverage"] = {
        "by_kind": by_kind,
        "uncovered": uncovered,
        "ratio": round(sum(v["covered"] for v in by_kind.values()) / total, 4) if total else 0.0,
    }
    try:
        out["knowledge_inconsistencies"] = list(knowledge.consistency_warnings())
    except Exception:  # older knowledge module
        out["knowledge_inconsistencies"] = []
    return out


def knowledge_gaps(df: pd.DataFrame, knowledge: "ObjectKnowledge | None" = None) -> pd.DataFrame:
    """Rows that point at missing or hard-to-find knowledge.

    Selected when the judge flagged ``missing_knowledge`` or ``fact_recall < 0.5``. A
    ``reason`` column says which rule fired. With ``knowledge``, one extra row per
    ``consistency_warnings()`` line (qid ``knowledge-inconsistency-N``) lists places where the
    file contradicts itself, for the JSON authors to fix.
    """
    rows = _knowledge_gap_rows(df)
    if knowledge is None:
        return rows
    try:
        warnings = list(knowledge.consistency_warnings())
    except Exception:
        warnings = []
    if not warnings:
        return rows
    extra = pd.DataFrame([{
        "qid": f"knowledge-inconsistency-{i + 1}", "category": "knowledge_file", "question": "",
        "fact_recall": None, "facts_missing": [], "judge_missing_knowledge": None,
        "judge_notes": w.replace("WARN: consistency: ", ""), "tools_called": [], "answer": "",
        "reason": "knowledge file inconsistency"} for i, w in enumerate(warnings)])
    return pd.concat([rows, extra], ignore_index=True) if not rows.empty else extra


def _knowledge_gap_rows(df: pd.DataFrame) -> pd.DataFrame:
    """Per-question gap rows (see :func:`knowledge_gaps`)."""
    cols = ["qid", "category", "question", "fact_recall", "facts_missing", "judge_missing_knowledge",
            "judge_notes", "tools_called", "answer", "reason"]
    if df.empty:
        return pd.DataFrame(columns=cols)
    mk = df["judge_missing_knowledge"].fillna(False).astype(bool) if "judge_missing_knowledge" in df else False
    low = pd.to_numeric(df["fact_recall"], errors="coerce").fillna(0.0) < 0.5
    sel = df[mk | low].copy()
    if sel.empty:
        return pd.DataFrame(columns=cols)
    sel_mk = mk[sel.index] if isinstance(mk, pd.Series) else pd.Series(False, index=sel.index)

    def _reason(i: Any) -> str:
        parts = []
        if bool(sel_mk[i]):
            parts.append("judge: missing_knowledge")
        if bool(low[i]):
            parts.append(f"fact_recall={sel.at[i, 'fact_recall']:.2f}")
        return "; ".join(parts)

    sel["reason"] = [_reason(i) for i in sel.index]
    return sel[cols].reset_index(drop=True)


# --------------------------------------------------------------------------------------
# export
# --------------------------------------------------------------------------------------

def _jsonable(v: Any) -> Any:
    """Convert numpy/pandas scalars and NaN to plain JSON values."""
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if hasattr(v, "item") and not isinstance(v, (str, bytes)):
        try:
            v = v.item()
        except (ValueError, AttributeError):
            pass
    if isinstance(v, float) and v != v:  # NaN
        return None
    return v


def _md_cell(v: Any) -> str:
    if v is None or (isinstance(v, float) and v != v):
        return ""
    if isinstance(v, float):
        return f"{v:.3f}".rstrip("0").rstrip(".") if v != int(v) else str(int(v))
    return str(v).replace("|", "\\|").replace("\n", " ")


def _md_table(df: pd.DataFrame, max_rows: int | None = None, max_cell: int = 160) -> str:
    """Render a DataFrame as a GitHub-flavoured Markdown table (no ``tabulate`` dependency)."""
    if df.empty:
        return "_none_\n"
    shown = df if max_rows is None else df.head(max_rows)
    head = "| " + " | ".join(map(str, shown.columns)) + " |"
    sep = "| " + " | ".join("---" for _ in shown.columns) + " |"
    body = []
    for _, row in shown.iterrows():
        cells = []
        for v in row.tolist():
            c = _md_cell(v)
            cells.append(c if len(c) <= max_cell else c[: max_cell - 1] + "…")
        body.append("| " + " | ".join(cells) + " |")
    more = f"\n_… {len(df) - len(shown)} more row(s) in results.csv_\n" if len(shown) < len(df) else ""
    return "\n".join([head, sep, *body]) + "\n" + more


def export(df: pd.DataFrame, out_dir: str, *, coverage_info: dict | None = None) -> list[str]:
    """Write ``results.csv``, ``results.jsonl`` and ``summary.md`` into ``out_dir``.

    ``coverage_info`` (the dict from :func:`coverage`) is optional; when given, the Markdown
    summary includes the coverage ratio and the never-touched chunk ids.

    Returns the three written paths.
    """
    os.makedirs(out_dir, exist_ok=True)
    csv_path = os.path.join(out_dir, "results.csv")
    jsonl_path = os.path.join(out_dir, "results.jsonl")
    md_path = os.path.join(out_dir, "summary.md")

    # CSV: list/dict columns as JSON strings so the file round-trips cleanly.
    flat = df.copy()
    for col in _LIST_COLUMNS:
        if col in flat:
            flat[col] = flat[col].map(lambda v: json.dumps(_jsonable(v), ensure_ascii=False, default=str))
    flat.to_csv(csv_path, index=False)

    with open(jsonl_path, "w", encoding="utf-8") as fh:
        for rec in df.to_dict(orient="records"):
            fh.write(json.dumps(_jsonable(rec), ensure_ascii=False, default=str) + "\n")

    summ = summary(df)
    gaps = knowledge_gaps(df)
    lines = ["# Object knowledge evaluation summary", ""]
    if not df.empty:
        n = len(df)
        lines += [
            f"- Questions: **{n}** (answerable {int(df['answerable'].astype(bool).sum())}, "
            f"unanswerable {int((~df['answerable'].astype(bool)).sum())})",
            f"- Pass rate: **{df['passed'].astype(bool).mean():.1%}**",
            f"- Mean fact recall: **{pd.to_numeric(df['fact_recall'], errors='coerce').mean():.3f}**",
            f"- Tool hit rate: **{df['tool_hit'].astype(bool).mean():.1%}**",
            f"- Agent errors: **{int(df['error'].notna().sum())}**",
        ]
        if "judge_hallucination" in df and df["judge_hallucination"].notna().any():
            lines.append(f"- Judge-flagged hallucinations: **{int(df['judge_hallucination'].fillna(False).astype(bool).sum())}**")
    if coverage_info:
        lines.append(f"- Tool coverage: **{coverage_info.get('ratio', 0):.1%}** of chunks returned by a focused "
                     f"tool call ({len(coverage_info.get('touched', []))} touched, "
                     f"{len(coverage_info.get('never_touched', []))} never touched)")
        qc = coverage_info.get("question_coverage")
        if qc:
            lines.append(f"- Question coverage: **{qc['ratio']:.1%}** of chunks are gold chunks of a passed, "
                         f"non-tautological question ({len(qc['covered'])} covered, "
                         f"{len(qc['untested'])} with no question at all)")
        ac = coverage_info.get("attribute_coverage")
        if ac:
            kinds = ", ".join(f"{k} {v['covered']}/{v['total']}" for k, v in sorted(ac["by_kind"].items()))
            lines.append(f"- Attribute coverage: **{ac['ratio']:.1%}** of JSON content leaves feed an expected "
                         f"fact ({kinds})")
    lines += ["", "## Per category", "", _md_table(summ)]
    if coverage_info and coverage_info.get("never_touched"):
        lines += ["## Never-touched knowledge chunks", "",
                  ", ".join(f"`{c}`" for c in coverage_info["never_touched"]), ""]
    qc = (coverage_info or {}).get("question_coverage") or {}
    if qc.get("never_covered"):
        lines += ["## Chunks no passed question verifies", "",
                  ", ".join(f"`{c}`" for c in qc["never_covered"]), ""]
    if (coverage_info or {}).get("knowledge_inconsistencies"):
        lines += ["## Knowledge file inconsistencies", "",
                  *[f"- {w.replace('WARN: consistency: ', '')}" for w in coverage_info["knowledge_inconsistencies"]], ""]
    lines += ["## Knowledge gaps", "",
              _md_table(gaps[["qid", "category", "question", "fact_recall", "facts_missing", "reason", "judge_notes"]]
                        if not gaps.empty else gaps, max_rows=50)]
    if not df.empty:
        fails = df[~df["passed"].astype(bool)][["qid", "category", "question", "fact_recall", "facts_missing",
                                                "tools_called", "error", "answer"]]
        lines += ["## Failures", "", _md_table(fails, max_rows=50)]
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return [csv_path, jsonl_path, md_path]
