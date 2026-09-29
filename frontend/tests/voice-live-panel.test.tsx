// @vitest-environment jsdom
/**
 * The transcript lines above the recording bar (components/VoiceBar.tsx
 * SessionPanel), with the durable preview and the live words.
 *
 * THE BUG (audit, 2026-09-29): the preview was `line-clamp-3`, which keeps the
 * FIRST three lines. Past three lines — the preview holds up to 600
 * characters — the words being spoken now were the ones hidden. Measured in
 * Chromium: the newest words sat at y=356 in a box that ended at y=74. jsdom
 * has no layout, so what is held here is the box's construction (a column
 * justified to its end, four lines tall at most, overflow hidden, nothing
 * clamped); the layout itself was checked in a real Chromium against the dev
 * server when this changed (the newest words inside the box, the oldest cut
 * at its top).
 */
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { VoiceBar } from '@/components/VoiceBar';
import type { SessionProgress } from '@/lib/voice';

afterEach(() => cleanup());

const progress = (over: Partial<SessionProgress> = {}): SessionProgress => ({
  preview: '',
  tentative: '',
  audioMs: 0,
  backlogMs: 0,
  waitingOn: 'none',
  pendingParts: 0,
  pendingMs: 0,
  offline: false,
  storageTrouble: false,
  lastAckAt: null,
  retentionDays: 0,
  progressive: true,
  ...over,
});

function bar(p: SessionProgress | null, state: 'recording' | 'finishing' = 'recording') {
  return render(
    <VoiceBar
      state={state}
      levels={[]}
      elapsedMs={65_000}
      maxMs={null}
      progress={p}
      onCancel={vi.fn()}
      onStop={vi.fn()}
    />,
  );
}

/** 600 characters of preview: well past four lines of the composer. */
const LONG = Array.from({ length: 60 }, (_, i) => `word${String(i).padStart(3, '0')}`).join(' ').slice(0, 600);

describe('the transcript above the recording bar', () => {
  it('keeps the NEWEST words in view: bottom-anchored, four lines at most, nothing clamped', () => {
    const view = bar(progress({ preview: LONG, tentative: 'and the newest' }));
    const text = screen.getByText('and the newest').closest('p')!;
    // No clamp on the transcript or anything around it: a clamp keeps the
    // head and cuts the tail, which is exactly the words being spoken now.
    const clamped: string[] = [];
    for (let el: Element | null = text; el && el !== view.container; el = el.parentElement) {
      for (const c of el.getAttribute('class')?.split(' ') ?? []) if (c.startsWith('line-clamp')) clamped.push(c);
    }
    expect(clamped).toEqual([]);
    // Anchored to its bottom edge, at most four of the text's own lines
    // (leading-5 = 1.25rem; max-h-20 = 5rem), the overflow cut at the top.
    const box = text.parentElement!;
    expect(box.getAttribute('data-testid')).toBe('voice-transcript');
    expect(box.className.split(' ')).toEqual(
      expect.arrayContaining(['flex', 'flex-col', 'justify-end', 'overflow-hidden', 'max-h-20']),
    );
    expect(text.className.split(' ')).toEqual(expect.arrayContaining(['text-sm', 'leading-5', 'text-ink']));
    expect(text.getAttribute('dir')).toBe('auto');
    // The newest words are the last thing in the box.
    expect(text.textContent!.endsWith('and the newest')).toBe(true);
  });

  it('draws the pieces in the order they were spoken, committed in ink and the rest muted', () => {
    bar(
      progress({
        preview: 'The stored words.',
        tentative: 'still held',
        live: { committed: 'Heard live.', partial: 'being said' },
      }),
    );
    const text = screen.getByTestId('voice-transcript').querySelector('p')!;
    const spans = [...text.querySelectorAll('span')];
    expect(spans.map((s) => s.textContent)).toEqual([
      'The stored words.',
      ' still held',
      ' Heard live.',
      ' being said',
    ]);
    expect(spans.map((s) => s.className.includes('text-muted'))).toEqual([false, true, false, true]);
    expect(text.textContent).toBe('The stored words. still held Heard live. being said');
  });

  it('shows live words before the stored recording has any', () => {
    bar(progress({ live: { committed: '', partial: 'first words' } }));
    const text = screen.getByTestId('voice-transcript').querySelector('p')!;
    expect(text.textContent).toBe('first words');
    expect(screen.getByText('first words').className).toContain('text-muted');
  });

  it('keeps showing the live words while the recording finishes', () => {
    bar(progress({ preview: 'Stored.', live: { committed: 'Last live words.', partial: '' } }), 'finishing');
    expect(screen.getByTestId('voice-transcript').textContent).toBe('Stored. Last live words.');
  });

  it('joins scripts written without spaces without adding any', () => {
    bar(progress({ preview: '今日は', live: { committed: '晴れです', partial: 'ね' } }));
    expect(screen.getByTestId('voice-transcript').textContent).toBe('今日は晴れですね');
  });

  it('is not announced: outside the status region, and not a live region itself', () => {
    bar(progress({ preview: 'x', live: { committed: 'y', partial: 'z' } }));
    const box = screen.getByTestId('voice-transcript');
    expect(box.closest('[aria-live]')).toBeNull();
    expect(box.closest('[role="status"]')).toBeNull();
    expect(screen.getByRole('status').contains(box)).toBe(false);
  });

  it('draws no transcript box when there are no words', () => {
    bar(progress());
    expect(screen.queryByTestId('voice-transcript')).toBeNull();
    const view = bar(null);
    expect(view.container.querySelector('[data-testid="voice-transcript"]')).toBeNull();
  });
});
