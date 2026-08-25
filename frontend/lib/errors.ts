/**
 * Turn a raw engine/model error into something a person can act on.
 *
 * Errors arrive as whatever the upstream said — typically a stringified
 * OpenAI/vLLM payload like:
 *   Error code: 400 - {'error': {'message': "This model's maximum context
 *   length is 8192 tokens. However, you requested 8000 output tokens..."}}
 * Dumping that into the thread is noise AND a leak: the user cannot tell what
 * to do, and an upstream sentence can carry a DSN, an echoed header or a
 * traceback. So every branch here returns copy WE wrote. Nothing that came
 * off the wire is ever returned for display — the original goes to the server
 * log instead (lib/serverLog.ts), which is where an engineer can use it.
 *
 * This is the NON-FATAL path: an error event that arrives mid-stream, after
 * the orchestrator already accepted the request. A send that never became a
 * stream is a fatal request failure and gets the error page instead
 * (lib/errorTypes.ts + components/ChatErrorPage.tsx).
 */

/**
 * Plain-language notice for a prompt that had to be shortened to fit the
 * model's window. Says WHICH kind of shortening happened, because the two
 * have different consequences: dropped turns lose older context, a clipped
 * message means part of what the user just pasted was not sent.
 */
export function trimNotice(info: {
  dropped_turns: number;
  clipped_messages: number;
}): string {
  const { dropped_turns: dropped, clipped_messages: clipped } = info;
  if (clipped > 0 && dropped > 0) {
    return `Input was shortened to fit the model's limit — part of a long message was left out, and ${dropped} earlier ${
      dropped === 1 ? 'turn' : 'turns'
    } dropped from context.`;
  }
  if (clipped > 0) {
    return "Input was shortened to fit the model's limit — part of a long message was left out.";
  }
  if (dropped > 0) {
    return `${dropped} earlier ${
      dropped === 1 ? 'turn was' : 'turns were'
    } dropped from context to fit the model's limit.`;
  }
  return "Input was shortened to fit the model's limit.";
}

export interface FriendlyError {
  /** One sentence, plain language, ending in what to do next. */
  message: string;
}

/** Pull the human sentence out of a stringified error payload. */
export function extractUpstreamMessage(raw: string): string | null {
  // Python-repr dicts use single quotes, JSON uses double — accept both.
  const m =
    /['"]message['"]\s*:\s*"((?:[^"\\]|\\.)*)"/.exec(raw) ??
    /['"]message['"]\s*:\s*'((?:[^'\\]|\\.)*)'/.exec(raw);
  return m ? m[1].replace(/\\(.)/g, '$1') : null;
}

const CONTEXT_OVERFLOW = /maximum context length|context length is|too many tokens/i;
const CONNECTION = /connection|unreachable|refused|timeout|timed out|ECONN/i;
const OUT_OF_MEMORY = /out of memory|CUDA|OOM/i;
const NOT_FOUND_MODEL = /model .* does not exist|not found/i;

/** The safe sentence for a failure we could not classify. */
const GENERIC =
  "We couldn't complete that request. Please try again.";

export function friendlyError(raw?: string | null): FriendlyError {
  const text = (raw ?? '').trim();
  if (!text) return { message: 'The engine reported an error.' };
  // The upstream sentence is used to CLASSIFY. It is never returned.
  const upstream = extractUpstreamMessage(text) ?? text;

  if (CONTEXT_OVERFLOW.test(upstream)) {
    return {
      message:
        'This conversation is too long for the selected model. Switch the model picker to Smart, or start a new chat.',
    };
  }
  if (CONNECTION.test(upstream)) {
    return {
      message:
        'The model is temporarily unavailable. It may still be starting up — please try again in a moment.',
    };
  }
  if (OUT_OF_MEMORY.test(upstream)) {
    return {
      message:
        'The model server ran out of memory on this request. Try a shorter message or a smaller attachment.',
    };
  }
  if (NOT_FOUND_MODEL.test(upstream)) {
    return {
      message: 'The selected model is not available on this machine right now.',
    };
  }
  // Unrecognized. Previously this returned the upstream sentence itself,
  // which is how raw payload text reached the thread; there is no way to know
  // what such a string contains, so it does not get rendered.
  return { message: GENERIC };
}
