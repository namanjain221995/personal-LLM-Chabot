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
