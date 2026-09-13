'use client';

/**
 * Webhook endpoints for a project.
 *
 * THE SIGNING SECRET IS SHOWN ONCE, ON THE SCREEN THAT CREATES IT (restored
 * 2026-09-13). An earlier rewrite that day removed the show-once view on the
 * belief that the router returned only `has_secret`. It does not:
 * `console_api.create_webhook` answers `{webhook, secret}`, and the secret is
 * in no later read and no rotate or reveal route. The rewrite therefore threw
 * away the only copy, so every endpoint created from the console received
 * deliveries its owner could never verify (the wave-3 re-verify finding). The
 * view is back, and it is CreateKeyDialog's flow — itself InviteDialog's: a
 * bordered code block, a CopyButton, an IconAlert line that says it cannot be
 * shown again, cleared on both the opening and the closing edge, and never put
 * back by a request that finishes after the dialog was dismissed.
 *
 * The URL field says HTTPS out loud because the server refuses anything else:
 * every delivery resolves the hostname and rejects loopback, link-local,
 * private, unique-local and the cloud metadata address, so a plain-HTTP or
 * internal URL is a refusal, not a warning.
 *
 * Enable/Disable is here because the delivery worker disables an endpoint
 * that keeps failing, and a test delivery to a disabled endpoint is a 409 —
 * without the switch, the only way back was a database edit.
 */

import { useEffect, useRef, useState, type FormEvent } from 'react';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { CopyButton } from '@/components/CopyButton';
import { Loader } from '@/components/Loader';
import { useToast } from '@/components/Providers';
import { IconAlert, IconPlus, IconTrash } from '@/components/icons';
import { formatRelative, formatWhen } from '@/lib/format';
import type { AdminColumn } from '@/components/admin/AdminTable';
import {
  AdminDialog,
  FIELD_INPUT,
  Field,
  PRIMARY_BUTTON,
  SECONDARY_BUTTON,
} from '@/components/admin/AdminDialog';
import { RowMenu, type RowMenuItem } from '@/components/admin/RowMenu';
import { StatusChip } from '@/components/admin/chips';
import { ADMIN_PRIMARY_BUTTON, AdminToolbar } from '@/components/admin/controls';
import { ConsoleHeader } from '@/components/admin/analytics/filters';
import { consoleDelete, consolePatch, consolePost, messageOf } from './api';
import { consolePaths } from './paths';
import { ConsoleEmpty, ConsoleTable, MonoValue, ProjectSelect, useProjects } from './shared';
import { useConsole } from './useConsole';
import { useConsoleStatus } from './status';
import { WEBHOOK_EVENTS, type WebhookEndpoint } from './types';

