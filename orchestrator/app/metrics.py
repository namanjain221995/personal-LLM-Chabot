"""Prometheus metrics for the living knowledge layer — stdlib only.

No prometheus_client dependency, for the same reason monitoring/exporters/
dgx-gpu is stdlib-only: this process already carries torch-adjacent weight and
a metrics library is a large surface for a few dozen counters. The text
exposition format is a handful of lines to render correctly.

CARDINALITY IS THE ONLY REAL RISK. A URL, a query or a user id as a label
would produce unbounded series and eventually take Prometheus down with it, so
every label here is drawn from a small closed set (freshness level, rule name,
hit/miss) and the module refuses anything it does not recognise.

Every function is called from request paths and must never raise.
"""
from __future__ import annotations

import threading
from typing import Dict, List, Tuple

_lock = threading.Lock()

#: name -> {label-tuple: value}
_counters: Dict[str, Dict[Tuple[Tuple[str, str], ...], float]] = {}
_gauges: Dict[str, Dict[Tuple[Tuple[str, str], ...], float]] = {}
#: name -> {label-tuple: (bucket counts, sum, count)}
_hists: Dict[str, Dict[Tuple[Tuple[str, str], ...], Tuple[List[int], float, int]]] = {}

_HELP: Dict[str, str] = {}
_TYPE: Dict[str, str] = {}

#: Seconds. Tuned for retrieval and small fetches, not for model generation.
_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)

#: Seconds, for the waits of the availability programme (docs/availability/
#: CONTRACT.md §7.2): a generation queued for a recovering engine waits
#: minutes, not milliseconds — a TP=2 reload measured 3 m 32 s warm and
#: 5 m 20 s cold, the queue window is 900 s — and on the default buckets every
#: such wait landed in +Inf, which is no histogram at all (review manifest
#: §13.4). Ends at the queue window plus the long recovery window.
_WAIT_BUCKETS = (1.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0, 900.0, 1200.0, 1800.0)

#: Seconds, for what a person waits before the answer starts. The default set
#: jumps from 1.0 to 2.5, and the owner's goal (2026-09-13) is "answers start
#: within 1-2 seconds": measured Fast/chat is p50 1.46 s / p95 6.16 s with
#: 78.4% of turns at or under 2 s (usage_events, n=431, 7 days), so almost
#: every turn that matters landed in the one bucket that cannot say whether it
#: met the goal. 1.5 and 2.0 are inserted; every default edge is kept, so the
#: existing le= series of chat_ttft_seconds carry on unchanged and only the
#: two new series start at the deploy (compare windows either side of it for
#: those, not across it).
_TTFT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 1.5, 2.0, 2.5, 5.0, 10.0, 30.0)

#: Per-metric bucket sets. A global change would break histogram_quantile
#: continuity for every retrieval histogram on the default set; an entry here
#: moves only the metric named.
_BUCKETS_BY_METRIC = {
    # Histograms whose observations are engine waits use the wide buckets.
    "llm_engine_wait_seconds": _WAIT_BUCKETS,
    "llm_queue_wait_seconds": _WAIT_BUCKETS,
    "llm_admission_wait_seconds": _WAIT_BUCKETS,
    # The 1-2 s goal, read directly (performance plan item 1a, 2026-09-13).
    "chat_ttft_seconds": _TTFT_BUCKETS,
    "chat_first_visible_seconds": _TTFT_BUCKETS,
    # The knowledge pre-pass is the largest share of that wait (web_memory
    # retrieve p50 0.72 s / p90 2.09 s), so its whole-prepare time is read on
    # the same edges as the TTFT it is being subtracted from.
    "knowledge_prepare_seconds": _TTFT_BUCKETS,
}


def _buckets_for(name: str) -> Tuple[float, ...]:
    return _BUCKETS_BY_METRIC.get(name, _BUCKETS)

