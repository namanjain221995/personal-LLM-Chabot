'use client';

/**
 * /admin — the workspace at a glance: name as the headline, the live
 * counters as stat tiles. One GET, skeleton tiles while it runs.
 */

import { useEffect, useState } from 'react';
import { useAdminMe } from '@/components/admin/AdminMeContext';
import { AdminApiError, adminJson } from '@/components/admin/api';
import { ErrorPanel, PageHeader, StatTile } from '@/components/admin/ui';

interface Overview {
  workspace: { id: string; name: string };
  stats: {
    active_members: number;
    disabled_members: number;
    pending_invites: number;
    conversations: number;
    messages: number;
    live_sessions: number;
    audit_events: number;
  };
}

export default function AdminOverviewPage() {
  const me = useAdminMe();
  const [overview, setOverview] = useState<Overview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    adminJson<Overview>('overview')
      .then((res) => {
        if (!cancelled) setOverview(res);
      })
      .catch((err) => {
        if (!cancelled)
          setError(
            err instanceof AdminApiError
              ? err.message
              : 'The overview could not be loaded.',
          );
      });
    return () => {
      cancelled = true;
    };
  }, [attempt]);

  const loading = overview === null && error === null;
  const stats = overview?.stats;

  return (
    <div>
      <PageHeader
        title={overview?.workspace.name ?? me.workspace.name}
        subtitle="Workspace overview"
      />

      {error ? (
        <div className="mt-6">
          <ErrorPanel message={error} onRetry={() => setAttempt((n) => n + 1)} />
        </div>
      ) : (
        <div className="mt-6 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
          <StatTile
            label="Active members"
            value={stats?.active_members}
            loading={loading}
          />
          <StatTile
            label="Pending invites"
            value={stats?.pending_invites}
            loading={loading}
          />
          <StatTile
            label="Conversations"
            value={stats?.conversations}
            loading={loading}
          />
          <StatTile label="Messages" value={stats?.messages} loading={loading} />
          <StatTile
            label="Live sessions"
            value={stats?.live_sessions}
            loading={loading}
          />
        </div>
      )}
    </div>
  );
}
