/**
 * lib/recordings: the "Your recordings" page's conversation with the session
 * routes — parsing the list, building the paging URL, and the sentences for
 * each refusal. The page-level behaviour is in recordings-page.test.tsx.
 */

import { describe, expect, it, vi } from 'vitest';
import {
  appendPage,
  deleteRecording,
  loadTranscript,
  parseRecordingPage,
  recordingsListUrl,
  removedByRetention,
  type Recording,
} from '@/lib/recordings';

const ID = 'ab'.repeat(16);

function reply(body: unknown, status = 200): Response {
  return new Response(body === null ? null : JSON.stringify(body), {
    status,
    headers: body === null ? {} : { 'content-type': 'application/json' },
  });
}

const fetchOnce = (res: Response | Error) =>
  vi.fn(async () => {
    if (res instanceof Error) throw res;
    return res;
  }) as unknown as typeof fetch;

describe('parseRecordingPage', () => {
  it('reads the list shape dictation.list_sessions returns', () => {
    const page = parseRecordingPage({
      sessions: [
        {
          session_id: ID,
          created_at: '2026-09-29T04:05:06.123456+00:00',
          status: 'done',
          outcome: 'transcribed',
          audio_ms: 61_000,
          bytes: 980_000,
          mime_type: 'audio/webm',
          delete_after: null,
          preview: 'hello there',
        },
      ],
      next_before: '2026-09-29T04:05:06.123456+00:00',
    });
    expect(page).toEqual({
      recordings: [
        {
          id: ID,
          createdAt: '2026-09-29T04:05:06.123456+00:00',
          status: 'done',
          outcome: 'transcribed',
          audioMs: 61_000,
          bytes: 980_000,
          mimeType: 'audio/webm',
          deleteAfter: null,
          preview: 'hello there',
        },
      ],
      nextBefore: '2026-09-29T04:05:06.123456+00:00',
    });
  });

  it('drops rows it could not play or delete, and cancelled ones', () => {
    const good = { session_id: ID, created_at: '2026-09-29T00:00:00+00:00', status: 'failed' };
    const page = parseRecordingPage({
      sessions: [
        good,
        { ...good, session_id: '../../etc/passwd' },
        { ...good, session_id: 'CD'.repeat(16) },
        { ...good, status: 'cancelled' },
        { ...good, created_at: null },
        'nonsense',
      ],
      next_before: null,
    });
    expect(page?.recordings.map((r) => r.id)).toEqual([ID]);
    expect(page?.recordings[0]?.preview).toBeNull();
  });

  it('refuses a body that is not a list page', () => {
    expect(parseRecordingPage({ detail: 'Not Found' })).toBeNull();
    expect(parseRecordingPage(null)).toBeNull();
  });
});

describe('recordingsListUrl', () => {
  it('encodes the cursor, whose "+" would otherwise arrive as a space', () => {
    expect(recordingsListUrl(null)).toBe('/api/audio/sessions?limit=20');
    expect(recordingsListUrl('2026-09-29T04:05:06.123456+00:00')).toBe(
      '/api/audio/sessions?limit=20&before=2026-09-29T04%3A05%3A06.123456%2B00%3A00',
    );
  });
});

describe('appendPage', () => {
  it('never shows a row twice', () => {
    const r = (id: string) => ({ id }) as Recording;
    expect(appendPage([r('a'), r('b')], [r('b'), r('c')]).map((x) => x.id)).toEqual(['a', 'b', 'c']);
  });
});

describe('removedByRetention', () => {
  const rec = (deleteAfter: string | null) => ({ deleteAfter }) as Recording;
  it('is true only once delete_after has passed', () => {
    const now = Date.parse('2026-09-29T12:00:00Z');
    expect(removedByRetention(rec(null), now)).toBe(false);
    expect(removedByRetention(rec('2026-10-29T12:00:00+00:00'), now)).toBe(false);
    expect(removedByRetention(rec('2026-09-28T12:00:00+00:00'), now)).toBe(true);
    expect(removedByRetention(rec('not a time'), now)).toBe(false);
  });
});

