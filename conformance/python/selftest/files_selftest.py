"""Prove the Files conformance tests of BOTH SDK suites against the real
handlers, and prove they catch what they claim to (2026-09-13).

    TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/test_files_conformance \
      conformance/python/.venv/bin/python conformance/python/selftest/files_selftest.py \
        --orchestrator-python orchestrator/.venv/bin/python --node "$(command -v node)"

Run it with the conformance suite's own Python (openai, httpx2, pytest).
`--orchestrator-python` is a Python that can import the orchestrator (FastAPI,
psycopg, PIL); TEST_DATABASE_URL a private `test_*` Postgres database.

WHAT IT CHECKS, against `files_local_target.py` (read its docstring for what
that harness stubs):

1. built   — every test in tests/test_files_uploads.py and
             node test/live/files.test.mjs PASSES;
2. planned — the same target with the Files features forced `planned`: every
             test fails as XPASS(strict), so a test cannot pass quietly on a
             stack whose schema does not declare the feature;
3. defects — the target restarted with each deliberate defect in
             `files_local_target.DEFECTS`: EXACTLY the tests in `EXPECTED`
             fail, in each suite. A defect nobody catches, or a failure nobody
             expected (a flaky test), fails the self-test.

Node stages need Node >= 22 (openai-node 7.x) and `npm ci` in conformance/node.
Without them the self-test FAILS, unless `--no-node` says the Node suite is
deliberately out of this run. `--defects a,b` limits stage 3; `--no-defects`
skips it.

EVERY DEFECT HAS AN ENTRY. A defect in `files_local_target.DEFECTS` without
one in `EXPECTED` (or the reverse) fails the self-test before any target
starts: an unlisted defect is one nobody proved a test catches (2026-09-13,
review finding: eight defects had been added with no entry and never ran).
An empty Node set is allowed only where the Node suite has no test that could
see the defect, and says why in `NODE_BLIND`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

HERE = Path(__file__).resolve().parent
PY_SUITE = HERE.parent
NODE_SUITE = HERE.parents[1] / "node"
REPO = HERE.parents[2]

PY_FEATURES = ("files", "uploads", "uploads_resume", "files_model_input")
NODE_FEATURES = "files-api,uploads-chunked,file-input"

#: defect -> (Python test functions, Node test titles) that must fail — and no others.
#: Measured 2026-09-13 against the suites as they stand, then read one by one: each
#: set is the tests that guard that promise (md5_ignored also fails the failed-file
#: test, whose failed file is made with a wrong md5; splice_drops_files replaces the
#: whole splice, so the input_audio test fails with it).
EXPECTED: Dict[str, Tuple[Set[str], Set[str]]] = {
    "cross_project_file_lookup": (
        {"test_another_projects_file_id_answers_exactly_like_a_file_id_that_never_existed"},
        {"another project's file id answers retrieve, content and delete exactly like an id that never existed"},
    ),
    "part_sha256_ignored": (
        {"test_parts_carrying_their_sha256_complete_in_part_ids_order_with_md5_and_return_a_nested_file",
         "test_raw_put_parts_with_a_part_digest_complete_without_part_ids_in_part_number_order"},
        {"parts carrying their sha256 complete in part_ids order with md5, return a nested file, and a wrong sha256 is 400 checksum_mismatch",
         "raw PUT parts with X-Part-SHA256 or Content-Digest complete without part_ids in part_number order, and a wrong digest records nothing"},
    ),
    "state_conflict_says_retry": (
        {"test_a_replayed_complete_returns_the_same_upload_and_file_and_a_late_part_is_409_without_retry",
         "test_cancel_is_idempotent_and_a_cancelled_upload_refuses_parts_and_complete_with_409"},
        {"a replayed complete returns the same upload and file, and a late part is 409 upload_state_conflict with x-should-retry false",
         "cancel is idempotent and a cancelled upload refuses parts and complete with 409"},
    ),
    "splice_drops_files": (
        {"test_an_uploaded_png_is_seen_by_the_model_as_input_image_with_its_file_id",
         "test_an_uploaded_text_file_is_read_by_the_model_as_a_file_part_on_chat_completions",
         "test_an_uploaded_text_file_is_read_by_the_model_as_input_file_on_responses_in_auto_and_full_file_context",
         "test_an_uploaded_wav_is_processed_as_audio_and_the_model_reads_its_length_from_it_as_input_file_and_input_video",
         "test_inline_file_data_text_is_read_by_the_model_on_both_routes_without_an_upload",
         "test_input_audio_on_chat_completions_refuses_an_unknown_format_and_puts_a_wav_clips_transcript_into_the_prompt"},
        {"an uploaded PNG is seen by the model as input_image with its file_id",
         "an uploaded WAV is processed as audio and the model reads its length from it as input_file and input_video",
         "an uploaded text file is read by the model as a file part on /v1/chat/completions",
         "an uploaded text file is read by the model as input_file on /v1/responses, in auto and full file_context, and an unknown mode is 400",
         "inline file_data text is read by the model on both routes without an upload",
         "input_audio on /v1/chat/completions refuses an unknown format by name and puts a WAV clip's transcript into the prompt"},
    ),
    "md5_ignored": (
        {"test_a_failed_file_and_a_file_of_the_wrong_kind_are_refused_as_model_input_with_400_naming_the_file_id",
         "test_complete_with_a_wrong_md5_or_a_wrong_sha256_returns_the_file_which_then_ends_in_error_checksum_mismatch"},
        {"a failed file and a file of the wrong kind are refused as model input with 400 naming the file_id",
         "complete with a wrong md5 or a wrong sha256 returns the file, which then ends in error with checksum_mismatch"},
    ),
    "range_ignored": (
        {"test_content_serves_one_byte_range_as_206_answers_304_to_its_etag_and_416_past_the_end"},
        {"content honours one Range with 206, its own ETag with 304, a range past the end with 416, and the download headers"},
    ),
    "cancel_not_idempotent": (
        {"test_cancel_is_idempotent_and_a_cancelled_upload_refuses_parts_and_complete_with_409"},
        {"cancel is idempotent and a cancelled upload refuses parts and complete with 409"},
    ),
    "complete_not_replayable": (
        {"test_a_replayed_complete_returns_the_same_upload_and_file_and_a_late_part_is_409_without_retry"},
        {"a replayed complete returns the same upload and file, and a late part is 409 upload_state_conflict with x-should-retry false"},
    ),
    "list_order_ignored": (
        {"test_list_pages_newest_first_with_limit_and_after_until_has_more_is_false"},
        {"files.list pages newest first with limit and after, and has_more ends the paging"},
    ),
    "resume_view_lists_no_parts": (
        {"test_a_chunked_upload_cut_by_a_dropped_connection_resumes_in_a_new_client_from_the_servers_part_list",
         "test_raw_put_parts_with_a_part_digest_complete_without_part_ids_in_part_number_order",
         "test_resending_a_part_number_replaces_that_part_under_the_same_part_id",
         "test_upload_file_chunked_rides_out_a_connection_dropped_mid_part_and_no_part_number_is_lost"},
        {"a part cut off by a dropped connection is retried by the SDK and takes no part number of its own",
         "an upload cut by a dropped connection resumes in a fresh client from GET /uploads/{id}, which lists only whole parts",
         "raw PUT parts with X-Part-SHA256 or Content-Digest complete without part_ids in part_number order, and a wrong digest records nothing",
         "resending a part_number replaces that part under the same part id, and an unnumbered part then is 400 naming part_number"},
    ),
    "upload_lookup_ignores_project": (
        {"test_another_projects_upload_id_answers_retrieve_parts_put_complete_and_cancel_exactly_like_one_that_never_existed"},
        {"another project's upload id answers retrieve, parts, put, complete and cancel exactly like an id that never existed"},
    ),
    "model_input_ignores_project": (
        {"test_another_projects_file_id_as_model_input_is_the_same_404_as_an_id_that_never_existed_on_both_routes"},
        {"another project's file id as model input is the same 404 file_not_found as an id that never existed, on both routes"},
    ),
    "complete_sha256_ignored": (
        {"test_complete_with_a_wrong_md5_or_a_wrong_sha256_returns_the_file_which_then_ends_in_error_checksum_mismatch"},
        {"complete with a wrong md5 or a wrong sha256 returns the file, which then ends in error with checksum_mismatch"},
    ),
    "input_audio_transcript_dropped": (
        {"test_input_audio_on_chat_completions_refuses_an_unknown_format_and_puts_a_wav_clips_transcript_into_the_prompt"},
        {"input_audio on /v1/chat/completions refuses an unknown format by name and puts a WAV clip's transcript into the prompt"},
    ),
    "files_routes_skip_scope": (
        {"test_a_key_without_files_write_gets_the_same_403_for_a_real_and_a_random_upload_id_on_every_upload_route",
         "test_a_key_without_the_files_scopes_gets_the_same_403_for_a_real_and_a_random_file_id_on_every_file_route"},
        {"a key without files.write gets the same 403 for a real and a random upload id on every upload route",
         "a key without the files scopes gets the same 403 for a real and a random file id on every file route"},
    ),
    "model_input_skips_files_read": (
        {"test_a_key_without_files_read_gets_the_same_403_for_a_real_and_a_random_file_id_in_a_prompt_but_may_send_file_data"},
        {"a key without files.read gets the same 403 for a real and a random file id in a prompt, but may send file_data"},
    ),
    "failed_files_reach_the_model": (
        {"test_a_failed_file_and_a_file_of_the_wrong_kind_are_refused_as_model_input_with_400_naming_the_file_id"},
        {"a failed file and a file of the wrong kind are refused as model input with 400 naming the file_id"},
    ),
    "part_kind_unchecked": (
        {"test_a_failed_file_and_a_file_of_the_wrong_kind_are_refused_as_model_input_with_400_naming_the_file_id"},
        {"a failed file and a file of the wrong kind are refused as model input with 400 naming the file_id"},
    ),
    "media_blocks_dropped": (
        {"test_an_uploaded_wav_is_processed_as_audio_and_the_model_reads_its_length_from_it_as_input_file_and_input_video"},
        {"an uploaded WAV is processed as audio and the model reads its length from it as input_file and input_video"},
    ),
}

#: defect -> why the Node suite cannot see it. Empty on purpose: the two suites
#: are kept at parity, so a defect Python catches must fail a Node test too.
NODE_BLIND: Dict[str, str] = {}

_TAP = re.compile(r"^\s*(not ok|ok) \d+ - (.+?)(?: # (SKIP|TODO)(.*))?$")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def clean_env(**extra: str) -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TECHSARA_", "CONFORMANCE_"))}
    env.update(extra)
    return env


class Target:
    def __init__(self, args: argparse.Namespace, work: Path, defect: str) -> None:
        self.port = free_port()
        self.keys = work / f"keys-{defect}.json"
        self.env_file = work / f"node-{defect}.env"
        self.log = work / f"target-{defect}.log"
        command = [
            args.orchestrator_python, str(HERE / "files_local_target.py"), "--orchestrator", args.orchestrator,
            "--port", str(self.port), "--data-dir", str(work / "data"), "--keys-out", str(self.keys),
            "--env-out", str(self.env_file), "--defect", defect,
        ]
        self.proc = subprocess.Popen(command, cwd=args.orchestrator, stdout=open(self.log, "wb"), stderr=subprocess.STDOUT,
                                     env={**os.environ, "TEST_DATABASE_URL": args.dsn})
        deadline = time.monotonic() + 90
        while True:
            if self.proc.poll() is not None:
                raise SystemExit(f"the local target exited ({self.proc.returncode}); see {self.log}")
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/v1/openapi.json", timeout=2).read()
                return
            except OSError:
                if time.monotonic() > deadline:
                    self.stop()
                    raise SystemExit(f"the local target did not answer in 90 s; see {self.log}")
                time.sleep(0.3)

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()


def run_python(args: argparse.Namespace, target: Target, work: Path, label: str, *, planned: bool) -> Dict[str, str]:
    report = work / f"py-{label}.json"
    command = [sys.executable, "-m", "pytest", "tests/test_files_uploads.py", f"--keys-file={target.keys}",
               f"--conformance-report={report}", "-p", "no:cacheprovider", "-q"]
    if planned:
        for feature in PY_FEATURES:
            command += ["--feature", f"{feature}=planned"]
    proc = subprocess.run(command, cwd=PY_SUITE, capture_output=True, text=True, timeout=1800,
                          env=clean_env(TECHSARA_FILES_POLL_S="0.5", TECHSARA_FILES_PROCESSING_TIMEOUT_S="120",
                                        TECHSARA_FILES_EDGE_REFUSALS="origin"))
    (work / f"py-{label}.log").write_text(proc.stdout + proc.stderr)
    if not report.exists():
        return {"<run>": f"no report (exit {proc.returncode}); see py-{label}.log"}
    data = json.loads(report.read_text())
    return {r["test"].split("::")[-1]: r["result"] for r in data.get("results", [])}


def run_node(args: argparse.Namespace, target: Target, work: Path, label: str, *, planned: bool) -> Dict[str, str]:
    extra = {"TECHSARA_ENV_FILE": str(target.env_file), "CONFORMANCE_FILES_POLL_MS": "500",
             "CONFORMANCE_FILES_PROCESSING_TIMEOUT_MS": "120000", "CONFORMANCE_FILES_EDGE_REFUSALS": "origin"}
    if not planned:
        extra["CONFORMANCE_BUILT_FEATURES"] = NODE_FEATURES
    proc = subprocess.run(
        [args.node, "--test", "--test-concurrency=1", "--test-reporter=tap", "test/live/files.test.mjs"],
        cwd=NODE_SUITE, capture_output=True, text=True, timeout=1800, env=clean_env(**extra),
    )
    (work / f"node-{label}.log").write_text(proc.stdout + proc.stderr)
    results: Dict[str, str] = {}
    lines = proc.stdout.splitlines()
    for i, line in enumerate(lines):
        match = _TAP.match(line)
        if not match:
            continue
        if any("type: 'suite'" in follow for follow in lines[i + 1 : i + 6]):
            continue
        name = match.group(2).split(" [feature: ")[0]
        outcome = "FAIL" if match.group(1) == "not ok" else {"SKIP": "SKIP", "TODO": "XFAIL"}.get(match.group(3) or "", "PASS")
        results[name] = outcome
    return results or {"<run>": f"no TAP results (exit {proc.returncode}); see node-{label}.log"}


def node_usable(node: Optional[str]) -> Tuple[bool, str]:
    if not node:
        return False, "no node on PATH (pass --node)"
    try:
        version = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=20).stdout.strip()
    except OSError as exc:
        return False, f"{node}: {exc}"
    major = int(re.match(r"v(\d+)", version).group(1)) if re.match(r"v(\d+)", version) else 0
    if major < 22:
        return False, f"{node} is {version}; openai-node 7.x needs Node >= 22"
    if not (NODE_SUITE / "node_modules" / "openai").is_dir():
        return False, "conformance/node has no node_modules (run npm ci there)"
    return True, version


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--orchestrator", default=str(REPO / "orchestrator"))
    parser.add_argument("--orchestrator-python", default=str(REPO / "orchestrator" / ".venv" / "bin" / "python"))
    parser.add_argument("--node", default=shutil.which("node"))
    parser.add_argument("--dsn", default=os.environ.get("TEST_DATABASE_URL", ""))
    parser.add_argument("--defects", default=",".join(EXPECTED), help="comma-separated subset of the defects")
    parser.add_argument("--no-defects", action="store_true")
    parser.add_argument("--no-node", action="store_true", help="run the Python suite only, on purpose")
    parser.add_argument("--work", default="", help="keep logs and reports here (default: a temporary directory)")
    args = parser.parse_args()
    if not args.dsn:
        print("set TEST_DATABASE_URL to a private test_* database", file=sys.stderr)
        return 2
    sys.path.insert(0, str(HERE))
    from files_local_target import DEFECTS  # names only: the module imports no orchestrator code at top level

    unlisted, stale = sorted(set(DEFECTS) - set(EXPECTED)), sorted(set(EXPECTED) - set(DEFECTS))
    if unlisted or stale:
        print(f"files selftest FAILED: defects without an EXPECTED entry {unlisted}; EXPECTED entries without a defect {stale}")
        return 1
    unknown = [d for d in args.defects.split(",") if d and d not in EXPECTED]
    if unknown:
        print(f"unknown defect(s) {unknown}; known: {sorted(EXPECTED)}", file=sys.stderr)
        return 2
    for defect, (want_py, want_node) in EXPECTED.items():
        if not want_py or (not want_node and defect not in NODE_BLIND):
            print(f"files selftest FAILED: defect {defect} expects no failing test in "
                  f"{'python' if not want_py else 'node (and is not in NODE_BLIND)'}")
            return 1
    work = Path(args.work or tempfile.mkdtemp(prefix="files-selftest-")).resolve()
    work.mkdir(parents=True, exist_ok=True)
    use_node, node_detail = (False, "--no-node") if args.no_node else node_usable(args.node)
    print(f"work dir {work}; node: {node_detail}")
    problems: List[str] = []

    def expect(label: str, ok: bool, detail: str = "") -> None:
        print(f"{'ok  ' if ok else 'FAIL'} {label}{f' — {detail}' if detail and not ok else ''}")
        if not ok:
            problems.append(label)

    target = Target(args, work, "none")
    try:
        built = run_python(args, target, work, "built", planned=False)
        expect(f"python built: all {len(built)} Files tests PASS", bool(built) and set(built.values()) == {"PASS"},
               json.dumps({k: v for k, v in built.items() if v != "PASS"}))
        planned = run_python(args, target, work, "planned", planned=True)
        expect(f"python planned: all {len(planned)} are XPASS(strict)", len(planned) == len(built) and set(planned.values()) == {"XPASS(strict)"},
               json.dumps({k: v for k, v in planned.items() if v != "XPASS(strict)"}))
        node_names: Set[str] = set()
        if use_node:
            node_built = run_node(args, target, work, "built", planned=False)
            node_names = set(node_built)
            expect(f"node built: all {len(node_built)} Files tests PASS", bool(node_built) and set(node_built.values()) == {"PASS"},
                   json.dumps({k: v for k, v in node_built.items() if v != "PASS"}))
            node_planned = run_node(args, target, work, "planned", planned=True)
            expect(f"node planned: all {len(node_planned)} fail as XPASS(strict)",
                   len(node_planned) == len(node_built) and set(node_planned.values()) == {"FAIL"},
                   json.dumps({k: v for k, v in node_planned.items() if v != "FAIL"}))
        else:
            expect("node stages ran", bool(args.no_node), node_detail)
    finally:
        target.stop()

    if not args.no_defects:
        for defect in [d for d in args.defects.split(",") if d]:
            want_py, want_node = EXPECTED[defect]
            target = Target(args, work, defect)
            try:
                py = run_python(args, target, work, defect, planned=False)
                failed = {name for name, result in py.items() if result != "PASS"}
                expect(f"defect {defect}: python fails exactly {sorted(want_py)}", failed == want_py,
                       f"failed {sorted(failed)}")
                if use_node:
                    nd = run_node(args, target, work, defect, planned=False)
                    failed_node = {name for name, result in nd.items() if result != "PASS"}
                    expect(f"defect {defect}: node fails exactly {len(want_node)} test(s)", failed_node == want_node,
                           f"failed {sorted(failed_node)}; expected {sorted(want_node)}")
                    unknown = want_node - node_names
                    expect(f"defect {defect}: every expected node title exists", not unknown, f"unknown {sorted(unknown)}")
            finally:
                target.stop()

    if problems:
        print(f"files selftest FAILED: {len(problems)} expectation(s); logs in {work}")
        return 1
    print(f"files selftest ok; logs in {work}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
