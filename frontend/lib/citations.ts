/**
 * Strip the model's inline [n] web-citation markers for display (owner
 * request 2026-08-05): "90 million streams [3][9]" → "90 million streams".
 * The numbered sources are not lost — they live in the ActivityPanel's
 * "Cited sources" list behind the action row's Sources button.
 *
 * Code is untouched: fenced blocks and inline code spans keep their brackets
 * (`arr[0]` must render exactly as written), and markdown link syntax
 * survives — `[1](url)` and `[1]: url` are links, not citations.
 */

/**
 * One citation run — `[3]`, `[3][9]`, `[12][3][9]` — plus the single space
 * before it, so "streams [3][9] and" collapses to "streams and":
 *  - `(?<![\w\]])` the opening bracket may not follow a word char (arr[0])
 *    or a `]` (that is the middle of a run the match already consumed);
 *  - `(?![:(])` a trailing `:` or `(` means link syntax, leave it alone.
 */
const CITATION_RUN = / ?(?<![\w\]])(?:\[\d{1,3}\])+(?![:(])/g;

function stripProse(segment: string): string {
  return (
    segment
      .replace(CITATION_RUN, '')
      // "streams [9]." leaves "streams ." — pull the punctuation back in.
      .replace(/ +([.,;:!?])/g, '$1')
  );
}

export function stripCitations(markdown: string): string {
  // Fenced code first; odd split indexes are the untouchable blocks.
  return markdown
    .split(/(```[\s\S]*?(?:```|$)|~~~[\s\S]*?(?:~~~|$))/)
    .map((part, i) => {
      if (i % 2 === 1) return part;
      // Inside a prose part, inline code spans are untouchable too.
      return part
        .split(/(`[^`\n]*`)/)
        .map((span, j) => (j % 2 === 1 ? span : stripProse(span)))
        .join('');
    })
    .join('');
}

/**
 * The opposite operation, for a Deep Research report: turn each `[n]` into a
 * link to the source it cites.
 *
 * WHY THIS EXISTS. `stripCitations` above is right for a chat answer — the
 * owner asked for clean prose in 2026-08-05 and the numbered sources moved to
 * the Sources panel. It is exactly wrong for a research report, where the
 * citation IS the product: the engine plans subquestions, reads sources,
 * resolves claims against each other and writes per-claim markers, and
 * stripping them delivers a 12 KB sourced report as an uncited essay. Nothing
 * downstream could tell which sentence rested on which source.
 *
 * A marker with no matching source is left as plain text rather than linked
 * to nothing — the engine's own citation validator can leave a gap, and an
 * anchor that goes nowhere is worse than a number.
 *
 * Code is protected exactly as it is above: fenced blocks and inline spans
 * pass through untouched, so `arr[0]` is never turned into a link.
 */
export function linkCitations(
  markdown: string,
  sources: readonly { n: number; url: string; title?: string }[],
): string {
  if (!sources.length) return markdown;
  const byNumber = new Map(sources.map((s) => [s.n, s]));

  const linkProse = (segment: string): string =>
    // One marker at a time, not the run: `[3][9]` must become two links.
    // The lookbehind is `(?<!\w)`, NOT `(?<![\w\]])` as in stripCitations
    // above. There one match consumes a whole run and the `]` guard stops it
    // re-matching mid-run; here each marker is matched separately, so that
    // guard would refuse the SECOND marker of `[1][3]` — it follows a `]` —
    // and half of every run would go unlinked. `\w` alone still protects
    // `arr[0]`, and replace() scans the original string, so an earlier
    // substitution cannot shift what follows.
    segment.replace(/(?<!\w)\[(\d{1,3})\](?![:(])/g, (whole, digits) => {
      const source = byNumber.get(Number(digits));
      if (!source?.url) return whole;
      // The title rides in the link title attribute so hovering a marker
      // names the source without opening it.
      const safeTitle = (source.title ?? source.url).replace(/"/g, "'");
      return `[[${digits}]](${source.url} "${safeTitle}")`;
    });

  return markdown
    .split(/(```[\s\S]*?(?:```|$)|~~~[\s\S]*?(?:~~~|$))/)
    .map((part, i) => {
      if (i % 2 === 1) return part;
      return part
        .split(/(`[^`\n]*`)/)
        .map((span, j) => (j % 2 === 1 ? span : linkProse(span)))
        .join('');
    })
    .join('');
}

/**
 * Does this turn's prose keep its citation markers?
 *
 * Only a Deep Research report does. Everything else follows the 2026-08-05
 * rule and reads clean, with its sources in the panel.
 */
export function keepsCitations(route: string | undefined): boolean {
  return route === 'deep_research';
}
