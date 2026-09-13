'use client';

/**
 * The playground: try the API without writing a client.
 *
 * IT NEVER TOUCHES AN API KEY. Not a field for one, not a "paste your key to
 * try it" box, nothing in localStorage. The request goes to the console's own
 * BFF on the session cookie, and the orchestrator runs it as the signed-in
 * person — CONTRACT §1 keeps the two credentials apart, and a page that asked
 * a developer to paste a live key into a browser would be teaching exactly the
 * habit the show-once flow exists to prevent. The snippets beside the output
 * read the key from an environment variable, which is where it belongs.
 *
 * The EVENT INSPECTOR is the point of the panel. CONTRACT §10 specifies the
 * stream precisely — `response.created → response.in_progress →
 * response.output_text.delta (×N) → response.output_text.done →
 * response.completed`, each frame carrying a `sequence_number` that starts at
 * 1 and increases by exactly 1, with exactly one terminal event. A developer
 * implementing against that contract needs to SEE it, so every frame is listed
 * with its number and the deltas are collapsed into one counted row rather
 * than five hundred lines nobody can scroll.
 *
 * SSE parsing is `lib/sse.ts` — the same spec-compliant incremental parser the
 * chat uses, handling \n, \r\n and events split across chunks. A second
 * hand-rolled parser is how two surfaces come to disagree about what a frame
 * is.
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import { CopyButton } from '@/components/CopyButton';
import { Loader } from '@/components/Loader';
import { IconAlert, IconPlay, IconStop } from '@/components/icons';
import { SSEParser } from '@/lib/sse';
import { ConsoleHeader } from '@/components/admin/analytics/filters';
import { Section, Stat, StatRow } from '@/components/admin/analytics/ui';
import { NOT_MEASURED, compact } from '@/components/admin/analytics/format';
import {
  ADMIN_PRIMARY_BUTTON,
  ADMIN_SECONDARY_BUTTON,
  AdminTabs,
} from '@/components/admin/controls';
import { FIELD_INPUT, Field } from '@/components/admin/AdminDialog';
import { CONSOLE_BASE, errorSentence } from './api';
import { consolePaths } from './paths';
import { ConsoleEmpty } from './shared';
import { useConsole } from './useConsole';
import { useConsoleStatus } from './status';
import {
  KEY_ENV_VAR,
  SNIPPET_LANGUAGES,
  type SnippetLanguage,
} from './snippets';
import type { ModelList } from './types';

interface Frame {
  /** The `sequence_number` the server stamped, or null when it sent none. */
  sequence: number | null;
  name: string;
  /** Set on the collapsed delta row: how many deltas it stands for. */
  count?: number;
}

interface Usage {
  input_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
}

function readUsage(payload: Record<string, unknown>): Usage | null {
  const raw =
    (payload.usage as Record<string, unknown> | null | undefined) ??
    ((payload.response as Record<string, unknown> | undefined)?.usage as
      | Record<string, unknown>
      | null
      | undefined);
  // Absent OR explicitly null both mean "not measured" (CONTRACT §9), and the
  // panel says so rather than showing zeros.
  if (!raw || typeof raw !== 'object') return null;
  const num = (key: string) =>
    typeof raw[key] === 'number' ? (raw[key] as number) : null;
  return {
    input_tokens: num('input_tokens'),
    output_tokens: num('output_tokens'),
    total_tokens: num('total_tokens'),
  };
}

