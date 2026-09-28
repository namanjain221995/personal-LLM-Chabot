# -*- coding: utf-8 -*-
"""Score `app.artifacts.intent.decide` against `understanding_corpus.json`:
did the product understand what the person typed?

    python3 score_understanding.py --orchestrator <worktree>/orchestrator
    python3 score_understanding.py --only answer_about_upload --lang gu
    python3 score_understanding.py --source authored-2026-09-28 --show-all
    python3 score_understanding.py --save-baseline base.json
    python3 score_understanding.py --compare base.json --fail-under 90
    python3 score_understanding.py --self-test

To measure the RUNNING PRODUCT (the authority for what production does):

    docker cp score_understanding.py    sf-local-ai-orchestrator-1:/tmp/
    docker cp understanding_corpus.json sf-local-ai-orchestrator-1:/tmp/
    docker exec sf-local-ai-orchestrator-1 python /tmp/score_understanding.py \
        --orchestrator /app --corpus /tmp/understanding_corpus.json

WHAT IT GRADES: the DECISION, never the wording of an answer. `decide` is
rules-only and synchronous — no model, no database, no network — which is
what makes it gradable and what the Fast lane falls back to in silence when
the classifier runs out of time. A turn that is only understood when a model
answers in time is not understood.

Every item names a `want` (what should happen) and a `category` (what the
person was doing); the grade is one outcome per item, worst first:

  made_a_file          a file for a turn that asked for none               HARM
  no_file              no file for a turn that asked for one               HARM
  wrong_action         a file of the wrong kind (a create for an edit …)   HARM
  wrong_format         a file, not in the format the person named          HARM
  plot_without_a_table a diagram routed to the plot-a-spreadsheet path     HARM
  prose_file           a Word/PDF for a turn that asked for a picture in chat HARM
  refused_a_drawable   a refusal for something this platform can draw      HARM
  refused_a_question   a refusal sentence for a question about a TERM      HARM
  no_chart             a file with no chart for a chart ask                HARM
  wrong_source         a question about the PERSON'S file answered from OURS HARM
  ungrounded_chat      right to make no file, but nothing reads ours back  HARM
  over_grounded        our file read back for a turn that is not about it  HARM
  read_back_missing    a diagram ask answered by reading a file back       HARM
  raised               decide() raised                                     HARM
  left_to_the_model    nothing can draw it and nothing in the code says so soft
  ok

`accept` on an item lists further wants that also count as ok (an upload's
file may be create, export or convert; a refusal with our card last may be
answered plainly or read back). `debatable` items are scored and reported
separately; --exclude-debatable leaves them out of the gate.

Exit code is 0 unless --fail-under is given and missed.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Optional, Tuple

WANTS = ("answer", "answer_about_artifact", "answer_about_upload", "create", "edit", "convert", "export",
         "chart", "diagram_in_chat", "diagram_in_file", "refuse_with_reason")
FILE_WANTS = ("create", "edit", "convert", "export")

OUTCOMES = ("made_a_file", "no_file", "wrong_action", "wrong_format", "plot_without_a_table", "prose_file",
            "refused_a_drawable", "refused_a_question", "no_chart", "wrong_source", "ungrounded_chat", "over_grounded",
            "read_back_missing", "raised", "left_to_the_model", "ok")
HARM = tuple(o for o in OUTCOMES if o not in ("left_to_the_model", "ok"))


# ------------------------------------------------------------- the module --

def find_orchestrator(explicit: Optional[str], corpus: Path) -> Path:
    cands: List[Path] = []
    if explicit:
        cands.append(Path(explicit).expanduser().resolve())
    if os.environ.get("UNDERSTANDING_EVAL_ORCH"):
        cands.append(Path(os.environ["UNDERSTANDING_EVAL_ORCH"]).expanduser().resolve())
    for start in (Path.cwd().resolve(), corpus.resolve().parent):
        p = start
        for _ in range(8):
            cands.append(p / "orchestrator")
            cands.append(p)
            p = p.parent
    for c in cands:
        if (c / "app" / "artifacts" / "intent.py").is_file():
            return c
    raise SystemExit("cannot find the orchestrator package. Pass --orchestrator "
                     "<worktree>/orchestrator (the directory holding app/artifacts/intent.py).")


def revision_of(orch: Path) -> str:
    """Which tree the module came from — printed because the upward search
    can find a DIFFERENT checkout than the one meant."""
    import subprocess
    try:
        rev = subprocess.run(["git", "-C", str(orch), "rev-parse", "--short=10", "HEAD"],
                             capture_output=True, text=True, timeout=10).stdout.strip() or "unknown"
        dirty = subprocess.run(["git", "-C", str(orch), "status", "--porcelain", "--", "app/artifacts"],
                               capture_output=True, text=True, timeout=20).stdout.strip()
        return rev + ("  (app/artifacts MODIFIED)" if dirty else "  (clean)")
    except Exception as exc:  # noqa: BLE001 — a missing git (inside the container) is not a scoring failure
        return f"unknown ({type(exc).__name__})"


def load_intent(orch: Path):
    sys.path.insert(0, str(orch))
    from app.artifacts import intent as I  # noqa: E402
    return I


def load_formats(orch: Path):
    """`formats.decide` — what the engine actually makes. `intent.formats`
    holds only the formats the RULES pinned; when it is empty the engine
    reads the instruction again (engines/artifact.py: `F.decide(instruction,
    explicit_only=intent.formats or None, chart_request=...)`), so grading
    `intent.formats` alone would call every 'make a presentation' a
    wrong_format. None when the tree has no such module."""
    try:
        from app.artifacts import formats as F  # noqa: E402
        return F
    except Exception:  # noqa: BLE001
        return None


# ----------------------------------------------------------------- context --

def kwargs_for(ctx_name: str, contexts: Dict[str, Any]) -> Dict[str, Any]:
    row = contexts[ctx_name]
    kw: Dict[str, Any] = {
        "has_artifacts": bool(row.get("has_artifacts")),
        "last_turn_is_artifact": bool(row.get("last_turn_is_artifact")),
        "has_assistant_answer": bool(row.get("has_assistant_answer")),
        "artifact_hints": tuple(row.get("artifact_hints") or ()),
    }
    if row.get("artifact_id"):
        kw["artifact_id"] = str(row["artifact_id"])
    if row.get("upload_formats"):
        kw["upload_formats"] = tuple(row["upload_formats"])
    if row.get("has_dataset"):
        kw["has_dataset"] = True
    if row.get("deliverable_has_chart"):
        try:
            from app.artifacts import deliverable as _D
            kw["last_deliverable"] = _D.Deliverable(kind="workbook", formats=("xlsx",), charts=1, chart_type="pie")
        except Exception:  # noqa: BLE001 — intent._shape_of accepts anything carrying `has_chart`
            kw["last_deliverable"] = SimpleNamespace(has_chart=True, kind="workbook")
    return kw


def text_of(item: Dict[str, Any]) -> str:
    r = item.get("text_build")
    if not r:
        return item.get("text", "")
    rows = "".join(str(r["row_template"]).format(i=i, owner=i % 7, day=1 + i % 28, pri=i % 3)
                   for i in range(int(r.get("rows", 0))))
    return str(r.get("prefix", "")) + str(r.get("header", "")) + rows + str(r.get("suffix", ""))


# ----------------------------------------------------------------- grading --

def observed(d: Any, F: Any = None) -> Dict[str, Any]:
    """The fields graded, read defensively so a tree that lacks one still scores."""
    action = str(getattr(d, "action", "") or "")
    formats = [str(f) for f in (getattr(d, "formats", []) or [])]
    effective = list(formats)
    if F is not None and action not in ("", "none", "<raised>"):
        try:
            effective = list(F.decide(str(getattr(d, "instruction", "") or ""), explicit_only=formats or None,
                                      chart_request=bool(getattr(d, "chart_request", False))).formats)
        except Exception as exc:  # noqa: BLE001 — a formats crash is reported, not hidden
            effective = formats + [f"<formats.decide raised {type(exc).__name__}>"]
    return {
        "effective_formats": effective,
        "action": action,
        "rule": str(getattr(d, "rule", "") or ""),
        "formats": formats,
        "chart_request": bool(getattr(d, "chart_request", False)),
        "unsupported_visual": str(getattr(d, "unsupported_visual", "") or ""),
        "answer_about_artifact": bool(getattr(d, "answer_about_artifact", False)),
        # None when the tree has no such field: then answer_about_artifact alone says "our file"
        "names_our_file": (bool(getattr(d, "names_our_file")) if hasattr(d, "names_our_file") else None),
        "target": str(getattr(d, "target", "") or ""),
        "language": str(getattr(d, "language", "") or ""),
        "wants_file": action not in ("", "none"),
    }


_IMAGE = {"png", "svg", "jpg", "jpeg", "image"}


def _formats_ok(want_formats: Iterable[str], o: Dict[str, Any]) -> bool:
    """The named format is among the files the engine would make (effective
    formats), or among the rules' own pins; an image format matches 'image'."""
    want = [f for f in want_formats if f]
    if not want:
        return True
    got = set(o.get("effective_formats") or []) | set(o.get("formats") or [])
    for f in want:
        if f in got or (f in _IMAGE and got & _IMAGE):
            return True
    return False


