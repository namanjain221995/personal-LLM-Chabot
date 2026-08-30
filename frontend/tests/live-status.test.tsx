// @vitest-environment jsdom
/**
 * The progress line must not look frozen.
 *
 * `describe(plan)` sends ONE static sentence and the backend then goes quiet
 * until the first step. Measured on this deployment: 213 s of silence for a
 * 23,520-character paste in Max. A sentence that never changes for three and
 * a half minutes reads as a hung app, so the line carries a ticking clock —
 * and, once the wait is long enough to worry someone, one honest sentence
 * about why. It never invents progress the backend did not report.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen } from '@testing-library/react';
import { LiveStatus } from '../components/LiveStatus';

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe('LiveStatus', () => {
  it('shows the phase text immediately', () => {
    render(<LiveStatus text="Planning steps and searching the web" />);
    expect(screen.getByText('Planning steps and searching the web')).toBeTruthy();
  });

  it('ticks a visible clock so the line is never static', () => {
    vi.useFakeTimers();
    render(<LiveStatus text="Planning steps and searching the web" />);
    expect(screen.queryByText('5s')).toBeNull();
    act(() => void vi.advanceTimersByTime(5000));
    expect(screen.getByText('5s')).toBeTruthy();
    act(() => void vi.advanceTimersByTime(70_000));
    expect(screen.getByText('1m 15s')).toBeTruthy();
  });

  it('explains a long wait only after it has become long', () => {
    vi.useFakeTimers();
    const note = 'It plans first, then runs each step.';
    render(<LiveStatus text="Planning steps" effortNote={note} />);
    act(() => void vi.advanceTimersByTime(10_000));
    expect(screen.queryByText(note)).toBeNull();
    act(() => void vi.advanceTimersByTime(20_000));
    expect(screen.getByText(note)).toBeTruthy();
  });

  it('restarts the clock when the phase changes', () => {
    vi.useFakeTimers();
    const { rerender } = render(<LiveStatus text="Planning steps" />);
    act(() => void vi.advanceTimersByTime(30_000));
    expect(screen.getByText('30s')).toBeTruthy();
    rerender(<LiveStatus text="Searching the web" />);
    act(() => void vi.advanceTimersByTime(2000));
    expect(screen.queryByText('30s')).toBeNull();
    expect(screen.getByText('2s')).toBeTruthy();
  });
});
