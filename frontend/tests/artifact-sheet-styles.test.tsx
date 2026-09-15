// @vitest-environment jsdom
/**
 * SheetViewer paints the styles the grid route resolves for an xlsx:
 * header and cell fills/colours/weights as inline styles, values as their
 * number format shows them, and nothing that is not a #RRGGBB colour.
 */
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { cellCss, cleanCellStyle, fromGrid, SheetViewer } from '@/components/artifacts/SheetViewer';

const FILE_ID = '0123456789abcdef';

function styledGrid() {
  return {
    sheets: ['Tasks'],
    sheet: 'Tasks',
    columns: ['ID', 'Status', 'Budget'],
    rows: [
      ['T-1', 'Blocked', 1000.5],
      ['Total', null, '=SUBTOTAL(109,C2:C2)'],
    ],
    total_rows: 2,
    total_columns: 3,
    truncated: false,
    formulas_as_text: true,
    header_styles: [{ fill: '#1F3864', color: '#FFFFFF', bold: true }, { fill: '#1F3864', color: '#FFFFFF', bold: true }, { fill: 'url(x)' }],
    cell_styles: {
      '0:1': { fill: '#C62828', color: '#FFFFFF' },
      '1:0': { fill: '#DCE6F2', color: '#1F3864', bold: true },
      'x:y': { fill: '#000000' },
    },
    display: [
      ['T-1', 'Blocked', '₹1,000.50'],
      ['Total', '', '₹1,000.50'],
    ],
  };
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('cleanCellStyle', () => {
  it('keeps only hex colours and true flags', () => {
    expect(cleanCellStyle({ fill: '#abcdef', color: 'red', bold: 'yes', italic: true, underline: true })).toEqual({
      fill: '#abcdef',
      italic: true,
      underline: true,
    });
    expect(cleanCellStyle({ fill: 'url(javascript:alert(1))', color: '#12345' })).toBeUndefined();
    expect(cleanCellStyle(null)).toBeUndefined();
    expect(cellCss({ fill: '#C62828', color: '#FFFFFF', bold: true })).toEqual({ backgroundColor: '#C62828', color: '#FFFFFF', fontWeight: 600 });
  });

  it('folds the styled grid answer into the view', () => {
    const view = fromGrid(styledGrid() as never);
    expect(view.sheet?.display?.[0][2]).toBe('₹1,000.50');
    expect(view.sheet?.cellStyles).toEqual({ '0:1': { fill: '#C62828', color: '#FFFFFF' }, '1:0': { fill: '#DCE6F2', color: '#1F3864', bold: true } });
    expect(view.sheet?.headerStyles?.[2]).toBeUndefined();
  });
});

describe('SheetViewer with styles', () => {
  it('paints fills, colours and formatted values', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(JSON.stringify(styledGrid()), { status: 200, headers: { 'Content-Type': 'application/json' } })),
    );
    render(<SheetViewer artifactId="art_1" version={1} fileId={FILE_ID} title="Tracker" />);
    await waitFor(() => expect(screen.getByTestId('sheet-viewer')).toBeTruthy());
    const blocked = screen.getByText('Blocked').closest('td') as HTMLElement;
    expect(blocked.style.backgroundColor).toBe('rgb(198, 40, 40)');
    expect(blocked.style.color).toBe('rgb(255, 255, 255)');
    expect(blocked.getAttribute('data-styled')).toBe('true');
    const header = screen.getByTitle('A · ID') as HTMLElement;
    expect(header.style.backgroundColor).toBe('rgb(31, 56, 100)');
    expect(screen.getAllByText('₹1,000.50').length).toBe(2);
    const total = screen.getAllByText('₹1,000.50')[1].closest('td') as HTMLElement;
    expect(total.getAttribute('data-formula')).toBe('true');
    expect(total.getAttribute('title')).toContain('SUBTOTAL');
    const label = screen.getByText('Total').closest('td') as HTMLElement;
    expect(label.style.fontWeight).toBe('600');
  });
});