def grade_one(want: str, o: Dict[str, Any], item: Dict[str, Any]) -> str:
    file = o["wants_file"]
    chart = o["chart_request"]
    unsup = bool(o["unsupported_visual"])
    ans = o["answer_about_artifact"]
    ours = ans if o["names_our_file"] is None else (ans and o["names_our_file"])
    formats = item.get("formats") or []
    tags = item.get("tags") or []

    if want == "answer":
        if file:
            return "made_a_file"
        if ans:
            return "over_grounded"
        if unsup:
            return "refused_a_question"
        return "ok"
    if want == "answer_about_artifact":
        if file:
            return "made_a_file"
        if unsup:
            return "refused_a_question"      # "what does the map on page 2 show?" answered with "I cannot draw a map"
        return "ok" if ans else "ungrounded_chat"
    if want == "answer_about_upload":
        if file:
            return "made_a_file"
        if unsup:
            return "refused_a_question"
        if ours:
            return "wrong_source"
        return "ok"
    if want in FILE_WANTS:
        if o["action"] == want:
            return "ok" if _formats_ok(formats, o) else "wrong_format"
        return "wrong_action" if file else "no_file"
    if want == "chart":
        if unsup:
            return "refused_a_drawable"
        if not file:
            return "no_chart"
        if not chart:
            return "prose_file"
        return "ok" if _formats_ok(formats, o) else "wrong_format"
    if want == "diagram_in_chat":
        if unsup:
            return "refused_a_drawable"
        if ans:
            return "read_back_missing"
        if file and chart:
            return "plot_without_a_table"
        if file:
            return "prose_file"
        return "ok"
    if want == "diagram_in_file":
        if "edit" in tags or item.get("accept") == ["edit"]:
            return "ok" if o["action"] == "edit" else ("no_file" if not file else "wrong_action")
        if unsup:
            return "refused_a_drawable"
        if not file:
            return "no_file"
        return "ok" if _formats_ok(formats, o) else "wrong_format"
    if want == "refuse_with_reason":
        if unsup:
            return "ok"
        if file and chart:
            return "plot_without_a_table"
        if file:
            return "prose_file"
        return "left_to_the_model"
    raise SystemExit(f"unknown want {want!r}")


