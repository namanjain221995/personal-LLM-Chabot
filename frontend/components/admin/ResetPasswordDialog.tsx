'use client';

/**
 * Set a temporary password for a locked-out member. The orchestrator revokes
 * their sessions on success; weak passwords come back as a 422 {detail}
 * (min 10 chars) and render inline.
 */

import { useEffect, useState, type FormEvent } from 'react';
import { IconAlert, IconCheck } from '@/components/icons';
import { Loader } from '@/components/Loader';
import { AdminApiError, adminPost } from './api';
import {
  AdminDialog,
  FIELD_INPUT,
  Field,
  PRIMARY_BUTTON,
  SECONDARY_BUTTON,
} from './AdminDialog';

export function ResetPasswordDialog({
  member,
  open,
  onClose,
}: {
  member: { id: number; name: string } | null;
  open: boolean;
  onClose: () => void;
}) {
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  useEffect(() => {
    if (!open) return;
    setPassword('');
    setBusy(false);
    setError(null);
    setDone(false);
  }, [open]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (busy || !member) return;
    setBusy(true);
    setError(null);
    try {
      await adminPost<{ ok: boolean }>(`members/${member.id}/reset-password`, {
        new_password: password,
      });
      setDone(true);
    } catch (err) {
      setError(
        err instanceof AdminApiError
          ? err.message
          : 'The password could not be reset.',
      );
    } finally {
      setBusy(false);
    }
  }

  if (!member) return null;

  return (
    <AdminDialog
      open={open}
      title={`Reset password — ${member.name}`}
      onClose={onClose}
    >
      {done ? (
        <div>
          <p className="flex items-start gap-1.5 text-sm text-muted">
            <IconCheck size={15} className="mt-0.5 shrink-0 text-accent" />
            Password reset. Their sessions were signed out — share the
            temporary password securely and ask them to change it after
            signing in.
          </p>
          <div className="mt-4 flex justify-end">
            <button type="button" onClick={onClose} className={SECONDARY_BUTTON}>
              Done
            </button>
          </div>
        </div>
      ) : (
        <form onSubmit={submit} className="space-y-3">
          <p className="text-sm text-muted">
            Sets a temporary password and signs them out everywhere.
          </p>
          <Field label="Temporary password">
            <input
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              type="text"
              required
              minLength={10}
              placeholder="At least 10 characters"
              autoComplete="off"
              className={`${FIELD_INPUT} font-mono`}
            />
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
              disabled={busy || password.length < 10}
              className={PRIMARY_BUTTON}
            >
              {busy && <Loader size={16} />}
              Reset password
            </button>
          </div>
        </form>
      )}
    </AdminDialog>
  );
}
