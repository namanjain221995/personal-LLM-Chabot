#!/usr/bin/env python3
"""B-04: freeze an eval-set baseline and compare a candidate run against it.

  # freeze the baseline from N finished run_evalset.py run directories
  # (N >= 3 runs of --repeats 1, each on its own fresh account, --workers 1)
  baseline.py freeze runs/evalset-a runs/evalset-b runs/evalset-c --out baseline.json [--markdown baseline.md]
  # compare candidate run directories (pooled the same way) against a frozen baseline; exits 1 when it does not pass
  baseline.py compare baseline.json runs/evalset-x runs/evalset-y runs/evalset-z [--allow-insufficient] \
      [--out report.json]
  # print a frozen baseline as the tables for docs/ai-platform-upgrade/BENCHMARK_RESULTS.md
  baseline.py show baseline.json

Reads only <run dir>/results.json (kind "evalset", schema 1, the contract that
run_evalset.py writes) and the baseline documents this script writes. Standard
library only; nothing here calls a service or the network. Every rule the
numbers follow (latency units, noise, ratios, tolerances, scores, regressions,
the expected false-block rate) is written into each baseline document under
"method", so a number never travels without the way it was made, and each
baseline lists the conditions of every run it pools.

A baseline may be committed to the public repository. It keeps no endpoint,
no health detail and no account detail beyond counts; run directories are
named by their last path component only; a conditions label holding a URL,
an IP address, a dotted host name, a control or a bidi character is refused.

Exit codes: 0 done (compare: passed), 1 compare did not pass, 2 bad input or
anything else that is not a verdict (an internal error included), so a crash
never reads as "did not pass".
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import math
import os
import re
import statistics
import sys
import unicodedata
from collections import Counter
from datetime import datetime
from fractions import Fraction
from typing import Any, Dict, Iterable, List, Optional, Sequence

RUN_KIND, RUN_SCHEMA = "evalset", 1
BASELINE_KIND, BASELINE_SCHEMA = "evalset-baseline", 3
REPORT_KIND, REPORT_SCHEMA = "evalset-comparison", 3

# MASTER_PROMPT §24 workload classes for the eval set (the set has no large-document job case). The same map
# lives in run_evalset.py, which writes it into every record; a record's own `workload` wins, this is the fallback.
WORKLOAD = {
    "EV01": "direct_fast", "EV02": "direct_fast", "EV03": "direct_fast", "RQ01": "direct_fast",
    "RQ03": "direct_fast", "RQ05": "direct_fast", "RQ06": "direct_fast", "RQ07": "direct_fast",
    "EV05": "evidence_fast", "RQ02": "evidence_fast",
    "EV04": "live_search_fast", "RQ04": "live_search_fast",
    "EV07": "long_context",
    "EV06": "think", "EV08": "think",
    "EV09": "max",
}
CLASS_ORDER = ("direct_fast", "evidence_fast", "live_search_fast", "long_context", "think", "max")
FAST_CLASSES = ("direct_fast", "evidence_fast", "live_search_fast", "long_context")
# Effort families: a class borrows the noise of its family (the sigma pooled over the family's units) when its own
# few units say less. Fast and think/max differ several-fold in repeat noise, so they are never pooled together.
SIGMA_FAMILIES = {"fast": FAST_CLASSES, "slow": ("think", "max")}
CLASS_ABS_S = {"direct_fast": 0.25, "evidence_fast": 0.5, "live_search_fast": 1.0, "long_context": 1.0,
               "think": 2.0, "max": 10.0}
METRICS = ("first_event_s", "first_token_s", "first_answer_s", "total_s")
TOTAL_ABS_FACTOR = 2.0          # total_s abs is 2x the class abs; first_event_s and first_token_s use the class abs
REL_FLOOR = 0.20
SIGMA_FACTOR = 2.0
MIN_SAMPLES = 3                 # per side and class/metric; also the fewest records per case a baseline freezes
MIN_UNIT_SAMPLES = 2            # a baseline unit needs this many samples to say anything about noise
P95_MIN_N = 20                  # below this, nearest-rank p95 is the maximum sample: reported, never gated
LOG_FLOOR_S = 1e-3              # values <= this are clamped before ln
OVERALL_DROP = Fraction(1, 20)
THINKING_CHECK = "thinking_off"
FLOAT_RTOL = 1e-9               # a stored derived number must equal its recomputation to this relative tolerance
SAFE_ACCOUNT_KEYS = ("conversations", "facts", "checked", "allowed_used")
_HEX_COMMIT = re.compile(r"[0-9a-f]{7,64}")


def family_of(workload: str) -> str:
    """The effort family whose units a class's noise is pooled with: 'fast' for the Fast classes, else 'slow'."""
    return "fast" if workload in FAST_CLASSES else "slow"


def gated_metrics(workload: str) -> tuple:
    """The metrics that gate a class: first_answer_s and total_s everywhere; first_event_s (the UI
    acknowledgment of §24) for the Fast classes only. first_token_s is reported, never gated."""
    return ("first_event_s", "first_answer_s", "total_s") if workload in FAST_CLASSES \
        else ("first_answer_s", "total_s")


CONSTANTS = {
    "rel_floor": REL_FLOOR, "sigma_factor": SIGMA_FACTOR, "class_abs_s": dict(CLASS_ABS_S),
    "total_abs_factor": TOTAL_ABS_FACTOR, "min_samples": MIN_SAMPLES, "min_unit_samples": MIN_UNIT_SAMPLES,
    "p95_min_n": P95_MIN_N, "log_floor_s": LOG_FLOOR_S, "overall_drop": str(OVERALL_DROP),
    "thinking_check": THINKING_CHECK, "gated": {w: list(gated_metrics(w)) for w in CLASS_ORDER},
    "sigma_families": {f: list(ws) for f, ws in SIGMA_FAMILIES.items()},
}


class BaselineError(ValueError):
    """Input that cannot be frozen or compared honestly; the message says why."""


def _abs_text() -> str:
    return ", ".join(f"{w} {CLASS_ABS_S[w]:g} s" for w in CLASS_ORDER)