def grade(item: Dict[str, Any], o: Dict[str, Any]) -> Tuple[str, str]:
    """(outcome, the want that made it ok). The primary want is tried first,
    then each `accept`; the primary's outcome is reported when none is ok."""
    first = grade_one(item["want"], o, item)
    if first == "ok":
        return first, item["want"]
    for alt in item.get("accept") or []:
        if grade_one(alt, o, item) == "ok":
            return "ok", alt
    return first, ""


# --------------------------------------------------------------------- run --

def run(corpus_path: Path, orch: Path, *, only=(), category=(), lang=(), source=(), tag=(),
        exclude_debatable: bool = False) -> Dict[str, Any]:
    doc = json.loads(corpus_path.read_text(encoding="utf-8"))
    contexts = doc["contexts"]
    I = load_intent(orch)
    F = load_formats(orch)
    rows: List[Dict[str, Any]] = []
    for it in doc["items"]:
        if only and it["want"] not in only:
            continue
        if category and it["category"] not in category:
            continue
        if lang and it["lang"] not in lang:
            continue
        if source and it["source"] not in source:
            continue
        if tag and not (set(tag) & set(it.get("tags", []))):
            continue
        if exclude_debatable and "debatable" in it.get("tags", []):
            continue
        text = text_of(it)
        kw = kwargs_for(it["ctx"], contexts)
        consultable = None
        try:
            intent = I.decide(text, **kw)
            o = observed(intent, F)
            err = ""
            # Would the classifier even be asked? When not, the rules' verdict is
            # FINAL at runtime and no model can rescue a misread (picture-eval's
            # observation; `_should_consult` is intent.py's own gate).
            try:
                consultable = bool(I._should_consult(intent, text))
            except Exception:  # noqa: BLE001 — an older tree without the gate
                consultable = None
        except Exception as exc:  # a crash is a finding, not a stack trace
            err = f"{type(exc).__name__}: {exc}"
            o = observed(SimpleNamespace(action="<raised>", rule=err))
        outcome, ok_as = ("raised", "") if err else grade(it, o)
        rows.append({
            "id": it["id"], "want": it["want"], "accept": it.get("accept", []), "category": it["category"],
            "lang": it["lang"], "script": it.get("script", ""), "ctx": it["ctx"], "source": it["source"],
            "source_label": it.get("source_label", ""), "tags": it.get("tags", []),
            "debatable": "debatable" in it.get("tags", []), "consultable": consultable,
            "text": one_line(text, 200), "text_chars": len(text), "observed": o, "outcome": outcome, "ok_as": ok_as, "raised": err,
        })
    return {"corpus": str(corpus_path), "orchestrator": str(orch), "revision": revision_of(orch), "rows": rows}


