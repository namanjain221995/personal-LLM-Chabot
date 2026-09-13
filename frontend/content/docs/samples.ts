/**
 * The literals every example on /docs is built from.
 *
 * ONE PLACE, because a reader copies what is in front of them. If the base
 * URL or the example key were retyped on twenty pages, nineteen of them
 * would eventually be wrong, and a documentation key that drifts into
 * something *shaped* like a real credential is the failure this file exists
 * to prevent.
 */

/**
 * The same-origin public edge (CONTRACT §1). `api.techsarasolutions.com` is
 * named in the contract as a later home for the surface; until it exists,
 * documenting it would hand every reader a hostname that does not resolve.
 */
export const API_BASE_URL = 'https://ai.techsarasolutions.com/v1';

/**
 * The main chat model, and the id every generic example uses (CONTRACT §15).
 * Kept as its own name because twenty pages were written against it and a
 * reader's first request should be to the model that answers in the chat app.
 */
export const MODEL_ID = 'techsara-35b';

/**
 * EVERY PUBLIC MODEL ID (2026-09-13, owner request: /v1 offers every model
 * TechSara runs). One place, so a page cannot spell an id the registry does
 * not declare — tests/docs-site.test.tsx refuses any `"model": "…"` in a
 * sample that is not one of these, and holds this list to CONTRACT §15.
 *
 * These are brand ids. The checkpoint behind each one is an operational
 * detail that never leaves the server (CONTRACT §15), which is why no page on
 * this site names it.
 */
export const VISION_MODEL_ID = 'techsara-8b-vision';
export const OCR_MODEL_ID = 'techsara-ocr';
export const EMBED_MODEL_ID = 'techsara-embed';
export const RERANK_MODEL_ID = 'techsara-rerank';
export const WHISPER_MODEL_ID = 'techsara-whisper';

export const MODEL_IDS = [
  MODEL_ID,
  VISION_MODEL_ID,
  OCR_MODEL_ID,
  EMBED_MODEL_ID,
  RERANK_MODEL_ID,
  WHISPER_MODEL_ID,
] as const;

/**
 * WHETHER A /v1 GENERATION CAN OUTLIVE THE CHAT APP'S WALL CLOCK — the second
 * honesty switch, after EXAMPLES_EXECUTED.
 *
 * 2026-09-13: the public ceiling for `max_output_tokens` on techsara-35b is
 * 1,000,000, and at 71-101 tok/s measured such an answer takes about three to
 * four hours. The per-request wall clock that lets it finish (CONTRACT §8.3)
 * is enforced inside `llm.stream_chat_events(wall_clock_s=…)`, and that
 * parameter lands in orchestrator/app/llm.py through a separate integration.
 * Until it does, every techsara-35b generation is still stopped at the chat
 * application's clock — 4,200 s, roughly 300,000-420,000 tokens — and a page
 * that promised a 1M answer as deliverable today would be the kind of stale
 * claim the EXAMPLES_EXECUTED history above warns about.
 *
 * So the long-output page and the changelog render `WALL_CLOCK_PENDING_NOTE`
 * while this is false, and tests/docs-site.test.tsx ties the value to llm.py
 * itself: the day `stream_chat_events` accepts `wall_clock_s`, the test fails
 * until this is flipped to `true` — nobody has to remember.
 */
export const LONG_OUTPUT_WALL_CLOCK_LIVE: boolean = false;

/** The caveat while the per-request wall clock is not yet enforced. */
export const WALL_CLOCK_PENDING_NOTE =
  '**Not yet deliverable end to end.** The 1,000,000 ceiling is accepted and ' +
  'clamped exactly as described here, but until the per-request wall clock ' +
  'ships, a techsara-35b generation is still stopped after 4,200 seconds ' +
  '(70 minutes) — roughly 300,000 to 420,000 tokens at the measured decode ' +
  'speed — and ends `failed` with code `timeout`, keeping the text it had ' +
  'produced. This notice disappears when that changes.';

/**
 * THE EXAMPLE KEYS — deliberately invalid, and invalid in the one way that
 * matters.
 *
 * Both tokens have a perfect SHAPE: the `tsk_live_` / `tsk_test_` prefix, a
 * 16-hex `public_id`, a 43-character urlsafe secret, a 6-character Base62
 * checksum — every offline check in orchestrator/app/apiplatform/keys.py
 * passes until the last one. The checksum is the literal text `EXAMPL`,
 * which is not the CRC32 of the rest, so `split_key()` returns None and the
 * platform answers `401 invalid_api_key` before it touches the database.
 *
 * Verified 2026-09-13 against the real validator:
 *
 *   keys.split_key(EXAMPLE_LIVE_KEY)  -> None   (real checksum: 1wPKxD)
 *   keys.split_key(EXAMPLE_TEST_KEY)  -> None   (real checksum: 2mdd9X)
 *
 * and, as the control, substituting the real checksum makes both parse — so
 * the checksum is provably the only thing wrong with them.
 *
 * WHY NOT JUST "tsk_live_xxx". Two reasons pulling the same way. A reader
 * needs to see the real anatomy of a key to recognise one in a config file
 * or a log; and a reader who pastes what they see must get a clean 401
 * rather than a confusing parse error or, far worse, a token that looks
 * plausible enough to be committed to a repository as a placeholder and then
 * flagged forever by a secret scanner. The words DO_NOT_USE and
 * THIS_IS_NOT_A_SECRET are in the secret itself for the human reading it.
 */
