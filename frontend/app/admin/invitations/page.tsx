'use client';

/**
 * /admin/invitations — the invitations list as its own destination (the same
 * panel also lives behind the Members page's second tab). Creating an invite
 * shows the ONE-TIME accept link immediately.
 */

import { useState } from 'react';
import { useAdminMe } from '@/components/admin/AdminMeContext';
import { can } from '@/components/admin/api';
import { PRIMARY_BUTTON } from '@/components/admin/AdminDialog';
import { InviteDialog } from '@/components/admin/InviteDialog';
import { InvitesPanel } from '@/components/admin/InvitesPanel';
import { IconUserPlus } from '@/components/admin/icons';
import { PageHeader } from '@/components/admin/ui';

export default function AdminInvitationsPage() {
  const me = useAdminMe();
  const [inviteOpen, setInviteOpen] = useState(false);
  const [refresh, setRefresh] = useState(0);

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

      <div className="mt-5">
        <InvitesPanel refresh={refresh} />
      </div>

      <InviteDialog
        open={inviteOpen}
        onClose={() => setInviteOpen(false)}
        onInvited={() => setRefresh((n) => n + 1)}
      />
    </div>
  );
}
