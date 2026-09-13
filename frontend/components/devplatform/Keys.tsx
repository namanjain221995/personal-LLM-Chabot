'use client';

/**
 * API keys for one project.
 *
 * The list shows what a person needs to RECOGNISE a key — its name, its
 * public id, its last four characters, when it was last used — and nothing
 * that could reconstruct it. CONTRACT §5: the secret is HMAC'd with a pepper
 * and never stored, so "show key" is not a feature this console withholds, it
 * is a thing that does not exist.
 *
 * Revocation is a single UPDATE upstream and the resolver reads the key row on
 * every request with no cached state, so it takes effect immediately
 * everywhere — the ConfirmDialog says so, because "revoke" that quietly meant
 * "in five minutes" would be the worst possible surprise on this page.
 */

import { useEffect, useMemo, useState } from 'react';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { useToast } from '@/components/Providers';
import { IconBan } from '@/components/admin/icons';
import { IconPlus } from '@/components/icons';
import { formatRelative, formatWhen } from '@/lib/format';
import { can, type Me } from '@/components/admin/api';
import type { AdminColumn } from '@/components/admin/AdminTable';
import { RowMenu, type RowMenuItem } from '@/components/admin/RowMenu';
import { StatusChip } from '@/components/admin/chips';
import { ADMIN_PRIMARY_BUTTON, AdminToolbar } from '@/components/admin/controls';
import { ConsoleHeader } from '@/components/admin/analytics/filters';
import { consolePost, messageOf } from './api';
import { consolePaths } from './paths';
import { CreateKeyDialog } from './CreateKeyDialog';
import {
  ConsoleEmpty,
  ConsoleTable,
  EnvironmentChip,
  MonoValue,
  ProjectSelect,
  useProjects,
} from './shared';
import { useConsole } from './useConsole';
import { useConsoleStatus } from './status';
import type { ApiKey } from './types';

/**
 * A key's scopes, in a column that is 200px wide.
 *
 * VISUAL QA, 2026-09-13: the cell was `<span class="truncate">` holding
 * "models.read, responses.read, responses.write". `truncate` on an inline
 * span clips nothing, so at 1440px the list ran straight over Status and Last
 * used and all three columns were unreadable. Now it draws the first scope and
 * a count — which fits the column by construction — and carries the whole
 * list as the hover title and as screen-reader text, so nothing is lost, only
 * folded.
 */
export function ScopeSummary({ scopes }: { scopes: string[] }) {
  if (scopes.length === 0) {
    return (
      <span data-testid="key-scopes" className="text-xs text-faint">
        None
      </span>
    );
  }
  const all = scopes.join(', ');
  const [first, ...rest] = scopes;
  return (
    <span
      data-testid="key-scopes"
      title={all}
      className="flex min-w-0 items-center gap-1.5"
    >
      <span className="sr-only">{all}</span>
      <code
        aria-hidden="true"
        className="min-w-0 truncate rounded-md border border-border px-1.5 py-0.5 font-mono text-[11px] text-muted"
      >
        {first}
      </code>
      {rest.length > 0 && (
        <span
          aria-hidden="true"
          className="shrink-0 rounded-md border border-border px-1.5 py-0.5 text-[11px] tabular-nums text-faint"
        >
          +{rest.length}
        </span>
      )}
    </span>
  );
}

