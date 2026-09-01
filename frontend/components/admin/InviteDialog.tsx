'use client';

/**
 * Invite a member. POST /api/admin/invitations answers with the ONE-TIME
 * accept link — the token exists nowhere else (only its hash is stored), so
 * the success view shows the full link with copy-to-clipboard immediately
 * and says plainly that it will not be shown again. No email is sent.
 */

import { useEffect, useState, type FormEvent } from 'react';
import { IconAlert } from '@/components/icons';
import { CopyButton } from '@/components/CopyButton';
import { Loader } from '@/components/Loader';
import { formatWhen } from '@/lib/format';
import {
  AdminApiError,
  ROLE_LABEL,
  adminPost,
  invitableRoles,
  type Role,
} from './api';
import { useAdminMe } from './AdminMeContext';
import {
  AdminDialog,
  FIELD_INPUT,
  Field,
  PRIMARY_BUTTON,
  SECONDARY_BUTTON,
} from './AdminDialog';

interface Created {
  email: string;
  expires_at: string | null;
  accept_path: string;
}

export interface InvitePrefill {
  email?: string;
  name?: string;
  role?: string;
}

export function InviteDialog({
  open,
  onClose,
  onInvited,
  initial,
}: {
  open: boolean;
  onClose: () => void;
  /** Called once an invitation was created, so lists can refresh. */
  onInvited?: () => void;
  /** Prefill for re-inviting from an expired or revoked invitation. */
  initial?: InvitePrefill;
}) {
  const me = useAdminMe();
  const roles = invitableRoles(me);

  const [name, setName] = useState('');
  const [email, setEmail] = useState('');
  const [role, setRole] = useState<Role>('member');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<Created | null>(null);

  // Each opening starts clean (or prefilled, for a re-invite).
  useEffect(() => {
    if (!open) return;
    setName(initial?.name ?? '');
    setEmail(initial?.email ?? '');
    setRole(
      initial?.role && roles.includes(initial.role as Role)
        ? (initial.role as Role)
        : 'member',
    );
    setBusy(false);
    setError(null);
    setCreated(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const res = await adminPost<Created>('invitations', {
        email: email.trim(),
        name: name.trim(),
        role,
      });
      setCreated(res);
      onInvited?.();
    } catch (err) {
      setError(
        err instanceof AdminApiError
          ? err.message
          : 'The invitation could not be created.',
      );
    } finally {
      setBusy(false);
    }
  }

  const acceptLink = created
    ? `${window.location.origin}${created.accept_path}`
    : '';

  return (
    <AdminDialog open={open} title="Invite member" onClose={onClose}>
      {created ? (
        <div>
          <p className="text-sm text-muted">
            Send this link to{' '}
            <span className="font-medium text-ink">{created.email}</span> — it
            lets them set a password and sign in.
          </p>
          <div className="mt-3 flex items-center gap-2 rounded-lg border border-border bg-bg px-3 py-2">
            <code className="min-w-0 flex-1 truncate font-mono text-xs text-ink">
              {acceptLink}
            </code>
            <CopyButton text={acceptLink} label="Copy link" />
          </div>
          <p className="mt-2 flex items-start gap-1.5 text-xs text-muted">
            <IconAlert size={13} className="mt-px shrink-0 text-warn" />
            This link is shown once and cannot be retrieved later — copy it
            now.
            {created.expires_at
              ? ` It expires ${formatWhen(created.expires_at)}.`
              : ''}
          </p>
          <div className="mt-4 flex justify-end">
            <button type="button" onClick={onClose} className={SECONDARY_BUTTON}>
              Done
            </button>
          </div>
        </div>
      ) : (
        <form onSubmit={submit} className="space-y-3">
          <Field label="Name (optional)">
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Ada Lovelace"
              autoComplete="off"
              className={FIELD_INPUT}
            />
          </Field>
          <Field label="Email">
            <input
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              type="email"
              required
              placeholder="person@company.com"
              autoComplete="off"
              className={FIELD_INPUT}
            />
          </Field>
          <Field label="Role">
            <select
              value={role}
              onChange={(e) => setRole(e.target.value as Role)}
              className={FIELD_INPUT}
            >
              {roles.map((r) => (
                <option key={r} value={r}>
                  {ROLE_LABEL[r]}
                </option>
              ))}
            </select>
          </Field>
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
              disabled={busy || !email.trim()}
              className={PRIMARY_BUTTON}
            >
              {busy && <Loader size={16} />}
              Create invite
            </button>
          </div>
        </form>
      )}
    </AdminDialog>
  );
}
