'use strict';
/**
 * A server-sent-events frame parser for the relay (2026-09-13).
 *
 * WHY THE GATEWAY PARSES AT ALL, when route.ts passes bytes through untouched:
 *
 *  1. The orchestrator writes an internal comment `: ts-seq=N` after every
 *     data frame of a gateway-tagged stream (no-timeout design, INTERNAL
 *     ATTACH PROTOCOL §3). It names how far the client has got, so a
 *     re-attach can ask for N+1 onward. It must never reach a caller, and it
 *     can only be removed by something that knows where frames end.
 *  2. A heartbeat written into the middle of a frame corrupts it. Relaying
 *     whole frames only means `: ping` can be injected at any moment the
 *     upstream is silent.
 *  3. A stream that already delivered its terminal event (`data: [DONE]`,
 *     `response.completed`, ...) must end, not re-attach, when the socket
 *     then drops.
 *
 * Line endings are normalised to LF on the way through; every SSE parser
 * (the WHATWG grammar, openai-python, openai-node) treats CRLF, CR and LF
 * identically, so this changes no event.
 */

const { StringDecoder } = require('node:string_decoder');

const TS_SEQ = /^:\s?ts-seq=(\d{1,15})\s*$/;

/** Events after which a stream has nothing left to say. */
const TERMINAL_EVENTS = new Set([
  'response.completed',
  'response.failed',
  'response.incomplete',
  'error',
  'transcript.text.done',
]);

/** The largest unfinished frame kept (characters); past it the relay is cut. */
const DEFAULT_MAX_FRAME_CHARS = 64 * 1024 * 1024;

class SseParser {
  /**
   * WHY LINEAR (2026-09-13, review finding, scratchpad p8_sse_quad.cjs): the
   * first version rescanned the whole unfinished buffer on every chunk. One
   * 16 MiB event in 64 KiB chunks took 4,540 ms, the worst single push
   * 39.5 ms, on the event loop every relay shares. Each push now scans only
   * its own text; the pieces of an unfinished line are joined once, when the
   * line ends. An unfinished frame larger than `maxFrameChars` throws
   * (SSE_FRAME_TOO_LARGE), which the relay turns into a cut client.
   */
  constructor({ maxFrameChars = DEFAULT_MAX_FRAME_CHARS } = {}) {
    this.decoder = new StringDecoder('utf8');
    this.maxFrameChars = maxFrameChars;
    this.tail = []; // pieces of the current unfinished line
    this.tailChars = 0;
    this.lines = [];
    this.frameChars = 0; // characters in this.lines
    this.skipLF = false; // the previous push ended on CR: a leading LF is its pair
  }

  /**
   * Feed bytes; returns the complete frames they finished. Each frame is
   *   { kind: 'event' | 'comment' | 'marker', text, seq, terminal, event }
   * `text` is the frame as it should be relayed (ts-seq lines removed, LF
   * endings, blank-line terminated); a 'marker' frame has no text to relay.
   */
  push(chunk) {
    const text = this.decoder.write(chunk);
    const frames = [];
    let start = 0;
    let i = 0;
    if (this.skipLF && text.length > 0) {
      this.skipLF = false;
      if (text.charCodeAt(0) === 10) {
        start = 1;
        i = 1;
      }
    }
    for (; i < text.length; i += 1) {
      const ch = text.charCodeAt(i);
      if (ch !== 10 && ch !== 13) continue;
      let line = text.slice(start, i);
      if (this.tail.length) {
        this.tail.push(line);
        line = this.tail.join('');
        this.tail = [];
        this.tailChars = 0;
      }
      if (ch === 13) {
        if (i + 1 < text.length) {
          if (text.charCodeAt(i + 1) === 10) i += 1;
        } else {
          this.skipLF = true; // CR may be half of a CRLF split across chunks
        }
      }
      start = i + 1;
      if (line === '') {
        if (this.lines.length) frames.push(classify(this.lines));
        this.lines = [];
        this.frameChars = 0;
      } else {
        this.lines.push(line);
        this.frameChars += line.length + 1;
      }
    }
    if (start < text.length) {
      this.tail.push(text.slice(start));
      this.tailChars += text.length - start;
    }
    if (this.frameChars + this.tailChars > this.maxFrameChars) {
      const err = new Error('an SSE frame exceeded the frame size limit');
      err.code = 'SSE_FRAME_TOO_LARGE';
      throw err;
    }
    return frames;
  }

  /** Bytes of a frame that had begun but not ended (dropped on failure). */
  get partial() {
    return this.lines.length > 0 || this.tail.length > 0;
  }
}

function classify(lines) {
  let seq = null;
  const kept = [];
  let hasField = false;
  let event = null;
  const data = [];
  for (const line of lines) {
    const marker = TS_SEQ.exec(line);
    if (marker) {
      seq = Number(marker[1]);
      continue;
    }
    kept.push(line);
    if (line.startsWith(':')) continue;
    hasField = true;
    const colon = line.indexOf(':');
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? '' : line.slice(colon + 1);
    if (value.startsWith(' ')) value = value.slice(1);
    if (field === 'event') event = value;
    else if (field === 'data') data.push(value);
  }
  if (kept.length === 0) return { kind: 'marker', text: '', seq, terminal: false, event: null };
  const text = `${kept.join('\n')}\n\n`;
  if (!hasField) return { kind: 'comment', text, seq, terminal: false, event: null };
  const dataText = data.join('\n');
  const terminal = dataText.trim() === '[DONE]' || (event !== null && TERMINAL_EVENTS.has(event));
  return { kind: 'event', text, seq, terminal, event };
}

function comment(note) {
  return `: ${note}\n\n`;
}

/**
 * An in-band error for a stream the gateway had to commit before the
 * orchestrator answered, when the orchestrator's answer was then a refusal.
 * `event: error` plus a `data` object carrying `error` raises APIError in
 * openai-python 3.13 (_streaming.py: event == "error" and data.error),
 * openai-node 7.15 (event === 'error') and 6.49 (data.error) alike.
 */
function errorFrame(envelope) {
  const payload = envelope && typeof envelope === 'object' && envelope.error ? envelope : {
    error: {
      message: 'The service is temporarily unavailable. Please retry.',
      type: 'service_unavailable_error',
      code: 'model_unavailable',
      param: null,
      request_id: null,
    },
  };
  return `event: error\ndata: ${JSON.stringify(payload)}\n\n`;
}

module.exports = { SseParser, TERMINAL_EVENTS, DEFAULT_MAX_FRAME_CHARS, comment, errorFrame, classify };