export function KeysPanel({ me }: { me: Me }) {
  const projectsQuery = useProjects();
  const projects = projectsQuery.data?.projects ?? [];
  const [projectId, setProjectId] = useState('');
  const selected =
    projects.find((p) => p.id === projectId) ?? projects[0] ?? null;

  const keys = useConsole<{ keys: ApiKey[] }>(
    consolePaths.keys(selected?.id ?? ''),
    {},
    selected !== null,
  );
  const { toast } = useToast();
  const { announce } = useConsoleStatus();
  const [createOpen, setCreateOpen] = useState(false);
  const [revokeTarget, setRevokeTarget] = useState<ApiKey | null>(null);

  const rows = keys.data?.keys ?? [];
  const mayCreate = can(me, 'api.keys.create');
  const mayRevoke = can(me, 'api.keys.revoke');

  useEffect(() => {
    if (keys.loading) announce('Loading API keys.');
    else if (keys.error) announce(keys.error);
    else announce(`${rows.length} key${rows.length === 1 ? '' : 's'}.`);
  }, [keys.loading, keys.error, rows.length, announce]);

  async function revoke(key: ApiKey) {
    try {
      await consolePost(consolePaths.revokeKey(key.project_id, key.id), {});
      toast(`${key.name} revoked.`);
      keys.reload();
    } catch (err) {
      toast(messageOf(err, 'The key could not be revoked.'), 'error');
    }
  }

  const columns: AdminColumn<ApiKey>[] = useMemo(
    () => [
      {
        key: 'name',
        label: 'Key',
        render: (k) => (
          <div className="min-w-0">
            <div className="truncate font-medium text-ink" title={k.name}>
              {k.name}
            </div>
            {/* Recognition only: the prefix and the last four, never a secret. */}
            <MonoValue value={`tsk_${k.environment}_${k.public_id}…${k.last_four}`} />
          </div>
        ),
      },
      {
        key: 'environment',
        label: 'Environment',
        width: '130px',
        // Below lg the key's own prefix (tsk_live_ / tsk_test_) says it, and
        // the 130px goes to the key column so a phone can still read it.
        hideBelowLg: true,
        render: (k) => <EnvironmentChip environment={k.environment} />,
      },
      {
        key: 'scopes',
        label: 'Scopes',
        width: '200px',
        hideBelowLg: true,
        render: (k) => <ScopeSummary scopes={k.scopes} />,
      },
      {
        key: 'status',
        label: 'Status',
        width: '110px',
        render: (k) => <StatusChip status={k.status} />,
      },
      {
        key: 'used',
        label: 'Last used',
        width: '150px',
        hideBelowLg: true,
        render: (k) =>
          k.last_used_at ? (
            <span className="text-muted" title={formatWhen(k.last_used_at)}>
              {formatRelative(k.last_used_at)}
            </span>
          ) : (
            // "Never" is a measurement, not a missing one — say the word.
            <span className="text-faint">Never</span>
          ),
      },
      {
        key: 'actions',
        label: '',
        // NEVER hideBelowLg: a leaked key is an emergency, and this menu is
        // the only way to revoke one. Status stays beside it for the same
        // reason — it is what the decision is made on.
        width: '56px',
        align: 'right',
        render: (k) => {
          const items: RowMenuItem[] = [];
          if (mayRevoke && k.status === 'active') {
            items.push({
              id: 'revoke',
              label: 'Revoke',
              icon: <IconBan size={15} />,
              danger: true,
            });
          }
          return (
            <RowMenu
              label={`Actions for ${k.name}`}
              items={items}
              onSelect={(action) => {
                // Named rather than assumed: a second item added later must
                // not fall through to a catch-all that revokes.
                if (action === 'revoke') setRevokeTarget(k);
              }}
            />
          );
        },
      },
    ],
    [mayRevoke],
  );

  return (
    <div>
      <ConsoleHeader
        title="API keys"
        description="A key carries the whole identity of a call: its project, its scopes and its limits. The secret is shown once, at creation, and never again."
      />

      <AdminToolbar
        action={
          mayCreate && selected ? (
            <button
              type="button"
              onClick={() => setCreateOpen(true)}
              className={ADMIN_PRIMARY_BUTTON}
            >
              <IconPlus size={15} />
              Create key
            </button>
          ) : undefined
        }
      >
        <ProjectSelect
          projects={projects}
          value={selected?.id ?? ''}
          onChange={setProjectId}
        />
      </AdminToolbar>

      <div className="mt-5">
        {!projectsQuery.loading && projects.length === 0 ? (
          <ConsoleEmpty
            title="No projects, so no keys"
            body="A key belongs to a project — create one on the Projects tab first, then come back here to mint a key for it."
          />
        ) : !keys.loading && !keys.error && rows.length === 0 ? (
          <ConsoleEmpty
            title="No keys in this project"
            body="Create a key to start calling /v1. You will see the secret exactly once, on the screen that creates it, so have somewhere ready to put it."
            action={
              mayCreate ? (
                <button
                  type="button"
                  onClick={() => setCreateOpen(true)}
                  className={ADMIN_PRIMARY_BUTTON}
                >
                  <IconPlus size={15} />
                  Create key
                </button>
              ) : undefined
            }
          />
        ) : (
          <ConsoleTable
            columns={columns}
            minWidth={920}
            rows={rows}
            rowKey={(k) => k.id}
            loading={keys.loading && keys.data === null}
            empty="No keys in this project."
            error={keys.error}
            onRetry={keys.reload}
          />
        )}
      </div>

      <CreateKeyDialog
        open={createOpen}
        project={selected}
        onClose={() => setCreateOpen(false)}
        onCreated={keys.reload}
      />

      <ConfirmDialog
        open={revokeTarget !== null}
        title="Revoke this key?"
        body={
          revokeTarget
            ? `${revokeTarget.name} stops working on its very next request, everywhere it is deployed. This cannot be undone — replace it with a new key first if something is live.`
            : ''
        }
        confirmLabel="Revoke"
        onConfirm={() => {
          const target = revokeTarget;
          setRevokeTarget(null);
          if (target) void revoke(target);
        }}
        onCancel={() => setRevokeTarget(null)}
      />
    </div>
  );
}
