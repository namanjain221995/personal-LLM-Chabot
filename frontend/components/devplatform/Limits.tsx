'use client';

/**
 * Platform limits for one project.
 *
 * `api.limits.manage` is a SUPER ADMIN capability and nothing else in the
 * console is — CONTRACT §6 says plainly why: "an admin may run projects, not
 * decide which models exist publicly or lift a workspace's ceiling." So this
 * panel is reached only by an account that holds it, and the orchestrator
 * refuses the PUT regardless of what is drawn here.
 *
 * NULL INHERITS, ZERO MEANS ZERO (the shared limits rule, 2026-09-13). The
 * router answers `GET projects/{id}/limits` with each ceiling or null, and a
 * null is "this project has no value of its own — the platform default
 * applies". It is shown as an empty field with the default as its hint, never
 * as a 0: a 0 is a real limit that allows nothing, and the previous form typed
 * `Number('')` — zero — into any field a person cleared.
 *
 * WHAT A SAVE SENDS. Only the fields a person changed. `set_limits` merges
 * with `exclude_none`, so an untouched field must be absent rather than
 * resent, and an emptied field cannot be sent as "go back to inheriting" —
 * the router has no such operation, and the form says so instead of
 * pretending a blank was saved. The server owns the maximum of each field;
 * the one check here is that a value is a whole number at all.
 *
 * UNLIMITED BY DEFAULT (owner decision, 2026-09-13). The public API has no
 * request, token-per-minute, daily-quota or concurrency limit unless the
 * operator sets PUBLIC_API_ENFORCE_LIMITS=true. When the server says the
 * limits are not enforced, those five rows read Unlimited and have NO input:
 * a field that saves a number nothing enforces is a control that does
 * nothing, and a stored number drawn beside it would advertise a ceiling that
 * does not exist. The two PER-REQUEST ceilings (max input and output tokens)
 * are technical safety limits the owner kept, so they stay editable in both
 * modes. A pre-switch orchestrator sends no flag and did enforce, so a
 * missing flag keeps the enforced form (`limitsEnforcement`).
 */

import { useEffect, useState, type FormEvent } from 'react';
import { Loader } from '@/components/Loader';
import { useToast } from '@/components/Providers';
import { IconAlert } from '@/components/icons';
import { ErrorPanel } from '@/components/admin/ui';
import { FIELD_INPUT, Field, PRIMARY_BUTTON } from '@/components/admin/AdminDialog';
import { AdminToolbar } from '@/components/admin/controls';
import { ConsoleHeader } from '@/components/admin/analytics/filters';
import { Section } from '@/components/admin/analytics/ui';
import { consolePut, messageOf } from './api';
import { consolePaths } from './paths';
import {
  ConsoleEmpty,
  ProjectSelect,
  limitText,
  limitsEnforcement,
  limitsUnlimited,
  useProjects,
} from './shared';
import { useConsole } from './useConsole';
import { useConsoleStatus } from './status';
import type { ProjectLimits } from './types';

/** A ceiling's field name — every key but the enforcement flag. */
type LimitKey = Exclude<keyof ProjectLimits, 'enforced'>;

/** One row of the form: CONTRACT §12's name, default and the smallest value. */
const FIELDS: { key: LimitKey; label: string; hint: string; min: number }[] = [
  { key: 'rpm', label: 'Requests per minute', hint: 'Platform default 60. A sliding window, counted durably.', min: 0 },
  { key: 'input_tpm', label: 'Input tokens per minute', hint: 'Platform default 200,000.', min: 0 },
  { key: 'output_tpm', label: 'Output tokens per minute', hint: 'Platform default 60,000.', min: 0 },
  { key: 'max_concurrency', label: 'Concurrent requests', hint: 'Platform default 4. The quota gate sits in FRONT of the shared admission lanes so one key cannot starve the chat app.', min: 0 },
  { key: 'daily_token_quota', label: 'Daily token quota', hint: 'Platform default 2,000,000, reset daily and surviving a restart.', min: 0 },
  { key: 'max_input_tokens', label: 'Max input tokens per request', hint: "Platform default: the model's ceiling.", min: 1 },
  { key: 'max_output_tokens', label: 'Max output tokens per request', hint: 'Platform default 8,192, never above the model ceiling.', min: 1 },
];

