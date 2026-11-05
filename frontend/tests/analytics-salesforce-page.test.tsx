// @vitest-environment jsdom
/**
 * The Salesforce analytics page must not report what nobody measured.
 *
 * The API's `live` total filters on a value no writer ever stored, so it is
 * always 0 and `synced` always equals `answers`, older answers that really read
 * the org included. The fixture sends non-zero values for both, so a stat or a
 * chart series drawn from them shows up here.
 */
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const chart = vi.hoisted(() => ({ names: [] as string[] }));

vi.mock('next/navigation', () => ({
  usePathname: () => '/admin/analytics/salesforce',
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

vi.mock('@/components/admin/analytics/AnalyticsChart', () => ({
  AnalyticsChart: ({ series }: { series: { name: string }[] }) => {
    chart.names = series.map((s) => s.name);
    return null;
  },
}));

vi.mock('@/components/admin/analytics/useAnalytics', () => ({
  useAnalytics: () => ({
    loading: false,
    error: null,
    reload: () => undefined,
    data: {
      range: { key: '30d', bucket: 'day' },
      totals: {
        answers: 41,
        users: 4,
        failed: 2,
        live: 7,
        synced: 34,
        sql_route: 30,
        dataset_route: 11,
        intents: 9,
        success_rate: 95.1,
      },
      deltas: { answers: null },
      series: [{ bucket: '2026-11-01', answers: 41, live: 7 }],
    },
  }),
}));

import SalesforceAnalyticsPage from '@/app/admin/analytics/salesforce/page';

afterEach(cleanup);

describe('the Salesforce analytics page', () => {
  it('has no live stat, no live chart series and no synced split', () => {
    const { container } = render(<SalesforceAnalyticsPage />);
    expect(chart.names).toEqual(['Answers']);
    // innerHTML, not textContent: a section's hint lives in title and aria-label.
    expect(container.innerHTML).not.toContain('Live against the org');
    expect(container.innerHTML).not.toContain('From the synced copy');
  });

  it('says plainly where answers come from, old ones included', () => {
    render(<SalesforceAnalyticsPage />);
    const hint = screen.getByLabelText(/nothing queries the org now/).getAttribute('aria-label');
    expect(hint).toContain('Older answers may have read the org directly');
  });

  it('keeps the counts that are measured', () => {
    render(<SalesforceAnalyticsPage />);
    expect(screen.getByText('Structured queries').parentElement?.textContent).toContain('30');
    expect(screen.getByText('Dataset answers').parentElement?.textContent).toContain('11');
    expect(screen.getByText('Failed answers').parentElement?.textContent).toContain('2');
  });
});
