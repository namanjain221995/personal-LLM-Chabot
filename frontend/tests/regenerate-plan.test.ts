/**
 * The rules behind "Try again is a version" (2026-09-13), without a React
 * tree: what a regenerate stores, what it tells the server, what a retry of
 * the same send repeats, and what a refused sync must not throw away.
 *
 * tests/regenerate-versions.test.tsx drives the same rules through the real
 * ChatApp against an in-memory server; these pin each rule on its own so a
 * failure names the rule rather than a screen.
 */

import { describe, expect, it } from 'vitest';
import {
  answerBranchFor,
  answerSelfForIntent,
  buildThread,
  treeIdOf,
  versionInfo,
} from '../lib/branching';
import { toOrchestratorChatRequest } from '../lib/orchestrator';
import {
  intentAnswered,
  planRegenerate,
  withoutFailedAttempt,
} from '../lib/regenerate';
import { reconcileThread, withLocalBranches } from '../lib/threadReconcile';
import type { ChatMessage, Meta } from '../lib/types';

function user(id: string, content: string, meta?: Meta): ChatMessage {
  return { id, role: 'user', content, createdAt: 1, ...(meta ? { meta } : {}) };
}

function answer(
  id: string,
  content: string,
  meta?: Meta,
  extra: Partial<ChatMessage> = {},
): ChatMessage {
  return {
    id,
    role: 'assistant',
    content,
    createdAt: 1,
    status: 'done',
    ...(meta ? { meta } : {}),
    ...extra,
  };
}

const question = user('u', 'Tell me about marigolds.', {
  branch: { self: 'b-q' },
  intent: { id: 'i-first', state: 'completed' },
});
const firstAnswer = answer('a0', 'Marigolds are hardy.', {
  generation_id: 'g0',
  intent_id: 'i-first',
});

describe('planRegenerate for a question that already has an answer', () => {
  it('keeps every stored message and files the new answer beside the old one', () => {
    const all = [question, firstAnswer];
    const plan = planRegenerate(all, question, 'i-new');
    expect(plan.turns).toBe(all);
    expect(plan.assistantBranch).toEqual({ self: 'b-i-new', parent: 'b-q' });
    expect(plan.announceBranch).toBe(true);

    const withNew = [
      ...plan.turns,
      answer('a1', 'Marigolds repel pests.', { branch: plan.assistantBranch }),
    ];
    expect(versionInfo(withNew, withNew[2])).toMatchObject({ number: 2, total: 2 });
    expect(buildThread(withNew).map((m) => m.id)).toEqual(['u', 'a1']);
  });

  it('names a question without branch meta by its position', () => {
    const plain = user('u', 'Tell me about marigolds.', {
      intent: { id: 'i-first', state: 'completed' },
    });
    const plan = planRegenerate([plain, firstAnswer], plain, 'i-new');
    expect(plan.assistantBranch).toEqual({ self: 'b-i-new', parent: '#0' });
  });

  it('keeps the later turns of an older answer instead of truncating them', () => {
    const all = [
      question,
      firstAnswer,
      user('u2', 'How often do I water them?'),
      answer('a2', 'Once a week.'),
    ];
    const plan = planRegenerate(all, question, 'i-new');
    expect(plan.turns).toBe(all);
    expect(plan.assistantBranch.parent).toBe('b-q');
  });
});