/**
 * The usage limits PUBLIC_API_ENFORCE_LIMITS switches off (2026-09-13). The
 * rest of FIELDS — the per-request token ceilings — are safety limits that
 * apply in both modes.
 */
export const USAGE_LIMIT_KEYS: readonly LimitKey[] = [
  'rpm',
  'input_tpm',
  'output_tpm',
  'max_concurrency',
  'daily_token_quota',
];

type Draft = Record<LimitKey, string>;

function draftOf(limits: ProjectLimits): Draft {
  const out = {} as Draft;
  for (const { key } of FIELDS) {
    const value = limits[key];
    out[key] = value === null ? '' : String(value);
  }
  return out;
}

/**
 * The PUT body: the changed fields, as numbers. Throws a sentence when a
 * field is not a whole number at or above its minimum, or when a stored value
 * was emptied (which the router cannot express).
 */
export function limitsChanges(
  saved: ProjectLimits,
  draft: Draft,
  unlimited = false,
): Partial<Record<LimitKey, number>> {
  const body: Partial<Record<LimitKey, number>> = {};
  for (const { key, label, min } of FIELDS) {
    // Not enforced, so not offered and never sent (2026-09-13): a PUT of a
    // usage limit while the switch is off would be a save that does nothing.
    if (unlimited && USAGE_LIMIT_KEYS.includes(key)) continue;
    const text = draft[key].trim();
    if (text === '') {
      if (saved[key] !== null) {
        throw new Error(
          `${label} cannot be emptied here — enter the value it should have.`,
        );
      }
      continue;
    }
    if (!/^\d+$/.test(text) || Number(text) < min) {
      throw new Error(`${label} must be a whole number of at least ${min}.`);
    }
    const value = Number(text);
    if (value !== saved[key]) body[key] = value;
  }
  return body;
}

