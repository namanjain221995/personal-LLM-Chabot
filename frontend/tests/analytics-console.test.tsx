// @vitest-environment jsdom
/**
 * The analytics console's contracts.
 *
 * The one that matters most: A VALUE NOBODY MEASURED IS NOT ZERO. The
 * backend sends null for it, and every formatter, stat and chart in the
 * console has to carry that through to an em dash instead of quietly
 * rendering "0 tokens" for a period that predates the telemetry.
 *
 * After that: percentage changes that refuse to lie, states that hold their
 * height, infrastructure that explains its own absence, and a sidebar whose
 * Analytics section exists only for the capability that can actually read it.
 */
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import type { ComponentProps, ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('next/navigation', () => ({
  usePathname: () => '/admin',
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

vi.mock('next/link', () => ({
  __esModule: true,
  default: (props: ComponentProps<'a'> & { children?: ReactNode }) => {
    const { children, ...rest } = props;
    return <a {...rest}>{children}</a>;
  },
}));

import AdminLayout from '@/app/admin/layout';
import {
  ChartFrame,
  CoverageNote,
  Delta,
  InfraBlock,
  Meter,
  Num,
  Stat,
  TelemetryUnavailable,
} from '@/components/admin/analytics/ui';
import {
  NOT_MEASURED,
  bytes,
  compact,
  duration,
  durationFromSeconds,
  exact,
  hertz,
  percent,
  ratio,
  uptime,
} from '@/components/admin/analytics/format';

afterEach(cleanup);

// ---------------------------------------------------------------------------
// Null is not zero
// ---------------------------------------------------------------------------

describe('formatting a value nobody measured', () => {
  it('renders an em dash, never a zero', () => {
    expect(compact(null)).toBe(NOT_MEASURED);
    expect(compact(undefined)).toBe(NOT_MEASURED);
    expect(exact(null)).toBe(NOT_MEASURED);
    expect(duration(null)).toBe(NOT_MEASURED);
    expect(durationFromSeconds(null)).toBe(NOT_MEASURED);
    expect(percent(null)).toBe(NOT_MEASURED);
    expect(ratio(null)).toBe(NOT_MEASURED);
    expect(bytes(null)).toBe(NOT_MEASURED);
    expect(hertz(null)).toBe(NOT_MEASURED);
    expect(uptime(null)).toBe(NOT_MEASURED);
  });

  it('still renders a measured zero as zero', () => {
    expect(compact(0)).toBe('0');
    expect(duration(0)).toBe('0ms');
    expect(percent(0)).toBe('0.0%');
  });

  it('refuses NaN and Infinity, which are never real measurements', () => {
    expect(compact(Number.NaN)).toBe(NOT_MEASURED);
    expect(duration(Number.POSITIVE_INFINITY)).toBe(NOT_MEASURED);
  });
});

describe('compact numbers', () => {
  it('shortens at each magnitude and keeps one decimal below ten', () => {
    expect(compact(999)).toBe('999');
    expect(compact(1234)).toBe('1.2K');
    expect(compact(94_200)).toBe('94K');
    expect(compact(1_250_000)).toBe('1.3M');
    expect(compact(4_100_000_000)).toBe('4.1B');
  });

  it('keeps the exact value available beside the compact one', () => {
    render(<Num value={35_289} />);
    const node = screen.getByTitle('35,289');
    expect(node.textContent).toBe('35K');
  });

  it('offers no tooltip for a value that was never measured', () => {
    const { container } = render(<Num value={null} />);
    expect(container.textContent).toBe(NOT_MEASURED);
    expect(container.querySelector('[title]')).toBeNull();
  });
});

describe('durations', () => {
  it('picks the shortest honest unit', () => {
    expect(duration(840)).toBe('840ms');
    expect(duration(6800)).toBe('6.8s');
    expect(duration(45_000)).toBe('45s');
    expect(duration(134_000)).toBe('2m 14s');
  });
});

// ---------------------------------------------------------------------------
// Comparisons that refuse to lie
// ---------------------------------------------------------------------------

describe('the change against the previous period', () => {
  it('shows nothing when the comparison is meaningless', () => {
    const { container: nullish } = render(<Delta value={null} />);
    expect(nullish.textContent).toBe('');
    cleanup();
    const { container: zero } = render(<Delta value={0} />);
    expect(zero.textContent).toBe('');
  });

  it('reads a rise in volume as good and a fall as bad', () => {
    const { container } = render(<Delta value={15.2} />);
    expect(container.textContent).toContain('15.2%');
    expect(container.innerHTML).toContain('text-ok');
  });

  it('inverts for latency, where down is the improvement', () => {
    const { container } = render(<Delta value={-8.4} goodWhenDown />);
    expect(container.textContent).toContain('8.4%');
    expect(container.innerHTML).toContain('text-ok');
  });

  it('marks a latency rise as bad', () => {
    const { container } = render(<Delta value={12} goodWhenDown />);
    expect(container.innerHTML).toContain('text-danger');
  });
});

// ---------------------------------------------------------------------------
// States
// ---------------------------------------------------------------------------

describe('a chart frame', () => {
  it('holds its height while loading, so the page below cannot jump', () => {
    const { container } = render(
      <ChartFrame height={230} loading>
        <div data-testid="chart" />
      </ChartFrame>,
    );
    const frame = container.firstElementChild as HTMLElement;
    expect(frame.style.height).toBe('230px');
    expect(frame.getAttribute('aria-busy')).toBe('true');
    expect(screen.queryByTestId('chart')).toBeNull();
  });

  it('holds it while empty too, and says so in words', () => {
    const { container } = render(
      <ChartFrame height={200} empty emptyMessage="No requests in this period.">
        <div data-testid="chart" />
      </ChartFrame>,
    );
    expect((container.firstElementChild as HTMLElement).style.height).toBe('200px');
    expect(screen.getByText('No requests in this period.')).toBeTruthy();
  });

  it('offers a retry when the request failed', () => {
    const onRetry = vi.fn();
    render(
      <ChartFrame height={200} error="It broke" onRetry={onRetry}>
        <div />
      </ChartFrame>,
    );
    expect(screen.getByText('It broke')).toBeTruthy();
    screen.getByRole('button', { name: 'Retry' }).click();
    expect(onRetry).toHaveBeenCalled();
  });
});

describe('infrastructure telemetry', () => {
  it('explains its absence instead of drawing zeros', () => {
    render(
      <InfraBlock
        state={{ available: false, reason: 'connection refused', source: 'http://prom:9090' }}
        what="GPU telemetry"
      >
        {() => <div data-testid="gpu" />}
      </InfraBlock>,
    );
    expect(screen.queryByTestId('gpu')).toBeNull();
    expect(screen.getByText(/GPU telemetry is not available/)).toBeTruthy();
    expect(screen.getByText(/connection refused/)).toBeTruthy();
  });

  it('renders the block when the collector answered', () => {
    render(
      <InfraBlock state={{ available: true, nodes: ['spark-1'] }} what="Nodes">
        {(block) => <div data-testid="nodes">{block.nodes.length}</div>}
      </InfraBlock>,
    );
    expect(screen.getByTestId('nodes').textContent).toBe('1');
  });

  it('names the collector so an operator knows where to look', () => {
    render(
      <TelemetryUnavailable reason="timed out" source="http://prometheus:9090" />,
    );
    expect(screen.getByText(/http:\/\/prometheus:9090/)).toBeTruthy();
  });
});

describe('the coverage note', () => {
  it('warns when telemetry has not started yet', () => {
    render(
      <CoverageNote firstEvent={null} events={0} since="2026-08-01T00:00:00Z" />,
    );
    expect(
      screen.getByText(/telemetry begins when this release is deployed/),
    ).toBeTruthy();
  });

  it('warns when the window reaches back past the first event', () => {
    render(
      <CoverageNote
        firstEvent="2026-09-04T00:00:00Z"
        events={12}
        since="2026-08-01T00:00:00Z"
      />,
    );
    expect(screen.getByText(/telemetry starts/)).toBeTruthy();
  });

  it('says nothing when the window is fully covered', () => {
    const { container } = render(
      <CoverageNote
        firstEvent="2026-07-01T00:00:00Z"
        events={12}
        since="2026-08-01T00:00:00Z"
      />,
    );
    expect(container.textContent).toBe('');
  });
});

describe('a meter', () => {
  it('states the value in words for a screen reader', () => {
    render(<Meter label="GPU" value={42.5} />);
    expect(screen.getByRole('img', { name: 'GPU: 43 percent' })).toBeTruthy();
  });

  it('says "not measured" rather than drawing an empty bar as zero', () => {
    render(<Meter label="CPU" value={null} />);
    expect(screen.getByRole('img', { name: 'CPU: not measured' })).toBeTruthy();
    expect(screen.getByText(NOT_MEASURED)).toBeTruthy();
  });

  it('warns above the caution threshold', () => {
    const { container } = render(<Meter label="Memory" value={92} />);
    expect(container.innerHTML).toContain('bg-warn');
  });
});

describe('a stat', () => {
  it('is a definition pair, so the label is bound to its value', () => {
    const { container } = render(<Stat label="P95 first token" value="6.8s" />);
    expect(container.querySelector('dt')?.textContent).toBe('P95 first token');
    expect(container.querySelector('dd')?.textContent).toBe('6.8s');
  });
});

// ---------------------------------------------------------------------------
// The gate, as the sidebar sees it
// ---------------------------------------------------------------------------

const BASE_CAPS = [
  'workspace.read',
  'members.read',
  'members.manage',
  'invites.manage',
];

function serveMe(capabilities: string[]) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({
        user: { id: 1, name: 'Grace Hopper', email: 'grace@corp.com' },
        workspace: { id: 'w1', name: 'Corp', role: 'admin' },
        capabilities,
        features: {},
      }),
    })),
  );
}

