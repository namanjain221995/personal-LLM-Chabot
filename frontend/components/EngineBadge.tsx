import type { Engine } from '@/lib/types';

/**
 * Engine identity chips (§9 + V2 §4f): color is used for badges/accents
 * only. V2 adds Chat (slate) and Agent (violet, gradient dot).
 */

const ENGINE_LABEL: Record<Engine, string> = {
  sql: 'SQL',
  rag: 'Records',
  vision: 'Vision',
  report: 'Report',
  chat: 'Chat',
  agent: 'Agent',
  search: 'Web',
  url: 'Page',
  // The site crawler (2026-08-30): a whole indexed site, not a single page.
  crawl: 'Site',
  repo: 'Repo',
  // The orchestrator has always emitted route "clarify" for a question asked
  // back; it was simply absent from the Engine union, so this map had no entry
  // and the badge fell through to the neutral Chat style.
  clarify: 'Question',
};

const ENGINE_STYLE: Record<Engine, { color: string; ink: string }> = {
  sql: { color: 'var(--ts-engine-sql)', ink: 'var(--ts-engine-sql-ink)' },
  rag: { color: 'var(--ts-engine-rag)', ink: 'var(--ts-engine-rag-ink)' },
  vision: {
    color: 'var(--ts-engine-vision)',
    ink: 'var(--ts-engine-vision-ink)',
  },
  report: {
    color: 'var(--ts-engine-report)',
    ink: 'var(--ts-engine-report-ink)',
  },
  chat: { color: 'var(--ts-engine-chat)', ink: 'var(--ts-engine-chat-ink)' },
  search: {
    color: 'var(--ts-engine-rag)',
    ink: 'var(--ts-engine-rag-ink)',
  },
  url: {
    color: 'var(--ts-engine-report)',
    ink: 'var(--ts-engine-report-ink)',
  },
  // Same palette as Page on purpose: "Site" is Page's plural.
  crawl: {
    color: 'var(--ts-engine-report)',
    ink: 'var(--ts-engine-report-ink)',
  },
  repo: {
    color: 'var(--ts-engine-vision)',
    ink: 'var(--ts-engine-vision-ink)',
  },
  agent: {
    color: 'var(--ts-engine-agent)',
    ink: 'var(--ts-engine-agent-ink)',
  },
  clarify: {
    color: 'var(--ts-engine-chat)',
    ink: 'var(--ts-engine-chat-ink)',
  },
};

export function engineAccent(engine: Engine): string {
  return (ENGINE_STYLE[engine] ?? ENGINE_STYLE.chat).color;
}

export function EngineBadge({
  engine,
  size = 'sm',
}: {
  engine: Engine;
  size?: 'xs' | 'sm';
}) {
  // Unknown future routes degrade to the neutral Chat style (V2 §2 asks the
  // frontend to tolerate additions) — the raw route text is still shown.
  const s = ENGINE_STYLE[engine] ?? ENGINE_STYLE.chat;
  const label = ENGINE_LABEL[engine] ?? engine;
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border font-medium ${
        size === 'xs' ? 'px-2 py-px text-[11px]' : 'px-2.5 py-0.5 text-xs'
      }`}
      style={{
        color: s.ink,
        borderColor: `color-mix(in srgb, ${s.color} 45%, transparent)`,
        background: `color-mix(in srgb, ${s.color} 12%, transparent)`,
      }}
    >
      <span
        aria-hidden
        className="h-1.5 w-1.5 rounded-full"
        style={{
          background:
            engine === 'agent'
              ? `linear-gradient(135deg, ${s.ink}, var(--ts-accent))`
              : s.ink,
        }}
      />
      {label}
    </span>
  );
}