describe('planRegenerate for a retry of the SAME send', () => {
  it('re-sends the identical answer_branch its first POST carried', () => {
    const recorded = { self: 'b-i-regen', parent: 'b-q' };
    const failedQuestion = user('u', 'Tell me about marigolds.', {
      branch: { self: 'b-q' },
      intent: { id: 'i-regen', state: 'failed', answer_branch: recorded },
    });
    const failure = answer(
      'a-failed',
      '',
      { branch: recorded, error: { message: 'This answer failed.' } },
      { status: 'error' },
    );
    const all = [failedQuestion, firstAnswer, failure];
    const plan = planRegenerate(all, failedQuestion, 'i-regen');
    expect(plan.assistantBranch).toBe(recorded);
    expect(plan.announceBranch).toBe(true);
    // Only that attempt's failure record goes; the earlier version stays.
    expect(plan.turns.map((m) => m.id)).toEqual(['u', 'a0']);
  });

  it('sends no answer_branch when the first POST of that send had none', () => {
    const unsent = user('u', 'Tell me about marigolds.', {
      branch: { self: 'b-q' },
      intent: { id: 'i-composer', state: 'unsent' },
    });
    const placeholder = answer(
      'a-err',
      '',
      { error: { message: 'Connection unavailable' } },
      { status: 'error' },
    );
    const plan = planRegenerate([unsent, placeholder], unsent, 'i-composer');
    expect(plan.announceBranch).toBe(false);
    expect(plan.turns.map((m) => m.id)).toEqual(['u']);
  });

  it('takes over the failed attempt\'s place in the tree, so that attempt is superseded rather than a version', () => {
    const unsent = user('u', 'Tell me about marigolds.', {
      branch: { self: 'b-q' },
      intent: { id: 'i-composer', state: 'unsent' },
    });
    const slot = { self: 'b-local-slot', parent: 'b-q' };
    const placeholder = answer(
      'a-err',
      '',
      { branch: slot, error: { message: 'Connection unavailable' } },
      { status: 'error' },
    );
    const plan = planRegenerate([unsent, placeholder], unsent, 'i-composer');
    expect(plan.assistantBranch).toBe(slot);
    // Should the browser already have stored the failed row, the answer that
    // follows under the same self hides it: one answer, no `1 / 2`.
    const stored = [unsent, placeholder, answer('a', 'Marigolds are hardy.', { branch: slot })];
    expect(buildThread(stored).map((m) => m.id)).toEqual(['u', 'a']);
    expect(versionInfo(stored, stored[2])).toBeNull();
  });

  it('never drops a failed answer that belongs to a different, later turn', () => {
    const retrying = user('u', 'Tell me about marigolds.', {
      branch: { self: 'b-q' },
      intent: { id: 'i-regen', state: 'failed' },
    });
    const laterFailure = answer(
      'a2-err',
      '',
      { error: { message: 'This answer failed.' } },
      { status: 'error' },
    );
    const all = [retrying, firstAnswer, user('u2', 'And watering?'), laterFailure];
    expect(withoutFailedAttempt(all, retrying)).toBe(all);
  });
});

describe('intentAnswered', () => {
  it('is true for an intent whose answer the server already stored, whatever the label says', () => {
    const staleLabel = user('u', 'q', { intent: { id: 'i-first', state: 'accepted' } });
    expect(intentAnswered([staleLabel, firstAnswer], staleLabel)).toBe(true);
  });

  it('is false while the only row for the intent is a failure record', () => {
    const retrying = user('u', 'q', { intent: { id: 'i-first', state: 'failed' } });
    const record = answer(
      'a',
      '',
      { intent_id: 'i-first', error: { message: 'failed' } },
      { status: 'error' },
    );
    expect(intentAnswered([retrying, record], retrying)).toBe(false);
  });
});

describe('the answer branch helpers', () => {
  it('derive one self per intent, inside the shape the server validates', () => {
    const self = answerSelfForIntent('3f2a9c0d1e');
    expect(self).toBe('b-3f2a9c0d1e');
    expect(self).toMatch(/^b-[A-Za-z0-9_-]{1,64}$/);
  });

  it('name a message by its branch self, its position, or its declared self when absent', () => {
    const all = [user('u', 'q'), firstAnswer];
    expect(treeIdOf(all, all[1])).toBe('#1');
    expect(treeIdOf([question], question)).toBe('b-q');
    expect(treeIdOf([], question)).toBe('b-q');
    expect(answerBranchFor(all, all[0], 'i9')).toEqual({ self: 'b-i9', parent: '#0' });
  });
});

