/**
 * The conversation tree behind ChatGPT-style editing.
 *
 * The behaviour under test is the one the destructive version got wrong:
 * asking a question a second way must not cost you the first answer. Every
 * case here is about what SURVIVES an edit, and about which single path down
 * the tree is handed to the model — a prompt that contained both versions of
 * a turn would be two contradictory histories in one context window.
 */

import { describe, expect, it } from 'vitest';
import type { BranchSelection } from '../lib/branching';
import {
  branchForAppend,
  branchForVersion,
  buildThread,
  buildTree,
  hasBranches,
  metaWithBranch,
  ROOT,
  selectVersion,
  versionInfo,
} from '../lib/branching';
import type { BranchMeta, ChatMessage } from '../lib/types';

let seq = 0;
function msg(
  role: 'user' | 'assistant',
  content: string,
  branch?: BranchMeta,
): ChatMessage {
  seq += 1;
  return {
    id: `m${seq}-${content}`,
    role,
    content,
    createdAt: seq,
    ...(branch ? { meta: { branch } } : {}),
  };
}

const texts = (ms: ChatMessage[]) => ms.map((m) => m.content);

/* ------------------------------------------------ legacy, untouched threads */

describe('a conversation with no branch metadata at all', () => {
  it('reads back exactly as it was stored', () => {
    const all = [
      msg('user', 'U1'),
      msg('assistant', 'A1'),
      msg('user', 'U2'),
      msg('assistant', 'A2'),
    ];
    // The whole point: every pre-existing thread works with nothing written
    // to it. No migration, no rewrite, no metadata backfilled onto history.
    expect(texts(buildThread(all))).toEqual(['U1', 'A1', 'U2', 'A2']);
    expect(hasBranches(all)).toBe(false);
  });

  it('has no version navigator on any message', () => {
    const all = [msg('user', 'U1'), msg('assistant', 'A1')];
    expect(versionInfo(all, all[0])).toBeNull();
    expect(versionInfo(all, all[1])).toBeNull();
  });

  it('handles the empty conversation', () => {
    expect(buildThread([])).toEqual([]);
    expect(hasBranches([])).toBe(false);
  });
});

/* ---------------------------------------------------- editing the first turn */

/** U1/A1/U2/A2, then U1 edited into U1v2 which is answered A1v2. */
function editedFirstTurn() {
  const all = [
    msg('user', 'U1'),
    msg('assistant', 'A1'),
    msg('user', 'U2'),
    msg('assistant', 'A2'),
  ];
  const v2 = branchForVersion(all, all[0]);
  const u1v2 = msg('user', 'U1v2', v2);
  const a1v2 = msg('assistant', 'A1v2', { self: 'a1v2', parent: v2.self });
  return { all: [...all, u1v2, a1v2], u1: all[0], u1v2, v2 };
}

describe('editing a turn appends a version instead of replacing it', () => {
  it('keeps the original message, its answer AND its continuation stored', () => {
    const { all } = editedFirstTurn();
    // Nothing was deleted — the destructive implementation truncated all four
    // of these away the moment the edit was sent.
    expect(texts(all)).toEqual(['U1', 'A1', 'U2', 'A2', 'U1v2', 'A1v2']);
  });

  it('shows the edited branch by default', () => {
    const { all } = editedFirstTurn();
    expect(texts(buildThread(all))).toEqual(['U1v2', 'A1v2']);
  });

  it('shows the ORIGINAL branch, answer and continuation when selected', () => {
    const { all, u1 } = editedFirstTurn();
    const info = versionInfo(all, u1)!;
    const thread = buildThread(all, { [info.parent]: '#0' });
    expect(texts(thread)).toEqual(['U1', 'A1', 'U2', 'A2']);
  });

  it('reports 2 versions, and points each way', () => {
    const { all, u1, u1v2, v2 } = editedFirstTurn();
    const original = versionInfo(all, u1)!;
    expect(original).toMatchObject({ number: 1, total: 2, parent: ROOT });
    expect(original.previous).toBeUndefined();
    expect(original.next).toBe(v2.self);

    const edited = versionInfo(all, u1v2)!;
    expect(edited).toMatchObject({ number: 2, total: 2, parent: ROOT });
    expect(edited.previous).toBe('#0');
    expect(edited.next).toBeUndefined();
  });

  it('never pairs one version s question with the other s answer', () => {
    const { all, u1 } = editedFirstTurn();
    const info = versionInfo(all, u1)!;
    const v1 = texts(buildThread(all, { [info.parent]: '#0' }));
    const v2 = texts(buildThread(all));
    expect(v1).toContain('A1');
    expect(v1).not.toContain('A1v2');
    expect(v2).toContain('A1v2');
    expect(v2).not.toContain('A1');
  });

  it('is recognised as branched, which gates destructive truncation', () => {
    const { all } = editedFirstTurn();
    expect(hasBranches(all)).toBe(true);
  });
});

