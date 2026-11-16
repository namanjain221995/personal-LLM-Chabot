"""``python -m sf_assistant.evaluation timeline``: where the seconds of an answer went (latency logging, 2026-10-29).

It reads the page log (``.private/web_log.jsonl``, the default) or an ``ask --file`` sidecar (``.private/runs/*.jsonl``;
an evaluation run keeps its sidecars in its folder) and prints, for each answer it selects, the waterfall of its
timeline (:mod:`sf_assistant.agent.timeline`): every stage, model call, tool call and check on one time axis, nested
under the stage it ran in, then a footer (the long pole, prompt growth per round, the rounds nudges cost, repeated
tool calls, a wasted merge, code outside every entry). ``--stats`` prints medians per stage, model-call stage and tool
over the selected answers instead.

    python -m sf_assistant.evaluation timeline                        # the last answer of the page log
    python -m sf_assistant.evaluation timeline --ticket 7             # page ticket 7 (tickets restart at each start)
    python -m sf_assistant.evaluation timeline --at 2026-10-29T09:14  # the answers that started in that UTC minute
    python -m sf_assistant.evaluation timeline --last 5
    python -m sf_assistant.evaluation timeline --slowest 10
    python -m sf_assistant.evaluation timeline --stats                # every answer of the log
    python -m sf_assistant.evaluation timeline .private/runs/x.jsonl --slowest 3

The page seconds of an answer are ``reopen`` (closing and opening the records after the read copy was replaced) +
``retry`` (a swap retry) + the answer's own seconds + ``failed reopen`` (a reopen after a swap error that failed) + the
hand-off back to the page; the time waited in line comes before them. Model calls show tokens in and out; ``+N`` after the tokens in is the net prompt growth since the
previous call of the same agent, shown only between calls that offer the same tools. A tool's ``~N tok`` is its
characters / 3.4. The first route and names calls after a reopen also build the session's model client.

Only numbers, stage and tool names, fixed words, UTC times and ticket numbers are printed: never a question, an answer,
SQL, parameters or a name, so the output can be pasted. Answers logged before 2026-10-29 have no timeline; their
stage totals are shown. Reads JSONL only; no model call, no database.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = ["Ask", "load", "select", "waterfall", "stats", "add_arguments", "run"]

#: Stages that run inside another one (the reader nests them; triage nests in records when records holds it).
SUB_STAGES = {"people_spots": "triage", "route": "triage", "names": "triage", "value_spots": "triage",
              "hints": "records", "post": "records"}
#: The labels of the tool-calling agents (their model calls get prompt growth, their tool calls the SQL join).
AGENTS = ("records", "lean", "knowledge")
CHARS_PER_TOKEN = 3.4
CUT_CHARS = 16_000
EPS = 0.002   # entries are rounded to milliseconds


@dataclass
class Ask:
    """One answer of a log: ``label`` (``t7`` page ticket, ``n3`` sidecar row), ``when`` (UTC), page seconds and
    time in line (page lines only), the reopen and retry seconds, and ``jl`` (the JSONL record)."""

    label: str
    when: str
    status: str
    mode: str | None
    page_s: float | None
    queue_s: float | None
    reopen: dict | None
    retry_s: float | None
    jl: dict
    failed_reopen: dict | None = None

    @property
    def seconds(self) -> float:
        for x in (self.page_s, self.jl.get("seconds")):
            if isinstance(x, (int, float)):
                return float(x)
        return 0.0

    @property
    def entries(self) -> list[dict]:
        tl = self.jl.get("timeline") if isinstance(self.jl.get("timeline"), dict) else {}
        return [e for e in tl.get("entries") or [] if isinstance(e, dict) and isinstance(e.get("at"), (int, float))
                and isinstance(e.get("s"), (int, float))]


def _utc(ts: Any) -> str:
    """A page line's ``ts`` or a timeline's ``t0_utc`` as ``YYYY-MM-DDTHH:MM:SSZ`` (UTC); '' when unreadable."""
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", None):
        try:
            t = datetime.strptime(str(ts), fmt) if fmt else datetime.fromisoformat(str(ts))
            return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if t.tzinfo else ""
        except (TypeError, ValueError):
            continue
    return ""


def _ask(label: str, line: dict, jl: Any, status: str, page_s: Any, queue_s: Any, detail: dict) -> Ask:
    jl = jl if isinstance(jl, dict) else {}
    tl = jl.get("timeline") if isinstance(jl.get("timeline"), dict) else {}
    num = lambda x: float(x) if isinstance(x, (int, float)) else None   # noqa: E731
    reopen = detail.get("reopen") if isinstance(detail.get("reopen"), dict) else None
    failed = detail.get("failed_reopen") if isinstance(detail.get("failed_reopen"), dict) else None
    return Ask(label=label, when=_utc(tl.get("t0_utc")) or _utc(line.get("ts")), status=status,
               mode=line.get("mode") if isinstance(line.get("mode"), str) else None, page_s=num(page_s),
               queue_s=num(queue_s), reopen=reopen, retry_s=num(detail.get("retry_s")), jl=jl, failed_reopen=failed)


def load(path: str | Path) -> list[Ask]:
    """The answers of a page log (``ask`` lines with a result; a ``timeout`` or ``abandoned`` line joined to its
    ``late_finish`` by ticket; a late finish that failed shows its exception class only) or of a sidecar (one JSONL
    record per line), in file order. Unreadable lines (a line cut mid-write included) are skipped."""
    out: list[Ask] = []
    timeouts: dict[Any, dict] = {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            event = d.get("event")
            detail = d.get("detail") if isinstance(d.get("detail"), dict) else {}
            if event is None and ("answer_kind" in d or "timeline" in d):   # an ask --file sidecar row
                out.append(_ask(f"n{d.get('n', len(out) + 1)}", d, d, "ok", None, None, {}))
            elif event == "ask" and d.get("status") == "ok":
                out.append(_ask(f"t{d.get('ticket')}", d, detail.get("jsonl"), "ok", d.get("seconds"),
                                d.get("queue_wait_s"), detail))
            elif event == "ask" and d.get("status") in ("timeout", "abandoned"):
                timeouts[d.get("ticket")] = d
            elif event == "late_finish":
                t = timeouts.pop(d.get("ticket"), {})
                status = f"{t.get('status') or 'timeout'}, finished late"
                if d.get("error"):   # the exception class only: its message can echo data
                    status += f", failed ({str(d['error']).split(':', 1)[0].strip()[:60]})"
                out.append(_ask(f"t{d.get('ticket')}", {**t, **d}, detail.get("jsonl"), status,
                                d.get("seconds"), t.get("queue_wait_s"), detail))
    return out


def select(asks: list[Ask], ticket: int | None = None, at: str | None = None, last: int | None = None,
           slowest: int | None = None) -> list[Ask]:
    """The answers of one selector (the last answer without any)."""
    if ticket is not None:
        return [a for a in asks if a.label == f"t{ticket}"]
    if at:
        prefix = at.strip().replace(" ", "T").rstrip("Z")
        return [a for a in asks if a.when.startswith(prefix)]
    if slowest:
        return sorted(asks, key=lambda a: -a.seconds)[:slowest]
    return asks[-(last or 1):] if asks else []


# --------------------------------------------------------------------------------------------- the waterfall
def _inside(outer: dict, e: dict) -> bool:
    return outer["at"] <= e["at"] + EPS and e["at"] + e["s"] <= outer["at"] + outer["s"] + EPS


def _tree(entries: list[dict]) -> dict[int, list[int]]:
    """Children of every entry (index; -1 is the answer): a stage nests in its parent stage (:data:`SUB_STAGES`;
    triage in a records stage that holds it), a model, tool or check entry in the stage it ran in; else at the top."""
    kids: dict[int, list[int]] = {i: [] for i in range(-1, len(entries))}
    stages = [i for i, e in enumerate(entries) if e.get("kind") == "stage"]
    for i, e in enumerate(entries):
        if e.get("kind") == "stage":
            want = SUB_STAGES.get(e.get("stage")) or ("records" if e.get("stage") == "triage" else None)
        else:
            want = e.get("stage")
        holders = [j for j in stages if j != i and entries[j].get("stage") == want and _inside(entries[j], e)]
        kids[min(holders, key=lambda j: entries[j]["s"]) if holders else -1].append(i)
    return kids


def _union(spans: list[tuple[float, float]]) -> float:
    total, end = 0.0, -math.inf
    for a, b in sorted(spans):
        if b > end:
            total += b - max(a, end)
            end = b
    return total


def _growth(entries: list[dict]) -> dict[int, int]:
    """Net prompt growth of each agent model call over the previous call of the same agent with the same tools offered
    (tokens in - the previous tokens in and out)."""
    out: dict[int, int] = {}
    prev: dict[str, int] = {}
    for i in sorted(range(len(entries)), key=lambda k: entries[k]["at"]):
        e = entries[i]
        if e.get("kind") != "model" or e.get("stage") not in AGENTS or not isinstance(e.get("tok_in"), int):
            continue
        p = prev.get(e["stage"])
        if p is not None and entries[p].get("offered") == e.get("offered"):
            out[i] = e["tok_in"] - entries[p]["tok_in"] - int(entries[p].get("tok_out") or 0)
        prev[e["stage"]] = i
    return out


def _sql(entries: list[dict], queries: list) -> dict[int, Any]:
    """The SQL seconds of each record tool entry: the n-th records tool entry that ran is query qn (a query that code
    ran after the record agent comes after them)."""
    out: dict[int, Any] = {}
    ran = [i for i in sorted(range(len(entries)), key=lambda k: entries[k]["at"])
           if entries[i].get("kind") == "tool" and entries[i].get("stage") == "records"
           and entries[i].get("error") != "not_run"]
    for n, i in enumerate(ran, 1):
        q = queries[n - 1] if n <= len(queries) and isinstance(queries[n - 1], dict) else {}
        if q.get("query_id") == f"q{n}" and isinstance(q.get("sql_seconds"), (int, float)):
            out[i] = q["sql_seconds"]
    return out


def _text(e: dict, growth: int | None, sql: Any) -> str:
    kind = e.get("kind")
    if kind == "stage":
        return f"{e.get('stage')}" + (f" ({e['chars']} chars)" if isinstance(e.get("chars"), int) else "")
    if kind == "model":
        parts = [f"model r{e.get('round')} after {e.get('after')}"]
        tok = ""
        if isinstance(e.get("tok_in"), int):
            tok = f"{e['tok_in']} in" + (f" ({growth:+d})" if growth is not None else "")
            out = int(e.get("tok_out") or 0)
            rate = f" ({out / e['s']:.0f} tok/s)" if e["s"] > 0 and out else ""
            tok += f", {out} out{rate}, {e.get('finish') or '?'}"
        if e.get("offered") == 0 and e.get("stage") in AGENTS:
            tok += ", no tools offered"
        if int(e.get("tries") or 1) > 1 or e.get("failed"):
            fails = "; ".join(f"{f.get('error')}{' ' + str(f.get('status')) if f.get('status') else ''} after "
                              f"{f.get('after_s')} s" for f in e.get("failed") or [] if isinstance(f, dict))
            tok += f", tries {e.get('tries')} ({fails})"
        if e.get("error"):
            tok += f", FAILED {e['error']}"
        return parts[0] + (": " + tok.lstrip(", ") if tok else "")
    if kind == "tool":
        chars = int(e.get("chars") or 0)
        s = f"tool {e.get('name')}: {chars} chars (~{chars / CHARS_PER_TOKEN:.0f} tok)"
        s += ", CUT at 16,000" if e.get("cut") else ""
        s += f", {e['error'].replace('_', ' ')}" if e.get("error") else ""
        s += f", sql {sql:.2f}" if isinstance(sql, (int, float)) else ""
        if e.get("again_of"):
            s += f", again of tool {e['again_of']} ({'same' if e.get('same') else 'narrowed'})"
        return s
    if kind == "check":
        then = "forced answer" if e.get("name") == "step_cap" else "nudge" if e.get("nudge") else "ok"
        return f"check {e.get('name')} -> {then}"
    return str(kind)


def _totals(jl: dict) -> str:
    keys = (("route", "route_seconds"), ("knowledge", "knowledge_seconds"), ("records", "record_seconds"),
            ("merge", "merge_seconds"), ("answer", "seconds"))
    return ", ".join(f"{k} {jl[v]}" for k, v in keys if isinstance(jl.get(v), (int, float)))


def _header(a: Ask) -> list[str]:
    head = f"{a.label}  {a.when or '?'}  {a.status}" + (f"  mode {a.mode}" if a.mode else "") + \
        (f"  knowledge {a.jl['knowledge_mode']}" if a.jl.get("knowledge_mode") else "")
    answer = a.jl.get("seconds")
    if a.page_s is None:
        return [head, f"answer {answer} s"]
    reopen = a.reopen or {}
    parts = [f"page {a.page_s:.2f} s ="]
    known = 0.0
    if isinstance(reopen.get("s"), (int, float)):
        parts.append(f"reopen {reopen['s']:.2f} (close {reopen.get('close_s')}, open {reopen.get('open_s')}) +")
        known += reopen["s"]
    if a.retry_s is not None:
        parts.append(f"retry {a.retry_s:.2f} +")
        known += a.retry_s
    failed = a.failed_reopen or {}
    if isinstance(answer, (int, float)):
        tail = f"answer {answer:.2f} +"
        if isinstance(failed.get("s"), (int, float)):
            tail += f" failed reopen {failed['s']:.2f} (close {failed.get('close_s')}, open {failed.get('open_s')}) +"
            known += failed["s"]
        parts.append(f"{tail} hand-off {a.page_s - known - answer:.2f}")
    else:
        parts.append("answer ?")
    waited = f"waited {a.queue_s:.2f} s; " if a.queue_s is not None else ""
    return [head, waited + " ".join(parts)]


def waterfall(a: Ask) -> list[str]:
    """The lines printed for one answer."""
    lines = _header(a)
    entries = a.entries
    if not entries:
        return lines + ["  no timeline in this line (answers before 2026-10-29 have none); stage totals: "
                        + (_totals(a.jl) or "none")]
    kids, growth = _tree(entries), _growth(entries)
    queries = a.jl.get("queries") if isinstance(a.jl.get("queries"), list) else []
    sql = _sql(entries, queries)
    lines.append(f"{'at':>8} {'s':>8}  task")
    other: list[str] = []

    def show(i: int, depth: int) -> None:
        e = entries[i]
        lines.append(f"{e['at']:8.3f} {e['s']:8.3f}  {'  ' * depth}{_text(e, growth.get(i), sql.get(i))}")
        children = sorted(kids.get(i) or [], key=lambda k: entries[k]["at"])
        if children and e.get("kind") == "stage":
            rest = e["s"] - _union([(entries[k]["at"], entries[k]["at"] + entries[k]["s"]) for k in children])
            if rest >= 0.01:
                other.append(f"{e.get('stage')} {rest:.2f}")
        for k in children:
            show(k, depth + 1)

    top = sorted(kids[-1], key=lambda k: entries[k]["at"])
    for i in top:
        show(i, 0)
    end = max(e["at"] + e["s"] for e in entries)
    lines += _footer(a, entries, top, growth, end, other)
    return lines


def _footer(a: Ask, entries: list[dict], top: list[int], growth: dict[int, int], end: float,
            other: list[str]) -> list[str]:
    out: list[str] = []
    by = {e.get("stage"): e for e in (entries[i] for i in top) if e.get("kind") == "stage"}
    rec, kno = by.get("records"), by.get("lean") or by.get("knowledge")
    if rec and kno:
        long, short = (rec, kno) if rec["s"] >= kno["s"] else (kno, rec)
        wait = by.get("wait_records")
        out.append(f"long pole: {long['stage']} {long['s']:.2f} s ({short['stage']} ended "
                   f"{(long['at'] + long['s']) - (short['at'] + short['s']):.2f} s earlier); main thread waited "
                   f"{wait['s'] if wait else 0:.2f} s for the records")
    rounds = [f"{entries[i]['stage']} r{entries[i].get('round')} {g:+d}" for i, g in sorted(
        growth.items(), key=lambda kv: entries[kv[0]]["at"])]
    if rounds:
        out.append("prompt growth per round (same tools offered): " + ", ".join(rounds))
    order = sorted(range(len(entries)), key=lambda k: entries[k]["at"])
    nudged = []
    for n, i in enumerate(order):
        e = entries[i]
        if e.get("kind") == "check" and e.get("nudge"):
            nxt = next((entries[k] for k in order[n + 1:] if entries[k].get("kind") == "model"
                        and entries[k].get("stage") == e.get("stage")), None)
            nudged.append((e.get("name"), nxt["s"] if nxt else 0.0))
    if nudged:
        out.append(f"rounds after a check: {len(nudged)} ({sum(s for _n, s in nudged):.2f} s): "
                   + ", ".join(f"{n} {s:.2f} s" for n, s in nudged))
    tools = [e for e in entries if e.get("kind") == "tool"]
    if tools:
        out.append(f"tool calls: {len(tools)} ({sum(e.get('error') == 'failed' for e in tools)} failed, "
                   f"{sum(e.get('error') == 'not_run' for e in tools)} not run, {sum(bool(e.get('cut')) for e in tools)} "
                   f"cut); repeated {sum(bool(e.get('again_of')) and bool(e.get('same')) for e in tools)}, narrowed "
                   f"{sum(bool(e.get('again_of')) and not e.get('same') for e in tools)}")
    calls = [e for e in entries if e.get("kind") == "model"]
    cut = [f"{e.get('stage')} r{e.get('round')}" for e in calls if e.get("finish") == "length"]
    if cut:
        out.append("cut at max tokens: " + ", ".join(cut))
    merge = [e for e in calls if e.get("stage") == "merge"]
    if merge and (a.jl.get("merge_error") or a.jl.get("rejected_numbers")):
        out.append(f"wasted merge: {sum(e['s'] for e in merge):.2f} s, {sum(int(e.get('tok_out') or 0) for e in merge)} "
                   f"tokens out (the merged answer was not used)")
    covered = _union([(entries[i]["at"], entries[i]["at"] + entries[i]["s"]) for i in top])
    tail = [f"top {end - covered:.2f}"] if end - covered >= 0.01 else []
    answer = a.jl.get("seconds")
    render = f"; render {answer - end:.2f} (answer seconds - timeline end)" if isinstance(answer, (int, float)) else ""
    out.append("other code: " + (", ".join(other + tail) or "none") + render)
    if a.jl.get("timeline", {}).get("dropped"):
        out.append(f"entries dropped: {a.jl['timeline']['dropped']}")
    return out


# --------------------------------------------------------------------------------------------- --stats
def _row(name: str, xs: list[float], total_s: float) -> str:
    xs = sorted(xs)
    p90 = xs[min(len(xs) - 1, max(0, math.ceil(0.9 * len(xs)) - 1))]
    share = f"{100 * sum(xs) / total_s:5.1f}%" if total_s else "    -"
    return f"{name:22} {len(xs):5} {statistics.median(xs):8.2f} {p90:8.2f} {xs[-1]:8.2f} {sum(xs):9.2f} {share:>6}"


def stats(asks: list[Ask]) -> list[str]:
    """Per stage, model-call stage and tool: n, median, p90, max, total and share of the answers' seconds."""
    timed = [a for a in asks if a.entries]
    total = sum(a.seconds for a in timed)
    lines = [f"answers: {len(asks)}, {len(timed)} with a timeline; their seconds: {total:.2f} in all"
             + (f", median {statistics.median(a.seconds for a in timed):.2f}" if timed else "")]
    if not timed:
        return lines
    groups: dict[str, dict[str, list[float]]] = {"stage": {}, "model calls by stage": {}, "tool calls": {}}
    length = retried = 0
    for a in timed:
        for e in a.entries:
            kind = e.get("kind")
            if kind == "stage":
                groups["stage"].setdefault(str(e.get("stage")), []).append(e["s"])
            elif kind == "model":
                groups["model calls by stage"].setdefault(str(e.get("stage")), []).append(e["s"])
                length += e.get("finish") == "length"
                retried += int(e.get("tries") or 1) > 1
            elif kind == "tool":
                groups["tool calls"].setdefault(str(e.get("name")), []).append(e["s"])
    head = f"{'':22} {'n':>5} {'median':>8} {'p90':>8} {'max':>8} {'total':>9} {'share':>6}"
    for title, rows in groups.items():
        if rows:
            lines += ["", f"{title}:", head]
            lines += [_row(k, v, total) for k, v in sorted(rows.items(), key=lambda kv: -sum(kv[1]))]
    lines += ["", f"model calls cut at max tokens: {length}; model calls with more than one try: {retried}",
              "share: total / the answers' seconds (stages that run side by side add up to more than 100%)"]
    return lines


