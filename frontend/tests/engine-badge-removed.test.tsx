// @vitest-environment jsdom
/**
 * The passive engine badge is gone (owner request 2026-09-03).
 *
 * "Vision" / "Chat" / "Records" told the reader which route answered — a fact
 * about the machine, not about the answer — and it sat in the corner of every
 * conversation whether or not anyone wanted it. Two places rendered it: the
 * thread header (the last engine used) and the proof drawer's bar.
 *
 * What is NOT gone, and is asserted here so a future "cleanup" cannot take it:
 * `meta.route` itself, the proof drawer's real sections, and every
 * user-selectable source control. Removing a label is not removing a mode.
 *
 * These tests name the badge by the text it rendered rather than by a
 * component, because the requirement is about what a reader sees. They are
 * deliberately scoped to the message/proof surfaces — a global "the word
 * Dataset may never appear" assertion would be wrong, since the composer
 * legitimately labels an attached dataset.
 */

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MessageRow } from '@/components/MessageRow';
import { ProofDrawer } from '@/components/ProofDrawer';
import { composerMenuItems } from '@/lib/composerMenu';
import { DEFAULT_PREFS } from '@/lib/prefs';
import type { ChatMessage, Engine, Meta } from '@/lib/types';

afterEach(cleanup);

const answer = (meta: Meta): ChatMessage => ({
  id: 'a1',
  role: 'assistant',
  content: 'Here is the answer.',
  status: 'done',
  createdAt: 0,
  meta,
});

function renderAnswer(meta: Meta) {
  return render(
    <MessageRow
      message={answer(meta)}
      isLast
      onRegenerate={vi.fn()}
      onRetry={vi.fn()}
    />,
  );
}

/** Every label the badge used to be able to print. */
const BADGE_LABELS = [
  'Vision',
  'Chat',
  'Records',
  'SQL',
  'Report',
  'Web',
  'Research',
  'Page',
  'Site',
  'Repo',
  'Question',
  'Agent',
];

describe('BADGE · no passive engine label is rendered', () => {
  const routes: Engine[] = ['vision', 'chat', 'rag', 'sql', 'report', 'search', 'agent'];

  it.each(routes)('BADGE-01/02/03 · route "%s" prints no badge', (route) => {
    renderAnswer({ route });
    for (const label of BADGE_LABELS) {
      expect(
        screen.queryByText(label, { exact: true }),
        `"${label}" badge should be gone`,
      ).toBeNull();
    }
  });

  it.each(routes)(
    'route "%s" prints no badge INSIDE a drawer that has real sections either',
    (route) => {
      // The `it.each` above renders a bare route, and a bare route has no
      // proof sections — so the drawer returns null and those cases cannot
      // see a badge that came back inside it. This one gives the drawer
      // something to show, which is the only state in which it renders at all.
      renderAnswer({ route, sql: 'SELECT 1', data: [{ a: 1 }] });
      expect(screen.getByRole('button', { name: /View SQL/ })).toBeTruthy();
      for (const label of BADGE_LABELS) {
        expect(
          screen.queryByText(label, { exact: true }),
          `"${label}" badge should be gone from the proof bar`,
        ).toBeNull();
      }
    },
  );

  it('BADGE-04 · the answer itself is untouched', () => {
    renderAnswer({ route: 'vision' });
    expect(screen.getByText('Here is the answer.')).toBeTruthy();
  });

  it('BADGE-05 · meta.route still rides on the message', () => {
    // The label went; the metadata that history, regenerate and the request
    // path read did not.
    const message = answer({ route: 'vision', mode: 'assistant' });
    renderAnswer(message.meta as Meta);
    expect(message.meta?.route).toBe('vision');
    expect(message.meta?.mode).toBe('assistant');
  });

  it('a plain answer renders no empty bar where the badge used to be', () => {
    // The drawer already returned null with no sections; removing the badge
    // must not have left a bordered box holding nothing.
    const { container } = render(<ProofDrawer meta={{ route: 'chat' }} />);
    expect(container.innerHTML).toBe('');
  });

  it('the proof drawer still opens its real sections, without the badge', () => {
    render(
      <ProofDrawer
        meta={{ route: 'rag', sql: 'SELECT 1', data: [{ a: 1 }] }}
      />,
    );
    expect(screen.getByRole('button', { name: /View SQL/ })).toBeTruthy();
    expect(screen.getByRole('button', { name: /Data \(1\)/ })).toBeTruthy();
    expect(screen.queryByText('Records', { exact: true })).toBeNull();
  });
});

describe('BADGE-06 · the selectable controls are all still there', () => {
  it('the composer menu still offers every source and research mode', () => {
    const items = composerMenuItems({
      salesforce: DEFAULT_PREFS.salesforce,
      sfLive: DEFAULT_PREFS.sfLive,
      webSearchOn: DEFAULT_PREFS.webSearch === 'on',
      deepResearchOn: DEFAULT_PREFS.deepResearch,
      streaming: false,
    });
    expect(items.map((i) => i.label)).toEqual([
      'Add photos & files',
      'Web search',
      'Deep research',
      'Salesforce',
      'Live Salesforce',
    ]);
    // …and none of them is armed on a new chat.
    for (const item of items.filter((i) => i.checked !== undefined)) {
      expect(item.checked, `${item.label} should start off`).toBe(false);
    }
  });
});
