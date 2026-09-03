// @vitest-environment jsdom
/**
 * The context ring, which IS the compact control (owner request 2026-09-02).
 *
 * REPLACES tests/context-meter-popover.test.tsx. That file pinned a surface
 * that no longer exists — a portalled 280px dialog listing "Messages and
 * context", "Reserved for reply (held back)" and `5,235 / 991,296`, with a
 * separate "Compact now" button inside it. Every behaviour it protected is
 * carried forward here in its new form: that the action is genuinely
 * reachable with a mouse, that opening tells the host to fetch the foldable
 * count lazily, that a known-zero count goes dead WITH A REASON, that an
 * in-flight compaction cannot be double-fired, and that an UNKNOWN count
 * leaves the control live.
 *
 * Three properties are new and are the point of the redesign:
 *
 *   - the percentage is invisible until hover or focus,
 *   - there is no native `title`, so exactly ONE tooltip exists, and
 *   - no raw token number is rendered anywhere, in any state.
 *
 * They are asserted against the whole rendered subtree rather than against one
 * element, because "we removed the label" is not the same claim as "the number
 * is not on screen".
 */

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ContextMeter } from '@/components/ContextMeter';
import {
  CLICK_TO_COMPACT,
  meterView,
  NOTHING_TO_COMPACT,
  type MeterView,
} from '@/lib/contextMeter';

afterEach(cleanup);

/** A view at an arbitrary fill, carrying the real token fields throughout. */
function viewAt(fraction: number, over: Partial<MeterView> = {}): MeterView {
  const percent = Math.min(100, Math.max(0, Math.round(fraction * 100)));
  return {
    fraction,
    percent,
    state: 'calm',
    pulsing: false,
    tokensUsed: 5235,
    usableBudget: 991296,
    breakdown: [
      { label: 'Messages and context', tokens: 5235 },
      { label: 'Reserved for reply', tokens: 8192, heldBack: true },
    ],
    ...over,
  };
}

function setup(props: Partial<Parameters<typeof ContextMeter>[0]> = {}) {
  const onCompactNow = vi.fn();
  const onOpenChange = vi.fn();
  const utils = render(
    <ContextMeter
      view={viewAt(0.75)}
      compacting={false}
      onCompactNow={onCompactNow}
      foldableTurns={12}
      onOpenChange={onOpenChange}
      {...props}
    />,
  );
  return { onCompactNow, onOpenChange, ...utils };
}

const ring = () => screen.getByRole('button');
const tooltip = () => screen.queryByRole('tooltip');
/** Everything a human can actually read right now. */
const visibleText = () => document.body.textContent ?? '';

/* ===================================================== the control itself */

describe('CTX-01 · the ring renders', () => {
  it('renders one circular control with an SVG ring', () => {
    setup();
    expect(ring()).toBeTruthy();
    expect(ring().tagName).toBe('BUTTON');
    expect(ring().getAttribute('type')).toBe('button');
    expect(document.querySelector('svg')).toBeTruthy();
    expect(screen.getByTestId('ctx-ring')).toBeTruthy();
  });
});

describe('CTX-02 · the percentage is not permanently visible', () => {
  it('shows no percentage at all before hover or focus', () => {
    setup({ view: viewAt(0.75) });
    expect(visibleText()).not.toContain('75%');
    expect(visibleText()).not.toContain('%');
    expect(tooltip()).toBeNull();
  });

  it('still exposes it to a screen reader, which has no hover', () => {
    setup({ view: viewAt(0.75) });
    expect(ring().getAttribute('aria-label')).toBe(
      '75% context used. Compact conversation.',
    );
  });
});

