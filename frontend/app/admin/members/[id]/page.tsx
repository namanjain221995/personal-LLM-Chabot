'use client';

/**
 * /admin/members/[id] — one member under the audited admin lens: identity
 * header, usage stat tiles, then tabs over their content (conversations →
 * the read-only viewer, uploads and reports with byte-true downloads) and
 * their sessions. Content tabs exist only with workspace_content.read,
 * sessions with sessions.manage — the server 404s regardless; the client
 * just never draws a dead tab.
 */

import { useCallback, useEffect, useState } from 'react';
import Link from 'next/link';
import { useParams, useRouter } from 'next/navigation';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { useToast } from '@/components/Providers';
import { IconFileText } from '@/components/icons';
import { formatBytes, formatWhen } from '@/lib/format';
import { useAdminMe } from '@/components/admin/AdminMeContext';
import {
  AdminApiError,
  adminJson,
  adminPost,
  can,
  deviceOf,
} from '@/components/admin/api';
import { AdminTable, Pagination, type AdminColumn } from '@/components/admin/AdminTable';
import { RoleChip, StatusChip } from '@/components/admin/chips';
import { IconArrowLeft } from '@/components/admin/icons';
import {
  AvatarInitial,
  ErrorPanel,
  SkeletonLine,
  StatTile,
} from '@/components/admin/ui';

const LIMIT = 25;

interface Member {
  id: number;
  name: string;
  email: string;
  role: string;
  status: string;
  joined_at: string | null;
  last_active_at: string | null;
}

interface MemberDetail {
  member: Member;
  stats: {
    conversations: number;
    messages: number;
    uploads: number;
    reports: number;
    memory_facts: number;
    research_runs: number;
  };
}

const DOWNLOAD_LINK =
  'inline-flex items-center gap-1.5 rounded-md border border-border bg-surface px-2.5 py-1 text-xs text-muted no-underline transition-colors duration-ts hover:bg-surface-2 hover:text-ink';

const em = <span className="text-faint">—</span>;

function useList<T>(path: string | null, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (path === null) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    adminJson<T>(path)
      .then((res) => {
        if (!cancelled) setData(res);
      })
      .catch((err) => {
        if (!cancelled)
          setError(
            err instanceof AdminApiError
              ? err.message
              : 'This list could not be loaded.',
          );
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, attempt, ...deps]);
  return { data, loading, error, retry: () => setAttempt((n) => n + 1) };
}

// ---------------------------------------------------------------------------
// Tabs
// ---------------------------------------------------------------------------

interface Conversation {
  id: string;
  title: string;
  updated_at: string | null;
  message_count: number;
}

function ConversationsTab({ memberId }: { memberId: string }) {
  const router = useRouter();
  const [offset, setOffset] = useState(0);
  const { data, loading, error, retry } = useList<{
    conversations: Conversation[];
    total: number;
  }>(`members/${memberId}/conversations?limit=${LIMIT}&offset=${offset}`, []);

  const columns: AdminColumn<Conversation>[] = [
    {
      key: 'title',
      label: 'Title',
      render: (c) => (
        <span className="block max-w-96 truncate font-medium text-ink">
          {c.title || 'Untitled conversation'}
        </span>
      ),
    },
    {
      key: 'updated',
      label: 'Updated',
      render: (c) => (c.updated_at ? formatWhen(c.updated_at) : em),
    },
    {
      key: 'messages',
      label: 'Messages',
      align: 'right',
      render: (c) => (
        <span className="font-mono text-xs">{c.message_count.toLocaleString()}</span>
      ),
    },
  ];

  return (
    <>
      <AdminTable
        columns={columns}
        rows={data?.conversations ?? []}
        rowKey={(c) => c.id}
        onRowClick={(c) =>
          router.push(`/admin/members/${memberId}/conversations/${c.id}`)
        }
        loading={loading && data === null}
        empty="No conversations yet."
        error={error}
        onRetry={retry}
      />
      {data !== null && (
        <Pagination
          total={data.total}
          offset={offset}
          limit={LIMIT}
          onOffset={setOffset}
        />
      )}
    </>
  );
}

interface Upload {
  id: string;
  conversation_id: string;
  conversation_title: string;
  filename: string;
  bytes: number;
  created_at: string | null;
}

