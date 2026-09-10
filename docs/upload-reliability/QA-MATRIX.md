# Upload reliability — QA matrix

Every row is a scenario the release must pass, the kind of test that proves
it, where that test lives, and its last recorded outcome. "Outcome" is only
ever filled from a real run (command + counts in [EVIDENCE.md](EVIDENCE.md));
a row without one is unverified, and the release notes say so.

Legend for kind: **U** unit (vitest / pytest, no I/O), **A** API contract
(pytest through the app's TestClient, real database), **H** browser harness
(the real ChatApp through `frontend/tests/_wireHarness.tsx`, network stubbed
with controlled ordering), **R** real media through the running stack
(`scripts/video_smoke.py`), **F** fault injection (a stub that fails on cue).

| # | scenario | kind | test | outcome |
|---|---|---|---|---|
| 1 | one MP4 from selection to a persisted answer, through the Next proxies | R | `scripts/video_smoke.py --via-frontend` (meeting_10min) | |
| 2a | chunked upload just below and above the 90 MB threshold | A + R | `test_uploads_resumable.py::test_threshold_*`; smoke with `fixture_89mb.mp4` / `fixture_91mb.mp4` | |
| 2b | rejection at the configured size limit, before any byte | A | `test_uploads_resumable.py::test_init_refuses_oversize` | |
| 3a | mixed attachments, out-of-order completion, identity by attachment_id not index | H | `frontend/tests/upload-lifecycle.test.tsx::out-of-order` | |
| 3b | duplicate filenames in one turn | H | `upload-lifecycle.test.tsx::duplicate-names` | |
| 3c | one failed attachment among successful siblings: siblings keep their ids; explicit choice, no silent subset | H | `upload-lifecycle.test.tsx::one-fails` | |
| 3d | 20 MB + 200 MB pairing (safe stand-in for 20/400) | R | smoke with `fixture_20mb.mp4` + `fixture_200mb.mp4` | |
| 4a | reload during early upload (single-shot) | H | `upload-recovery.test.tsx::reload-early` | |
| 4b | reload during a chunk | H + A | `upload-recovery.test.tsx::reload-mid-part`; `test_uploads_resumable.py::test_part_interrupted_is_not_accepted` | |
| 4c | reload between chunks → resume sends only the rest | H + A | `upload-recovery.test.tsx::resume-missing-parts`; `test_uploads_resumable.py::test_discovery_lists_accepted_parts` | |
| 4d | reload during finalization → retried complete replays | A | `test_uploads_resumable.py::test_complete_twice_same_result` | |
| 5a | upload committed, acknowledgement lost → retry recovers the same session | A | `test_uploads_resumable.py::test_complete_after_lost_ack` | |
| 5b | chat accepted, acknowledgement lost → same intent attaches, no second generation | A | `test_chat_requests.py::test_same_intent_attaches` | |
| 6a | reload during probing / transcription / frames → progress re-attaches | R + H | smoke `--reload-at transcript`; `chat-recovery.test.tsx::reattach-live` | |
| 6b | navigate away and back during analysis → same job, no restart | R | smoke; assert `video_analyses.attempt` unchanged | |
| 6c | reload during answer generation | H | `chat-recovery.test.tsx::reattach-generating` | |
| 7 | close every viewer; reopen after completion → answer in history, once | A + R | `test_chat_requests.py::test_answer_persisted_unattached`; smoke with `--detach` | |
| 8 | disconnect at final delivery before the browser saves → server keeps the answer, no duplicate on the browser's later PUT | A + H | `test_chat_requests.py::test_server_persist_then_client_put_no_duplicate`; `chat-recovery.test.tsx::late-put` | |
| 9a | offline period mid-stream → reconnect, no false "never sent" | H | `chat-recovery.test.tsx::offline-then-back` | |
| 9b | failed /chat/active or /chat/requests → "status unknown", not "missing" | H | `chat-recovery.test.tsx::status-unknown` | |
| 9c | proxy 502/HTML error body handled | H | `upload-recovery.test.tsx::html-error` | |
| 9d | session expiry mid-upload → sign-in, draft kept | H | `upload-recovery.test.tsx::session-expiry` | |
| 10a | double-click Send → one intent, one generation | H + A | `chat-recovery.test.tsx::double-send`; `test_chat_requests.py::test_same_intent_attaches` | |
| 10b | repeated Retry → no duplicate answers | H | `chat-recovery.test.tsx::retry-twice` | |
| 10c | two tabs on one conversation → both attach, one generation | A | `test_chat_requests.py::test_two_attachers_one_generation` | |
| 10d | concurrent complete / concurrent analysis of one file | A | `test_uploads_resumable.py::test_concurrent_complete`; `test_chat_requests.py::test_lease_second_runner_yields` | |
| 11a | orchestrator restart mid-generation → request interrupted → reattach resumes | A | `test_chat_requests.py::test_restart_marks_interrupted_and_attach_resumes` | |
| 11b | orchestrator restart mid-analysis → stage resume, no double run | A | `test_video_understanding.py` (cache) + `test_upload_reliability_schema.py::test_a_live_lease…` | |
| 11c | bounded retries during inference outage | F | `test_chat_requests.py::test_resume_bounded` | |
| 12a | missing final chunk | A | `test_uploads_resumable.py::test_missing_final_part_is_refused` | |
| 12b | partial chunk (body cut) not accepted | A | `test_uploads_resumable.py::test_part_interrupted_is_not_accepted` | |
| 12c | repeated part counts once | A | `test_uploads_resumable.py::test_repeated_part_counts_once` | |
| 12d | repeated completion | A | `test_uploads_resumable.py::test_complete_twice_same_result` | |
| 12e | corrupt media / no audio / no video / unsupported | R | `scripts/video_edge_cases.sh` (corrupt, silent, vp9) | |
| 12f | malicious filename / path input | A | `test_uploads_resumable.py::test_filename_is_basename_only` | |
| 13a | cross-user access to a session (get/part/complete/delete) | A | `test_uploads_resumable.py::test_cross_user_404` | |
| 13b | cross-user access to a request / attach / stop | A | `test_chat_requests.py::test_cross_user_404` | |
| 13c | feature permission (video off for member) | A | `test_video_understanding.py::test_video_upload_is_gated…` | |
| 13d | expiry cleanup leaves active sessions alone | A | `test_uploads_resumable.py::test_sweep_leaves_live_sessions` | |
| 14a | old history rows (no intent, send_state only) render and behave | U | `unsent-turn.test.tsx`, `chat-recovery.test.tsx::legacy-rows` | |
| 14b | old browser (no intent_id) against the new backend | A | `test_chat_requests.py::test_missing_intent_is_minted` | |
| 15 | regression: normal chat, document/image uploads, Salesforce mode, web research, voice, retry/regenerate, history, attachment reuse | U + A | full frontend suite; full orchestrator suite | |
| R1 | key races repeated ≥ 50 timing points (reload / lost ack / late PUT) | H (parameterised) | `chat-recovery.test.tsx::races` | |

## Controlled ordering, not sleeps

The harness stubs `fetch`. A race is expressed as a **barrier**: the stub
returns a promise the test resolves on cue (e.g. "the part PUT has been
received by the server but its response is withheld; now reload"). Timing
points are enumerated (`for (const cut of CUTS)`), each a deterministic
place in the sequence, so a run either passes for every point or names the
one that failed.

## What a pass requires

No failure in the completed rows; no lost accepted upload or completed
answer in 7/8; no duplicate logical submission or result in 5/10; no
incorrect recovery warning in 4/9; no cross-user access in 13; no
unexplained regression in 15. Rows left without an outcome are listed in
the release notes as unverified.