# --------------------------------------------------------------------------------------------- command line
def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("log", nargs="?", help="the page log or an ask --file sidecar (default: .private/web_log.jsonl)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--ticket", type=int, help="page ticket N (tickets restart at each page start)")
    g.add_argument("--at", metavar="UTC", help="answers that started in this UTC minute (or any prefix), "
                                               "e.g. 2026-10-29T09:14")
    g.add_argument("--last", type=int, metavar="N", help="the last N answers")
    g.add_argument("--slowest", type=int, metavar="N", help="the N slowest answers")
    p.add_argument("--stats", action="store_true", help="medians per stage, model-call stage and tool over the "
                                                         "selected answers (every answer without a selector)")


def run(a: argparse.Namespace) -> int:
    if a.log:
        path = Path(a.log)
    else:
        from sf_assistant.config import PRIVATE_DIR

        path = Path(PRIVATE_DIR) / "web_log.jsonl"
    try:
        asks = load(path)
    except OSError as exc:
        print(f"timeline: cannot read {path}: {exc.strerror or exc}")
        return 2
    if a.stats:
        chosen = select(asks, a.ticket, a.at, a.last, a.slowest) if any(
            x is not None for x in (a.ticket, a.at, a.last, a.slowest)) else asks
        print("\n".join(stats(chosen)))
        return 0
    chosen = select(asks, a.ticket, a.at, a.last, a.slowest)
    if not chosen:
        print("timeline: no answer matches")
        return 1
    print("\n\n".join("\n".join(waterfall(x)) for x in chosen))
    return 0