#: Closed label vocabularies. Anything else becomes "other" rather than a new
#: series — a typo in a call site must not be able to grow the index.
_ALLOWED = {
    "level": {"static", "recent", "realtime"},
    "rule": {
        "lexical:realtime", "lexical:office", "lexical:recent", "lexical:static",
        "router", "default", "empty",
        # freshness.TIMELESS_TASK_REASON (2026-09-13): a Fast question settled
        # STATIC without the router because it is a timeless task with no
        # live-value signal. Its own rule so a wrong skip is countable; the
        # first version's "no_time_signal" did not survive review (it
        # answered "euro to dollar" from weights) and is not listed.
        "timeless_task",
    },
    # `result` is shared by every counter that reports an outcome, so it is
    # the union of their vocabularies. V29 (2026-09-10) added the upload
    # session's terminal states and the chat request's dispositions; both are
    # closed sets, so a typo folds to "other" instead of minting a series.
    "result": {
        "hit", "miss", "fresh", "stale", "ok", "fail",
        # upload_session_total
        "complete", "rejected", "cancelled", "expired",
        # chat_request_total
        "accepted", "attached", "replayed", "resumed", "conflict",
        # video_jobs_total / video_stage_total / artifact_jobs_total: a job
        # put back in the queue because its engine was down for the whole
        # recovery window. video/pipeline.py has reported it since
        # 2026-09-11 and it folded to "other" until the artifact pipeline
        # (V31) needed the same word.
        "deferred",
    },
    # What an upload is FOR. Three values, fixed by the upload rail.
    "purpose": {"dataset", "document", "video"},
    # `state` is the OPEN half of a lifecycle — what something is doing right
    # now, as opposed to `result`, which is how it ended. Added 2026-09-11 for
    # the gauges health._publish_work_gauges sets (video_queue_depth,
    # upload_sessions_open). Both vocabularies are the database's own CHECK
    # constraints (video_analyses.status, upload_sessions.status) narrowed to
    # the states that are still in flight, so the set cannot drift without a
    # migration; anything else folds to "other" rather than minting a series.
    "state": {"queued", "running", "uploading", "finalizing"},
    "job": {"index", "refresh", "expand"},
    # The circuit breaker in front of the main model engine (app/breaker.py,
    # docs/availability/CONTRACT.md §7.2): llm_breaker_state{engine},
    # llm_breaker_transitions_total{engine,to} and
    # llm_breaker_failures_total{engine,reason}, plus the durability
    # ledger's chat_request_attempts_total{engine}. ONE engine exists in
    # strict one-model mode (CONTRACT v2 §1: nothing stands in for the
    # TP=2 model) and three breaker states; a base URL or a state string
    # that is neither folds to "other" rather than minting a series.
    # `reason` and `outcome` are shared with other counters' free
    # vocabularies, so the availability metrics bound them PER METRIC in
    # _ALLOWED_BY_METRIC below.
    "engine": {"main"},
    "to": {"CLOSED", "OPEN", "HALF_OPEN"},
    # Long-context admission (app/admission.py, CONTRACT §6.7): the two
    # lanes of llm_admission_lane_active / _waiting / _wait_seconds.
    "lane": {"normal", "long"},
    # Video understanding pipeline stages (app/video/types.STAGES). Closed so
    # a renamed stage folds to "other" rather than minting a series.
    "stage": {
        "probe", "audio", "transcript", "frames", "ocr", "vision", "fusion",
        "index", "artifacts",
        # Artifact Studio stages (app/artifacts/types.STAGES) plus the
        # runner's publish step, for artifact_stage_seconds and
        # artifact_corrections_total.
        "intent", "gather", "outline", "compose", "render", "validate",
        "visual",  # the Max-effort visual correction pass
        "preview", "publish",
    },
    # A file format the Artifact Studio writes (app/artifacts/types.FORMATS):
    # artifact_render_seconds{format} and artifact_download_total{format}.
    # Five values, fixed by the renderer set (csv since 2026-09-12,
    # CONTRACT-2 §1), plus "zip" for the download metric only: the bundle
    # route `GET …/zip` is downloaded and counted, but never rendered.
    "format": {"pdf", "docx", "pptx", "xlsx", "csv", "zip"},
    # Speech to text. The vocabulary is the ASR model's own published set
    # (app/asr.SUPPORTED_LANGUAGES) plus "unknown" for a clip whose language
    # was not identified. Closed for the usual reason: a mis-parsed engine
    # reply must not be able to mint a new series.
    #
    # WIDENED from Qwen3-ASR's thirty to Whisper's ninety-nine. The two lists
    # must move together — a language asr.py can report but this set does not
    # hold is folded to "other", so the metric would have quietly lied about
    # sixty-nine languages while transcription worked fine.
    # `test_voice_input.py` asserts they agree.
    "language": {
        "Afrikaans", "Albanian", "Amharic", "Arabic", "Armenian",
        "Assamese", "Azerbaijani", "Bashkir", "Basque", "Belarusian",
        "Bengali", "Bosnian", "Breton", "Bulgarian", "Cantonese",
        "Catalan", "Chinese", "Croatian", "Czech", "Danish", "Dutch",
        "English", "Estonian", "Faroese", "Finnish", "French",
        "Galician", "Georgian", "German", "Greek", "Gujarati",
        "Haitian Creole", "Hausa", "Hawaiian", "Hebrew", "Hindi",
        "Hungarian", "Icelandic", "Indonesian", "Italian", "Japanese",
        "Javanese", "Kannada", "Kazakh", "Khmer", "Korean", "Lao",
        "Latin", "Latvian", "Lingala", "Lithuanian", "Luxembourgish",
        "Macedonian", "Malagasy", "Malay", "Malayalam", "Maltese",
        "Maori", "Marathi", "Mongolian", "Myanmar", "Nepali",
        "Norwegian", "Nynorsk", "Occitan", "Pashto", "Persian",
        "Polish", "Portuguese", "Punjabi", "Romanian", "Russian",
        "Sanskrit", "Serbian", "Shona", "Sindhi", "Sinhala", "Slovak",
        "Slovenian", "Somali", "Spanish", "Sundanese", "Swahili",
        "Swedish", "Tagalog", "Tajik", "Tamil", "Tatar", "Telugu",
        "Thai", "Tibetan", "Turkish", "Turkmen", "Ukrainian", "Urdu",
        "Uzbek", "Vietnamese", "Welsh", "Yiddish", "Yoruba", "unknown"
    },
}


