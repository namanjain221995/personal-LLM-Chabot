// @vitest-environment jsdom
/**
 * The `‹ 2 / 2 ›` control under a turn that has been edited.
 *
 * Its whole job is to be inert. Every version is already in memory, so moving
 * between them is view selection and nothing else — no generation, no
 * truncate, no request of any kind. A navigator that quietly re-asked the
 * model would defeat the point of keeping both answers.
 *
 * It also must not appear when there is nothing to navigate: `1 / 1` is a
 * control that can only disappoint.
 */

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MessageRow } from '@/components/MessageRow';
import { buildThread, versionMap } from '@/lib/branching';
import type { BranchMeta, ChatMessage } from '@/lib/types';

afterEach(cleanup);

let fetchSpy: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchSpy = vi.fn(async () => {
    throw new Error('the version navigator must not reach the network');
  });
  vi.stubGlobal('fetch', fetchSpy);
  Object.assign(navigator, {
    clipboard: { writeText: vi.fn(async () => undefined) },
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

let seq = 0;
function msg(
  role: 'user' | 'assistant',
  content: string,
  branch?: BranchMeta,
): ChatMessage {
  seq += 1;
  return {
    id: `id${seq}`,
    role,
    content,
    createdAt: seq,
    ...(branch ? { meta: { branch } } : {}),
  };
}

/** "Explain Docker" answered, then edited into a second version. */
function edited() {
  const u1 = msg('user', 'Explain Docker');
  const a1 = msg('assistant', 'Original Docker answer');
  const u2 = msg('user', 'Explain Docker with an example', { self: 'v2' });
  const a2 = msg('assistant', 'New answer with example', {
    self: 'a2',
    parent: 'v2',
  });
  return { all: [u1, a1, u2, a2], u1, a1, u2, a2 };
}

function renderRow(
  message: ChatMessage,
  all: ChatMessage[],
  onSelectVersion = vi.fn(),
) {
  render(
    <MessageRow
      message={message}
      isLast={false}
      onRegenerate={vi.fn()}
      onRetry={vi.fn()}
      onEditStart={vi.fn()}
      versions={versionMap(all).get(message.id) ?? null}
      onSelectVersion={onSelectVersion}
    />,
  );
  return { onSelectVersion };
}

const prev = () => screen.getByRole('button', { name: 'Previous version' });
const next = () => screen.getByRole('button', { name: 'Next version' });

describe('when a turn has only one version', () => {
  it('renders no navigator at all', () => {
    const all = [msg('user', 'only one'), msg('assistant', 'answer')];
    renderRow(all[0], all);
    expect(screen.queryByRole('button', { name: 'Previous version' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Next version' })).toBeNull();
    expect(screen.queryByText('1 / 1')).toBeNull();
  });
});

describe('when a turn has been edited', () => {
  it('shows 2 / 2 on the edited version', () => {
    const { all, u2 } = edited();
    renderRow(u2, all);
    expect(screen.getByText(/2\s*\/\s*2/)).toBeTruthy();
  });

  it('shows 1 / 2 on the original', () => {
    const { all, u1 } = edited();
    renderRow(u1, all);
    expect(screen.getByText(/1\s*\/\s*2/)).toBeTruthy();
  });

  it('asks for the ORIGINAL when previous is clicked from 2 / 2', () => {
    const { all, u2 } = edited();
    const { onSelectVersion } = renderRow(u2, all);

    fireEvent.click(prev());

    // '' is the root fork; '#0' is the original's durable id.
    expect(onSelectVersion).toHaveBeenCalledTimes(1);
    expect(onSelectVersion).toHaveBeenCalledWith('', '#0');
  });

  it('asks for the EDIT when next is clicked from 1 / 2', () => {
    const { all, u1 } = edited();
    const { onSelectVersion } = renderRow(u1, all);

    fireEvent.click(next());

    expect(onSelectVersion).toHaveBeenCalledWith('', 'v2');
  });

  it('disables the arrow at each end instead of removing it', () => {
    const { all, u1, u2 } = edited();
    renderRow(u1, all);
    expect((prev() as HTMLButtonElement).disabled).toBe(true);
    expect((next() as HTMLButtonElement).disabled).toBe(false);
    cleanup();

    renderRow(u2, all);
    // The control keeps its width as you move through versions.
    expect((prev() as HTMLButtonElement).disabled).toBe(false);
    expect((next() as HTMLButtonElement).disabled).toBe(true);
  });

  it('does nothing at all when a disabled end is clicked', () => {
    const { all, u1 } = edited();
    const { onSelectVersion } = renderRow(u1, all);
    fireEvent.click(prev());
    expect(onSelectVersion).not.toHaveBeenCalled();
  });

  it('makes NO request — not to generate, not to truncate', () => {
    const { all, u2 } = edited();
    renderRow(u2, all);
    fireEvent.click(prev());
    fireEvent.click(next());
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('stays visible without hovering, unlike the other actions', () => {
    const { all, u2 } = edited();
    renderRow(u2, all);
    // The navigator is the only sign that another answer exists; hiding it
    // until hover would hide the fact itself.
    const row = prev().parentElement?.parentElement as HTMLElement;
    expect(row.className).not.toContain('opacity-0');
  });
});

describe('Copy follows the version on screen', () => {
  it('copies version 1 s text from version 1 s row', () => {
    const { all, u1 } = edited();
    renderRow(u1, all);
    fireEvent.click(screen.getByRole('button', { name: 'Copy message' }));
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith('Explain Docker');
  });

  it('copies version 2 s text from version 2 s row', () => {
    const { all, u2 } = edited();
    renderRow(u2, all);
    fireEvent.click(screen.getByRole('button', { name: 'Copy message' }));
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
      'Explain Docker with an example',
    );
  });
});

describe('the rendered thread pairs each question with its own answer', () => {
  it('never shows version 1 s question above version 2 s answer', () => {
    const { all } = edited();
    // Default (newest) selection.
    expect(buildThread(all).map((m) => m.content)).toEqual([
      'Explain Docker with an example',
      'New answer with example',
    ]);
    // Original selected.
    expect(buildThread(all, { '': '#0' }).map((m) => m.content)).toEqual([
      'Explain Docker',
      'Original Docker answer',
    ]);
  });
});

describe('an answer that was retried', () => {
  it('gets the same control, so the earlier answer stays reachable', () => {
    const u = msg('user', 'Q', { self: 'u1' });
    const a1 = msg('assistant', 'first answer', { self: 'a1', parent: 'u1' });
    const a2 = msg('assistant', 'second answer', { self: 'a2', parent: 'u1' });
    const all = [u, a1, a2];
    const { onSelectVersion } = renderRow(a2, all);

    expect(screen.getByText(/2\s*\/\s*2/)).toBeTruthy();
    fireEvent.click(prev());
    expect(onSelectVersion).toHaveBeenCalledWith('u1', 'a1');
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});

describe('three versions', () => {
  it('counts 1 / 3 · 2 / 3 · 3 / 3 in a single navigator', () => {
    const { all, u1, u2 } = edited();
    const u3 = msg('user', 'third wording', { self: 'v3' });
    const full = [...all, u3];

    for (const [message, label] of [
      [u1, '1 / 3'],
      [u2, '2 / 3'],
      [u3, '3 / 3'],
    ] as const) {
      renderRow(message, full);
      expect(screen.getByText(label)).toBeTruthy();
      // One navigator per turn, never a navigator inside a navigator.
      expect(screen.getAllByRole('button', { name: 'Next version' })).toHaveLength(1);
      cleanup();
    }
  });
});