export function LimitsPanel() {
  const projectsQuery = useProjects();
  const projects = projectsQuery.data?.projects ?? [];
  const [projectId, setProjectId] = useState('');
  const selected = projects.find((p) => p.id === projectId) ?? projects[0] ?? null;

  const limits = useConsole<{
    limits: ProjectLimits;
    can_manage: boolean;
    enforced?: boolean;
    limits_enforced?: boolean;
  }>(
    consolePaths.limits(selected?.id ?? ''),
    {},
    selected !== null,
  );
  const { toast } = useToast();
  const { announce } = useConsoleStatus();
  const [draft, setDraft] = useState<Draft | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const saved = limits.data?.limits ?? null;
  // Unknown (no answer yet, or a pre-switch server) is NOT unlimited: the
  // form stays in its enforced shape until the server says otherwise.
  const unlimited = limitsUnlimited(limitsEnforcement(limits.data));
  const fields = unlimited
    ? FIELDS.filter((field) => !USAGE_LIMIT_KEYS.includes(field.key))
    : FIELDS;

  // Only the server's answer populates the form. There is no fallback to a
  // guessed value while it loads: an empty form that is disabled is honest, a
  // form pre-filled with the wrong project's numbers is not.
  useEffect(() => {
    setDraft(saved ? draftOf(saved) : null);
    setError(null);
  }, [saved]);

  useEffect(() => {
    if (limits.loading) announce('Loading limits.');
    else if (limits.error) announce(limits.error);
  }, [limits.loading, limits.error, announce]);

  async function save(e: FormEvent) {
    e.preventDefault();
    if (!selected || !draft || !saved || busy) return;
    let body: Partial<Record<LimitKey, number>>;
    try {
      body = limitsChanges(saved, draft, unlimited);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Check the values.');
      return;
    }
    if (Object.keys(body).length === 0) {
      setError('Nothing has changed.');
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await consolePut(consolePaths.limits(selected.id), body);
      toast(`Limits saved for ${selected.name}.`);
      announce('Limits saved.');
      limits.reload();
    } catch (err) {
      const message = messageOf(err, 'The limits could not be saved.');
      setError(message);
      toast(message, 'error');
    } finally {
      setBusy(false);
    }
  }

  if (!projectsQuery.loading && !projectsQuery.error && projects.length === 0) {
    return (
      <div>
        <ConsoleHeader title="Limits" />
        <ConsoleEmpty
          title="No projects to limit"
          body="Limits belong to a project. Create one on the Projects tab and its ceilings appear here."
        />
      </div>
    );
  }

  return (
    <div>
      <ConsoleHeader
        title="Limits"
        description={
          unlimited
            ? 'The public API is unlimited: no request, token-per-minute, daily quota or concurrency limit is applied to /v1 or the playground. Usage is still recorded. The per-request token ceilings below still apply.'
            : 'Ceilings for this project. They are enforced in the orchestrator on every call, counted in PostgreSQL, and they survive a restart. An empty field inherits the platform default; 0 allows nothing.'
        }
      />

      <AdminToolbar>
        <ProjectSelect
          projects={projects}
          value={selected?.id ?? ''}
          onChange={setProjectId}
        />
      </AdminToolbar>

      {unlimited && (
        <Section title="Usage limits" first>
          <dl className="max-w-xl space-y-2 text-sm">
            {FIELDS.filter((field) => USAGE_LIMIT_KEYS.includes(field.key)).map(
              (field) => (
                <div
                  key={field.key}
                  className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 border-b border-[var(--admin-separator)] pb-2"
                >
                  <dt className="text-xs text-faint">{field.label}</dt>
                  <dd className="text-right text-sm text-ink">Unlimited</dd>
                </div>
              ),
            )}
          </dl>
          <p className="mt-3 max-w-xl text-xs text-muted">
            Not enforced, so there is nothing to set. An operator turns these
            limits on with PUBLIC_API_ENFORCE_LIMITS=true; the fields to set
            them appear here when they are.
          </p>
        </Section>
      )}

      <Section title={unlimited ? 'Per-request ceilings' : 'Rate and quota'} first={!unlimited}>
        {(error || limits.error) && (
          <div className="mb-4">
            <ErrorPanel message={(error ?? limits.error) as string} />
          </div>
        )}
        <form onSubmit={save} className="max-w-xl space-y-4">
          {fields.map((field) => (
            <div key={field.key}>
              <Field label={field.label}>
                <input
                  type="number"
                  min={field.min}
                  step={1}
                  inputMode="numeric"
                  value={draft ? draft[field.key] : ''}
                  placeholder={draft ? 'Platform default' : ''}
                  disabled={draft === null}
                  onChange={(e) =>
                    setDraft((prev) =>
                      prev ? { ...prev, [field.key]: e.target.value } : prev,
                    )
                  }
                  className={FIELD_INPUT}
                />
              </Field>
              <p className="mt-1 text-xs text-faint">
                {field.hint}
                {saved && ` Saved: ${limitText(saved[field.key])}.`}
              </p>
            </div>
          ))}
          <p className="flex items-start gap-1.5 text-xs text-muted">
            <IconAlert size={13} className="mt-px shrink-0 text-warn" />
            {unlimited
              ? 'Requests still wait in the engine’s shared admission queue, which the chat application draws on too.'
              : 'Raising a ceiling raises what this project can take from the shared inference lanes. The chat application draws on the same engine.'}
          </p>
          <div className="flex justify-end">
            <button
              type="submit"
              disabled={busy || draft === null}
              className={PRIMARY_BUTTON}
            >
              {busy && <Loader size={16} />}
              Save limits
            </button>
          </div>
        </form>
      </Section>
    </div>
  );
}
