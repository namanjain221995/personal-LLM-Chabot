// @vitest-environment jsdom
/**
 * MessageRow renders generated-file cards from `meta.artifacts` — the only
 * place they can live and survive a reload (lib/history.ts restores meta
 * verbatim and nothing else) — and renders nothing of the kind when the
 * key is absent. A non-terminal ref polls its status URL from the row, so a
 * job that outlived its chat turn still finishes on screen.
 */
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { OpenArtifact } from '@/components/artifacts/ArtifactCards';
import { MessageRow } from '@/components/MessageRow';
import type { ArtifactRef, ChatMessage, Meta } from '@/lib/types';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

const ID = 'a3f9c2d1e4b5f6a7b8c9d0e1f2a3b4c5';
const JOB = 'ffffffffffffffffffffffffffffffff';

const ref = (over: Partial<ArtifactRef> = {}): ArtifactRef => ({
  artifact_id: ID,
  version: 1,
  job_id: JOB,
  title: 'Onboarding SOP',
  kind: 'document',
  status: 'completed',
  files: [
    {
      format: 'docx',
      filename: 'onboarding-sop-v1.docx',
      mime_type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
      size: 40_000,
      pages: 4,
      download_url: `/artifacts/${ID}/v/1/file/docx?disposition=attachment`,
      inline_url: `/artifacts/${ID}/v/1/file/docx?disposition=inline`,
    },
  ],
  preview_kind: 'pages',
  preview_pages: 4,
  preview_url: `/artifacts/${ID}/v/1/preview`,
  thumbnail_url: `/artifacts/${ID}/v/1/preview/1.png?w=240`,
  warnings: [],
  created_at: '2026-09-11T10:00:00Z',
  operation: 'create',
  status_url: `/artifacts/jobs/${JOB}`,
  ...over,
});

const answer = (meta: Meta): ChatMessage => ({
  id: 'a1',
  role: 'assistant',
  content: 'Here is your SOP.',
  status: 'done',
  createdAt: 0,
  meta,
});

function renderAnswer(meta: Meta, onOpenArtifact?: OpenArtifact) {
  return render(
    <MessageRow
      message={answer(meta)}
      isLast
      onRegenerate={vi.fn()}
      onRetry={vi.fn()}
      onOpenArtifact={onOpenArtifact}
    />,
  );
}

