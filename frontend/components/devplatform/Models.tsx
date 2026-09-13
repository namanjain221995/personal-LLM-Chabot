'use client';

/**
 * Which models the public API publishes.
 *
 * CONTRACT §15: a code-level registry is the source of truth and the database
 * may only NARROW it — a `public_models` row can disable a model the code
 * declares, never enable one it does not. So this page lists what the code
 * catalogues and the only control on it is a switch that turns one off. There
 * is no "add model" button, because there is no such operation; infrastructure
 * cannot publish a model by appearing in Compose.
 *
 * EVERY MODEL THE PLATFORM RUNS (owner request, 2026-09-13). The page used to
 * show techsara-35b alone; it now lists the whole catalogue — the main chat
 * model, the 8B vision model, OCR, embeddings, the reranker and speech-to-text
 * — each with what it can do, where it is served and its ceilings. A model
 * this deployment runs no engine for is listed as "Not configured on this
 * deployment" rather than hidden, so a documented model is never simply
 * missing, and it can still be withdrawn before its engine is switched on.
 *
 * Turning a model off is `api.models.manage`, which CONTRACT §6 gives to a
 * SUPER ADMIN alone: an admin may run projects, not decide what the platform
 * exposes to the world. Without the capability this page is a read-only list,
 * and the orchestrator refuses the call regardless.
 *
 * The ceilings are read from the registry, which reads the engines' own
 * settings, never hard-coded here — which is why they are `number | null` and
 * render as "—" when not reported. They are ENGINE facts, not usage limits:
 * the public API has no usage limits by owner decision.
 */

import { useEffect, useMemo, useState } from 'react';
import { useToast } from '@/components/Providers';
import { can, type Me } from '@/components/admin/api';
import { Switch } from '@/components/admin/Switch';
import { ConsoleHeader } from '@/components/admin/analytics/filters';
import { NOT_MEASURED, compact } from '@/components/admin/analytics/format';
import { consolePut, messageOf } from './api';
import { consolePaths } from './paths';
import { ConsoleEmpty, ConsoleTable, type ConsoleColumn } from './shared';
import { useConsole } from './useConsole';
import { useConsoleStatus } from './status';
import {
  longAnswerHours,
  modelConfigured,
  modelEndpoints,
  modelKind,
  type ConsoleModel,
  type ModelCapabilities,
  type ModelKind,
  type ModelList,
} from './types';

/** The badges, in the order they read best: what it does, then how. */
export const CAPABILITY_BADGES: { flag: keyof ModelCapabilities; label: string }[] = [
  { flag: 'chat', label: 'Chat' },
  { flag: 'streaming', label: 'Streaming' },
  { flag: 'vision', label: 'Vision' },
  { flag: 'ocr', label: 'OCR' },
  { flag: 'embeddings', label: 'Embeddings' },
  { flag: 'rerank', label: 'Rerank' },
  { flag: 'audio_transcription', label: 'Speech-to-text' },
  { flag: 'tools', label: 'Tools' },
  { flag: 'background', label: 'Background' },
];

export const KIND_LABEL: Record<ModelKind, string> = {
  chat: 'Chat model',
  embedding: 'Embeddings model',
  rerank: 'Reranker',
  transcription: 'Speech-to-text model',
};

/** The capability words a model declares, in badge order. */
export function capabilityLabels(model: ConsoleModel): string[] {
  const caps = model.capabilities ?? ({} as ModelCapabilities);
  return CAPABILITY_BADGES.filter((b) => caps[b.flag] === true).map((b) => b.label);
}

/** An exact count, grouped, or "—" when the registry did not report it. */
function exactTokens(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? NOT_MEASURED : value.toLocaleString();
}

function mebibytes(bytes: number): string {
  const mib = bytes / 1_048_576;
  return `${Number.isInteger(mib) ? mib : mib.toFixed(1)} MiB`;
}

function audioLength(seconds: number): string {
  if (seconds < 60) return `${seconds} s`;
  const minutes = seconds / 60;
  return `${seconds.toLocaleString()} s (${Number.isInteger(minutes) ? minutes : minutes.toFixed(1)} min)`;
}

/**
 * The rows of a model's reference card, in reading order, for its kind.
 *
 * Token ceilings are always listed for a chat model ("—" when not reported,
 * because a chat model without a reported window is worth noticing); for the
 * other kinds a ceiling that does not apply — an embeddings model has no
 * output — is left out rather than drawn as a dash that looks like a gap.
 */
