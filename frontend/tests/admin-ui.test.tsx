// @vitest-environment jsdom
/**
 * The admin area's visual contracts (2026-09-04 redesign).
 *
 * These pin the things a redesign is most likely to quietly undo: the table's
 * deterministic column tracks, the switch's accessibility, the access row's
 * single grid, and the dependency rule that must read as an explanation
 * rather than an error.
 */
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AdminTable, type AdminColumn } from '@/components/admin/AdminTable';
import { AdminSearchInput, AdminSelect, AdminTabs } from '@/components/admin/controls';
import { FeatureToggles, AccessSettingRow } from '@/components/admin/FeatureToggles';
import { MemberRoleControl } from '@/components/admin/MemberRoleControl';
import { Switch } from '@/components/admin/Switch';
import { StatusChip } from '@/components/admin/chips';
import type { FeatureSpec } from '@/components/admin/api';

afterEach(cleanup);

interface Row {
  id: number;
  name: string;
}

const ROWS: Row[] = [
  { id: 1, name: 'Ada Lovelace' },
  { id: 2, name: 'A person with a very long name indeed' },
];

const COLUMNS: AdminColumn<Row>[] = [
  { key: 'name', label: 'Name', render: (r) => r.name },
  { key: 'role', label: 'Role', width: '160px', render: () => 'Member' },
  { key: 'actions', label: '', width: '56px', align: 'right', render: () => '⋯' },
];