export const EXAMPLE_LIVE_KEY =
  'tsk_live_0123456789abcdef_EXAMPLE_KEY_DO_NOT_USE_THIS_IS_NOT_A_SECRETEXAMPL';

export const EXAMPLE_TEST_KEY =
  'tsk_test_fedcba9876543210_EXAMPLE_KEY_DO_NOT_USE_THIS_IS_NOT_A_SECRETEXAMPL';

/** Every fake credential this documentation contains, for the test that
 * proves each one fails the real validator. */
export const EXAMPLE_KEYS = [EXAMPLE_LIVE_KEY, EXAMPLE_TEST_KEY] as const;

/**
 * A response id in the documented shape (`resp_` + 24 hex, SCHEMA-V34). Not
 * a real one: no response with this id has ever existed, and reading one
 * back needs a key whose project created it.
 */
export const EXAMPLE_RESPONSE_ID = 'resp_4f2b8c1d9e0a7b6c5d4e3f20';

/** The console, where a workspace admin creates projects and keys. */
export const CONSOLE_PATH = '/api';

/**
 * WHETHER THE EXAMPLES HAVE BEEN RUN — the one switch (CONTRACT §17).
 *
 * 2026-09-13, second change the same day: the switch was flipped to `true`
 * after the first end-to-end run, then set back to `false` when the owner
 * removed every usage limit from the API. The pages changed with that
 * decision (the rate-limits page, the `curl -i` sample that printed RateLimit
 * headers, the retry guidance), and a run that checked the OLD pages cannot
 * vouch for the new ones. The integration lead re-runs
 * scripts/docs_examples_run.py against the stack and flips it back only when
 * it passes — with a changelog heading containing "examples executed".
 *
 * The original reason for `false`, still the rule: The `/v1` routes EXIST: they are
 * declared in `orchestrator/app/publicapi/router.py` and mounted by
 * `orchestrator/app/main.py`. What has not happened yet is the end-to-end run
 * of THESE examples against a running deployment, and CONTRACT §17 says an
 * example that has not been executed is marked as not executed rather than
 * presented as verified.
 *
 * 2026-09-13, verifier finding: the earlier notice told every reader "the /v1
 * routes are still being built", which stopped being true the moment the
 * router was mounted. A caveat that is itself false is worse than none — it
 * teaches a reader that the warnings on this site are stale. So the notice
 * now says exactly what is and is not true, and nothing else.
 *
 * TO FLIP IT: when the end-to-end run of the examples passes against a
 * running deployment, the integration lead sets this to `true` and adds a
 * changelog entry naming the run (content/docs/pages/changelog.ts, under a
 * heading containing "examples executed"). Every page reads this constant
 * through `EXAMPLE_STATUS`, so that is the whole edit — and
 * tests/docs-site.test.tsx refuses a `true` that has no changelog entry
 * behind it.
 */
export const EXAMPLES_EXECUTED: boolean = false;

/** The notice while the examples have not been run. */
export const NOT_EXECUTED_NOTE =
  'The /v1 routes these examples call are live, but the examples themselves ' +
  'have not yet been executed against a running deployment. They are written ' +
  'from the shipped request and response models and the mounted router, and ' +
  'checked against that code on every commit — nothing on this page is ' +
  'presented as verified output.';

/** The notice once the end-to-end run has passed. */
export const EXECUTED_NOTE =
  'The runnable examples in this documentation were executed on 2026-09-13 ' +
  'against a running TechSara stack through the public /v1 edge, with a real ' +
  'test key: all 48 that make a request passed. The other 36 blocks are ' +
  'response shapes and code fragments that are not programs on their own, ' +
  'and signed webhook delivery was verified against the signing code rather ' +
  'than end to end. The changelog records the run.';

/**
 * What every page carries. ONE value, derived from the switch above, so no
 * page can claim a status the others do not.
 */
export const EXAMPLE_STATUS = {
  executed: EXAMPLES_EXECUTED,
  note: EXAMPLES_EXECUTED ? EXECUTED_NOTE : NOT_EXECUTED_NOTE,
} as const;
