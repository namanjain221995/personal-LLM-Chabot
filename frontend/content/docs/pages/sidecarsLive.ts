/**
 * WHETHER THE CLOCK-FREE EMBEDDINGS, RERANK AND TRANSCRIPTION ROUTES ARE LIVE
 * (2026-09-14).
 *
 * The three routes that generate no text lost their clocks ahead of the rest
 * of the no-timeout release: no gate wait, a committed response that sends
 * spaces while it works, 2,048 inputs and 1,000 documents in an 8 MiB body,
 * audio of any length in a 90 MiB body. Before this switch, the live site said
 * 256 inputs, 300 seconds and a 504 for them. It also printed their new limits
 * in the model catalogue, so it described two APIs at once (review
 * 2026-09-14, medium).
 *
 * So their reference pages — and the rows about them on the errors, rate-limit
 * and Python pages — follow THIS switch. Everything about generations on those
 * pages still follows NO_TIMEOUT_LIVE (pages/longOutput.ts). A link to a page
 * that only exists after the release (`/docs/timeouts`) is printed only once
 * NO_TIMEOUT_LIVE is true.
 *
 * tests/docs-site.test.tsx ties the value to the code: it is true exactly
 * when publicapi/endpoints.py sends embeddings and rerank to the engine with
 * `wait_s=None` inside a committed response, and the today pages are held
 * to the caps in publicapi/endpoint_models.py and registry.py.
 */
export const SIDECARS_NO_TIMEOUT_LIVE: boolean = true;
