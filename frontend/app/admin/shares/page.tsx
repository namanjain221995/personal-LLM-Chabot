'use client';

/**
 * /admin/shares — every link that leaves this workspace.
 *
 * WHAT THIS PAGE DELIBERATELY CANNOT DO. It cannot open a shared
 * conversation, and it cannot hand out a working link. The API returns only
 * the addressable half of each token; the secret was hashed at creation and
 * nobody — including whoever is reading this page — can recover it. A
 * governance console that let an administrator read every conversation an
 * author had published would be a bigger leak than the feature it governs;
 * reading a member's content is a different power, behind
 * `workspace_content.read` and its own audit trail.
 *
 * THE POLICY IS A CEILING, NOT A DEFAULT. Turning public links off does not
 * hide a button — the orchestrator re-reads this policy on every create and
 * every republish, so an author with a stale tab open still cannot publish.
 * Existing links are a SEPARATE decision, asked for separately: tightening a
 * rule for the future is not the same as breaking the link somebody sent a
 * customer this morning, and quietly doing the second when asked for the
 * first is how a console loses its administrator's trust.
 *
 * The sidebar hides this page without `shares.manage`; landing here anyway
 * bounces to /admin, and every endpoint behind it 404s regardless.
 */

import { useCallback, useEffect, useState } from 'react';
import { formatWhen } from '@/lib/format';
import { useAdminMe } from '@/components/admin/AdminMeContext';
import {
  AdminApiError,
  adminJson,
  can,
} from '@/components/admin/api';
import { AdminTable, type AdminColumn } from '@/components/admin/AdminTable';
import { AdminDialog } from '@/components/admin/AdminDialog';
import { AdminSelect, AdminToolbar } from '@/components/admin/controls';
import { nav } from '@/components/admin/nav';
import { Switch } from '@/components/admin/Switch';
import { ErrorPanel, PageHeader, StatTile } from '@/components/admin/ui';

interface ShareAuthor {
  id: number | null;
  name: string | null;
  email: string | null;
}

interface ShareRow {
  id: number;
  conversation_id: string;
  title: string;
  visibility: 'public' | 'workspace';
  status: 'active' | 'revoked';
  public_id: string | null;
  created_at: string | null;
  expires_at: string | null;
  revoked_at: string | null;
  last_viewed_at: string | null;
  view_count: number;
  message_count: number;
  author: ShareAuthor;
}

interface SharePolicy {
  public_enabled: boolean;
  workspace_enabled: boolean;
  allow_never: boolean;
  allow_owner_name: boolean;
  max_days: number;
}

interface SharesPage {
  shares: ShareRow[];
  summary: {
    active: number;
    public: number;
    workspace: number;
    views: number;
    authors: number;
  };
  policy: SharePolicy;
}

const em = <span className="text-faint">—</span>;

function Pill({ tone, children }: { tone: 'live' | 'off' | 'internal'; children: string }) {
  const styles = {
    live: 'border-ok/40 bg-ok/10 text-ok',
    internal: 'border-border bg-surface-2 text-muted',
    off: 'border-border bg-surface-2 text-faint',
  }[tone];
  return (
    <span
      className={`inline-flex items-center rounded-md border px-1.5 py-0.5 text-[11px] font-medium ${styles}`}
    >
      {children}
    </span>
  );
}

/**
 * A setting and its consequence, on one row. The hint is not decoration:
 * every switch here changes what may leave the building, and a switch whose
 * effect you have to guess gets flipped by guesswork.
 */
function PolicyRow({
  title,
  hint,
  checked,
  disabled,
  onChange,
}: {
  title: string;
  hint: string;
  checked: boolean;
  disabled: boolean;
  onChange: (next: boolean) => void;
}) {
  const hintId = `policy-${title.replace(/\W+/g, '-').toLowerCase()}`;
  return (
    <div className="flex items-start justify-between gap-6 border-b border-[var(--admin-separator)] py-3.5 last:border-b-0">
      <div className="min-w-0">
        <div className="text-sm font-medium text-ink">{title}</div>
        <p id={hintId} className="mt-0.5 text-xs leading-relaxed text-muted">
          {hint}
        </p>
      </div>
      <Switch
        checked={checked}
        disabled={disabled}
        label={title}
        describedBy={hintId}
        onChange={onChange}
      />
    </div>
  );
}