# ------------------------------------------------------------------ report --

def one_line(text: str, width: int = 46) -> str:
    s = " ".join((text or "").split())
    if not s:
        return "<empty>"
    return s if len(s) <= width else s[: width - 1] + "…"


def _table(rows: List[Dict[str, Any]], key: str, order: Iterable[str], title: str) -> None:
    groups: Dict[str, List[Dict[str, Any]]] = collections.defaultdict(list)
    for r in rows:
        groups[r[key]].append(r)
    keys = [k for k in order if k in groups] + sorted(k for k in groups if k not in set(order))
    print(title)
    print(f"  {'':24s} {'n':>5s} {'ok':>5s} {'%':>5s}   misreads (worst first)")
    for k in keys:
        sub = groups[k]
        c = collections.Counter(r["outcome"] for r in sub)
        ok = c.get("ok", 0)
        worst = ", ".join(f"{o}={c[o]}" for o in OUTCOMES if o != "ok" and c.get(o))
        print(f"  {k:24s} {len(sub):>5d} {ok:>5d} {100.0*ok/len(sub):>4.0f}%   {worst or '-'}")
    print("")


def summarise(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    c = collections.Counter(r["outcome"] for r in rows)
    firm = [r for r in rows if not r["debatable"]]
    return {
        "total": len(rows), "ok": c.get("ok", 0),
        "harm": sum(c.get(o, 0) for o in HARM), "soft": c.get("left_to_the_model", 0),
        "firm_total": len(firm), "firm_ok": sum(1 for r in firm if r["outcome"] == "ok"),
        "debatable_total": len(rows) - len(firm),
        "debatable_ok": sum(1 for r in rows if r["debatable"] and r["outcome"] == "ok"),
        "outcomes": {o: c.get(o, 0) for o in OUTCOMES if c.get(o)},
    }


def report(res: Dict[str, Any], *, show: str, categories: Iterable[str], langs: Iterable[str]) -> Dict[str, Any]:
    rows = res["rows"]
    s = summarise(rows)
    print(f"corpus        {res['corpus']}")
    print(f"orchestrator  {res['orchestrator']}")
    print(f"revision      {res.get('revision', 'unknown')}")
    print(f"turns         {s['total']}")
    try:
        print(f"load          {os.getloadavg()}")
    except OSError:
        pass
    print("")
    _table(rows, "want", WANTS, "PER WANT   (ok = the decision matches what the person wanted)")
    _table(rows, "category", categories, "PER CATEGORY   (what the person was doing)")
    _table(rows, "lang", langs, "PER LANGUAGE")
    _table(rows, "source", [], "PER SOURCE")
    pct = 100.0 * s["ok"] / max(1, s["total"])
    firm_pct = 100.0 * s["firm_ok"] / max(1, s["firm_total"])
    print(f"TOTAL        {s['total']:>5d} {s['ok']:>5d} {pct:>4.0f}%   wrong_decision={s['harm']}  left_to_the_model={s['soft']}")
    print(f"  firm       {s['firm_total']:>5d} {s['firm_ok']:>5d} {firm_pct:>4.0f}%   (excluding `debatable`)")
    if s["debatable_total"]:
        print(f"  debatable  {s['debatable_total']:>5d} {s['debatable_ok']:>5d}")
    misses = [r for r in rows if r["outcome"] != "ok"]
    final = [r for r in misses if r.get("consultable") is False]
    if misses and any(r.get("consultable") is not None for r in rows):
        print(f"  of the {len(misses)} misses, {len(final)} are FINAL: intent._should_consult refuses to ask the classifier, "
              f"so no model can rescue them at runtime")
    print("")
    print("HARM  (these are what the owner sees)")
    for o in OUTCOMES:
        if o in ("ok",) or not s["outcomes"].get(o):
            continue
        print(f"  {o:20s} {s['outcomes'][o]:>5d}")
    print("")
    bad = [r for r in rows if r["outcome"] != "ok"]
    shown = rows if show == "all" else (bad if show == "harm" else [])
    if shown:
        print(f"{'EVERY TURN' if show == 'all' else 'MISREADS'}  ({len(shown)})")
        print(f"  {'id':<22s} {'ctx':<7s} {'want':<21s} {'got':<8s} {'file':<4s} {'outcome':<20s} {'rule':<28s} text")
        for r in sorted(shown, key=lambda r: (r["want"], r["source"], r["id"])):
            o = r["observed"]
            flags = ("D" if r["debatable"] else " ")
            print(f"  {r['id']:<22s} {r['ctx']:<7s} {r['want']:<21s} {o['action']:<8s} "
                  f"{('yes' if o['wants_file'] else 'no'):<4s} {r['outcome']:<20s} {o['rule'][:28]:<28s} {flags} {one_line(r['text'])}")
    else:
        print("no misreads")
    return s


def compare(now: Dict[str, Any], base_path: Path) -> None:
    base = json.loads(base_path.read_text(encoding="utf-8"))
    bo = {r["id"]: r for r in base["rows"]}
    print("")
    print(f"COMPARED WITH  {base_path}  (revision {base.get('revision', '?')})")
    bs, ns = summarise(base["rows"]), summarise(now["rows"])
    for k in ("ok", "harm", "soft"):
        print(f"  {k:16s} {bs[k]:>5d} -> {ns[k]:>5d}  {ns[k]-bs[k]:+d}")
    fixed = [r for r in now["rows"] if r["outcome"] == "ok" and bo.get(r["id"], {}).get("outcome") not in ("ok", None)]
    broke = [r for r in now["rows"] if r["outcome"] != "ok" and bo.get(r["id"], {}).get("outcome") == "ok"]
    for title, rs in (("FIXED", fixed), ("REGRESSED", broke)):
        print(f"  {title} ({len(rs)})")
        for r in sorted(rs, key=lambda r: r["id"]):
            was = bo.get(r["id"], {})
            wo = was.get("observed", {})
            print(f"    {r['id']:<22s} {was.get('outcome','?'):<20s} -> {r['outcome']:<20s} "
                  f"[{wo.get('action','?')}/{wo.get('rule','?')[:18]} -> {r['observed']['action']}/{r['observed']['rule'][:18]}] "
                  f"{one_line(r['text'], 40)}")


# --------------------------------------------------------------- self-test --

def self_test() -> int:
    N = SimpleNamespace

    def d(action, rule="r", **kw):
        return N(action=action, rule=rule, formats=kw.pop("formats", []), **kw)

    def it(want, accept=(), formats=(), tags=()):
        return {"want": want, "accept": list(accept), "formats": list(formats), "tags": list(tags)}

    checks = [
        # want, decision, expected outcome
        (it("answer"), d("none"), "ok"),
        (it("answer"), d("create"), "made_a_file"),
        (it("answer"), d("none", answer_about_artifact=True), "over_grounded"),
        (it("answer"), d("none", unsupported_visual="sankey"), "refused_a_question"),
        (it("answer", accept=["answer_about_artifact"]), d("none", answer_about_artifact=True), "ok"),
        (it("answer_about_artifact"), d("none", answer_about_artifact=True), "ok"),
        (it("answer_about_artifact"), d("none"), "ungrounded_chat"),
        (it("answer_about_artifact"), d("convert"), "made_a_file"),
        (it("answer_about_upload"), d("none"), "ok"),
        (it("answer_about_upload"), d("none", answer_about_artifact=True), "wrong_source"),
        (it("answer_about_upload"), d("none", answer_about_artifact=True, names_our_file=False), "ok"),
        (it("answer_about_upload"), d("none", answer_about_artifact=True, names_our_file=True), "wrong_source"),
        (it("answer_about_upload"), d("create"), "made_a_file"),
        (it("answer_about_upload"), d("none", unsupported_visual="map"), "refused_a_question"),
        (it("answer_about_artifact"), d("none", unsupported_visual="map"), "refused_a_question"),
        (it("create"), d("create"), "ok"),
        (it("create", formats=["pdf"]), d("create", formats=["docx"]), "wrong_format"),
        (it("create", formats=["pdf"]), d("create", formats=["docx", "pdf"]), "ok"),
        (it("create", formats=["png"]), d("create", formats=["image"]), "ok"),
        (it("create"), d("convert"), "wrong_action"),
        (it("create"), d("none"), "no_file"),
        (it("create", accept=["export", "convert"]), d("export"), "ok"),
        (it("edit"), d("edit"), "ok"),
        (it("convert", formats=["pdf"]), d("convert", formats=["pdf"]), "ok"),
        (it("export"), d("create"), "wrong_action"),
        (it("chart"), d("create", chart_request=True), "ok"),
        (it("chart"), d("create"), "prose_file"),
        (it("chart"), d("none"), "no_chart"),
        (it("chart"), d("none", unsupported_visual="map"), "refused_a_drawable"),
        (it("diagram_in_chat"), d("none"), "ok"),
        (it("diagram_in_chat"), d("create", chart_request=True), "plot_without_a_table"),
        (it("diagram_in_chat"), d("create"), "prose_file"),
        (it("diagram_in_chat"), d("none", unsupported_visual="venn"), "refused_a_drawable"),
        (it("diagram_in_chat"), d("none", answer_about_artifact=True), "read_back_missing"),
        (it("diagram_in_file", formats=["pdf"]), d("create", formats=["pdf"]), "ok"),
        (it("diagram_in_file", formats=["pdf"]), d("create", formats=["docx"]), "wrong_format"),
        (it("diagram_in_file", formats=["pdf"]), d("none"), "no_file"),
        (it("diagram_in_file", tags=["edit"]), d("edit"), "ok"),
        (it("diagram_in_file", tags=["edit"]), d("create"), "wrong_action"),
        (it("refuse_with_reason"), d("none", unsupported_visual="map"), "ok"),
        (it("refuse_with_reason"), d("none"), "left_to_the_model"),
        (it("refuse_with_reason"), d("create", chart_request=True), "plot_without_a_table"),
        (it("refuse_with_reason"), d("create"), "prose_file"),
    ]
    bad = 0
    for item, dec, expect in checks:
        got, _ = grade(item, observed(dec))
        if got != expect:
            bad += 1
            print(f"  FAIL want={item['want']} accept={item['accept']} action={dec.action} -> {got}, expected {expect}")
    txt = text_of({"text_build": {"prefix": "q?\n", "header": "A\tB\n", "row_template": "{i}\t{owner}\n", "rows": 3, "suffix": "END"}})
    if txt != "q?\nA\tB\n0\t0\n1\t1\n2\t2\nEND":
        bad += 1
        print(f"  FAIL text_of -> {txt!r}")
    if text_of({"text": "x"}) != "x":
        bad += 1
        print("  FAIL text_of literal")
    if observed(SimpleNamespace(action="none"))["names_our_file"] is not None:
        bad += 1
        print("  FAIL names_our_file must be None when the tree has no such field")
    print(f"self-test: {len(checks) + 3 - bad}/{len(checks) + 3} passed")
    return 1 if bad else 0


# -------------------------------------------------------------------- main --

def main(argv: Optional[List[str]] = None) -> int:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus", default=str(here / "understanding_corpus.json"))
    ap.add_argument("--orchestrator", default=None, help="<worktree>/orchestrator; also UNDERSTANDING_EVAL_ORCH; else searched upward")
    ap.add_argument("--only", action="append", choices=list(WANTS), default=[], help="one want (repeatable)")
    ap.add_argument("--category", action="append", default=[], help="one category (repeatable)")
    ap.add_argument("--lang", action="append", default=[], help="en | en-typo | hinglish | hi | gu | gujlish | other (repeatable)")
    ap.add_argument("--source", action="append", default=[], help="one source corpus (repeatable)")
    ap.add_argument("--tag", action="append", default=[], help="items carrying this tag (repeatable)")
    ap.add_argument("--exclude-debatable", action="store_true", help="leave `debatable` items out entirely")
    ap.add_argument("--show", default="harm", choices=("harm", "all", "none"), help="which rows to print")
    ap.add_argument("--show-all", action="store_true", help="same as --show all")
    ap.add_argument("--json", dest="json_out", default=None, help="write the full per-turn result here")
    ap.add_argument("--save-baseline", default=None, help="write the full per-turn result here AND call it the baseline")
    ap.add_argument("--compare", default=None, help="a baseline JSON to diff this run against")
    ap.add_argument("--fail-under", type=float, default=None, help="exit 1 when the FIRM ok%% (non-debatable) is below this")
    ap.add_argument("--self-test", action="store_true", help="check the grading rules themselves and exit")
    a = ap.parse_args(argv)
    if a.self_test:
        return self_test()

    corpus = Path(a.corpus).expanduser().resolve()
    doc = json.loads(corpus.read_text(encoding="utf-8"))
    orch = find_orchestrator(a.orchestrator, corpus)
    res = run(corpus, orch, only=tuple(a.only), category=tuple(a.category), lang=tuple(a.lang), source=tuple(a.source),
              tag=tuple(a.tag), exclude_debatable=a.exclude_debatable)
    s = report(res, show="all" if a.show_all else a.show, categories=list(doc.get("_categories", {})),
               langs=("en", "en-typo", "hinglish", "hi", "gu", "gujlish", "other"))
    for path in (a.json_out, a.save_baseline):
        if path:
            Path(path).expanduser().write_text(json.dumps(res, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
            print(f"\nwrote {path}")
    if a.compare:
        compare(res, Path(a.compare).expanduser().resolve())
    if a.fail_under is not None and 100.0 * s["firm_ok"] / max(1, s["firm_total"]) < a.fail_under:
        print(f"\nFAIL: firm ok {100.0 * s['firm_ok'] / max(1, s['firm_total']):.1f}% < {a.fail_under}%")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
