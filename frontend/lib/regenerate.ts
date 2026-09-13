/**
 * "Try again" as a VERSION — what a regenerate sends and what it keeps.
 *
 * 2026-09-13 (duplicate answers). Since V29 the SERVER writes every answer to
 * history before `done`. Regenerate was never updated for that: it still
 * replaced the old answer on the client — a whole-thread PUT the server
 * always refuses now (409), or the truncate endpoint, called only when the
 * tab happened to have loaded more than one answer. So, depending on whether
 * an 8-second poll had landed, a click either stacked a third copy of the
 * answer or silently deleted every earlier one.
 *
 * The fix is the one the edit path already follows: nothing is deleted. The
 * new answer is a sibling of the old ones under the same question — a
 * `‹ 2 / 2 ›` version — and the request TELLS the server where it belongs
 * (`answer_branch`), so the row the server writes is already in the right
 * place whatever the browser does or fails to do afterwards.
 *
 * Pure functions, so the rules are unit-tested without a React tree.
 */

import { answerBranchFor, buildTree, treeIdOf } from './branching';
import type { BranchMeta, ChatMessage } from './types';

/** A row that records an attempt which did not produce an answer. */
export function isFailedAttempt(m: ChatMessage): boolean {
  return (
    m.role === 'assistant' &&
    (m.status === 'error' || m.status === 'queued' || Boolean(m.meta?.error))
  );
}

/**
 * Does the question's CURRENT intent already have its answer in `all`?
 *
 * `intentForRetry` reuses an `accepted` / `processing` intent, which is right
 * for "Send now" on a turn whose answer is still coming. But the question's
 * meta is also adopted from the server copy (a refused PUT, the poll), and
 * that copy can say `accepted` for a send whose answer has since landed. A
 * "Try again" that reused it would make the server REPLAY the stored answer —
 * a click that does nothing. The stored answer carries the intent id on its
 * meta, which settles it.
 */
export function intentAnswered(all: ChatMessage[], question: ChatMessage): boolean {
  const id = question.meta?.intent?.id;
  if (!id) return false;
  return all.some(
    (m) =>
      m.role === 'assistant' &&
      m.meta?.intent_id === id &&
      m.status !== 'streaming' &&
      !isFailedAttempt(m) &&
      m.content.trim() !== '',
  );
}

export interface RegeneratePlan {
  /**
   * What the conversation stores once the answer is appended to it: every
   * message — other versions, other branches, later turns — minus only the
   * failure record of THIS send's previous attempt (which the server
   * discards on the retry too). Never a truncation.
   */
  turns: ChatMessage[];
  /** Where the answer goes in the tree. */
  assistantBranch: BranchMeta;
  /**
   * Send `assistantBranch` to the server as `answer_branch`. False only for
   * a retry of a send whose first POST did not carry one, so every attempt
   * of one intent sends the same body.
   */
  announceBranch: boolean;
}

/**
 * Re-ask `question` under `intentId` (see ChatApp's intentForRegenerate).
 *
 * A NEW intent is a new version: `answer_branch` = a child of the question,
 * with a self derived from the intent. The SAME intent (a retry of a send
 * that never finished) re-sends exactly the branch its first POST carried —
 * recorded on `meta.intent.answer_branch` — or none, if that POST had none.
 */
export function planRegenerate(
  all: ChatMessage[],
  question: ChatMessage,
  intentId: string,
): RegeneratePlan {
  const intent = question.meta?.intent;
  const sameSend = intent?.id === intentId;
  const turns = sameSend ? withoutFailedAttempt(all, question) : all;
  if (sameSend && intent?.answer_branch) {
    return { turns, assistantBranch: intent.answer_branch, announceBranch: true };
  }
  if (sameSend) {
    // A send whose first POST carried no branch. Its failed attempt's row
    // still names a place in the tree, and the retry takes THAT place: the
    // browser may already have saved the row to the server, and a later row
    // with the same `self` supersedes it (branching.ts buildTree) instead of
    // standing beside the answer as a version of its own.
    const questionId = treeIdOf(all, question);
    const superseded = all
      .slice(turns.length)
      .map((m) => m.meta?.branch)
      .filter((b) => b !== undefined && b.parent === questionId)
      .pop();
    return {
      turns,
      assistantBranch: superseded ?? answerBranchFor(turns, question, intentId),
      announceBranch: false,
    };
  }
  return {
    turns,
    assistantBranch: answerBranchFor(turns, question, intentId),
    announceBranch: true,
  };
}

/**
 * `all` without the trailing failure records filed under `question` — the
 * placeholder of an attempt that is about to be retried under the same
 * intent. Only a TAIL is removed, so no earlier message changes position
 * (the positional `#i` ids of rows without branch meta depend on that), and
 * only rows whose parent in the tree is the question: a failed answer to a
 * different, later turn is that turn's record and stays.
 */
export function withoutFailedAttempt(
  all: ChatMessage[],
  question: ChatMessage,
): ChatMessage[] {
  const questionId = treeIdOf(all, question);
  if (questionId === undefined) return all;
  const { parents } = buildTree(all);
  let end = all.length;
  while (
    end > 0 &&
    all[end - 1].id !== question.id &&
    isFailedAttempt(all[end - 1]) &&
    parents[end - 1] === questionId
  ) {
    end -= 1;
  }
  return end === all.length ? all : all.slice(0, end);
}
