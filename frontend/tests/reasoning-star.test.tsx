// @vitest-environment jsdom
/**
 * The Salesforce phase indicator.
 *
 * The artwork itself is covered in loader.test.tsx. What is tested here is the
 * property that actually matters: it only ever claims work the backend
 * reported, and it stops. An indicator that keeps turning after the answer
 * arrives is worse than none — every future spinner in the product stops
 * meaning anything.
 */

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { ReasoningStar } from '@/components/ReasoningStar';
import type { PhaseStatus } from '@/lib/phases';

afterEach(cleanup);

const QUERYING: PhaseStatus = {
  phase: 'querying_salesforce',
  label: 'Searching Salesforce',
  run_id: 'r1',
};

function loader(): HTMLVideoElement {
  return screen.getByTestId('app-loader') as HTMLVideoElement;
}

describe('when it renders at all', () => {
  it('shows the backend label verbatim', () => {
    render(<ReasoningStar status={QUERYING} />);
    expect(screen.getByText('Searching Salesforce')).toBeTruthy();
  });

  it('renders nothing with no status', () => {
    const { container } = render(<ReasoningStar status={null} />);
    expect(container.firstChild).toBeNull();
  });

  it('stops on a terminal phase', () => {
    const { container } = render(
      <ReasoningStar status={{ phase: 'completed', label: 'Done' }} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it('stops when the backend is waiting on the USER', () => {
    // `clarifying` hands over to the card. An indicator turning above a
    // question reads as "still working" when nothing is happening at all.
    const { container } = render(
      <ReasoningStar
        status={{ phase: 'clarifying', label: 'Checking one detail with you' }}
      />,
    );
    expect(container.firstChild).toBeNull();
  });

  it('exposes the phase for styling and for tests, not as user-facing text', () => {
    const { container } = render(<ReasoningStar status={QUERYING} />);
    expect(
      (container.firstChild as HTMLElement).getAttribute('data-phase'),
    ).toBe('querying_salesforce');
  });
});

describe('tempo follows the kind of work', () => {
  it('runs the same artwork at a different rate per phase', () => {
    // Rate rather than a different clip: changing the source would restart the
    // loop every time a phase advanced.
    render(<ReasoningStar status={QUERYING} />);
    const searching = loader().playbackRate;
    cleanup();
    render(
      <ReasoningStar status={{ phase: 'drafting_answer', label: 'Preparing' }} />,
    );
    expect(loader().playbackRate).not.toBe(searching);
  });

  it('grows for a centred empty response', () => {
    render(<ReasoningStar status={QUERYING} size="lg" />);
    expect(loader().getAttribute('width')).toBe('40');
  });
});

describe('accessibility', () => {
  it('announces the phase politely, once', () => {
    render(<ReasoningStar status={QUERYING} />);
    expect(document.querySelector('[aria-live="polite"]')?.textContent).toBe(
      'Salesforce assistant is processing the request: Searching Salesforce',
    );
  });

  it('leaves the artwork silent — the live region carries the meaning', () => {
    render(<ReasoningStar status={QUERYING} />);
    expect(loader().getAttribute('aria-hidden')).toBe('true');
  });

  it('can drop the visible label without losing the announcement', () => {
    render(<ReasoningStar status={QUERYING} hideLabel />);
    expect(screen.queryByText('Searching Salesforce')).toBeNull();
    expect(document.querySelector('[aria-live="polite"]')?.textContent).toContain(
      'Searching Salesforce',
    );
  });
});
