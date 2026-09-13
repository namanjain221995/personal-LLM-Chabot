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

/** The one public model id the registry declares (CONTRACT §15). */
export const MODEL_ID = 'techsara-35b';

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
