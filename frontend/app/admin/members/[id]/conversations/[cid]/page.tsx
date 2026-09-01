'use client';

/**
 * /admin/members/[id]/conversations/[cid] — the READ-ONLY transcript viewer.
 *
 * Deliberately not the chat renderer: no Markdown pipeline, no actions, no
 * composer — just whitespace-preserved text in plain bubbles with
 * timestamps and the model/mode chips the message meta carries. Every load
 * is audited server-side, and the page says so out loud.
 */

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { useParams } from 'next/navigation';
import { formatWhen } from '@/lib/format';
import {
  AdminApiError,
  adminJson,
} from '@/components/admin/api';
import { IconArrowLeft, IconShield } from '@/components/admin/icons';
import { ErrorPanel, SkeletonLine } from '@/components/admin/ui';

interface Message {
  id: number | string;
  role: string;
  content: string;
  created_at: string | null;
  meta: Record<string, unknown> | null;
}

interface ConversationPayload {
  conversation: {
    id: string;
    title: string;
    created_at: string | null;
    updated_at: string | null;
  };
  messages: Message[];
}

const TAG =
  'rounded border border-border px-1.5 py-px text-[10px] font-medium uppercase tracking-wide text-faint';

/** The meta keys worth surfacing — model and routing mode, when present. */
export function metaChips(meta: Record<string, unknown> | null): string[] {
  if (!meta) return [];
  const chips: string[] = [];
  const model = meta.model;
  if (typeof model === 'string' && model) chips.push(model);
  const mode = meta.mode ?? meta.engine ?? meta.route;
  if (typeof mode === 'string' && mode) chips.push(mode);
  return chips;
}

export default function AdminConversationViewerPage() {
  const params = useParams<{ id: string; cid: string }>();
  const memberId = String(params?.id ?? '');
  const conversationId = String(params?.cid ?? '');

  const [data, setData] = useState<ConversationPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    adminJson<ConversationPayload>(
      `members/${memberId}/conversations/${encodeURIComponent(conversationId)}`,
    )
      .then((res) => {
        if (!cancelled) setData(res);
      })
      .catch((err) => {
        if (!cancelled)
          setError(
            err instanceof AdminApiError
              ? err.message
              : 'This conversation could not be loaded.',
          );
      });
    return () => {
      cancelled = true;
    };
  }, [memberId, conversationId, attempt]);

  const loading = data === null && error === null;

  return (
    <div className="mx-auto w-full max-w-thread">
      <Link
        href={`/admin/members/${memberId}`}
        className="inline-flex items-center gap-1.5 text-sm text-muted transition-colors duration-ts hover:text-ink"
      >
        <IconArrowLeft size={15} />
        Back to member
      </Link>

      {error ? (
        <div className="mt-4">
          <ErrorPanel message={error} onRetry={() => setAttempt((n) => n + 1)} />
        </div>
      ) : (
        <>
          <h1 className="mt-4 text-xl font-semibold tracking-tight text-ink">
            {loading ? (
              <SkeletonLine className="w-64" />
            ) : (
              data?.conversation.title || 'Untitled conversation'
            )}
          </h1>
          {data?.conversation.updated_at && (
            <p className="mt-1 text-xs text-muted">
              Updated {formatWhen(data.conversation.updated_at)}
            </p>
          )}
          <p className="mt-2 flex items-center gap-1.5 text-xs text-faint">
            <IconShield size={13} className="shrink-0" />
            Administrative access is recorded in the audit log.
          </p>

          <div className="mt-8 space-y-6">
            {loading &&
              [0, 1, 2].map((i) => (
                <div
                  key={i}
                  className={i % 2 === 0 ? 'flex justify-end' : undefined}
                >
                  <div className="h-12 w-2/3 animate-pulse rounded-2xl bg-surface-2" />
                </div>
              ))}

            {data?.messages.map((message) => {
              const chips = metaChips(message.meta);
              if (message.role === 'user') {
                return (
                  <div key={message.id} className="flex justify-end">
                    <div className="max-w-[80%]">
                      <div className="whitespace-pre-wrap break-words rounded-2xl bg-bubble px-4 py-2.5 text-[15px] leading-6 text-ink">
                        {message.content}
                      </div>
                      {message.created_at && (
                        <div className="mt-1 text-right text-[11px] text-faint">
                          {formatWhen(message.created_at)}
                        </div>
                      )}
                    </div>
                  </div>
                );
              }
              return (
                <div key={message.id} className="max-w-[92%]">
                  <div className="whitespace-pre-wrap break-words text-[15px] leading-6 text-ink">
                    {message.content}
                  </div>
                  <div className="mt-1.5 flex flex-wrap items-center gap-2 text-[11px] text-faint">
                    {message.role !== 'assistant' && (
                      <span className={TAG}>{message.role}</span>
                    )}
                    {message.created_at && (
                      <span>{formatWhen(message.created_at)}</span>
                    )}
                    {chips.map((chip) => (
                      <span key={chip} className={TAG}>
                        {chip}
                      </span>
                    ))}
                  </div>
                </div>
              );
            })}

            {data !== null && data.messages.length === 0 && (
              <p className="rounded-ts border border-border bg-surface px-4 py-10 text-center text-sm text-muted">
                This conversation has no messages.
              </p>
            )}
          </div>
        </>
      )}
    </div>
  );
}