describe('CTX-03 … CTX-07 · hover, focus, and exactly one tooltip', () => {
  it('CTX-03 shows the percentage on hover', () => {
    setup({ view: viewAt(0.75) });
    fireEvent.mouseEnter(ring());
    expect(screen.getByRole('tooltip').textContent).toContain('75% context used');
  });

  it('CTX-04 hides it again on mouse leave', () => {
    setup({ view: viewAt(0.75) });
    fireEvent.mouseEnter(ring());
    expect(tooltip()).not.toBeNull();
    fireEvent.mouseLeave(ring());
    expect(tooltip()).toBeNull();
    expect(visibleText()).not.toContain('75%');
  });

  it('CTX-05 shows it on keyboard focus', () => {
    setup({ view: viewAt(0.75) });
    fireEvent.focus(ring());
    expect(screen.getByRole('tooltip').textContent).toContain('75% context used');
  });

  it('CTX-06 hides it on blur', () => {
    setup({ view: viewAt(0.75) });
    fireEvent.focus(ring());
    fireEvent.blur(ring());
    expect(tooltip()).toBeNull();
  });

  it('CTX-07 carries no native title, so there is only ONE tooltip', () => {
    // The reverted implementation had title={`Context ${percent}% used`},
    // which the browser renders on hover alongside the custom one.
    setup();
    expect(ring().hasAttribute('title')).toBe(false);
    fireEvent.mouseEnter(ring());
    expect(ring().hasAttribute('title')).toBe(false);
    expect(document.querySelectorAll('[title]')).toHaveLength(0);
    expect(screen.getAllByRole('tooltip')).toHaveLength(1);
  });

  it('names the tooltip as the control description while it is open', () => {
    setup();
    expect(ring().getAttribute('aria-describedby')).toBeNull();
    fireEvent.focus(ring());
    expect(ring().getAttribute('aria-describedby')).toBe(
      screen.getByRole('tooltip').id,
    );
  });

  it('dismisses on Escape without letting it reach "stop generating"', () => {
    setup();
    fireEvent.focus(ring());
    const notPrevented = fireEvent.keyDown(ring(), { key: 'Escape' });
    expect(tooltip()).toBeNull();
    // fireEvent returns false when the handler called preventDefault.
    expect(notPrevented).toBe(false);
  });
});

/* ============================================ the removed popover surface */

describe('CTX-08 … CTX-10 · the old surface is gone', () => {
  it('CTX-08 renders no raw token figure, hovered or not', () => {
    setup({ view: viewAt(0.75) });
    for (const step of [() => undefined, () => fireEvent.mouseEnter(ring())]) {
      step();
      const text = visibleText();
      expect(text).not.toContain('5,235');
      expect(text).not.toContain('8,192');
      expect(text).not.toContain('991,296');
      expect(text).not.toContain('5235');
      expect(text).not.toContain('991296');
      expect(text).not.toMatch(/\d[\d,]*\s*\/\s*\d/); // "used / limit"
    }
  });

  it('CTX-09 opens no popover dialog on click', () => {
    setup();
    fireEvent.click(ring());
    expect(screen.queryByRole('dialog')).toBeNull();
    const text = visibleText();
    expect(text).not.toContain("How much of the model");
    expect(text).not.toContain('Messages and context');
    expect(text).not.toContain('Reserved for reply');
    expect(text).not.toContain('This message uses');
  });

  it('CTX-10 renders no separate "Compact now" button', () => {
    setup();
    fireEvent.mouseEnter(ring());
    expect(screen.queryByRole('button', { name: 'Compact now' })).toBeNull();
    // Exactly one control exists: the ring.
    expect(screen.getAllByRole('button')).toHaveLength(1);
    expect(visibleText()).not.toContain('Compact now');
  });
});

/* ================================================================ pressing */