export default function AdminSharesPage() {
  const me = useAdminMe();
  const allowed = can(me, 'shares.manage');

  const [data, setData] = useState<SharesPage | null>(null);
  const [status, setStatus] = useState('active');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState<ShareRow | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    if (!allowed) nav.assign('/admin');
  }, [allowed]);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await adminJson<SharesPage>(`shares?status=${status}`));
    } catch (err) {
      if (err instanceof AdminApiError && err.status === 401) return;
      setError(err instanceof Error ? err.message : 'Could not load shared links.');
    } finally {
      setLoading(false);
    }
  }, [status]);

  useEffect(() => {
    if (!allowed) return;
    void load();
  }, [allowed, load, attempt]);

  async function revoke(row: ShareRow) {
    setBusy(true);
    try {
      await adminJson(`shares/${row.id}`, { method: 'DELETE' });
      setNotice(`The link to “${row.title}” no longer opens.`);
      setConfirming(null);
      await load();
    } catch (err) {
      if (err instanceof AdminApiError && err.status === 401) return;
      setError(err instanceof Error ? err.message : 'Could not revoke the link.');
    } finally {
      setBusy(false);
    }
  }

  async function setPolicy(patch: Partial<SharePolicy> & { revoke_existing_public?: boolean }) {
    setBusy(true);
    try {
      const body = await adminJson<{ policy: SharePolicy; revoked: number }>(
        'shares/policy',
        {
          method: 'PATCH',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify(patch),
        },
      );
      setNotice(
        body.revoked > 0
          ? `Policy saved. ${body.revoked} existing public ${
              body.revoked === 1 ? 'link was' : 'links were'
            } revoked.`
          : 'Policy saved. Links that are already out keep working.',
      );
      await load();
    } catch (err) {
      if (err instanceof AdminApiError && err.status === 401) return;
      setError(err instanceof Error ? err.message : 'Could not save the policy.');
    } finally {
      setBusy(false);
    }
  }

  if (!allowed) return null;

  const policy = data?.policy;
  const livePublic = data?.summary.public ?? 0;

  const columns: AdminColumn<ShareRow>[] = [
    {
      key: 'title',
      label: 'Conversation',
      render: (r) => (
        <span className="min-w-0">
          <span className="block truncate font-medium text-ink">{r.title}</span>
          <span className="block truncate text-xs text-muted">
            {r.message_count} message{r.message_count === 1 ? '' : 's'}
            {r.public_id ? ` · /share/${r.public_id.slice(0, 8)}…` : ''}
          </span>
        </span>
      ),
    },
    {
      key: 'author',
      label: 'Shared by',
      width: '200px',
      render: (r) => (
        <span className="min-w-0">
          <span className="block max-w-48 truncate text-ink">
            {r.author.name || r.author.email || '—'}
          </span>
          {r.author.name && r.author.email && (
            <span className="block max-w-48 truncate text-xs text-muted">
              {r.author.email}
            </span>
          )}
        </span>
      ),
    },
    {
      key: 'visibility',
      label: 'Reach',
      width: '130px',
      render: (r) =>
        r.status !== 'active' ? (
          <Pill tone="off">Revoked</Pill>
        ) : r.visibility === 'public' ? (
          <Pill tone="live">Anyone with link</Pill>
        ) : (
          <Pill tone="internal">Workspace</Pill>
        ),
    },
    {
      key: 'views',
      label: 'Views',
      align: 'right',
      width: '90px',
      hideBelowLg: true,
      render: (r) => (
        <span className="tabular-nums text-ink">{r.view_count.toLocaleString()}</span>
      ),
    },
    {
      key: 'expires',
      label: 'Expires',
      width: '150px',
      hideBelowLg: true,
      render: (r) =>
        r.expires_at ? (
          <span className="text-muted">{formatWhen(r.expires_at)}</span>
        ) : (
          <span className="text-muted">Never</span>
        ),
    },
    {
      key: 'created',
      label: 'Shared',
      width: '150px',
      render: (r) =>
        r.created_at ? <span className="text-muted">{formatWhen(r.created_at)}</span> : em,
    },
    {
      key: 'action',
      label: '',
      align: 'right',
      width: '110px',
      render: (r) =>
        r.status === 'active' ? (
          <button
            type="button"
            onClick={() => setConfirming(r)}
            className="rounded-lg border border-border px-2.5 py-1.5 text-xs font-medium text-danger transition-colors duration-ts hover:bg-danger/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            Revoke
          </button>
        ) : (
          em
        ),
    },
  ];

  return (
    <div className="space-y-6">
      <PageHeader
        title="Shared links"
        subtitle="Every conversation published out of this workspace, who published it, and how often it has been opened. Links can be revoked here; the conversations themselves are not readable from this page."
      />

      {error && <ErrorPanel message={error} onRetry={() => setAttempt((a) => a + 1)} />}
      {notice && (
        <p
          role="status"
          className="rounded-ts border border-border bg-surface px-4 py-2.5 text-sm text-muted"
        >
          {notice}
        </p>
      )}

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatTile label="Live links" value={data?.summary.active} loading={loading} />
        <StatTile label="Public" value={livePublic} loading={loading} />
        <StatTile label="Workspace only" value={data?.summary.workspace} loading={loading} />
        <StatTile label="Total views" value={data?.summary.views} loading={loading} />
      </div>

      <section className="rounded-ts border border-border bg-surface px-4 py-1">
        <h2 className="sr-only">Sharing policy</h2>
        <PolicyRow
          title="Allow public links"
          hint="When off, nobody in this workspace can publish a conversation to the open internet — including from a page that was already open."
          checked={policy?.public_enabled ?? false}
          disabled={busy || loading || !policy}
          onChange={(next) => void setPolicy({ public_enabled: next })}
        />
        <PolicyRow
          title="Allow workspace links"
          hint="A read-only snapshot that only signed-in colleagues can open."
          checked={policy?.workspace_enabled ?? false}
          disabled={busy || loading || !policy}
          onChange={(next) => void setPolicy({ workspace_enabled: next })}
        />
        <PolicyRow
          title="Allow links that never expire"
          hint="Off by default. A link with no expiry outlives the reason it was created."
          checked={policy?.allow_never ?? false}
          disabled={busy || loading || !policy}
          onChange={(next) => void setPolicy({ allow_never: next })}
        />
        <PolicyRow
          title="Let authors show their name"
          hint="Off means every shared page is anonymous, whatever the author chooses. Email addresses are never shown either way."
          checked={policy?.allow_owner_name ?? false}
          disabled={busy || loading || !policy}
          onChange={(next) => void setPolicy({ allow_owner_name: next })}
        />
      </section>

      {policy && !policy.public_enabled && livePublic > 0 && (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-ts border border-border bg-surface px-4 py-3">
          <p className="text-sm text-muted">
            Public sharing is off, but {livePublic} public{' '}
            {livePublic === 1 ? 'link is' : 'links are'} still live from before.
          </p>
          <button
            type="button"
            disabled={busy}
            onClick={() =>
              void setPolicy({ public_enabled: false, revoke_existing_public: true })
            }
            className="rounded-lg border border-border px-3 py-1.5 text-sm font-medium text-danger transition-colors duration-ts hover:bg-danger/10 disabled:opacity-50"
          >
            Revoke them too
          </button>
        </div>
      )}

      <AdminToolbar>
        <AdminSelect
          value={status}
          onChange={setStatus}
          label="Status"
          options={[
            { value: 'active', label: 'Live links' },
            { value: 'revoked', label: 'Revoked' },
            { value: 'all', label: 'All' },
          ]}
        />
      </AdminToolbar>

      <AdminTable
        columns={columns}
        rows={data?.shares ?? []}
        rowKey={(r) => r.id}
        loading={loading}
        minWidth={900}
        empty={
          status === 'active'
            ? 'Nothing is shared out of this workspace right now.'
            : 'No links match this filter.'
        }
      />

      <AdminDialog
        open={confirming !== null}
        title="Revoke this link?"
        onClose={() => setConfirming(null)}
      >
        <p className="text-sm leading-relaxed text-muted">
          Anyone opening “{confirming?.title}” will see that the link is no longer
          available. {confirming?.author.name || 'The author'} can publish a new
          link afterwards; this one will not work again.
        </p>
        <div className="mt-5 flex justify-end gap-2">
          <button
            type="button"
            onClick={() => setConfirming(null)}
            className="rounded-lg border border-border px-3 py-2 text-sm text-ink transition-colors duration-ts hover:bg-surface-2"
          >
            Keep it
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => confirming && void revoke(confirming)}
            className="rounded-lg bg-danger px-3 py-2 text-sm font-medium text-white transition-opacity duration-ts hover:opacity-90 disabled:opacity-50"
          >
            {busy ? 'Revoking…' : 'Revoke link'}
          </button>
        </div>
      </AdminDialog>
    </div>
  );
}
