/**
 * CSV formula-injection guard (charts design v2 §8.6, C18). Made-up cells.
 *
 * A text cell starting with = + - @ TAB or CR runs as a formula in Excel or
 * Sheets. Salesforce values and Mermaid labels are untrusted text, so such a
 * cell gets a leading apostrophe; real numbers are left alone.
 */

import { describe, expect, it } from 'vitest';
import { neutraliseFormula, rowsToCsv } from '../lib/csv';

describe('neutraliseFormula', () => {
  it.each(['=1+1', '+x', '-x', '@SUM(A1)', '\t=cmd', '\r=cmd', '=HYPERLINK("http://example.invalid","x")'])(
    'prefixes %j',
    (cell) => {
      expect(neutraliseFormula(cell)).toBe(`'${cell}`);
    },
  );

  it.each(['-5', '-5.2', '1e3', '+7', 'Owner A', '', 'a=b'])('leaves %j alone', (cell) => {
    expect(neutraliseFormula(cell)).toBe(cell);
  });

  // Review R11: number-like text stays a number in a spreadsheet.
  it.each(['-1,000', '-12.5%', '-$5', '+1 555 0100', '-', '+44 20 7946 0000', '-€1.234,50'])(
    'leaves number-like %j alone',
    (cell) => {
      expect(neutraliseFormula(cell)).toBe(cell);
    },
  );

  it.each(['-1+cmd', '+1,2,SUM(A1)', '-2+3+cmd|\' /C calc\'!A0', '-A1', '+1)', '-1 x'])(
    'still guards %j',
    (cell) => {
      expect(neutraliseFormula(cell)).toBe(`'${cell}`);
    },
  );
});

describe('rowsToCsv', () => {
  it('guards text cells and keeps numbers as numbers', () => {
    const csv = rowsToCsv([
      { name: '=1+1', value: -5 },
      { name: '@evil', value: '-5.2' },
      { name: 'Plain, with comma', value: 3 },
    ]);
    const lines = csv.trim().split('\r\n');
    expect(lines[0]).toBe('name,value');
    expect(lines[1]).toBe("'=1+1,-5");
    expect(lines[2]).toBe("'@evil,-5.2");
    expect(lines[3]).toBe('"Plain, with comma",3');
  });

  it('quotes a guarded cell that also needs quoting', () => {
    const csv = rowsToCsv([{ a: '=SUM(1,2)' }]);
    expect(csv.split('\r\n')[1]).toBe(`"'=SUM(1,2)"`);
  });
});
