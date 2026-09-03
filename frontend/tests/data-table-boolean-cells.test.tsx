// @vitest-environment jsdom
/**
 * L-19: a checkbox column is not a measure.
 *
 * DataTable decided "is this numeric?" with `!Number.isNaN(Number(value))`,
 * and `Number(true)` is 1 while `Number(false)` is 0 — so a real JSON boolean
 * was right-aligned in monospace, styled identically to a currency or a count.
 * The app already owns the correct predicate in lib/chartFormat (`isNumeric`,
 * commented "Booleans are NOT numbers"), which the chart path has used all
 * along; the table now goes through the same one, for BOTH the cell styling
 * and the sort comparator.
 *
 * `text-right font-mono` is the numeric presentation. Its absence is the text
 * presentation — there is no positive class for that.
 */
import { describe, expect, it } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { DataTable } from '../components/DataTable';
import { isNumeric } from '@/lib/chartFormat';

/** The one cell in a single-column, single-row table. */
function cellFor(value: unknown): HTMLElement {
  cleanup();
  render(<DataTable rows={[{ v: value }]} csvName="t" />);
  const cells = screen.getAllByRole('cell');
  return cells[0];
}

const looksNumeric = (el: HTMLElement) =>
  el.className.includes('text-right') && el.className.includes('font-mono');

describe('L-19 · numeric presentation matrix', () => {
  const NUMERIC: Array<[string, unknown]> = [
    ['0', 0],
    ['1', 1],
    ['"0"', '0'],
    ['"1"', '1'],
    ['42.5', 42.5],
    ['"42.5"', '42.5'],
  ];

  const NOT_NUMERIC: Array<[string, unknown]> = [
    ['true', true],
    ['false', false],
    ['"true"', 'true'],
    ['"false"', 'false'],
    ['null', null],
    ['undefined', undefined],
    ['""', ''],
  ];

  for (const [label, value] of NUMERIC) {
    it(`${label} renders with numeric presentation`, () => {
      expect(looksNumeric(cellFor(value))).toBe(true);
    });
  }

  for (const [label, value] of NOT_NUMERIC) {
    it(`${label} renders as text, not as a measure`, () => {
      expect(looksNumeric(cellFor(value))).toBe(false);
    });
  }
});

describe('L-19 · one predicate for the whole app', () => {
  it('the table agrees with lib/chartFormat cell by cell', () => {
    const values: unknown[] = [
      0, 1, 42.5, '0', '1', '42.5',
      true, false, 'true', 'false', 'False', '', '  ', 'abc', null, undefined,
    ];
    for (const v of values) {
      expect(looksNumeric(cellFor(v))).toBe(isNumeric(v as never));
    }
  });
});

describe('L-19 · the realistic table', () => {
  const rows = [
    { name: 'A', active: true, count: 10 },
    { name: 'B', active: false, count: 20 },
  ];

  it('aligns count as a number and leaves name and active as text', () => {
    cleanup();
    render(<DataTable rows={rows} csvName="t" />);
    const cells = screen.getAllByRole('cell');
    // Row 1: name | active | count
    expect(looksNumeric(cells[0])).toBe(false); // name  = "A"
    expect(looksNumeric(cells[1])).toBe(false); // active = true  ← the bug
    expect(looksNumeric(cells[2])).toBe(true); // count = 10
  });

  it('still prints the boolean values themselves', () => {
    cleanup();
    render(<DataTable rows={rows} csvName="t" />);
    expect(screen.getByText('true')).toBeTruthy();
    expect(screen.getByText('false')).toBeTruthy();
  });
});

describe('L-19 · sorting uses the same definition as styling', () => {
  const order = () =>
    screen
      .getAllByRole('row')
      .filter((r) => r.hasAttribute('data-row'))
      .map((r) => r.querySelector('td')?.textContent);

  it('sorts a numeric column numerically, not lexically', () => {
    cleanup();
    render(
      <DataTable
        rows={[{ n: 9 }, { n: 10 }, { n: 1 }]}
        csvName="t"
      />,
    );
    fireEvent.click(screen.getByLabelText('Sort by n'));
    // Lexical order would be 1, 10, 9.
    expect(order()).toEqual(['1', '9', '10']);
  });

  it('sorts a boolean column as text — false before true — without coercing to 0/1', () => {
    cleanup();
    render(
      <DataTable
        rows={[{ b: true }, { b: false }, { b: true }]}
        csvName="t"
      />,
    );
    fireEvent.click(screen.getByLabelText('Sort by b'));
    expect(order()).toEqual(['false', 'true', 'true']);
  });

  it('sorts string booleans identically to real booleans', () => {
    cleanup();
    render(
      <DataTable
        rows={[{ b: 'true' }, { b: 'false' }, { b: 'true' }]}
        csvName="t"
      />,
    );
    fireEvent.click(screen.getByLabelText('Sort by b'));
    expect(order()).toEqual(['false', 'true', 'true']);
  });
});
