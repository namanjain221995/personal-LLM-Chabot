'use client';

/**
 * Leaderboards — who and what carries the platform.
 *
 * Two boards, because this deployment has two real dimensions to rank:
 * PEOPLE and MODELS. There is no groups board: this workspace has no group
 * model, and a board of invented cohorts would be decoration pretending to be
 * data. If groups arrive, this is where they slot in.
 *
 * The people board pages in the DATABASE — search, ordering and offset all
 * travel to SQL — so the table never receives rows it will not draw, whatever
 * the workspace grows to.
 */

import { useEffect, useState } from 'react';
import { AdminTable, type AdminColumn } from '@/components/admin/AdminTable';
import { AdminSearchInput, AdminSelect, AdminTabs } from '@/components/admin/controls';
import { RoleChip } from '@/components/admin/chips';
import {
  ConsoleHeader,
  RangePicker,
  useQueryState,
  useRange,
} from '@/components/admin/analytics/filters';
import { Num, Section, Stat, StatRow } from '@/components/admin/analytics/ui';
import {
  NOT_MEASURED,
  compact,
  duration,
  exact,
  initialOf,
  percent,
} from '@/components/admin/analytics/format';
import {
  useAnalytics,
  useDebouncedValue,
} from '@/components/admin/analytics/useAnalytics';
import { formatRelative } from '@/lib/format';
import type {
  Leaderboard,
  MemberUsage,
  ModelsAnalytics,
  ModelUsage,
} from '@/components/admin/analytics/types';

const ORDERS = [
  { value: 'output_tokens', label: 'Tokens generated' },
  { value: 'total_tokens', label: 'Total tokens' },
  { value: 'requests', label: 'Requests' },
  { value: 'messages', label: 'Messages' },
  { value: 'research', label: 'Research runs' },
  { value: 'web_searches', label: 'Web searches' },
];

const PAGE_SIZE = 25;

/** The identity cell: initial, name, email — the console's one avatar shape. */
function Person({ row, rank }: { row: MemberUsage; rank: number }) {
  return (
    <div className="flex items-center gap-3">
      <span className="w-6 shrink-0 text-right text-xs text-faint [font-variant-numeric:tabular-nums]">
        {rank}
      </span>
      <span
        aria-hidden
        className="grid h-9 w-9 shrink-0 place-items-center rounded-full bg-[var(--admin-control)] text-xs font-semibold text-muted"
      >
        {initialOf(row.name, row.email)}
      </span>
      <span className="min-w-0">
        <span className="block truncate text-[13px] font-medium text-ink">
          {row.name}
        </span>
        <span className="block truncate text-xs text-faint">{row.email}</span>
      </span>
    </div>
  );
}

function PeopleBoard() {
  const [range] = useRange();
  const [order, setOrder] = useQueryState('order', 'output_tokens');
  const [search, setSearch] = useQueryState('q', '');
  const [page, setPage] = useState(0);
  const debounced = useDebouncedValue(search, 250);

  // A new search or ordering starts at the first page — leaving someone on
  // page 4 of a two-page result is how a table appears to lose its rows.
  useEffect(() => {
    setPage(0);
  }, [debounced, order, range]);

  const { data, loading, error, reload } = useAnalytics<Leaderboard>(
    'analytics/leaderboard',
    {
      range,
      order,
      search: debounced,
      limit: PAGE_SIZE,
      offset: page * PAGE_SIZE,
    },
  );

  const rows = data?.rows ?? [];
  const total = data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const columns: AdminColumn<MemberUsage>[] = [
    {
      key: 'person',
      label: 'Person',
      // An explicit share, not the leftover: with eight fixed columns beside
      // it, `table-fixed` spreads the slack across ALL of them and the names
      // truncate to "Soham Pipr…" while the numbers float in whitespace.
      width: '30%',
      render: (row) => (
        <Person row={row} rank={page * PAGE_SIZE + rows.indexOf(row) + 1} />
      ),
    },
    {
      key: 'role',
      label: 'Role',
      width: '110px',
      hideBelowLg: true,
      render: (row) => <RoleChip role={row.role} />,
    },
    {
      key: 'requests',
      label: 'Requests',
      width: '104px',
      align: 'right',
      render: (row) => <Num value={row.requests} />,
    },
    {
      key: 'tokens',
      label: 'Tokens',
      width: '112px',
      align: 'right',
      render: (row) => (
        <span title={exact(row.total_tokens)}>
          {compact(row.total_tokens)}
        </span>
      ),
    },
    {
      key: 'messages',
      label: 'Messages',
      width: '104px',
      align: 'right',
      render: (row) => <Num value={row.messages} />,
    },
    {
      key: 'research',
      label: 'Research',
      width: '96px',
      align: 'right',
      hideBelowLg: true,
      render: (row) => <Num value={row.research_runs} />,
    },
    {
      key: 'searches',
      label: 'Searches',
      width: '96px',
      align: 'right',
      hideBelowLg: true,
      render: (row) => <Num value={row.web_searches} />,
    },
    {
      key: 'ttft',
      label: 'Avg first token',
      width: '128px',
      align: 'right',
      hideBelowLg: true,
      render: (row) => (
        <span className="text-muted">{duration(row.avg_ttft_ms)}</span>
      ),
    },
    {
      key: 'active',
      label: 'Last active',
      width: '128px',
      align: 'right',
      render: (row) => (
        <span className="text-muted">
          {row.last_active_at ? formatRelative(row.last_active_at) : NOT_MEASURED}
        </span>
      ),
    },
  ];

  return (
    <>
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <div className="min-w-[220px] flex-1">
          <AdminSearchInput
            value={search}
            onChange={setSearch}
            label="Search people"
            placeholder="Search by name or email…"
          />
        </div>
        <AdminSelect
          value={order}
          onChange={setOrder}
          label="Rank by"
          options={ORDERS}
        />
      </div>

      <AdminTable
        columns={columns}
        rows={rows}
        rowKey={(row) => row.id}
        loading={loading && !data}
        error={error}
        onRetry={reload}
        minWidth={1080}
        empty={
          debounced
            ? 'Nobody matches that search.'
            : 'No members in this workspace.'
        }
      />

      {total > PAGE_SIZE && (
        <div className="mt-4 flex items-center justify-between gap-4">
          <p className="text-xs text-faint [font-variant-numeric:tabular-nums]">
            {page * PAGE_SIZE + 1}–{Math.min(total, (page + 1) * PAGE_SIZE)} of{' '}
            {total}
          </p>
          <div className="flex items-center gap-2">
            <button
              type="button"
              disabled={page === 0}
              onClick={() => setPage((p) => Math.max(0, p - 1))}
              className="h-8 rounded-lg border border-[var(--admin-separator)] px-3 text-xs text-ink transition-colors hover:bg-[var(--admin-row-hover)] disabled:cursor-not-allowed disabled:opacity-40"
            >
              Previous
            </button>
            <button
              type="button"
              disabled={page + 1 >= pages}
              onClick={() => setPage((p) => p + 1)}
              className="h-8 rounded-lg border border-[var(--admin-separator)] px-3 text-xs text-ink transition-colors hover:bg-[var(--admin-row-hover)] disabled:cursor-not-allowed disabled:opacity-40"
            >
              Next
            </button>
          </div>
        </div>
      )}
    </>
  );
}

