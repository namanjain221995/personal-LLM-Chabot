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
  it('01/02 · an error toast wears the neutral surface, not the danger red', () => {
    renderToasts();
    act(() => click('fail A'));
    const el = alerts()[0];
    expect(el.className).toContain('bg-surface');
    expect(el.className).toContain('border-border');
    expect(el.className).toContain('text-ink');
    expect(el.className).not.toMatch(/bg-danger|text-danger|border-danger/);
  });

  it('03/04 · text tokens are the page\'s own ink, readable in both themes by construction', () => {
    // text-ink / bg-surface are the theme tokens every other panel uses; the
    // contrast of that pair is already pinned per theme in accent-palette.
    renderToasts();
    act(() => click('fail A'));
    expect(alerts()[0].className).toContain('text-ink');
  });

  it('05 · an error is still announced as one', () => {
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

  it('06 · danger styling outside the toast is untouched', () => {
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
