// @vitest-environment jsdom
/**
 * The admin area on a phone, a tablet and a 1024px laptop (responsive audit,
 * 2026-09-13).
 *
 * jsdom lays nothing out, so these pin the STRUCTURE each fix depends on:
 * a dialog whose body scrolls inside a height-capped panel, a row-actions
 * column pinned to the scroller's edge, a table floor that drops the widths
 * hidden below lg, a rail that is a drawer below lg rather than a sideways
 * strip, infrastructure blocks that report a failed load, a chart that draws
 * a lone point, and analytics pages that send a plain admin home.
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { ComponentProps, ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

let pathname = '/admin/members';

vi.mock('next/navigation', () => ({
  usePathname: () => pathname,
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
import AnalyticsLayout from '@/app/admin/analytics/layout';
import { AdminDialog, DIALOG_FOOTER } from '@/components/admin/AdminDialog';
import { AdminMeProvider } from '@/components/admin/AdminMeContext';
import {
  AdminTable,
  isPinnedColumn,
  narrowMinWidth,
  type AdminColumn,
} from '@/components/admin/AdminTable';
import {
  isolatedPoints,
  legendBottom,
  legendRows,
} from '@/components/admin/analytics/AnalyticsChart';
import { ConsoleHeader } from '@/components/admin/analytics/filters';
import { InfraBlock } from '@/components/admin/analytics/ui';
import type { Me } from '@/components/admin/api';
import { AdminSelect, AdminToolbar } from '@/components/admin/controls';
import { nav } from '@/components/admin/nav';
import { RowMenu } from '@/components/admin/RowMenu';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const classes = (el: Element | null) => (el?.getAttribute('class') ?? '').split(/\s+/);

describe('a dialog taller than the window', () => {
  it('caps the panel to the overlay and scrolls only the body, so the close button stays on screen', () => {
    render(
      <AdminDialog open title="Access · Ada" onClose={vi.fn()} size="md">
        <div style={{ height: 2000 }}>switches</div>
        <div className={DIALOG_FOOTER}>
          <button type="button">Save access</button>
        </div>
      </AdminDialog>,
    );
    const panel = screen.getByRole('dialog', { name: 'Access · Ada' });
    // The Manage access dialog measured 1225px in a 900px window with its
    // title above the top edge and Save access below the bottom: no cap, and
    // the overlay could not scroll.
    expect(classes(panel)).toEqual(expect.arrayContaining(['max-h-full', 'flex', 'flex-col']));
    const body = screen.getByTestId('admin-dialog-body');
    expect(classes(body)).toEqual(expect.arrayContaining(['min-h-0', 'overflow-y-auto']));
    const close = screen.getByRole('button', { name: 'Close dialog' });
    expect(body.contains(close)).toBe(false);
    expect(classes(close)).toEqual(expect.arrayContaining(['h-8', 'w-8']));
    // The footer rides the bottom of the scrolling body.
    const save = screen.getByRole('button', { name: 'Save access' });
    expect(classes(save.parentElement)).toEqual(expect.arrayContaining(['sticky', 'bottom-0']));
  });
});

interface Row {
  id: number;
  name: string;
}

const COLUMNS: AdminColumn<Row>[] = [
  { key: 'name', label: 'Name', render: (r) => r.name },
  { key: 'role', label: 'Role', width: '160px', render: () => 'Member' },
  { key: 'active', label: 'Last active', width: '160px', hideBelowLg: true, render: () => 'now' },
  {
    key: 'actions',
    label: '',
    width: '56px',
    align: 'right',
    render: (r) => <RowMenu label={`Actions for ${r.name}`} items={[{ id: 'x', label: 'Remove' }]} onSelect={vi.fn()} />,
  },
];

describe('an admin table narrower than its floor', () => {
  it('pins the row-actions column to the right edge, header and cells', () => {
    const { container } = render(
      <AdminTable columns={COLUMNS} rows={[{ id: 1, name: 'Ada' }]} rowKey={(r) => r.id} empty="none" minWidth={900} />,
    );
    const trigger = screen.getByRole('button', { name: 'Actions for Ada' });
    expect(classes(trigger.closest('td'))).toEqual(expect.arrayContaining(['sticky', 'right-0', 'bg-bg']));
    const ths = container.querySelectorAll('th');
    expect(classes(ths[3])).toEqual(expect.arrayContaining(['sticky', 'right-0']));
    // Only the actions column: pinning the identity column would cover it.
    expect(classes(ths[0])).not.toContain('sticky');
    expect(isPinnedColumn({ key: 'actions' })).toBe(true);
    expect(isPinnedColumn({ key: 'name' })).toBe(false);
  });

  it('draws the pinned cell row rule with the same cell border as its neighbours, so the line has no step', () => {
    const { container } = render(
      <AdminTable columns={COLUMNS} rows={[{ id: 1, name: 'Ada' }]} rowKey={(r) => r.id} empty="none" minWidth={900} />,
    );
    // Separate borders: every cell owns its bottom border, and a sticky cell
    // carries it along. A collapsed grid plus an inset shadow on the pinned
    // cell drew its rule 1px above everyone else's in Chrome.
    expect(classes(container.querySelector('table'))).toEqual(
      expect.arrayContaining(['border-separate', 'border-spacing-0']),
    );
    expect(classes(container.querySelector('table'))).not.toContain('border-collapse');
    const [first, , , pinned] = Array.from(container.querySelectorAll('tbody td'));
    expect(classes(pinned)).toEqual(expect.arrayContaining(['border-b', 'border-[var(--admin-separator)]']));
    expect((pinned.getAttribute('class') ?? '').includes('shadow-')).toBe(false);
    expect(classes(first)).toEqual(expect.arrayContaining(['border-b', 'border-[var(--admin-separator)]']));
  });

  it('gives the row menu a 32px hit area', () => {
    render(<RowMenu label="Actions for Ada" items={[{ id: 'x', label: 'Remove' }]} onSelect={vi.fn()} />);
    expect(classes(screen.getByRole('button', { name: 'Actions for Ada' }))).toEqual(
      expect.arrayContaining(['h-8', 'w-8']),
    );
  });

  it('drops the widths hidden below lg from the phone floor, and keeps the desktop floor at lg', () => {
    expect(narrowMinWidth(900, COLUMNS)).toBe(740);
    // A percent width cannot be subtracted; nothing is guessed.
    expect(narrowMinWidth(900, [{ width: '20%', hideBelowLg: true }])).toBe(900);
    const { container } = render(
      <AdminTable columns={COLUMNS} rows={[{ id: 1, name: 'Ada' }]} rowKey={(r) => r.id} empty="none" minWidth={900} />,
    );
    const table = container.querySelector('table') as HTMLTableElement;
    expect(table.style.getPropertyValue('--admin-table-min')).toBe('900px');
    expect(table.style.getPropertyValue('--admin-table-min-narrow')).toBe('740px');
    expect(classes(table)).toEqual(
      expect.arrayContaining(['min-w-[var(--admin-table-min-narrow)]', 'lg:min-w-[var(--admin-table-min)]']),
    );
  });

  it('keeps a hidden-below-lg <col> a column at lg, so the widths after it do not shift', () => {
    const { container } = render(
      <AdminTable columns={COLUMNS} rows={[{ id: 1, name: 'Ada' }]} rowKey={(r) => r.id} empty="none" />,
    );
    const scroller = screen.getByTestId('admin-table-scroll');
    expect(classes(scroller)).toEqual(expect.arrayContaining(['overflow-x-auto', 'lg:[&_col]:!table-column']));
    expect(scroller.contains(container.querySelector('table'))).toBe(true);
  });
});

describe('the admin toolbar on a phone', () => {
  it('gives the filter half a real basis, so the action wraps before the search is squeezed', () => {
    render(
      <AdminToolbar action={<button type="button">Invite member</button>}>
        <input aria-label="Search" />
      </AdminToolbar>,
    );
    const filters = screen.getByRole('textbox', { name: 'Search' }).parentElement;
    expect(classes(filters)).toContain('flex-[1_1_18rem]');
  });
});

const ME: Me = {
  user: { id: 1, name: 'Grace Hopper', email: 'grace@corp.com' },
  workspace: { id: 'w1', name: 'Corp', role: 'super_admin' },
  capabilities: ['workspace.read', 'members.read', 'members.manage', 'invites.manage', 'analytics.read'],
  features: {},
};

function serveMe(me: Me) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({ ok: true, status: 200, json: async () => me })),
  );
}

describe('the admin rail below lg', () => {
  beforeEach(() => {
    pathname = '/admin/members';
  });

  it('is one navigation, opened as a drawer from the header, not a sideways strip of copies', async () => {
    serveMe(ME);
    render(
      <AdminLayout>
        <p>page</p>
      </AdminLayout>,
    );
    await screen.findByText('page');
    // One <nav>: the header no longer repeats every link in a scroller.
    expect(screen.getAllByRole('navigation')).toHaveLength(1);
    expect(screen.getAllByRole('link', { name: 'Members' })).toHaveLength(1);

    const header = screen.getByRole('banner');
    expect(classes(header)).toContain('lg:hidden');
    expect(classes(header)).not.toContain('overflow-x-auto');
    // The header names the section that is open.
    expect(within(header).getByText('Members')).toBeTruthy();

    const rail = document.getElementById('admin-rail') as HTMLElement;
    expect(classes(rail)).toEqual(expect.arrayContaining(['hidden', 'lg:flex']));

    const toggle = within(header).getByRole('button', { name: 'Open admin menu' });
    expect(toggle.getAttribute('aria-controls')).toBe('admin-rail');
    expect(classes(toggle)).toEqual(expect.arrayContaining(['h-10', 'w-10']));
    toggle.focus();
    fireEvent.click(toggle);

    const close = within(header).getByRole('button', { name: 'Close admin menu' });
    expect(close.getAttribute('aria-expanded')).toBe('true');
    expect(classes(rail)).toEqual(expect.arrayContaining(['fixed', 'top-[52px]', 'flex']));
    // Focus moves into the drawer.
    expect(rail.contains(document.activeElement)).toBe(true);

    fireEvent.keyDown(document, { key: 'Escape' });
    const reopened = within(header).getByRole('button', { name: 'Open admin menu' });
    expect(reopened.getAttribute('aria-expanded')).toBe('false');
    expect(document.activeElement).toBe(reopened);
    expect(classes(rail)).toContain('hidden');
  });

  it('links the Models entry to the Models page, which git can track again', async () => {
    serveMe(ME);
    render(
      <AdminLayout>
        <p>page</p>
      </AdminLayout>,
    );
    await screen.findByText('page');
    const models = screen.getByRole('link', { name: 'Models' });
    expect(models.getAttribute('href')).toBe('/admin/analytics/models');
    // The route the link names is a real page module (tests/source-not-gitignored
    // pins that git no longer ignores its folder).
    const page = await import('@/app/admin/analytics/models/page');
    expect(typeof page.default).toBe('function');
  });
});

describe('the invitations status filter', () => {
  it('insets its buttons by 2px on touch screens, so each is 34px tall inside the 40px strip', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ invitations: [], total: 0 }) })),
    );
    const { default: AdminInvitationsPage } = await import('@/app/admin/invitations/page');
    render(
      <AdminMeProvider me={ME}>
        <AdminInvitationsPage />
      </AdminMeProvider>,
    );
    const group = screen.getByRole('group', { name: 'Filter invitations' });
    expect(classes(group)).toEqual(
      expect.arrayContaining(['h-10', 'p-1', 'max-sm:p-0.5', '[@media(pointer:coarse)]:p-0.5']),
    );
    expect(classes(within(group).getByRole('button', { name: 'Pending' }))).toContain('h-full');
    await screen.findByText(/No invitations yet/);
  });
});

describe('the Models analytics page', () => {
  it('reports a failed load with Retry where the engine cards go, instead of pulsing forever', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: false,
        status: 500,
        json: async () => ({ detail: 'Analytics query failed.' }),
      })),
    );
    const { default: ModelAnalyticsPage } = await import('@/app/admin/analytics/models/page');
    render(<ModelAnalyticsPage />);
    await waitFor(() => expect(screen.getAllByText('Analytics query failed.').length).toBe(2));
    expect(screen.queryByLabelText('Loading live engine state')).toBeNull();
    expect(screen.getAllByRole('button', { name: 'Retry' }).length).toBe(2);
  });

  it('draws one card per live engine once the collector answers', async () => {
    const payload = {
      range: { since: '2026-09-01T00:00:00Z', until: '2026-09-13T00:00:00Z', label: '30d' },
      coverage: { first_event: '2026-08-01T00:00:00Z', events: 10 },
      models: [],
      effort: [],
      engines: {
        available: true,
        engines: [
          { service: 'vllm', model: 'nvidia/Qwen3.6-35B-A3B-NVFP4', node: 'n1', instance: 'i1', running: 1, waiting: 0 },
          { service: 'router', model: 'Qwen/Qwen3-VL-8B', node: 'n1', instance: 'i2', running: 0, waiting: 0 },
        ],
      },
    };
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: true, status: 200, json: async () => payload })),
    );
    const { default: ModelAnalyticsPage } = await import('@/app/admin/analytics/models/page');
    render(<ModelAnalyticsPage />);
    expect(await screen.findByText('Qwen3.6-35B-A3B-NVFP4')).toBeTruthy();
    expect(screen.getByText('Qwen3-VL-8B')).toBeTruthy();
  });
});

describe('an infrastructure block whose request failed', () => {
  it('says so and offers Retry instead of pulsing as loading forever', () => {
    const onRetry = vi.fn();
    render(
      <InfraBlock state={undefined} what="Node telemetry" error="Not found." onRetry={onRetry}>
        {() => <div data-testid="nodes" />}
      </InfraBlock>,
    );
    expect(screen.queryByLabelText('Loading node telemetry')).toBeNull();
    expect(screen.getByText('Not found.')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it('still shows the skeleton while the first load is in flight', () => {
    render(
      <InfraBlock state={undefined} what="Node telemetry" error={null}>
        {() => <div data-testid="nodes" />}
      </InfraBlock>,
    );
    expect(screen.getByLabelText('Loading node telemetry')).toBeTruthy();
  });
});

describe('a line with a point that has no neighbour', () => {
  it('marks a single measured day, and a day between two gaps', () => {
    expect(isolatedPoints([600])).toEqual([true]);
    expect(isolatedPoints([null, 4, null, 1, 2])).toEqual([false, true, false, false, false]);
    expect(isolatedPoints([1, 2, 3])).toEqual([false, false, false]);
    expect(isolatedPoints([])).toEqual([]);
  });
});

describe('a chart legend on a phone', () => {
  // 6px per character stands in for the 11px legend font.
  const sixPx = (text: string) => text.length * 6;
  const DAU = ['Active people', 'Chat', 'Deep research', 'Web search'];

  it('counts the second row a four-series legend wraps into on a phone, and one row where it fits', () => {
    // Items are 13px of icon and gap plus their text, 18px apart, in a box
    // 10px narrower than the chart: 91 + 37 + 91 + 73 + 3 gaps = 346px.
    expect(legendRows(DAU, 328, sixPx)).toBe(2);
    expect(legendRows(DAU, 356, sixPx)).toBe(1);
    expect(legendRows(DAU, 0, sixPx)).toBe(1);
    expect(legendRows(['Only one'], 100, sixPx)).toBe(1);
  });

  it('reserves one more row pitch under the plot for every extra legend row, and none without a legend', () => {
    expect(legendBottom(0)).toBe(8);
    expect(legendBottom(1)).toBe(34);
    expect(legendBottom(2)).toBe(66);
  });
});

describe('the analytics filters on a phone', () => {
  it('keeps the filter group inside the header and lets each select truncate its label', () => {
    render(
      <ConsoleHeader title="Usage">
        <AdminSelect
          value=""
          onChange={() => undefined}
          label="Model"
          options={[{ value: '', label: 'Qwen3-VL-8B-Instruct-with-a-very-long-checkpoint-name-for-wrapping' }]}
        />
      </ConsoleHeader>,
    );
    const group = screen.getByRole('group', { name: 'Filters' });
    expect(classes(group)).toEqual(
      expect.arrayContaining(['min-w-0', 'max-w-full', '[&>div]:max-w-full', '[&_select]:max-w-full', '[&_select]:truncate']),
    );
  });
});

describe('the super-admin landing range toggle', () => {
  it('insets its buttons by 2px on phones and touch screens, like the invitations filter', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: false, status: 503, json: async () => ({ detail: 'down' }) })),
    );
    const { default: AdminLandingPage } = await import('@/app/admin/page');
    render(
      <AdminMeProvider me={ME}>
        <AdminLandingPage />
      </AdminMeProvider>,
    );
    const group = screen.getByRole('group', { name: 'Time range' });
    expect(classes(group)).toEqual(
      expect.arrayContaining(['h-10', 'p-1', 'max-sm:p-0.5', '[@media(pointer:coarse)]:p-0.5']),
    );
    expect(classes(within(group).getAllByRole('button')[0])).toContain('h-full');
  });
});

describe('an analytics URL typed in by an admin without analytics.read', () => {
  it('goes back to /admin and renders nothing of the console', async () => {
    const assign = vi.spyOn(nav, 'assign').mockImplementation(() => undefined);
    render(
      <AdminMeProvider me={{ ...ME, capabilities: ['workspace.read', 'members.read'] }}>
        <AnalyticsLayout>
          <p>charts</p>
        </AnalyticsLayout>
      </AdminMeProvider>,
    );
    await waitFor(() => expect(assign).toHaveBeenCalledWith('/admin'));
    expect(screen.queryByText('charts')).toBeNull();
  });

  it('renders the page for a capability that can read it', () => {
    const assign = vi.spyOn(nav, 'assign').mockImplementation(() => undefined);
    render(
      <AdminMeProvider me={ME}>
        <AnalyticsLayout>
          <p>charts</p>
        </AnalyticsLayout>
      </AdminMeProvider>,
    );
    expect(screen.getByText('charts')).toBeTruthy();
    expect(assign).not.toHaveBeenCalled();
  });
});
