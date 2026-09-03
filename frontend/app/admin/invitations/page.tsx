'use client';

/**
 * /admin/invitations — the invitations list as its own destination (the same
 * panel also lives behind the Members page's second tab). Creating an invite
 * shows the ONE-TIME accept link immediately.
 *
 * THIS page keeps the whole history and filters it; the Members tab, titled
 * "Pending invites", asks the server for pending only. One list that
 * silently meant "everything ever sent" is what made that tab show nine
 * rows reading "Accepted" (owner report, 2026-09-03).
 */

import { useState } from 'react';
import { useAdminMe } from '@/components/admin/AdminMeContext';
import { can, type InviteStatus } from '@/components/admin/api';
import { PRIMARY_BUTTON } from '@/components/admin/AdminDialog';
import { InviteDialog } from '@/components/admin/InviteDialog';
import { InvitesPanel } from '@/components/admin/InvitesPanel';
import { IconUserPlus } from '@/components/admin/icons';
import { PageHeader } from '@/components/admin/ui';

export default function AdminInvitationsPage() {
  const me = useAdminMe();
  const [inviteOpen, setInviteOpen] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [status, setStatus] = useState<'' | InviteStatus>('');

  const FILTERS: ['' | InviteStatus, string][] = [
    ['', 'All'],
    ['pending', 'Pending'],
    ['accepted', 'Accepted'],
    ['expired', 'Expired'],
    ['revoked', 'Revoked'],
  ];

  return (
    <div>
      <PageHeader
        title="Invitations"
        subtitle={me.workspace.name}
        actions={
          can(me, 'invites.manage') ? (
            <button
              type="button"
              onClick={() => setInviteOpen(true)}
              className={PRIMARY_BUTTON}
            >
              <IconUserPlus size={15} />
              Invite member
            </button>
          ) : undefined
        }
      />

      <div
        role="group"
        aria-label="Filter invitations"
        className="mt-5 flex w-fit items-center rounded-full border border-border bg-surface p-0.5"
      >
        {FILTERS.map(([value, label]) => (
          <button
            key={label}
            type="button"
            aria-pressed={status === value}
            onClick={() => setStatus(value)}
            className={`rounded-full px-3 py-1 text-xs font-medium transition-colors duration-ts ${
              status === value
                ? 'bg-surface-2 text-ink'
                : 'text-muted hover:bg-surface-2 hover:text-ink'
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      <div className="mt-3">
        <InvitesPanel
          refresh={refresh}
          status={status}
          empty={
            status
              ? `No ${status} invitations.`
              : 'No invitations yet. Invite someone and the one-time link appears here.'
          }
        />
      </div>

      <InviteDialog
        open={inviteOpen}
        onClose={() => setInviteOpen(false)}
        onInvited={() => setRefresh((n) => n + 1)}
      />
    </div>
  );
}
