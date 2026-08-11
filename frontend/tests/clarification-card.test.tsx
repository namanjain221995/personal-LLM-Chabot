// @vitest-environment jsdom
/**
 * The clarification card, in a DOM.
 *
 * Only what genuinely needs one is here — focus movement, roving tabindex, ARIA
 * wiring, and the double-click guard. The keyboard MAP itself is pure and is
 * tested in clarification.test.ts; simulating keystrokes to prove a lookup
 * table works is slower and covers less.
 */

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ClarificationCard } from '@/components/ClarificationCard';
import { parseClarification, type ClarificationRequest } from '@/lib/clarification';

afterEach(cleanup);

const WIRE = {
  clarification_id: 'clr_1',
  conversation_id: 'conv',
  run_id: 'run',
  root_user_message_id: 'msg',
  intent_id: 'int',
  source: 'salesforce',
  header: 'Salesforce',
  question: 'Which period should I use?',
  slot: 'date_range',
  options: [
    { id: 'm', label: 'This month', value: 'THIS_MONTH' },
    { id: 'q', label: 'This quarter', description: 'Aug–Oct', value: 'THIS_QUARTER' },
    { id: 'y', label: 'This year', value: 'THIS_YEAR' },
  ],
  allow_custom: true,
  custom_placeholder: 'Enter another date range…',
  multi_select: false,
  round_number: 1,
  created_at: '2026-08-11T09:00:00+00:00',
  state: 'pending',
  resume_token: 'tok',
  question_fingerprint: 'fp',
};

function card(overrides: Partial<Parameters<typeof ClarificationCard>[0]> = {}) {
  const request = parseClarification(WIRE) as ClarificationRequest;
  const onSubmit = vi.fn();
  const onUseComposer = vi.fn();
  const utils = render(
    <ClarificationCard
      request={request}
      onSubmit={onSubmit}
      onUseComposer={onUseComposer}
      {...overrides}
    />,
  );
  return { ...utils, request, onSubmit, onUseComposer };
}

describe('rendering', () => {
  it('shows the question and every option', () => {
    card();
    expect(screen.getByText('Which period should I use?')).toBeTruthy();
    expect(screen.getByRole('radio', { name: /This month/ })).toBeTruthy();
    expect(screen.getByRole('radio', { name: /This quarter/ })).toBeTruthy();
    expect(screen.getByRole('radio', { name: /This year/ })).toBeTruthy();
    expect(screen.getByText('Aug–Oct')).toBeTruthy();
  });

  it('labels the group with the question, so a screen reader reads both', () => {
    card();
    const group = screen.getByRole('radiogroup');
    expect(group.getAttribute('aria-labelledby')).toBe('clr-clr_1');
    expect(document.getElementById('clr-clr_1')?.textContent).toBe(
      'Which period should I use?',
    );
  });

  it('offers "Something else" only when free text is allowed', () => {
    card();
    expect(screen.getByRole('button', { name: /Something else/ })).toBeTruthy();
    cleanup();
    render(
      <ClarificationCard
        request={parseClarification({ ...WIRE, allow_custom: false })!}
        onSubmit={vi.fn()}
        onUseComposer={vi.fn()}
      />,
    );
    expect(screen.queryByRole('button', { name: /Something else/ })).toBeNull();
  });

  it('offers a dismiss control only when one is supplied', () => {
    card();
    expect(screen.queryByRole('button', { name: /Dismiss/ })).toBeNull();
    cleanup();
    card({ onDismiss: vi.fn() });
    expect(screen.getByRole('button', { name: /Dismiss/ })).toBeTruthy();
  });

  it('collapses to the chosen answer once it has one', () => {
    card({ answeredWith: 'This quarter' });
    expect(screen.queryByRole('radiogroup')).toBeNull();
    expect(screen.getByText('This quarter')).toBeTruthy();
  });
});

describe('focus', () => {
  it('moves focus onto the first option when the question appears', () => {
    card();
    expect(document.activeElement?.textContent).toContain('This month');
  });

  it('is ONE tab stop: the arrows move within the group', () => {
    card();
    const options = screen.getAllByRole('radio');
    expect(options.map((o) => o.getAttribute('tabindex'))).toEqual(['0', '-1', '-1']);

    fireEvent.keyDown(options[0], { key: 'ArrowDown' });
    expect(document.activeElement?.textContent).toContain('This quarter');
    expect(screen.getAllByRole('radio').map((o) => o.getAttribute('tabindex'))).toEqual(
      ['-1', '0', '-1'],
    );
  });

  it('cycles backwards onto the "Something else" row, which is in the loop', () => {
    // The custom row is a navigable row, not a separate tab stop — otherwise
    // reaching it by keyboard means tabbing out of the question.
    card();
    fireEvent.keyDown(screen.getByRole('radiogroup'), { key: 'ArrowUp' });
    expect(document.activeElement?.textContent).toContain('Something else');
  });
});