/* ------------------------------------------------------- editing repeatedly */

describe('editing an already-edited turn', () => {
  it('makes a third version in ONE group, not a nested one', () => {
    const { all, u1, u1v2 } = editedFirstTurn();
    const v3 = branchForVersion(all, u1v2);
    const u1v3 = msg('user', 'U1v3', v3);
    const full = [...all, u1v3];

    // All three are alternatives of the same logical turn, so there is one
    // navigator reading 1/3 · 2/3 · 3/3 — not a navigator inside a navigator.
    expect(versionInfo(full, u1)).toMatchObject({ number: 1, total: 3 });
    expect(versionInfo(full, u1v2)).toMatchObject({ number: 2, total: 3 });
    expect(versionInfo(full, u1v3)).toMatchObject({ number: 3, total: 3 });
    expect(texts(buildThread(full))).toEqual(['U1v3']);
  });

  it('can still reach version 1 and version 2', () => {
    const { all, u1v2, v2 } = editedFirstTurn();
    const u1v3 = msg('user', 'U1v3', branchForVersion(all, u1v2));
    const full = [...all, u1v3];
    expect(texts(buildThread(full, { [ROOT]: '#0' }))).toEqual([
      'U1', 'A1', 'U2', 'A2',
    ]);
    expect(texts(buildThread(full, { [ROOT]: v2.self }))).toEqual([
      'U1v2', 'A1v2',
    ]);
  });
});

/* ------------------------------------ continuing from the branch on screen */

describe('a follow-up continues the branch that is selected', () => {
  it('extends version 1 without touching version 2', () => {
    const { all, v2 } = editedFirstTurn();
    const selection = { [ROOT]: '#0' };
    const thread = buildThread(all, selection);

    const follow = msg('user', 'U3', branchForAppend(all, thread));
    const full = [...all, follow];

    expect(texts(buildThread(full, selection))).toEqual([
      'U1', 'A1', 'U2', 'A2', 'U3',
    ]);
    // Version 2 is untouched by a follow-up asked on version 1.
    expect(texts(buildThread(full, { [ROOT]: v2.self }))).toEqual([
      'U1v2', 'A1v2',
    ]);
  });

  it('extends version 2 without touching version 1', () => {
    const { all } = editedFirstTurn();
    const thread = buildThread(all); // v2 is the default
    const follow = msg('user', 'U3', branchForAppend(all, thread));
    const full = [...all, follow];

    expect(texts(buildThread(full))).toEqual(['U1v2', 'A1v2', 'U3']);
    expect(texts(buildThread(full, { [ROOT]: '#0' }))).toEqual([
      'U1', 'A1', 'U2', 'A2',
    ]);
  });

  it('never leaks the sibling branch into the path sent to the model', () => {
    // The failure this guards: a prompt containing U1, A1, U1v2 AND A1v2 is
    // two contradictory histories of the same turn in one context window.
    const { all } = editedFirstTurn();
    const cases: BranchSelection[] = [{}, { [ROOT]: '#0' }];
    for (const selection of cases) {
      const thread = texts(buildThread(all, selection));
      const hasOriginal = thread.includes('U1');
      expect(thread.includes('U1v2')).toBe(!hasOriginal);
    }
  });
});

/* -------------------------------------------------------- assistant retries */

describe('two answers under one question', () => {
  it('shows the newest and keeps the older one stored', () => {
    const u = msg('user', 'U1', { self: 'u1' });
    const a1 = msg('assistant', 'A1', { self: 'a1', parent: 'u1' });
    const a2 = msg('assistant', 'A2', { self: 'a2', parent: 'u1' });
    const all = [u, a1, a2];
    expect(texts(buildThread(all))).toEqual(['U1', 'A2']);
    expect(texts(buildThread(all, { u1: 'a1' }))).toEqual(['U1', 'A1']);
  });
});

/* --------------------------------------------------------------- robustness */

