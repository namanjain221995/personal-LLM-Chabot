/**
 * The planner's line breaker (charts design v2 §4.1, C1).
 *
 * ECharts' own wrapper walks UTF-16 units and measures every non-ASCII
 * character as '国', so it split emoji and conjuncts and over-wrapped accented
 * and Indic labels. `wrapLines` breaks on word and grapheme boundaries with an
 * injected measurer; these tests use a deterministic one:
 * 7 px per grapheme, plus 12 for a wide one (emoji, CJK).
 */

import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  estimateWidth,
  graphemes,
  lineHeightFor,
  LINE_HEIGHT,
  TALL_LINE_HEIGHT,
  wrapLines,
  WIDTH_SAFETY,
} from '../lib/chartText';

const WIDE = /[\u{1F000}-\u{1FAFF}\u{2E80}-\u{A4CF}\u{AC00}-\u{D7A3}]/u;
const measure = (text: string) =>
  graphemes(text).reduce((w, g) => w + (WIDE.test(g) ? 19 : 7), 0);

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('wrapLines', () => {
  it('keeps a label that fits on one line, untouched', () => {
    expect(wrapLines('Owner A', 200, measure)).toEqual(['Owner A']);
  });

  it('breaks at spaces and never drops a word', () => {
    const label = 'Recruitment coordinator for the eastern region';
    const lines = wrapLines(label, 120, measure);
    expect(lines.length).toBeGreaterThan(1);
    expect(lines.join(' ')).toBe(label);
    for (const line of lines) expect(measure(line) + WIDTH_SAFETY).toBeLessThanOrEqual(120);
  });

  it('trims spaces at line ends', () => {
    const lines = wrapLines('alpha   beta    gamma', 50, measure);
    for (const line of lines) {
      expect(line).toBe(line.trim());
    }
  });

  it('breaks a long unbroken Id between graphemes', () => {
    const id = 'a0X5g00000AbCdEfGhIjKlMnOp';
    const lines = wrapLines(id, 60, measure);
    expect(lines.length).toBeGreaterThan(1);
    expect(lines.join('')).toBe(id);
  });

  it('never splits a ZWJ family emoji, a flag, a conjunct or a combining accent', () => {
    const family = '👨‍👩‍👧‍👦';
    const flag = '🇮🇳';
    const conjunct = 'क्ष';
    const accented = 'é';
    const text = `${family}${flag}${conjunct}${accented}`.repeat(6);
    const lines = wrapLines(text, 30, measure);
    expect(lines.join('')).toBe(text);
    for (const line of lines) {
      // Every line is made of whole clusters: re-segmenting it gives back
      // only the four clusters we put in.
      for (const g of graphemes(line)) {
        expect([family, flag, conjunct, accented]).toContain(g);
      }
    }
  });

  it('breaks Arabic only between words', () => {
    const arabic = 'مدير المبيعات الإقليمي للمنطقة الشرقية';
    const words = arabic.split(' ');
    const lines = wrapLines(arabic, 80, measure);
    expect(lines.length).toBeGreaterThan(1);
    for (const line of lines) {
      for (const w of line.split(' ')) expect(words).toContain(w);
    }
  });

  it('breaks a spaceless Thai run between words, not letters', () => {
    const thai = 'ภาษาไทยง่ายนิดเดียว';
    const lines = wrapLines(thai, 40, measure);
    expect(lines.join('')).toBe(thai);
    expect(lines.length).toBeGreaterThan(1);
  });

  it('returns the line count ECharts receives (newlines + 1)', () => {
    const lines = wrapLines('Quarterly pipeline review for enterprise accounts', 90, measure);
    const joined = lines.join('\n');
    expect(joined.split('\n').length).toBe(lines.length);
  });

  it('keeps explicit newlines', () => {
    expect(wrapLines('one\ntwo', 500, measure)).toEqual(['one', 'two']);
  });

  it('falls back without Intl.Segmenter (code points, spaces)', () => {
    vi.stubGlobal('Intl', { ...Intl, Segmenter: undefined });
    const lines = wrapLines('Owner with a fairly long name', 70, measure);
    expect(lines.join(' ')).toBe('Owner with a fairly long name');
    expect(graphemes('🇮🇳')).toEqual(Array.from('🇮🇳'));
  });
});

describe('script-aware line height', () => {
  it('uses 16 px for Latin and 18 px when a label carries Devanagari or Arabic', () => {
    expect(lineHeightFor(['Owner A', 'Owner B'])).toBe(LINE_HEIGHT);
    expect(lineHeightFor(['Owner A', 'मित्र'])).toBe(TALL_LINE_HEIGHT);
    expect(lineHeightFor(['مدير'])).toBe(TALL_LINE_HEIGHT);
  });
});

describe('estimateWidth (no canvas)', () => {
  it('is deterministic and grows with the text', () => {
    expect(estimateWidth('abc')).toBe(estimateWidth('abc'));
    expect(estimateWidth('abcdef')).toBeGreaterThan(estimateWidth('abc'));
    // A wide grapheme costs more than a narrow one.
    expect(estimateWidth('国')).toBeGreaterThan(estimateWidth('i'));
  });
});