function UploadsTab({ memberId }: { memberId: string }) {
  const [offset, setOffset] = useState(0);
  const { data, loading, error, retry } = useList<{
    uploads: Upload[];
    total: number;
  }>(`members/${memberId}/uploads?limit=${LIMIT}&offset=${offset}`, []);

  const columns: AdminColumn<Upload>[] = [
    {
      key: 'filename',
      label: 'File',
      render: (u) => (
        <span className="flex items-center gap-2">
          <IconFileText size={15} className="shrink-0 text-muted" />
          <span className="max-w-72 truncate font-medium text-ink">
            {u.filename}
          </span>
        </span>
      ),
    },
    {
      key: 'bytes',
      label: 'Size',
      align: 'right',
      render: (u) => <span className="font-mono text-xs">{formatBytes(u.bytes)}</span>,
    },
    {
      key: 'conversation',
      label: 'Conversation',
      render: (u) => (
        <span className="block max-w-64 truncate text-muted">
          {u.conversation_title || em}
        </span>
      ),
    },
    {
      key: 'created',
      label: 'Date',
      render: (u) => (u.created_at ? formatWhen(u.created_at) : em),
    },
    {
      key: 'download',
      label: '',
      align: 'right',
      render: (u) => (
        <a
          href={`/api/admin/members/${memberId}/uploads/${encodeURIComponent(u.id)}/download`}
          className={DOWNLOAD_LINK}
        >
          Download
        </a>
      ),
    },
  ];

  return (
    <>
      <AdminTable
        columns={columns}
        rows={data?.uploads ?? []}
        rowKey={(u) => u.id}
        loading={loading && data === null}
        empty="No uploads yet."
        error={error}
        onRetry={retry}
      />
      {data !== null && (
        <Pagination
          total={data.total}
          offset={offset}
          limit={LIMIT}
          onOffset={setOffset}
        />
      )}
    </>
  );
}

interface Report {
  filename: string;
  conversation_id: string;
  created_at: string | null;
}

function ReportsTab({ memberId }: { memberId: string }) {
  const { data, loading, error, retry } = useList<{ reports: Report[] }>(
    `members/${memberId}/reports`,
    [],
  );

  const columns: AdminColumn<Report>[] = [
    {
      key: 'filename',
      label: 'File',
      render: (r) => (
        <span className="flex items-center gap-2">
          <IconFileText size={15} className="shrink-0 text-muted" />
          <span className="max-w-96 truncate font-medium text-ink">
            {r.filename}
          </span>
        </span>
      ),
    },
    {
      key: 'created',
      label: 'Date',
      render: (r) => (r.created_at ? formatWhen(r.created_at) : em),
    },
    {
      key: 'download',
      label: '',
      align: 'right',
      render: (r) => (
        <a
          href={`/api/admin/members/${memberId}/reports/${encodeURIComponent(r.filename)}`}
          className={DOWNLOAD_LINK}
        >
          Download
        </a>
      ),
    },
  ];

  return (
    <AdminTable
      columns={columns}
      rows={data?.reports ?? []}
      rowKey={(r) => r.filename}
      loading={loading && data === null}
      empty="No reports yet."
      error={error}
      onRetry={retry}
    />
  );
}

interface Session {
  id: string;
  created_at: string | null;
  last_seen_at: string | null;
  expires_at: string | null;
  revoked_at: string | null;
  user_agent: string;
  ip: string;
}

