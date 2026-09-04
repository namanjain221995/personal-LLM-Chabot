'use client';

/**
 * Share this conversation.
 *
 * Two states in one dialog, because they are two halves of one question:
 * before a link exists it asks what to publish, and after it exists it shows
 * what IS published and offers the four things an owner ever wants — copy it,
 * bring it up to date, change when it dies, or stop it.
 *
 * THE MODAL NEVER DECIDES ANYTHING. Every refusal shown here was computed
 * server-side and arrives as a sentence; the dialog renders it. A conversation
 * that draws on Salesforce is not blocked by this file noticing, it is blocked
 * by the server refusing, and this file explains why. That distinction is the
 * whole reason a hidden button is not a security control.
 *
 * The dialog follows SearchPalette rather than ConfirmDialog: it is the only
 * other surface here with real controls inside it, and it is the one that
 * already traps focus (lib/focusTrap.ts). A dialog you can Tab out of into the
 * chat behind it is not modal in any sense a keyboard user experiences.
 */

import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { createPortal } from 'react-dom';

import { focusTrapNext, focusableWithin } from '@/lib/focusTrap';
import {
  EXPIRY_LABEL,
  ShareError,
  copyToClipboard,
  createShare,
  deriveExpiryChoice,
  expiryLabel,
  getShareStatus,
  refreshShare,
  revokeShare,
  updateShare,
  type ShareStatus,
  type ShareVisibility,
} from '@/lib/share';
import { IconAlert, IconCheck, IconExternal, IconLink, IconX } from './icons';
import { useToast } from './Providers';

const PANEL =
  'palette-panel relative flex max-h-[85dvh] w-full max-w-[520px] flex-col ' +
  'overflow-hidden rounded-ts border border-border bg-surface shadow-2xl';

const FIELD =
  'h-10 w-full rounded-lg border border-border bg-bg px-3 text-sm text-ink ' +
  'transition-colors duration-ts focus-visible:outline-none focus-visible:ring-2 ' +
  'focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-surface';

const PRIMARY =
  'inline-flex h-10 items-center justify-center gap-2 rounded-lg bg-accent-strong ' +
  'px-4 text-sm font-medium text-white transition-colors duration-ts ' +
  'hover:brightness-110 focus-visible:outline-none focus-visible:ring-2 ' +
  'focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-surface ' +
  'disabled:cursor-not-allowed disabled:opacity-50';

const SECONDARY =
  'inline-flex h-10 items-center justify-center gap-2 rounded-lg border border-border ' +
  'px-3 text-sm text-ink transition-colors duration-ts hover:bg-surface-2 ' +
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent ' +
  'focus-visible:ring-offset-2 focus-visible:ring-offset-surface ' +
  'disabled:cursor-not-allowed disabled:opacity-50';

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1.5 block text-xs font-medium text-muted">{label}</span>
      {children}
    </label>
  );
}