describe('MessageRow · meta.artifacts', () => {
  it('renders one group per ref, one card per file, and keeps the proof drawer for the same meta', () => {
    // CONTRACT-2 §9: one FileCard per file under a version header.
    renderAnswer({ route: 'artifact', artifacts: [ref()], sql: 'SELECT 1' });
    expect(screen.getAllByTestId('artifact-card').length).toBe(1);
    expect(screen.getAllByTestId('file-card').length).toBe(1);
    expect(screen.getByRole('button', { name: /Open Onboarding SOP/ })).toBeTruthy();
    // The proof drawer is untouched: its SQL section is still offered.
    expect(screen.getByText('View SQL')).toBeTruthy();
  });

  it('renders legacy report_files through the SAME card component, and a file only once', () => {
    // CONTRACT-2 §9: `meta.report_files` render through FileCard via the
    // legacy adapter (inside the drawer's Files section), never twice for
    // one file — a name that is also an artifact file is left to the
    // artifact's own card.
    renderAnswer({
      route: 'report',
      report_files: [
        { filename: 'pipeline-review.docx', type: 'docx', size: 48_213 },
        { filename: 'onboarding-sop-v1.docx', type: 'docx', size: 40_000 },
      ],
      artifacts: [ref()],
    });
    // The artifact's card in the thread …
    expect(screen.getAllByTestId('artifact-card').length).toBe(1);
    // … and the drawer offers only the report file the artifact does not cover.
    expect(screen.getByRole('button', { name: 'Files (1)' })).toBeTruthy();
    const cards = screen.getAllByTestId('file-card');
    expect(cards.length).toBe(2);
    // The drawer sits above the artifact cards in the row.
    expect(cards.map((c) => c.getAttribute('data-file-key'))).toEqual([
      'legacy:pipeline-review.docx',
      `${ID}:1:docx:onboarding-sop-v1.docx`,
    ]);
    expect(screen.getByRole('link', { name: 'Download pipeline-review.docx' }).getAttribute('href')).toBe(
      '/api/reports/pipeline-review.docx',
    );
    expect(screen.getAllByRole('link', { name: 'Download onboarding-sop-v1.docx' }).length).toBe(1);
  });

  it('renders no card section when the key is absent or empty', () => {
    const { unmount } = renderAnswer({ route: 'chat' });
    expect(screen.queryByTestId('artifact-cards')).toBeNull();
    unmount();
    renderAnswer({ route: 'artifact', artifacts: [] });
    expect(screen.queryByTestId('artifact-cards')).toBeNull();
  });

  it('hands Open to the host with the ref, the card id, the file key and the message siblings', () => {
    // CONTRACT-2 §9: the panel opens on a (group, fileKey); the card id is
    // the FILE card's (was `artifact-card-<id>-v1` for the version card).
    const onOpen = vi.fn();
    renderAnswer({ route: 'artifact', artifacts: [ref()] }, onOpen);
    fireEvent.click(screen.getByRole('button', { name: /Open Onboarding SOP/ }));
    expect(onOpen).toHaveBeenCalledWith(
      expect.objectContaining({ artifact_id: ID }),
      `artifact-file-${ID}_1_docx_onboarding-sop-v1_docx`,
      `${ID}:1:docx:onboarding-sop-v1.docx`,
      [expect.objectContaining({ artifact_id: ID })],
    );
  });

  it('marks the card of the file the panel is showing', () => {
    render(
      <MessageRow
        message={answer({ route: 'artifact', artifacts: [ref()] })}
        isLast
        onRegenerate={vi.fn()}
        onRetry={vi.fn()}
        activeArtifactKey={`${ID}:1:docx:onboarding-sop-v1.docx`}
      />,
    );
    expect(screen.getByRole('button', { name: /Open Onboarding SOP/ }).getAttribute('aria-current')).toBe('true');
  });

  it('still renders and downloads without a host panel', () => {
    renderAnswer({ route: 'artifact', artifacts: [ref()] });
    expect(screen.getByRole('link', { name: 'Download onboarding-sop-v1.docx' })).toBeTruthy();
    // Open without a host is a no-op, not a crash.
    fireEvent.click(screen.getByRole('button', { name: /Open Onboarding SOP/ }));
  });
});

describe('MessageRow · no "Memory updated" chip on an artifact turn', () => {
  it('renders no chip when the meta carries no memory_updated — nothing is synthesised', () => {
    // CONTRACT-2 §8/§9: the backend no longer sends `memory_updated` on an
    // artifact turn and the frontend must not invent one.
    renderAnswer({ route: 'artifact', artifacts: [ref()], effort: 'think', model: 'qwen' });
    expect(screen.queryByText('Memory updated')).toBeNull();
    const { unmount } = renderAnswer({ route: 'artifact', artifacts: [ref()], memory_updated: [] });
    expect(screen.queryByText('Memory updated')).toBeNull();
    unmount();
  });

  it('still renders the chip exactly as before when a turn does carry facts', () => {
    renderAnswer({ route: 'chat', memory_updated: ['Prefers concise answers'] });
    expect(screen.getByText('Memory updated').getAttribute('title')).toBe('Prefers concise answers');
  });
});

