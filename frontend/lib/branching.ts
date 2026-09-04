/**
 * The conversation as a TREE — ChatGPT-style non-destructive editing.
 *
 * Editing a user turn used to REPLACE it: the original message, its answer and
 * every turn after it were deleted through the truncate endpoint, and the only
 * surviving history was the edited path. Asking a question a second way cost
 * you the first answer.
 *
 * A conversation is really a tree. Editing a turn adds an alternative version
 * NEXT TO the original rather than on top of it, and what you read is one path
 * down that tree. Both answers keep existing; the `< 2 / 2 >` control under an
 * edited message chooses which path is live.
 *
 * ── Why this needs no migration, no rewrite, and no schema change ───────────
 *
 * Storage stays exactly what it was: one flat, append-only list per
 * conversation. The tree lives in two fields on `meta.branch` — `self` (a
 * durable id) and `parent` — and ONLY on messages created since this feature.
 *
 * A message without them is a child of whatever physically precedes it, which
 * is precisely what a linear conversation already is. So every pre-existing
 * thread reads back correctly with nothing written to it, and an edit never
 * has to touch the messages it branches from. That is the whole reason the
 * original turn survives: we only ever append.
 *
 * The positional fallback id (`#3`) is safe for the same reason the server's
 * own replace endpoint documents — "cannot shrink a thread, only append to
 * it, so index N before is index N after". Truncation is the one operation
 * that could invalidate it, which is why `hasBranches` gates it (see
 * ChatApp's regenerate).
 *
 * ── Why siblings need no explicit "version group" ───────────────────────────
 *
 * Two children of the same node are, by construction, alternative versions of
 * the same turn: a conversation never legitimately continues in two directions
 * at once. So the parent pointer alone expresses versioning, and an edit needs
 * to write nothing to the message it is a version of.
 */

import type { BranchMeta, ChatMessage, Meta } from './types';

/** Parent id of a message that starts the thread. */
export const ROOT = '';

/** A fresh durable id. Not `ChatMessage.id` — see BranchMeta. */
export function newBranchId(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) {
    return `b-${crypto.randomUUID()}`;
  }
  return `b-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

export function branchOf(m: ChatMessage | undefined): BranchMeta | undefined {
  return m?.meta?.branch;
}

/** Attach tree position to a meta object without disturbing anything else. */
export function metaWithBranch(
  meta: Meta | undefined,
  branch: BranchMeta | undefined,
): Meta | undefined {
  if (!branch) return meta;
  return { ...(meta ?? {}), branch };
}

/**
 * Which messages are alternatives of which, for one conversation.
 *
 * Indices are positions in `all`, so this is only ever valid for the COMPLETE
 * stored list — handing it a slice would renumber the positional fallback ids
 * and silently re-parent everything.
 */
export interface Tree {
  /** Durable id of each message, by position. */
  ids: string[];
  /** Durable id of each message's parent (ROOT for thread starts). */
  parents: string[];
  /** Child positions by parent id, in document order (oldest version first). */
  children: Map<string, number[]>;
}

export function buildTree(all: ChatMessage[]): Tree {
  const ids = all.map((m, i) => branchOf(m)?.self ?? `#${i}`);
  const known = new Set(ids);
  const parents = all.map((m, i) => {
    const declared = branchOf(m)?.parent;
    // A declared parent that is no longer present (its branch was truncated
    // away) would strand this message and everything under it. Fall back to
    // the physical predecessor so the walk stays total.
    if (declared !== undefined && known.has(declared)) return declared;
    if (declared === undefined && branchOf(m)) return ROOT;
    return i === 0 ? ROOT : ids[i - 1];
  });
  const children = new Map<string, number[]>();
  for (let i = 0; i < all.length; i += 1) {
    const list = children.get(parents[i]);
    if (list) list.push(i);
    else children.set(parents[i], [i]);
  }
  return { ids, parents, children };
}

/**
 * Which alternative is live at each fork: parent durable id → child durable id.
 *
 * Absent entries mean "the newest", which is what an edit selects and what a
 * freshly reloaded conversation shows.
 */
export type BranchSelection = Record<string, string>;

function pickChild(
  candidates: number[],
  ids: string[],
  selected: string | undefined,
): number {
  if (selected !== undefined) {
    const hit = candidates.find((i) => ids[i] === selected);
    if (hit !== undefined) return hit;
  }
  return candidates[candidates.length - 1];
}

/**
 * The visible conversation: one path from the start, taking the selected
 * alternative at every fork.
 *
 * This is what the thread renders AND what the model is sent, which is what
 * keeps a sibling branch's turns out of the prompt.
 */
export function buildThread(
  all: ChatMessage[],
  selection: BranchSelection = {},
): ChatMessage[] {
  return threadIndices(all, selection).map((i) => all[i]);
}

/**
 * The POSITIONS in `all` of the visible path — what `buildThread` then looks
 * the messages up at.
 *
 * Split out because the walk reads only the tree (ids, parents, order) and
 * never message content, so a caller that re-derives the thread on every
 * streaming token can cache the path against `treeShape` and pay for nothing
 * but the index lookup. `buildThread` is this plus that lookup, so there is
 * still exactly one walk rather than two implementations that could drift.
 */
