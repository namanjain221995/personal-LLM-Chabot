/**
 * Mermaid xychart / pie → ChartModel (charts design v2 §10). The live shapes
 * (counts and shapes only were read from the DB) are reproduced with made-up
 * labels and numbers.
 */

import { describe, expect, it } from 'vitest';
import { isChartHead, parseMermaidChart, MERMAID_CHART_LIMITS } from '../lib/mermaidChart';

const NAMES = Array.from({ length: 26 }, (_, i) => `Owner ${String.fromCharCode(65 + i)} of the team ${i}`);

/** The complaint shape: 26 bare items with spaces, untitled x-axis, quoted y title + range, one unnamed series. */
const COMPLAINT = [
  'xychart-beta',
  '    title "Records by owner"',
  `    x-axis [${NAMES.join(', ')}]`,
  '    y-axis "Count" 0 --> 3500',
  `    bar [${NAMES.map((_, i) => 3091 - i * 100).join(', ')}]`,
].join('\n');

function parse(code: string) {
  return parseMermaidChart(code)?.model ?? null;
}

describe('live shapes', () => {
  it('converts the 26-bar complaint block with full labels', () => {
    const m = parse(COMPLAINT)!;
    expect(m).not.toBeNull();
    expect(m.requested).toBe('bar');
    expect(m.title).toBe('Records by owner');
    expect(m.x.cats.map((c) => c.label)).toEqual(NAMES);
    expect(m.series).toHaveLength(1);
    expect(m.series[0].label).toBe('Count');
    expect(m.series[0].values[0]).toBe(3091);
    expect(m.axisCaptions).toEqual({ x: undefined, y: 'Count' });
    expect(m.origin).toBe('mermaid');
  });

  it('reads `pie title …` on the head line and keeps percent wholes', () => {
    const m = parse('pie title Share of stages\n  "Applied" : 30\n  "Screened" : 30\n  "Offer" : 25\n  "Placed" : 15')!;
    expect(m.requested).toBe('pie');
    expect(m.title).toBe('Share of stages');
    expect(m.valueFlags.percentWhole).toBe(true);
    expect(m.x.cats.map((c) => c.label)).toEqual(['Applied', 'Screened', 'Offer', 'Placed']);
  });

  it('names unnamed series "Series n" and keeps names that are given', () => {
    const m = parse('xychart-beta\n x-axis [a, b]\n bar [1, 2]\n line [3, 4]')!;
    expect(m.requested).toBe('combo');
    expect(m.series.map((s) => s.label)).toEqual(['Series 1', 'Series 2']);
    const named = parse('xychart-beta\n x-axis [a, b]\n line "Plan" [1, 2]\n line "Actual" [3, 4]')!;
    expect(named.series.map((s) => s.label)).toEqual(['Plan', 'Actual']);
    expect(named.requested).toBe('line');
  });
});