describe('the walk is total', () => {
  it('re-parents a message whose declared parent was truncated away', () => {
    const orphan = msg('assistant', 'orphan', {
      self: 'x',
      parent: 'a-branch-that-no-longer-exists',
    });
    const all = [msg('user', 'U1'), orphan];
    // It falls back to the physical predecessor rather than vanishing.
    expect(texts(buildThread(all))).toEqual(['U1', 'orphan']);
  });

  it('does not hang on metadata that points at itself', () => {
    const loop = msg('user', 'loop', { self: 'l1', parent: 'l1' });
    expect(() => buildThread([loop])).not.toThrow();
    expect(buildThread([loop]).length).toBeLessThanOrEqual(1);
  });

  it('treats a mix of branched and legacy messages sensibly', () => {
    const all = [
      msg('user', 'U1'),
      msg('assistant', 'A1'),
      msg('user', 'U2', { self: 'u2', parent: '#1' }),
    ];
    expect(texts(buildThread(all))).toEqual(['U1', 'A1', 'U2']);
  });
});

describe('supporting helpers', () => {
  it('metaWithBranch preserves everything else on the meta', () => {
    const out = metaWithBranch(
      { route: 'sql', sql: 'SELECT 1' },
      { self: 'b1', parent: 'p' },
    );
    expect(out).toMatchObject({
      route: 'sql',
      sql: 'SELECT 1',
      branch: { self: 'b1', parent: 'p' },
    });
  });

  it('metaWithBranch is a no-op without a branch', () => {
    expect(metaWithBranch({ route: 'chat' }, undefined)).toEqual({
      route: 'chat',
    });
  });

  it('branchForVersion inherits the original s parent — that is the pairing', () => {
    const all = [msg('user', 'U1'), msg('assistant', 'A1'), msg('user', 'U2')];
    // Editing U2 makes it a sibling of U2 under A1, so both versions answer
    // from the same point in the conversation.
    const v = branchForVersion(all, all[2]);
    expect(v.parent).toBe('#1');
    expect(buildTree(all).parents[2]).toBe('#1');
  });

  it('branchForVersion on the first turn has no parent', () => {
    const all = [msg('user', 'U1'), msg('assistant', 'A1')];
    expect(branchForVersion(all, all[0]).parent).toBeUndefined();
  });

  it('selectVersion records the choice and forgets dead forks', () => {
    const { all, v2 } = editedFirstTurn();
    const next = selectVersion(all, { 'a-gone-fork': 'x' }, ROOT, v2.self);
    expect(next[ROOT]).toBe(v2.self);
    expect(next['a-gone-fork']).toBeUndefined();
  });

  it('gives every message a distinct durable id', () => {
    const { all } = editedFirstTurn();
    const { ids } = buildTree(all);
    expect(new Set(ids).size).toBe(all.length);
  });
});

/* ------------------------------------------------- survives a real reload */

describe('reconstruction from what the SERVER actually stores', () => {
  /**
   * Captured verbatim from a live round-trip through the orchestrator and
   * PostgreSQL (POST /history/conversations/{id}/messages, then GET). Two
   * things matter here and neither is hypothetical:
   *
   * 1. `meta.branch` comes back byte-for-byte — the server types meta as an
   *    unvalidated dict and stores it as JSON, so no schema change was needed.
   * 2. A reload renumbers every `ChatMessage.id` to `srv-<conversation>-<i>`,
   *    which is exactly why the tree is keyed on durable ids inside meta
   *    rather than on message ids. An id-based design would dangle here.
   */
  const hydrated: ChatMessage[] = [
    { id: 'srv-c-0', role: 'user', content: 'Explain Docker', createdAt: 1 },
    {
      id: 'srv-c-1',
      role: 'assistant',
      content: 'Original Docker answer',
      createdAt: 2,
    },
    {
      id: 'srv-c-2',
      role: 'user',
      content: 'Explain Docker with an example',
      createdAt: 3,
      meta: { branch: { self: 'b-v2' } },
    },
    {
      id: 'srv-c-3',
      role: 'assistant',
      content: 'New answer with example',
      createdAt: 4,
      meta: { branch: { self: 'b-a2', parent: 'b-v2' } },
    },
  ];

  it('still has both versions after the ids were rewritten', () => {
    expect(versionInfo(hydrated, hydrated[0])).toMatchObject({
      number: 1,
      total: 2,
    });
    expect(versionInfo(hydrated, hydrated[2])).toMatchObject({
      number: 2,
      total: 2,
    });
  });

  it('reads the edited branch by default', () => {
    expect(texts(buildThread(hydrated))).toEqual([
      'Explain Docker with an example',
      'New answer with example',
    ]);
  });

  it('reads the original branch and its original answer when selected', () => {
    expect(texts(buildThread(hydrated, { [ROOT]: '#0' }))).toEqual([
      'Explain Docker',
      'Original Docker answer',
    ]);
  });
});
