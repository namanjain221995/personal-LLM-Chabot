// @vitest-environment jsdom
/**
 * QA round 2: the REAL ArtifactPanel open beside the chat (desktop), then a
 * confirm opened inside a memory/settings dialog. An Escape meant for that
 * confirm must not also close the artifact panel two layers behind it.
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ArtifactPanel } from '@/components/artifacts/ArtifactPanel';
import { MemoryDialog } from '@/components/MemoryPanel';
import { Providers } from '@/components/Providers';
import { SettingsDialog } from '@/components/SettingsDialog';
import type { FetchLike } from '@/lib/auth';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});
beforeEach(() => {
  HTMLMediaElement.prototype.play =
    HTMLMediaElement.prototype.play ?? (async () => undefined);
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: /\(max-width:\s*(\d+)px\)/.test(query) ? 1440 <= Number(/(\d+)px/.exec(query)![1]) : false,
    media: query,
    onchange: null,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    addListener: () => undefined,
    removeListener: () => undefined,
    dispatchEvent: () => false,
  }));
});

const ID = 'a3f9c2d1e4b5f6a7b8c9d0e1f2a3b4c5';
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

function artifactBehind(onArtifactClose: () => void) {
  vi.stubGlobal('fetch', vi.fn(async () => json({ detail: 'x' }, 404)));
  return <ArtifactPanel artifactId={ID} version={1} originId={null} onClose={onArtifactClose} />;
}

describe('QA2 (real ArtifactPanel): Escape answered by a nested confirm', () => {
  it('Settings > Sessions sign-out confirm, focus on Cancel: only the confirm closes', async () => {
    const onArtifactClose = vi.fn();
    const onSettingsClose = vi.fn();
    const fn = vi.fn(async (input: RequestInfo | URL) =>
      String(input) === '/api/auth/sessions'
        ? json({
            sessions: [
              { id: 's1', current: true, created_at: '2026-09-18T09:00:00Z', last_seen_at: '2026-09-18T09:00:00Z', user_agent: null },
              { id: 's2', current: false, created_at: '2026-09-17T09:00:00Z', last_seen_at: '2026-09-17T09:00:00Z', user_agent: null },
            ],
          })
        : json({}, 599),
    );
    render(
      <Providers>
        {artifactBehind(onArtifactClose)}
        <SettingsDialog open initialSection="sessions" account={null} onClose={onSettingsClose} fetchFn={fn as unknown as FetchLike} />
      </Providers>,
    );
    fireEvent.click(await screen.findByRole('button', { name: 'Sign out' }));
    const confirm = await screen.findByRole('alertdialog');
    const cancel = within(confirm).getByRole('button', { name: 'Cancel' });
    expect(document.activeElement).toBe(cancel);
    fireEvent.keyDown(cancel, { key: 'Escape' });
    expect(screen.queryByRole('alertdialog')).toBeNull();
    expect(onSettingsClose).not.toHaveBeenCalled();
    expect(onArtifactClose).not.toHaveBeenCalled();
  });

  it('chip dialog row-delete confirm, after a click on its text: only the confirm closes', async () => {
    const onArtifactClose = vi.fn();
    const facts = [{ id: 11, fact: 'Works in Pune', source: 'stated', source_excerpt: null, created_at: null, updated_at: null }];
    const fn = vi.fn(async () => json({ facts }));
    render(
      <Providers>
        {artifactBehind(onArtifactClose)}
        <MemoryDialog open onClose={vi.fn()} fetchFn={fn as unknown as FetchLike} />
      </Providers>,
    );
    await waitFor(() => expect(screen.getAllByTestId('memory-fact')).toHaveLength(1));
    fireEvent.click(screen.getByRole('button', { name: /^Delete “Works/ }));
    await screen.findByRole('alertdialog');
    act(() => (document.activeElement as HTMLElement | null)?.blur());
    expect(document.activeElement).toBe(document.body);
    fireEvent.keyDown(document.body, { key: 'Escape' });
    expect(screen.queryByRole('alertdialog')).toBeNull();
    expect(screen.getByRole('dialog', { name: 'Memory' })).toBeTruthy();
    expect(onArtifactClose).not.toHaveBeenCalled();
  });
});
