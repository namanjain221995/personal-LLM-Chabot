/**
 * The shape of one documentation page (CONTRACT §17).
 *
 * The documentation is DATA, not JSX. Every page below is a record with a
 * markdown body, which is what makes the tests in tests/docs-site.test.tsx
 * possible at all: they read every body as text and prove — mechanically —
 * that each /v1 route named in the prose exists in CONTRACT.md, that every
 * example key fails the real checksum, that every in-page anchor resolves,
 * and that no page links to a page that does not exist. None of that can be
 * checked against hand-written JSX without a browser.
 *
 * WHY THE CODE FENCES ARE TILDES (~~~), NOT BACKTICKS. A page body is a
 * TypeScript template literal, and a backtick ends one. CommonMark accepts
 * `~~~lang` as a fenced block with an info string exactly as it accepts the
 * backtick form, and mdast-util-to-hast puts the same `language-lang` class
 * on the <code> element either way — so rehype-highlight colours a tilde
 * fence identically. Escaping three backticks per fence, sixty times over,
 * is how a stray \` ends up truncating a page nobody notices for a month.
 */

/** A page's examples, and whether anyone has actually run them. */
export interface ExampleStatus {
  /**
   * CONTRACT §17: "Every example in it is executed against the running API
   * before it ships; an example that cannot be executed is marked as not
   * executed rather than presented as verified."
   *
   * Every page reads `EXAMPLE_STATUS` from content/docs/samples.ts, which
   * is derived from the single `EXAMPLES_EXECUTED` switch. The `/v1` routes
   * are mounted; the examples have not yet been run end to end against a
   * running deployment, so the switch is false and the page renders a
   * visible notice saying exactly that.
   */
  executed: boolean;
  /** One sentence, shown to the reader, saying why — or when it was run. */
  note: string;
}

export interface DocPage {
  /** URL segment. The overview's slug is rendered at /docs itself. */
  slug: string;
  /** Sidebar and <h1> text. */
  title: string;
  /** One sentence under the title, and the page description in <head>. */
  summary: string;
  /** Which sidebar section this page belongs to, by section title. */
  section: string;
  examples: ExampleStatus;
  /** GitHub-flavoured markdown. See the tilde-fence note above. */
  body: string;
}

export interface DocSection {
  title: string;
  /** One sentence for the section, read by nobody in a hurry and by everyone
   * who is lost. */
  summary: string;
  pages: DocPage[];
}