export function modelFacts(model: ConsoleModel): { label: string; value: string }[] {
  const kind = modelKind(model);
  const limits = model.limits ?? {};
  const facts: { label: string; value: string }[] = [];
  const tokens = (label: string, value: number | null | undefined, always: boolean) => {
    if (always || value != null) facts.push({ label, value: exactTokens(value) });
  };
  const chat = kind === 'chat';
  if (kind === 'rerank') {
    tokens('Max tokens per query and document', model.max_input_tokens, false);
  } else if (kind === 'embedding') {
    tokens('Max tokens per input', model.max_input_tokens, false);
  } else {
    tokens('Context window', model.context_window, chat);
    tokens('Max input tokens', model.max_input_tokens, chat);
    tokens('Max output tokens', model.max_output_tokens, chat);
    tokens('Default output tokens', model.default_max_output_tokens, chat);
  }
  const count = (label: string, value: number | undefined) => {
    if (value != null) facts.push({ label, value: value.toLocaleString() });
  };
  count('Images per request', limits.max_images_per_request);
  count('Inputs per request', limits.max_inputs_per_request);
  count('Embedding dimensions', limits.embedding_dimensions);
  count('Documents per request', limits.max_documents_per_request);
  if (limits.max_audio_seconds != null) {
    facts.push({ label: 'Max audio length', value: audioLength(limits.max_audio_seconds) });
  }
  if (limits.max_audio_bytes != null) {
    facts.push({ label: 'Max audio file', value: mebibytes(limits.max_audio_bytes) });
  }
  if (limits.response_formats && limits.response_formats.length > 0) {
    facts.push({ label: 'Response formats', value: limits.response_formats.join(', ') });
  }
  return facts;
}

/**
 * Above this many output tokens the card says the answer has to stream or run
 * in the background. A synchronous request sends no byte until it is done, and
 * the public hostname's edge gives up after 100 s with nothing sent — about
 * 5,000 tokens at the measured 70–100 tokens a second.
 */
const STREAM_ONLY_OUTPUT_TOKENS = 32_768;

function CapabilityBadges({ model }: { model: ConsoleModel }) {
  const labels = capabilityLabels(model);
  if (labels.length === 0) {
    return <span className="text-xs text-faint">None declared</span>;
  }
  return (
    <ul aria-label={`${model.id} capabilities`} className="flex flex-wrap gap-1">
      {labels.map((label) => (
        <li
          key={label}
          className="inline-flex items-center rounded-md border border-[var(--admin-separator)] px-1.5 py-px text-[11px] font-medium leading-4 text-muted"
        >
          {label}
        </li>
      ))}
    </ul>
  );
}

function NotConfigured() {
  return (
    <span className="inline-flex items-center rounded-md border border-dashed border-[var(--admin-separator)] px-1.5 py-px text-[11px] leading-4 text-faint">
      Not configured on this deployment
    </span>
  );
}

function ModelCard({ model }: { model: ConsoleModel }) {
  const kind = modelKind(model);
  const facts = modelFacts(model);
  const endpoints = modelEndpoints(model);
  const headingId = `model-card-${model.id}`;
  return (
    <article
      aria-labelledby={headingId}
      data-testid={`model-card-${model.id}`}
      className="min-w-0 rounded-lg border border-border bg-bg p-4"
    >
      <header className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <h3
          id={headingId}
          className="min-w-0 font-mono text-sm font-medium text-ink [overflow-wrap:anywhere]"
        >
          {model.id}
        </h3>
        <span className="text-xs text-faint">{KIND_LABEL[kind]}</span>
      </header>
      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        <CapabilityBadges model={model} />
        {!modelConfigured(model) && <NotConfigured />}
      </div>
      {facts.length > 0 && (
        <dl className="mt-3 grid grid-cols-[minmax(0,1fr)_auto] gap-x-4 gap-y-1.5 text-xs">
          {facts.map((fact) => (
            <div key={fact.label} className="contents">
              <dt className="min-w-0 text-faint">{fact.label}</dt>
              <dd className="text-right tabular-nums text-ink">{fact.value}</dd>
            </div>
          ))}
        </dl>
      )}
      {kind === 'chat' && (model.max_output_tokens ?? 0) > STREAM_ONLY_OUTPUT_TOKENS && (
        <p className="mt-2 text-xs leading-relaxed text-faint">
          Long answers must stream or run in the background: at 70–100 tokens a
          second, {(model.max_output_tokens ?? 0).toLocaleString()} tokens take{' '}
          {longAnswerHours(model.max_output_tokens ?? 0)}, and a request that waits
          for the whole answer is cut off by the network long before that.
        </p>
      )}
      <div className="mt-3">
        <p className="text-xs text-faint">Endpoints</p>
        <ul className="mt-1 space-y-0.5">
          {endpoints.map((path) => (
            <li key={path}>
              <code className="font-mono text-xs text-muted [overflow-wrap:anywhere]">
                POST {path}
              </code>
            </li>
          ))}
        </ul>
      </div>
    </article>
  );
}

