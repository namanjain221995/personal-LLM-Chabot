'use client';

/**
 * /admin/audit — the audit trail (super admin only; audit.read). Keyset
 * pagination via next_before_id: filtering restarts the list, "Load more"
 * appends older events. The sidebar already hides the link without the
 * capability; landing here anyway bounces to /admin (the server 404s the
 * data regardless).
 */

import { useEffect, useRef, useState } from 'react';
import { Loader } from '@/components/Loader';
import { IconSearch } from '@/components/icons';
import { formatWhen } from '@/lib/format';
import { useAdminMe } from '@/components/admin/AdminMeContext';
import {
  AdminApiError,
  adminJson,
  can,
} from '@/components/admin/api';
import { AdminTable, type AdminColumn } from '@/components/admin/AdminTable';
import { nav } from '@/components/admin/nav';
import { PageHeader } from '@/components/admin/ui';
import { useDebounced } from '@/components/admin/useDebounced';

const LIMIT = 50;

interface AuditActor {
  id: number | null;
  name: string;
  email: string;
}

interface AuditEvent {
  id: number;
  action: string;
  actor: AuditActor;
  target: AuditActor;
  resource_type: string | null;
  resource_id: string | null;
  ip: string;
  created_at: string | null;
}

interface AuditPage {
  events: AuditEvent[];
  next_before_id: number | null;
}

const em = <span className="text-faint">—</span>;

function person(p: AuditActor) {
  if (!p.name && !p.email) return em;
  return (
    <span className="min-w-0">
      <span className="block max-w-48 truncate font-medium text-ink">
        {p.name || p.email}
      </span>
      {p.name && p.email && (
        <span className="block max-w-48 truncate text-xs text-muted">
          {p.email}
        </span>
      )}
    </span>
  );
}

export default function AdminAuditPage() {
  const me = useAdminMe();
  const allowed = can(me, 'audit.read');

  const [action, setAction] = useState('');
  const debouncedAction = useDebounced(action, 300);
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [nextBeforeId, setNextBeforeId] = useState<number | null>(null);
  const [exhausted, setExhausted] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  // Guards against a slow first page landing after a filter changed.
  const queryRef = useRef('');

  useEffect(() => {
    if (!allowed) nav.assign('/admin');
  }, [allowed]);

  useEffect(() => {
    if (!allowed) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    setEvents([]);
    setExhausted(false);
    const params = new URLSearchParams({
      action: debouncedAction.trim(),
      limit: String(LIMIT),
    });
    queryRef.current = params.toString();
    adminJson<AuditPage>(`audit?${params.toString()}`)
      .then((res) => {
        if (cancelled || queryRef.current !== params.toString()) return;
        setEvents(res.events);
        setNextBeforeId(res.next_before_id);
        setExhausted(res.events.length < LIMIT || res.next_before_id === null);
      })
      .catch((err) => {
        if (!cancelled)
          setError(
            err instanceof AdminApiError
              ? err.message
              : 'The audit log could not be loaded.',
          );
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [allowed, debouncedAction, attempt]);

  async function loadMore() {
    if (loadingMore || exhausted || nextBeforeId === null) return;
    setLoadingMore(true);
    try {
      const params = new URLSearchParams({
        action: debouncedAction.trim(),
        limit: String(LIMIT),
        before_id: String(nextBeforeId),
      });
      const res = await adminJson<AuditPage>(`audit?${params.toString()}`);
      setEvents((prev) => [...prev, ...res.events]);
      setNextBeforeId(res.next_before_id);
      setExhausted(res.events.length < LIMIT || res.next_before_id === null);
    } catch (err) {
      setError(
        err instanceof AdminApiError
          ? err.message
          : 'Older events could not be loaded.',
      );
    } finally {
      setLoadingMore(false);
    }
  }

  if (!allowed) return null;

  const columns: AdminColumn<AuditEvent>[] = [
    {
      key: 'time',
      label: 'Time',
      render: (e) => (e.created_at ? formatWhen(e.created_at) : em),
    },
    { key: 'actor', label: 'Actor', render: (e) => person(e.actor) },
    {
      key: 'action',
      label: 'Action',
      render: (e) => (
        <span className="inline-flex rounded-full border border-border bg-surface-2/60 px-2 py-0.5 font-mono text-[11px] text-muted">
          {e.action}
        </span>
      ),
    },
    { key: 'target', label: 'Target', render: (e) => person(e.target) },
    {
      key: 'resource',
      label: 'Resource',
      render: (e) =>
        e.resource_type ? (
          <span className="block max-w-56 truncate font-mono text-xs text-muted">
            {e.resource_type}
            {e.resource_id ? `:${e.resource_id}` : ''}
          </span>
        ) : (
          em
        ),
    },
    {
      key: 'ip',
      label: 'IP',
      render: (e) =>
        e.ip ? <span className="font-mono text-xs">{e.ip}</span> : em,
    },
  ];

  return (
    <div>
      <PageHeader title="Audit Log" subtitle={me.workspace.name} />

      <div className="mt-4 flex flex-wrap items-center gap-2">
        <div className="flex min-w-0 flex-1 items-center gap-2 rounded-lg border border-border bg-bg px-2.5 transition-colors duration-ts focus-within:border-accent/60 sm:max-w-xs">
          <IconSearch size={14} className="shrink-0 text-faint" />
          <input
            value={action}
            onChange={(e) => setAction(e.target.value)}
            placeholder="Filter by action, e.g. role_changed"
            aria-label="Filter by action"
            className="min-w-0 flex-1 bg-transparent py-1.5 font-mono text-sm text-ink placeholder:text-faint focus:outline-none"
          />
        </div>
      </div>

      <div className="mt-4">
        <AdminTable
          columns={columns}
          rows={events}
          rowKey={(e) => e.id}
          loading={loading}
          skeletonRows={8}
          empty={
            debouncedAction.trim()
              ? 'No events match this action.'
              : 'No audit events yet.'
          }
          error={error}
          onRetry={() => setAttempt((n) => n + 1)}
        />
      </div>

      {!loading && !error && !exhausted && (
        <div className="mt-3 flex justify-center">
          <button
            type="button"
            onClick={() => void loadMore()}
            disabled={loadingMore}
            className="inline-flex items-center gap-2 rounded-md border border-border bg-surface px-3 py-1.5 text-sm text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink disabled:cursor-not-allowed disabled:opacity-40"
          >
            {loadingMore && <Loader size={16} />}
            Load more
          </button>
        </div>
      )}
    </div>
  );
}