describe('the admin sidebar', () => {
  it('hides the analytics and infrastructure sections without the capability', async () => {
    serveMe(BASE_CAPS);
    render(
      <AdminLayout>
        <div />
      </AdminLayout>,
    );
    await waitFor(() => expect(screen.getAllByText('Members').length).toBeGreaterThan(0));
    // Scoped to the desktop rail: the mobile header renders the same links.
    const rail = within(screen.getByRole('navigation'));
    expect(rail.queryByText('Analytics')).toBeNull();
    expect(rail.queryByText('Infrastructure')).toBeNull();
    expect(rail.queryByRole('link', { name: /Leaderboards/ })).toBeNull();
  });

  it('shows them for a capability that can actually read the data', async () => {
    serveMe([...BASE_CAPS, 'analytics.read', 'audit.read']);
    render(
      <AdminLayout>
        <div />
      </AdminLayout>,
    );
    await waitFor(() => expect(screen.getByText('Analytics')).toBeTruthy());
    expect(screen.getByText('Infrastructure')).toBeTruthy();
    const rail = within(screen.getByRole('navigation'));
    for (const label of [
      'Usage',
      'Leaderboards',
      'Deep research',
      'Web search',
      'Salesforce',
      'Models',
      'Performance',
      'Nodes',
      'GPU',
    ]) {
      expect(rail.getAllByRole('link', { name: label }).length).toBeGreaterThan(0);
    }
  });

  it('keeps the existing admin pages exactly where they were', async () => {
    serveMe([...BASE_CAPS, 'analytics.read']);
    render(
      <AdminLayout>
        <div />
      </AdminLayout>,
    );
    await waitFor(() => expect(screen.getAllByText('Members').length).toBeGreaterThan(0));
    const hrefs = within(screen.getByRole('navigation'))
      .getAllByRole('link')
      .map((a) => a.getAttribute('href'));
    expect(hrefs).toContain('/admin');
    expect(hrefs).toContain('/admin/members');
    expect(hrefs).toContain('/admin/invitations');
    expect(hrefs).toContain('/admin/access');
  });
});