#: Closed vocabularies that hold for ONE metric only — the label name is
#: shared with counters whose values are free (`reason` on
#: knowledge_degraded_total, `outcome` on embed_requests_total), so bounding
#: it globally would fold theirs, and leaving it unbounded would let a typo
#: in an availability call site mint a series. Every value the availability
#: modules emit is listed here (CONTRACT §4 for the breaker's reasons, §7.2
#: for the rest); tests/test_breaker.py pins breaker.REASONS to this set.
_ALLOWED_BY_METRIC: Dict[str, Dict[str, set]] = {
    "llm_breaker_failures_total": {
        "reason": {
            "connection", "readiness", "request_timeout", "queue_timeout",
            "engine_dead", "worker_lost", "capacity", "malformed", "cancelled",
        },
    },
    "llm_retry_total": {"reason": {"connection", "engine_error"}},
    "llm_engine_unavailable_total": {"reason": {"connection", "engine_error", "breaker_open"}},
    "llm_engine_wait_seconds": {"outcome": {"recovered", "gave_up", "interrupted"}},
    "llm_resumed_generations_total": {"outcome": {"resumed", "expired", "duplicate_suppressed"}},
    "llm_admission_rejections_total": {"reason": {"capacity", "timeout"}},
    # The durability ledger's counter (main._attempt_record): the CONTRACT
    # §8.4 terminal vocabulary.
    "chat_request_attempts_total": {"terminal_state": {"completed", "interrupted", "failed", "cancelled"}},
    # The knowledge retrieval stages (web_memory.py: lexical, meta, rerank;
    # web_index.py: embed, dense_scan, servable). They were emitted into the
    # global `stage` vocabulary before it was narrowed to the video pipeline
    # in 2c51487 (2026-09-09); since then every one of them folded to "other"
    # (385 samples in 3 days, all stage="other"), so per-stage attribution of
    # the pre-pass has been blind. Bounded HERE rather than in _ALLOWED
    # ["stage"]: that set is pinned equal to the video and artifact pipeline
    # stages (test_video_understanding), and a knowledge stage has no business
    # being a valid video stage.
    "knowledge_stage_seconds": {
        "stage": {"lexical", "dense_scan", "embed", "meta", "rerank", "servable"},
    },
    # main.py's escalation counter reports the two ways a Think/Max turn
    # escalates; it folded to "other" for the same reason.
    "knowledge_escalation_total": {"stage": {"local_first", "search"}},
}


# ---------------------------------------------------------------------------
# Latency histograms of the 1-2 s programme (performance plan item 1c,
# 2026-09-13). Their label NAMES are closed as well as their values: a label
# key a call site passes that is not declared here is DROPPED, so a user id,
# an API key id or a conversation id can never become a series, even by
# accident (these are observed on every chat turn — the busiest path there is).
# ---------------------------------------------------------------------------

