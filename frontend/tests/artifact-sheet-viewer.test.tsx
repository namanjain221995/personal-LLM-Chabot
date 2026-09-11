// @vitest-environment jsdom
/**
 * SheetViewer: one tab per sheet, formulas shown as text with a ƒ marker
 * and never evaluated, and a truncation notice when the server's window is
 * smaller than the sheet.
 */
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { cellAddress, columnLetter, SheetViewer } from '@/components/artifacts/SheetViewer';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const ID = 'a3f9c2d1e4b5f6a7b8c9d0e1f2a3b4c5';

const summary = {
  sheets: [
    { name: 'Summary', rows: 3, cols: 3 },
    { name: 'Data', rows: 1000, cols: 80 },
  ],
  sheet: {
    name: 'Summary',
    columns: ['Region', 'Revenue', 'Share'],
    rows: [
      ['North', 1200, 0.4],
      ['South', 1800, 0.6],
      ['Total', null, null],
    ],
    truncated: false,
    formulas: { B4: '=SUM(B2:B3)', C4: '=SUM(C2:C3)' },
  },
};

const data = {
  sheets: summary.sheets,
  sheet: {
    name: 'Data',
    columns: Array.from({ length: 50 }, (_, i) => `col${i + 1}`),
    rows: Array.from({ length: 200 }, (_, r) => Array.from({ length: 50 }, (_, c) => r * 50 + c)),
    truncated: true,
    formulas: {},
  },
};

function serve() {
  const fetchMock = vi.fn(async (url: string) => {
    const sheet = new URL(url, 'http://localhost').searchParams.get('sheet');
    const body = sheet === 'Data' ? data : summary;
    return { ok: true, status: 200, json: async () => body } as unknown as Response;
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe('SheetViewer', () => {
  it('maps indices to Excel addresses', () => {
    expect(columnLetter(0)).toBe('A');
    expect(columnLetter(25)).toBe('Z');
    expect(columnLetter(26)).toBe('AA');
    expect(columnLetter(701)).toBe('ZZ');
    // Data row 0 is sheet row 2 — the header is row 1.
    expect(cellAddress(1, 2)).toBe('B4');
  });

  it('renders a tab per sheet and asks for the sheet a tab selects', async () => {
    const fetchMock = serve();
    render(<SheetViewer artifactId={ID} version={1} title="Budget" />);
    await flush();
    const tabs = screen.getAllByRole('tab');
    expect(tabs.map((t) => t.textContent)).toEqual(['Summary', 'Data']);
    expect(tabs[0].getAttribute('aria-selected')).toBe('true');
    expect(fetchMock.mock.calls[0][0]).toBe(`/api/artifacts/${ID}/v/1/sheets?rows=200&cols=50`);

    fireEvent.click(tabs[1]);
    await flush();
    expect(fetchMock.mock.calls[1][0]).toBe(`/api/artifacts/${ID}/v/1/sheets?sheet=Data&rows=200&cols=50`);
    expect(screen.getByRole('tab', { name: 'Data' }).getAttribute('aria-selected')).toBe('true');
  });

  it('shows a formula as text with a ƒ marker, never a computed value', async () => {
    serve();
    render(<SheetViewer artifactId={ID} version={1} title="Budget" />);
    await flush();
    const grid = screen.getByRole('tabpanel');
    const formulaCells = grid.querySelectorAll('td[data-formula="true"]');
    expect(formulaCells.length).toBe(2);
    expect(formulaCells[0].textContent).toContain('=SUM(B2:B3)');
    expect(within(formulaCells[0] as HTMLElement).getByLabelText('formula').textContent).toBe('ƒ');
    // 1200 + 1800 was NOT computed for the reader.
    expect(grid.textContent).not.toContain('3000');
    // Plain cells carry no marker.
    expect(grid.querySelectorAll('td').length - formulaCells.length).toBe(7);
    // Column letters and row numbers, so the formula's references can be found.
    expect(within(grid).getByText('B', { exact: true })).toBeTruthy();
    expect(within(grid).getByText('4', { exact: true })).toBeTruthy();
  });

  it('says so when the window is truncated', async () => {
    serve();
    render(<SheetViewer artifactId={ID} version={1} title="Budget" />);
    await flush();
    expect(screen.queryByTestId('sheet-truncated')).toBeNull();
    fireEvent.click(screen.getByRole('tab', { name: 'Data' }));
    await flush();
    const notice = screen.getByTestId('sheet-truncated');
    expect(notice.textContent).toContain('Showing the first 200 rows and 50 columns');
    expect(screen.getByText('1000 rows × 80 columns')).toBeTruthy();
  });

  it('says so for a workbook with no visible sheet instead of throwing on `sheet: null`', async () => {
    // preview.sheet_grid answers exactly this for a workbook with every
    // sheet hidden; reading `.rows` off null took the whole panel down.
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ sheets: [], sheet: null }) }) as unknown as Response),
    );
    render(<SheetViewer artifactId={ID} version={1} title="Budget" />);
    await flush();
    expect(screen.getByText('This workbook has no visible sheets.')).toBeTruthy();
    expect(screen.queryByRole('tablist')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull();
  });

  it('names a refusal and offers retry only for a server failure', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: false, status: 403, json: async () => ({ detail: 'You do not have access to this file.' }) }) as unknown as Response),
    );
    const { unmount } = render(<SheetViewer artifactId={ID} version={1} title="Budget" />);
    await flush();
    expect(screen.getAllByText('You do not have access to this file.').length).toBeGreaterThan(0);
    expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull();
    unmount();

    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: false, status: 500, json: async () => ({}) }) as unknown as Response),
    );
    render(<SheetViewer artifactId={ID} version={1} title="Budget" />);
    await flush();
    expect(screen.getByRole('button', { name: 'Try again' })).toBeTruthy();
  });
});
