import { describe, expect, it } from 'vitest';
import { foldModelContent, imageExtFromMime } from '../lib/pasted';
import type { PastedText } from '../lib/types';

/**
 * The composer no longer turns a long paste into a chip (2026-09-04) — that
 * lives in tests/paste-inline.test.tsx now. What is left here is the READ
 * side, which outlives the feature: every turn sent before that date still
 * carries `meta.pasted`, and its blocks must still reach the model.
 */

describe('foldModelContent', () => {
  const p = (content: string, id = 'x'): PastedText => ({
    id,
    content,
    lines: content.split('\n').length,
    chars: content.length,
  });

  it('returns typed content unchanged with no pasted blocks', () => {
    expect(foldModelContent('hi', undefined)).toBe('hi');
    expect(foldModelContent('hi', [])).toBe('hi');
  });

  it('prepends pasted blocks before the typed instruction', () => {
    const out = foldModelContent('summarize this', [p('BLOCK')]);
    expect(out).toBe('BLOCK\n\nsummarize this');
  });

  it('joins multiple blocks in order and preserves code verbatim', () => {
    const code = 'def f():\n    return 1';
    const out = foldModelContent('explain', [p(code, 'a'), p('second', 'b')]);
    expect(out).toBe(`${code}\n\nsecond\n\nexplain`);
  });

  it('drops empty/whitespace parts (pasted-only send has no trailing text)', () => {
    expect(foldModelContent('', [p('only block')])).toBe('only block');
    expect(foldModelContent('   ', [p('block')])).toBe('block');
    expect(foldModelContent('', [])).toBe('');
  });
});

describe('imageExtFromMime', () => {
  it('maps known image types', () => {
    expect(imageExtFromMime('image/png')).toBe('png');
    expect(imageExtFromMime('image/jpeg')).toBe('jpg');
    expect(imageExtFromMime('image/webp')).toBe('webp');
    expect(imageExtFromMime('image/gif')).toBe('gif');
  });

  it('falls back to the mime subtype, then png', () => {
    expect(imageExtFromMime('image/x-icon')).toBe('x-icon');
    expect(imageExtFromMime('bogus')).toBe('png');
  });
});