describe('grammar', () => {
  it('accepts xychart and `horizontal`', () => {
    expect(parse('xychart horizontal\n x-axis [a, b]\n bar [1, 2]')!.requested).toBe('horizontal_bar');
  });
  it('accepts a quoted or one-word x title before the list', () => {
    expect(parse('xychart-beta\n x-axis "Month name" [Jan, Feb]\n bar [1, 2]')!.x.label).toBe('Month name');
    expect(parse('xychart-beta\n x-axis Month [Jan, Feb]\n bar [1, 2]')!.x.label).toBe('Month');
  });
  it('builds categories from a numeric x range', () => {
    const m = parse('xychart-beta\n x-axis "Score" 0 --> 100\n line [1, 2, 3]')!;
    expect(m.x.cats.map((c) => c.label)).toEqual(['0', '50', '100']);
  });
  it('splits on commas outside quotes, and treats curly quotes as quotes', () => {
    const m = parse('xychart-beta\n x-axis ["Smith, Ann", “Lee, Bo”, Plain]\n bar [1, 2, 3]')!;
    expect(m.x.cats.map((c) => c.label)).toEqual(['Smith, Ann', 'Lee, Bo', 'Plain']);
  });
  it('drops a trailing %% comment and skips %% lines', () => {
    const m = parse('xychart-beta\n %% a comment\n x-axis [a, b] %% trailing\n bar [1, 2]')!;
    expect(m.x.cats).toHaveLength(2);
  });
  it('reads a multi-line accDescr block and accTitle', () => {
    const m = parse('xychart-beta\n accTitle: Owners\n accDescr {\n  Counts per owner\n  this year\n }\n x-axis [a, b]\n bar [1, 2]')!;
    expect(m.a11y).toEqual({ title: 'Owners', description: 'Counts per owner this year' });
  });
  it('continues a list onto the next line', () => {
    const m = parse('xychart-beta\n x-axis [a, b,\n c]\n bar [1, 2,\n 3]')!;
    expect(m.x.cats.map((c) => c.label)).toEqual(['a', 'b', 'c']);
    expect(m.series[0].values).toEqual([1, 2, 3]);
  });
  it('ignores a preamble', () => {
    const m = parse('---\nconfig:\n  theme: base\n---\n%%{init: {"theme":"dark"}}%%\nxychart-beta\n x-axis [a]\n bar [1]');
    expect(m).not.toBeNull();
  });
  it('accepts `area` leniently and a 0 pie slice', () => {
    expect(parse('xychart-beta\n x-axis [a, b]\n area [1, 2]')!.series[0].kind).toBe('area');
    const pie = parse('pie\n "A" : 0\n "B" : 3')!;
    expect(pie.series[0].values).toEqual([3, 0]);
  });
  it('accepts showData', () => {
    expect(parse('pie showData\n title T\n "A" : 1\n "B" : 2')!.title).toBe('T');
  });
  it('accepts a y range written `0 to 3500` (seen live; ignored like any range)', () => {
    const m = parse('xychart-beta\n title "Placements"\n x-axis [Jan 1, Feb 2]\n y-axis "Count" 0 to 3500\n bar [10, 20]')!;
    expect(m.series[0].values).toEqual([10, 20]);
    expect(m.axisCaptions?.y).toBe('Count');
    // An x range must still use `-->`: it creates categories.
    expect(parse('xychart-beta\n x-axis 0 to 10\n bar [1, 2]')).toBeNull();
  });

  it('keeps a y range that starts at 0 and ignores any other (data never clipped)', () => {
    expect(parse('xychart-beta\n x-axis [a, b]\n y-axis "Revenue" 4000 --> 11000\n bar [5000, 6000]')!.series[0].values).toEqual([5000, 6000]);
  });
});

describe('failures fall back to mermaid (null)', () => {
  const cases: Array<[string, string]> = [
    ['unknown statement', 'xychart-beta\n x-axis [a]\n bar [1]\n frobnicate 3'],
    ['length mismatch', 'xychart-beta\n x-axis [a, b]\n bar [1, 2, 3]'],
    ['series lengths differ', 'xychart-beta\n x-axis [a, b]\n bar [1, 2]\n line [1]'],
    ['non-number', 'xychart-beta\n x-axis [a, b]\n bar [1, two]'],
    ['unclosed list', 'xychart-beta\n x-axis [a, b\n bar [1, 2]'],
    ['negative pie value', 'pie\n "A" : -1\n "B" : 2'],
    ['duplicate pie labels', 'pie\n "A" : 1\n "A" : 2'],
    ['not a chart', 'flowchart TD\n A --> B'],
  ];
  it.each(cases)('%s', (_, code) => {
    expect(parse(code)).toBeNull();
  });

  it('more than 8 series', () => {
    const series = Array.from({ length: 9 }, () => ' line [1, 2]').join('\n');
    expect(parse(`xychart-beta\n x-axis [a, b]\n${series}`)).toBeNull();
  });
  it('more than 500 points', () => {
    const n = MERMAID_CHART_LIMITS.maxPoints + 1;
    const xs = Array.from({ length: n }, (_, i) => `c${i}`).join(', ');
    const ys = Array.from({ length: n }, (_, i) => i).join(', ');
    expect(parse(`xychart-beta\n x-axis [${xs}]\n bar [${ys}]`)).toBeNull();
  });
  it('a 301-character label', () => {
    expect(parse(`xychart-beta\n x-axis [${'x'.repeat(301)}, b]\n bar [1, 2]`)).toBeNull();
    expect(parse(`xychart-beta\n x-axis [${'x'.repeat(300)}, b]\n bar [1, 2]`)).not.toBeNull();
  });
  it('a 20,001-character source', () => {
    const pad = `%%${'x'.repeat(MERMAID_CHART_LIMITS.maxSource)}\n`;
    expect(parse(`${pad}xychart-beta\n x-axis [a]\n bar [1]`)).toBeNull();
  });
});

