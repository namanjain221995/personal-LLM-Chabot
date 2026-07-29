import { describe, expect, it } from 'vitest';
import {
  foldStreamState,
  mergeStep,
  readChatStream,
  SSEParser,
  toChatStreamEvent,
  type ChatStreamEvent,
  type SSEEvent,
} from '../lib/sse';
import type { AgentStep, Meta } from '../lib/types';

function feedAll(parser: SSEParser, chunks: string[]): SSEEvent[] {
  return chunks.flatMap((c) => parser.feed(c));
}

describe('SSEParser framing', () => {
  it('parses a single token event', () => {
    const parser = new SSEParser();
    const events = parser.feed('event: token\ndata: {"text": "Hi"}\n\n');
    expect(events).toEqual([{ event: 'token', data: '{"text": "Hi"}' }]);
  });

  it('parses the full §10 sequence: token → meta → done', () => {
    const parser = new SSEParser();
    const events = parser.feed(
      'event: token\ndata: {"text": "42"}\n\n' +
        'event: meta\ndata: {"route":"sql","sql":"SELECT 1","truncated":false}\n\n' +
        'event: done\ndata: {}\n\n',
    );
    expect(events.map((e) => e.event)).toEqual(['token', 'meta', 'done']);
    expect(JSON.parse(events[1].data).route).toBe('sql');
  });

  it('parses error events', () => {
    const parser = new SSEParser();
    const events = parser.feed(
      'event: error\ndata: {"message": "SQL guard rejected the statement"}\n\n',
    );
    expect(events).toHaveLength(1);
    expect(events[0].event).toBe('error');
    expect(JSON.parse(events[0].data).message).toMatch(/SQL guard/);
  });

  it('handles one event split across many chunks (mid-field, mid-line)', () => {
    const parser = new SSEParser();
    const events = feedAll(parser, [
      'ev',
      'ent: tok',
      'en\nda',
      'ta: {"text": "he',
      'llo"}\n',
      '\n',
    ]);
    expect(events).toEqual([{ event: 'token', data: '{"text": "hello"}' }]);
  });

  it('handles multiple events arriving in one chunk plus a partial tail', () => {
    const parser = new SSEParser();
    const first = parser.feed(
      'event: token\ndata: {"text":"a"}\n\nevent: token\ndata: {"text":"b"}\n\nevent: do',
    );
    expect(first.map((e) => JSON.parse(e.data).text)).toEqual(['a', 'b']);
    const rest = parser.feed('ne\ndata: {}\n\n');
    expect(rest).toEqual([{ event: 'done', data: '{}' }]);
  });

  it('supports CRLF line endings', () => {
    const parser = new SSEParser();
    const events = parser.feed(
      'event: token\r\ndata: {"text":"x"}\r\n\r\n',
    );
    expect(events).toEqual([{ event: 'token', data: '{"text":"x"}' }]);
  });

  it('supports CRLF pairs split across chunks', () => {
    const parser = new SSEParser();
    const events = feedAll(parser, [
      'event: token\r',
      '\ndata: {"text":"x"}\r\n',
      '\r\n',
    ]);
    expect(events).toEqual([{ event: 'token', data: '{"text":"x"}' }]);
  });

  it('joins multiple data lines with newlines per the SSE spec', () => {
    const parser = new SSEParser();
    const events = parser.feed('event: token\ndata: line1\ndata: line2\n\n');
    expect(events[0].data).toBe('line1\nline2');
  });

  it('ignores comment/keep-alive lines', () => {
    const parser = new SSEParser();
    const events = parser.feed(
      ': keep-alive\n\nevent: done\ndata: {}\n\n',
    );
    expect(events).toEqual([{ event: 'done', data: '{}' }]);
  });

  it('defaults the event type to "message" when absent', () => {
    const parser = new SSEParser();
    const events = parser.feed('data: {}\n\n');
    expect(events[0].event).toBe('message');
  });
});