export function WebhooksPanel() {
  const projectsQuery = useProjects();
  const projects = projectsQuery.data?.projects ?? [];
  const [projectId, setProjectId] = useState('');
  const selected = projects.find((p) => p.id === projectId) ?? projects[0] ?? null;

  const hooks = useConsole<{ webhooks: WebhookEndpoint[] }>(
    consolePaths.webhooks(selected?.id ?? ''),
    {},
    selected !== null,
  );
  const { toast } = useToast();
  const { announce } = useConsoleStatus();
  const [createOpen, setCreateOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<WebhookEndpoint | null>(null);

  const rows = hooks.data?.webhooks ?? [];

  useEffect(() => {
    if (hooks.loading) announce('Loading webhook endpoints.');
    else if (hooks.error) announce(hooks.error);
    else announce(`${rows.length} endpoint${rows.length === 1 ? '' : 's'}.`);
  }, [hooks.loading, hooks.error, rows.length, announce]);

  async function remove(endpoint: WebhookEndpoint) {
    try {
      await consoleDelete(consolePaths.webhook(endpoint.project_id, endpoint.id));
      toast('Endpoint removed.');
      hooks.reload();
    } catch (err) {
      toast(messageOf(err, 'The endpoint could not be removed.'), 'error');
    }
  }

  async function sendTest(endpoint: WebhookEndpoint) {
    try {
      await consolePost(consolePaths.testWebhook(endpoint.project_id, endpoint.id), {});
      toast('Test delivery queued.');
      hooks.reload();
    } catch (err) {
      toast(messageOf(err, 'The test delivery could not be sent.'), 'error');
    }
  }

  async function setStatus(endpoint: WebhookEndpoint, status: 'active' | 'disabled') {
    try {
      await consolePatch(consolePaths.webhook(endpoint.project_id, endpoint.id), {
        status,
      });
      toast(status === 'active' ? 'Endpoint enabled.' : 'Endpoint disabled.');
      hooks.reload();
    } catch (err) {
      toast(messageOf(err, 'The endpoint could not be changed.'), 'error');
    }
  }

  const columns: AdminColumn<WebhookEndpoint>[] = [
    {
      key: 'url',
      label: 'Endpoint',
      render: (e) => (
        <div className="min-w-0">
          <div className="truncate font-medium text-ink" title={e.url}>
            {e.url}
          </div>
          <MonoValue value={e.events.join(', ') || 'No events subscribed'} />
        </div>
      ),
    },
    {
      key: 'status',
      label: 'Status',
      width: '120px',
      render: (e) => <StatusChip status={e.status} />,
    },
    {
      key: 'delivery',
      label: 'Last delivery',
      width: '170px',
      // Hidden below lg so the row menu — test, disable, remove — fits a
      // phone beside a readable URL (visual QA, 2026-09-13).
      hideBelowLg: true,
      render: (e) =>
        e.last_delivery_at ? (
          // `block truncate`: an inline span clips nothing, and "delivered ·
          // 12 minutes ago" is wider than 170px (visual QA, 2026-09-13).
          <span className="block truncate text-muted" title={formatWhen(e.last_delivery_at)}>
            {e.last_delivery_status || 'delivered'} ·{' '}
            {formatRelative(e.last_delivery_at)}
          </span>
        ) : (
          <span className="text-faint">Never delivered</span>
        ),
    },
    {
      key: 'failures',
      label: 'Failures',
      width: '110px',
      align: 'right',
      hideBelowLg: true,
      render: (e) => (
        <span
          className={`tabular-nums ${e.consecutive_failures > 0 ? 'text-danger' : 'text-muted'}`}
        >
          {e.consecutive_failures.toLocaleString()}
        </span>
      ),
    },
    {
      key: 'actions',
      label: '',
      width: '56px',
      align: 'right',
      render: (e) => {
        const active = e.status === 'active';
        const items: RowMenuItem[] = [
          ...(active ? [{ id: 'test', label: 'Send test delivery' }] : []),
          { id: 'status', label: active ? 'Disable' : 'Enable' },
          { id: 'delete', label: 'Remove', icon: <IconTrash size={15} />, danger: true },
        ];
        return (
          <RowMenu
            label={`Actions for ${e.url}`}
            items={items}
            onSelect={(action) => {
              // Named, never a catch-all: a fall-through to Remove is the
              // one mistake this menu must not be able to make.
              if (action === 'test') void sendTest(e);
              else if (action === 'status') void setStatus(e, active ? 'disabled' : 'active');
              else if (action === 'delete') setDeleteTarget(e);
            }}
          />
        );
      },
    },
  ];

  if (!projectsQuery.loading && projects.length === 0) {
    return (
      <div>
        <ConsoleHeader title="Webhooks" />
        <ConsoleEmpty
          title="No projects to notify"
          body="A webhook endpoint belongs to a project. Create a project first, then point it at an HTTPS URL you control."
        />
      </div>
    );
  }

  return (
    <div>
      <ConsoleHeader
        title="Webhooks"
        description="Where background responses report their outcome. Deliveries are signed, retried with backoff, and carry the response id rather than its text."
      />

      <AdminToolbar
        action={
          selected ? (
            <button
              type="button"
              onClick={() => setCreateOpen(true)}
              className={ADMIN_PRIMARY_BUTTON}
            >
              <IconPlus size={15} />
              Add endpoint
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
        {!hooks.loading && !hooks.error && rows.length === 0 ? (
          <ConsoleEmpty
            title="No endpoints in this project"
            body="Add an HTTPS endpoint to be told when a background response finishes, fails or is cancelled. Every delivery is signed with a secret you see once, when the endpoint is created."
          />
        ) : (
          <ConsoleTable
            columns={columns}
            minWidth={920}
            rows={rows}
            rowKey={(e) => e.id}
            loading={hooks.loading && hooks.data === null}
            empty="No endpoints in this project."
            error={hooks.error}
            onRetry={hooks.reload}
          />
        )}
      </div>

      <CreateWebhookDialog
        open={createOpen}
        projectId={selected?.id ?? ''}
        onClose={() => setCreateOpen(false)}
        onCreated={() => {
          toast('Endpoint added.');
          hooks.reload();
        }}
      />

      <ConfirmDialog
        open={deleteTarget !== null}
        title="Remove this endpoint?"
        body={
          deleteTarget
            ? `${deleteTarget.url} stops receiving deliveries immediately. Its signing secret is destroyed with it.`
            : ''
        }
        confirmLabel="Remove"
        onConfirm={() => {
          const target = deleteTarget;
          setDeleteTarget(null);
          if (target) void remove(target);
        }}
        onCancel={() => setDeleteTarget(null)}
      />
    </div>
  );
}

/**
 * What POST /projects/{id}/webhooks answers (`console_api.create_webhook`).
 * `secret` is the plaintext signing secret, returned in this response and in
 * no other. Declared here rather than in types.ts, whose WebhookEndpoint is
 * the stored row and correctly never carries it.
 */
interface CreatedWebhook {
  webhook: WebhookEndpoint;
  secret: string;
}

/**
 * Create an endpoint, then show its signing secret once. The secret lives in
 * this component's state only while the success view is open.
 */
function CreateWebhookDialog({
  open,
  projectId,
  onClose,
  onCreated,
}: {
  open: boolean;
  projectId: string;
  onClose: () => void;
  onCreated: () => void;
}) {
  const [url, setUrl] = useState('');
  const [events, setEvents] = useState<string[]>([...WEBHOOK_EVENTS]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<CreatedWebhook | null>(null);

  // Read by an in-flight submit: a secret that arrives after the dialog was
  // dismissed must not be put back into state nobody can see.
  const openRef = useRef(open);
  openRef.current = open;

  // Both edges drop the secret. The panel keeps this component mounted
  // (AdminDialog only renders nothing when closed), so clearing on the next
  // OPEN alone would keep the plaintext in memory for the life of the panel —
  // the exact CreateKeyDialog bug fixed 2026-09-13.
  useEffect(() => {
    setCreated(null);
    if (!open) return;
    setUrl('');
    setEvents([...WEBHOOK_EVENTS]);
    setBusy(false);
    setError(null);
  }, [open]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (busy || !projectId) return;
    setBusy(true);
    setError(null);
    try {
      const res = await consolePost<CreatedWebhook>(consolePaths.webhooks(projectId), {
        url: url.trim(),
        events,
      });
      // A response without a secret has nothing to show once: close as a
      // plain create rather than render `undefined` in a copy box.
      if (openRef.current && typeof res?.secret === 'string' && res.secret) {
        setCreated(res);
      } else if (openRef.current) {
        onClose();
      }
      onCreated();
    } catch (err) {
      setError(messageOf(err, 'The endpoint could not be created.'));
    } finally {
      setBusy(false);
    }
  }

  return (
    <AdminDialog
      open={open}
      size="md"
      title={created ? 'Copy your signing secret' : 'Add webhook endpoint'}
      onClose={onClose}
    >
      {created ? (
        <div>
          <p className="text-sm text-muted">
            Deliveries to{' '}
            <span className="break-all font-medium text-ink">{created.webhook.url}</span>{' '}
            are signed with this secret. Check the{' '}
            <code className="font-mono text-xs">TechSara-Signature</code> header
            against it before trusting a delivery.
          </p>
          <div className="mt-3 flex items-center gap-2 rounded-lg border border-border bg-bg px-3 py-2">
            <code
              data-testid="created-webhook-secret"
              className="min-w-0 flex-1 break-all font-mono text-xs text-ink"
            >
              {created.secret}
            </code>
            <CopyButton text={created.secret} label="Copy secret" />
          </div>
          <p className="mt-2 flex items-start gap-1.5 text-xs text-muted">
            <IconAlert size={13} className="mt-px shrink-0 text-warn" />
            This secret is shown once and cannot be retrieved later — copy it
            now. No page in this console reads it back.
          </p>
          <div className="mt-4 flex justify-end">
            <button type="button" onClick={onClose} className={SECONDARY_BUTTON}>
              Done
            </button>
          </div>
        </div>
      ) : (
        <form onSubmit={submit} className="space-y-3">
          <Field label="HTTPS endpoint">
            <input
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              type="url"
              required
              placeholder="https://example.com/hooks/techsara"
              autoComplete="off"
              className={FIELD_INPUT}
            />
          </Field>
          <p className="text-xs leading-relaxed text-faint">
            HTTPS only, and the address it resolves to must be public: loopback,
            private and link-local addresses are refused so a webhook cannot be
            aimed back at this network.
          </p>
          <fieldset className="rounded-lg border border-border p-3">
            <legend className="px-1 text-xs font-medium text-muted">Events</legend>
            <div className="space-y-2">
              {WEBHOOK_EVENTS.map((event) => (
                <label key={event} className="flex cursor-pointer items-center gap-2.5 text-sm">
                  <input
                    type="checkbox"
                    checked={events.includes(event)}
                    onChange={() =>
                      setEvents((prev) =>
                        prev.includes(event)
                          ? prev.filter((e) => e !== event)
                          : [...prev, event],
                      )
                    }
                    className="h-4 w-4 shrink-0 accent-[var(--ts-accent-strong)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                  />
                  <code className="font-mono text-xs text-ink">{event}</code>
                </label>
              ))}
            </div>
          </fieldset>
          <p className="flex items-start gap-1.5 text-xs text-muted">
            <IconAlert size={13} className="mt-px shrink-0 text-warn" />
            The signing secret is shown once, on the next screen. Copy it then:
            it cannot be shown again.
          </p>
          {error && (
            <p role="alert" className="flex items-start gap-1.5 text-sm text-danger">
              <IconAlert size={15} className="mt-0.5 shrink-0" />
              {error}
            </p>
          )}
          <div className="flex justify-end gap-2 pt-1">
            <button type="button" onClick={onClose} className={SECONDARY_BUTTON}>
              Cancel
            </button>
            <button
              type="submit"
              disabled={busy || !url.trim() || events.length === 0}
              className={PRIMARY_BUTTON}
            >
              {busy && <Loader size={16} />}
              Add endpoint
            </button>
          </div>
        </form>
      )}
    </AdminDialog>
  );
}