export function ShareDialog({
  conversationId,
  title,
  onClose,
}: {
  conversationId: string;
  title: string;
  onClose: () => void;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  const headingId = useId();
  const { toast } = useToast();

  const [status, setStatus] = useState<ShareStatus | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [visibility, setVisibility] = useState<ShareVisibility>('public');
  const [expiry, setExpiry] = useState('30d');
  /** The expiry shown for a link that ALREADY exists — see deriveExpiryChoice. */
  const [liveExpiry, setLiveExpiry] = useState('30d');
  const [showName, setShowName] = useState(false);
  /** The full link, with its secret. Held in memory only, never re-fetched. */
  const [fullUrl, setFullUrl] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [manualCopy, setManualCopy] = useState(false);
  const [confirmStop, setConfirmStop] = useState(false);
  /** Announced to a screen reader; the visual equivalent is the toast. */
  const [live, setLive] = useState('');

  const load = useCallback(async () => {
    try {
      const next = await getShareStatus(conversationId);
      setStatus(next);
      setLoadError(null);
      if (next.share) {
        setVisibility(next.share.visibility);
        setShowName(next.share.show_owner_name);
        setLiveExpiry(
          deriveExpiryChoice(next.share.expires_at, next.expiry_choices),
        );
      } else {
        setExpiry(next.default_expiry);
        // Offer the narrower option first when the wider one is refused.
        setVisibility(next.policy.public_allowed ? 'public' : 'workspace');
      }
    } catch (err) {
      setLoadError(
        err instanceof ShareError ? err.message : 'This could not be loaded.',
      );
    }
  }, [conversationId]);

  useEffect(() => {
    void load();
  }, [load]);

  // Focus the panel on open, and put it back where it came from on close.
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const first = focusableWithin(panelRef.current)[0];
    (first ?? panelRef.current)?.focus();
    return () => previous?.focus?.();
  }, []);

  const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.key === 'Escape') {
      e.stopPropagation();
      onClose();
      return;
    }
    // Tab must not reach the conversation behind the dialog.
    if (e.key !== 'Tab') return;
    const next = focusTrapNext(
      focusableWithin(panelRef.current),
      document.activeElement,
      e.shiftKey,
    );
    if (next) {
      e.preventDefault();
      next.focus();
    }
  };

  async function run<T>(
    action: () => Promise<T>,
    done: (v: T) => void,
    label: string,
    /** Put an optimistic control back where it was. See the expiry select. */
    onFail?: () => void,
  ) {
    setBusy(true);
    try {
      done(await action());
      setLive(label);
      toast(label, 'info');
    } catch (err) {
      onFail?.();
      const message =
        err instanceof ShareError ? err.message : 'That did not work. Try again.';
      setLive(message);
      toast(message, 'error');
    } finally {
      setBusy(false);
    }
  }

  const share = status?.share ?? null;
  const policy = status?.policy;
  /** What the menu offers, plus this link's own state if policy dropped it. */
  const expiryOptions = (() => {
    const offered = status?.expiry_choices ?? [];
    return offered.includes(liveExpiry) ? offered : [...offered, liveExpiry];
  })();
  const linkToShow = fullUrl ?? share?.url ?? '';
  const blocked =
    policy && (visibility === 'public' ? !policy.public_allowed : !policy.workspace_allowed);

  async function onCopy() {
    const ok = await copyToClipboard(linkToShow);
    if (ok) {
      setCopied(true);
      setLive('Link copied');
      toast('Link copied', 'info');
      window.setTimeout(() => setCopied(false), 2000);
    } else {
      setManualCopy(true);
      setLive('Copy the link manually — the browser refused clipboard access.');
    }
  }

  const body = (
    <div
      className="fixed inset-0 z-[70] flex items-start justify-center bg-black/60 p-4 pt-[10vh] backdrop-blur-sm"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={headingId}
        tabIndex={-1}
        onKeyDown={onKeyDown}
        className={PANEL}
      >
        <div className="flex items-start justify-between gap-3 border-b border-border px-5 py-4">
          <div className="min-w-0">
            <h2 id={headingId} className="text-sm font-semibold text-ink">
              Share this conversation
            </h2>
            <p className="mt-0.5 truncate text-xs text-faint">{title}</p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="-mr-1 rounded-lg p-1.5 text-faint transition-colors duration-ts hover:bg-surface-2 hover:text-ink"
          >
            <IconX size={16} />
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          <p aria-live="polite" className="sr-only">
            {live}
          </p>

          {loadError && (
            <p className="rounded-lg border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-ink">
              {loadError}
            </p>
          )}

          {!status && !loadError && (
            <div className="space-y-3" aria-busy="true">
              <div className="h-10 animate-pulse rounded-lg bg-surface-2" />
              <div className="h-10 animate-pulse rounded-lg bg-surface-2" />
            </div>
          )}

          {status && !share && (
            <div className="space-y-4">
              <p className="text-sm text-muted">
                Creates a read-only copy of this conversation as it is right now.
              </p>

              <Row label="Who can open it">
                <select
                  value={visibility}
                  onChange={(e) => setVisibility(e.target.value as ShareVisibility)}
                  className={FIELD}
                >
                  <option value="public" disabled={!policy?.public_allowed}>
                    Anyone with the link
                  </option>
                  <option value="workspace" disabled={!policy?.workspace_allowed}>
                    People in this workspace
                  </option>
                </select>
              </Row>

              <Row label="Expires">
                <select
                  value={expiry}
                  onChange={(e) => setExpiry(e.target.value)}
                  className={FIELD}
                >
                  {status.expiry_choices.map((c) => (
                    <option key={c} value={c}>
                      {EXPIRY_LABEL[c] ?? c}
                    </option>
                  ))}
                </select>
              </Row>

              {/* Shown whenever the server refused ANYTHING, not only when it
                  refused the option currently selected. `load` pre-selects
                  the narrower reach when public is unavailable, so keying
                  this off `blocked` meant the one case that most needs an
                  explanation — public refused, workspace fine — silently
                  offered a disabled option and said nothing about why. */}
              {policy && policy.blocking_reasons.length > 0 && (
                <div className="flex gap-2 rounded-lg border border-warn/40 bg-warn/10 px-3 py-2.5">
                  <IconAlert size={15} className="mt-0.5 shrink-0 text-warn" />
                  <div className="min-w-0 text-xs leading-relaxed text-ink">
                    {policy.blocking_reasons.map((r) => (
                      <p key={r}>{r}</p>
                    ))}
                    {!policy.public_allowed && policy.workspace_allowed && (
                      <p className="mt-1 text-muted">
                        You can still share it inside this workspace.
                      </p>
                    )}
                  </div>
                </div>
              )}

              {policy?.warnings.map((w) => (
                <p key={w} className="text-xs text-muted">
                  {w}
                </p>
              ))}

              <ul className="space-y-1 border-t border-border pt-3 text-xs text-faint">
                <li>Your email address is never shown.</li>
                <li>Messages you send later are not added automatically.</li>
                <li>Anyone holding the link can pass it on.</li>
              </ul>

              <label className="flex items-center gap-2 text-xs text-muted">
                <input
                  type="checkbox"
                  checked={showName}
                  onChange={(e) => setShowName(e.target.checked)}
                  className="h-4 w-4 rounded border-border bg-bg accent-[var(--ts-accent-strong)]"
                />
                Show my display name on the page
              </label>
            </div>
          )}

          {status && share && (
            <div className="space-y-4">
              <div>
                <span className="mb-1.5 block text-xs font-medium text-muted">
                  Link
                </span>
                <div className="flex gap-2">
                  <input
                    readOnly
                    value={linkToShow}
                    onFocus={(e) => e.currentTarget.select()}
                    aria-label="Share link"
                    className={`${FIELD} font-mono text-xs`}
                  />
                  <button type="button" onClick={onCopy} className={SECONDARY}>
                    {copied ? <IconCheck size={15} /> : <IconLink size={15} />}
                    {copied ? 'Copied' : 'Copy'}
                  </button>
                </div>
                {!fullUrl && (
                  <p className="mt-1.5 text-xs text-faint">
                    The full link was shown once, when it was created. If you no
                    longer have it, stop sharing and create a new one.
                  </p>
                )}
                {manualCopy && (
                  <p className="mt-1.5 text-xs text-warn">
                    Your browser refused clipboard access — select the text above
                    and copy it.
                  </p>
                )}
              </div>

              <dl className="grid grid-cols-2 gap-x-4 gap-y-2 border-t border-border pt-3 text-xs">
                <div>
                  <dt className="text-faint">Visible to</dt>
                  <dd className="mt-0.5 text-ink">
                    {share.visibility === 'public'
                      ? 'Anyone with the link'
                      : 'This workspace'}
                  </dd>
                </div>
                <div>
                  <dt className="text-faint">
                    <label htmlFor={`${headingId}-expiry`}>Expires</label>
                  </dt>
                  {/* Editable, not a readout: the commonest thing an owner
                      wants after sharing is to cut it shorter, and making
                      them revoke and re-send a new link to do that is how a
                      long-lived link stays live. Saved server-side, which
                      re-checks the workspace ceiling. */}
                  <dd className="mt-0.5">
                    <select
                      id={`${headingId}-expiry`}
                      value={liveExpiry}
                      disabled={busy}
                      onChange={(e) => {
                        // Optimistic, but REVERTED on refusal. Without the
                        // revert a rejected change left the control showing
                        // the value the server had just refused, directly
                        // above a caption still reporting the real one —
                        // `run`'s success callback is what reloads, and on
                        // failure it never fires. The select is disabled
                        // while busy, so `previous` cannot go stale.
                        const previous = liveExpiry;
                        const next = e.target.value;
                        setLiveExpiry(next);
                        void run(
                          () => updateShare(conversationId, { expiry: next }),
                          () => void load(),
                          next === 'never'
                            ? 'This link no longer expires'
                            : `Link now expires in ${
                                EXPIRY_LABEL[next]?.toLowerCase() ?? next
                              }`,
                          () => setLiveExpiry(previous),
                        );
                      }}
                      className="h-7 w-full rounded-md border border-border bg-bg px-1.5 text-xs text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50"
                    >
                      {/* The link's CURRENT state is always in the list,
                          even when policy has since withdrawn it — otherwise
                          a never-expiring link under a tightened policy would
                          render as "24 hours" (a React select falls back to
                          its first option) while the caption below said
                          "Never expires". It is disabled, so it can be read
                          but not re-chosen; the server would refuse anyway. */}
                      {expiryOptions.map((c) => (
                        <option
                          key={c}
                          value={c}
                          disabled={!status.expiry_choices.includes(c)}
                        >
                          {EXPIRY_LABEL[c] ?? c}
                        </option>
                      ))}
                    </select>
                    <span className="mt-1 block text-faint">
                      {expiryLabel(share.expires_at) ?? 'Never expires'}
                    </span>
                  </dd>
                </div>
                <div>
                  <dt className="text-faint">Shows</dt>
                  <dd className="mt-0.5 text-ink">
                    {share.message_count} message
                    {share.message_count === 1 ? '' : 's'}
                  </dd>
                </div>
                <div>
                  <dt className="text-faint">Opened</dt>
                  <dd className="mt-0.5 text-ink">
                    {share.view_count} time{share.view_count === 1 ? '' : 's'}
                  </dd>
                </div>
              </dl>

              {status.unshared_messages > 0 && (
                <div className="rounded-lg border border-border bg-bg px-3 py-2.5">
                  <p className="text-xs text-ink">
                    {status.unshared_messages} newer message
                    {status.unshared_messages === 1 ? ' has' : 's have'} not been
                    added to this link.
                  </p>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() =>
                      void run(
                        () => refreshShare(conversationId),
                        () => void load(),
                        'Shared link updated',
                      )
                    }
                    className={`${SECONDARY} mt-2 h-8`}
                  >
                    Update the link
                  </button>
                </div>
              )}
            </div>
          )}
        </div>

        <div className="flex items-center justify-between gap-2 border-t border-border px-5 py-3">
          {share ? (
            <>
              {confirmStop ? (
                <div className="flex items-center gap-2">
                  <span className="text-xs text-muted">Stop sharing?</span>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() =>
                      void run(
                        () => revokeShare(conversationId),
                        () => {
                          setFullUrl(null);
                          setConfirmStop(false);
                          void load();
                        },
                        'Sharing stopped',
                      )
                    }
                    className="h-8 rounded-lg bg-danger px-3 text-xs font-medium text-white transition-colors hover:brightness-110"
                  >
                    Stop sharing
                  </button>
                  <button
                    type="button"
                    onClick={() => setConfirmStop(false)}
                    className="h-8 rounded-lg px-2 text-xs text-muted hover:text-ink"
                  >
                    Keep it
                  </button>
                </div>
              ) : (
                <button
                  type="button"
                  onClick={() => setConfirmStop(true)}
                  className="h-8 rounded-lg px-2 text-xs text-danger transition-colors hover:bg-danger/10"
                >
                  Stop sharing
                </button>
              )}
              <a
                href={linkToShow}
                target="_blank"
                rel="noopener noreferrer"
                className={SECONDARY}
              >
                <IconExternal size={14} />
                Preview
              </a>
            </>
          ) : (
            <>
              <button type="button" onClick={onClose} className={SECONDARY}>
                Cancel
              </button>
              <button
                type="button"
                disabled={busy || !status || blocked}
                onClick={() =>
                  void run(
                    () =>
                      createShare(conversationId, {
                        visibility,
                        expiry,
                        show_owner_name: showName,
                      }),
                    (created) => {
                      setFullUrl(created.url);
                      void load();
                    },
                    'Link created',
                  )
                }
                className={PRIMARY}
              >
                Create link
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  );

  if (typeof document === 'undefined') return null;
  return createPortal(body, document.body);
}