METHOD = {
    "runs": "The baseline is N run directories of run_evalset.py --repeats 1 --workers 1, each on its own fresh "
            "account (no other conversations, no saved facts), so repeats never see each other; within a run the "
            "case order is fixed. Every pooled run must have finished (not interrupted, no --deadline reached), "
            "hold exactly conditions.repeats records of every case it ran (all of its --only, every case of the "
            "pool when --only is empty), and share eval_set_sha256 and workers; a conversation id seen twice is "
            f"refused (a copied run). Freeze needs at least {MIN_SAMPLES} records of every case.",
    "workload": "Each record's own `workload` field; when absent, the eval-set map in baseline.WORKLOAD "
                "(MASTER_PROMPT §24 classes). A class without a defined tolerance is refused.",
    "case_score": "Score of one record (case x repeat) = checks passed / checks run, over all its turns, as an "
                  "exact fraction. A record with an error, or with no checks at all, scores 0 and does not pass.",
    "case_pass": "A record passes when every check is ok and it has no error. Case pass_rate = passing records / "
                 "records; mean_score = mean record score.",
    "case_check": "Per (case, check name): a record passes that check when it has no error, runs the check at "
                  "least once and it never fails (check_passes counts those records); check_runs counts the "
                  "records with no error that ran it. A complete record has no error and at least one check. A "
                  "check is conditional for a case when the baseline did not run it in every complete record "
                  "(the harness emits some only when something happened, e.g. job_completed only when a turn "
                  "made a file); a record fails a conditional check when it errored or the check failed in it. "
                  "A check the baseline ran in every complete record is unconditional: a record that lacks it "
                  "fails it.",
    "check_rate": "Per check name: ok / total over every (case, repeat, turn), errored records included (reported).",
    "overall": "Overall mean score = mean over cases of each case's mean_score (every case weighs the same), exact.",
    "fast_thinking": f"Fast thinking turns = turns of effort 'fast' records whose `{THINKING_CHECK}` check failed; "
                     f"leak rate = those / Fast turns, exact. A Fast turn without a `{THINKING_CHECK}` check is "
                     "unchecked: freeze refuses one and compare fails on one (the harness always emits it).",
    "failed_turns": "A turn fails when its record has an error, or its result is missing, http is not 200, "
                    "timed_out is true, errors is non-empty, or terminal (when present) is not 'done'. A record "
                    "that errored before any turn counts as one failed turn. Failed turns are excluded from every "
                    "latency sample and counted as `excluded`.",
    "latency_unit": "Latency unit = (case id, turn index), written CASE/tN. Samples of a unit are that turn's "
                    "timing values over every repeat of every pooled run, failed turns excluded, nulls counted in "
                    "`missing`. Metrics: " + ", ".join(METRICS) + ".",
    "percentile": "n, p50, p95, min, max and mean are over every sample of the class/metric; p50 and p95 are "
                  "nearest rank: the sample at 1-based rank ceil(q x n) of the ascending samples, q = 0.50 and "
                  "0.95, computed in integers; no interpolation. Unit medians use the usual median (mean of the "
                  "two middle samples for an even count).",
    "noise": f"Noise sigma per class/metric = sqrt(mean over the baseline units with >= {MIN_UNIT_SAMPLES} samples "
             f"of the sample variance (n - 1) of ln x), values <= {LOG_FLOOR_S:g} s clamped to {LOG_FLOOR_S:g} s "
             "before ln. It measures repeat-to-repeat noise of the same turn, not the spread between cases. "
             "The sigma used is max(the class's own sigma, the same figure pooled over the units of every class "
             "of its effort family for that metric); the families are fast (" + ", ".join(FAST_CLASSES) + ") "
             "and slow (" + ", ".join(SIGMA_FAMILIES["slow"]) + "). Classes with one unit (long_context, max) "
             "would otherwise estimate their noise from three samples, and a class that really is noisier keeps "
             "its own larger figure. The families are never pooled together: think and max are several times "
             "noisier than Fast, and pooling them in loosens every Fast gate: in the Monte Carlo of false_block, "
             "with Fast sigma 0.08 and think/max sigma 0.40-0.60, a 1.3x Fast slowdown was blocked in only 35-58 % "
             "of comparisons when every class was pooled and in 99 % within the family (sigma_class, sigma_pooled "
             "and sigma_family are stored with each class/metric).",
    "ratio": "ratio = geometric mean, over the units with samples on both sides, of median(candidate unit) / "
             "median(baseline unit). scale = geometric mean of the baseline unit medians.",
    "tolerance": f"allowed_ratio = 1 + max({REL_FLOOR:.2f}, {SIGMA_FACTOR:g} x sigma) + abs / scale; abs by class: "
                 f"{_abs_text()}; total_s uses {TOTAL_ABS_FACTOR:g} x the class abs, first_event_s and "
                 "first_token_s the class abs.",
    "p95": f"p95 gates only when both sides have >= {P95_MIN_N} samples in the class/metric: candidate p95 <= "
           f"baseline p95 x (1 + max({REL_FLOOR:.2f}, {SIGMA_FACTOR:g} x sigma)) + abs. Below {P95_MIN_N} the "
           "nearest-rank p95 is the maximum sample: it is reported (p95_status 'reported') and never gates.",
    "gated": "Gated: first_answer_s and total_s in every class, first_event_s (the UI acknowledgment of §24) in "
             "the Fast classes (" + ", ".join(FAST_CLASSES) + "). first_token_s is reported only.",
    "insufficient": f"A class/metric needs >= {MIN_SAMPLES} samples on each side and >= 1 baseline unit with >= "
                    f"{MIN_UNIT_SAMPLES} samples; otherwise its status is insufficient_samples and it has no "
                    "allowed values. A comparison never counts it as a pass.",
    "output_rate": "Output tokens/s per sample = usage.completion_tokens / (total_s - first_answer_s), over the "
                   "turns that did not fail and carry all three numbers with a positive denominator; median per "
                   "class, reported, never gated. completion_tokens sums every traced main-model call of the turn, "
                   "reasoning included, so for think and max the figure overstates decode speed.",
    "false_block": "Expected false blocks and power of the LATENCY gates only (no quality gate is in these figures: "
                   "every simulated check passes), measured by a Monte Carlo of this exact freeze and compare: the "
                   "16 eval-set cases, one turn each, at plausible medians, lognormal repeat noise sigma, spikes = "
                   "10 % of requests also slowed 1.5-3x by other load, fixed seeds. UNIFORM noise (400 trials a "
                   "row), 3 baseline runs vs 3 candidate runs, unchanged build: blocked in 3.0 % at sigma 0.10 and "
                   "8.5 % at sigma 0.15 without spikes, 19 % (sigma 0.15) and 24 % (sigma 0.30) with spikes; with "
                   "a noisy first_event 1-6 % and 25-27 %; 5 baseline runs vs 3 in 1.8 % / 4.8 % without spikes and "
                   "16-18 % with. Power, 3 vs 3, every class slowed alike: 1.3x is blocked in 99.8 % (sigma 0.10) "
                   "and 96.5 % (sigma 0.15) without spikes, but only in 72-75 % with spikes; 1.5x in 100 % without "
                   "and 94.5-97 % with; 2x in 100 %. HETEROGENEOUS noise (300 trials a cell; Fast sigma 0.08 or 0.15, "
                   "think/max sigma 0.35-0.60; only the Fast cases slowed): unchanged build blocked in 11-17 % "
                   "without spikes (almost all by the noisy think/max classes; direct_fast 0-3 %) and 21-24 % with; "
                   "a 1.3x Fast slowdown is blocked in 99 % (Fast sigma 0.08) and 87 % (0.15) without spikes, but "
                   "with spikes only in 54-57 %, against a 21-24 % false-block rate in the same conditions, and the "
                   "direct_fast gate itself catches it in 33-34 %. So under load spikes a real 1.3x slowdown passes "
                   "about half the time: a latency pass in a spiky window is weak evidence, and only a quiet window "
                   "or more runs make it strong. Pooling sigma within the effort family, not over every class, "
                   "costs some false blocks under uniform noise (8.5 % against 6.5 % at sigma 0.15, 19 % against "
                   "15 % with spikes: think and max then pool three units, not sixteen) and restores the Fast power "
                   "when think/max are noisier (a 1.3x Fast slowdown blocked in 87-99 % against 35-70 % when every "
                   "class was pooled); a per-class sigma alone blocks more unchanged builds (19-29 % without "
                   "spikes, 39-42 % with). More baseline runs lower false blocks. "
                   "Procedure: when a latency gate fails, re-run the BASELINE commit in the same window (same "
                   "stack, same hour) as a tie-break and compare it with the frozen "
                   "baseline. If it fails the same gate, the window is slow, not the candidate: the latency verdict "
                   "is void and the comparison is repeated in a quieter window. If it passes, the candidate's "
                   "failure stands.",
    "compare_conditions": "The candidate must have the baseline's eval_set_sha256 and workers (refused, exit 2, "
                          "otherwise). Labels, model ids (result.versions.model) and harness commits that differ "
                          "are listed in condition_changes, with both sides' labels; they do not block.",
    "compare_case": "Only cases the baseline has are compared; new cases are listed. A case regresses when the "
                    "candidate pass_rate < baseline pass_rate - 1 / min(baseline repeats, candidate repeats), exact "
                    "fractions. A baseline case missing from the candidate fails.",
    "compare_case_check": "Per (case, check), exact fractions, k = min(baseline repeats, candidate repeats): an "
                          "unconditional check fails when the candidate's check_passes / repeats < the baseline's "
                          "- 1/k; a conditional check (see case_check) fails when the candidate's failing records / "
                          "repeats > the baseline's + 1/k, so a candidate that stops emitting it (e.g. stops making "
                          "an unwanted file) is not a regression. Both gate cases that never fully pass.",
    "compare_failed_turns": "Per case: fails when the candidate's failed-turn rate > the baseline's + 1 / "
                            "min(baseline repeats, candidate repeats), exact fractions.",
    "compare_overall": "The candidate regresses overall when its overall mean score, over the cases both have, is "
                       f"more than {OVERALL_DROP} below the baseline's over the same cases, exact fractions (a drop "
                       f"of exactly {OVERALL_DROP} passes).",
    "compare_latency": "Per class and gated metric the candidate fails when ratio > allowed_ratio, or (both sides >= "
                       f"{P95_MIN_N} samples) when its p95 > allowed p95. Only the baseline's units are compared; a "
                       "new candidate unit is listed, never pooled. Reported metrics get a verdict that never gates.",
    "compare_thinking": "Fails when the candidate's Fast leak rate > the baseline's (exact; with a baseline of 0 "
                        "any leak fails), or when any candidate Fast turn lacks the check.",
    "compare_passed": "passed = no failure, and no gated class/metric whose BASELINE side is insufficient unless "
                      "--allow-insufficient. A candidate side with too few samples where the baseline had enough "
                      "is a failure, never waived.",
    "compare_integrity": "compare recomputes every derived number of the baseline (unit medians and log variances, "
                         "n, percentiles, sigma, scale, allowed values, counts against the sources) with the "
                         "constants of this script and refuses a baseline that disagrees.",
}


# ------------------------------------------------------------- validation --

def _is_int(value) -> bool:
    return type(value) is int


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _opt(value, check, where: str, what: str) -> None:
    if value is not None and not check(value):
        raise BaselineError(f"{where}: {what} (got {type(value).__name__} {value!r:.80})")


_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://|\bwww\.", re.IGNORECASE)
_IPV4 = re.compile(r"(?<![0-9.])[0-9]{1,3}(?:\.[0-9]{1,3}){3}(?![0-9])")
_IPV6_TOKEN = re.compile(r"[0-9A-Fa-f:.%]*:[0-9A-Fa-f:.%]*:[0-9A-Fa-f:.%]*")
_HOST = re.compile(r"(?<![A-Za-z0-9-])(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z]{2,63}"
                   r"(?![A-Za-z0-9-])")
_BIDI = set(range(0x202A, 0x202F)) | set(range(0x2066, 0x206A)) | {0x200E, 0x200F, 0x061C}


def public_text_problem(text: str) -> Optional[str]:
    """Why `text` must not go into a public document, or None. Refused: a URL, an IPv4 or IPv6 address, a
    dotted host name (labels joined by dots ending in an alphabetic tail of two or more letters, e.g.
    host-a.internal, box.local, example.com), a control character (line breaks included) and a bidi
    control (U+202A-U+202E, U+2066-U+2069, U+200E, U+200F, U+061C)."""
    for ch in text:
        cat = unicodedata.category(ch)
        if cat == "Cc" or cat in ("Zl", "Zp"):
            return f"a control character U+{ord(ch):04X}"
        if ord(ch) in _BIDI:
            return f"a bidi control U+{ord(ch):04X}"
    if _URL.search(text):
        return "a URL"
    if _IPV4.search(text):
        return "an IPv4 address"
    for token in _IPV6_TOKEN.findall(text):
        token = token.strip("[]").rstrip(".").split("%")[0]
        try:
            if isinstance(ipaddress.ip_address(token), ipaddress.IPv6Address):
                return "an IPv6 address"
        except ValueError:
            pass
    m = _HOST.search(text)
    if m:
        return f"a dotted host name ({m.group(0)!r})"
    return None