export function ModelsPanel({ me }: { me: Me }) {
  const { data, loading, error, reload } = useConsole<ModelList>(consolePaths.models());
  const { toast } = useToast();
  const { announce } = useConsoleStatus();
  const [pending, setPending] = useState<string | null>(null);

  const models = data?.models ?? [];
  const mayManage = can(me, 'api.models.manage');

  useEffect(() => {
    if (loading) announce('Loading models.');
    else if (error) announce(error);
    else announce(`${models.length} model${models.length === 1 ? '' : 's'}.`);
  }, [loading, error, models.length, announce]);

  async function setEnabled(model: ConsoleModel, enabled: boolean) {
    setPending(model.id);
    try {
      // PUT, as console_api.set_model_enabled declares it (this was a PATCH
      // the router never served until 2026-09-13).
      await consolePut(consolePaths.model(model.id), { enabled });
      toast(
        enabled
          ? `${model.id} is available to API keys again.`
          : `${model.id} is no longer offered to API keys.`,
      );
      reload();
    } catch (err) {
      toast(messageOf(err, 'The model could not be changed.'), 'error');
    } finally {
      setPending(null);
    }
  }

  const columns: ConsoleColumn<ConsoleModel>[] = useMemo(
    () => [
      {
        key: 'model',
        label: 'Model',
        render: (m) => (
          // `whitespace-normal`: every console cell is nowrap, and the badges
          // must wrap onto a second line on a phone rather than run under the
          // publish switch.
          <div className="min-w-0 whitespace-normal py-2">
            <div className="flex flex-wrap items-baseline gap-x-2">
              <span className="min-w-0 font-medium text-ink [overflow-wrap:anywhere]">
                {m.id}
              </span>
              <span className="text-xs text-faint">{KIND_LABEL[modelKind(m)]}</span>
            </div>
            <div className="mt-1 flex flex-wrap items-center gap-1.5">
              <CapabilityBadges model={m} />
              {!modelConfigured(m) && <NotConfigured />}
            </div>
          </div>
        ),
      },
      {
        key: 'context',
        label: 'Context window',
        width: '140px',
        align: 'right',
        // Below lg the publish switch needs the room more than the ceilings
        // do; the reference cards under the table carry them on a phone.
        hideBelowLg: true,
        render: (m) => (
          <span className="tabular-nums text-muted" title={exactTokens(m.context_window)}>
            {compact(m.context_window)}
          </span>
        ),
      },
      {
        key: 'input',
        label: 'Max input',
        width: '120px',
        align: 'right',
        // Folded below xl so a 1024px laptop keeps the model column readable
        // and the publish switch on screen; the cards below carry it.
        hideBelow: 'xl',
        render: (m) => (
          <span className="tabular-nums text-muted" title={exactTokens(m.max_input_tokens)}>
            {compact(m.max_input_tokens)}
          </span>
        ),
      },
      {
        key: 'output',
        label: 'Max output',
        width: '120px',
        align: 'right',
        hideBelowLg: true,
        render: (m) => (
          <span className="tabular-nums text-muted" title={exactTokens(m.max_output_tokens)}>
            {compact(m.max_output_tokens)}
          </span>
        ),
      },
      {
        key: 'published',
        label: 'Published',
        width: '150px',
        align: 'right',
        render: (m) =>
          mayManage ? (
            <Switch
              checked={m.enabled}
              onChange={(next) => void setEnabled(m, next)}
              label={`Publish ${m.id} to API keys`}
              disabled={pending === m.id}
            />
          ) : (
            <span className="text-xs text-muted">
              {m.enabled ? 'Published' : 'Withdrawn'}
            </span>
          ),
      },
    ],
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [mayManage, pending],
  );

  return (
    <div>
      <ConsoleHeader
        title="Models"
        description="Every model the public API can offer, what each one does and its ceilings. The list itself comes from code — this page can withdraw a model from the API, never add one."
      />

      <div className="mt-2">
        {!loading && !error && models.length === 0 ? (
          <ConsoleEmpty
            title="No models are published"
            body="The platform declares its public models in code and narrows them here. An empty list means the deployment has published none — nothing on this page can create one."
          />
        ) : (
          <ConsoleTable
            columns={columns}
            minWidth={900}
            rows={models}
            rowKey={(m) => m.id}
            loading={loading && data === null}
            empty="No models are published."
            error={error}
            onRetry={reload}
          />
        )}
      </div>

      {models.length > 0 && (
        <section aria-labelledby="model-reference-heading" className="mt-8">
          <h2 id="model-reference-heading" className="text-sm font-medium text-ink">
            Ceilings and endpoints
          </h2>
          <p className="mt-1 max-w-2xl text-xs leading-relaxed text-faint">
            Technical ceilings of each engine, not usage limits — the API has none. A
            request whose input and output together would overflow a chat model’s
            context window is clamped to the room left, and the response says what was
            applied.
          </p>
          <div className="mt-3 grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {models.map((m) => (
              <ModelCard key={m.id} model={m} />
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
