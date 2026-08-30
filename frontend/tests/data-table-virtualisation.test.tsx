// @vitest-environment jsdom
/**
 * The table under a Salesforce answer must show EVERY record that matched.
 *
 * It used to render one <tr> per row with no windowing. That is fine for a
 * handful of rows and ruinous for a real result: 28,230 records x 10 columns
 * is 282,300 cells in the DOM, which locks the tab. The fix mounts only the
 * rows inside the scroll viewport (plus overscan) and holds the scrollbar at
 * its true height with spacer rows — so the full result is present for
 * sorting and for the CSV download, without the DOM cost.
 *
 * These tests pin both halves: small results are NOT windowed (no behaviour
 * change), and large ones are.
 */
import { describe, expect, it } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { DataTable } from '../components/DataTable';

function rows(n: number) {
  return Array.from({ length: n }, (_, i) => ({
    Id: `a03Ps${String(i).padStart(6, '0')}`,
    Name: `I-${String(i).padStart(6, '0')}`,
    Round__c: 'Final',
    Interview_Status__c: 'Completed',
  }));
}

const dataRows = () => screen.queryAllByRole('row').filter((r) => r.hasAttribute('data-row'));

describe('DataTable', () => {
  it('renders every row when the result is small (unchanged behaviour)', () => {
    cleanup();
    render(<DataTable rows={rows(120)} csvName="t" />);
    expect(dataRows()).toHaveLength(120);
    expect(screen.getByText('120 rows')).toBeTruthy();
  });

  it('mounts only a window of a large result, but reports the true count', () => {
    cleanup();
    render(<DataTable rows={rows(5000)} csvName="t" />);
    const mounted = dataRows().length;
    // Far fewer than 5,000 in the DOM...
    expect(mounted).toBeGreaterThan(0);
    expect(mounted).toBeLessThan(500);
    // ...while the header still states the whole result.
    expect(screen.getByText('5,000 rows')).toBeTruthy();
  });

  it('states how many matched when the result was truncated', () => {
    cleanup();
    render(<DataTable rows={rows(2000)} truncated totalRows={28230} csvName="t" />);
    expect(
      screen.getByText('2,000 rows of 28,230 matching — narrow the question to see the rest.'),
    ).toBeTruthy();
  });

  it('keeps the no-rows message', () => {
    cleanup();
    render(<DataTable rows={[]} csvName="t" />);
    expect(screen.getByText('The query returned no rows.')).toBeTruthy();
  });
});
