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
import { ADMIN_PRIMARY_BUTTON, CONTROL_HEIGHT } from '@/components/admin/controls';
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
              className={ADMIN_PRIMARY_BUTTON}
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
        // max-w-full + overflow: five filters are 347px, wider than a 360px
        // phone's content column, and the group pushed <main> sideways.
        // p-0.5 on touch: inside the 40px strip, p-1 left each filter 30px
        // tall, under the 32px tap floor (re-audit 2026-09-13); 2px of inset
        // makes them 34px without changing the strip's height.
        className={`${CONTROL_HEIGHT} mt-6 flex w-fit max-w-full items-center overflow-x-auto rounded-ts border border-border bg-[var(--admin-control)] p-1 max-sm:p-0.5 [@media(pointer:coarse)]:p-0.5`}
      >
        {FILTERS.map(([value, label]) => (
          <button
            key={label}
            type="button"
            aria-pressed={status === value}
            onClick={() => setStatus(value)}
            className={`h-full shrink-0 rounded-md px-2.5 text-xs font-medium sm:px-3 transition-colors duration-ts focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent ${
              status === value
                ? 'bg-surface-2 text-ink'
                : 'text-muted hover:text-ink'
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      <div className="mt-5">
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