describe('MessageRow · a ref the meta left non-terminal', () => {
  beforeEach(() => vi.useFakeTimers());

  it('polls the status URL until terminal, then shows the refreshed ref', async () => {
    const answers = [
      { ok: true, status: 200, body: { job_id: JOB, artifact_id: ID, version: 1, status: 'running', stage: 'render', stage_title: 'Building the files' } },
      { ok: true, status: 200, body: { job_id: JOB, artifact_id: ID, version: 1, status: 'completed', artifact: ref({ status: 'completed' }) } },
    ];
    const fetchMock = vi.fn(async (url: string) => {
      expect(url).toBe(`/api/artifacts/jobs/${JOB}`);
      const next = answers.shift() ?? answers[0];
      return { ok: next!.ok, status: next!.status, json: async () => next!.body } as unknown as Response;
    });
    vi.stubGlobal('fetch', fetchMock);

    renderAnswer({ route: 'artifact', artifacts: [ref({ status: 'running', files: [] })] });
    // Before any answer: the card is honest about not knowing the stage.
    expect(screen.getByTestId('artifact-status').textContent).toContain('Working');
    expect(screen.queryByTestId('artifact-download')).toBeNull();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('artifact-status').textContent).toContain('Building the files');

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    // waitFor polls with REAL timers, which fake timers never advance —
    // flushing the microtask queue under act is what lets the state land.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByTestId('artifact-status').textContent).toContain('Ready');
    expect(screen.getByRole('link', { name: 'Download onboarding-sop-v1.docx' })).toBeTruthy();

    // Terminal: nothing else is asked.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('keeps a polled "Ready" when a history reload hands it an equal-valued new ref object', async () => {
    // A forced history load (streams.ts adoptPersistedAnswer, ChatApp
    // loadInto(id, true)) rebuilds every message, and reconcileThread returns
    // `{...s, meta: s.meta}` — a new object, the same stale non-terminal
    // snapshot. Keyed on the object, the hook's reset adopted that snapshot
    // without restarting the poll, and the card fell back to "Working…"
    // for good.
    const completed = ref({ status: 'completed' });
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({ job_id: JOB, artifact_id: ID, version: 1, status: 'completed', artifact: completed }),
    })) as unknown as typeof fetch;
    vi.stubGlobal('fetch', fetchMock);

    const running = (): Meta => ({ route: 'artifact', artifacts: [ref({ status: 'running', files: [] })] });
    const { rerender } = render(
      <MessageRow message={answer(running())} isLast onRegenerate={vi.fn()} onRetry={vi.fn()} />,
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('artifact-status').textContent).toContain('Ready');

    // The same values, a different object — and the message object too.
    rerender(<MessageRow message={answer(running())} isLast onRegenerate={vi.fn()} onRetry={vi.fn()} />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(screen.getByTestId('artifact-status').textContent).toContain('Ready');
    expect(screen.getByRole('link', { name: 'Download onboarding-sop-v1.docx' })).toBeTruthy();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('adopts the meta when its status actually changed — the final meta landing on a live turn', async () => {
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({ job_id: JOB, artifact_id: ID, version: 1, status: 'running', stage: 'render' }),
    })) as unknown as typeof fetch;
    vi.stubGlobal('fetch', fetchMock);
    const { rerender } = render(
      <MessageRow
        message={answer({ route: 'artifact', artifacts: [ref({ status: 'running', files: [] })] })}
        isLast
        onRegenerate={vi.fn()}
        onRetry={vi.fn()}
      />,
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);

    rerender(
      <MessageRow
        message={answer({ route: 'artifact', artifacts: [ref({ status: 'completed_with_warnings', warnings: ['One note.'] })] })}
        isLast
        onRegenerate={vi.fn()}
        onRetry={vi.fn()}
      />,
    );
    expect(screen.getByTestId('artifact-status').textContent).toContain('Ready · 1 note');
    // Terminal now: the old poll was aborted and no new one started.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('does not poll at all for a terminal ref', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    renderAnswer({ route: 'artifact', artifacts: [ref()] });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('stops polling when the row unmounts', async () => {
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({ job_id: JOB, artifact_id: ID, version: 1, status: 'running' }),
    })) as unknown as typeof fetch;
    vi.stubGlobal('fetch', fetchMock);
    const { unmount } = renderAnswer({ route: 'artifact', artifacts: [ref({ status: 'queued', files: [] })] });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    unmount();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
