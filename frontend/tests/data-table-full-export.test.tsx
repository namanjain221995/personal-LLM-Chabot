// @vitest-environment jsdom
/**
 * The Download CSV button must hand over the whole result, not the preview.
 *
 * Owner report 2026-08-31: a table captioned "10,000 of 10,423 matching"
 * downloaded 10,000 rows. The rows in this component ARE the preview by
 * design (see the virtualisation note) — so when the orchestrator has written
 * the complete result to /reports, the button links to THAT file instead of
 * re-serialising what is on screen.
 */
import { describe, expect, it } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { DataTable } from '../components/DataTable';

const preview = Array.from({ length: 100 }, (_, i) => ({ Id: i, Name: `I-${i}` }));

describe('DataTable download', () => {
  it('links to the full export when one exists', () => {
    cleanup();
    render(
      <DataTable
        rows={preview}
        truncated
        totalRows={10_423}
        csvName="interview-2026-08-31-1432.csv"
        fullCsvHref="/api/reports/interview-20260831-143200.csv"
        fullCsvRows={10_423}
      />,
    );
    const link = screen.getByRole('link', { name: /download csv/i });
    expect(link.getAttribute('href')).toBe(
      '/api/reports/interview-20260831-143200.csv',
    );
    // The downloaded file is named for the data, not for the app.
    expect(link.getAttribute('download')).toBe('interview-2026-08-31-1432.csv');
    // The count is on the button, so the user can see it is the whole thing.
    expect(link.textContent).toContain('10,423');
  });

  it('stops telling the user to narrow a question they answered correctly', () => {
    cleanup();
    render(
      <DataTable
        rows={preview}
        truncated
        totalRows={10_423}
        csvName="interview.csv"
        fullCsvHref="/api/reports/interview.csv"
        fullCsvRows={10_423}
      />,
    );
    expect(screen.getByText(/10,423 matching/).textContent).toContain(
      'download for the rest',
    );
  });

  it('still keeps the old advice when there is no file to download', () => {
    cleanup();
    render(
      <DataTable rows={preview} truncated totalRows={10_423} csvName="x.csv" />,
    );
    expect(screen.getByText(/10,423 matching/).textContent).toContain(
      'narrow the question',
    );
  });

  it('falls back to the in-browser download when no export was written', () => {
    cleanup();
    render(<DataTable rows={preview} csvName="interview.csv" />);
    expect(screen.getByRole('button', { name: /download csv/i })).toBeTruthy();
    expect(screen.queryByRole('link', { name: /download csv/i })).toBeNull();
  });
});
