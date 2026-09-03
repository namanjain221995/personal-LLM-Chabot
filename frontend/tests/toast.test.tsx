// @vitest-environment jsdom
/**
 * The toast: neutral by design, and one per identical message (2026-09-03).
 *
 * Two owner requests in one place. The error pill was a red block over
 * messages like "re-attach the file", which is a note, not an alarm; and the
 * same failing click five times stacked five copies of it. Both are decided
 * in the provider — dedupe here means no call site needs a guard.
 */

import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Providers, useToast } from '@/components/Providers';

function Trigger() {
  const { toast } = useToast();
  return (
    <>
      <button type="button" onClick={() => toast('Re-attach the file to edit this message', 'error')}>
        fail A
      </button>
      <button type="button" onClick={() => toast('File B is unavailable', 'error')}>
        fail B
      </button>
      <button type="button" onClick={() => toast('Re-attach the file to edit this message')}>
        info A
      </button>
      <button type="button" onClick={() => toast('  Re-attach the file   to edit this message ', 'error')}>
        fail A spaced
      </button>
    </>
  );
}

const renderToasts = () =>
  render(
    <Providers>
      <Trigger />
    </Providers>,
  );

const click = (name: string, times = 1) => {
  for (let i = 0; i < times; i += 1) {
    fireEvent.click(screen.getByRole('button', { name }));
  }
};
const alerts = () => screen.queryAllByRole('alert');

beforeEach(() => {
  vi.useFakeTimers();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe('TOAST-DEDUPE', () => {
  it('01 · one trigger, one toast', () => {
    renderToasts();
    act(() => click('fail A'));
    expect(alerts()).toHaveLength(1);
  });

  it('02 · five rapid identical triggers, still one toast', () => {
    renderToasts();
    act(() => click('fail A', 5));
    expect(alerts()).toHaveLength(1);
    expect(screen.getAllByText('Re-attach the file to edit this message')).toHaveLength(1);
  });

  it('03 · two DIFFERENT errors both show', () => {
    renderToasts();
    act(() => {
      click('fail A');
      click('fail B');
    });
    expect(alerts()).toHaveLength(2);
  });

  it('04 · after the toast has gone, the same error may appear again', () => {
    renderToasts();
    act(() => click('fail A'));
    expect(alerts()).toHaveLength(1);
    act(() => {
      vi.advanceTimersByTime(5300);
    });
    expect(alerts()).toHaveLength(0);
    act(() => click('fail A'));
    expect(alerts()).toHaveLength(1);
  });

  it('05 · the same words as an INFO are a different notification', () => {
    renderToasts();
    act(() => {
      click('fail A');
      click('info A');
    });
    expect(alerts()).toHaveLength(1);
    expect(screen.getAllByText('Re-attach the file to edit this message')).toHaveLength(2);
  });

  it('whitespace differences are the same message', () => {
    renderToasts();
    act(() => {
      click('fail A');
      click('fail A spaced');
    });
    expect(alerts()).toHaveLength(1);
  });

  it('re-triggering restarts the timer rather than stacking', () => {
    renderToasts();
    act(() => click('fail A'));
    act(() => {
      vi.advanceTimersByTime(4000);
    });
    act(() => click('fail A'));
    act(() => {
      vi.advanceTimersByTime(4000);
    });
    // 8s after the first trigger, 4s after the second: still up, still one.
    expect(alerts()).toHaveLength(1);
    act(() => {
      vi.advanceTimersByTime(1300);
    });
    expect(alerts()).toHaveLength(0);
  });

  it('06 · deduplication is central — the call site carries no guard', () => {
    // The trigger above is the naive call, five times. That it produced one
    // toast is the whole proof; this pins that no call site in the app had to
    // grow a guard of its own for it.
    const src = readFileSync(
      join(process.cwd(), 'components/ChatApp.tsx'),
      'utf8',
    );
    expect(src).not.toMatch(/lastToast|toastShown|alreadyToasted/);
  });
});

describe('TOAST-STYLE', () => {
  it('ERROR-TOAST-01/02/04 · a light card with near-black text, and no danger red', () => {
    // Neutral was quiet enough to miss (owner, 2026-09-03). `paper`/`navy`
    // rather than `surface`/`ink`: those two are declared once in :root and
    // never re-declared per theme, so this card stays light-with-dark-text in
    // BOTH themes rather than following the page into the dark.
    renderToasts();
    act(() => click('fail A'));
    const el = alerts()[0];
    expect(el.className).toContain('bg-paper');
    expect(el.className).toContain('text-navy');
    expect(el.className).not.toMatch(/bg-danger|text-danger|border-danger/);
    expect(el.className).not.toContain('bg-surface');
  });

  it('ERROR-TOAST-03 · it carries a border and a heavier shadow than an info toast', () => {
    // In light mode a near-white card on a white page is 1.06:1 — the border
    // and the elevation are the only things separating it from the page.
    renderToasts();
    act(() => {
      click('fail A');
      click('info A');
    });
    const error = alerts()[0];
    expect(error.className).toMatch(/border-black\/\d+/);
    expect(error.className).toContain('shadow-xl');
    const info = document.querySelector('[data-tone="info"]') as HTMLElement;
    expect(info.className).toContain('shadow-lg');
    expect(info.className).not.toContain('shadow-xl');
  });

  it('ERROR-TOAST-09 · the info toast keeps the themed neutral surface', () => {
    // A routine "Uploaded 4 documents." is not an alarm and was not asked to
    // change; only the error treatment did.
    renderToasts();
    act(() => click('info A'));
    const info = document.querySelector('[data-tone="info"]') as HTMLElement;
    expect(info.className).toContain('bg-surface');
    expect(info.className).toContain('border-border');
    expect(info.className).toContain('text-ink');
    expect(info.className).not.toContain('bg-paper');
  });

  it('the tokens the error card uses do not flip with the theme', () => {
    // The claim above, checked against the stylesheet rather than assumed:
    // --ts-paper and --ts-navy must be declared exactly once, in :root.
    const css = readFileSync(join(process.cwd(), 'app/globals.css'), 'utf8');
    expect(css.match(/--ts-paper:/g)).toHaveLength(1);
    expect(css.match(/--ts-navy:/g)).toHaveLength(1);
  });

  it('ERROR-TOAST-02 · the marker inherits the card ink instead of the muted token', () => {
    // It used to be `text-muted`, which flips with the theme and would sit at
    // #b3b3b3 on this now-always-light card.
    renderToasts();
    act(() => click('fail A'));
    const marker = alerts()[0].querySelector('span[aria-hidden]') as HTMLElement;
    expect(marker.textContent).toBe('!');
    expect(marker.className).not.toContain('text-muted');
  });

  it('ERROR-TOAST-05 · an error is still announced as one', () => {
    renderToasts();
    act(() => {
      click('fail A');
      click('info A');
    });
    // The error row is an alert; the info row is not.
    expect(alerts()).toHaveLength(1);
    expect(screen.getByRole('status')).toBeTruthy();
    expect(screen.getByRole('status').getAttribute('aria-live')).toBe('polite');
  });

  it('ERROR-TOAST-06 · danger styling outside the toast is untouched', () => {
    const composer = readFileSync(
      join(process.cwd(), 'components/Composer.tsx'),
      'utf8',
    );
    const css = readFileSync(
      join(process.cwd(), 'app/globals.css'),
      'utf8',
    );
    expect(composer).toContain('bg-danger/15 text-danger');
    expect(css).toContain('--ts-danger: #ef5a5f');
  });
});
