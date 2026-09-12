// @vitest-environment jsdom
/**
 * SheetViewer: one tab per sheet, formulas shown as text with a ƒ marker
 * and never evaluated, and a truncation notice when the server's window is
 * smaller than the sheet.
 */
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { cellAddress, columnLetter, fromGrid, SheetViewer } from '@/components/artifacts/SheetViewer';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const ID = 'a3f9c2d1e4b5f6a7b8c9d0e1f2a3b4c5';
const FILE_ID = '0123456789abcdef';

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
    // CONTRACT-2 §9: the size reads "N rows · M columns" (was "N rows × M columns").
    expect(screen.getByTestId('sheet-size').textContent).toBe('1000 rows · 80 columns');
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

/* --------------------------------------------- the grid route (CONTRACT-2 §2, §11) */

/** What GET …/grid answers for a csv (render.preview.grid_for): one sheet, the file's title. */
const csvGrid = {
  sheets: ['IR Session Audit'],
  sheet: 'IR Session Audit',
  columns: ['Host', 'Candidate', 'Start', 'Audit Comments'],
  rows: [
    ['Priya', 'CAND-0001', '09:00', 'Late by 3 min'],
    ['', 'CAND-0002', '09:30', ''],
    ['Rahul', 'CAND-0003', '10:00', 'Camera off until 10:04'],
  ],
  total_rows: 30,
  total_columns: 4,
  truncated: true,
  formulas_as_text: true,
};

/** …and for a two-sheet xlsx by id: names only, the window of the sheet asked for. */
const xlsxGrid = (sheet: string | null) => ({
  sheets: ['Summary', 'Data'],
  sheet: sheet ?? 'Summary',
  columns: sheet === 'Data' ? ['col1', 'col2'] : ['Region', 'Revenue'],
  rows: sheet === 'Data' ? [[1, 2]] : [['North', 1200]],
  total_rows: sheet === 'Data' ? 1000 : 1,
  total_columns: 2,
  truncated: sheet === 'Data',
  formulas_as_text: true,
});

describe('SheetViewer — a file by id uses the grid route', () => {
  it('fetches grid?file=<id>&limit= for a CSV, shows no tab strip for its single sheet, and states "N rows · M columns"', async () => {
    const fetchMock = vi.fn(
      async (_url: string) => ({ ok: true, status: 200, json: async () => csvGrid }) as unknown as Response,
    );
    vi.stubGlobal('fetch', fetchMock);
    render(<SheetViewer artifactId={ID} version={1} fileId={FILE_ID} title="IR Session Audit" />);
    await flush();
    expect(fetchMock.mock.calls[0][0]).toBe(`/api/artifacts/${ID}/v/1/grid?file=${FILE_ID}&limit=200`);
    // One sheet: no tabs — a strip with one tab is a decoration.
    expect(screen.queryByRole('tablist')).toBeNull();
    const grid = screen.getByRole('tabpanel', { name: 'IR Session Audit of IR Session Audit' });
    expect(grid.querySelectorAll('tbody tr').length).toBe(3);
    // A blank source cell stays blank (CONTRACT-2 §6), and no ƒ appears when
    // the route says formulas are already text.
    expect(grid.querySelectorAll('td')[4].textContent).toBe('');
    expect(grid.querySelectorAll('td[data-formula="true"]').length).toBe(0);
    expect(screen.getByTestId('sheet-size').textContent).toBe('30 rows · 4 columns');
    // The truncation notice comes from the response, not from counting rows.
    expect(screen.getByTestId('sheet-truncated').textContent).toContain('Showing the first 3 rows');
  });

  it('shows tabs for a workbook with several sheets and asks the grid for the one selected', async () => {
    const fetchMock = vi.fn(async (url: string) => {
      const sheet = new URL(url, 'http://localhost').searchParams.get('sheet');
      return { ok: true, status: 200, json: async () => xlsxGrid(sheet) } as unknown as Response;
    });
    vi.stubGlobal('fetch', fetchMock);
    render(<SheetViewer artifactId={ID} version={1} fileId={FILE_ID} title="Budget" />);
    await flush();
    const tabs = screen.getAllByRole('tab');
    expect(tabs.map((t) => t.textContent)).toEqual(['Summary', 'Data']);
    expect(screen.getByTestId('sheet-size').textContent).toBe('1 row · 2 columns');
    fireEvent.click(tabs[1]);
    await flush();
    expect(fetchMock.mock.calls[1][0]).toBe(`/api/artifacts/${ID}/v/1/grid?file=${FILE_ID}&sheet=Data&limit=200`);
    expect(screen.getByRole('tab', { name: 'Data' }).getAttribute('aria-selected')).toBe('true');
    expect(screen.getByTestId('sheet-size').textContent).toBe('1000 rows · 2 columns');
    expect(screen.getByTestId('sheet-truncated')).toBeTruthy();
    // Never the older /sheets alias once the file has an id.
    expect(fetchMock.mock.calls.every(([url]) => !String(url).includes('/sheets'))).toBe(true);
  });

  it('folds the flat grid answer into the one view the table draws', () => {
    const view = fromGrid(csvGrid);
    expect(view.sheets).toEqual([{ name: 'IR Session Audit' }]);
    expect(view.sheet?.columns).toEqual(csvGrid.columns);
    expect(view.sheet?.rows.length).toBe(3);
    expect(view.sheet?.truncated).toBe(true);
    expect(view.totalRows).toBe(30);
    expect(view.totalColumns).toBe(4);
    // An empty answer is "nothing to show", not a crash.
    expect(fromGrid({ sheets: [], sheet: '', columns: [], rows: [], total_rows: 0, total_columns: 0, truncated: false }).sheet).toBeNull();
  });

  it('refuses to build a grid URL for a malformed id and falls back to the workbook alias', async () => {
    const fetchMock = serve();
    render(<SheetViewer artifactId={ID} version={1} fileId="legacy:budget.xlsx" title="Budget" />);
    await flush();
    expect(fetchMock.mock.calls[0][0]).toBe(`/api/artifacts/${ID}/v/1/sheets?rows=200&cols=50`);
  });
});