export function threadIndices(
  all: ChatMessage[],
  selection: BranchSelection = {},
): number[] {
  if (all.length === 0) return [];
  const { ids, children } = buildTree(all);
  const out: number[] = [];
  const seen = new Set<number>();
  let cursor = ROOT;
  for (;;) {
    const candidates = children.get(cursor);
    if (!candidates || candidates.length === 0) break;
    const pick = pickChild(candidates, ids, selection[cursor]);
    if (seen.has(pick)) break; // corrupt metadata must not hang the render
    seen.add(pick);
    out.push(pick);
    cursor = ids[pick];
  }
  return out;
}

/**
 * A string that changes exactly when the TREE does — NEW-24.
 *
 * It carries everything `buildTree` reads (order, message ids, branch
 * pointers) and deliberately nothing else. Content is absent on purpose: a
 * streaming answer replaces its message object on every frame without moving
 * anything in the tree, so a derivation keyed on this skips the walk for the
 * whole generation AND keeps the identity of what it produced — which is
 * what stops a `versions` prop from re-rendering a memoized row (M-08).
 */
export function treeShape(all: ChatMessage[]): string {
  let out = String(all.length);
  for (const m of all) {
    const branch = branchOf(m);
    out += `\u0001${m.id}\u0002${branch?.self ?? ''}\u0002${branch?.parent ?? ''}`;
  }
  return out;
}

/** True once any turn has more than one version — i.e. a fork exists. */
export function hasBranches(all: ChatMessage[]): boolean {
  const { children } = buildTree(all);
  for (const list of children.values()) if (list.length > 1) return true;
  return false;
}

/** What the `< n / total >` control needs for one message. */
export interface VersionInfo {
  /** 1-based position of this version among the alternatives. */
  number: number;
  /** How many alternatives exist. 1 means there is nothing to navigate. */
  total: number;
  /** The fork to re-point, and the ids to point it at. */
  parent: string;
  previous?: string;
  next?: string;
}

/**
 * The versions of the turn `message` belongs to, or null when it is the only
 * one — a `1 / 1` navigator is noise, so it is never rendered.
 */
export function versionInfo(
  all: ChatMessage[],
  message: ChatMessage,
): VersionInfo | null {
  const index = all.findIndex((m) => m.id === message.id);
  if (index === -1) return null;
  const { ids, parents, children } = buildTree(all);
  const parent = parents[index];
  const siblings = children.get(parent) ?? [];
  if (siblings.length < 2) return null;
  const at = siblings.indexOf(index);
  if (at === -1) return null;
  return {
    number: at + 1,
    total: siblings.length,
    parent,
    ...(at > 0 ? { previous: ids[siblings[at - 1]] } : {}),
    ...(at < siblings.length - 1 ? { next: ids[siblings[at + 1]] } : {}),
  };
}

/**
 * Every message that HAS alternatives, keyed by `ChatMessage.id`.
 *
 * One tree walk for the whole conversation, rather than `versionInfo` per
 * rendered row — that is the same walk repeated once per message, which on a
 * long thread is quadratic for a control most rows never show.
 *
 * Messages with a single version are absent: `1 / 1` is not a navigator.
 */
export function versionMap(all: ChatMessage[]): Map<string, VersionInfo> {
  const out = new Map<string, VersionInfo>();
  const { ids, parents, children } = buildTree(all);
  for (let i = 0; i < all.length; i += 1) {
    const siblings = children.get(parents[i]);
    if (!siblings || siblings.length < 2) continue;
    const at = siblings.indexOf(i);
    if (at === -1) continue;
    out.set(all[i].id, {
      number: at + 1,
      total: siblings.length,
      parent: parents[i],
      ...(at > 0 ? { previous: ids[siblings[at - 1]] } : {}),
      ...(at < siblings.length - 1 ? { next: ids[siblings[at + 1]] } : {}),
    });
  }
  return out;
}

/**
 * The tree position for a message appended to the end of `thread`.
 *
 * Used for an ordinary send and for a streaming answer: both continue the
 * path currently on screen, so a follow-up asked while version 1 is selected
 * belongs to version 1's branch and not to the newest one.
 */
export function branchForAppend(
  all: ChatMessage[],
  thread: ChatMessage[],
): BranchMeta {
  const last = thread[thread.length - 1];
  if (!last) return { self: newBranchId() };
  const { ids } = buildTree(all);
  const index = all.findIndex((m) => m.id === last.id);
  const parent = index === -1 ? branchOf(last)?.self : ids[index];
  return { self: newBranchId(), ...(parent ? { parent } : {}) };
}

/**
 * The tree position for a NEW VERSION of `message` — the edit itself.
 *
 * It takes the original's parent, which is what makes the two siblings, and
 * is why nothing has to be written to the original.
 */
export function branchForVersion(
  all: ChatMessage[],
  message: ChatMessage,
): BranchMeta {
  const index = all.findIndex((m) => m.id === message.id);
  if (index === -1) return { self: newBranchId() };
  const { parents } = buildTree(all);
  const parent = parents[index];
  return { self: newBranchId(), ...(parent !== ROOT ? { parent } : {}) };
}

/**
 * Select `id` at `parent`, and drop selections for forks that are no longer
 * reachable so the map cannot grow without bound across a long session.
 */
export function selectVersion(
  all: ChatMessage[],
  selection: BranchSelection,
  parent: string,
  id: string,
): BranchSelection {
  const next: BranchSelection = { ...selection, [parent]: id };
  const { children } = buildTree(all);
  for (const key of Object.keys(next)) {
    if (!children.has(key)) delete next[key];
  }
  return next;
}
