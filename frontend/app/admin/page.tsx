'use client';

/**
 * /admin — workspace analytics: who is using this, how much, and with which
 * tools, over a selectable window.
 *
 * Two tabs because there are two questions. "Overview" answers *is the
 * workspace using it* (totals, the daily shape, which tools). "People"
 * answers *who* — and lists every member, including the ones who sent
 * nothing, because "who has not started" is usually why this page is open.
 *
 * Seat counts do not move with the range: a seat is a seat whatever window
 * is selected, and only the usage numbers below them are windowed. The pills
 * say which window, and the export hands back exactly the People table.
 */

import { useEffect, useMemo, useState } from 'react';
import { useAdminMe } from '@/components/admin/AdminMeContext';
import {
  AdminApiError,
  adminJson,
  RANGE_LABEL,
  ROLE_LABEL,
  type Analytics,
  type AnalyticsMember,
  type RangeKey,
} from '@/components/admin/api';
import { AdminTable, type AdminColumn } from '@/components/admin/AdminTable';
import { RoleChip, StatusChip } from '@/components/admin/chips';
import { UsageChart } from '@/components/admin/UsageChart';
import {
  AvatarInitial,
  ErrorPanel,
  PageHeader,
  SkeletonLine,
  StatTile,
} from '@/components/admin/ui';
import { IconDownload } from '@/components/admin/icons';
import { formatWhen } from '@/lib/format';

const RANGES: RangeKey[] = ['7d', '1m', '3m', '6m', '12m'];
type Tab = 'overview' | 'people';

/** The tool columns, in the order the composer offers them. */
const TOOLS: { id: keyof AnalyticsMember; label: string; short: string }[] = [
  { id: 'web_search', label: 'Web search', short: 'Web' },
  { id: 'deep_research', label: 'Deep research', short: 'Research' },
  { id: 'salesforce', label: 'Salesforce', short: 'Salesforce' },
  { id: 'files', label: 'Files and images', short: 'Files' },
  { id: 'agent', label: 'Multi-step agent', short: 'Agent' },
  { id: 'links', label: 'Links and sites', short: 'Links' },
];

