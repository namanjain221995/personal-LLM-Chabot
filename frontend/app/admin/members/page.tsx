'use client';

/**
 * /admin/members — the workspace roster. Users tab: debounced search,
 * role/status filters, offset pagination, and a per-row action menu whose
 * items follow ME_PAYLOAD.capabilities (role changes need roles.manage,
 * management needs members.manage, session revocation sessions.manage).
 * Pending invites tab: the shared InvitesPanel. The orchestrator's 409
 * refusals — last super admin, self-deactivation — surface as error toasts,
 * never silently.
 */

import { useEffect, useMemo, useState } from 'react';
import { useRouter } from 'next/navigation';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { useToast } from '@/components/Providers';
import {
  IconPencil,
  IconSearch,
  IconTrash,
} from '@/components/icons';
import { formatWhen } from '@/lib/format';
import { useAdminMe } from '@/components/admin/AdminMeContext';
import {
  AdminApiError,
  adminJson,
  adminPost,
  can,
} from '@/components/admin/api';
import { AdminTable, Pagination, type AdminColumn } from '@/components/admin/AdminTable';
import { ChangeRoleDialog } from '@/components/admin/ChangeRoleDialog';
import { InviteDialog } from '@/components/admin/InviteDialog';
import { InvitesPanel } from '@/components/admin/InvitesPanel';
import { ResetPasswordDialog } from '@/components/admin/ResetPasswordDialog';
import { RowMenu, type RowMenuItem } from '@/components/admin/RowMenu';
import { RoleChip, StatusChip } from '@/components/admin/chips';
import {
  IconBan,
  IconEye,
  IconKey,
  IconMonitor,
  IconUserPlus,
} from '@/components/admin/icons';
import { PRIMARY_BUTTON } from '@/components/admin/AdminDialog';
import { AvatarInitial, PageHeader } from '@/components/admin/ui';
import { useDebounced } from '@/components/admin/useDebounced';

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

interface MembersResponse {
  members: Member[];
  total: number;
  active_members: number;
  pending_invites: number;
}

const FIELD =
  'rounded-lg border border-border bg-bg px-2.5 py-1.5 text-sm text-ink focus:border-accent/60 focus:outline-none';