describe('the admin table', () => {
  it('gives every row the same tracks when columns declare widths', () => {
    const { container } = render(
      <AdminTable columns={COLUMNS} rows={ROWS} rowKey={(r) => r.id} empty="none" />,
    );
    const table = container.querySelector('table')!;
    // Fixed layout is what stops a long email in row 2 from moving the Role
    // column three pixels right on that one row.
    expect(table.className).toContain('table-fixed');
    const cols = container.querySelectorAll('colgroup col');
    expect(cols).toHaveLength(3);
    expect((cols[1] as HTMLElement).style.width).toBe('160px');
    expect((cols[2] as HTMLElement).style.width).toBe('56px');
    // The identity column declares no width: it absorbs the slack.
    expect((cols[0] as HTMLElement).style.width).toBe('');
  });

  it('leaves layout to the browser when no column declares a width', () => {
    const { container } = render(
      <AdminTable
        columns={COLUMNS.map(({ width, ...rest }) => rest)}
        rows={ROWS}
        rowKey={(r) => r.id}
        empty="none"
      />,
    );
    expect(container.querySelector('table')!.className).toContain('table-auto');
  });

  it('is flat — no card, no zebra striping', () => {
    const { container } = render(
      <AdminTable columns={COLUMNS} rows={ROWS} rowKey={(r) => r.id} empty="none" />,
    );
    const html = container.innerHTML;
    expect(html).not.toContain('odd:bg-surface');
    expect(html).not.toContain('even:bg-surface');
    // Rows are separated by a hairline, not boxed.
    expect(html).toContain('--admin-separator');
  });

  it('still reports empty and error states', () => {
    const { rerender } = render(
      <AdminTable columns={COLUMNS} rows={[]} rowKey={(r) => r.id} empty="Nobody here" />,
    );
    expect(screen.getByText('Nobody here')).toBeTruthy();
    const onRetry = vi.fn();
    rerender(
      <AdminTable
        columns={COLUMNS}
        rows={[]}
        rowKey={(r) => r.id}
        empty="Nobody here"
        error="It broke"
        onRetry={onRetry}
      />,
    );
    expect(screen.getByText('It broke')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(onRetry).toHaveBeenCalled();
  });
});

describe('toolbar controls', () => {
  it('share one height so the row reads as a single strip', () => {
    const { container } = render(
      <>
        <AdminSearchInput value="" onChange={vi.fn()} label="Search" placeholder="Search…" />
        <AdminSelect
          value=""
          onChange={vi.fn()}
          label="Filter by role"
          options={[{ value: '', label: 'All roles' }]}
        />
      </>,
    );
    const heights = [...container.querySelectorAll('div, select')]
      .map((el) => (el as HTMLElement).className)
      .filter((c) => c.includes('h-10'));
    expect(heights.length).toBeGreaterThanOrEqual(2);
  });

  it('keeps the filter a real <select>, so keyboard and mobile pickers work', () => {
    const onChange = vi.fn();
    render(
      <AdminSelect
        value=""
        onChange={onChange}
        label="Filter by role"
        options={[
          { value: '', label: 'All roles' },
          { value: 'admin', label: 'Admin' },
        ]}
      />,
    );
    const select = screen.getByLabelText('Filter by role') as HTMLSelectElement;
    expect(select.tagName).toBe('SELECT');
    fireEvent.change(select, { target: { value: 'admin' } });
    expect(onChange).toHaveBeenCalledWith('admin');
  });

  it('marks the selected tab and hides a zero count', () => {
    const onChange = vi.fn();
    const { rerender } = render(
      <AdminTabs
        label="Members tabs"
        active="users"
        onChange={onChange}
        tabs={[
          { id: 'users', label: 'Users' },
          { id: 'invites', label: 'Pending invites', count: 0 },
        ]}
      />,
    );
    expect(screen.getByRole('tab', { name: 'Users' }).getAttribute('aria-selected')).toBe('true');
    expect(screen.getByRole('tab', { name: 'Pending invites' }).textContent).toBe(
      'Pending invites',
    );
    fireEvent.click(screen.getByRole('tab', { name: 'Pending invites' }));
    expect(onChange).toHaveBeenCalledWith('invites');

    rerender(
      <AdminTabs
        label="Members tabs"
        active="users"
        onChange={onChange}
        tabs={[
          { id: 'users', label: 'Users' },
          { id: 'invites', label: 'Pending invites', count: 3 },
        ]}
      />,
    );
    expect(
      within(screen.getByRole('tab', { name: /Pending invites/ })).getByText('3'),
    ).toBeTruthy();
  });
});

describe('the switch', () => {
  it('is a real switch with state, not a styled div', () => {
    const onChange = vi.fn();
    render(<Switch checked={false} label="Web search" onChange={onChange} />);
    const control = screen.getByRole('switch', { name: 'Web search' });
    expect(control.getAttribute('aria-checked')).toBe('false');
    fireEvent.click(control);
    expect(onChange).toHaveBeenCalledWith(true);
  });

  it('cannot be moved when disabled', () => {
    const onChange = vi.fn();
    render(<Switch checked disabled label="Live Salesforce" onChange={onChange} />);
    const control = screen.getByRole('switch', { name: 'Live Salesforce' });
    expect((control as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(control);
    expect(onChange).not.toHaveBeenCalled();
  });
});

describe('access rows', () => {
  const catalog: FeatureSpec[] = [
    { id: 'web_search', label: 'Web search', hint: 'Answer from the public web.', default: true, requires: null },
    {
      id: 'deep_research',
      label: 'Deep research',
      hint: 'Run the research loop.',
      default: true,
      requires: 'web_search',
    },
  ];

  it('puts every switch in the same grid track', () => {
    const { container } = render(
      <FeatureToggles
        catalog={catalog}
        values={{ web_search: true, deep_research: true }}
        onChange={vi.fn()}
      />,
    );
    const rows = [...container.querySelectorAll('li')];
    expect(rows).toHaveLength(2);
    // One structural class on every row — that identical track is what makes
    // the switches share an X, whatever each row's text does.
    const tracks = new Set(
      rows.map((r) => r.className.match(/grid-cols-\[[^\]]+\]/)?.[0]),
    );
    expect(tracks.size).toBe(1);
    expect([...tracks][0]).toBe('grid-cols-[minmax(0,1fr)_auto]');
    // And the toggle is centred against the row, not pinned to its top.
    expect(rows.every((r) => r.className.includes('items-center'))).toBe(true);
  });

  it('explains a dependency instead of failing silently', () => {
    render(
      <FeatureToggles
        catalog={catalog}
        values={{ web_search: false, deep_research: true }}
        onChange={vi.fn()}
      />,
    );
    expect(screen.getByText('Needs Web search, which is off.')).toBeTruthy();
    const dependent = screen.getByRole('switch', { name: 'Deep research' });
    expect((dependent as HTMLButtonElement).disabled).toBe(true);
    // The stored value is true, but a dangling dependency never shows as on.
    expect(dependent.getAttribute('aria-checked')).toBe('false');
  });

  it('describes each switch by its own description', () => {
    render(
      <AccessSettingRow
        title="Web search"
        description="Answer from the public web."
        enabled
        onChange={vi.fn()}
      />,
    );
    const control = screen.getByRole('switch', { name: 'Web search' });
    const describedBy = control.getAttribute('aria-describedby');
    expect(describedBy).toBeTruthy();
    expect(document.getElementById(describedBy!)?.textContent).toBe(
      'Answer from the public web.',
    );
  });
});

describe('role and status marks', () => {
  it('offers the role as a control only to someone who may change it', () => {
    const onEdit = vi.fn();
    const { rerender } = render(
      <MemberRoleControl role="member" name="Ada" editable={false} onEdit={onEdit} />,
    );
    expect(screen.queryByRole('button')).toBeNull();

    rerender(<MemberRoleControl role="member" name="Ada" editable onEdit={onEdit} />);
    const button = screen.getByRole('button', { name: 'Change role for Ada' });
    fireEvent.click(button);
    expect(onEdit).toHaveBeenCalled();
  });

  it('states every status in words, never colour alone', () => {
    render(
      <>
        <StatusChip status="active" />
        <StatusChip status="disabled" />
        <StatusChip status="pending" />
      </>,
    );
    expect(screen.getByText('Active')).toBeTruthy();
    expect(screen.getByText('Disabled')).toBeTruthy();
    expect(screen.getByText('Pending')).toBeTruthy();
  });
});