describe('isChartHead (streaming guard)', () => {
  it('recognises chart heads, ignoring preamble and comments', () => {
    expect(isChartHead('pie')).toBe(true);
    expect(isChartHead('%% note\nxychart-beta\n')).toBe(true);
    expect(isChartHead('flowchart TD')).toBe(false);
    expect(isChartHead('')).toBe(false);
  });
});

describe('review fixes (2026-11-25)', () => {
  it.each([
    ['y-axis + 19,900 digits', `xychart-beta\n y-axis ${'9'.repeat(19_900)}\n bar [1, 2]`],
    ['x-axis + digits', `xychart-beta\n x-axis ${'1'.repeat(19_900)}\n bar [1, 2]`],
    ['x-axis + dot-digits', `xychart-beta\n x-axis ${'.1'.repeat(9_900)}\n bar [1, 2]`],
    ['y-axis spaces then digits', `xychart-beta\n y-axis ${' '.repeat(9_000)}${'5'.repeat(9_000)}\n bar [1, 2]`],
    ['9,900 open brackets', `xychart-beta\n x-axis\n${'[\n'.repeat(9_900)}`],
  ])('R8: a hostile 20 KB block (%s) is refused in linear time', (_name, src) => {
    const t0 = performance.now();
    const r = parseMermaidChart(src.slice(0, MERMAID_CHART_LIMITS.maxSource));
    const ms = performance.now() - t0;
    expect(r).toBeNull();
    expect(ms).toBeLessThan(150); // was 420-1,045 ms before the fix
  });

  it('R8: ranges still parse from the end — `0 --> 3500`, `0 to 100`, exponents, negatives', () => {
    expect(parseMermaidChart(COMPLAINT)).not.toBeNull();
    const lenient = parseMermaidChart('xychart-beta\n x-axis [a, b]\n y-axis "Share" 0 to 100\n bar [1, 2]');
    expect(lenient?.model.axisCaptions?.y).toBe('Share');
    const range = parseMermaidChart('xychart-beta\n x-axis "Year" -1e1 --> 10\n line [1, 2, 3]')!;
    expect(range.model.x.cats.map((c) => c.label)).toEqual(['-10', '0', '10']);
    expect(range.model.x.label).toBe('Year');
  });

  it('R9: Table columns never collide with the x title, and "__proto__" is a real column', () => {
    const same = parseMermaidChart('xychart-beta\n x-axis "Count" [a, b]\n y-axis "Count"\n bar [1, 2]')!;
    expect(same.model.tableRows).toEqual([
      { Count: 'a', 'Count (2)': 1 },
      { Count: 'b', 'Count (2)': 2 },
    ]);
    const proto = parseMermaidChart('xychart-beta\n x-axis [a, b]\n bar "__proto__" [1, 2]\n bar "B" [3, 4]')!;
    const row = proto.model.tableRows[0];
    expect(Object.prototype.hasOwnProperty.call(row, '__proto__')).toBe(true);
    expect(Object.keys(row)).toEqual(['Category', '__proto__', 'B']);
  });

  it('R14: the streaming head check reads only the head of a huge block', () => {
    const big = `pie title "Split"\n${'"a" : 1\n'.repeat(25_000)}`;
    const t0 = performance.now();
    for (let i = 0; i < 50; i++) expect(isChartHead(big)).toBe(true);
    expect(performance.now() - t0).toBeLessThan(150);
    expect(isChartHead('%% note\n\nflowchart LR\n a --> b')).toBe(false);
  });
});