export default function AdminAnalyticsPage() {
  const me = useAdminMe();
  const [range, setRange] = useState<RangeKey>('1m');
  const [tab, setTab] = useState<Tab>('overview');
  const [data, setData] = useState<Analytics | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    setData(null);
    adminJson<Analytics>(`analytics?range=${range}`)
      .then((res) => {
        if (!cancelled) setData(res);
      })
      .catch((err) => {
        if (!cancelled)
          setError(
            err instanceof AdminApiError
              ? err.message
              : 'The analytics could not be loaded.',
          );
      });
    return () => {
      cancelled = true;
    };
  }, [range, attempt]);

  const loading = data === null && error === null;
  const summary = data?.summary;
  const windowLabel = data ? `last ${data.range.days} days` : '';

  const columns: AdminColumn<AnalyticsMember>[] = useMemo(
    () => [
      {
        key: 'user',
        label: 'Name',
        render: (m) => (
          <span className="flex min-w-0 items-center gap-2.5">
            <AvatarInitial name={m.name} />
            <span className="min-w-0">
              <span className="block truncate font-medium text-ink">{m.name}</span>
              <span className="block truncate text-xs text-muted">{m.email}</span>
            </span>
          </span>
        ),
      },
      { key: 'role', label: 'Role', render: (m) => <RoleChip role={m.role} /> },
      {
        key: 'status',
        label: 'Status',
        render: (m) => <StatusChip status={m.status} />,
      },
      {
        key: 'messages',
        label: 'Messages',
        align: 'right',
        render: (m) => (
          <span className="tabular-nums text-ink">
            {m.messages.toLocaleString()}
          </span>
        ),
      },
      {
        key: 'conversations',
        label: 'Chats',
        align: 'right',
        render: (m) => (
          <span className="tabular-nums text-muted">
            {m.conversations.toLocaleString()}
          </span>
        ),
      },
      {
        key: 'tool_runs',
        label: 'Tool runs',
        align: 'right',
        render: (m) => (
          <span className="tabular-nums text-muted">
            {m.tool_runs.toLocaleString()}
          </span>
        ),
      },
      ...TOOLS.map<AdminColumn<AnalyticsMember>>((tool) => ({
        key: tool.id as string,
        label: tool.short,
        align: 'right',
        render: (m) => {
          const n = Number(m[tool.id] ?? 0);
          return (
            <span
              className={`tabular-nums ${n ? 'text-muted' : 'text-faint'}`}
              title={`${tool.label}: ${n}`}
            >
              {n.toLocaleString()}
            </span>
          );
        },
      })),
      {
        key: 'last_active',
        label: 'Last active',
        render: (m) =>
          m.last_active_at ? (
            <span className="text-muted">{formatWhen(m.last_active_at)}</span>
          ) : (
            <span className="text-faint">Never</span>
          ),
      },
    ],
    [],
  );

  const pill = (activeItem: boolean) =>
    `rounded-full px-3 py-1 text-xs font-medium transition-colors duration-ts ${
      activeItem
        ? 'bg-surface-2 text-ink'
        : 'text-muted hover:bg-surface-2 hover:text-ink'
    }`;

  return (
    <div>
      <PageHeader
        title="Workspace analytics"
        subtitle={
          data
            ? `${data.workspace.name} · ${windowLabel}`
            : me.workspace.name
        }
        actions={
          <div className="flex items-center gap-2">
            <div
              role="group"
              aria-label="Time range"
              className="flex items-center rounded-full border border-border bg-surface p-0.5"
            >
              {RANGES.map((key) => (
                <button
                  key={key}
                  type="button"
                  aria-pressed={range === key}
                  onClick={() => setRange(key)}
                  className={pill(range === key)}
                >
                  {RANGE_LABEL[key]}
                </button>
              ))}
            </div>
            <a
              href={`/api/admin/analytics/export?range=${range}`}
              className="inline-flex items-center gap-1.5 rounded-lg border border-border bg-surface px-3 py-1.5 text-sm text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink"
            >
              <IconDownload size={14} />
              Export
            </a>
          </div>
        }
      />

      {error ? (
        <div className="mt-6">
          <ErrorPanel message={error} onRetry={() => setAttempt((n) => n + 1)} />
        </div>
      ) : (
        <>
          <div className="mt-6 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
            <StatTile
              label="Members"
              value={summary?.members}
              loading={loading}
            />
            <StatTile
              label="Pending invites"
              value={summary?.pending_invites}
              loading={loading}
            />
            <StatTile
              label={`Active people`}
              value={summary?.active_users}
              loading={loading}
            />
            <StatTile
              label="Messages"
              value={summary?.messages}
              loading={loading}
            />
            <StatTile
              label="Tool runs"
              value={summary?.tool_runs}
              loading={loading}
            />
          </div>

          <div
            role="tablist"
            aria-label="Analytics sections"
            className="mt-6 flex items-center gap-1 border-b border-border"
          >
            {(
              [
                ['overview', 'Overview'],
                ['people', 'People'],
              ] as [Tab, string][]
            ).map(([id, label]) => (
              <button
                key={id}
                type="button"
                role="tab"
                aria-selected={tab === id}
                onClick={() => setTab(id)}
                className={`-mb-px border-b-2 px-3 py-2 text-sm font-medium transition-colors duration-ts ${
                  tab === id
                    ? 'border-accent text-ink'
                    : 'border-transparent text-muted hover:text-ink'
                }`}
              >
                {label}
                {id === 'people' && data ? (
                  <span className="ml-1.5 text-xs text-faint">
                    {data.members.length}
                  </span>
                ) : null}
              </button>
            ))}
          </div>

          {tab === 'overview' ? (
            <div className="mt-5 grid gap-4 lg:grid-cols-3">
              <section className="rounded-ts border border-border bg-surface p-4 lg:col-span-2">
                <h2 className="text-sm font-semibold text-ink">
                  Messages sent
                </h2>
                <p className="mt-0.5 text-xs text-muted">
                  Questions people asked, over the {windowLabel || 'period'}.
                </p>
                <div className="mt-4">
                  {loading ? (
                    <div className="h-[132px] animate-pulse rounded bg-surface-2" />
                  ) : (
                    <UsageChart points={data?.daily ?? []} />
                  )}
                </div>
              </section>

              <section className="rounded-ts border border-border bg-surface p-4">
                <h2 className="text-sm font-semibold text-ink">Tools used</h2>
                <p className="mt-0.5 text-xs text-muted">
                  Answers that ran each tool.
                </p>
                <ul className="mt-4 space-y-2.5">
                  {(data?.tools ?? TOOLS.map((t) => ({ id: t.id as string, label: t.label, count: 0 }))).map(
                    (tool) => {
                      const peak = Math.max(
                        1,
                        ...(data?.tools ?? []).map((t) => t.count),
                      );
                      const label =
                        TOOLS.find((t) => t.id === tool.id)?.label ?? tool.label;
                      return (
                        <li key={tool.id}>
                          <div className="flex items-baseline justify-between gap-2 text-xs">
                            <span className="truncate text-muted">{label}</span>
                            <span className="tabular-nums text-ink">
                              {loading ? (
                                <SkeletonLine className="w-6" />
                              ) : (
                                tool.count.toLocaleString()
                              )}
                            </span>
                          </div>
                          {/* One hue, magnitude only — a bar per tool, not a
                              colour per tool: these are not categories the
                              reader must tell apart, they are sizes. */}
                          <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-surface-2">
                            <div
                              className="h-full rounded-full bg-accent"
                              style={{
                                width: `${loading ? 0 : Math.round((tool.count / peak) * 100)}%`,
                              }}
                            />
                          </div>
                        </li>
                      );
                    },
                  )}
                </ul>
              </section>
            </div>
          ) : (
            <div className="mt-5">
              <AdminTable
                columns={columns}
                rows={data?.members ?? []}
                rowKey={(m) => m.id}
                loading={loading}
                empty="No members yet."
              />
            </div>
          )}
        </>
      )}
    </div>
  );
}