describe('answering', () => {
  it('submits the option that was clicked', () => {
    const { onSubmit } = card();
    fireEvent.click(screen.getByRole('radio', { name: /This quarter/ }));
    expect(onSubmit).toHaveBeenCalledTimes(1);
    const [response, summary] = onSubmit.mock.calls[0];
    expect(response.selected_option_ids).toEqual(['q']);
    expect(response.resume_token).toBe('tok');
    expect(summary).toBe('This quarter');
  });

  it('submits by number key', () => {
    const { onSubmit } = card();
    fireEvent.keyDown(screen.getByRole('radiogroup'), { key: '3' });
    expect(onSubmit.mock.calls[0][0].selected_option_ids).toEqual(['y']);
  });

  it('does not submit twice on a double click', () => {
    // The server's first-response-wins UPDATE is the authority; this guard is
    // what stops a second stream even opening.
    const { onSubmit } = card();
    const option = screen.getByRole('radio', { name: /This month/ });
    fireEvent.click(option);
    fireEvent.click(option);
    fireEvent.click(screen.getByRole('radio', { name: /This year/ }));
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it('locks every control while the continuation is starting', () => {
    const { onSubmit } = card({ submitting: true });
    const option = screen.getByRole('radio', { name: /This month/ });
    expect((option as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(option);
    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByText('Continuing your request…')).toBeTruthy();
  });

  it('opens a text field IN the card for "Something else"', () => {
    // Sending someone to the main composer meant leaving the question in order
    // to answer it.
    const { onSubmit } = card();
    expect(screen.queryByRole('textbox')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /Something else/ }));
    const field = screen.getByRole('textbox');
    expect(field).toBeTruthy();
    expect((field as HTMLTextAreaElement).placeholder).toBe(
      'Enter another date range…',
    );
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('sends what was typed, and nothing until there is something to send', () => {
    const { onSubmit } = card();
    fireEvent.click(screen.getByRole('button', { name: /Something else/ }));
    const send = screen.getByRole('button', { name: 'Send' });
    expect((send as HTMLButtonElement).disabled).toBe(true);

    fireEvent.change(screen.getByRole('textbox'), {
      target: { value: 'last 90 days' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    expect(onSubmit.mock.calls[0][0].custom_text).toBe('last 90 days');
  });

  it('sends the typed answer on Enter, and keeps Shift+Enter as a newline', () => {
    const { onSubmit } = card();
    fireEvent.click(screen.getByRole('button', { name: /Something else/ }));
    const field = screen.getByRole('textbox');
    fireEvent.change(field, { target: { value: 'last 90 days' } });

    fireEvent.keyDown(field, { key: 'Enter', shiftKey: true });
    expect(onSubmit).not.toHaveBeenCalled();

    fireEvent.keyDown(field, { key: 'Enter' });
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it('can still hand over to the main composer for anyone who prefers it', () => {
    const { onSubmit, onUseComposer } = card();
    fireEvent.click(screen.getByText('Use composer'));
    expect(onUseComposer).toHaveBeenCalledTimes(1);
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('dismisses on Escape only when dismissing is offered', () => {
    const onDismiss = vi.fn();
    card({ onDismiss });
    fireEvent.keyDown(screen.getByRole('radiogroup'), { key: 'Escape' });
    expect(onDismiss).toHaveBeenCalledTimes(1);

    cleanup();
    card();
    fireEvent.keyDown(screen.getByRole('radiogroup'), { key: 'Escape' });
    expect(onDismiss).toHaveBeenCalledTimes(1); // unchanged
  });
});

describe('multi-select', () => {
  // The single-answer card was actively wrong for this org's questions: asked
  // which object holds payment AND invoice data, the honest answer is both, and
  // a radio group forced a choice between two things the user needed together.
  function multi(overrides: Record<string, unknown> = {}) {
    const request = parseClarification({ ...WIRE, multi_select: true, ...overrides })!;
    const onSubmit = vi.fn();
    render(
      <ClarificationCard
        request={request}
        onSubmit={onSubmit}
        onUseComposer={vi.fn()}
      />,
    );
    return { onSubmit };
  }

  it('accumulates choices and sends them together', () => {
    const { onSubmit } = multi();
    fireEvent.click(screen.getByRole('checkbox', { name: /This month/ }));
    fireEvent.click(screen.getByRole('checkbox', { name: /This year/ }));
    expect(onSubmit).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: /^Done/ }));
    expect(onSubmit.mock.calls[0][0].selected_option_ids).toEqual(['m', 'y']);
  });

  it('unticks on a second click', () => {
    const { onSubmit } = multi();
    const option = screen.getByRole('checkbox', { name: /This month/ });
    fireEvent.click(option);
    expect(option.getAttribute('aria-checked')).toBe('true');
    fireEvent.click(option);
    expect(option.getAttribute('aria-checked')).toBe('false');
    expect(screen.queryByRole('button', { name: /^Done/ })).toBeNull();
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('shows Done only once there is something to send, with a count', () => {
    multi();
    expect(screen.queryByRole('button', { name: /^Done/ })).toBeNull();
    fireEvent.click(screen.getByRole('checkbox', { name: /This month/ }));
    expect(screen.getByRole('button', { name: 'Done' })).toBeTruthy();
    fireEvent.click(screen.getByRole('checkbox', { name: /This year/ }));
    expect(screen.getByRole('button', { name: 'Done (2)' })).toBeTruthy();
  });

  it('toggles by number key instead of submitting on the first press', () => {
    const { onSubmit } = multi();
    const group = screen.getByRole('group');
    fireEvent.keyDown(group, { key: '1' });
    fireEvent.keyDown(group, { key: '3' });
    expect(onSubmit).not.toHaveBeenCalled();
    fireEvent.keyDown(group, { key: 'Enter', metaKey: true });
    expect(onSubmit.mock.calls[0][0].selected_option_ids).toEqual(['m', 'y']);
  });

  it('combines ticked options with typed text rather than dropping either', () => {
    const { onSubmit } = multi();
    fireEvent.click(screen.getByRole('checkbox', { name: /This month/ }));
    fireEvent.click(screen.getByRole('button', { name: /Something else/ }));
    fireEvent.change(screen.getByRole('textbox'), {
      target: { value: 'and anything on a renewal' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    const [response] = onSubmit.mock.calls[0];
    expect(response.selected_option_ids).toEqual(['m']);
    expect(response.custom_text).toBe('and anything on a renewal');
  });

  it('leaves a genuinely exclusive question as a single-answer card', () => {
    // The server pins these (EXCLUSIVE_SLOTS); the card obeys.
    const { onSubmit } = multi({ multi_select: false });
    expect(screen.getAllByRole('radio')).toHaveLength(3);
    fireEvent.click(screen.getByRole('radio', { name: /This month/ }));
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });
});

describe('the submitting lock is scoped to ONE question', () => {
  // Owner report 2026-08-11: the second card in a thread could not be clicked
  // at all. `submitting` was driven by a boolean that latched on after the
  // first answer and was only cleared when the CONVERSATION changed, so every
  // later question rendered permanently disabled.
  it('locks the question whose answer is in flight', () => {
    const { onSubmit } = card({ submitting: true });
    fireEvent.click(screen.getByRole('radio', { name: /This month/ }));
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('leaves a DIFFERENT question fully clickable', () => {
    const next = parseClarification({
      ...WIRE,
      clarification_id: 'clr_2',
      question: 'Which region?',
      options: [
        { id: 'emea', label: 'EMEA' },
        { id: 'amer', label: 'AMER' },
      ],
    })!;
    const onSubmit = vi.fn();
    render(
      <ClarificationCard
        request={next}
        onSubmit={onSubmit}
        onUseComposer={vi.fn()}
        submitting={false}
      />,
    );
    fireEvent.click(screen.getByRole('radio', { name: /EMEA/ }));
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it('unlocks its own controls when a new question replaces it', () => {
    const request = parseClarification(WIRE)!;
    const onSubmit = vi.fn();
    const { rerender } = render(
      <ClarificationCard
        request={request}
        onSubmit={onSubmit}
        onUseComposer={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole('radio', { name: /This month/ }));
    expect(onSubmit).toHaveBeenCalledTimes(1);

    // A round-2 question arrives; the card must accept a click again.
    const second = parseClarification({
      ...WIRE,
      clarification_id: 'clr_2',
      question: 'Which status?',
      options: [
        { id: 'open', label: 'Open' },
        { id: 'closed', label: 'Closed' },
      ],
    })!;
    rerender(
      <ClarificationCard
        request={second}
        onSubmit={onSubmit}
        onUseComposer={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole('radio', { name: /Open/ }));
    expect(onSubmit).toHaveBeenCalledTimes(2);
  });
});
