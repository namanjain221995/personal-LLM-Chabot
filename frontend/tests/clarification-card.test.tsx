// @vitest-environment jsdom
/**
 * The clarification PANEL, in a DOM.
 *
 * It renders inside the composer rather than the transcript, and free text is
 * answered in the composer rather than in a field of its own — so what needs a
 * DOM here is focus movement, the roving tabindex, ARIA wiring, the
 * double-click guard, and the hand-over to the composer. The keyboard MAP
 * itself is pure and is tested in clarification.test.ts; simulating keystrokes
 * to prove a lookup table works is slower and covers less.
 */

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  ClarificationCard,
  ClarificationRecord,
} from '@/components/ClarificationCard';
import { parseClarification, type ClarificationRequest } from '@/lib/clarification';

afterEach(cleanup);

const WIRE = {
  clarification_id: 'clr_1',
  conversation_id: 'conv',
  run_id: 'run',
  root_user_message_id: 'msg',
  intent_id: 'int',
  source: 'salesforce',
  header: 'Time period',
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

function multiCard(extra: Record<string, unknown> = {}) {
  const request = parseClarification({ ...WIRE, multi_select: true, ...extra })!;
  const onSubmit = vi.fn();
  const utils = render(
    <ClarificationCard request={request} onSubmit={onSubmit} onUseComposer={vi.fn()} />,
  );
  return { ...utils, request, onSubmit };
}

describe('rendering', () => {
  it('shows the question and every option', () => {
    card();
    expect(screen.getByText('Which period should I use?')).toBeTruthy();
    for (const label of ['This month', 'This quarter', 'This year']) {
      expect(screen.getByRole('radio', { name: new RegExp(label) })).toBeTruthy();
    }
    expect(screen.getByText('Aug–Oct')).toBeTruthy();
  });

  it('labels the group with the question, so a screen reader reads both', () => {
    card();
    expect(
      screen.getByRole('radiogroup', { name: 'Which period should I use?' }),
    ).toBeTruthy();
  });

  it('names the topic in the header rather than repeating the source', () => {
    card();
    expect(screen.getByText(/Clarification . Time period/)).toBeTruthy();
  });

  it('offers "Something else" only when free text is allowed', () => {
    card();
    expect(screen.getByRole('button', { name: /Something else/ })).toBeTruthy();
    cleanup();

    const request = parseClarification({ ...WIRE, allow_custom: false })!;
    render(
      <ClarificationCard request={request} onSubmit={vi.fn()} onUseComposer={vi.fn()} />,
    );
    expect(screen.queryByRole('button', { name: /Something else/ })).toBeNull();
  });

  it('offers a skip control only when one is supplied', () => {
    card();
    expect(screen.queryByRole('button', { name: /Skip/ })).toBeNull();
    cleanup();
    card({ onSkip: vi.fn() });
    expect(screen.getByRole('button', { name: /Skip/ })).toBeTruthy();
  });

  it('has NO text field of its own', () => {
    // A textarea here would sit forty pixels above the composer's own — two
    // inputs, one question, and no way to tell which is listening.
    card();
    expect(screen.queryByRole('textbox')).toBeNull();
  });
});

describe('focus', () => {
  it('moves focus onto the first option when the question appears', () => {
    card();
    expect(document.activeElement?.textContent).toContain('This month');
  });

  it('is ONE tab stop: the arrows move within the group', () => {
    card();
    const rows = screen.getAllByRole('radio');
    expect(rows[0].getAttribute('tabindex')).toBe('0');
    expect(rows[1].getAttribute('tabindex')).toBe('-1');

    fireEvent.keyDown(rows[0], { key: 'ArrowDown' });
    expect(document.activeElement?.textContent).toContain('This quarter');
  });

  it('cycles backwards onto the "Something else" row, which is in the loop', () => {
    card();
    fireEvent.keyDown(screen.getAllByRole('radio')[0], { key: 'ArrowUp' });
    expect(document.activeElement?.textContent).toContain('Something else');
  });
});

describe('answering', () => {
  it('submits the option that was clicked, with no second action needed', () => {
    const { onSubmit } = card();
    fireEvent.click(screen.getByRole('radio', { name: /This quarter/ }));
    expect(onSubmit).toHaveBeenCalledTimes(1);
    const [response, summary] = onSubmit.mock.calls[0];
    expect(response.selected_option_ids).toEqual(['q']);
    expect(summary).toBe('This quarter');
  });

  it('submits by number key', () => {
    const { onSubmit } = card();
    fireEvent.keyDown(screen.getAllByRole('radio')[0], { key: '3' });
    expect(onSubmit.mock.calls[0][0].selected_option_ids).toEqual(['y']);
  });

  it('does not submit twice on a double click', () => {
    const { onSubmit } = card();
    const option = screen.getByRole('radio', { name: /This month/ });
    fireEvent.click(option);
    fireEvent.click(option);
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it('locks every control while the continuation is starting', () => {
    const { onSubmit } = card({ submitting: true, onSkip: vi.fn() });
    for (const el of [
      ...screen.getAllByRole('radio'),
      screen.getByRole('button', { name: /Something else/ }),
      screen.getByRole('button', { name: /Skip/ }),
    ]) {
      expect((el as HTMLButtonElement).disabled).toBe(true);
    }
    fireEvent.click(screen.getAllByRole('radio')[0]);
    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByText(/Continuing your request/)).toBeTruthy();
  });

  it('skips by submitting a "no preference" answer, not by vanishing', () => {
    const onSkip = vi.fn();
    card({ onSkip });
    fireEvent.click(screen.getByRole('button', { name: /Skip/ }));
    expect(onSkip).toHaveBeenCalledTimes(1);
  });
});

describe('handing over to the composer', () => {
  it('sends "Something else" to the composer instead of opening a field', () => {
    const { onUseComposer, onSubmit } = card();
    fireEvent.click(screen.getByRole('button', { name: /Something else/ }));
    expect(onUseComposer).toHaveBeenCalledTimes(1);
    expect(onUseComposer.mock.calls[0][0]).toBeUndefined();
    // …and it does NOT answer the question with the literal words.
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('leaves for the composer on Escape without answering', () => {
    const { onUseComposer, onSubmit } = card({ onSkip: vi.fn() });
    fireEvent.keyDown(screen.getAllByRole('radio')[0], { key: 'Escape' });
    expect(onUseComposer).toHaveBeenCalledTimes(1);
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('forwards the first typed character so the answer is not decapitated', () => {
    // The panel takes focus so the number keys work immediately. That is only
    // safe because typing still goes where typing goes.
    const { onUseComposer, onSubmit } = card();
    fireEvent.keyDown(screen.getAllByRole('radio')[0], { key: 'A' });
    expect(onUseComposer).toHaveBeenCalledWith('A');
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('still treats a digit WITHIN the card as a shortcut', () => {
    const { onUseComposer, onSubmit } = card();
    fireEvent.keyDown(screen.getAllByRole('radio')[0], { key: '2' });
    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onUseComposer).not.toHaveBeenCalled();
  });
});

describe('multi-select', () => {
  it('accumulates choices and sends them together', () => {
    const { onSubmit } = multiCard();
    fireEvent.click(screen.getByRole('checkbox', { name: /This month/ }));
    fireEvent.click(screen.getByRole('checkbox', { name: /This year/ }));
    expect(onSubmit).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: /Done/ }));
    expect(onSubmit.mock.calls[0][0].selected_option_ids).toEqual(['m', 'y']);
  });

  it('unticks on a second click', () => {
    const { onSubmit } = multiCard();
    const option = screen.getByRole('checkbox', { name: /This month/ });
    fireEvent.click(option);
    fireEvent.click(option);
    expect(screen.queryByRole('button', { name: /Done/ })).toBeNull();
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('shows Done only once there is something to send, with a count', () => {
    multiCard();
    expect(screen.queryByRole('button', { name: /Done/ })).toBeNull();
    fireEvent.click(screen.getByRole('checkbox', { name: /This month/ }));
    expect(screen.getByRole('button', { name: 'Done' })).toBeTruthy();
    fireEvent.click(screen.getByRole('checkbox', { name: /This year/ }));
    expect(screen.getByRole('button', { name: 'Done (2)' })).toBeTruthy();
  });

  it('toggles by number key instead of submitting on the first press', () => {
    const { onSubmit } = multiCard();
    fireEvent.keyDown(screen.getAllByRole('checkbox')[0], { key: '1' });
    expect(onSubmit).not.toHaveBeenCalled();
    expect(
      screen.getByRole('checkbox', { name: /This month/ }).getAttribute('aria-checked'),
    ).toBe('true');
  });

  it('leaves a genuinely exclusive question as a single-answer card', () => {
    // The server pins those slots (EXCLUSIVE_SLOTS) and sends multi_select:false.
    card();
    expect(screen.getAllByRole('radio')).toHaveLength(3);
    expect(screen.queryByRole('checkbox')).toBeNull();
  });
});

describe('the submitting lock is scoped to ONE question', () => {
  it('unlocks when a new question replaces it', () => {
    const request = parseClarification(WIRE)!;
    const onSubmit = vi.fn();
    const { rerender } = render(
      <ClarificationCard request={request} onSubmit={onSubmit} onUseComposer={vi.fn()} />,
    );
    fireEvent.click(screen.getByRole('radio', { name: /This month/ }));
    expect(onSubmit).toHaveBeenCalledTimes(1);

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
      <ClarificationCard request={second} onSubmit={onSubmit} onUseComposer={vi.fn()} />,
    );
    fireEvent.click(screen.getByRole('radio', { name: /Open/ }));
    expect(onSubmit).toHaveBeenCalledTimes(2);
  });
});

describe('accessibility and presentation', () => {
  it('reports SELECTION, never focus, on a single-answer card', () => {
    // aria-checked used to track the focus ring, so arrowing down the list
    // announced each row in turn as selected.
    card();
    for (const radio of screen.getAllByRole('radio')) {
      expect(radio.getAttribute('aria-checked')).toBe('false');
    }
  });

  it('ticks aria-checked on the chosen rows of a multi-answer card', () => {
    multiCard();
    fireEvent.click(screen.getByRole('checkbox', { name: /This month/ }));
    expect(
      screen.getByRole('checkbox', { name: /This month/ }).getAttribute('aria-checked'),
    ).toBe('true');
    expect(
      screen.getByRole('checkbox', { name: /This year/ }).getAttribute('aria-checked'),
    ).toBe('false');
  });

  it('keeps a visible focus ring on every control it suppresses the outline of', () => {
    card({ onSkip: vi.fn() });
    const controls = [
      ...screen.getAllByRole('radio'),
      screen.getByRole('button', { name: /Something else/ }),
      screen.getByRole('button', { name: /Skip/ }),
    ];
    for (const el of controls) {
      expect(el.getAttribute('class') ?? '', el.textContent ?? '').toContain(
        'focus-visible:ring',
      );
    }
  });

  it('tints a ticked option with a class that actually compiles', () => {
    // `bg-accent/12` is not on Tailwind's opacity scale and emitted no CSS, so
    // a ticked option looked identical to an unticked one.
    multiCard();
    const option = screen.getByRole('checkbox', { name: /This month/ });
    fireEvent.click(option);
    const cls = option.getAttribute('class') ?? '';
    expect(cls).toContain('bg-accent/10');
    expect(cls).not.toContain('bg-accent/12');
  });

  it('tells the user they can simply type instead', () => {
    card();
    expect(screen.getByText(/just type your answer/)).toBeTruthy();
  });
});

describe('the record left in the transcript', () => {
  it('is one line with no controls at all', () => {
    render(
      <ClarificationRecord
        question="Which period should I use?"
        answer="This quarter"
      />,
    );
    expect(screen.getByText('This quarter')).toBeTruthy();
    expect(screen.getByText('Which period should I use?')).toBeTruthy();
    // A disabled card still reads as something you might be able to use; a
    // thread full of them is a thread full of dead controls.
    expect(screen.queryByRole('button')).toBeNull();
    expect(screen.queryByRole('radio')).toBeNull();
    expect(screen.queryByRole('radiogroup')).toBeNull();
  });
});