function SessionsTab({
  memberId,
  memberName,
}: {
  memberId: string;
  memberName: string;
}) {
  const { toast } = useToast();
  const [confirming, setConfirming] = useState(false);
  const [reload, setReload] = useState(0);
  const { data, loading, error, retry } = useList<{ sessions: Session[] }>(
    `members/${memberId}/sessions`,
    [reload],
  );

  async function revokeAll() {
    setConfirming(false);
    try {
      const res = await adminPost<{ revoked: number }>(
        `members/${memberId}/sessions/revoke`,
        {},
      );
      toast(`Revoked ${res.revoked} session${res.revoked === 1 ? '' : 's'}.`);
      setReload((n) => n + 1);
    } catch (err) {
      toast(
        err instanceof AdminApiError
          ? err.message
          : 'The sessions could not be revoked.',
        'error',
      );
    }
  }

  const columns: AdminColumn<Session>[] = [
    {
      key: 'device',
      label: 'Device',
      render: (s) => (
        <span title={s.user_agent} className="font-medium text-ink">
          {deviceOf(s.user_agent)}
        </span>
      ),
    },
    {
      key: 'ip',
      label: 'IP',
      render: (s) =>
        s.ip ? <span className="font-mono text-xs">{s.ip}</span> : em,
    },
    {
      key: 'created',
      label: 'Created',
      render: (s) => (s.created_at ? formatWhen(s.created_at) : em),
    },
    {
      key: 'seen',
      label: 'Last seen',
      render: (s) => (s.last_seen_at ? formatWhen(s.last_seen_at) : em),
    },
    {
      key: 'until',
      label: 'Expires / revoked',
      render: (s) =>
        s.revoked_at ? (
          <span className="text-danger">Revoked {formatWhen(s.revoked_at)}</span>
        ) : s.expires_at ? (
          `Expires ${formatWhen(s.expires_at)}`
        ) : (
          em
        ),
    },
  ];

  const hasLive = (data?.sessions ?? []).some(
    (s) =>
      !s.revoked_at &&
      (!s.expires_at || new Date(s.expires_at).getTime() > Date.now()),
  );

  return (
    <>
      {hasLive && (
        <div className="mb-3 flex justify-end">
          <button
            type="button"
            onClick={() => setConfirming(true)}
            className="rounded-lg px-3 py-1.5 text-sm font-medium text-white transition-opacity duration-ts hover:opacity-90"
            style={{ background: 'var(--ts-danger)' }}
          >
            Revoke all sessions
          </button>
        </div>
      )}
      <AdminTable
        columns={columns}
        rows={data?.sessions ?? []}
        rowKey={(s) => s.id}
        loading={loading && data === null}
        empty="No sessions recorded."
        error={error}
        onRetry={retry}
      />
      <ConfirmDialog
        open={confirming}
        title={`Revoke all sessions for ${memberName}?`}
        body="They will be signed out everywhere and have to sign in again."
        confirmLabel="Revoke sessions"
        onConfirm={() => void revokeAll()}
        onCancel={() => setConfirming(false)}
      />
    </>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function AdminMemberDetailPage() {
  const me = useAdminMe();
  const params = useParams<{ id: string }>();
  const memberId = String(params?.id ?? '');

  const [detail, setDetail] = useState<MemberDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  const load = useCallback(async () => {
    setError(null);
    try {
      setDetail(await adminJson<MemberDetail>(`members/${memberId}`));
    } catch (err) {
      setError(
        err instanceof AdminApiError
          ? err.message
          : 'This member could not be loaded.',
      );
    }
  }, [memberId]);

  useEffect(() => {
    void load();
  }, [load, attempt]);

  const contentRead = can(me, 'workspace_content.read');
  const sessionsManage = can(me, 'sessions.manage');
  const tabs = [
    ...(contentRead
      ? ([
          { id: 'conversations', label: 'Conversations' },
          { id: 'uploads', label: 'Uploads' },
          { id: 'reports', label: 'Reports' },
        ] as const)
      : []),
    ...(sessionsManage ? ([{ id: 'sessions', label: 'Sessions' }] as const) : []),
  ];
  const [tab, setTab] = useState<string>(tabs[0]?.id ?? '');

  const loading = detail === null && error === null;
  const member = detail?.member;
  const stats = detail?.stats;

  const tabClass = (active: boolean) =>
    `-mb-px border-b-2 px-3 py-2 text-sm font-medium transition-colors duration-ts ${
      active
        ? 'border-accent text-ink'
        : 'border-transparent text-muted hover:text-ink'
    }`;

  return (
    <div>
      <Link
        href="/admin/members"
        className="inline-flex items-center gap-1.5 text-sm text-muted transition-colors duration-ts hover:text-ink"
      >
        <IconArrowLeft size={15} />
        Members
      </Link>

      {error ? (
        <div className="mt-4">
          <ErrorPanel message={error} onRetry={() => setAttempt((n) => n + 1)} />
        </div>
      ) : (
        <>
          <div className="mt-4 flex flex-wrap items-center gap-4">
            <AvatarInitial name={member?.name ?? ''} size="lg" />
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-2.5">
                <h1 className="truncate text-xl font-semibold tracking-tight text-ink">
                  {loading ? <SkeletonLine className="w-40" /> : member?.name}
                </h1>
                {member && <RoleChip role={member.role} />}
                {member && <StatusChip status={member.status} />}
              </div>
              <p className="mt-1 truncate text-sm text-muted">
                {loading ? <SkeletonLine className="w-56" /> : member?.email}
              </p>
              {member && (
                <p className="mt-0.5 text-xs text-faint">
                  {member.joined_at ? `Joined ${formatWhen(member.joined_at)}` : ''}
                  {member.joined_at && member.last_active_at ? ' · ' : ''}
                  {member.last_active_at
                    ? `Last active ${formatWhen(member.last_active_at)}`
                    : ''}
                </p>
              )}
            </div>
          </div>

          <div className="mt-6 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
            <StatTile label="Conversations" value={stats?.conversations} loading={loading} />
            <StatTile label="Messages" value={stats?.messages} loading={loading} />
            <StatTile label="Uploads" value={stats?.uploads} loading={loading} />
            <StatTile label="Reports" value={stats?.reports} loading={loading} />
            <StatTile label="Memory facts" value={stats?.memory_facts} loading={loading} />
            <StatTile label="Research runs" value={stats?.research_runs} loading={loading} />
          </div>

          {tabs.length > 0 && (
            <>
              <div
                role="tablist"
                aria-label="Member content"
                className="mt-6 flex gap-1 border-b border-border"
              >
                {tabs.map((t) => (
                  <button
                    key={t.id}
                    type="button"
                    role="tab"
                    aria-selected={tab === t.id}
                    onClick={() => setTab(t.id)}
                    className={tabClass(tab === t.id)}
                  >
                    {t.label}
                  </button>
                ))}
              </div>

              <div className="mt-4">
                {tab === 'conversations' && <ConversationsTab memberId={memberId} />}
                {tab === 'uploads' && <UploadsTab memberId={memberId} />}
                {tab === 'reports' && <ReportsTab memberId={memberId} />}
                {tab === 'sessions' && (
                  <SessionsTab memberId={memberId} memberName={member?.name ?? 'this member'} />
                )}
              </div>
            </>
          )}
        </>
      )}
    </div>
  );
}