describe('deleteRecording', () => {
  it('counts 204 and 404 as deleted', async () => {
    expect(await deleteRecording(fetchOnce(reply(null, 204)), ID)).toEqual({ kind: 'deleted' });
    expect(
      await deleteRecording(fetchOnce(reply({ detail: 'gone', reason: 'not_found' }, 404)), ID),
    ).toEqual({ kind: 'deleted' });
  });

  it('never reads a failure as a deletion', async () => {
    const cases: [Response | Error, string][] = [
      [new TypeError('Failed to fetch'), "Not deleted. The server couldn't be reached."],
      [reply({ detail: 'The recording service is unreachable.', reason: 'proxy_unreachable' }, 502), "Not deleted. The server couldn't be reached."],
      [new Response('<html>Bad gateway</html>', { status: 502 }), "Not deleted. The server couldn't be reached."],
      [reply({ detail: 'Not signed in' }, 401), 'Not deleted. You were signed out.'],
      [reply({ detail: 'boom', reason: 'x' }, 500), 'Not deleted. The server answered with error 500: "boom"'],
    ];
    for (const [res, start] of cases) {
      const result = await deleteRecording(fetchOnce(res), ID);
      expect(result.kind).toBe('not_deleted');
      expect(result.kind === 'not_deleted' && result.message.startsWith(start)).toBe(true);
    }
  });

  it('sends one DELETE to the recording, and nothing else', async () => {
    const fn = fetchOnce(reply(null, 204));
    await deleteRecording(fn, ID);
    expect(fn).toHaveBeenCalledTimes(1);
    expect((fn as unknown as ReturnType<typeof vi.fn>).mock.calls[0]).toEqual([
      `/api/audio/sessions/${ID}`,
      expect.objectContaining({ method: 'DELETE' }),
    ]);
  });
});

describe('loadTranscript', () => {
  const state = (over: Record<string, unknown>) => ({
    session_id: ID,
    status: 'done',
    text: 'the whole transcript',
    gaps: [],
    outcome: 'transcribed',
    ...over,
  });

  it('returns the full text of a finished recording', async () => {
    expect(await loadTranscript(fetchOnce(reply(state({}))), ID)).toEqual({
      kind: 'text',
      text: 'the whole transcript',
      note: null,
    });
  });

  it('names the stretches the engine could not transcribe, not the ones dropped as noise', async () => {
    const result = await loadTranscript(
      fetchOnce(
        reply(
          state({
            outcome: 'transcribed_with_gaps',
            gaps: [
              { start_ms: 250_000, end_ms: 280_000, reason: 'engine_unavailable' },
              { start_ms: 400_000, end_ms: 410_000, reason: 'dropped_as_noise' },
            ],
          }),
        ),
      ),
      ID,
    );
    expect(result.kind).toBe('text');
    expect(result.kind === 'text' && result.note).toContain('4:10');
    expect(result.kind === 'text' && result.note).not.toContain('6:40');
  });

  it('says why there is no text', async () => {
    expect(await loadTranscript(fetchOnce(reply(state({ text: '', outcome: 'no_speech' }))), ID)).toEqual({
      kind: 'none',
      message: 'No speech was detected in this recording.',
    });
    expect(
      await loadTranscript(
        fetchOnce(reply(state({ status: 'failed', text: null, error: { reason: 'x', detail: 'The engine refused it.' } }))),
        ID,
      ),
    ).toEqual({ kind: 'none', message: 'The engine refused it. The audio is kept.' });
    const gone = await loadTranscript(fetchOnce(reply({ detail: 'gone', reason: 'not_found' }, 404)), ID);
    expect(gone).toEqual({
      kind: 'failed',
      message: 'This recording is no longer on the server. It was discarded or has expired.',
    });
  });
});