function ModelsBoard() {
  const [range] = useRange();
  const { data, loading, error, reload } = useAnalytics<ModelsAnalytics>(
    'analytics/models',
    { range },
  );
  const rows = data?.models ?? [];

  const columns: AdminColumn<ModelUsage>[] = [
    {
      key: 'model',
      label: 'Model',
      width: '28%',
      render: (row) => (
        <span className="min-w-0">
          <span className="block truncate text-[13px] font-medium text-ink">
            {row.model.split('/').pop() ?? row.model}
          </span>
          <span className="block truncate text-xs text-faint">{row.model}</span>
        </span>
      ),
    },
    {
      key: 'share',
      label: 'Share',
      width: '88px',
      align: 'right',
      render: (row) => percent(row.share, 0),
    },
    {
      key: 'requests',
      label: 'Requests',
      width: '104px',
      align: 'right',
      render: (row) => <Num value={row.requests} />,
    },
    {
      key: 'input',
      label: 'Input tokens',
      width: '120px',
      align: 'right',
      hideBelowLg: true,
      render: (row) => (
        <span title={exact(row.input_tokens ?? null)}>
          {compact(row.input_tokens ?? null)}
        </span>
      ),
    },
    {
      key: 'output',
      label: 'Output tokens',
      width: '128px',
      align: 'right',
      render: (row) => (
        <span title={exact(row.output_tokens)}>{compact(row.output_tokens)}</span>
      ),
    },
    {
      key: 'ttft',
      label: 'Avg first token',
      width: '128px',
      align: 'right',
      render: (row) => duration(row.avg_ttft_ms),
    },
    {
      key: 'p95',
      label: 'P95 first token',
      width: '128px',
      align: 'right',
      hideBelowLg: true,
      render: (row) => (
        <span className="text-muted">{duration(row.p95_ttft_ms ?? null)}</span>
      ),
    },
    {
      key: 'throughput',
      label: 'Throughput',
      width: '112px',
      align: 'right',
      render: (row) =>
        row.avg_tokens_per_second == null
          ? NOT_MEASURED
          : `${row.avg_tokens_per_second.toFixed(1)} t/s`,
    },
    {
      key: 'errors',
      label: 'Failed',
      width: '88px',
      align: 'right',
      render: (row) =>
        (row.errors ?? 0) > 0 ? (
          <span className="text-danger">{row.errors}</span>
        ) : (
          <span className="text-faint">0</span>
        ),
    },
  ];

  return (
    <>
      <AdminTable
        columns={columns}
        rows={rows}
        rowKey={(row) => row.model}
        loading={loading && !data}
        error={error}
        onRetry={reload}
        minWidth={1120}
        empty="No model telemetry for this period yet."
      />
      {data && data.effort.length > 0 && (
        <Section title="By effort tier" hint="What Fast, Think and Max actually cost on this hardware.">
          <StatRow columns={4}>
            {data.effort.slice(0, 8).map((e) => (
              <Stat
                key={e.effort}
                label={e.effort.replace(/_/g, ' ')}
                value={compact(e.requests)}
                sub={`${duration(e.avg_ttft_ms)} first token · ${duration(e.avg_duration_ms)} total`}
              />
            ))}
          </StatRow>
        </Section>
      )}
    </>
  );
}

export default function LeaderboardsPage() {
  const [tab, setTab] = useQueryState('tab', 'people');
  const active = tab === 'models' ? 'models' : 'people';
  return (
    <>
      <ConsoleHeader
        title="Leaderboards"
        description="Who uses the platform, and which model carries the work."
      >
        <RangePicker />
      </ConsoleHeader>
      <div className="mb-5">
        <AdminTabs
          label="Leaderboard type"
          active={active}
          onChange={setTab}
          tabs={[
            { id: 'people', label: 'People' },
            { id: 'models', label: 'Models' },
          ]}
        />
      </div>
      {active === 'people' ? <PeopleBoard /> : <ModelsBoard />}
    </>
  );
}
