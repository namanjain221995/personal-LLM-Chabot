import { describe, expect, it } from 'vitest';
import {
  PASTE_MIN_CHARS,
  PASTE_MIN_LINES,
  countLines,
  foldModelContent,
  imageExtFromMime,
  makePastedText,
  shouldAttachPaste,
} from '../lib/pasted';
import type { PastedText } from '../lib/types';

describe('shouldAttachPaste', () => {
  it('leaves short single-line text inline', () => {
    expect(shouldAttachPaste('hello world')).toBe(false);
    expect(shouldAttachPaste('')).toBe(false);
  });

  it('chips text past the character threshold', () => {
    expect(shouldAttachPaste('x'.repeat(PASTE_MIN_CHARS))).toBe(true);
    expect(shouldAttachPaste('x'.repeat(PASTE_MIN_CHARS - 1))).toBe(false);
  });

  it('chips text past the line threshold even when short', () => {
    const many = Array.from({ length: PASTE_MIN_LINES }, () => 'a').join('\n');
    expect(shouldAttachPaste(many)).toBe(true);
    const few = Array.from({ length: PASTE_MIN_LINES - 1 }, () => 'a').join(
      '\n',
    );
    expect(shouldAttachPaste(few)).toBe(false);
  });
});

describe('makePastedText', () => {
  it('records line and char counts', () => {
    const p = makePastedText('a\nb\nc', 'p1');
    expect(p).toEqual({ id: 'p1', content: 'a\nb\nc', lines: 3, chars: 5 });
    expect(countLines('')).toBe(0);
  });
});

describe('foldModelContent', () => {
  const p = (content: string, id = 'x'): PastedText => makePastedText(content, id);

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
