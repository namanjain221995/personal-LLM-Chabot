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

#: Histograms whose observations are engine waits use the wide buckets.
_BUCKETS_BY_METRIC = {
    "llm_engine_wait_seconds": _WAIT_BUCKETS,
    "llm_queue_wait_seconds": _WAIT_BUCKETS,
    "llm_admission_wait_seconds": _WAIT_BUCKETS,
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
}


def _clean(labels: Dict[str, str], name: str = "") -> Tuple[Tuple[str, str], ...]:
    out = []
    per_metric = _ALLOWED_BY_METRIC.get(name, {})
    for key, value in sorted(labels.items()):
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
