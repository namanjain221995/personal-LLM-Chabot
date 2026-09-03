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
import { IconPencil, IconTrash } from '@/components/icons';
import { formatDay, formatRelative, formatWhen } from '@/lib/format';
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
import { AccessDialog } from '@/components/admin/AccessDialog';
import { StatusChip } from '@/components/admin/chips';
import {
  IconBan,
  IconEye,
  IconKey,
  IconMonitor,
  IconSliders,
  IconUserPlus,
} from '@/components/admin/icons';
import {
  ADMIN_PRIMARY_BUTTON,
  AdminSearchInput,
  AdminSelect,
  AdminTabs,
  AdminToolbar,
} from '@/components/admin/controls';
import { MemberRoleControl } from '@/components/admin/MemberRoleControl';
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
  const [accessTarget, setAccessTarget] = useState<Member | null>(null);
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
      // Which TOOLS this person may use — the per-member override over the
      // workspace default set on /admin/access.
      items.push({
        id: 'access',
        label: 'Manage access',
        icon: <IconSliders size={15} />,
      });
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
      case 'access':
        setAccessTarget(member);
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

  const canManageRoles = can(me, 'roles.manage');

  const columns: AdminColumn<Member>[] = useMemo(
    () => [
      {
        key: 'user',
        label: 'Name',
        // No width: the identity column absorbs the slack, and its two
        // lines truncate rather than widening the row.
        render: (m) => (
          <div className="flex min-w-0 items-center gap-3">
            <AvatarInitial name={m.name} size="md" />
            <div className="min-w-0">
              <div className="truncate font-medium text-ink" title={m.name}>
                {m.name}
              </div>
              <div className="truncate text-xs text-muted" title={m.email}>
                {m.email}
              </div>
            </div>
          </div>
        ),
      },
      {
        key: 'role',
        label: 'Role',
        width: '160px',
        render: (m) => (
          <MemberRoleControl
            role={m.role}
            name={m.name}
            editable={canManageRoles}
            onEdit={() => setRoleTarget(m)}
          />
        ),
      },
      {
        key: 'status',
        label: 'Status',
        width: '120px',
        render: (m) => <StatusChip status={m.status} />,
      },
      {
        key: 'joined',
        label: 'Date added',
        width: '140px',
        render: (m) =>
          m.joined_at ? (
            // The day is what the column is for; the exact moment stays one
            // hover away rather than costing every row 20 characters.
            <span className="text-muted" title={formatWhen(m.joined_at)}>
              {formatDay(m.joined_at)}
            </span>
          ) : (
            <span className="text-faint">—</span>
          ),
      },
      {
        key: 'active',
        label: 'Last active',
        width: '160px',
        hideBelowLg: true,
        render: (m) =>
          m.last_active_at ? (
            <span className="text-muted" title={formatWhen(m.last_active_at)}>
              {formatRelative(m.last_active_at)}
            </span>
          ) : (
            <span className="text-faint">Never</span>
          ),
      },
      {
        key: 'actions',
        label: '',
        width: '56px',
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
    [me, canManageRoles],
  );

  const counts =
    data === null
      ? null
      : `${data.active_members.toLocaleString()} member${
          data.active_members === 1 ? '' : 's'
        } · ${data.pending_invites.toLocaleString()} pending invite${
          data.pending_invites === 1 ? '' : 's'
        }`;

  return (
    <div>
      <PageHeader
        title="Members"
        subtitle={
          <>
            {me.workspace.name}
            {counts && <span className="text-faint"> · {counts}</span>}
          </>
        }
      />

      <div className="mt-6">
        <AdminTabs
          label="Members tabs"
          active={tab}
          onChange={(id) => setTab(id as 'users' | 'invites')}
          tabs={[
            { id: 'users', label: 'Users' },
            {
              id: 'invites',
              label: 'Pending invites',
              count: data?.pending_invites,
            },
          ]}
        />
      </div>

      {tab === 'users' ? (
        <>
          <div className="mt-5">
            <AdminToolbar
              action={
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
            >
              <AdminSearchInput
                value={q}
                onChange={setQ}
                label="Search members"
                placeholder="Search by name or email…"
                className="w-full sm:w-72"
              />
              <AdminSelect
                value={roleFilter}
                onChange={setRoleFilter}
                label="Filter by role"
                options={[
                  { value: '', label: 'All roles' },
                  { value: 'super_admin', label: 'Super admin' },
                  { value: 'admin', label: 'Admin' },
                  { value: 'member', label: 'Member' },
                ]}
              />
              <AdminSelect
                value={statusFilter}
                onChange={setStatusFilter}
                label="Filter by status"
                options={[
                  { value: '', label: 'All statuses' },
                  { value: 'active', label: 'Active' },
                  { value: 'disabled', label: 'Disabled' },
                ]}
              />
            </AdminToolbar>
          </div>

          <div className="mt-5">
            <AdminTable
              columns={columns}
              // 636px of fixed columns + ~264px the names actually need.
              minWidth={900}
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
        <div className="mt-6">
          <InvitesPanel
            refresh={refresh}
            onChanged={bump}
            status="pending"
            empty="No invitations are waiting. Accepted, expired and revoked ones are on the Invitations page."
          />
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
      <AccessDialog
        member={accessTarget}
        onClose={() => setAccessTarget(null)}
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
