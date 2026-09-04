'use client';

/**
 * The public read-only conversation.
 *
 * WHAT THIS RENDERS IS ALL THERE IS. The payload arrived sanitised — the
 * server built it from an allowlist when the owner published — so this
 * component is not a filter and must never become one. It maps role, text and
 * public citations, and there is nothing else in the object to leak.
 *
 * It renders through the SAME `Markdown` component the private chat uses,
 * deliberately. A second renderer for untrusted text would be a second set of
 * sanitisation rules to keep in step, and the one that got less attention
 * would be this one. `Markdown` passes no raw HTML through to the DOM, so a
 * `<script>` in a message is text on the page.
 *
 * Every failure — malformed token, unknown link, revoked, expired, a
 * workspace link opened by a stranger — renders the SAME thing. Telling them
 * apart would tell a stranger which links exist.
 */

import { useEffect, useState } from 'react';
import Link from 'next/link';

import { Markdown } from './Markdown';
import { TechSaraMark } from './TechSaraMark';
import { IconExternal } from './icons';
import { getPublicShare, type PublicShare } from '@/lib/share';

function Unavailable() {
  return (
    <main className="flex min-h-dvh flex-col items-center justify-center bg-bg px-6 text-center">
      <TechSaraMark size={40} />
      <h1 className="mt-5 text-lg font-semibold text-ink">
        This link is not available
      </h1>
      <p className="mt-2 max-w-sm text-sm text-muted">
        It may have been turned off by the person who shared it, or it may have
        expired.
      </p>
      <Link
        href="/"
        className="mt-6 inline-flex h-10 items-center rounded-lg border border-border px-4 text-sm text-ink transition-colors duration-ts hover:bg-surface-2"
      >
        Go to TechSara
      </Link>
    </main>
  );
}

function Loading() {
  return (
    <main className="min-h-dvh bg-bg" aria-busy="true">
      <div className="mx-auto w-full max-w-3xl px-4 py-16">
        <div className="h-6 w-2/5 animate-pulse rounded bg-surface-2" />
        <div className="mt-8 space-y-4">
          <div className="h-20 animate-pulse rounded-ts bg-surface-2" />
          <div className="h-32 animate-pulse rounded-ts bg-surface-2" />
        </div>
      </div>
    </main>
  );
}

export function SharedConversation({ token }: { token: string }) {
  const [data, setData] = useState<PublicShare | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let live = true;
    getPublicShare(token)
      .then((body) => {
        if (live) setData(body);
      })
      // One outcome for every failure — see the file comment.
      .catch(() => {
        if (live) setFailed(true);
      });
    return () => {
      live = false;
    };
  }, [token]);

  if (failed) return <Unavailable />;
  if (!data) return <Loading />;

  const { snapshot } = data;
  const sharedOn = new Date(snapshot.shared_at).toLocaleDateString(undefined, {
    year: 'numeric',
    month: 'long',
    day: 'numeric',
  });

  return (
    <main className="min-h-dvh bg-bg text-ink">
      <header className="sticky top-0 z-10 border-b border-border bg-bg/90 backdrop-blur">
        <div className="mx-auto flex h-[52px] w-full max-w-3xl items-center gap-2.5 px-4">
          <TechSaraMark size={22} />
          <span className="text-sm font-semibold">TechSara</span>
          <span className="ml-auto rounded-md border border-border px-2 py-0.5 text-[11px] text-faint">
            Shared conversation
          </span>
        </div>
      </header>

      <div className="mx-auto w-full max-w-3xl px-4 py-8">
        <h1 className="text-xl font-semibold tracking-tight">{snapshot.title}</h1>
        <p className="mt-1 text-xs text-faint">
          Read-only snapshot · shared on {sharedOn}
          {snapshot.owner_name ? ` by ${snapshot.owner_name}` : ''}
        </p>

        {snapshot.truncated && (
          <p className="mt-4 rounded-lg border border-border bg-surface px-3 py-2 text-xs text-muted">
            This conversation was too long to include in full. The most recent
            part is shown.
          </p>
        )}

        <div className="mt-8 space-y-7">
          {snapshot.messages.map((m, i) => (
            <article
              key={i}
              className={
                m.role === 'user'
                  ? 'ml-auto max-w-[85%] rounded-ts bg-bubble px-4 py-3'
                  : ''
              }
            >
              <h2 className="sr-only">
                {m.role === 'user' ? 'Question' : 'Answer'}
              </h2>
              {m.role === 'user' ? (
                <p className="whitespace-pre-wrap break-words text-[15px] leading-relaxed">
                  {m.content}
                </p>
              ) : (
                <Markdown text={m.content} />
              )}

              {m.sources && m.sources.length > 0 && (
                <div className="mt-3 border-t border-border pt-3">
                  <h3 className="mb-1.5 text-[11px] font-medium uppercase tracking-wide text-faint">
                    Sources
                  </h3>
                  <ul className="space-y-1">
                    {m.sources.map((s) => (
                      <li key={`${s.n}-${s.url}`} className="flex gap-2 text-xs">
                        <span className="shrink-0 text-faint">[{s.n}]</span>
                        <a
                          href={s.url}
                          target="_blank"
                          // noreferrer as well as noopener: the URL of a shared
                          // conversation must not travel to the sites it cites.
                          rel="noopener noreferrer nofollow"
                          className="min-w-0 truncate text-accent hover:underline"
                        >
                          {s.title || s.domain || s.url}
                        </a>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </article>
          ))}
        </div>

        <footer className="mt-12 border-t border-border pt-6 text-center">
          <p className="text-xs text-faint">
            This is a snapshot. Messages sent after it was shared are not
            included.
          </p>
          <Link
            href="/"
            className="mt-4 inline-flex h-10 items-center gap-2 rounded-lg bg-accent-strong px-4 text-sm font-medium text-white transition-colors duration-ts hover:brightness-110"
          >
            <IconExternal size={15} />
            Start your own chat
          </Link>
        </footer>
      </div>
    </main>
  );
}