#: Every route the engines put in the final meta (app/engines/*, main.py's
#: "unknown" default). A new engine folds to "other" until it is listed.
CHAT_ROUTES = frozenset({
    "agent", "artifact", "chat", "clarify", "crawl", "dataset",
    "dataset_report", "deep_research", "document", "live_sf", "ocr", "rag",
    "repo", "report", "search", "sf_intel", "sql", "url", "video", "vision",
    "unknown",
})

#: ChatRequest.effort's Literal (main.py), legacy names included.
CHAT_EFFORTS = frozenset({"fast", "think", "max", "low", "medium", "high", "extra_high"})

#: What the person saw first: a reasoning token (Think/Max stream reasoning
#: before the answer — a trivial probe spent 2.47 s reasoning), a status line
#: that carries content, or an answer token.
FIRST_VISIBLE_KINDS = frozenset({"reasoning", "status", "answer"})

#: living_knowledge._decided's vocabulary, for the prepare histogram.
KNOWLEDGE_DECISIONS = frozenset({
    "static_model", "static_topical", "degraded_busy", "local",
    "stale_offline", "escalate_search", "fast_lookup", "fast_lookup_failed",
})

#: How a timed step ended. `deadline` is a budget that fired and the turn went
#: on without the step; `skipped` is a step not run at all (Fast never asks
#: the orchestrate router).
STEP_OUTCOMES = frozenset({"ok", "deadline", "skipped", "error"})

#: engines/orchestrate.Plan, flattened.
DECIDE_PLANS = frozenset({"none", "agent", "search", "agent_search"})

#: What living_knowledge._topical_precheck said about a Fast timeless question
#: before its retrieval finished: a page CAN pass the topical gate (hit), none
#: can (miss), or it could not tell (fail — no testable term, a DB error).
#: A high fail share means Fast is waiting the pre-change time on those turns.
TOPICAL_PRECHECK_RESULTS = frozenset({"hit", "miss", "fail"})

#: Why the in-conversation recall block was left out of a prompt (recall.py,
#: team RELAY, 2026-09-13): the embedding call behind it moved from
#: embed_texts (90 s batch timeout) to embed_query (1 s semaphore wait, 4 s
#: timeout), so under an embedding burst or a sidecar restart the block now
#: disappears instead of holding the turn. Counted so that prompt change is
#: visible, not silent. Call site contract:
#:     metrics.inc("recall_block_dropped_total", reason="embed_busy")
RECALL_DROP_REASONS = frozenset({"embed_busy", "embed_timeout", "embed_error"})

_ROUTE_EFFORT = {"route": set(CHAT_ROUTES), "effort": set(CHAT_EFFORTS)}

#: metric -> {label name: closed value set}. Only these label NAMES survive.
_LABELS_BY_METRIC: Dict[str, Dict[str, set]] = {
    "chat_first_visible_seconds": {**_ROUTE_EFFORT, "kind": set(FIRST_VISIBLE_KINDS)},
    "knowledge_prepare_seconds": {
        "effort": set(CHAT_EFFORTS),
        "decision": set(KNOWLEDGE_DECISIONS),
        "outcome": set(STEP_OUTCOMES),
    },
    # The route is not known yet while the context is assembled; `mode` is
    # ChatRequest.mode's Literal.
    "context_assembly_seconds": {
        "effort": set(CHAT_EFFORTS),
        "mode": {"salesforce", "assistant"},
    },
    "orchestrate_decide_seconds": {
        "effort": set(CHAT_EFFORTS),
        "plan": set(DECIDE_PLANS),
        "outcome": set(STEP_OUTCOMES),
    },
    # Engine first token to the SSE write that carries it — the part of the
    # 105 -> 88 tok/s relay loss that is time, not throughput.
    "relay_overhead_seconds": dict(_ROUTE_EFFORT),
    # Counters of the same programme, closed the same way (names AND values).
    "knowledge_topical_precheck_total": {"result": set(TOPICAL_PRECHECK_RESULTS)},
    "recall_block_dropped_total": {"reason": set(RECALL_DROP_REASONS)},
}
_ALLOWED_BY_METRIC.update(_LABELS_BY_METRIC)


def _clean(labels: Dict[str, str], name: str = "") -> Tuple[Tuple[str, str], ...]:
    out = []
    per_metric = _ALLOWED_BY_METRIC.get(name, {})
    closed_names = _LABELS_BY_METRIC.get(name)
    for key, value in sorted(labels.items()):
        if closed_names is not None and key not in closed_names:
            continue  # an undeclared label name on a closed metric is dropped
        allowed = per_metric.get(key, _ALLOWED.get(key))
        v = str(value)
        if allowed is not None and v not in allowed:
            v = "other"
        if v.startswith("year:"):  # freshness rule carries a year; bucket it
            v = "year"
        out.append((key, v))
    return tuple(out)


