// @vitest-environment jsdom
/**
 * L-06: the Copy confirmation belongs to the press that started it.
 *
 * The reset timer used to be fired and forgotten. Copying twice inside the
 * confirmation window therefore left TWO timers running, and the first one to
 * expire cleared the second press's tick — so the second "Copied" vanished
 * after a few hundred milliseconds instead of its full duration. The same
 * missing handle meant nothing was cancelled when the button unmounted, which
 * happens routinely: a message is regenerated, edited, or scrolled out of a
 * conversation the user switched away from.
 *
 * These tests pin the ownership, not the duration — the exact millisecond
 * count is a design value and may change.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen } from '@testing-library/react';
import { CopyButton } from '../components/CopyButton';

/** The confirmation window the component uses. */
const CONFIRM_MS = 1600;

function writeText() {
  return Promise.resolve();
}

beforeEach(() => {
  vi.useFakeTimers();
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value: { writeText: vi.fn(writeText) },
  });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

/** Click and let the clipboard promise settle, still on fake timers. */
async function press(button: HTMLElement) {
  await act(async () => {
    button.click();
    await Promise.resolve();
  });
}

const isConfirming = () => Boolean(screen.queryByTitle('Copied'));

describe('L-06 · Copy confirmation timer ownership', () => {
  it('shows the confirmation for the full window after one press', async () => {
    render(<CopyButton text="hello" label="Copy" />);
    const button = screen.getByRole('button');

    await press(button);
    expect(isConfirming()).toBe(true);

    await act(async () => {
      vi.advanceTimersByTime(CONFIRM_MS - 1);
    });
    expect(isConfirming()).toBe(true);

    await act(async () => {
      vi.advanceTimersByTime(1);
    });
    expect(isConfirming()).toBe(false);
  });

  it('a second press gets its OWN full window — the first timer cannot cut it short', async () => {
    render(<CopyButton text="hello" label="Copy" />);
    const button = screen.getByRole('button');

    await press(button);
    // 1200ms in: the first press's timer has 400ms left to run.
    await act(async () => {
      vi.advanceTimersByTime(1200);
    });
    expect(isConfirming()).toBe(true);

    await press(button);

    // This is the regression. The first timer would have fired here and
    // cleared a confirmation that is only 400ms old.
    await act(async () => {
      vi.advanceTimersByTime(400);
    });
    expect(isConfirming()).toBe(true);

    // Still up just before the SECOND press's own window closes...
    await act(async () => {
      vi.advanceTimersByTime(CONFIRM_MS - 400 - 1);
    });
    expect(isConfirming()).toBe(true);

    // ...and gone exactly when it should be.
    await act(async () => {
      vi.advanceTimersByTime(1);
    });
    expect(isConfirming()).toBe(false);
  });

  it('rapid repeat presses leave exactly one timer pending, not one per press', async () => {
    render(<CopyButton text="hello" label="Copy" />);
    const button = screen.getByRole('button');

    for (let i = 0; i < 5; i += 1) {
      await press(button);
      await act(async () => {
        vi.advanceTimersByTime(100);
      });
    }

    expect(vi.getTimerCount()).toBe(1);
    expect(isConfirming()).toBe(true);
  });

  it('clears the pending timer on unmount, so nothing fires into a dead button', async () => {
    const { unmount } = render(<CopyButton text="hello" label="Copy" />);
    await press(screen.getByRole('button'));
    expect(vi.getTimerCount()).toBe(1);

    unmount();

    expect(vi.getTimerCount()).toBe(0);
    // And running the clock produces no update on the unmounted tree.
    await act(async () => {
      vi.advanceTimersByTime(CONFIRM_MS * 2);
    });
  });

  it('still copies the text it was given (both variants)', async () => {
    const spy = navigator.clipboard.writeText as unknown as ReturnType<typeof vi.fn>;

    render(<CopyButton text="chip text" label="Copy" />);
    await press(screen.getByRole('button'));
    expect(spy).toHaveBeenCalledWith('chip text');

    cleanup();
    render(<CopyButton text="icon text" label="Copy code" variant="icon" />);
    await press(screen.getByRole('button'));
    expect(spy).toHaveBeenCalledWith('icon text');
  });
});
