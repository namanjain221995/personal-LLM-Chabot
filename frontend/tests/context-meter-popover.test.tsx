// @vitest-environment jsdom
/**
 * The ONE join neither the pure `contextMeter` tests nor a POST to
 * /chat/compact can see: can a human actually press "Compact now"?
 *
 * The popover is portalled to <body>, so it is NOT inside `buttonRef`. The
 * outside-click guard used to close on any `pointerdown` that missed the ring
 * button — which included every control inside the popover itself. The
 * popover unmounted between `pointerdown` and `click`, so the click never
 * landed on anything and the action was unreachable from the UI, while a
 * direct POST to the endpoint worked perfectly. That is exactly the shape of
 * bug an endpoint test cannot catch, so it is pinned here.
 */

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ContextMeter } from '@/components/ContextMeter';
import { NOTHING_TO_COMPACT, type MeterView } from '@/lib/contextMeter';

afterEach(cleanup);

const VIEW: MeterView = {
  fraction: 0.5,
  percent: 50,
  state: 'calm',
  pulsing: false,
  tokensUsed: 1000,
  usableBudget: 2000,
  breakdown: [{ label: 'Conversation', tokens: 1000 }],
};

function open(props: Partial<Parameters<typeof ContextMeter>[0]> = {}) {
  const onCompactNow = vi.fn();
  const onOpenChange = vi.fn();
  render(
    <ContextMeter
      view={VIEW}
      compacting={false}
      onCompactNow={onCompactNow}
      foldableTurns={12}
      onOpenChange={onOpenChange}
      {...props}
    />,
  );
  fireEvent.click(screen.getByLabelText('Context 50% used'));
  return { onCompactNow, onOpenChange };
}

describe('the compact control is reachable with a mouse', () => {
  it('survives its own pointerdown and fires onCompactNow', () => {
    const { onCompactNow } = open();
    const button = screen.getByRole('button', { name: 'Compact now' });

    // A real press: pointerdown reaches document first, then the click.
    fireEvent.pointerDown(button);
    expect(
      screen.queryByRole('dialog', { name: 'Context usage' }),
    ).not.toBeNull();

    fireEvent.click(button);
    expect(onCompactNow).toHaveBeenCalledTimes(1);
  });

  it('still closes on a pointerdown that is genuinely outside', () => {
    open();
    expect(screen.queryByRole('dialog', { name: 'Context usage' })).not.toBeNull();
    fireEvent.pointerDown(document.body);
    expect(screen.queryByRole('dialog', { name: 'Context usage' })).toBeNull();
  });

  it('stays open after compacting, so the result is where the user is looking', () => {
    const { onCompactNow } = open({ lastFoldedTurns: 12 });
    fireEvent.click(screen.getByRole('button', { name: 'Compact now' }));
    expect(onCompactNow).toHaveBeenCalled();
    expect(screen.queryByRole('dialog', { name: 'Context usage' })).not.toBeNull();
    expect(screen.getByText('Compacted 12 earlier messages')).toBeTruthy();
  });

  it('tells the host when it opens, so the count is fetched lazily', () => {
    const { onOpenChange } = open();
    expect(onOpenChange).toHaveBeenCalledWith(true);
  });
});

describe('what the button claims', () => {
  it('is dead with a reason when nothing can be folded', () => {
    open({ foldableTurns: 0 });
    const button = screen.getByRole('button', {
      name: 'Compact now',
    }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(screen.getByText(NOTHING_TO_COMPACT)).toBeTruthy();
  });

  it('is live with a count when turns can be folded', () => {
    open({ foldableTurns: 12 });
    const button = screen.getByRole('button', {
      name: 'Compact now',
    }) as HTMLButtonElement;
    expect(button.disabled).toBe(false);
    expect(screen.getByText('Folds 12 earlier messages into a summary.')).toBeTruthy();
  });

  it('goes dead and says so while a compaction is running', () => {
    // The other half of "one press, one request" — lib/compact refuses a
    // duplicate, and the control refuses to offer one. Both, because the
    // guard has to hold whether or not the click reaches the callback.
    const { onCompactNow } = open({ compacting: true });
    const button = screen.getByRole('button', {
      name: 'Compacting…',
    }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);

    fireEvent.click(button);
    expect(onCompactNow).not.toHaveBeenCalled();
  });

  it('stays live and silent when the server could not say', () => {
    open({ foldableTurns: null });
    const button = screen.getByRole('button', {
      name: 'Compact now',
    }) as HTMLButtonElement;
    expect(button.disabled).toBe(false);
    expect(button.getAttribute('aria-describedby')).toBeNull();
  });
});