def _declare(name: str, kind: str, help_text: str) -> None:
    _HELP.setdefault(name, help_text)
    _TYPE.setdefault(name, kind)


def inc(name: str, help_text: str = "", **labels: str) -> None:
    try:
        _declare(name, "counter", help_text or name)
        key = _clean(labels, name)
        with _lock:
            _counters.setdefault(name, {})
            _counters[name][key] = _counters[name].get(key, 0.0) + 1.0
    except Exception:  # noqa: BLE001 — a metric must never break a request
        pass


def set_gauge(name: str, value: float, help_text: str = "", **labels: str) -> None:
    try:
        _declare(name, "gauge", help_text or name)
        key = _clean(labels, name)
        with _lock:
            _gauges.setdefault(name, {})
            _gauges[name][key] = float(value)
    except Exception:  # noqa: BLE001
        pass


def observe(name: str, seconds: float, help_text: str = "", **labels: str) -> None:
    try:
        _declare(name, "histogram", help_text or name)
        key = _clean(labels, name)
        buckets = _buckets_for(name)
        with _lock:
            _hists.setdefault(name, {})
            counts, total, n = _hists[name].get(key, ([0] * len(buckets), 0.0, 0))
            counts = list(counts)
            for i, edge in enumerate(buckets):
                if seconds <= edge:
                    counts[i] += 1
            _hists[name][key] = (counts, total + float(seconds), n + 1)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# The living-knowledge call sites, named once so spelling cannot drift.
# ---------------------------------------------------------------------------


def freshness_classified(level: str, rule: str) -> None:
    inc(
        "techsara_freshness_classified_total",
        "Questions classified by required freshness.",
        level=level,
        rule=rule,
    )


def web_memory_query(*, hit: bool, fresh: bool, seconds: float) -> None:
    inc(
        "techsara_web_memory_queries_total",
        "Local web-memory retrievals attempted.",
        result="hit" if hit else "miss",
    )
    if hit:
        inc(
            "techsara_web_memory_hits_total",
            "Local web-memory retrievals that returned evidence.",
            result="fresh" if fresh else "stale",
        )
    observe(
        "techsara_web_memory_seconds",
        seconds,
        "Time to retrieve local web evidence.",
    )


def freshness_auto_search(ok: bool) -> None:
    inc(
        "techsara_freshness_auto_search_total",
        "Lightweight live lookups triggered because local evidence was insufficient.",
        result="ok" if ok else "fail",
    )


def worker_job(job: str, ok: bool, seconds: float) -> None:
    inc(
        "techsara_web_worker_jobs_total",
        "Background knowledge-worker jobs.",
        job=job,
        result="ok" if ok else "fail",
    )
    observe("techsara_web_worker_seconds", seconds, "Background job duration.", job=job)


def corpus_gauges(pages: int, pending: int, due: int) -> None:
    set_gauge("techsara_web_pages_total", pages, "Pages in the public web corpus.")
    set_gauge(
        "techsara_web_embedding_pending", pending, "Pages stored but not yet embedded."
    )
    set_gauge(
        "techsara_web_refresh_queue_depth", due, "Pages past their refresh deadline."
    )


# ---------------------------------------------------------------------------
# The 1-2 s programme's call sites, named once (performance plan item 1c).
# The instrumenting code in main.py, living_knowledge.py and
# engines/orchestrate.py belongs to other owners; these helpers are the
# contract they call, so a metric name or a label cannot be misspelt there.
# ---------------------------------------------------------------------------


def chat_first_visible(seconds: float, *, route: str, effort: str, kind: str) -> None:
    observe(
        "chat_first_visible_seconds",
        seconds,
        "Request start to the first thing the person sees: a reasoning token, "
        "a status line with content, or an answer token.",
        route=route,
        effort=effort,
        kind=kind,
    )


def knowledge_prepare(seconds: float, *, effort: str, decision: str, outcome: str = "ok") -> None:
    observe(
        "knowledge_prepare_seconds",
        seconds,
        "Whole living-knowledge pre-pass (classify, router, retrieve) per turn.",
        effort=effort,
        decision=decision,
        outcome=outcome,
    )


def context_assembly(seconds: float, *, effort: str, mode: str) -> None:
    observe(
        "context_assembly_seconds",
        seconds,
        "Mode resolved to context assembled: facts, recall, documents, history.",
        effort=effort,
        mode=mode,
    )