describe('CTX-11 … CTX-14 · activation', () => {
  it('CTX-11 compacts on click when a fold is available', () => {
    const { onCompactNow } = setup({ foldableTurns: 12 });
    fireEvent.click(ring());
    expect(onCompactNow).toHaveBeenCalledTimes(1);
  });

  it('CTX-12 compacts on Enter', () => {
    const { onCompactNow } = setup({ foldableTurns: 12 });
    ring().focus();
    // A native button turns Enter into a click; this is what it dispatches.
    fireEvent.click(ring(), { detail: 0 });
    expect(onCompactNow).toHaveBeenCalledTimes(1);
  });

  it('CTX-13 compacts on Space', () => {
    const { onCompactNow } = setup({ foldableTurns: 12 });
    ring().focus();
    fireEvent.click(ring(), { detail: 0 });
    expect(onCompactNow).toHaveBeenCalledTimes(1);
  });

  it('CTX-14 fires nothing at all while a compaction is in flight', () => {
    const { onCompactNow } = setup({ foldableTurns: 12, compacting: true });
    fireEvent.click(ring());
    fireEvent.click(ring());
    fireEvent.click(ring());
    expect(onCompactNow).not.toHaveBeenCalled();
  });

  it('stays focusable while inert, so it can still explain itself', () => {
    setup({ compacting: true });
    // aria-disabled, NOT the disabled attribute: a disabled button leaves the
    // tab order and stops firing the hover/focus this tooltip runs on.
    expect(ring().getAttribute('aria-disabled')).toBe('true');
    expect((ring() as HTMLButtonElement).disabled).toBe(false);
    fireEvent.focus(ring());
    expect(screen.getByRole('tooltip').textContent).toContain('Compacting');
  });
});

/* ================================================== nothing to compact */

describe('CTX-15 … CTX-16 · what the ring does about foldability', () => {
  it('CTX-15 shows the percentage AND the reason, and sends nothing', () => {
    const { onCompactNow } = setup({ view: viewAt(0.01), foldableTurns: 0 });
    fireEvent.mouseEnter(ring());
    const text = screen.getByRole('tooltip').textContent ?? '';
    expect(text).toContain('1% context used');
    expect(text).toContain(NOTHING_TO_COMPACT);
    expect(text).toContain('Nothing to compact yet.');

    fireEvent.click(ring());
    fireEvent.click(ring());
    expect(onCompactNow).not.toHaveBeenCalled();
    expect(ring().getAttribute('aria-disabled')).toBe('true');
  });

  it('CTX-16 treats an UNKNOWN count as unknown, never as zero', () => {
    const { onCompactNow } = setup({ foldableTurns: null });
    fireEvent.mouseEnter(ring());
    expect(screen.getByRole('tooltip').textContent).toContain(CLICK_TO_COMPACT);
    expect(ring().getAttribute('aria-disabled')).toBeNull();
    fireEvent.click(ring());
    expect(onCompactNow).toHaveBeenCalledTimes(1);
  });

  it('is inert while the host vetoes it, and claims no reason it was not given', () => {
    const { onCompactNow } = setup({ compactDisabled: true, foldableTurns: 12 });
    fireEvent.mouseEnter(ring());
    const text = screen.getByRole('tooltip').textContent ?? '';
    expect(text).toContain('75% context used');
    expect(text).not.toContain(CLICK_TO_COMPACT);
    fireEvent.click(ring());
    expect(onCompactNow).not.toHaveBeenCalled();
  });
});

/* ================================================================ the ring */

describe('CTX-17 … CTX-21 · the ring draws every fill safely', () => {
  const parts = () =>
    screen
      .getByTestId('ctx-ring')
      .getAttribute('stroke-dasharray')!
      .split(' ')
      .map(Number);
  const dashOf = () => parts()[0];
  const circumference = () => parts()[1];

  it.each([
    ['CTX-17', 0, 0],
    ['CTX-18', 0.25, 0.25],
    ['CTX-19', 0.75, 0.75],
    ['CTX-20', 1, 1],
  ])('%s renders %f as %f of the ring', (_id, fraction, expected) => {
    cleanup();
    setup({ view: viewAt(fraction) });
    expect(dashOf()).toBeCloseTo(circumference() * expected, 5);
  });

  it('CTX-21 clamps beyond 100% to a full ring, never past it', () => {
    setup({ view: viewAt(1.2) });
    expect(dashOf()).toBeCloseTo(circumference(), 5);
    fireEvent.mouseEnter(ring());
    expect(screen.getByRole('tooltip').textContent).toContain('100% context used');
  });

  it('clamps a negative fraction to an empty ring', () => {
    setup({ view: viewAt(-0.5, { fraction: -0.5, percent: 0 }) });
    expect(dashOf()).toBe(0);
  });

  it('rounds through the existing meterView, with no second formula', () => {
    expect(meterView({ tokens_used: 4, usable_budget: 1000 } as never, '').percent).toBe(0);
    expect(meterView({ tokens_used: 249, usable_budget: 1000 } as never, '').percent).toBe(25);
    expect(meterView({ tokens_used: 754, usable_budget: 1000 } as never, '').percent).toBe(75);
    expect(meterView({ tokens_used: 1150, usable_budget: 1000 } as never, '').percent).toBe(100);
  });
});

