/**
 * fe audit 2026-09-13: the search palette showed a message hit's snippet as
 * raw markdown ("# Heading one for the audit Here is **bold**,…"). The row is
 * one line of evidence, so the syntax goes and every word stays.
 */
import { describe, expect, it } from 'vitest';
import { plainSnippet, rowSnippet, type SearchResult } from '../lib/searchPalette';

function hit(snippet: string | null, matchedIn: SearchResult['matchedIn'] = 'message'): SearchResult {
  return {
    id: 'c1',
    title: 'AUDIT markdown table code rendering',
    snippet,
    matchedIn,
    updatedAt: 0,
    pinned: false,
    archived: false,
  };
}

describe('plainSnippet', () => {
  it('drops the markdown the audit saw and keeps the words', () => {
    expect(plainSnippet('# Heading one for the audit Here is **bold**, *italic* and `code`…')).toBe(
      'Heading one for the audit Here is bold, italic and code…',
    );
  });

  it('keeps link and image text, not their targets', () => {
    expect(plainSnippet('See [the docs](https://example.com/docs) and ![a chart](/x.png) here')).toBe(
      'See the docs and a chart here',
    );
  });

  it('flattens a table row and its rule', () => {
    expect(plainSnippet('| Region | Revenue | | --- | ---: | | EMEA | 42 |')).toBe(
      'Region Revenue EMEA 42',
    );
  });

  it('removes quote and bullet markers and strikethrough', () => {
    expect(plainSnippet('> quoted * first item ~~old~~ new')).toBe('quoted first item old new');
  });

  it('drops single-underscore emphasis and keeps lone and inner underscores', () => {
    // The browser re-audit saw `_emphasis_` still raw after the first pass.
    expect(plainSnippet('an _important_ note, (_see below_) and _this_.')).toBe(
      'an important note, (see below) and this.',
    );
    expect(plainSnippet('the _id field of my_table_name, not __init__')).toBe(
      'the _id field of my_table_name, not __init__',
    );
  });

  it('leaves prose that only looks like syntax alone', () => {
    expect(plainSnippet('…2 + 3 in C# with snake_case and __init__ - done')).toBe(
      '…2 + 3 in C# with snake_case and __init__ - done',
    );
  });
});

describe('rowSnippet', () => {
  it('returns the plain snippet for a content hit', () => {
    expect(rowSnippet(hit('## Summary **Q3** grew'))).toBe('Summary Q3 grew');
  });

  it('still shows nothing for a title hit or an empty snippet', () => {
    expect(rowSnippet(hit('# anything', 'title'))).toBeNull();
    expect(rowSnippet(hit(null))).toBeNull();
    expect(rowSnippet(hit('**  **'))).toBeNull();
  });
});
