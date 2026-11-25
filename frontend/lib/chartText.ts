/**
 * Chart text: the one place that decides where a label breaks.
 *
 * WHY THE PLANNER BREAKS LINES ITSELF (design v2 §4.1, C1). ECharts'
 * `axisLabel.width` + `overflow: 'break'` wraps with zrender's `wrapText`,
 * which walks UTF-16 code units and measures every character above U+007F as
 * the width of '国' (zrender/lib/contain/text.js, parseText.js). Accented,
 * Devanagari and Arabic labels therefore wrap into more lines than any planner
 * reserves, and an emoji surrogate pair or a conjunct can be split across two
 * lines. So ECharts is never asked to wrap: this module breaks every label on
 * word and grapheme boundaries, measured in the font ECharts draws with, and
 * the option receives the finished text joined with '\n'.
 *
 * Pure apart from the optional canvas measurer, which is looked up lazily and
 * replaced by a deterministic per-grapheme estimate where there is no canvas
 * (node, jsdom). Tests inject their own measurer.
 */

export type Measure = (text: string) => number;

export const CHART_FONT_PX = 12;
export const CHART_FONT_FAMILY = '"IBM Plex Sans", system-ui, sans-serif';
/** The exact font string both the measurer and ECharts' textStyle use. */
export const CHART_FONT = `${CHART_FONT_PX}px ${CHART_FONT_FAMILY}`;

/** Line pitch for chart labels; scripts with marks above/below get more. */
export const LINE_HEIGHT = 16;
export const TALL_LINE_HEIGHT = 18;
/** Sub-pixel rounding guard added to every measured line. */
export const WIDTH_SAFETY = 2;

/**
 * Devanagari, Bengali, Gurmukhi, Gujarati, Oriya, Tamil, Telugu, Kannada,
 * Malayalam, Thai and Arabic carry marks above and below the line.
 */
const TALL_SCRIPT =
  /[ऀ-ൿ฀-๿؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]/;

export function lineHeightFor(labels: readonly string[]): number {
  return labels.some((l) => TALL_SCRIPT.test(l)) ? TALL_LINE_HEIGHT : LINE_HEIGHT;
}

// ---------------------------------------------------------------------------
// Segmentation
// ---------------------------------------------------------------------------

type SegmenterCtor = new (
  locale?: string,
  options?: { granularity?: 'grapheme' | 'word' | 'sentence' },
) => { segment(input: string): Iterable<{ segment: string; isWordLike?: boolean }> };

interface SegmenterCache {
  ctor: SegmenterCtor | null;
  grapheme: InstanceType<SegmenterCtor> | null;
  word: InstanceType<SegmenterCtor> | null;
}

const segCache: SegmenterCache = { ctor: null, grapheme: null, word: null };

/** The Segmenter instances, created once (and again only if the global changes). */
function segmenters(): SegmenterCache | null {
  const ctor =
    typeof Intl !== 'undefined'
      ? ((Intl as unknown as { Segmenter?: SegmenterCtor }).Segmenter ?? null)
      : null;
  if (!ctor) return null;
  if (segCache.ctor !== ctor) {
    segCache.ctor = ctor;
    segCache.grapheme = new ctor(undefined, { granularity: 'grapheme' });
    segCache.word = new ctor(undefined, { granularity: 'word' });
  }
  return segCache;
}

/** User-perceived characters. Never splits a cluster when Intl.Segmenter exists. */
export function graphemes(text: string): string[] {
  const seg = segmenters();
  if (!seg?.grapheme) return Array.from(text);
  const out: string[] = [];
  for (const s of seg.grapheme.segment(text)) out.push(s.segment);
  return out;
}

/** Word pieces of a run with no spaces (Thai, CJK); code-point runs without a Segmenter. */
function wordPieces(run: string): string[] {
  const seg = segmenters();
  if (!seg?.word) return [run];
  const out: string[] = [];
  for (const s of seg.word.segment(run)) {
    // Punctuation stays glued to the piece before it, so a line never starts with ')'.
    if (!s.isWordLike && out.length > 0) out[out.length - 1] += s.segment;
    else out.push(s.segment);
  }
  return out;
}

// ---------------------------------------------------------------------------
// Measuring
// ---------------------------------------------------------------------------

/** Rough advance of one grapheme, in em. Errs wide: an over-estimate only costs space. */
function graphemeEm(g: string): number {
  const cp = g.codePointAt(0) ?? 0;
  if (g === ' ') return 0.28;
  if (cp < 0x80) {
    if ('iIjl.,:;!|\'`'.includes(g)) return 0.3;
    if ('ftr()[]{}'.includes(g)) return 0.38;
    if ('mwMW@%&'.includes(g)) return 0.86;
    if (g >= 'A' && g <= 'Z') return 0.66;
    if (g >= '0' && g <= '9') return 0.57;
    return 0.54;
  }
  const wide =
    (cp >= 0x1100 && cp <= 0x115f) ||
    (cp >= 0x2e80 && cp <= 0xa4cf) ||
    (cp >= 0xac00 && cp <= 0xd7a3) ||
    (cp >= 0xf900 && cp <= 0xfaff) ||
    (cp >= 0xfe30 && cp <= 0xfe4f) ||
    (cp >= 0xff00 && cp <= 0xff60) ||
    (cp >= 0x1f000 && cp <= 0x1faff) ||
    (cp >= 0x20000 && cp <= 0x3fffd);
  return wide ? 1.0 : 0.62;
}

