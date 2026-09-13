'use client';

/**
 * Mint an API key.
 *
 * THE SHOW-ONCE FLOW IS INVITEDIALOG'S, COPIED DELIBERATELY. That dialog has
 * shown a one-time accept link since the auth retrofit: POST answers with the
 * plaintext, the dialog swaps to a success view, the value sits in a bordered
 * code block beside a CopyButton, and an IconAlert line says in plain English
 * that it will not be shown again. Every one of those pieces is here, in the
 * same order, for the same reason — a person who has used one of these dialogs
 * has used both.
 *
 * WHAT IS NOT HERE, AND NEVER WILL BE: a way to see the key later. CONTRACT §5
 * stores `HMAC-SHA256(pepper, secret)` and nothing else, so there is no "show
 * key" the server could answer even if the console asked — and a UI control
 * that implied otherwise would be a promise the database cannot keep. The
 * secret lives in this component's state until the dialog closes, is never
 * written to localStorage, never put in a URL, and never sent anywhere.
 *
 * The `last_four` on the list afterwards is for RECOGNITION, not recovery: it
 * is how someone tells which of four keys they are about to revoke.
 */

import { useEffect, useRef, useState, type FormEvent } from 'react';
import { CopyButton } from '@/components/CopyButton';
import { Loader } from '@/components/Loader';
import { IconAlert } from '@/components/icons';
import {
  AdminDialog,
  FIELD_INPUT,
  Field,
  PRIMARY_BUTTON,
  SECONDARY_BUTTON,
} from '@/components/admin/AdminDialog';
import { consolePost, messageOf } from './api';
import { consolePaths } from './paths';
import { SCOPES, type CreatedKey, type Project } from './types';

/**
 * What a new key gets when nobody chooses — `scopes.DEFAULT_SCOPES` upstream,
 * spelled the same way here so the ticked boxes match what the server would
 * have picked. Usage is deliberately NOT in it: a credential that can read the
 * quota has no business being able to spend it, and the restricted key is the
 * default path rather than the one you have to remember to ask for.
 */
const DEFAULT_SCOPES = ['models.read', 'responses.read', 'responses.write'];

export function CreateKeyDialog({
  open,
  project,
  onClose,
  onCreated,
}: {
  open: boolean;
  /** Which project the key belongs to. A key cannot exist without one. */
  project: Project | null;
  onClose: () => void;
  /** Fired once the key is minted, so the list behind the dialog refreshes. */
  onCreated: () => void;
}) {
  const [name, setName] = useState('');
  const [scopes, setScopes] = useState<string[]>(DEFAULT_SCOPES);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<CreatedKey | null>(null);

  // Read by an in-flight submit: a key minted after the dialog was dismissed
  // must not be put back into state that nobody can see.
  const openRef = useRef(open);
  openRef.current = open;

  // Each CLOSING drops the secret, and each opening starts clean.
  //
  // FIXED 2026-09-13. This effect used to return early when `open` went false,
  // so the plaintext key stayed in React state for the life of the Keys panel
  // (this component is always mounted; AdminDialog merely renders nothing)
  // and was only cleared on the NEXT open. The comment promised the closing
  // edge; the code did the opening edge. CONTRACT §5: never store the secret
  // after creation — a live component's state is storage.
  useEffect(() => {
    setCreated(null);
    if (!open) return;
    setName('');
    setScopes(DEFAULT_SCOPES);
    setBusy(false);
    setError(null);
  }, [open]);

  function toggleScope(id: string) {
    setScopes((prev) =>
      prev.includes(id) ? prev.filter((s) => s !== id) : [...prev, id],
    );
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (busy || !project) return;
    setBusy(true);
    setError(null);
    try {
      const res = await consolePost<CreatedKey>(consolePaths.keys(project.id), {
        name: name.trim(),
        scopes,
      });
      if (openRef.current) setCreated(res);
      onCreated();
    } catch (err) {
      setError(messageOf(err, 'The key could not be created.'));
    } finally {
      setBusy(false);
    }
  }

  return (
    <AdminDialog
      open={open}
      title={created ? 'Copy your API key' : 'Create API key'}
      size="md"
      onClose={onClose}
    >
      {created ? (
        <div>
          <p className="text-sm text-muted">
            <span className="font-medium text-ink">{created.key.name}</span> is
            ready for{' '}
            <span className="font-medium text-ink">{project?.name}</span>. Put it
            in an environment variable and send it as{' '}
            <code className="font-mono text-xs">Authorization: Bearer …</code>.
          </p>
          <div className="mt-3 flex items-center gap-2 rounded-lg border border-border bg-bg px-3 py-2">
            <code
              data-testid="created-key-secret"
              className="min-w-0 flex-1 break-all font-mono text-xs text-ink"
            >
              {created.secret}
            </code>
            <CopyButton text={created.secret} label="Copy key" />
          </div>
          <p className="mt-2 flex items-start gap-1.5 text-xs text-muted">
            <IconAlert size={13} className="mt-px shrink-0 text-warn" />
            This key is shown once and cannot be retrieved later — copy it now.
            Only a hash of it is stored, so nobody, including an administrator,
            can read it back.
          </p>
          <div className="mt-4 flex justify-end">
            <button type="button" onClick={onClose} className={SECONDARY_BUTTON}>
              Done
            </button>
          </div>
        </div>
      ) : (
        <form onSubmit={submit} className="space-y-3">
          <Field label="Name">
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              required
              placeholder="Production server"
              autoComplete="off"
              className={FIELD_INPUT}
            />
          </Field>
          <p className="text-xs text-faint">
            Project: <span className="text-muted">{project?.name ?? '—'}</span>
          </p>

          <fieldset className="rounded-lg border border-border p-3">
            <legend className="px-1 text-xs font-medium text-muted">
              Scopes
            </legend>
            <div className="space-y-2">
              {SCOPES.map((scope) => (
                <label
                  key={scope.id}
                  className="flex cursor-pointer items-start gap-2.5 text-sm"
                >
                  <input
                    type="checkbox"
                    checked={scopes.includes(scope.id)}
                    onChange={() => toggleScope(scope.id)}
                    className="mt-0.5 h-4 w-4 shrink-0 accent-[var(--ts-accent-strong)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                  />
                  <span className="min-w-0">
                    <span className="block text-ink">{scope.label}</span>
                    <span className="block text-xs text-faint">{scope.hint}</span>
                  </span>
                </label>
              ))}
            </div>
          </fieldset>

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
              disabled={busy || !name.trim() || scopes.length === 0 || !project}
              className={PRIMARY_BUTTON}
            >
              {busy && <Loader size={16} />}
              Create key
            </button>
          </div>
        </form>
      )}
    </AdminDialog>
  );
}