def _read_json(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise BaselineError(f"{path}: no such file") from None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise BaselineError(f"{path}: not readable JSON ({type(exc).__name__}: {exc})") from None


def _check_record(rec, where: str) -> None:
    """Every field this script reads, with its type. A wrong type is bad input (exit 2), never a verdict."""
    if not isinstance(rec, dict):
        raise BaselineError(f"{where}: a case record must be an object")
    if not isinstance(rec.get("id"), str) or not rec["id"]:
        raise BaselineError(f"{where}: a case record needs a non-empty string `id`")
    where = f"{where} ({rec['id']})"
    _opt(rec.get("workload"), lambda v: isinstance(v, str) and v, where, "`workload` must be a non-empty string")
    if not isinstance(rec.get("effort"), str) or not rec["effort"]:
        raise BaselineError(f"{where}: `effort` must be a non-empty string")
    _opt(rec.get("category"), lambda v: isinstance(v, str), where, "`category` must be a string or null")
    _opt(rec.get("error"), lambda v: isinstance(v, str), where, "`error` must be a string or null")
    _opt(rec.get("conversation_id"), lambda v: isinstance(v, str), where, "`conversation_id` must be a string")
    turns = rec.get("turns", [])
    if not isinstance(turns, list) or not all(isinstance(t, dict) for t in turns):
        raise BaselineError(f"{where}: `turns` must be a list of objects")
    for j, t in enumerate(turns):
        tw = f"{where} turn {j}"
        checks = t.get("checks", [])
        if not isinstance(checks, list) or not all(
                isinstance(c, dict) and isinstance(c.get("check"), str) and c["check"]
                and isinstance(c.get("ok"), bool) for c in checks):
            raise BaselineError(f"{tw}: `checks` must be a list of objects with a non-empty string `check` and a "
                                "boolean `ok`")
        result = t.get("result")
        _opt(result, lambda v: isinstance(v, dict), tw, "`result` must be an object or null")
        result = result or {}
        _opt(result.get("http"), _is_int, tw, "`result.http` must be an integer or null")
        _opt(result.get("errors"), lambda v: isinstance(v, list), tw, "`result.errors` must be a list or null")
        _opt(result.get("timed_out"), lambda v: isinstance(v, bool), tw, "`result.timed_out` must be a boolean")
        _opt(result.get("terminal"), lambda v: isinstance(v, str), tw, "`result.terminal` must be a string or null")
        timing = result.get("timing")
        _opt(timing, lambda v: isinstance(v, dict), tw, "`result.timing` must be an object or null")
        for m in METRICS:
            _opt((timing or {}).get(m), lambda v: _is_number(v) and v >= 0, tw,
                 f"timing {m} must be a non-negative number or null")
        usage = result.get("usage")
        _opt(usage, lambda v: isinstance(v, dict), tw, "`result.usage` must be an object or null")
        _opt((usage or {}).get("completion_tokens"), lambda v: _is_int(v) and v >= 0, tw,
             "usage.completion_tokens must be a non-negative integer or null")
        versions = result.get("versions")
        _opt(versions, lambda v: isinstance(v, dict), tw, "`result.versions` must be an object or null")
        _opt((versions or {}).get("model"), lambda v: isinstance(v, str), tw, "versions.model must be a string")


def _check_source(src, where: str) -> None:
    """The run conditions a record carries in `_run` (load_runs builds them from results.json)."""
    if not isinstance(src, dict):
        raise BaselineError(f"{where}: run conditions must be an object")
    for key in ("dir", "started", "finished", "label", "harness_commit"):
        _opt(src.get(key), lambda v: isinstance(v, str), where, f"`{key}` must be a string or null")
    _opt(src.get("seconds"), _is_number, where, "`seconds` must be a number or null")
    for key in ("interrupted", "deadline_reached"):
        _opt(src.get(key), lambda v: isinstance(v, bool), where, f"`{key}` must be a boolean or null")
    for key in ("workers", "repeats"):
        if not (_is_int(src.get(key)) and src[key] >= 1):
            raise BaselineError(f"{where}: conditions.{key} must be a positive integer (got {src.get(key)!r:.40})")
    only = src.get("only")
    if only is not None and not (isinstance(only, list) and all(isinstance(c, str) and c for c in only)):
        raise BaselineError(f"{where}: conditions.only must be a list of case ids")
    if not isinstance(src.get("eval_set_sha256"), str) or not src["eval_set_sha256"]:
        raise BaselineError(f"{where}: conditions.eval_set_sha256 is missing; without the eval-set fingerprint "
                            "no comparison can show that both sides ran the same set")
    account = src.get("account")
    if account is not None:
        if not isinstance(account, dict):
            raise BaselineError(f"{where}: conditions.account must be an object or null")
        for key in ("conversations", "facts"):
            _opt(account.get(key), lambda v: _is_int(v) and v >= 0, where, f"account.{key} must be a count")
        for key in ("checked", "allowed_used"):
            _opt(account.get(key), lambda v: isinstance(v, bool), where, f"account.{key} must be a boolean")


def read_run(run_dir: str) -> dict:
    """One run directory's results.json, refused unless it is kind 'evalset', schema 1, with typed fields."""
    path = os.path.join(run_dir, "results.json")
    if not os.path.isfile(path):
        raise BaselineError(f"{run_dir}: no results.json (expected a run_evalset.py run directory)")
    doc = _read_json(path)
    if not isinstance(doc, dict):
        raise BaselineError(f"{path}: not a JSON object")
    kind, schema = doc.get("kind"), doc.get("schema")
    if kind != RUN_KIND or type(schema) is not int or schema != RUN_SCHEMA:
        raise BaselineError(f"{path}: kind={kind!r} schema={schema!r}; only kind {RUN_KIND!r} schema {RUN_SCHEMA} "
                            "(run_evalset.py results) is accepted")
    if not isinstance(doc.get("conditions"), dict):
        raise BaselineError(f"{path}: `conditions` is missing or not an object")
    if not isinstance(doc.get("cases"), list):
        raise BaselineError(f"{path}: `cases` is missing or not a list")
    if not doc["cases"]:
        raise BaselineError(f"{path}: the run has no case records; it would be listed as a source of no numbers")
    for i, rec in enumerate(doc["cases"]):
        _check_record(rec, f"{path}: cases[{i}]")
    return doc


def _source(run_dir: str, doc: dict) -> dict:
    cond = doc["conditions"]

    def flag(key):
        return doc[key] if key in doc else cond.get(key)

    src = {"dir": run_dir, "started": doc.get("started"), "finished": doc.get("finished"),
           "seconds": doc.get("seconds"), "interrupted": flag("interrupted"),
           "deadline_reached": flag("deadline_reached"), "label": cond.get("label"),
           "harness_commit": cond.get("harness_commit"), "workers": cond.get("workers"),
           "repeats": cond.get("repeats"), "only": cond.get("only"), "eval_set_sha256": cond.get("eval_set_sha256"),
           "account": cond.get("account")}
    _check_source(src, os.path.join(run_dir, "results.json"))
    return src


def load_runs(dirs: Sequence[str], allow_mixed: bool = False) -> List[dict]:
    """Pool the case records of one or more run directories.

    Each returned record is a shallow copy carrying `_run`: the conditions of the run it came from (the same
    dict for every record of a run). Runs whose conditions.label differ are refused unless allow_mixed, because
    numbers measured under different conditions must not be pooled silently. freeze and compare check the rest
    of the pool (finished, counts, fingerprints, duplicates).
    """
    if isinstance(dirs, (str, os.PathLike)):
        dirs = [dirs]
    dirs = [os.fspath(d) for d in dirs]
    if not dirs:
        raise BaselineError("no run directory given")
    seen: Dict[str, str] = {}
    for d in dirs:
        real = os.path.realpath(d)
        if real in seen:
            raise BaselineError(f"{d}: the same run directory is given twice (also as {seen[real]}); pooling it "
                                "twice would double-count its samples")
        seen[real] = d
    records: List[dict] = []
    sources: List[dict] = []
    for d in dirs:
        doc = read_run(d)
        src = _source(d, doc)
        sources.append(src)
        for rec in doc["cases"]:
            out = dict(rec)
            out["_run"] = src
            records.append(out)
    labels = _unique(s["label"] for s in sources)
    if len(labels) > 1 and not allow_mixed:
        listing = "; ".join(f"{os.path.basename(os.path.normpath(s['dir']))}: {s['label']!r}" for s in sources)
        raise BaselineError(f"the runs have different conditions labels ({listing}); numbers from different "
                            "conditions must not be pooled silently. Pass --allow-mixed-conditions (allow_mixed=True) "
                            "to pool them anyway; every label is then listed")
    return records


def _unique(values: Iterable) -> list:
    out: list = []
    for v in values:
        if v not in out:
            out.append(v)
    return out


def sources_of(records: Iterable[dict]) -> List[dict]:
    """The distinct run conditions behind a list of pooled records, in first-seen order."""
    out: List[dict] = []
    for rec in records:
        src = rec.get("_run")
        if isinstance(src, dict) and not any(src is s for s in out):
            out.append(src)
    return out


def _run_name(src: dict) -> str:
    return os.path.basename(os.path.normpath(src["dir"])) if src.get("dir") else "<run>"


def _check_pool(records: Sequence[dict], side: str) -> List[dict]:
    """The integrity of pooled records; returns their run conditions. Refuses (BaselineError): a record without
    run conditions, an unfinished, interrupted or deadline-cut run, runs with different eval_set_sha256 or
    workers, a run whose record count per case differs from its conditions.repeats or its --only, and a
    conversation id seen twice."""
    if any(not isinstance(r.get("_run"), dict) for r in records):
        raise BaselineError(f"{side}: a record carries no run conditions (`_run`); read run directories with "
                            "load_runs")
    sources = sources_of(records)
    by_src: Dict[int, List[dict]] = {id(s): [] for s in sources}
    for rec in records:
        by_src[id(rec["_run"])].append(rec)
    for s in sources:
        name = _run_name(s)
        _check_source(s, f"{side} run {name}")
        if s.get("finished") is None:
            raise BaselineError(f"{side} run {name}: `finished` is null; the run did not finish (still running or "
                                "killed), so its records are not a complete repeat")
        if s.get("interrupted") is True:
            raise BaselineError(f"{side} run {name}: the run was interrupted; its records are not a complete repeat")
        if s.get("deadline_reached") is True:
            raise BaselineError(f"{side} run {name}: the run reached its --deadline and skipped cases")
    for key in ("eval_set_sha256", "workers"):
        values = _unique(s[key] for s in sources)
        if len(values) > 1:
            listing = "; ".join(f"{_run_name(s)}: {s[key]!r}" for s in sources)
            raise BaselineError(f"{side}: the runs differ in conditions.{key} ({listing}); they are not comparable "
                                "and are never pooled")
    pool_ids = _unique(r["id"] for r in records)
    for s in sources:
        name, counts = _run_name(s), Counter(r["id"] for r in by_src[id(s)])
        expected = list(s.get("only") or []) or pool_ids
        for cid in _unique(list(expected) + list(counts)):
            if cid not in expected:
                raise BaselineError(f"{side} run {name}: case {cid} is not in its --only {s['only']}")
            if counts.get(cid, 0) != s["repeats"]:
                raise BaselineError(f"{side} run {name}: case {cid} has {counts.get(cid, 0)} record(s), "
                                    f"conditions.repeats is {s['repeats']}; the run is incomplete or edited")
    seen: Dict[str, str] = {}
    for rec in records:
        conv = rec.get("conversation_id")
        if conv is None:
            continue
        if conv in seen:
            raise BaselineError(f"{side}: conversation id {conv!r} appears twice ({seen[conv]} and "
                                f"{_run_name(rec['_run'])}); a copied run directory would double-count its samples")
        seen[conv] = _run_name(rec["_run"])
    return sources


def _model_ids(records: Iterable[dict]) -> List[str]:
    out = set()
    for rec in records:
        for t in rec.get("turns") or []:
            model = ((t.get("result") or {}).get("versions") or {}).get("model")
            if isinstance(model, str) and model:
                out.add(model)
    return sorted(out)


def _public_account(account) -> Optional[dict]:
    if not isinstance(account, dict):
        return None
    return {k: account.get(k) for k in SAFE_ACCOUNT_KEYS}


def _public_sources(sources: Sequence[dict], records: Sequence[dict]) -> List[dict]:
    """What a public document keeps of each run: no endpoint, no health, no account detail beyond counts, and
    the run directory's last path component only."""
    out = []
    for s in sources:
        mine = [r for r in records if r.get("_run") is s]
        out.append({"dir": _run_name(s) if s.get("dir") else None, "started": s.get("started"),
                    "finished": s.get("finished"), "seconds": s.get("seconds"), "label": s.get("label"),
                    "harness_commit": s.get("harness_commit"), "workers": s.get("workers"),
                    "repeats": s.get("repeats"), "only": list(s.get("only") or []),
                    "eval_set_sha256": s.get("eval_set_sha256"), "model_ids": _model_ids(mine),
                    "account": _public_account(s.get("account"))})
    return out


def _pool_conditions(public_sources: Sequence[dict]) -> dict:
    return {"eval_set_sha256": public_sources[0]["eval_set_sha256"] if public_sources else None,
            "workers": public_sources[0]["workers"] if public_sources else None,
            "labels": _unique(s["label"] for s in public_sources),
            "model_ids": sorted({m for s in public_sources for m in s["model_ids"]}),
            "harness_commits": _unique(s["harness_commit"] for s in public_sources)}


def _check_public_source(s: dict, where: str) -> None:
    label = s.get("label")
    if not isinstance(label, str) or not label.strip():
        raise BaselineError(f"{where}: conditions.label is empty; every baseline number must state its conditions "
                            "(run_evalset.py --label)")
    problem = public_text_problem(label)
    if problem:
        raise BaselineError(f"{where}: conditions.label holds {problem}; a baseline may be committed to a public "
                            "repository. Re-run with a label that names the conditions without hosts or addresses")
    commit = s.get("harness_commit")
    if commit is not None and not _HEX_COMMIT.fullmatch(commit):
        raise BaselineError(f"{where}: harness_commit {commit!r:.80} is not a hex commit id")
    for model in s.get("model_ids") or []:
        problem = public_text_problem(model)
        if problem:
            raise BaselineError(f"{where}: model id {model!r:.80} holds {problem}")
    if s.get("dir") is not None and ("/" in s["dir"] or "\\" in s["dir"]):
        raise BaselineError(f"{where}: sources[].dir must be a last path component, not {s['dir']!r:.80}")


# --------------------------------------------------------------- statistics --

def nearest_rank(values: Iterable[float], pct: int) -> float:
    """The nearest-rank percentile: the value at 1-based rank ceil(pct/100 x n) of the sorted values."""
    vals = sorted(float(v) for v in values)
    if not vals:
        raise ValueError("no values")
    if type(pct) is not int or not 0 < pct <= 100:
        raise ValueError(f"pct must be an integer in 1..100, not {pct!r}")
    rank = -(-pct * len(vals) // 100)          # ceil(pct * n / 100), exact in integers
    return vals[max(rank, 1) - 1]


def _ln(x: float) -> float:
    return math.log(max(float(x), LOG_FLOOR_S))


def abs_slack(workload: str, metric: str) -> float:
    if workload not in CLASS_ABS_S:
        raise BaselineError(f"no latency tolerance is defined for workload class {workload!r} "
                            f"(known: {', '.join(CLASS_ORDER)})")
    if metric not in METRICS:
        raise BaselineError(f"unknown latency metric {metric!r}")
    return CLASS_ABS_S[workload] * (TOTAL_ABS_FACTOR if metric == "total_s" else 1.0)


def tolerance(workload: str, metric: str, sigma: float = 0.0) -> dict:
    """rel = max(0.20, 2 x sigma) and the class abs (seconds) of one class/metric."""
    return {"rel": max(REL_FLOOR, SIGMA_FACTOR * sigma), "abs": abs_slack(workload, metric)}


def workload_of(rec: dict) -> str:
    w = rec.get("workload") or WORKLOAD.get(rec["id"])
    if not w:
        raise BaselineError(f"case {rec['id']}: the record has no `workload` and the id is not in the eval-set map")
    if not isinstance(w, str) or w not in CLASS_ABS_S:
        raise BaselineError(f"case {rec['id']}: workload class {w!r:.40} has no tolerance "
                            f"(known: {', '.join(CLASS_ORDER)})")
    return w


def _turn_failed(rec: dict, turn: dict) -> bool:
    if rec.get("error"):
        return True
    res = turn.get("result")
    if not isinstance(res, dict) or res.get("http") != 200 or res.get("timed_out") is True or res.get("errors"):
        return True
    return "terminal" in res and res["terminal"] != "done"


def _token_rate(res: dict) -> Optional[float]:
    tokens = (res.get("usage") or {}).get("completion_tokens")
    timing = res.get("timing") or {}
    total, first = timing.get("total_s"), timing.get("first_answer_s")
    if not _is_int(tokens) or total is None or first is None or total - first <= 0:
        return None
    return tokens / (total - first)


def _collect(records: Sequence[dict]) -> Dict[str, dict]:
    """Per class: unit samples per metric, missing per metric, excluded (failed) turns, output token rates."""
    out: Dict[str, dict] = {}
    for rec in records:
        w = workload_of(rec)
        c = out.setdefault(w, {"units": {m: {} for m in METRICS}, "missing": Counter(), "excluded": 0, "rates": []})
        for i, t in enumerate(rec.get("turns") or []):
            if _turn_failed(rec, t):
                c["excluded"] += 1
                continue
            unit = f"{rec['id']}/t{i + 1}"
            timing = t["result"].get("timing") or {}
            for m in METRICS:
                v = timing.get(m)
                if v is None:
                    c["missing"][m] += 1
                else:
                    c["units"][m].setdefault(unit, []).append(float(v))
            rate = _token_rate(t["result"])
            if rate is not None:
                c["rates"].append(rate)
    return out


def _unit_log_vars(units: Dict[str, List[float]]) -> List[float]:
    """The sample variance of ln x of every unit with >= MIN_UNIT_SAMPLES samples."""
    return [statistics.variance([_ln(float(v)) for v in vs]) for vs in units.values() if len(vs) >= MIN_UNIT_SAMPLES]


def pooled_sigma(unit_maps: Iterable[Dict[str, List[float]]]) -> Optional[float]:
    """Repeat noise of one metric pooled over the units of the given classes: sqrt(mean log variance)."""
    log_vars = [lv for units in unit_maps for lv in _unit_log_vars(units)]
    return math.sqrt(math.fsum(log_vars) / len(log_vars)) if log_vars else None


def family_sigmas(unit_maps: Dict[str, Dict[str, List[float]]]) -> Dict[str, Optional[float]]:
    """Per class (the keys of `unit_maps`, one metric's units per class): the sigma pooled over the units of every
    class of the same effort family (SIGMA_FAMILIES). freeze and the baseline check both call this, so the stored
    figure and its recomputation come from one rule."""
    by_family = {f: pooled_sigma(units for w, units in unit_maps.items() if family_of(w) == f)
                 for f in SIGMA_FAMILIES}
    return {w: by_family[family_of(w)] for w in unit_maps}


def _describe(workload: str, metric: str, units: Dict[str, List[float]], missing: int, excluded: int,
              sigma_pooled: Optional[float] = None) -> dict:
    """One baseline class/metric: the per-unit statistics, the class description and the allowed values.

    The noise used is the larger of the class's own sigma and `sigma_pooled`, the sigma pooled over the units of
    every class of the same effort family for this metric (family_sigmas): a class with one or two units
    estimates its own noise from very few samples, and the family figure keeps that estimate from coming out too
    small; a class that really is noisier keeps its own. Fast and think/max are separate families, so the
    several-fold larger noise of think and max never loosens the Fast classes."""
    unit_stats = {}
    for u, vs in units.items():
        if not vs:
            continue
        vs = sorted(float(v) for v in vs)
        unit_stats[u] = {"n": len(vs), "median": statistics.median(vs),
                         "log_var": (statistics.variance([_ln(v) for v in vs]) if len(vs) >= MIN_UNIT_SAMPLES
                                     else None),
                         "samples": vs}
    values = [v for s in unit_stats.values() for v in s["samples"]]
    n = len(values)
    noisy = [s["log_var"] for s in unit_stats.values() if s["log_var"] is not None]
    sigma_class = math.sqrt(math.fsum(noisy) / len(noisy)) if noisy else None
    sigma = (max(sigma_class, sigma_pooled) if sigma_class is not None and sigma_pooled is not None
             else sigma_class)
    scale = (math.exp(math.fsum(_ln(s["median"]) for s in unit_stats.values()) / len(unit_stats))
             if unit_stats else None)
    ok = n >= MIN_SAMPLES and bool(noisy)
    tol = tolerance(workload, metric, sigma or 0.0)
    entry = {"gated": metric in gated_metrics(workload), "status": "ok" if ok else "insufficient_samples",
             "n": n, "units_n": len(unit_stats), "noise_units": len(noisy), "missing": missing, "excluded": excluded,
             "p50": nearest_rank(values, 50) if n else None, "p95": nearest_rank(values, 95) if n else None,
             "min": min(values) if n else None, "max": max(values) if n else None,
             "mean": math.fsum(values) / n if n else None,
             "p95_status": "gated" if n >= P95_MIN_N else "reported",
             "sigma": sigma, "sigma_class": sigma_class, "sigma_pooled": sigma_pooled if noisy else None,
             "sigma_family": family_of(workload), "scale": scale, "rel": tol["rel"] if ok else None, "abs": tol["abs"],
             "allowed_ratio": 1 + tol["rel"] + tol["abs"] / scale if ok else None,
             "allowed_p95": None, "units": unit_stats}
    if ok and n >= P95_MIN_N:
        entry["allowed_p95"] = entry["p95"] * (1 + tol["rel"]) + tol["abs"]
    return entry


def _rate_summary(rates: List[float]) -> dict:
    return {"n": len(rates), "median": statistics.median(rates) if rates else None}


def latency_stats(records: Sequence[dict]) -> dict:
    coll = _collect(records)
    pooled = {m: family_sigmas({w: coll[w]["units"][m] for w in coll}) for m in METRICS}
    return {w: {m: _describe(w, m, coll[w]["units"][m], coll[w]["missing"][m], coll[w]["excluded"], pooled[m][w])
                for m in METRICS}
            for w in CLASS_ORDER if w in coll}


def output_rates(records: Sequence[dict]) -> dict:
    coll = _collect(records)
    return {w: _rate_summary(coll[w]["rates"]) for w in CLASS_ORDER if w in coll}


def _record_checks(rec: dict) -> List[dict]:
    return [c for t in rec.get("turns") or [] for c in t.get("checks") or []]


def complete_records(case: dict) -> int:
    """Records of a case (quality_stats' per-case counts) with no error and at least one check."""
    return case["repeats"] - case["errors"] - case["no_checks"]


def conditional_checks(case: dict) -> List[str]:
    """The checks a case did not run in every complete record: the harness emits some only when something
    happened (job_completed only when a turn made a file), so their absence is not a failure."""
    full = complete_records(case)
    return [name for name, runs in case["check_runs"].items() if runs < full]


def check_failures(case: dict, name: str) -> int:
    """Records that fail check `name`: errored ones, and complete ones in which it ran and failed at least once."""
    runs = case["check_runs"].get(name, 0)
    return case["errors"] + runs - case["check_passes"].get(name, 0)


def quality_stats(records: Sequence[dict]) -> dict:
    names: Dict[str, set] = {}
    for rec in records:
        names.setdefault(rec["id"], set()).update(c["check"] for c in _record_checks(rec))
    acc: Dict[str, dict] = {}
    checks: Dict[str, List[int]] = {}
    fast_turns = fast_thinking = fast_unchecked = 0
    thinking_ids: List[str] = []
    unchecked_ids: List[str] = []
    for rec in records:
        cid = rec["id"]
        w, effort = workload_of(rec), rec["effort"]
        a = acc.get(cid)
        if a is None:
            a = acc[cid] = {"workload": w, "effort": effort, "category": rec.get("category"), "repeats": 0,
                            "passes": 0, "score": Fraction(0), "errors": 0, "no_checks": 0, "failing": Counter(),
                            "check_passes": {name: 0 for name in sorted(names[cid])},
                            "check_runs": {name: 0 for name in sorted(names[cid])}, "failed_turns": 0, "turns": 0}
        elif (a["workload"], a["effort"]) != (w, effort):
            raise BaselineError(f"case {cid}: records disagree on workload/effort ({a['workload']}/{a['effort']} "
                                f"vs {w}/{effort}); the runs used different eval-set versions")
        rec_checks = _record_checks(rec)
        ok = sum(1 for c in rec_checks if c["ok"])
        for c in rec_checks:
            tally = checks.setdefault(c["check"], [0, 0])
            tally[0] += c["ok"]
            tally[1] += 1
            if not c["ok"]:
                a["failing"][c["check"]] += 1
        errored = bool(rec.get("error"))
        a["repeats"] += 1
        if errored:
            a["errors"] += 1
        elif not rec_checks:
            a["no_checks"] += 1
        else:
            a["score"] += Fraction(ok, len(rec_checks))
            a["passes"] += ok == len(rec_checks)
        for name in a["check_passes"]:
            runs = [c["ok"] for c in rec_checks if c["check"] == name]
            a["check_runs"][name] += (not errored) and bool(runs)
            a["check_passes"][name] += (not errored) and bool(runs) and all(runs)
        turns = rec.get("turns") or []
        if errored and not turns:
            a["turns"] += 1
            a["failed_turns"] += 1
        else:
            a["turns"] += len(turns)
            a["failed_turns"] += sum(_turn_failed(rec, t) for t in turns)
        if effort == "fast":
            for t in turns:
                fast_turns += 1
                th = [c for c in t.get("checks") or [] if c["check"] == THINKING_CHECK]
                if not th:
                    fast_unchecked += 1
                    if cid not in unchecked_ids:
                        unchecked_ids.append(cid)
                elif not all(c["ok"] for c in th):
                    fast_thinking += 1
                    if cid not in thinking_ids:
                        thinking_ids.append(cid)
    cases = {}
    for cid, a in acc.items():
        exact = a["score"] / a["repeats"]
        cases[cid] = {"workload": a["workload"], "effort": a["effort"], "category": a["category"],
                      "repeats": a["repeats"], "passes": a["passes"], "pass_rate": a["passes"] / a["repeats"],
                      "mean_score": float(exact), "mean_score_exact": str(exact),
                      "errors": a["errors"], "no_checks": a["no_checks"],
                      "failed_turns": a["failed_turns"], "turns": a["turns"],
                      "check_passes": a["check_passes"], "check_runs": a["check_runs"],
                      "failing_checks": dict(sorted(a["failing"].items(), key=lambda kv: (-kv[1], kv[0])))}
    overall = sum((Fraction(c["mean_score_exact"]) for c in cases.values()), Fraction(0)) / len(cases) \
        if cases else None
    return {"cases": cases,
            "checks": {name: {"passed": p, "total": t, "rate": p / t} for name, (p, t) in sorted(checks.items())},
            "overall_mean_score": None if overall is None else float(overall),
            "overall_mean_score_exact": None if overall is None else str(overall),
            "fast_turns": fast_turns, "fast_thinking_turns": fast_thinking,
            "fast_thinking_case_ids": thinking_ids, "fast_unchecked_turns": fast_unchecked,
            "fast_unchecked_case_ids": unchecked_ids}


# ------------------------------------------------------- baseline integrity --

_FRACTION = re.compile(r"[0-9]+(?:/[1-9][0-9]*)?")


def _exact(value, where: str) -> Fraction:
    if not isinstance(value, str) or not _FRACTION.fullmatch(value):
        raise BaselineError(f"{where}: not an exact fraction string ({value!r:.40})")
    return Fraction(value)


def _same(a, b) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(a, b, rel_tol=FLOAT_RTOL, abs_tol=1e-12)
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    return type(a) is type(b) and a == b


def _expected_repeats(sources: Sequence[dict], cid: str) -> int:
    return sum(s["repeats"] for s in sources if not s["only"] or cid in s["only"])


def _verify_baseline(doc, where: str = "baseline") -> None:
    """Refuse a baseline whose derived numbers disagree with a recomputation from its own stored statistics and
    this script's constants (tampering, rot or a different version of the method)."""
    if not isinstance(doc, dict):
        raise BaselineError(f"{where}: not a JSON object")
    kind, schema = doc.get("kind"), doc.get("schema")
    if kind != BASELINE_KIND or type(schema) is not int or schema != BASELINE_SCHEMA:
        raise BaselineError(f"{where}: kind={kind!r} schema={schema!r}; only kind {BASELINE_KIND!r} schema "
                            f"{BASELINE_SCHEMA} (baseline.py freeze output) is accepted")
    if not _same(doc.get("constants"), json.loads(json.dumps(CONSTANTS))):
        raise BaselineError(f"{where}: frozen with different method constants than this baseline.py; re-freeze it "
                            "from its run directories")
    sources = doc.get("sources")
    if not isinstance(sources, list) or not sources or not all(isinstance(s, dict) for s in sources):
        raise BaselineError(f"{where}: `sources` must be a non-empty list of objects")
    for i, s in enumerate(sources):
        sw = f"{where}: sources[{i}]"
        _check_source(s, sw)
        if not isinstance(s.get("model_ids"), list) or not all(isinstance(m, str) for m in s["model_ids"]):
            raise BaselineError(f"{sw}: model_ids must be a list of strings")
        if not isinstance(s.get("only"), list):
            raise BaselineError(f"{sw}: only must be a list")
        if s.get("account") is not None and set(s["account"]) != set(SAFE_ACCOUNT_KEYS):
            raise BaselineError(f"{sw}: account keeps counts only ({', '.join(SAFE_ACCOUNT_KEYS)})")
        _check_public_source(s, sw)
    if _unique(s["eval_set_sha256"] for s in sources)[1:] or _unique(s["workers"] for s in sources)[1:]:
        raise BaselineError(f"{where}: sources differ in eval_set_sha256 or workers")
    if not _same(doc.get("conditions"), _pool_conditions(sources)):
        raise BaselineError(f"{where}: `conditions` disagree with `sources`")

    quality, latency = doc.get("quality"), doc.get("latency")
    if not isinstance(quality, dict) or not isinstance(quality.get("cases"), dict) or not quality["cases"] \
            or not isinstance(latency, dict):
        raise BaselineError(f"{where}: `quality.cases` or `latency` is missing")
    cases = quality["cases"]
    exacts = []
    for cid, c in cases.items():
        cw = f"{where}: quality.cases.{cid}"
        if not isinstance(c, dict):
            raise BaselineError(f"{cw}: not an object")
        reps = c.get("repeats")
        if not (_is_int(reps) and reps >= MIN_SAMPLES):
            raise BaselineError(f"{cw}: repeats must be an integer >= {MIN_SAMPLES}")
        if reps != _expected_repeats(sources, cid):
            raise BaselineError(f"{cw}: {reps} records, but the sources' repeats add up to "
                                f"{_expected_repeats(sources, cid)}")
        for key in ("passes", "errors", "no_checks"):
            if not (_is_int(c.get(key)) and 0 <= c[key] <= reps):
                raise BaselineError(f"{cw}: {key} must be an integer in 0..{reps}")
        if c["passes"] + c["errors"] + c["no_checks"] > reps:
            raise BaselineError(f"{cw}: passes + errors + no_checks exceed repeats")
        if not (isinstance(c.get("workload"), str) and c["workload"] in CLASS_ABS_S
                and isinstance(c.get("effort"), str)):
            raise BaselineError(f"{cw}: workload/effort missing or unknown")
        exact = _exact(c.get("mean_score_exact"), cw)
        if not 0 <= exact <= 1 or not _same(c.get("mean_score"), float(exact)) \
                or not _same(c.get("pass_rate"), c["passes"] / reps) or (c["passes"] == reps and exact != 1):
            raise BaselineError(f"{cw}: mean_score / pass_rate disagree with the exact counts")
        exacts.append(exact)
        cp = c.get("check_passes")
        if not isinstance(cp, dict) or not all(isinstance(k, str) and _is_int(v) and 0 <= v <= reps
                                               for k, v in cp.items()):
            raise BaselineError(f"{cw}: check_passes must map check names to integers in 0..{reps}")
        if not (_is_int(c.get("turns")) and _is_int(c.get("failed_turns")) and 0 <= c["failed_turns"] <= c["turns"]):
            raise BaselineError(f"{cw}: failed_turns must be an integer in 0..turns")
    if not _same(doc.get("repeats_per_case"), {cid: c["repeats"] for cid, c in cases.items()}):
        raise BaselineError(f"{where}: repeats_per_case disagrees with quality.cases")
    overall = sum(exacts, Fraction(0)) / len(exacts)
    if _exact(quality.get("overall_mean_score_exact"), f"{where}: quality") != overall \
            or not _same(quality.get("overall_mean_score"), float(overall)):
        raise BaselineError(f"{where}: overall_mean_score disagrees with the cases")
    ft, fth = quality.get("fast_turns"), quality.get("fast_thinking_turns")
    if not (_is_int(ft) and _is_int(fth) and 0 <= fth <= ft) or quality.get("fast_unchecked_turns") != 0:
        raise BaselineError(f"{where}: fast_turns / fast_thinking_turns / fast_unchecked_turns are not valid counts")
    checks = quality.get("checks")
    if not isinstance(checks, dict) or not all(
            isinstance(c, dict) and _is_int(c.get("passed")) and _is_int(c.get("total"))
            and 0 <= c["passed"] <= c["total"] and c["total"] > 0 and _same(c.get("rate"), c["passed"] / c["total"])
            for c in checks.values()):
        raise BaselineError(f"{where}: quality.checks must hold passed <= total counts and their rate")

    classes = {c["workload"] for c in cases.values()}
    if set(latency) != classes:
        raise BaselineError(f"{where}: latency classes {sorted(latency)} differ from the cases' classes "
                            f"{sorted(classes)}")
    stored: Dict[tuple, tuple] = {}
    for w, per in latency.items():
        if w not in CLASS_ABS_S or not isinstance(per, dict) or set(per) != set(METRICS):
            raise BaselineError(f"{where}: latency.{w} must be a known class with every metric")
        for m in METRICS:
            e = per[m]
            lw = f"{where}: latency.{w}.{m}"
            units = e.get("units") if isinstance(e, dict) else None
            if not isinstance(units, dict):
                raise BaselineError(f"{lw}: `units` is missing")
            samples = {}
            for u, s in units.items():
                cid = u.rsplit("/t", 1)[0]
                if not (isinstance(s, dict) and isinstance(s.get("samples"), list) and s["samples"]
                        and all(_is_number(v) and v >= 0 for v in s["samples"])):
                    raise BaselineError(f"{lw}: unit {u} needs a non-empty list of non-negative samples")
                if cid not in cases or cases[cid]["workload"] != w:
                    raise BaselineError(f"{lw}: unit {u} is not a turn of a {w} case of this baseline")
                if len(s["samples"]) > cases[cid]["repeats"]:
                    raise BaselineError(f"{lw}: unit {u} has more samples than its case has records")
                samples[u] = s["samples"]
            for key in ("missing", "excluded"):
                if not (_is_int(e.get(key)) and e[key] >= 0):
                    raise BaselineError(f"{lw}: {key} must be a count")
            stored[(w, m)] = (lw, e, samples)
    for m in METRICS:
        pooled = family_sigmas({w: samples for (w, mm), (_lw, _e, samples) in stored.items() if mm == m})
        for (w, mm), (lw, e, samples) in stored.items():
            if mm == m and not _same(e, _describe(w, m, samples, e["missing"], e["excluded"], pooled[w])):
                raise BaselineError(f"{lw}: stored statistics or allowed values disagree with a recomputation from "
                                    "its unit samples and this script's constants")
    rates = doc.get("output_tokens_per_s")
    if not isinstance(rates, dict) or not all(
            w in CLASS_ABS_S and isinstance(r, dict) and _is_int(r.get("n")) and r["n"] >= 0
            and (r.get("median") is None or _is_number(r["median"])) for w, r in rates.items()):
        raise BaselineError(f"{where}: output_tokens_per_s is malformed")


def load_baseline(path: str) -> dict:
    doc = _read_json(path)
    _verify_baseline(doc, path)
    return doc


# ------------------------------------------------------------ freeze/compare --

def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def freeze(records: Sequence[dict], *, frozen_at: Optional[str] = None) -> dict:
    """The baseline document for pooled case records (as load_runs returns them, each with its `_run`)."""
    records = list(records)
    if not records:
        raise BaselineError("no case records to freeze")
    for i, rec in enumerate(records):
        _check_record(rec, f"records[{i}]")
    sources = _public_sources(_check_pool(records, "baseline"), records)
    for s in sources:
        _check_public_source(s, f"baseline run {s['dir'] or '<run>'}")
    quality = quality_stats(records)
    thin = {cid: c["repeats"] for cid, c in quality["cases"].items() if c["repeats"] < MIN_SAMPLES}
    if thin:
        raise BaselineError("cases with fewer than " + str(MIN_SAMPLES) + " records cannot be frozen ("
                            + ", ".join(f"{cid}: {n}" for cid, n in thin.items()) + "); pool more runs")
    if quality["fast_unchecked_turns"]:
        raise BaselineError(f"{quality['fast_unchecked_turns']} Fast turn(s) carry no `{THINKING_CHECK}` check "
                            f"(cases {', '.join(quality['fast_unchecked_case_ids'])}); the harness always emits one, "
                            "so these runs came from a different harness")
    doc = {
        "kind": BASELINE_KIND, "schema": BASELINE_SCHEMA,
        "frozen_at": frozen_at or _now(),
        "sources": sources,
        "conditions": _pool_conditions(sources),
        "repeats_per_case": {cid: c["repeats"] for cid, c in quality["cases"].items()},
        "quality": quality,
        "latency": latency_stats(records),
        "output_tokens_per_s": output_rates(records),
        "constants": json.loads(json.dumps(CONSTANTS)),
        "method": dict(METHOD),
    }
    _verify_baseline(json.loads(json.dumps(doc)), "the baseline just frozen")
    return doc


def _fmt_s(v) -> str:
    return "n/a" if v is None else f"{v:.2f}"


def _fmt_rate(v) -> str:
    return "n/a" if v is None else f"{float(v):.3f}"


def _frac(f: Fraction) -> str:
    return f"{f.numerator}/{f.denominator}"


def _compare_latency(base_lat: dict, coll: Dict[str, dict], fails: List[str], insufficient: List[dict]) -> tuple:
    out: Dict[str, dict] = {}
    new_units: List[str] = []
    for w in CLASS_ORDER:
        if w not in base_lat and w not in coll:
            continue
        out[w] = {}
        for m in METRICS:
            b = (base_lat.get(w) or {}).get(m)
            b_units = (b or {}).get("units") or {}
            c_all = coll[w]["units"][m] if w in coll else {}
            new_units += [u for u in c_all if u not in b_units and u not in new_units]
            c_units = {u: vs for u, vs in c_all.items() if u in b_units}
            c_vals = [v for vs in c_units.values() for v in vs]
            common = [u for u in b_units if u in c_units]
            gated = m in gated_metrics(w)
            entry = {"gated": gated, "baseline_n": (b or {}).get("n", 0), "candidate_n": len(c_vals),
                     "units_compared": len(common), "baseline_units": len(b_units),
                     "candidate_missing": coll[w]["missing"][m] if w in coll else 0,
                     "candidate_excluded": coll[w]["excluded"] if w in coll else 0,
                     "baseline_p50": (b or {}).get("p50"), "baseline_p95": (b or {}).get("p95"),
                     "baseline_max": (b or {}).get("max"),
                     "candidate_p50": nearest_rank(c_vals, 50) if c_vals else None,
                     "candidate_p95": nearest_rank(c_vals, 95) if c_vals else None,
                     "candidate_max": max(c_vals) if c_vals else None,
                     "sigma": (b or {}).get("sigma"), "scale": (b or {}).get("scale"),
                     "allowed_ratio": (b or {}).get("allowed_ratio"), "ratio": None,
                     "p95_status": "reported", "allowed_p95": None, "over": [], "insufficient_side": None}
            if b is None or b["status"] != "ok":
                entry.update(verdict="insufficient_samples", insufficient_side="baseline")
                if gated:
                    insufficient.append({"workload": w, "metric": m, "baseline_n": entry["baseline_n"],
                                         "noise_units": (b or {}).get("noise_units", 0),
                                         "candidate_n": entry["candidate_n"]})
            elif len(c_vals) < MIN_SAMPLES or not common:
                entry.update(verdict="insufficient_samples", insufficient_side="candidate")
                if gated:
                    fails.append(f"latency {w}/{m}: the candidate has {len(c_vals)} sample(s) in the baseline's "
                                 f"units (need {MIN_SAMPLES}); the baseline had {b['n']}")
            else:
                ratio = math.exp(math.fsum(_ln(statistics.median(c_units[u])) - _ln(b_units[u]["median"])
                                           for u in common) / len(common))
                entry["ratio"] = ratio
                if ratio > b["allowed_ratio"]:
                    entry["over"].append("ratio")
                if b["n"] >= P95_MIN_N and len(c_vals) >= P95_MIN_N:
                    entry["p95_status"], entry["allowed_p95"] = "gated", b["allowed_p95"]
                    if entry["candidate_p95"] > b["allowed_p95"]:
                        entry["over"].append("p95")
                entry["verdict"] = "fail" if entry["over"] else "pass"
                if gated and entry["over"]:
                    parts = []
                    if "ratio" in entry["over"]:
                        parts.append(f"median ratio {ratio:.3f} over {len(common)} unit(s) > allowed "
                                     f"{b['allowed_ratio']:.3f}")
                    if "p95" in entry["over"]:
                        parts.append(f"p95 {_fmt_s(entry['candidate_p95'])} s > allowed {_fmt_s(b['allowed_p95'])} s")
                    fails.append(f"latency {w}/{m}: " + "; ".join(parts))
            out[w][m] = entry
    return out, new_units


def _condition_changes(base: dict, cand: dict) -> List[dict]:
    out = []
    for field in ("labels", "model_ids", "harness_commits"):
        if sorted(map(str, base[field])) != sorted(map(str, cand[field])):
            out.append({"field": field, "baseline": base[field], "candidate": cand[field],
                        "baseline_labels": base["labels"], "candidate_labels": cand["labels"]})
    return out


def compare(baseline: dict, candidate_records: Sequence[dict], *, allow_insufficient: bool = False,
            compared_at: Optional[str] = None) -> dict:
    """Compare pooled candidate records (as load_runs returns them) with a frozen baseline. `passed` is False on
    any failure and, unless allow_insufficient, on any gated class/metric whose baseline side is insufficient."""
    _verify_baseline(baseline)
    records = list(candidate_records)
    if not records:
        raise BaselineError("no candidate case records to compare")
    for i, rec in enumerate(records):
        _check_record(rec, f"candidate records[{i}]")
    cand_sources = _public_sources(_check_pool(records, "candidate"), records)
    base_cond, cand_cond = baseline["conditions"], _pool_conditions(cand_sources)
    for key in ("eval_set_sha256", "workers"):
        if base_cond[key] != cand_cond[key]:
            raise BaselineError(f"the candidate's conditions.{key} {cand_cond[key]!r} differs from the baseline's "
                                f"{base_cond[key]!r}; the two are not comparable")
    cand = quality_stats(records)
    bq = baseline["quality"]["cases"]
    for cid, c in cand["cases"].items():
        if cid in bq and (bq[cid]["workload"], bq[cid]["effort"]) != (c["workload"], c["effort"]):
            raise BaselineError(f"case {cid}: workload/effort {c['workload']}/{c['effort']} differ from the "
                                f"baseline's {bq[cid]['workload']}/{bq[cid]['effort']}")
    fails: List[str] = []
    insufficient: List[dict] = []

    cases: Dict[str, dict] = {}
    missing: List[str] = []
    common: List[str] = []
    for cid, b in bq.items():
        c = cand["cases"].get(cid)
        if c is None:
            missing.append(cid)
            cases[cid] = {"verdict": "fail", "reason": "missing from the candidate",
                          "baseline_pass_rate": b["pass_rate"], "baseline_repeats": b["repeats"]}
            fails.append(f"case {cid}: missing from the candidate run(s)")
            continue
        common.append(cid)
        k = min(b["repeats"], c["repeats"])
        slack = Fraction(1, k)
        b_rate, c_rate = Fraction(b["passes"], b["repeats"]), Fraction(c["passes"], c["repeats"])
        regressed = c_rate < b_rate - slack
        if regressed:
            fails.append(f"case {cid}: pass rate {c['passes']}/{c['repeats']} < baseline {b['passes']}/"
                         f"{b['repeats']} - 1/{k}")
        check_fails = []
        conditional = conditional_checks(b)
        for name, bp in b["check_passes"].items():
            if name in conditional:
                bf, cf = check_failures(b, name), check_failures(c, name)
                if Fraction(cf, c["repeats"]) > Fraction(bf, b["repeats"]) + slack:
                    check_fails.append({"check": name, "rule": "failures", "baseline": f"{bf}/{b['repeats']}",
                                        "candidate": f"{cf}/{c['repeats']}"})
                    fails.append(f"case {cid} check {name} (conditional: absent from some baseline records): "
                                 f"{cf}/{c['repeats']} records fail it > baseline {bf}/{b['repeats']} + 1/{k}")
                continue
            cp = c["check_passes"].get(name, 0)
            if Fraction(cp, c["repeats"]) < Fraction(bp, b["repeats"]) - slack:
                check_fails.append({"check": name, "rule": "passes", "baseline": f"{bp}/{b['repeats']}",
                                    "candidate": f"{cp}/{c['repeats']}"})
                fails.append(f"case {cid} check {name}: {cp}/{c['repeats']} records pass < baseline {bp}/"
                             f"{b['repeats']} - 1/{k}")
        b_ft = Fraction(b["failed_turns"], b["turns"]) if b["turns"] else Fraction(0)
        c_ft = Fraction(c["failed_turns"], c["turns"]) if c["turns"] else Fraction(0)
        turns_failed = c_ft > b_ft + slack
        if turns_failed:
            fails.append(f"case {cid}: failed turns {c['failed_turns']}/{c['turns']} > baseline "
                         f"{b['failed_turns']}/{b['turns']} + 1/{k}")
        cases[cid] = {"verdict": "fail" if (regressed or check_fails or turns_failed) else "pass",
                      "baseline_pass_rate": b["pass_rate"], "baseline_repeats": b["repeats"],
                      "candidate_pass_rate": c["pass_rate"], "candidate_repeats": c["repeats"],
                      "floor": float(b_rate - slack), "pass_rate_verdict": "fail" if regressed else "pass",
                      "check_fails": check_fails, "conditional_checks": conditional,
                      "new_checks": [n for n in c["check_passes"] if n not in b["check_passes"]],
                      "baseline_failed_turns": f"{b['failed_turns']}/{b['turns']}",
                      "candidate_failed_turns": f"{c['failed_turns']}/{c['turns']}",
                      "failed_turns_verdict": "fail" if turns_failed else "pass",
                      "baseline_mean_score": b["mean_score"], "candidate_mean_score": c["mean_score"],
                      "candidate_failing_checks": c["failing_checks"], "candidate_errors": c["errors"]}
    new_cases = [cid for cid in cand["cases"] if cid not in bq]

    overall = {"cases_compared": len(common), "threshold": float(OVERALL_DROP), "baseline": None,
               "candidate": None, "drop": None, "drop_exact": None, "verdict": "fail"}
    if common:
        b_mean = sum((Fraction(bq[cid]["mean_score_exact"]) for cid in common), Fraction(0)) / len(common)
        c_mean = sum((Fraction(cand["cases"][cid]["mean_score_exact"]) for cid in common), Fraction(0)) / len(common)
        drop = b_mean - c_mean
        regressed = drop > OVERALL_DROP
        overall.update({"baseline": float(b_mean), "candidate": float(c_mean), "drop": float(drop),
                        "drop_exact": _frac(drop), "verdict": "fail" if regressed else "pass"})
        if regressed:
            fails.append(f"overall mean score {_fmt_rate(c_mean)} is {_frac(drop)} ({float(drop):.4f}) below the "
                         f"baseline {_fmt_rate(b_mean)} over {len(common)} cases (allowed drop {OVERALL_DROP})")
    else:
        fails.append("overall: the candidate has none of the baseline's cases")

    bqual = baseline["quality"]
    b_leak = Fraction(bqual["fast_thinking_turns"], bqual["fast_turns"]) if bqual["fast_turns"] else Fraction(0)
    c_leak = Fraction(cand["fast_thinking_turns"], cand["fast_turns"]) if cand["fast_turns"] else Fraction(0)
    leak = c_leak > b_leak
    thinking = {"baseline": f"{bqual['fast_thinking_turns']}/{bqual['fast_turns']}",
                "candidate": f"{cand['fast_thinking_turns']}/{cand['fast_turns']}",
                "candidate_case_ids": cand["fast_thinking_case_ids"],
                "candidate_unchecked_turns": cand["fast_unchecked_turns"],
                "candidate_unchecked_case_ids": cand["fast_unchecked_case_ids"],
                "verdict": "fail" if (leak or cand["fast_unchecked_turns"]) else "pass"}
    if leak:
        fails.append(f"fast thinking leak: {thinking['candidate']} Fast turns with reasoning, a higher rate than the "
                     f"baseline's {thinking['baseline']} (cases {', '.join(cand['fast_thinking_case_ids'])})")
    if cand["fast_unchecked_turns"]:
        fails.append(f"fast thinking: {cand['fast_unchecked_turns']} candidate Fast turn(s) carry no "
                     f"`{THINKING_CHECK}` check (cases {', '.join(cand['fast_unchecked_case_ids'])}); a turn that "
                     "was not checked cannot show it did not think")

    coll = _collect([r for r in records if r["id"] in bq])
    latency, new_units = _compare_latency(baseline["latency"], coll, fails, insufficient)
    b_rates = baseline["output_tokens_per_s"]
    rates = {w: {"baseline": b_rates.get(w), "candidate": _rate_summary(coll[w]["rates"]) if w in coll else None}
             for w in CLASS_ORDER if w in b_rates or w in coll}
    passed = not fails and (allow_insufficient or not insufficient)
    return {
        "kind": REPORT_KIND, "schema": REPORT_SCHEMA, "compared_at": compared_at or _now(),
        "passed": passed, "allow_insufficient": allow_insufficient,
        "fails": fails, "insufficient": insufficient,
        "baseline": {"frozen_at": baseline.get("frozen_at"), "sources": baseline["sources"],
                     "conditions": base_cond},
        "candidate": {"sources": cand_sources, "conditions": cand_cond, "records": len(records)},
        "condition_changes": _condition_changes(base_cond, cand_cond),
        "missing_cases": missing, "new_cases": new_cases, "new_units": new_units,
        "cases": cases, "overall": overall, "fast_thinking": thinking, "latency": latency,
        "output_tokens_per_s": rates,
        "method": {k: METHOD[k] for k in METHOD if k.startswith("compare_") or k == "false_block"},
    }


# ----------------------------------------------------------------- markdown --

def _cell(value) -> str:
    return str(value).replace("\r", " ").replace("\n", " ").replace("|", "\\|")


def _row(cells: Iterable) -> str:
    return "| " + " | ".join(_cell(c) for c in cells) + " |"


def _table(header: Sequence[str], rows: Iterable[Sequence]) -> List[str]:
    return [_row(header), _row(["---"] * len(header))] + [_row(r) for r in rows]


def _failing_text(case: dict, top: int = 3) -> str:
    parts = []
    if case.get("errors"):
        parts.append(f"error x{case['errors']}")
    if case.get("no_checks"):
        parts.append(f"no checks x{case['no_checks']}")
    parts += [f"{name} x{n}" for name, n in list((case.get("failing_checks") or {}).items())[:top]]
    return ", ".join(parts) or "none"


def _fmt_num(v, digits: int = 3) -> str:
    return "n/a" if v is None else f"{v:.{digits}f}"


def _sigma_source(e: dict) -> str:
    """Which figure the sigma used came from: the class's own units, or the units of its effort family."""
    if e["sigma"] is None:
        return "n/a"
    return "class" if e["sigma_pooled"] is None or e["sigma_class"] >= e["sigma_pooled"] else \
        f"{e['sigma_family']} family"


def render_markdown(baseline: dict) -> str:
    _verify_baseline(baseline)
    q, sources = baseline["quality"], baseline["sources"]
    cond = baseline["conditions"]
    out = ["## Eval-set baseline", "",
           f"Frozen {baseline.get('frozen_at')} by `scripts/aiq/baseline.py` from {len(sources)} run(s). Every "
           "number below holds only under the conditions listed here.", "", "### Conditions", "",
           f"Eval set sha256 `{cond['eval_set_sha256']}`; workers {cond['workers']}; model ids "
           f"{', '.join(f'`{m}`' for m in cond['model_ids']) or 'not reported'}.", ""]
    for s in sources:
        only, acct = s.get("only"), s.get("account")
        acct_text = ("not checked" if not acct or not acct.get("checked") else
                     f"{acct.get('conversations')} other conversations, {acct.get('facts')} saved facts"
                     + (" (used account allowed)" if acct.get("allowed_used") else ""))
        out.append(f"- run `{_cell(s.get('dir'))}`: label \"{_cell(s.get('label'))}\"; started {s.get('started')}; "
                   f"finished {s.get('finished')}; harness commit `{s.get('harness_commit')}`; repeats "
                   f"{s.get('repeats')}; cases {', '.join(only) if only else 'all'}; account {acct_text}")
    n_records = sum(c["repeats"] for c in q["cases"].values())
    out += ["", "### Quality", "",
            f"Overall mean case score {_fmt_rate(q['overall_mean_score'])} (exact {q['overall_mean_score_exact']}) "
            f"over {len(q['cases'])} cases ({n_records} records). Fast turns with reasoning (`{THINKING_CHECK}` "
            f"failed): {q['fast_thinking_turns']} of {q['fast_turns']}.", ""]
    out += _table(["Case", "Workload", "Effort", "Repeats", "Pass rate", "Mean score", "Failed turns",
                   "Failing checks (most often)"],
                  [[cid, c["workload"], c["effort"], c["repeats"], _fmt_rate(c["pass_rate"]),
                    _fmt_rate(c["mean_score"]), f"{c['failed_turns']}/{c['turns']}", _failing_text(c)]
                   for cid, c in q["cases"].items()])
    out += ["", "### Latency by workload class", "",
            "Seconds. " + METHOD["latency_unit"] + " " + METHOD["percentile"] + " " + METHOD["noise"] + " "
            + METHOD["ratio"] + " " + METHOD["tolerance"] + " " + METHOD["p95"] + " " + METHOD["gated"] + " "
            + METHOD["insufficient"], ""]
    rows = []
    for w, per in baseline["latency"].items():
        for m in METRICS:
            e = per[m]
            if e["status"] == "ok":
                allowed = [_fmt_num(e["allowed_ratio"]),
                           _fmt_s(e["allowed_p95"]) if e["allowed_p95"] is not None
                           else f"reported (n < {P95_MIN_N})"]
            else:
                allowed = ["insufficient samples", "n/a"]
            rows.append([w, m, "yes" if e["gated"] else "no", e["n"], e["units_n"], e["missing"], e["excluded"],
                         _fmt_s(e["p50"]), _fmt_s(e["p95"]), _fmt_s(e["min"]), _fmt_s(e["max"]),
                         _fmt_num(e["sigma"]), _sigma_source(e), _fmt_s(e["scale"])] + allowed)
    out += _table(["Class", "Metric", "Gated", "n", "Units", "Missing", "Excluded", "p50", "p95", "min", "max",
                   "Sigma (ln)", "Sigma from", "Scale", "Allowed ratio", "Allowed p95"], rows)
    out += ["", "### Output tokens per second", "", METHOD["output_rate"], ""]
    out += _table(["Class", "Samples", "Median tokens/s"],
                  [[w, r["n"], _fmt_num(r["median"], 1)] for w, r in baseline["output_tokens_per_s"].items()])
    out += ["", "### Check pass rate", "", METHOD["check_rate"], ""]
    out += _table(["Check", "Passed", "Total", "Rate"],
                  [[name, c["passed"], c["total"], _fmt_rate(c["rate"])] for name, c in q["checks"].items()])
    out += ["", "### Method", ""] + [f"- **{k}**: {_cell(v)}" for k, v in baseline["method"].items()]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------- CLI --

def _write_text(path: str, text: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _write_json(path: str, doc: dict) -> None:
    _write_text(path, json.dumps(doc, indent=2, ensure_ascii=False) + "\n")


def _guard_outputs(outputs: Sequence[tuple], inputs: Sequence[str]) -> None:
    """Refuse an output path that is one of the inputs (a run's results.json or summary.json, the baseline), or
    two outputs that are the same file."""
    real_in = {os.path.realpath(p) for p in inputs}
    seen: Dict[str, str] = {}
    for flag, path in outputs:
        if not path:
            continue
        real = os.path.realpath(path)
        if real in real_in:
            raise BaselineError(f"{flag} {path} is one of the inputs; refusing to overwrite it")
        if real in seen:
            raise BaselineError(f"{flag} and {seen[real]} name the same file ({path})")
        seen[real] = flag


def _run_inputs(run_dirs: Sequence[str]) -> List[str]:
    return [os.path.join(d, name) for d in run_dirs for name in ("results.json", "summary.json")]


def _summary(report: dict) -> str:
    waived = report["allow_insufficient"] and report["insufficient"]
    lines = [f"compare: {'PASSED' if report['passed'] else 'NOT PASSED'} "
             f"({len(report['fails'])} failure(s), {len(report['insufficient'])} gated class/metric(s) with "
             f"insufficient baseline samples{', waived' if waived else ''})"]
    lines.append("  baseline labels: " + "; ".join(repr(x) for x in report["baseline"]["conditions"]["labels"]))
    lines.append("  candidate labels: " + "; ".join(repr(x) for x in report["candidate"]["conditions"]["labels"]))
    lines += [f"  CHANGED {c['field']}: baseline {c['baseline']} -> candidate {c['candidate']}"
              for c in report["condition_changes"]]
    lines += [f"  FAIL {f}" for f in report["fails"]]
    lines += [f"  INSUFFICIENT latency {i['workload']}/{i['metric']}: baseline n={i['baseline_n']} with "
              f"{i['noise_units']} unit(s) of >= {MIN_UNIT_SAMPLES} samples (need {MIN_SAMPLES} samples and one "
              f"such unit); candidate n={i['candidate_n']}" for i in report["insufficient"]]
    if report["new_cases"] or report["new_units"]:
        lines.append(f"  NOT COMPARED (not in the baseline): cases {report['new_cases'] or 'none'}; turns "
                     f"{report['new_units'] or 'none'}")
    if not report["passed"] and any(f.startswith("latency ") for f in report["fails"]):
        lines.append("  latency tie-break: re-run the baseline commit in the same window and compare it with this "
                     "baseline; if it fails the same gate, the window is slow and the latency verdict is void")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="baseline.py", description="Freeze an eval-set baseline and compare a "
                                 "candidate run against it (B-04).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("freeze", help="freeze a baseline from run_evalset.py run directories")
    f.add_argument("run_dirs", nargs="+")
    f.add_argument("--out", required=True, help="baseline JSON to write")
    f.add_argument("--markdown", help="also write the BENCHMARK_RESULTS.md tables here")
    f.add_argument("--allow-mixed-conditions", action="store_true",
                   help="pool runs whose conditions.label differ (every label is listed); eval_set_sha256 and "
                        "workers must still match")
    c = sub.add_parser("compare", help="compare candidate run directories against a frozen baseline")
    c.add_argument("baseline")
    c.add_argument("run_dirs", nargs="+")
    c.add_argument("--allow-insufficient", action="store_true",
                   help="do not fail on gated class/metrics whose BASELINE side has too few samples (still listed); "
                        "a candidate side with too few samples always fails")
    c.add_argument("--allow-mixed-conditions", action="store_true",
                   help="pool candidate runs whose conditions.label differ")
    c.add_argument("--out", help="comparison report JSON to write")
    s = sub.add_parser("show", help="print a frozen baseline as markdown tables")
    s.add_argument("baseline")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "freeze":
            _guard_outputs([("--out", args.out), ("--markdown", args.markdown)], _run_inputs(args.run_dirs))
            records = load_runs(args.run_dirs, allow_mixed=args.allow_mixed_conditions)
            doc = freeze(records)
            markdown = render_markdown(doc) if args.markdown else None
            _write_json(args.out, doc)
            if markdown is not None:
                _write_text(args.markdown, markdown)
            print(f"froze {len(records)} records of {len(doc['quality']['cases'])} cases from "
                  f"{len(doc['sources'])} run(s) -> {args.out}")
            return 0
        if args.cmd == "compare":
            _guard_outputs([("--out", args.out)], [args.baseline] + _run_inputs(args.run_dirs))
            base = load_baseline(args.baseline)
            records = load_runs(args.run_dirs, allow_mixed=args.allow_mixed_conditions)
            report = compare(base, records, allow_insufficient=args.allow_insufficient)
            if args.out:
                _write_json(args.out, report)
            print(_summary(report))
            return 0 if report["passed"] else 1
        sys.stdout.write(render_markdown(load_baseline(args.baseline)))
        return 0
    except BaselineError as exc:
        print(f"baseline.py: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 — anything that is not a verdict must not exit 1
        print(f"baseline.py: cannot process the input ({type(exc).__name__}: {exc})", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
