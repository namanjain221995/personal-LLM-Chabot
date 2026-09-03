'use client';

/**
 * Pending invitations — the second Members tab and the whole /admin
 * /invitations page. Status is derived client-side from the timestamps
 * (pending / accepted / revoked / expired); revoking asks first, and a dead
 * invitation (expired or revoked) can be re-issued with the same details —
 * which mints a NEW one-time link via the standard invite dialog.
 */

import { useCallback, useEffect, useState } from 'react';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { useToast } from '@/components/Providers';
import { formatWhen } from '@/lib/format';
import {
  AdminApiError,
  adminJson,
  adminPost,
  inviteStatusOf,
  type Invitation,
  type InviteStatus,
} from './api';
import { AdminTable, type AdminColumn } from './AdminTable';
import { InviteDialog, type InvitePrefill } from './InviteDialog';
import { RoleChip, StatusChip } from './chips';

export function InvitesPanel({
  refresh = 0,
  onChanged,
  status = '',
  empty,
}: {
  /** Bump to force a reload (e.g. after the header's invite dialog). */
  refresh?: number;
  /** Called after a revoke or re-invite, so counts elsewhere can update. */
  onChanged?: () => void;
  /**
   * Server-side filter. The Members tab passes 'pending' — it is titled
   * "Pending invites" and used to list every invitation ever sent, so a
   * workspace whose invites had all been accepted showed nine rows reading
   * "Accepted" under a heading promising one pending (owner report,
   * 2026-09-03). Empty keeps the whole history, for /admin/invitations.
   */
  status?: '' | InviteStatus;
  /** Override the empty-state sentence for a filtered list. */
  empty?: string;
}) {
  const { toast } = useToast();
  const [invitations, setInvitations] = useState<Invitation[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const [revoking, setRevoking] = useState<Invitation | null>(null);
  const [reinvite, setReinvite] = useState<InvitePrefill | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    adminJson<{ invitations: Invitation[] }>(
      status ? `invitations?status=${status}` : 'invitations',
    )
      .then((res) => {
        if (!cancelled) setInvitations(res.invitations);
      })
      .catch((err) => {
        if (!cancelled)
          setError(
            err instanceof AdminApiError
              ? err.message
              : 'The invitations could not be loaded.',
          );
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [refresh, reload, status]);

  const revoke = useCallback(async () => {
    const target = revoking;
    setRevoking(null);
    if (!target) return;
    try {
      await adminPost<{ ok: boolean }>(`invitations/${target.id}/revoke`, {});
      toast(`Invitation for ${target.email} revoked.`);
      setReload((n) => n + 1);
      onChanged?.();
    } catch (err) {
      toast(
        err instanceof AdminApiError
          ? err.message
          : 'The invitation could not be revoked.',
        'error',
      );
    }
  }, [revoking, toast, onChanged]);

  const smallButton =
    'inline-flex items-center gap-1.5 rounded-lg border border-border bg-[var(--admin-control)] px-2.5 py-1.5 text-xs font-medium text-muted transition-colors duration-ts hover:bg-[var(--admin-control-hover)] hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-bg';

  const columns: AdminColumn<Invitation>[] = [
    {
      key: 'email',
      label: 'Email',
      render: (inv) => (
        <span className="min-w-0">
          <span className="block truncate font-medium text-ink">
            {inv.email}
          </span>
          {inv.name && (
            <span className="block truncate text-xs text-muted">{inv.name}</span>
          )}
        </span>
      ),
    },
    { key: 'role', label: 'Role', render: (inv) => <RoleChip role={inv.role} /> },
    {
      key: 'invited_by',
      label: 'Invited by',
      render: (inv) =>
        inv.invited_by || <span className="text-faint">—</span>,
    },
    {
      key: 'expires',
      label: 'Expires',
      render: (inv) =>
        inv.expires_at ? (
          formatWhen(inv.expires_at)
        ) : (
          <span className="text-faint">—</span>
        ),
    },
    {
      key: 'status',
      label: 'Status',
      render: (inv) => <StatusChip status={inviteStatusOf(inv)} />,
    },
    {
      key: 'actions',
      label: '',
      align: 'right',
      render: (inv) => {
        const status = inviteStatusOf(inv);
        if (status === 'pending') {
          return (
            <button
              type="button"
              onClick={() => setRevoking(inv)}
              className={`${smallButton} hover:text-danger`}
            >
              Revoke
            </button>
          );
        }
        if (status === 'expired' || status === 'revoked') {
          return (
            <button
              type="button"
              onClick={() =>
                setReinvite({ email: inv.email, name: inv.name, role: inv.role })
              }
              className={smallButton}
            >
              Re-invite
            </button>
          );
        }
        return <span className="text-faint">—</span>;
      },
    },
  ];

  return (
    <>
      <AdminTable
        columns={columns}
        rows={invitations}
        rowKey={(inv) => inv.id}
        loading={loading}
        empty={
          empty ??
          'No invitations yet. Invite someone and the one-time link appears here.'
        }
        error={error}
        onRetry={() => setReload((n) => n + 1)}
      />

      <ConfirmDialog
        open={revoking !== null}
        title="Revoke this invitation?"
        body={`The link sent to ${revoking?.email ?? ''} will stop working immediately.`}
        confirmLabel="Revoke"
        onConfirm={revoke}
        onCancel={() => setRevoking(null)}
      />

      <InviteDialog
        open={reinvite !== null}
        initial={reinvite ?? undefined}
        onClose={() => setReinvite(null)}
        onInvited={() => {
          setReload((n) => n + 1);
          onChanged?.();
        }}
      />
    </>
  );
}
