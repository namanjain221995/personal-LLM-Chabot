/**
 * Progress phases, and the promise that the indicator never says more than the
 * backend did.
 *
 * The old failure mode this replaces: a spinner with a rotating list of
 * plausible-sounding steps. It looks better and it is a lie — a user who reads
 * "Searching Salesforce" when nothing is being searched cannot tell the real
 * one apart from the decoration.
 */

import { describe, expect, it } from 'vitest';
import {
  accessibleStatus,
  isActivePhase,
  isPhase,
  parsePhaseStatus,
  PHASES,
  starState,
} from '@/lib/phases';
import { toChatStreamEvent } from '@/lib/sse';

describe('phase vocabulary', () => {
  it('matches the backend list exactly', () => {
    // Mirrors app/core/sf_intel/phases.py. A phase the server can emit and the
    // client cannot name shows the user nothing at all.
    expect([...PHASES]).toEqual([
      'understanding',
      'resolving_context',
      'checking_schema',
      'clarifying',
      'querying_salesforce',
      'retrieving_more_results',
      'analyzing_records',
      'calculating',
      'verifying',
      'drafting_answer',
      'reconnecting',
      'completed',
      'failed',
    ]);
  });

  it('rejects anything not on the list', () => {
    expect(isPhase('querying_salesforce')).toBe(true);
    expect(isPhase('thinking_really_hard')).toBe(false);
    expect(isPhase(undefined)).toBe(false);
  });

  it('stops animating on a terminal phase', () => {
    expect(isActivePhase('querying_salesforce')).toBe(true);
    expect(isActivePhase('completed')).toBe(false);
    expect(isActivePhase('failed')).toBe(false);
    // `clarifying` hands over to the card; a star spinning above a question
    // reads as "still working" when the backend is waiting on the user.
    expect(isActivePhase('clarifying')).toBe(false);
    expect(isActivePhase(undefined)).toBe(false);
  });

  it('groups phases into six visual states so the star does not restart', () => {
    expect(starState('checking_schema')).toBe('searching');
    expect(starState('querying_salesforce')).toBe('searching');
    expect(starState('analyzing_records')).toBe('calculating');
    expect(starState('calculating')).toBe('calculating');
    expect(starState('completed')).toBeNull();
    expect(starState('clarifying')).toBeNull();
  });
});

describe('parsing the status event', () => {
  it('reads a typed phase payload', () => {
    const parsed = parsePhaseStatus({
      text: 'Analyzing 42 records',
      phase: 'analyzing_records',
      run_id: 'r1',
      record_count: 42,
    });
    expect(parsed).toEqual({
      phase: 'analyzing_records',
      label: 'Analyzing 42 records',
      run_id: 'r1',
      record_count: 42,
    });
  });

  it('leaves the older web-search progress line alone', () => {
    // A bare `text` is the Phase 1 search/URL line. It keeps its own row and
    // must not drive the star, or the two progress systems fight.
    expect(parsePhaseStatus({ text: 'Reading 5 sources…' })).toBeNull();
  });

  it('refuses a phase the client does not know', () => {
    expect(parsePhaseStatus({ text: 'x', phase: 'inventing_things' })).toBeNull();
  });
});

describe('the SSE status event stays backward compatible', () => {
  it('still yields plain text for a payload with no phase', () => {
    const event = toChatStreamEvent({
      event: 'status',
      data: JSON.stringify({ text: 'Searching the web…' }),
    });
    expect(event).toEqual({ kind: 'status', text: 'Searching the web…' });
  });

  it('attaches the phase when the payload carries one', () => {
    const event = toChatStreamEvent({
      event: 'status',
      data: JSON.stringify({
        text: 'Searching Salesforce',
        phase: 'querying_salesforce',
        run_id: 'r1',
      }),
    });
    expect(event).toMatchObject({
      kind: 'status',
      text: 'Searching Salesforce',
      phase: { phase: 'querying_salesforce', label: 'Searching Salesforce' },
    });
  });

  it('drops a status event with no text at all', () => {
    expect(
      toChatStreamEvent({ event: 'status', data: JSON.stringify({ phase: 'x' }) }),
    ).toBeNull();
  });
});

describe('screen readers', () => {
  it('announces what is happening, not the decoration', () => {
    expect(
      accessibleStatus({ phase: 'querying_salesforce', label: 'Searching Salesforce' }),
    ).toBe('Salesforce assistant is processing the request: Searching Salesforce');
    expect(accessibleStatus({ phase: 'completed', label: 'Done' })).toBe(
      'Answer ready.',
    );
    expect(accessibleStatus(null)).toBe('');
  });
});