export function PlaygroundPanel() {
  const models = useConsole<ModelList>(consolePaths.models());
  const { announce } = useConsoleStatus();

  const available = (models.data?.models ?? []).filter((m) => m.enabled);
  const [model, setModel] = useState('');
  const [instructions, setInstructions] = useState('');
  const [input, setInput] = useState('');
  const [temperature, setTemperature] = useState('0.2');
  const [maxOutputTokens, setMaxOutputTokens] = useState('512');
  const [language, setLanguage] = useState<SnippetLanguage>('curl');

  const [running, setRunning] = useState(false);
  const [output, setOutput] = useState('');
  const [frames, setFrames] = useState<Frame[]>([]);
  const [usage, setUsage] = useState<Usage | null>(null);
  const [requestId, setRequestId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const abort = useRef<AbortController | null>(null);

  const chosen = model || available[0]?.id || '';

  // HYDRATION #418, 2026-09-13 visual QA. The snippet used to read
  // `window.location.origin` during render: '' on the server, the real origin
  // in the browser, so the first client render disagreed with the HTML and
  // React threw away the server markup. The origin is now state that starts
  // '' on BOTH sides and is filled in after mount — React's own recipe for a
  // browser-only value (Settings reads the origin the same way).
  const [origin, setOrigin] = useState('');
  useEffect(() => setOrigin(window.location.origin), []);

  // A run that is still in flight when the panel goes away must be stopped:
  // an abandoned generation holds an admission lane the chat app shares.
  useEffect(() => () => abort.current?.abort(), []);

  const snippet = useMemo(() => {
    const builder = SNIPPET_LANGUAGES.find((l) => l.id === language) ?? SNIPPET_LANGUAGES[0];
    return builder.build({
      baseUrl: origin,
      model: chosen || 'techsara-35b',
      input: input || 'Explain retrieval-augmented generation.',
      instructions,
      stream: true,
      temperature: Number.isFinite(Number(temperature)) ? Number(temperature) : null,
      maxOutputTokens: Number.isFinite(Number(maxOutputTokens))
        ? Number(maxOutputTokens)
        : null,
    });
  }, [origin, language, chosen, input, instructions, temperature, maxOutputTokens]);

  function stop() {
    abort.current?.abort();
    abort.current = null;
    setRunning(false);
    announce('Run stopped.');
  }

  async function run() {
    if (running || !input.trim() || !chosen) return;
    const controller = new AbortController();
    abort.current = controller;
    setRunning(true);
    setOutput('');
    setFrames([]);
    setUsage(null);
    setRequestId(null);
    setError(null);
    announce('Running against the API.');

    let res: Response;
    try {
      res = await fetch(`${CONSOLE_BASE}${consolePaths.playground()}`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        cache: 'no-store',
        signal: controller.signal,
        body: JSON.stringify({
          model: chosen,
          input: input.trim(),
          instructions: instructions.trim() || undefined,
          stream: true,
          temperature: Number(temperature),
          max_output_tokens: Number(maxOutputTokens),
        }),
      });
    } catch {
      if (!controller.signal.aborted) {
        setError('The request could not be sent. Check the connection.');
        announce('The request could not be sent.');
      }
      setRunning(false);
      return;
    }

    setRequestId(res.headers.get('x-request-id'));

    if (!res.ok || !res.body) {
      // A refusal arrives as JSON, not as a stream; show the server's sentence.
      // The playground answers refusals in the CONTRACT §9 envelope,
      // `{error: {message}}` — the same one /v1 returns — so that is read
      // first-class rather than falling through to the status sentence.
      let message = `The request failed with status ${res.status}.`;
      try {
        message = errorSentence(await res.json()) ?? message;
      } catch {
        // Non-JSON body — the status sentence stands.
      }
      setError(message);
      announce(message);
      setRunning(false);
      return;
    }

    const parser = new SSEParser();
    const decoder = new TextDecoder();
    const reader = res.body.getReader();
    const seen: Frame[] = [];
    let deltas = 0;

    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        for (const event of parser.feed(decoder.decode(value, { stream: true }))) {
          let payload: Record<string, unknown> = {};
          try {
            payload = JSON.parse(event.data) as Record<string, unknown>;
          } catch {
            // A frame whose data is not JSON is still a frame worth listing.
          }
          const sequence =
            typeof payload.sequence_number === 'number'
              ? payload.sequence_number
              : null;
          const name = event.event || String(payload.type ?? 'message');

          if (name === 'response.output_text.delta') {
            deltas += 1;
            const delta = typeof payload.delta === 'string' ? payload.delta : '';
            if (delta) setOutput((prev) => prev + delta);
            // One collapsed row for the deltas, counted — five hundred
            // identical lines is not an inspector, it is a wall.
            const existing = seen.find((f) => f.name === name);
            if (existing) existing.count = deltas;
            else seen.push({ sequence, name, count: deltas });
          } else {
            seen.push({ sequence, name });
          }

          if (name === 'response.output_text.done') {
            const text = typeof payload.text === 'string' ? payload.text : null;
            if (text !== null) setOutput(text);
          }
          if (
            name === 'response.completed' ||
            name === 'response.failed' ||
            name === 'error'
          ) {
            setUsage(readUsage(payload));
            if (name !== 'response.completed') {
              // `error` carries its sentence at the top level
              // (ApiError.stream_payload); `response.failed` carries it on
              // the response object.
              const failed = (payload.response as { error?: unknown } | undefined)?.error;
              setError(
                errorSentence(payload) ??
                  errorSentence({ error: failed }) ??
                  'The generation did not finish.',
              );
            }
          }
          setFrames([...seen]);
        }
      }
      announce('Run finished.');
    } catch {
      if (!controller.signal.aborted) {
        setError('The stream ended unexpectedly.');
        announce('The stream ended unexpectedly.');
      }
    } finally {
      abort.current = null;
      setRunning(false);
    }
  }

  if (!models.loading && available.length === 0) {
    return (
      <div>
        <ConsoleHeader title="Playground" />
        <ConsoleEmpty
          title="No model is published"
          body="The playground calls the same models the public API offers. When none is published there is nothing to call — the Models tab lists what this deployment declares."
        />
      </div>
    );
  }

  return (
    <div>
      <ConsoleHeader
        title="Playground"
        description="Send a request the way your application will, and watch the stream frame by frame. It runs on your session — the playground never asks for, stores or sends an API key."
      />

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <section aria-label="Request">
          <h2 className="text-sm font-medium text-ink">Request</h2>
          <div className="mt-3 space-y-3">
            <Field label="Model">
              <select
                value={chosen}
                onChange={(e) => setModel(e.target.value)}
                className={FIELD_INPUT}
              >
                {available.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.id}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Instructions (optional)">
              <textarea
                value={instructions}
                onChange={(e) => setInstructions(e.target.value)}
                rows={2}
                placeholder="Answer in British English."
                className={`${FIELD_INPUT} resize-y`}
              />
            </Field>
            <Field label="Input">
              <textarea
                value={input}
                onChange={(e) => setInput(e.target.value)}
                rows={6}
                required
                placeholder="Explain retrieval-augmented generation."
                className={`${FIELD_INPUT} resize-y`}
              />
            </Field>
            <div className="grid grid-cols-2 gap-3">
              <Field label="Temperature">
                <input
                  type="number"
                  step="0.1"
                  min="0"
                  max="2"
                  value={temperature}
                  onChange={(e) => setTemperature(e.target.value)}
                  className={FIELD_INPUT}
                />
              </Field>
              <Field label="Max output tokens">
                <input
                  type="number"
                  min="1"
                  value={maxOutputTokens}
                  onChange={(e) => setMaxOutputTokens(e.target.value)}
                  className={FIELD_INPUT}
                />
              </Field>
            </div>
            <div className="flex items-center gap-2">
              {running ? (
                <button type="button" onClick={stop} className={ADMIN_SECONDARY_BUTTON}>
                  <IconStop size={15} />
                  Stop
                </button>
              ) : (
                <button
                  type="button"
                  onClick={() => void run()}
                  disabled={!input.trim()}
                  className={ADMIN_PRIMARY_BUTTON}
                >
                  <IconPlay size={15} />
                  Run
                </button>
              )}
              {running && <Loader size={16} />}
            </div>
            {/* The sentence is ONE inline run inside its own span. As bare
                children of the flex paragraph, the words, the code chip and
                the full stop were three separate flex items, so at 1440px the
                chip floated to the end of the first line and split the
                sentence (visual QA, 2026-09-13). */}
            <p className="flex items-start gap-1.5 text-xs text-faint">
              <IconAlert size={13} className="mt-px shrink-0" />
              <span className="min-w-0">
                This runs on your signed-in session, not on an API key. Copy the
                snippet to run the same request from your own code with{' '}
                <code className="whitespace-nowrap font-mono">${KEY_ENV_VAR}</code>.
              </span>
            </p>
          </div>
        </section>

        <section aria-label="Response">
          <h2 className="text-sm font-medium text-ink">Response</h2>
          {error && (
            <p role="alert" className="mt-3 flex items-start gap-1.5 text-sm text-danger">
              <IconAlert size={15} className="mt-0.5 shrink-0" />
              {error}
            </p>
          )}
          <pre
            data-testid="playground-output"
            className="mt-3 max-h-72 min-h-[120px] overflow-auto whitespace-pre-wrap break-words rounded-lg border border-border bg-bg p-3 font-mono text-xs text-ink"
          >
            {output || (running ? '' : 'The answer appears here.')}
          </pre>

          <h3 className="mt-5 text-sm font-medium text-ink">Event inspector</h3>
          {frames.length === 0 ? (
            <p className="mt-2 rounded-lg border border-dashed border-[var(--admin-separator)] px-4 py-6 text-center text-xs text-faint">
              Frames appear here as the stream arrives, in the order the contract
              specifies, each with its sequence number.
            </p>
          ) : (
            <ol className="mt-2 space-y-1">
              {frames.map((frame, i) => (
                <li
                  key={`${frame.name}-${i}`}
                  className="flex items-baseline justify-between gap-3 rounded-lg px-2 py-1 text-xs odd:bg-[var(--admin-row-hover)]"
                >
                  <span className="w-6 shrink-0 tabular-nums text-faint">
                    {frame.sequence ?? '·'}
                  </span>
                  <code className="min-w-0 flex-1 truncate font-mono text-ink">
                    {frame.name}
                  </code>
                  {frame.count !== undefined && (
                    <span className="shrink-0 tabular-nums text-faint">
                      ×{frame.count}
                    </span>
                  )}
                </li>
              ))}
            </ol>
          )}

          <div className="mt-5">
            <StatRow columns={3}>
              <Stat
                label="Input tokens"
                value={
                  usage?.input_tokens == null
                    ? NOT_MEASURED
                    : compact(usage.input_tokens)
                }
              />
              <Stat
                label="Output tokens"
                value={
                  usage?.output_tokens == null
                    ? NOT_MEASURED
                    : compact(usage.output_tokens)
                }
              />
              <Stat
                label="Total tokens"
                value={
                  usage?.total_tokens == null
                    ? NOT_MEASURED
                    : compact(usage.total_tokens)
                }
              />
            </StatRow>
            <p className="mt-2 text-xs text-faint">
              Request id:{' '}
              {requestId ? (
                <code className="font-mono text-muted">{requestId}</code>
              ) : (
                <span className="text-faint">
                  not yet — it arrives with the response
                </span>
              )}
            </p>
          </div>
        </section>
      </div>

      <Section title="Use this from your code">
        <AdminTabs
          label="Snippet language"
          active={language}
          onChange={(id) => setLanguage(id as SnippetLanguage)}
          tabs={SNIPPET_LANGUAGES.map((l) => ({ id: l.id, label: l.label }))}
        />
        <div className="mt-3 flex items-start gap-2 rounded-lg border border-border bg-bg p-3">
          <pre
            data-testid="playground-snippet"
            className="min-w-0 flex-1 overflow-x-auto whitespace-pre font-mono text-xs text-ink"
          >
            {snippet}
          </pre>
          <CopyButton text={snippet} label="Copy snippet" />
        </div>
      </Section>
    </div>
  );
}