describe('withLocalBranches', () => {
  it('puts back the branch this tab gave an answer the server stored without one', () => {
    const local = [
      question,
      answer('local-a2', 'Marigolds repel pests.', {
        generation_id: 'g2',
        branch: { self: 'b-i2', parent: 'b-q' },
      }),
    ];
    const server = [
      question,
      answer('srv-1', 'Marigolds are hardy.', { generation_id: 'g1' }),
      answer('srv-2', 'Marigolds repel pests.', { generation_id: 'g2' }),
    ];
    const repaired = withLocalBranches(local, server);
    expect(repaired[2].meta?.branch).toEqual({ self: 'b-i2', parent: 'b-q' });
    expect(repaired[2].id).toBe('srv-2');
    // Matched by generation id only: the row at the same POSITION is untouched.
    expect(repaired[1]).toBe(server[1]);
  });

  it('returns the very same array when there is nothing to repair', () => {
    const server = [
      question,
      answer('srv-1', 'x', { generation_id: 'g1', branch: { self: 'b-s', parent: 'b-q' } }),
    ];
    const local = [
      question,
      answer('l', 'x', { generation_id: 'g1', branch: { self: 'b-other', parent: 'b-q' } }),
    ];
    expect(withLocalBranches(local, server)).toBe(server);
    expect(withLocalBranches([], server)).toBe(server);
  });

  it('keeps a version a version when the poll folds a branch-less server copy into the view', () => {
    const local = [
      question,
      firstAnswer,
      answer('local-a1', 'Marigolds repel pests.', {
        generation_id: 'g1',
        branch: { self: 'b-i1', parent: 'b-q' },
      }),
    ];
    const server = [
      question,
      firstAnswer,
      answer('srv-2', 'Marigolds repel pests.', { generation_id: 'g1' }),
    ];
    const merged = reconcileThread(local, server);
    // Without the branch the new answer is a child of the OLD one: stacked.
    expect(buildThread(server).map((m) => m.id)).toEqual(['u', 'a0', 'srv-2']);
    expect(buildThread(merged).map((m) => m.content)).toEqual([
      'Tell me about marigolds.',
      'Marigolds repel pests.',
    ]);
    expect(versionInfo(merged, merged[2])).toMatchObject({ number: 2, total: 2 });
  });
});

describe('the proxy forwards answer_branch', () => {
  const base = { session_id: 's', messages: [{ role: 'user', content: 'hi' }] };

  it('passes it through when the browser sent one', () => {
    const out = toOrchestratorChatRequest({
      ...base,
      intent_id: 'i1',
      answer_branch: { self: 'b-i1', parent: 'b-q' },
    });
    expect(out?.answer_branch).toEqual({ self: 'b-i1', parent: 'b-q' });
  });

  it('keeps an ordinary send byte-identical: no answer_branch key at all', () => {
    const out = toOrchestratorChatRequest({ ...base, intent_id: 'i1' });
    expect(out && 'answer_branch' in out).toBe(false);
    expect(JSON.stringify(out)).toBe(
      JSON.stringify(toOrchestratorChatRequest({ ...base, intent_id: 'i1' })),
    );
  });

  it('forwards exactly self and parent, and nothing that is not that shape', () => {
    const extra = toOrchestratorChatRequest({
      ...base,
      answer_branch: { self: 'b-x', parent: '#0', admin: true } as never,
    });
    expect(extra?.answer_branch).toEqual({ self: 'b-x', parent: '#0' });
    for (const bad of [{ parent: 'b-q' }, { self: 7 }, 'b-x', ['b-x'], { self: 'b-x', parent: 3 }]) {
      const out = toOrchestratorChatRequest({ ...base, answer_branch: bad as never });
      expect(out && 'answer_branch' in out).toBe(false);
    }
  });
});