/** Deterministic width estimate (no canvas): node, jsdom, SSR. */
export function estimateWidth(text: string, px = CHART_FONT_PX): number {
  let em = 0;
  for (const g of graphemes(text)) em += graphemeEm(g);
  return em * px;
}

let ctx2d: CanvasRenderingContext2D | null | undefined;

function canvasContext(): CanvasRenderingContext2D | null {
  if (ctx2d !== undefined) return ctx2d;
  ctx2d = null;
  try {
    const isJsdom =
      typeof navigator !== 'undefined' && /jsdom/i.test(navigator.userAgent || '');
    if (typeof document !== 'undefined' && !isJsdom) {
      ctx2d = document.createElement('canvas').getContext('2d');
    }
  } catch {
    ctx2d = null;
  }
  return ctx2d;
}

const widthCache = new Map<string, Map<string, number>>();
let fontsVersion = 0;
const MAX_CACHED = 4000;

/** Forget cached widths (after a web font finishes loading). */
export function resetMeasureCache(): void {
  widthCache.clear();
  fontsVersion += 1;
}

/** Bumped by resetMeasureCache, so layouts can key on it. */
export function measureVersion(): number {
  return fontsVersion;
}

/** Width of `text` in `font`: canvas measureText when there is one, else the estimate. */
export function measureText(text: string, font: string = CHART_FONT): number {
  let perFont = widthCache.get(font);
  if (!perFont) {
    perFont = new Map();
    widthCache.set(font, perFont);
  }
  const hit = perFont.get(text);
  if (hit !== undefined) return hit;
  const ctx = canvasContext();
  let w: number;
  if (ctx) {
    ctx.font = font;
    w = ctx.measureText(text).width;
  } else {
    const px = Number(/(\d+(?:\.\d+)?)px/.exec(font)?.[1] ?? CHART_FONT_PX);
    w = estimateWidth(text, px);
  }
  // Review R6: evict the oldest quarter instead of clearing everything, so a
  // large chart does not re-measure every label once the cache fills.
  if (perFont.size >= MAX_CACHED) {
    let drop = MAX_CACHED >> 2;
    for (const key of perFont.keys()) {
      perFont.delete(key);
      if (--drop <= 0) break;
    }
  }
  perFont.set(text, w);
  return w;
}

/** A Measure bound to one font. */
export function measurerFor(font: string = CHART_FONT): Measure {
  return (text: string) => measureText(text, font);
}

// ---------------------------------------------------------------------------
// Wrapping
// ---------------------------------------------------------------------------

/**
 * Break `text` into lines no wider than `maxWidth` under `measure`.
 *
 * Breaks at spaces first; a run with no spaces that is still too wide is split
 * into words (Intl.Segmenter, so Thai/CJK break between words), and a word
 * that is wider than a whole line is split between graphemes, never inside
 * one. Spaces at line ends are trimmed. Explicit newlines are kept. Nothing is
 * ever dropped: joining the lines (with the trimmed spaces) gives the label.
 */
export function wrapLines(text: string, maxWidth: number, measure: Measure): string[] {
  return wrapLinesInfo(text, maxWidth, measure).lines;
}

/**
 * `wrapLines` plus whether any word had to be split between graphemes. A
 * column axis uses that to prefer the bar list over a label like "Overs/eas".
 */
export function wrapLinesInfo(
  text: string,
  maxWidth: number,
  measure: Measure,
): { lines: string[]; splitWord: boolean } {
  const info = { splitWord: false };
  const out: string[] = [];
  for (const para of String(text ?? '').split(/\r\n|\r|\n/)) {
    out.push(...wrapParagraph(para, maxWidth, measure, info));
  }
  return { lines: out.length ? out : [''], splitWord: info.splitWord };
}

function fits(text: string, maxWidth: number, measure: Measure): boolean {
  return measure(text) + WIDTH_SAFETY <= maxWidth;
}

function wrapParagraph(
  text: string,
  maxWidth: number,
  measure: Measure,
  info: { splitWord: boolean },
): string[] {
  const trimmed = text.trim();
  if (!trimmed) return [''];
  if (!(maxWidth > 0) || fits(trimmed, maxWidth, measure)) return [trimmed];

  const lines: string[] = [];
  let line = '';
  const flush = () => {
    if (line) lines.push(line);
    line = '';
  };
  const place = (piece: string, joiner: string) => {
    const candidate = line ? line + joiner + piece : piece;
    if (fits(candidate, maxWidth, measure)) {
      line = candidate;
      return true;
    }
    return false;
  };

  for (const run of trimmed.split(/\s+/)) {
    if (place(run, ' ')) continue;
    flush();
    if (fits(run, maxWidth, measure)) {
      line = run;
      continue;
    }
    // A run wider than a line: words first, then graphemes.
    for (const word of wordPieces(run)) {
      if (place(word, '')) continue;
      flush();
      if (fits(word, maxWidth, measure)) {
        line = word;
        continue;
      }
      info.splitWord = true;
      for (const g of graphemes(word)) {
        if (place(g, '')) continue;
        flush();
        line = g; // a single grapheme always goes on a line, even if wider
      }
    }
  }
  flush();
  return lines.length ? lines : [''];
}

/** Widest line under `measure`. */
export function widest(lines: readonly string[], measure: Measure): number {
  let w = 0;
  for (const l of lines) w = Math.max(w, measure(l));
  return w;
}
