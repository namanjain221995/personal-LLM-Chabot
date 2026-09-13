'use client';

/**
 * The console's landing section.
 *
 * It answers the four questions someone opening /api actually has: do I have a
 * project, do I have a key, which models can I call, and is anything using the
 * API yet. Nothing more — an overview that tries to be every other section at
 * once is a section nobody reads.
 *
 * NO INVENTED NUMBERS. Every figure is one `GET /overview` sends
 * (`console_api._overview_stats`): project and key counts, the published
 * model count, and today's requests from the usage ledger. Nothing is
 * derived, extrapolated or defaulted in here — while the answer is loading a
 * figure is "…", and when the call failed it is "—", never 0.
 */

import Link from 'next/link';
import { useEffect } from 'react';
import { ErrorPanel } from '@/components/admin/ui';
import { ConsoleHeader } from '@/components/admin/analytics/filters';
import { Section, Stat, StatRow } from '@/components/admin/analytics/ui';
import { compact } from '@/components/admin/analytics/format';
import { ADMIN_SECONDARY_BUTTON } from '@/components/admin/controls';
import type { Me } from '@/components/admin/api';
import { tabHref } from './nav';
import { ConsoleEmpty } from './shared';
import { useConsole } from './useConsole';
import { consolePaths } from './paths';
import { useConsoleStatus } from './status';
import type { ConsoleOverview } from './types';

export function OverviewPanel({ me }: { me: Me }) {
  const { data, loading, error, reload } = useConsole<ConsoleOverview>(
    consolePaths.overview(),
  );
  const { announce } = useConsoleStatus();

  useEffect(() => {
    if (loading) announce('Loading the developer platform overview.');
    else if (error) announce(error);
    else if (data) announce('Developer platform overview loaded.');
  }, [loading, error, data, announce]);

  const stats = data?.stats;
  const figure = (value: number | undefined) =>
    value === undefined ? (loading ? '…' : '—') : compact(value);

  return (
    <div>
      <ConsoleHeader
        title="Developer platform"
        description={`Projects, keys and traffic for ${me.workspace.name}. The public API answers at /v1 and reads an API key only — never this session.`}
      />

      {error && (
        <div className="mb-4">
          <ErrorPanel message={error} onRetry={reload} />
        </div>
      )}

      <Section title="At a glance" first>
        <StatRow columns={4}>
          <Stat
            label="Projects"
            value={figure(stats?.projects)}
            sub={stats ? `${compact(stats.active_projects)} active` : undefined}
          />
          <Stat label="Active keys" value={figure(stats?.active_keys)} />
          <Stat label="Models available" value={figure(stats?.models)} />
          <Stat
            label="Requests today"
            value={figure(stats?.today.requests)}
            sub={
              stats ? `${compact(stats.today.errors)} errors · UTC day` : undefined
            }
          />
        </StatRow>
      </Section>

      <Section title="Getting started">
        {stats && stats.active_keys === 0 ? (
          <ConsoleEmpty
            title="No API key yet"
            body="Create a project, mint a key for it, then call POST /v1/responses with that key in an Authorization header. Usage and request logs fill in from the first call onwards — nothing is estimated before then."
            action={
              <div className="flex flex-wrap items-center justify-center gap-2">
                <Link href={tabHref('projects')} className={ADMIN_SECONDARY_BUTTON}>
                  Create a project
                </Link>
                <Link href={tabHref('playground')} className={ADMIN_SECONDARY_BUTTON}>
                  Open the playground
                </Link>
                <Link href="/docs" className={ADMIN_SECONDARY_BUTTON}>
                  Read the documentation
                </Link>
              </div>
            }
          />
        ) : (
          <div className="flex flex-wrap gap-2">
            <Link href={tabHref('keys')} className={ADMIN_SECONDARY_BUTTON}>
              Manage API keys
            </Link>
            <Link href={tabHref('usage')} className={ADMIN_SECONDARY_BUTTON}>
              See usage
            </Link>
            <Link href={tabHref('logs')} className={ADMIN_SECONDARY_BUTTON}>
              Request logs
            </Link>
            <Link href="/docs" className={ADMIN_SECONDARY_BUTTON}>
              Documentation
            </Link>
          </div>
        )}
      </Section>
    </div>
  );
}
