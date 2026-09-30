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
 *
 * Also here (2026-09-30): the live transcript's language control on the
 * panel's last row, and the follow-up line after a recording, whose actions
 * now wrap under the message instead of squeezing it (both measured in
 * headless Chromium with the app's compiled CSS at 375, 390 and 800 px).
 */
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { VoiceBar, VoiceFollowUpLine } from '@/components/VoiceBar';
import { VOICE_MESSAGES, type SessionProgress } from '@/lib/voice';
import { LIVE_INSERT_PARTIAL_LABEL, type VoiceLanguage } from '@/lib/voiceLive';

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

  // Spec 14.3 (2026-09-30): the engine keeps the danda or comma that ends an
  // utterance on the next one; joined with a space it read "है । फिर".
  it('joins a piece that starts with the punctuation closing the one before straight onto it', () => {
    bar(progress({ preview: 'जंगल में है', tentative: '। फिर हम', live: { committed: ', और घर गए', partial: '। अब' } }));
    const text = screen.getByTestId('voice-transcript').querySelector('p')!;
    expect(text.textContent).toBe('जंगल में है। फिर हम, और घर गए। अब');
    expect([...text.querySelectorAll('span')].map((s) => s.textContent)).toEqual([
      'जंगल में है',
      '। फिर हम',
      ', और घर गए',
      '। अब',
    ]);
    cleanup();
    bar(progress({ preview: 'I told them', live: { committed: 'and they agreed', partial: '… mostly' } }));
    expect(screen.getByTestId('voice-transcript').textContent).toBe('I told them and they agreed… mostly');
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

describe('the language control above the recording bar', () => {
  function withLanguage(
    language: VoiceLanguage | null,
    state: 'recording' | 'finishing' | 'requesting' = 'recording',
    p: SessionProgress | null = progress(),
  ) {
    const onLanguage = vi.fn();
    const view = render(
      <VoiceBar
        state={state}
        levels={[]}
        elapsedMs={65_000}
        maxMs={null}
        progress={p}
        language={language}
        onLanguage={onLanguage}
        onCancel={vi.fn()}
        onStop={vi.fn()}
      />,
    );
    return { onLanguage, view };
  }

  it('is one labelled group of three radio buttons, each language named in itself, the current one checked', () => {
    withLanguage('en');
    const group = screen.getByRole('radiogroup', { name: 'Language of the live transcript' });
    const radios = within(group).getAllByRole('radio') as HTMLInputElement[];
    expect(radios.map((r) => r.labels?.[0]?.textContent)).toEqual(['Auto', 'English', 'हिन्दी']);
    expect(radios.map((r) => r.checked)).toEqual([false, true, false]);
    expect(new Set(radios.map((r) => r.name)).size).toBe(1);
    expect(screen.getByText('हिन्दी').getAttribute('lang')).toBe('hi');
  });

  it('says the choice, and only a new one', () => {
    const { onLanguage } = withLanguage('auto');
    fireEvent.click(screen.getByRole('radio', { name: 'हिन्दी' }));
    expect(onLanguage).toHaveBeenCalledWith('hi');
    onLanguage.mockClear();
    fireEvent.click(screen.getByRole('radio', { name: 'Auto' }));
    expect(onLanguage).not.toHaveBeenCalled();
  });

  it('is outside the status row, the one live region', () => {
    withLanguage('auto');
    const group = screen.getByRole('radiogroup');
    expect(group.closest('[aria-live]')).toBeNull();
    expect(screen.getByRole('status').contains(group)).toBe(false);
  });

  it('shares its row with the saved line, and needs no transcript to be drawn', () => {
    withLanguage('auto', 'recording', progress({ savedMs: 5000 }));
    const group = screen.getByRole('radiogroup');
    const saved = screen.getByText(/^Saved to your account/).closest('p')!;
    expect(saved.parentElement).toBe(group.parentElement);
    cleanup();
    withLanguage('auto', 'recording', null);
    expect(screen.getByRole('radiogroup')).toBeTruthy();
  });

  it('uses the theme tokens, with no opacity modifier on them', () => {
    withLanguage('hi');
    const group = screen.getByRole('radiogroup');
    const classes = [group, ...group.querySelectorAll('*')].flatMap((el) => (el.getAttribute('class') ?? '').split(' '));
    expect(classes.filter((c) => /\/\d+$/.test(c))).toEqual([]);
    const chosen = screen.getByText('हिन्दी');
    expect(chosen.className.split(' ')).toEqual(
      expect.arrayContaining(['peer-checked:bg-surface-2', 'peer-checked:text-ink', 'peer-focus-visible:ring-2']),
    );
  });

  it('is drawn only while recording, and only with a language', () => {
    withLanguage('auto', 'finishing');
    expect(screen.queryByRole('radiogroup')).toBeNull();
    cleanup();
    withLanguage('auto', 'requesting', null);
    expect(screen.queryByRole('radiogroup')).toBeNull();
    cleanup();
    withLanguage(null);
    expect(screen.queryByRole('radiogroup')).toBeNull();
  });
});

describe('the follow-up line after a recording', () => {
  // Measured in Chromium at 390 px (311 px of composer), 2026-09-30: with
  // Retry and the partial-insert label beside it at full width, the message
  // was 0 px wide, one letter per line, 2,300 px tall, and the second button
  // ran off the edge. jsdom has no layout, so the construction is held here.
  it('keeps the message readable at any width: the actions wrap under it, and a long label inside its button', () => {
    render(
      <VoiceFollowUpLine
        followUp={{
          message: VOICE_MESSAGES.engineUnavailable('1:05'),
          tone: 'error',
          actionLabel: VOICE_MESSAGES.retry,
          busy: false,
          run: vi.fn(),
          dismiss: vi.fn(),
          secondaryLabel: LIVE_INSERT_PARTIAL_LABEL,
          runSecondary: vi.fn(),
        }}
      />,
    );
    const message = screen.getByText(VOICE_MESSAGES.engineUnavailable('1:05'));
    expect(message.className.split(' ')).toEqual(expect.arrayContaining(['flex-1', 'min-w-[min(12rem,100%)]']));
    const row = message.parentElement!;
    expect(row.className.split(' ')).toEqual(expect.arrayContaining(['flex', 'flex-wrap', 'min-w-0', 'flex-1']));
    const actions = screen.getByTestId('voice-follow-up-actions');
    expect(actions.parentElement).toBe(row);
    expect(actions.className.split(' ')).toEqual(expect.arrayContaining(['flex', 'flex-wrap', 'max-w-full']));
    for (const label of [VOICE_MESSAGES.retry, LIVE_INSERT_PARTIAL_LABEL]) {
      const button = screen.getByText(label).closest('button')!;
      expect(actions.contains(button)).toBe(true);
      expect(button.className.split(' ')).toEqual(expect.arrayContaining(['max-w-full', 'text-left']));
      expect(button.className.split(' ')).not.toContain('shrink-0');
    }
    // The dismiss stays at the top right, outside the row that wraps.
    const dismiss = screen.getByLabelText('Dismiss');
    expect(dismiss.parentElement).toBe(row.parentElement);
    expect(dismiss.className.split(' ')).toContain('shrink-0');
  });

  it('runs each action, and draws no action row when there is nothing to press', () => {
    const run = vi.fn();
    const runSecondary = vi.fn();
    const view = render(
      <VoiceFollowUpLine
        followUp={{ message: 'x', tone: 'info', actionLabel: 'Use the other one', busy: false, run, dismiss: vi.fn(), secondaryLabel: 'Other', runSecondary }}
      />,
    );
    fireEvent.click(screen.getByText('Use the other one'));
    fireEvent.click(screen.getByText('Other'));
    expect(run).toHaveBeenCalledTimes(1);
    expect(runSecondary).toHaveBeenCalledTimes(1);
    view.unmount();
    render(<VoiceFollowUpLine followUp={{ message: 'y', tone: 'info', actionLabel: null, busy: false, run, dismiss: vi.fn() }} />);
    expect(screen.queryByTestId('voice-follow-up-actions')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// What the browser's comments say was measured (build spec 14.4, 2026-09-30)
// ---------------------------------------------------------------------------

describe('the accuracy figures the browser code quotes', () => {
  // The first FLEURS-Hindi numbers (8.58% auto, 7.48% hi) were redone after
  // the scoring was fixed (a \w word class had split Hindi words apart):
  // 11.8% (auto) and 10.9% (hi) for the live model, 41.9% for whisper
  // (benchmarks/voice-live/README.md).
  it('are the redone FLEURS-Hindi ones, wherever they are quoted', async () => {
    const { readdirSync, readFileSync } = await import('node:fs');
    const { join } = await import('node:path');
    const root = process.cwd();
    const sources: string[] = [];
    const walk = (dir: string) => {
      for (const entry of readdirSync(join(root, dir), { withFileTypes: true })) {
        const path = join(dir, entry.name);
        if (entry.isDirectory()) walk(path);
        else if (/\.(tsx?|m?js|cjs)$/.test(entry.name)) sources.push(path);
      }
    };
    for (const dir of ['app', 'components', 'lib', 'public/voice']) walk(dir);
    expect(sources).toContain(join('components', 'VoiceBar.tsx'));
    const stale = sources.filter((path) => /\b(?:8\.58|7\.48)\s*%/.test(readFileSync(join(root, path), 'utf8')));
    expect(stale).toEqual([]);
    // The comment's own words, its line breaks and leading asterisks taken out.
    const comment = readFileSync(join(root, 'components', 'VoiceBar.tsx'), 'utf8').replace(/\s*\n\s*\*\s*/g, ' ');
    expect(comment).toContain('FLEURS-hi 10.9% against 11.8% on auto; whisper, the full pass, 41.9%');
  });
});