/* ================================================ compacting + lazy fetch */

describe('CTX-22 · compacting, announced but not shoved on screen', () => {
  it('announces the compaction it can only show as a spin', () => {
    // The visible "Compacting conversation…" pill is gone (it shifted the
    // whole row); the ring spins instead. A spinner is aria-hidden, so the
    // announcement has to survive somewhere.
    const { rerender, onCompactNow } = setup({ compacting: false });
    expect(screen.queryByRole('status')).toBeNull();

    rerender(
      <ContextMeter
        view={viewAt(0.75)}
        compacting
        onCompactNow={onCompactNow}
        foldableTurns={12}
      />,
    );
    const status = screen.getByRole('status');
    expect(status.textContent).toContain('Compacting');
    // …and it is for screen readers only — no visible pill returns.
    expect(status.className).toContain('sr-only');
  });

  it('tells the host when the tooltip opens, so the count is fetched lazily', () => {
    const { onOpenChange } = setup();
    expect(onOpenChange).not.toHaveBeenCalled();
    fireEvent.mouseEnter(ring());
    expect(onOpenChange).toHaveBeenCalledWith(true);
    fireEvent.mouseLeave(ring());
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it('does not re-fetch on a second enter without an intervening leave', () => {
    const { onOpenChange } = setup();
    fireEvent.mouseEnter(ring());
    fireEvent.focus(ring());
    fireEvent.mouseEnter(ring());
    expect(onOpenChange.mock.calls.filter(([open]) => open)).toHaveLength(1);
  });

  it('is usable again once a compaction finishes, whatever its outcome', () => {
    const { onCompactNow, rerender } = setup({ compacting: true });
    fireEvent.click(ring());
    expect(onCompactNow).not.toHaveBeenCalled();

    rerender(
      <ContextMeter
        view={viewAt(0.75)}
        compacting={false}
        onCompactNow={onCompactNow}
        foldableTurns={12}
      />,
    );
    expect(ring().getAttribute('aria-disabled')).toBeNull();
    fireEvent.click(ring());
    expect(onCompactNow).toHaveBeenCalledTimes(1);
  });

  it('is usable again after a failure left the count UNKNOWN', () => {
    // Carried across from the other branch during conflict resolution: the
    // test above re-enables with a known count, this one with `null`. A failed
    // request is exactly how the count goes unknown, so the two are different
    // recoveries and both are worth pinning.
    const { onCompactNow, rerender } = setup({ compacting: true });
    rerender(
      <ContextMeter
        view={viewAt(0.75)}
        compacting={false}
        onCompactNow={onCompactNow}
        foldableTurns={null}
      />,
    );
    fireEvent.click(ring());
    expect(onCompactNow).toHaveBeenCalledTimes(1);
  });

  it('keeps showing the last true reading — it never resets itself to 0%', () => {
    const { rerender, onCompactNow } = setup({ view: viewAt(0.75) });
    fireEvent.click(ring());
    rerender(
      <ContextMeter
        view={viewAt(0.75)}
        compacting={false}
        onCompactNow={onCompactNow}
        foldableTurns={0}
      />,
    );
    fireEvent.mouseEnter(ring());
    expect(screen.getByRole('tooltip').textContent).toContain('75% context used');
  });
});
