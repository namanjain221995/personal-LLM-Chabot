import { describe, expect, it } from 'vitest';
import { stripCitations } from '../lib/citations';

describe('stripCitations (hide [n] markers, owner request 2026-08-05)', () => {
  it('removes single and stacked citation runs with their leading space', () => {
    expect(stripCitations('crossed 90 million streams [3][9] before')).toBe(
      'crossed 90 million streams before',
    );
    expect(stripCitations('Director: Leo Ben [3][5][9]')).toBe(
      'Director: Leo Ben',
    );
    expect(stripCitations('[1] Leading citation')).toBe(' Leading citation');
  });

  it('pulls punctuation back in after a stripped citation', () => {
    expect(stripCitations('the Spotify Viral 50 Global list [3][9].')).toBe(
      'the Spotify Viral 50 Global list.',
    );
  });

  it('leaves code fences and inline code alone', () => {
    const md = 'Use `arr[0]` here.\n```js\nconst x = a[1][2];\n```\nDone [4]';
    expect(stripCitations(md)).toBe(
      'Use `arr[0]` here.\n```js\nconst x = a[1][2];\n```\nDone',
    );
  });

  it('never touches markdown link syntax', () => {
    expect(stripCitations('see [1](https://x.test) and [2]: def')).toBe(
      'see [1](https://x.test) and [2]: def',
    );
    expect(stripCitations('a [link text](https://x.test) [7]')).toBe(
      'a [link text](https://x.test)',
    );
  });

  it('does not strip indexing like arr[0] in prose', () => {
    expect(stripCitations('the value arr[0] is first')).toBe(
      'the value arr[0] is first',
    );
  });
});

// ---------------------------------------------------------------------------
// A research report keeps its citations
//
// stripCitations is right for a chat answer and exactly wrong for a Deep
// Research report, where the engine plans subquestions, reads sources,
// resolves claims against each other and writes per-claim markers. Stripping
// them delivered a 12 KB sourced report as an uncited essay, and nothing
// downstream could tell which sentence rested on which source.
// ---------------------------------------------------------------------------

import { keepsCitations, linkCitations } from '@/lib/citations';

const SOURCES = [
  { n: 1, url: 'https://openai.com/about', title: 'About OpenAI' },
  { n: 3, url: 'https://npr.org/story', title: 'NPR' },
];

describe('citations on a research report', () => {
  it('keeps them only for deep research', () => {
    expect(keepsCitations('deep_research')).toBe(true);
    expect(keepsCitations('chat')).toBe(false);
    expect(keepsCitations(undefined)).toBe(false);
  });

  it('turns each marker into a link to its own source', () => {
    const out = linkCitations('Revenue rose [1] and then fell [3].', SOURCES);
    expect(out).toContain('[[1]](https://openai.com/about "About OpenAI")');
    expect(out).toContain('[[3]](https://npr.org/story "NPR")');
  });

  it('links every marker in a run separately', () => {
    // `[1][3]` is two citations, not one — each must reach its own source.
    const out = linkCitations('Both agree [1][3].', SOURCES);
    expect(out).toContain('[[1]](https://openai.com/about');
    expect(out).toContain('[[3]](https://npr.org/story');
  });

  it('leaves a marker with no source as plain text', () => {
    // The engine's citation validator can leave a gap; an anchor that goes
    // nowhere is worse than a number.
    expect(linkCitations('Claimed [9].', SOURCES)).toBe('Claimed [9].');
  });

  it('never touches code', () => {
    const code = 'Use `arr[1]` here.\n```js\nconst x = arr[3];\n```\n';
    expect(linkCitations(code, SOURCES)).toBe(code);
  });

  it('leaves existing markdown links alone', () => {
    const md = 'See [1](https://elsewhere.test) and [3]: https://ref.test';
    const out = linkCitations(md, SOURCES);
    expect(out).toContain('[1](https://elsewhere.test)');
    expect(out).toContain('[3]: https://ref.test');
  });

  it('is a no-op with no sources', () => {
    expect(linkCitations('Claimed [1].', [])).toBe('Claimed [1].');
  });
});
