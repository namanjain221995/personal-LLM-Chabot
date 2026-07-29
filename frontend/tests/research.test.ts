import { describe, expect, it } from 'vitest';
import { toChatStreamEvent, foldStreamState } from '../lib/sse';
import {
  countSources,
  formatElapsed,
  rankDomains,
} from '../components/ResearchPanel';
import type { Research } from '../lib/types';

const ev = (event: string, data: unknown) =>
  toChatStreamEvent({ event, data: JSON.stringify(data) });

describe('research SSE events', () => {
  it('parses a search and its results', () => {
    const out = ev('research', {
      phase: 'query',
      query: 'linkedin algorithm 2026',
      results: [
        { title: 'How it works', url: 'https://a.test/x', domain: 'a.test' },
      ],
    });
    expect(out).toEqual({
      kind: 'research',
      phase: 'query',
      query: {
        query: 'linkedin algorithm 2026',
        results: [
          { title: 'How it works', url: 'https://a.test/x', domain: 'a.test' },
        ],
      },
    });
  });

  it('drops malformed results instead of rendering blank rows', () => {
    const out = ev('research', {
      phase: 'query',
      query: 'q',
      results: [{ title: 'no url' }, { url: 'https://ok.test' }],
    });
    expect(out).toMatchObject({
      query: { results: [{ url: 'https://ok.test', title: 'https://ok.test' }] },
    });
  });

  it('parses the reading and read counts', () => {
    expect(ev('research', { phase: 'reading', count: 30 })).toEqual({
      kind: 'research',
      phase: 'reading',
      count: 30,
    });
    expect(ev('research', { phase: 'read', count: 28 })).toEqual({
      kind: 'research',
      phase: 'read',
      count: 28,
    });
  });

  it('skips events it cannot understand rather than crashing the stream', () => {
    expect(ev('research', { phase: 'unknown-future-phase' })).toBeNull();
    expect(ev('research', { phase: 'query' })).toBeNull();
    expect(ev('research', { phase: 'reading' })).toBeNull();
    expect(toChatStreamEvent({ event: 'research', data: '{bad json' })).toBeNull();
  });
});

describe('research is kept on the persisted message', () => {
  const research: Research = {
    queries: [{ query: 'q', results: [] }],
    elapsedMs: 5000,
    active: true,
  };

  it('folds live research into meta so reopening a chat replays the panel', () => {
    const meta = foldStreamState({ route: 'search' }, { research });
    expect(meta.research?.queries).toHaveLength(1);
    expect(meta.research?.elapsedMs).toBe(5000);
  });

  it('marks it finished — a stored panel must not spin forever', () => {
    const meta = foldStreamState({ route: 'search' }, { research });
    expect(meta.research?.active).toBe(false);
  });

  it('never overwrites research the server already supplied', () => {
    const server: Research = { queries: [], elapsedMs: 1 };
    const meta = foldStreamState({ route: 'search', research: server }, { research });
    expect(meta.research).toBe(server);
  });

  it('adds nothing when no search ran', () => {
    const meta = foldStreamState({ route: 'chat' }, { research: { queries: [] } });
    expect(meta.research).toBeUndefined();
  });
});

describe('elapsed time', () => {
  it.each([
    [0, '0s'],
    [47_000, '47s'],
    [60_000, '1m 0s'],
    [490_000, '8m 10s'],
  ])('formats %ims as %s', (ms, expected) => {
    expect(formatElapsed(ms)).toBe(expected);
  });

  it('never shows a negative time', () => {
    expect(formatElapsed(-5)).toBe('0s');
  });
});

describe('source counting', () => {
  const research: Research = {
    queries: [
      {
        query: 'q1',
        results: [
          { title: 'a', url: 'https://a.test/1', domain: 'a.test' },
          { title: 'b', url: 'https://b.test/1', domain: 'b.test' },
        ],
      },
      {
        query: 'q2',
        results: [
          // same page found again by another search
          { title: 'a', url: 'https://a.test/1', domain: 'a.test' },
          { title: 'c', url: 'https://a.test/2', domain: 'a.test' },
        ],
      },
    ],
  };

  it('counts each page once even when several searches find it', () => {
    expect(countSources(research)).toBe(3);
  });

  it('ranks domains by how many results they supplied', () => {
    expect(rankDomains(research)).toEqual([
      { domain: 'a.test', count: 2 },
      { domain: 'b.test', count: 1 },
    ]);
  });

  it('does not double-count a domain for a repeated url', () => {
    const total = rankDomains(research).reduce((n, d) => n + d.count, 0);
    expect(total).toBe(countSources(research));
  });

  it('handles a research run with no results at all', () => {
    expect(countSources({ queries: [] })).toBe(0);
    expect(rankDomains({ queries: [] })).toEqual([]);
  });
});
