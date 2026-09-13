'use client';

/**
 * What this deployment's API is, in one place.
 *
 * There is nothing to change here, and that is deliberate: the base URL, the
 * key format and the authentication rule are properties of the platform, not
 * preferences. What the panel is FOR is the question a developer asks first —
 * "what do I point my client at, and what exactly does it send?" — answered
 * without making them read the documentation to find one line.
 *
 * The capability list at the bottom is the honest account of what THIS account
 * may do, read from ME_PAYLOAD rather than inferred from the role, because
 * that is what the orchestrator will check on every call.
 */

import { useEffect, useState } from 'react';
import { CopyButton } from '@/components/CopyButton';
import Link from 'next/link';
import { ConsoleHeader } from '@/components/admin/analytics/filters';
import { Section } from '@/components/admin/analytics/ui';
import { ADMIN_SECONDARY_BUTTON } from '@/components/admin/controls';
import { ROLE_LABEL, type Me } from '@/components/admin/api';
import { KEY_ENV_VAR } from './snippets';

/** The capabilities this console cares about, in the order CONTRACT §6 lists. */
const CONSOLE_CAPS: { id: string; label: string }[] = [
  { id: 'api.console.access', label: 'Open the developer console' },
  { id: 'api.projects.read', label: 'Read projects and keys' },
  { id: 'api.projects.manage', label: 'Create and change projects' },
  { id: 'api.keys.create', label: 'Create API keys' },
  { id: 'api.keys.revoke', label: 'Revoke API keys' },
  { id: 'api.usage.read', label: 'Read usage' },
  { id: 'api.logs.read', label: 'Read request logs' },
  { id: 'api.webhooks.manage', label: 'Manage webhooks' },
  { id: 'api.models.manage', label: 'Publish or withdraw models (super admin)' },
  { id: 'api.limits.manage', label: 'Change platform limits (super admin)' },
];

export function SettingsPanel({ me }: { me: Me }) {
  // window is not there during the server render, so the origin is filled in
  // after mount rather than guessed — a wrong base URL is worse than a late one.
  const [origin, setOrigin] = useState('');
  useEffect(() => setOrigin(window.location.origin), []);
  const base = origin ? `${origin}/v1` : '/v1';

  return (
    <div>
      <ConsoleHeader
        title="Settings"
        description="Where the API lives and what this account may do with it."
      />

      <Section title="Endpoint" first>
        <div className="flex max-w-xl items-center gap-2 rounded-lg border border-border bg-bg px-3 py-2">
          <code className="min-w-0 flex-1 break-all font-mono text-xs text-ink">
            {base}
          </code>
          <CopyButton text={base} label="Copy base URL" />
        </div>
        <dl className="mt-4 max-w-xl space-y-2 text-sm">
          <Row
            label="Authentication"
            value="Authorization: Bearer tsk_live_… — the API reads the header and nothing else. It never accepts this browser session."
          />
          <Row
            label="Key format"
            value="tsk_live_… for production and tsk_test_… for test, so a key's environment is readable at a glance."
          />
          <Row
            label="Where to keep a key"
            value={`An environment variable — the snippets read $${KEY_ENV_VAR}. A key is shown once, at creation, and cannot be recovered afterwards.`}
          />
          <Row
            label="Request id"
            value="Every response carries X-Request-Id. Quote it when asking about a call; it is the line in the request log."
          />
        </dl>
        <div className="mt-4">
          <Link href="/docs" className={ADMIN_SECONDARY_BUTTON}>
            Read the documentation
          </Link>
        </div>
      </Section>

      <Section title="This account">
        <p className="text-sm text-muted">
          {me.user.name} · {ROLE_LABEL[me.workspace.role] ?? me.workspace.role} ·{' '}
          {me.workspace.name}
        </p>
        <ul className="mt-3 max-w-xl space-y-1.5">
          {CONSOLE_CAPS.map((cap) => {
            const held = me.capabilities.includes(cap.id);
            return (
              <li key={cap.id} className="flex items-baseline gap-2 text-sm">
                {/* The word carries the state; the mark is decoration. */}
                <span aria-hidden className={held ? 'text-ok' : 'text-faint'}>
                  {held ? '✓' : '·'}
                </span>
                <span className={held ? 'text-ink' : 'text-faint'}>
                  {cap.label}
                </span>
                <span className="sr-only">
                  {held ? ' — granted' : ' — not granted'}
                </span>
              </li>
            );
          })}
        </ul>
      </Section>
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="border-b border-[var(--admin-separator)] pb-2">
      <dt className="text-xs text-faint">{label}</dt>
      <dd className="mt-0.5 text-sm leading-relaxed text-muted">{value}</dd>
    </div>
  );
}