describe('toChatStreamEvent (§10 contract mapping)', () => {
  it('maps token events', () => {
    expect(
      toChatStreamEvent({ event: 'token', data: '{"text": "delta"}' }),
    ).toEqual({ kind: 'token', text: 'delta' });
  });

  it('maps meta events with the full §10 payload', () => {
    const meta: Meta = {
      route: 'sql',
      sql: 'SELECT 1',
      data: [{ n: 1 }],
      truncated: true,
      chart: {
        type: 'bar',
        x_key: 'n',
        y_keys: ['n'],
        title: 't',
        stacked: false,
      },
    };
    const ev = toChatStreamEvent({ event: 'meta', data: JSON.stringify(meta) });
    expect(ev).toEqual({ kind: 'meta', meta });
  });

  it('maps done and error events', () => {
    expect(toChatStreamEvent({ event: 'done', data: '{}' })).toEqual({
      kind: 'done',
    });
    expect(
      toChatStreamEvent({ event: 'error', data: '{"message":"boom"}' }),
    ).toEqual({ kind: 'error', message: 'boom' });
  });

  it('drops unknown event types and malformed JSON instead of throwing', () => {
    expect(toChatStreamEvent({ event: 'mystery', data: '{}' })).toBeNull();
    expect(toChatStreamEvent({ event: 'token', data: 'not json' })).toBeNull();
    expect(toChatStreamEvent({ event: 'token', data: '{"nope":1}' })).toBeNull();
  });

  it('maps reasoning events (V2 §2)', () => {
    expect(
      toChatStreamEvent({ event: 'reasoning', data: '{"text": "hmm "}' }),
    ).toEqual({ kind: 'reasoning', text: 'hmm ' });
    expect(
      toChatStreamEvent({ event: 'reasoning', data: '{"nope": 1}' }),
    ).toBeNull();
  });

  it('maps step events and validates their shape (V2 §2)', () => {
    expect(
      toChatStreamEvent({
        event: 'step',
        data: '{"id":1,"title":"Plan","status":"running"}',
      }),
    ).toEqual({
      kind: 'step',
      step: { id: 1, title: 'Plan', status: 'running' },
    });
    expect(
      toChatStreamEvent({
        event: 'step',
        data: '{"id":2,"title":"Query","status":"done","detail":"5 rows"}',
      }),
    ).toEqual({
      kind: 'step',
      step: { id: 2, title: 'Query', status: 'done', detail: '5 rows' },
    });
    // Invalid payloads are skipped, never thrown.
    expect(
      toChatStreamEvent({ event: 'step', data: '{"id":"x","title":"t","status":"done"}' }),
    ).toBeNull();
    expect(
      toChatStreamEvent({ event: 'step', data: '{"id":1,"title":"t","status":"paused"}' }),
    ).toBeNull();
    expect(toChatStreamEvent({ event: 'step', data: 'not json' })).toBeNull();
  });

  it('tolerates unknown meta keys (V2 §2: future keys pass through)', () => {
    const ev = toChatStreamEvent({
      event: 'meta',
      data: '{"route":"chat","mode":"assistant","model":"m","effort":"low","brand_new_key":{"x":1}}',
    });
    expect(ev?.kind).toBe('meta');
    if (ev?.kind === 'meta') {
      expect(ev.meta.route).toBe('chat');
      expect(
        (ev.meta as unknown as Record<string, unknown>).brand_new_key,
      ).toEqual({ x: 1 });
    }
  });
});

describe('readChatStream end-to-end (V2 §2 tolerance)', () => {
  function streamOf(chunks: string[]): ReadableStream<Uint8Array> {
    const encoder = new TextEncoder();
    return new ReadableStream({
      start(controller) {
        for (const c of chunks) controller.enqueue(encoder.encode(c));
        controller.close();
      },
    });
  }

  it('ignores unknown event types mid-stream without breaking it', async () => {
    const body = streamOf([
      'event: ping\ndata: {"ts":1}\n\n',
      'event: reasoning\ndata: {"text":"think"}\n\n',
      'event: shiny_future_event\ndata: {"anything":true}\n\n',
      'event: step\ndata: {"id":1,"title":"Plan","status":"running"}\n\n',
      'event: token\ndata: {"text":"Hello"}\n\n',
      'event: meta\ndata: {"route":"agent","steps":[{"id":1,"title":"Plan","status":"done"}]}\n\n',
      'event: done\ndata: {}\n\n',
    ]);
    const got: ChatStreamEvent[] = [];
    for await (const ev of readChatStream(body)) got.push(ev);
    expect(got.map((e) => e.kind)).toEqual([
      'reasoning',
      'step',
      'token',
      'meta',
      'done',
    ]);
  });
});

describe('mergeStep / foldStreamState (V2 §4d/§4e helpers)', () => {
  it('mergeStep appends new ids and updates existing ones in place', () => {
    let steps = mergeStep(undefined, { id: 1, title: 'Plan', status: 'running' });
    steps = mergeStep(steps, { id: 2, title: 'Query', status: 'running' });
    steps = mergeStep(steps, {
      id: 1,
      title: 'Plan',
      status: 'done',
      detail: '3 steps',
    });
    expect(steps).toHaveLength(2);
    expect(steps[0]).toEqual({
      id: 1,
      title: 'Plan',
      status: 'done',
      detail: '3 steps',
    });
    expect(steps[1].status).toBe('running');
  });

  it('mergeStep keeps an earlier detail when the update carries none', () => {
    let steps = mergeStep(undefined, {
      id: 1,
      title: 'Query',
      status: 'running',
      detail: 'SELECT …',
    });
    steps = mergeStep(steps, { id: 1, title: 'Query', status: 'done' });
    expect(steps[0].detail).toBe('SELECT …');
    expect(steps[0].status).toBe('done');
  });

  it('foldStreamState stores reasoning + seconds into meta for history', () => {
    const meta: Meta = { route: 'sql', sql: 'SELECT 1' };
    const folded = foldStreamState(meta, {
      reasoning: 'thought about it',
      reasoningSeconds: 4,
    });
    expect(folded.reasoning).toBe('thought about it');
    expect(folded.reasoning_seconds).toBe(4);
    expect(folded.sql).toBe('SELECT 1'); // untouched
    expect(meta.reasoning).toBeUndefined(); // input not mutated
  });

  it('foldStreamState merges live step detail with final meta statuses', () => {
    const live: AgentStep[] = [
      { id: 1, title: 'Plan', status: 'running', detail: 'the plan' },
      { id: 2, title: 'Query', status: 'running' },
    ];
    const folded = foldStreamState(
      {
        route: 'agent',
        steps: [
          { id: 1, title: 'Plan', status: 'done' },
          { id: 2, title: 'Query', status: 'failed' },
          { id: 3, title: 'Extra', status: 'done' },
        ],
      },
      { steps: live },
    );
    expect(folded.steps).toEqual([
      { id: 1, title: 'Plan', status: 'done', detail: 'the plan' },
      { id: 2, title: 'Query', status: 'failed' },
      { id: 3, title: 'Extra', status: 'done' },
    ]);
  });
});