def orchestrate_decide(seconds: float, *, effort: str, plan: str, outcome: str = "ok") -> None:
    observe(
        "orchestrate_decide_seconds",
        seconds,
        "The orchestrate router's agent/search decision.",
        effort=effort,
        plan=plan,
        outcome=outcome,
    )


def plan_label(agent: bool, search: bool) -> str:
    """engines/orchestrate.Plan as one bounded label value."""
    if agent and search:
        return "agent_search"
    return "agent" if agent else ("search" if search else "none")


def topical_precheck(result: str) -> None:
    """One answer of the Fast topical pre-check: hit, miss or fail."""
    inc(
        "knowledge_topical_precheck_total",
        _TOPICAL_PRECHECK_HELP,
        result=result,
    )


def recall_block_dropped(reason: str) -> None:
    """The in-conversation recall block was left out: embed_busy,
    embed_timeout or embed_error. Same series as calling inc() directly."""
    inc("recall_block_dropped_total", _RECALL_DROPPED_HELP, reason=reason)


def relay_overhead(seconds: float, *, route: str, effort: str) -> None:
    observe(
        "relay_overhead_seconds",
        seconds,
        "Engine first token received to the SSE write that carries it.",
        route=route,
        effort=effort,
    )


_TOPICAL_PRECHECK_HELP = (
    "Fast topical pre-check answers before retrieval finished: hit (a page can "
    "pass the gate), miss (none can), fail (could not tell)."
)
_RECALL_DROPPED_HELP = (
    "In-conversation recall blocks left out of the prompt because the "
    "embedding call was busy, timed out or failed."
)
# Declared at import, so the HELP text is right whichever call site — the
# helper or a bare inc() with no help text — reaches the registry first.
_declare("knowledge_topical_precheck_total", "counter", _TOPICAL_PRECHECK_HELP)
_declare("recall_block_dropped_total", "counter", _RECALL_DROPPED_HELP)


# ---------------------------------------------------------------------------
# Exposition
# ---------------------------------------------------------------------------


def _fmt_labels(key: Tuple[Tuple[str, str], ...], extra: str = "") -> str:
    parts = [f'{k}="{v}"' for k, v in key]
    if extra:
        parts.append(extra)
    return "{" + ",".join(parts) + "}" if parts else ""


def render() -> str:
    """The whole registry in Prometheus text exposition format."""
    lines: List[str] = []
    with _lock:
        counters = {n: dict(v) for n, v in _counters.items()}
        gauges = {n: dict(v) for n, v in _gauges.items()}
        hists = {n: dict(v) for n, v in _hists.items()}

    for name, series in sorted(counters.items()):
        lines.append(f"# HELP {name} {_HELP.get(name, name)}")
        lines.append(f"# TYPE {name} counter")
        for key, value in sorted(series.items()):
            lines.append(f"{name}{_fmt_labels(key)} {value:g}")

    for name, series in sorted(gauges.items()):
        lines.append(f"# HELP {name} {_HELP.get(name, name)}")
        lines.append(f"# TYPE {name} gauge")
        for key, value in sorted(series.items()):
            lines.append(f"{name}{_fmt_labels(key)} {value:g}")

    for name, series in sorted(hists.items()):
        lines.append(f"# HELP {name} {_HELP.get(name, name)}")
        lines.append(f"# TYPE {name} histogram")
        buckets = _buckets_for(name)
        for key, (counts, total, n) in sorted(series.items()):
            # The le= label is built OUTSIDE the f-string. A backslash inside
            # an f-string expression is only legal from Python 3.12 (PEP 701),
            # and the containers run 3.11 — this file parsed fine on the dev
            # box and on the 3.12 image, then failed to import in CI.
            for edge, c in zip(buckets, counts):
                edge_label = 'le="{}"'.format(edge)
                lines.append(
                    "{}_bucket{} {}".format(name, _fmt_labels(key, edge_label), c)
                )
            inf_label = 'le="+Inf"'
            lines.append("{}_bucket{} {}".format(name, _fmt_labels(key, inf_label), n))
            lines.append(f"{name}_sum{_fmt_labels(key)} {total:g}")
            lines.append(f"{name}_count{_fmt_labels(key)} {n}")

    return "\n".join(lines) + "\n"


def reset() -> None:
    """Tests only."""
    with _lock:
        _counters.clear()
        _gauges.clear()
        _hists.clear()