export default function AdminMembersPage() {
  const me = useAdminMe();
  const router = useRouter();
  const { toast } = useToast();

  const [tab, setTab] = useState<'users' | 'invites'>('users');
  const [q, setQ] = useState('');
  const [roleFilter, setRoleFilter] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<MembersResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);

  const [inviteOpen, setInviteOpen] = useState(false);
  const [roleTarget, setRoleTarget] = useState<Member | null>(null);
  const [resetTarget, setResetTarget] = useState<Member | null>(null);
  const [sessionsTarget, setSessionsTarget] = useState<Member | null>(null);
  const [deactivateTarget, setDeactivateTarget] = useState<Member | null>(null);
  const [removeTarget, setRemoveTarget] = useState<Member | null>(null);

  const debouncedQ = useDebounced(q, 300);
  const bump = () => setRefresh((n) => n + 1);

  // A new search or filter starts back at page one.
  useEffect(() => {
    setOffset(0);
  }, [debouncedQ, roleFilter, statusFilter]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    const params = new URLSearchParams({
      q: debouncedQ.trim(),
      role: roleFilter,
      status: statusFilter,
      limit: String(LIMIT),
      offset: String(offset),
    });
    adminJson<MembersResponse>(`members?${params.toString()}`)
      .then((res) => {
        if (!cancelled) setData(res);
      })
      .catch((err) => {
        if (!cancelled)
          setError(
            err instanceof AdminApiError
              ? err.message
              : 'The members could not be loaded.',
          );
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [debouncedQ, roleFilter, statusFilter, offset, refresh]);

  async function setStatus(member: Member, disabled: boolean) {
    try {
      await adminPost<{ ok: boolean }>(`members/${member.id}/status`, {
        disabled,
      });
      toast(disabled ? `${member.name} deactivated.` : `${member.name} reactivated.`);
      bump();
    } catch (err) {
      toast(
        err instanceof AdminApiError
          ? err.message
          : 'The status could not be changed.',
        'error',
      );
    }
  }

  async function removeMember(member: Member) {
    try {
      await adminJson<{ ok: boolean }>(`members/${member.id}`, {
        method: 'DELETE',
      });
      toast(`${member.name} removed from the workspace.`);
      bump();
    } catch (err) {
      toast(
        err instanceof AdminApiError
          ? err.message
          : 'The member could not be removed.',
        'error',
      );
    }
  }

  async function revokeSessions(member: Member) {
    try {
      const res = await adminPost<{ revoked: number }>(
        `members/${member.id}/sessions/revoke`,
        {},
      );
      toast(
        `Revoked ${res.revoked} session${res.revoked === 1 ? '' : 's'} for ${member.name}.`,
      );
    } catch (err) {
      toast(
        err instanceof AdminApiError
          ? err.message
          : 'The sessions could not be revoked.',
        'error',
      );
    }
  }

  function menuItemsFor(member: Member): RowMenuItem[] {
    const items: RowMenuItem[] = [
      { id: 'view', label: 'View', icon: <IconEye size={15} /> },
    ];
    if (can(me, 'roles.manage')) {
      items.push({
        id: 'role',
        label: 'Change role',
        icon: <IconPencil size={15} />,
      });
    }
    if (can(me, 'members.manage')) {
      items.push({
        id: 'reset',
        label: 'Reset password',
        icon: <IconKey size={15} />,
      });
    }
    if (can(me, 'sessions.manage')) {
      items.push({
        id: 'sessions',
        label: 'Revoke sessions',
        icon: <IconMonitor size={15} />,
      });
    }
    if (can(me, 'members.manage')) {
      const disabled = member.status === 'disabled';
      items.push({
        id: 'status',
        label: disabled ? 'Reactivate' : 'Deactivate',
        icon: <IconBan size={15} />,
        danger: !disabled,
      });
      items.push({
        id: 'remove',
        label: 'Remove',
        icon: <IconTrash size={15} />,
        danger: true,
      });
    }
    return items;
  }

  function onMenuSelect(member: Member, action: string) {
    switch (action) {
      case 'view':
        router.push(`/admin/members/${member.id}`);
        break;
      case 'role':
        setRoleTarget(member);
        break;
      case 'reset':
        setResetTarget(member);
        break;
      case 'sessions':
        setSessionsTarget(member);
        break;
      case 'status':
        if (member.status === 'disabled') void setStatus(member, false);
        else setDeactivateTarget(member);
        break;
      case 'remove':
        setRemoveTarget(member);
        break;
    }
  }

  const columns: AdminColumn<Member>[] = useMemo(
    () => [
      {
        key: 'user',
        label: 'User',
        render: (m) => (
          <span className="flex items-center gap-2.5">
            <AvatarInitial name={m.name} />
            <span className="min-w-0">
              <span className="block max-w-56 truncate font-medium text-ink">
                {m.name}
              </span>
              <span className="block max-w-56 truncate text-xs text-muted">
                {m.email}
              </span>
            </span>
          </span>
        ),
      },
      { key: 'role', label: 'Role', render: (m) => <RoleChip role={m.role} /> },
      {
        key: 'status',
        label: 'Status',
        render: (m) => <StatusChip status={m.status} />,
      },
      {
        key: 'joined',
        label: 'Date added',
        render: (m) =>
          m.joined_at ? formatWhen(m.joined_at) : <span className="text-faint">—</span>,
      },
      {
        key: 'active',
        label: 'Last active',
        render: (m) =>
          m.last_active_at ? (
            formatWhen(m.last_active_at)
          ) : (
            <span className="text-faint">—</span>
          ),
      },
      {
        key: 'actions',
        label: '',
        align: 'right',
        render: (m) => (
          <RowMenu
            label={`Actions for ${m.name}`}
            items={menuItemsFor(m)}
            onSelect={(action) => onMenuSelect(m, action)}
          />
        ),
      },
    ],
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [me],
  );

  const counts =
    data === null
      ? null
      : `${data.active_members.toLocaleString()} member${
          data.active_members === 1 ? '' : 's'
        } · ${data.pending_invites.toLocaleString()} pending invite${
          data.pending_invites === 1 ? '' : 's'
        }`;

  const tabClass = (active: boolean) =>
    `-mb-px border-b-2 px-3 py-2 text-sm font-medium transition-colors duration-ts ${
      active
        ? 'border-accent text-ink'
        : 'border-transparent text-muted hover:text-ink'
    }`;

  return (
    <div>
      <PageHeader
        title="Members"
        subtitle={
          <>
            {me.workspace.name}
            {counts && <span className="text-faint"> — {counts}</span>}
          </>
        }
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

      <div role="tablist" aria-label="Members tabs" className="mt-5 flex gap-1 border-b border-border">
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'users'}
          onClick={() => setTab('users')}
          className={tabClass(tab === 'users')}
        >
          Users
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'invites'}
          onClick={() => setTab('invites')}
          className={tabClass(tab === 'invites')}
        >
          Pending invites
          {data !== null && data.pending_invites > 0 && (
            <span className="ml-1.5 rounded-full bg-surface-2 px-1.5 py-px text-[11px] text-muted">
              {data.pending_invites}
            </span>
          )}
        </button>
      </div>

      {tab === 'users' ? (
        <>
          <div className="mt-4 flex flex-wrap items-center gap-2">
            <div className="flex min-w-0 flex-1 items-center gap-2 rounded-lg border border-border bg-bg px-2.5 transition-colors duration-ts focus-within:border-accent/60 sm:max-w-xs">
              <IconSearch size={14} className="shrink-0 text-faint" />
              <input
                value={q}
                onChange={(e) => setQ(e.target.value)}
                placeholder="Search by name or email…"
                aria-label="Search members"
                className="min-w-0 flex-1 bg-transparent py-1.5 text-sm text-ink placeholder:text-faint focus:outline-none"
              />
            </div>
            <select
              value={roleFilter}
              onChange={(e) => setRoleFilter(e.target.value)}
              aria-label="Filter by role"
              className={FIELD}
            >
              <option value="">All roles</option>
              <option value="super_admin">Super admin</option>
              <option value="admin">Admin</option>
              <option value="member">Member</option>
            </select>
            <select
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value)}
              aria-label="Filter by status"
              className={FIELD}
            >
              <option value="">All statuses</option>
              <option value="active">Active</option>
              <option value="disabled">Disabled</option>
            </select>
          </div>

          <div className="mt-4">
            <AdminTable
              columns={columns}
              rows={data?.members ?? []}
              rowKey={(m) => m.id}
              onRowClick={(m) => router.push(`/admin/members/${m.id}`)}
              loading={loading && data === null}
              empty={
                debouncedQ || roleFilter || statusFilter
                  ? 'No members match these filters.'
                  : 'No members yet.'
              }
              error={error}
              onRetry={bump}
            />
            {data !== null && (
              <Pagination
                total={data.total}
                offset={offset}
                limit={LIMIT}
                onOffset={setOffset}
              />
            )}
          </div>
        </>
      ) : (
        <div className="mt-4">
          <InvitesPanel refresh={refresh} onChanged={bump} />
        </div>
      )}

      <InviteDialog
        open={inviteOpen}
        onClose={() => setInviteOpen(false)}
        onInvited={bump}
      />
      <ChangeRoleDialog
        member={roleTarget}
        open={roleTarget !== null}
        onClose={() => setRoleTarget(null)}
        onChanged={() => {
          toast('Role updated.');
          bump();
        }}
      />
      <ResetPasswordDialog
        member={resetTarget}
        open={resetTarget !== null}
        onClose={() => setResetTarget(null)}
      />
      <ConfirmDialog
        open={sessionsTarget !== null}
        title={`Revoke all sessions for ${sessionsTarget?.name ?? ''}?`}
        body="They will be signed out everywhere and have to sign in again."
        confirmLabel="Revoke sessions"
        onConfirm={() => {
          const target = sessionsTarget;
          setSessionsTarget(null);
          if (target) void revokeSessions(target);
        }}
        onCancel={() => setSessionsTarget(null)}
      />
      <ConfirmDialog
        open={deactivateTarget !== null}
        title={`Deactivate ${deactivateTarget?.name ?? ''}?`}
        body="They are signed out immediately and cannot sign in until reactivated. Their data is kept."
        confirmLabel="Deactivate"
        onConfirm={() => {
          const target = deactivateTarget;
          setDeactivateTarget(null);
          if (target) void setStatus(target, true);
        }}
        onCancel={() => setDeactivateTarget(null)}
      />
      <ConfirmDialog
        open={removeTarget !== null}
        title={`Remove ${removeTarget?.name ?? ''} from the workspace?`}
        body="Their membership is removed, the account is disabled and every session is revoked. Their data is kept."
        confirmLabel="Remove"
        onConfirm={() => {
          const target = removeTarget;
          setRemoveTarget(null);
          if (target) void removeMember(target);
        }}
        onCancel={() => setRemoveTarget(null)}
      />
    </div>
  );
}
