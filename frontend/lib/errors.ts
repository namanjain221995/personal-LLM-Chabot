/**
 * Turn a raw engine/model error into something a person can act on.
 *
 * Errors arrive as whatever the upstream said — typically a stringified
 * OpenAI/vLLM payload like:
 *   Error code: 400 - {'error': {'message': "This model's maximum context
 *   length is 8192 tokens. However, you requested 8000 output tokens..."}}
 * Dumping that into the thread is noise: the user cannot tell what to do, and
 * the sentence that matters is buried. We show a plain explanation and keep
 * the original available behind a disclosure.
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
  /** The raw upstream text, or null when it adds nothing. */
  detail: string | null;
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

export function friendlyError(raw?: string | null): FriendlyError {
  const text = (raw ?? '').trim();
  if (!text) {
    return { message: 'The engine reported an error.', detail: null };
  }
  const upstream = extractUpstreamMessage(text) ?? text;

  if (CONTEXT_OVERFLOW.test(upstream)) {
    return {
      message:
        'This conversation is too long for the selected model. Switch the model picker to Smart, or start a new chat.',
      detail: text,
    };
  }
  if (CONNECTION.test(upstream)) {
    return {
      message:
        'The model server did not respond. It may still be starting up — wait a moment and retry.',
      detail: text,
    };
  }
  if (OUT_OF_MEMORY.test(upstream)) {
    return {
      message:
        'The model server ran out of memory on this request. Try a shorter message or a smaller attachment.',
      detail: text,
    };
  }
  if (NOT_FOUND_MODEL.test(upstream)) {
    return {
      message:
        'The selected model is not available on this machine right now.',
      detail: text,
    };
  }
  // Unrecognized: show the upstream sentence if we isolated one (it is far
  // more readable than the wrapper), and keep the full payload behind the
  // disclosure. If it was already a bare sentence, there is nothing to hide.
  const isolated = extractUpstreamMessage(text);
  return {
    message: isolated ?? text,
    detail: isolated ? text : null,
  };
}
