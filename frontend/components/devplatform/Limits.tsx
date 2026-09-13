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
import { ConsoleEmpty, ProjectSelect, limitText, useProjects } from './shared';
import { useConsole } from './useConsole';
import { useConsoleStatus } from './status';
import type { ProjectLimits } from './types';

type LimitKey = keyof ProjectLimits;

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
): Partial<Record<LimitKey, number>> {
  const body: Partial<Record<LimitKey, number>> = {};
  for (const { key, label, min } of FIELDS) {
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

  const limits = useConsole<{ limits: ProjectLimits; can_manage: boolean }>(
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
      body = limitsChanges(saved, draft);
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
        description="Ceilings for this project. They are enforced in the orchestrator on every call, counted in PostgreSQL, and they survive a restart. An empty field inherits the platform default; 0 allows nothing."
      />

      <AdminToolbar>
        <ProjectSelect
          projects={projects}
          value={selected?.id ?? ''}
          onChange={setProjectId}
        />
      </AdminToolbar>

      <Section title="Rate and quota" first>
        {(error || limits.error) && (
          <div className="mb-4">
            <ErrorPanel message={(error ?? limits.error) as string} />
          </div>
        )}
        <form onSubmit={save} className="max-w-xl space-y-4">
          {FIELDS.map((field) => (
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
            Raising a ceiling raises what this project can take from the shared
            inference lanes. The chat application draws on the same engine.
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
