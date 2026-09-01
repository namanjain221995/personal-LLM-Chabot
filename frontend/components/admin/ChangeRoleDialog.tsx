'use client';

/**
 * Change a member's role. The 409s here are the important part: the
 * orchestrator refuses to demote or disable the last active super admin, and
 * that refusal must be READ, not swallowed — the detail sentence renders
 * inline in the dialog.
 */

import { useEffect, useState, type FormEvent } from 'react';
import { IconAlert } from '@/components/icons';
import { Loader } from '@/components/Loader';
import {
  AdminApiError,
  ROLE_LABEL,
  adminPost,
  assignableRoles,
  type Role,
} from './api';
import { useAdminMe } from './AdminMeContext';
import {
  AdminDialog,
  PRIMARY_BUTTON,
  SECONDARY_BUTTON,
} from './AdminDialog';

export function ChangeRoleDialog({
  member,
  open,
  onClose,
  onChanged,
}: {
  member: { id: number; name: string; role: string } | null;
  open: boolean;
  onClose: () => void;
  onChanged: (role: Role) => void;
}) {
  const me = useAdminMe();
  const roles = assignableRoles(me);

  const [role, setRole] = useState<Role>('member');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open || !member) return;
    setRole(
      roles.includes(member.role as Role) ? (member.role as Role) : 'member',
    );
    setBusy(false);
    setError(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, member?.id]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (busy || !member) return;
    setBusy(true);
    setError(null);
    try {
      const res = await adminPost<{ ok: boolean; role: Role }>(
        `members/${member.id}/role`,
        { role },
      );
      onChanged(res.role);
      onClose();
    } catch (err) {
      setError(
        err instanceof AdminApiError
          ? err.message
          : 'The role could not be changed.',
      );
    } finally {
      setBusy(false);
    }
  }

  if (!member) return null;

  return (
    <AdminDialog
      open={open}
      title={`Change role — ${member.name}`}
      onClose={onClose}
    >
      <form onSubmit={submit}>
        <div role="radiogroup" aria-label="Role" className="space-y-1">
          {roles.map((r) => (
            <label
              key={r}
              className={`flex cursor-pointer items-center gap-2.5 rounded-lg border px-3 py-2 text-sm transition-colors duration-ts ${
                role === r
                  ? 'border-accent/60 bg-accent/10 text-ink'
                  : 'border-border text-muted hover:bg-surface-2 hover:text-ink'
              }`}
            >
              <input
                type="radio"
                name="role"
                value={r}
                checked={role === r}
                onChange={() => setRole(r)}
                className="accent-[var(--ts-accent)]"
              />
              {ROLE_LABEL[r]}
            </label>
          ))}
        </div>
        {error && (
          <p
            role="alert"
            className="mt-3 flex items-start gap-1.5 text-sm text-danger"
          >
            <IconAlert size={15} className="mt-0.5 shrink-0" />
            {error}
          </p>
        )}
        <div className="mt-4 flex justify-end gap-2">
          <button type="button" onClick={onClose} className={SECONDARY_BUTTON}>
            Cancel
          </button>
          <button
            type="submit"
            disabled={busy || role === member.role}
            className={PRIMARY_BUTTON}
          >
            {busy && <Loader size={16} />}
            Save role
          </button>
        </div>
      </form>
    </AdminDialog>
  );
}
