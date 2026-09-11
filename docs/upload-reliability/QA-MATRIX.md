# Upload reliability — QA matrix

Every row is a scenario the release must pass, the kind of test that proves
it, where that test lives, and its last recorded outcome. "Outcome" is only
ever filled from a real run (command + counts in [EVIDENCE.md](EVIDENCE.md));
a row without one is unverified, and the release notes say so.

Legend for kind: **U** unit (vitest / pytest, no I/O), **A** API contract
(pytest through the app's TestClient, real database), **H** browser harness
(the real ChatApp through `frontend/tests/_wireHarness.tsx`, network stubbed
with controlled ordering), **R** real media through the running stack
(`scripts/qa_upload_matrix.py` on the isolated stack), **F** fault injection (a stub that fails on cue).

| # | scenario | kind | test | outcome |
|---|---|---|---|---|
| 1 | one MP4 from selection to a persisted answer, through the Next proxies | R | `qa_upload_matrix.py` case 1 | pass — e2e ×2 (50.4 s, 2,449 chars) |
| 2a | chunked upload just below and above the 90 MiB threshold | A + R | `test_init_records_the_expectation…`; e2e case 2a with the 89.6/98.7 MB fixtures | pass — e2e ×2 |
| 2b | rejection at the configured size limit, before any byte | A + R | `test_init_refuses_an_oversize_declaration_before_any_byte`; e2e 2b | pass — 413, e2e ×2 |
| 3a | mixed attachments, identity by attachment_id not index | H | `upload-lifecycle.test.tsx` — every chip carries its own attachment_id through submit | pass |
| 3b | duplicate filenames in one turn | H | `upload-lifecycle.test.tsx` — resends every video by reference when the ids are there | pass |
| 3c | one failed attachment among successful siblings; explicit choice, no silent subset | H | `chat-recovery.test.tsx` — keeps the words and both files, names the missing one, sends only what landed; `upload-lifecycle.test.tsx` — names the ones that never landed | pass |
| 3d | 20 MB + 200 MB pairing (safe stand-in for 20/400) | R | `qa_upload_matrix.py --heavy` case 3d | pass — 174.2 s, one answer |
| 4a | reload during early upload | H | `chat-recovery.test.tsx` — shows progress and no notice; the poll may not force-load over it | pass |
| 4b | reload during a chunk | A + R | `test_a_part_interrupted_mid_body_is_not_a_part`; e2e 4b (raw-socket cut) | pass — e2e ×2 |
| 4c | reload between chunks → resume sends only the rest | A + R | `test_parts_out_of_order_and_the_discovery_call`, `test_the_discovery_call_reports_what_the_disk_holds`; e2e 4c | pass — 1 of 2 parts resent |
| 4d | reload during finalization → retried complete replays | A + R | `test_complete_twice_answers_from_the_stored_result`; e2e 4d | pass — identical replay |
| 5a | upload committed, acknowledgement lost → retry recovers the same session | A + R | `test_a_rejected_finalisation_is_terminal_and_replays`; e2e 5a | pass |
| 5b | chat accepted, acknowledgement lost → same intent attaches, no second generation | A + R | `test_same_intent_twice_while_live_attaches_to_the_one_generation`; e2e 5b | pass — one generation, one answer |
| 6a | reload during probing / transcription / frames → re-attaches | R | `qa_upload_matrix.py` case 6 | pass — 45.7 s, no restart |
| 6b | navigate away and back during analysis → same job | R | case 6 asserts `video_analyses.attempt` unchanged | pass |
| 6c | reload during answer generation | H | `chat-recovery.test.tsx` — re-attaches with no notice when the server says the intent is live | pass |
| 7 | close every viewer; reopen after completion → answer in history, once | A + R | `test_post_chat_records_the_intent_and_the_durable_answer`; e2e case 7/8 | pass — 2,535 chars stored after the cut |
| 8 | disconnect at final delivery before the browser saves | A + H | `test_server_persist_then_client_put_does_not_duplicate`, `test_the_finished_answer_overwrites_a_partial_a_viewer_persisted`; `chat-recovery.test.tsx` — sends expected_updated_at, 409 re-applies only the local-only tail | pass |
| 9a | offline period mid-stream → reconnect, no false "never sent" | H | `chat-recovery.test.tsx` — a 502 from the attach is never read as "finished" | pass |
| 9b | failed /chat/active or /chat/requests → status unknown, not missing | H | `chat-recovery.test.tsx` — a 503 from /chat/active is status_unknown and the attach still happens; `chat-status-proxies.test.ts` | pass |
| 9c | proxy 502 / HTML error body handled | U | `upload-document.test.ts` — an HTML 502 becomes a safe sentence | pass |
| 9d | session expiry mid-upload | H | `auth-streams-401.test.ts` | pass |
| 10a | double-click Send → one intent, one generation | H + R | `chat-recovery.test.tsx` — survives a double click as ONE post with ONE intent id; e2e 5b/10a | pass |
| 10b | repeated Retry → no duplicate answers | H | `chat-recovery.test.tsx` — the intent is REUSED when an unsent turn is sent again | pass |
| 10c | two tabs on one conversation → both attach, one generation | A | `test_request_status_reports_live_and_unpersisted`, `test_same_intent_twice_while_live_attaches_to_the_one_generation` | pass |
| 10d | concurrent complete / concurrent analysis of one file | A | `test_two_concurrent_completes_finalise_once`; `test_a_second_runner_leaves_a_leased_run_to_its_owner` | pass |
| 11a | orchestrator restart mid-generation → interrupted → reattach resumes | A + R | `test_startup_marks_open_requests_interrupted`, `test_a_lost_request_is_resumed_by_attach_under_a_new_attempt`; **`scripts/recovery-tests/restart_drill.py`** | **pass — a real container restart: back in 6.3 s, attempt 2, one answer** |
| 11b | orchestrator restart mid-analysis → stage resume, no double run | A + R | `test_the_run_heartbeats_its_lease_and_startup_requeue_respects_it`, `test_a_live_lease_is_not_requeued_and_an_expired_one_is`; the drill | pass — 2 durable stages kept |
| 11c | bounded retries during a dependency outage | F | `test_a_busy_engine_is_retried_and_the_clip_still_lands`, `test_a_clip_that_stays_unavailable_counts_once_against_the_threshold` | pass |
| 12a | missing final chunk | A + R | `test_a_missing_final_part_is_a_refusal_not_a_shorter_file`, `test_a_hole_in_the_middle_is_listed`; e2e 4b | pass — 409 naming the part |
| 12b | partial chunk not accepted | A + R | `test_a_part_interrupted_mid_body_is_not_a_part`; e2e | pass |
| 12c | repeated part counts once | A | `test_a_repeated_part_counts_once` | pass |
| 12d | repeated completion | A + R | `test_complete_twice_answers_from_the_stored_result`; e2e | pass |
| 12e | corrupt media, missing tracks, unsupported container | R | e2e case 12e; `scripts/video_edge_cases.sh` (from the 2026-09-09 run) | pass — a sentence, not a stack trace |
| 12f | malicious filename / path input | A + R | `test_a_forged_or_malformed_id_is_…`; e2e 12f | pass — stored as `passwd.mp4` |
| 13a | cross-user access to a session (get/part/complete/delete) | A + R | `test_another_user_sees_…`; e2e 13a | pass — 404 on all four verbs |
| 13b | cross-user access to a request / attach / stop | A | `test_attach_never_resumes_someone_elses_request`, `test_request_status_shape_and_ownership` | pass |
| 13c | feature permission (video off for a member) | A | `test_video_upload_is_gated_per_member_then_per_deployment` | pass |
| 13d | expiry cleanup leaves active work alone | A | `test_a_finalizing_session_survives_even_when_backdated`, `test_nothing_under_the_video_data_dir_is_swept`, `test_the_analysis_source_survives_the_sweep_because_it_is_a_hard_link` | pass |
| 14a | old history rows (send_state only, or neither) | U | `unsent-turn.test.tsx`; `chat-recovery.test.tsx` — warns only after the server has answered | pass |
| 14b | old browser (no intent_id) against the new backend | A + R | `test_an_old_client_without_an_intent_keeps_the_old_event_shape`; e2e 14b | pass |
| 15 | regression: chat, documents, images, Salesforce, research, voice, retry, history, reuse | U + A | the whole backend and frontend suites | pass — 3115 backend, 1920 frontend |
| R1 | the key races across many controlled timing points | H | `chat-recovery.test.tsx` race matrix | pass — 60 deterministic iterations (12 cuts × 5 scenarios) |

## Where each kind of evidence lives

`e2e ×2` means `scripts/qa_upload_matrix.py` run twice — once at the
orchestrator, once through the Next.js proxies — on the isolated stack
(`scripts/e2e-stack.sh`). Everything else is a named test file. The raw
output is in [EVIDENCE.md](EVIDENCE.md).

## Not run, and why

* **3d through the proxies.** The heavy pairing was run at the orchestrator
  only; the proxy path is proven for every other scenario including the
  98.7 MB chunked upload, so the remaining risk is a 200 MB body through the
  Next handler specifically.
* **The Cloudflare edge path.** Nothing here went through the public
  hostname, so the 100 MB edge wall is verified only by the part size
  (64 MiB) being under it by construction.
* **A real browser.** Every frontend scenario is driven through the real
  components with the network stubbed, not through a browser engine; there
  is no Playwright suite in this repo and adding one was out of scope.
* **Concurrency at scale.** One video at a time is the configured policy and
  what was measured; no multi-user load test was run, so no capacity claim
  is made.

## What a pass requires

No failure in the completed rows; no lost accepted upload or completed
answer in 7/8; no duplicate logical submission or result in 5/10; no
incorrect recovery warning in 4/9; no cross-user access in 13; no
unexplained regression in 15. Rows left without an outcome are listed in
the release notes as unverified.
