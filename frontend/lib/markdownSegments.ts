/**
 * Splitting a streaming answer into a FROZEN prefix and a LIVE tail (NEW-24).
 *
 * ── Why ────────────────────────────────────────────────────────────────────
 *
 * `<Markdown>` re-parses the answer from character 1 on every visual update.
 * remark-parse is superlinear, so that cost runs away as the answer grows.
 * Measured in V8 (node, not jsdom) for ONE parse of a whole answer:
 *
 *     prose-markdown   8 KB →  12 ms   20 KB →  30 ms   40 KB →  65 ms
 *     a large table    8 KB →  15 ms   20 KB →  57 ms   40 KB → 173 ms
 *
 * A 60 Hz frame is 16.7 ms. Past a few kilobytes a single frame's parse
 * already overruns the budget, so the browser cannot paint on time and cannot
 * service wheel/touch input either — which is NEW-24 and NEW-25 at once.
 *
 * Markdown is block-structured, and a streaming answer only ever changes at
 * its END. So most of that work is spent re-deriving output that cannot have
 * changed. This module finds the point up to which the text is settled; the
 * renderer parses that part once and keeps it, and re-parses only the tail.
 *
 * ── The safety rule ────────────────────────────────────────────────────────
 *
 * A split is only allowed where `render(a) + render(b)` is guaranteed to equal
 * `render(a + b)`. Getting that wrong would silently corrupt answers, so the
 * rule is deliberately conservative and refuses far more splits than it has
 * to. When in doubt it returns NO split, and the renderer simply behaves as it
 * does today — slower, never wrong.
 *
 * A boundary must sit after a blank line (nothing can continue across one) and
 * the line that follows it must start a block that cannot be a continuation of
 * what came before:
 *
 *   - not inside a fenced code block, which may contain blank lines;
 *   - not a list marker — `a\n\n- x` and `- x` alone differ, because a blank
 *     line inside a list makes it LOOSE (items wrapped in <p>);
 *   - not a block quote, for the same continuation reason;
 *   - not indented four spaces or a tab, which continues a list item or opens
 *     an indented code block.
 *
 * And three document-wide constructs make ANY split unsafe, because they let
 * text at the end change how text at the beginning renders:
 *
 *   - link reference definitions (`[label]: /url`), which resolve `[label]`
 *     anywhere in the document, including above the definition;
 *   - GFM footnote definitions (`[^1]: …`), likewise;
 *   - HTML blocks, whose extent depends on lines that may not have arrived.
 *
 * Their presence disables splitting for the whole answer.
 *
 * `tests/markdown-segments.test.ts` is the real guarantee: it renders every
 * split of a corpus of documents through the actual react-markdown pipeline
 * and asserts the HTML is byte-identical to the unsplit rendering.
 */

/** Frozen blocks (each individually stable) plus the still-growing tail. */
export interface MarkdownSegments {
  /**
   * Settled chunks, in order. Each one's text is final: appending to the
   * answer can never change it, which is what lets the renderer memoize each
   * chunk and skip both the parse and the React work for it.
   */
  frozen: string[];
  /** Everything after the last safe boundary — re-parsed on every update. */
  tail: string;
}

/** `[label]: url` / `[^note]: …` — resolve across the whole document. */
const DEFINITION = /^ {0,3}\[[^\]]*\]:/;
/** A line that continues, or could re-open, the block above the blank line. */
const CONTINUATION = /^(?: {0,3}(?:[-*+]|\d{1,9}[.)])(?:[ \t]|$)| {0,3}>|(?: {4,}|\t))/;

/**
 * Is there a blank line at all? Without one there can be no boundary, so a
 * single long paragraph — the one shape segmentation cannot help — is
 * rejected in a single scan instead of a full line walk.
 */
const BLANK_LINE = /\n[ \t]*\n/;

/**
 * Split `text` at every safe boundary.
 *
 * Walks the text by character index rather than `split('\n')`: at 60 frames a
 * second, materialising ~1000 line strings and a parallel offset array per
 * frame is real allocation and GC pressure for a scan that only needs to look
 * at a few characters of most lines. Lines are only sliced when a regex is
 * genuinely needed, which the first-character checks below make rare.
 *
 * Linear, allocation-light, and a small fraction of the parse it saves.
 * Returns everything in `tail` when no boundary is safe, which is exactly the
 * unsegmented behaviour.
 */
export function splitMarkdown(text: string): MarkdownSegments {
  if (text.length === 0) return { frozen: [], tail: '' };
  if (!BLANK_LINE.test(text)) return { frozen: [], tail: text };

  const boundaries: number[] = [];
  /** The fence currently open, or null. */
  let fence: { marker: string; size: number } | null = null;
  let previousBlank = false;

  let i = 0;
  while (i <= text.length) {
    let end = text.indexOf('\n', i);
    if (end === -1) end = text.length;

    // First non-space character, and whether the line is blank — both read
    // straight off the string, no slicing.
    let j = i;
    while (j < end && (text[j] === ' ' || text[j] === '\t')) j += 1;
    const blank = j === end;
    const first = blank ? '' : text[j];
    const indent = j - i;

    if (fence) {
      // Only a matching, at-least-as-long closing fence ends it. Nothing
      // inside a fence is a boundary — blank lines included.
      if ((first === '`' || first === '~') && first === fence.marker && indent < 4) {
        let run = 0;
        while (j + run < end && text[j + run] === first) run += 1;
        if (run >= fence.size && text.slice(j + run, end).trim() === '') {
          fence = null;
        }
      }
      previousBlank = false;
    } else if (blank) {
      previousBlank = true;
    } else if (
      // One construct anywhere in the document forbids splitting all of it.
      (first === '[' && DEFINITION.test(text.slice(i, end))) ||
      (first === '<' && indent < 4)
    ) {
      return { frozen: [], tail: text };
    } else if ((first === '`' || first === '~') && indent < 4) {
      let run = 0;
      while (j + run < end && text[j + run] === first) run += 1;
      if (run >= 3) {
        // A fence opens a block that may swallow blank lines, but the fence
        // line ITSELF can start a frozen chunk if a blank line preceded it.
        if (previousBlank) boundaries.push(i);
        fence = { marker: first, size: run };
      } else if (previousBlank && !CONTINUATION.test(text.slice(i, end))) {
        boundaries.push(i);
      }
      previousBlank = false;
    } else {
      if (previousBlank && !CONTINUATION.test(text.slice(i, end))) {
        boundaries.push(i);
      }
      previousBlank = false;
    }

    if (end === text.length) break;
    i = end + 1;
  }

  // An unterminated fence means the whole fenced block is still arriving; the
  // boundary that opened it is not settled, so it does not count.
  if (fence && boundaries.length > 0) boundaries.pop();

  if (boundaries.length === 0) return { frozen: [], tail: text };

  const frozen: string[] = [];
  let from = 0;
  for (const to of boundaries) {
    // Chunks are sliced on line starts, so each keeps its own trailing blank
    // line(s) and re-joining them reproduces the original text exactly.
    if (to > from) frozen.push(text.slice(from, to));
    from = to;
  }
  return { frozen, tail: text.slice(from) };
}
