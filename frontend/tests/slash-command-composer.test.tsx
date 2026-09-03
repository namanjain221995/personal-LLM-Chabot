// @vitest-environment jsdom
/**
 * Slash commands in the composer (2026-09-03): "/" shows the picker, typing
 * narrows it, Enter completes the command, and the send that follows runs
 * under the command's prefs with the command word stripped from the text.
 */
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { Composer } from '@/components/Composer';
import { DEFAULT_PREFS } from '@/lib/prefs';

afterEach(cleanup);

function mount() {
  const onSend = vi.fn();
  render(
    <Composer
      streaming={false}
      prefs={{ ...DEFAULT_PREFS, salesforce: true }}
      onPrefsChange={vi.fn()}
      onSend={onSend}
      onStop={vi.fn()}
    />,
  );
  const box = screen.getByLabelText('Message') as HTMLTextAreaElement;
  return { box, onSend };
}

describe('slash commands', () => {
  it('shows the picker for "/" and narrows as the user types', () => {
    const { box } = mount();
    act(() => {
      fireEvent.change(box, { target: { value: '/' } });
    });
    const rows = screen.getAllByRole('option');
    expect(rows.map((r) => r.textContent)).toEqual(
      expect.arrayContaining([expect.stringContaining('/deep-research')]),
    );
    act(() => {
      fireEvent.change(box, { target: { value: '/de' } });
    });
    expect(screen.getAllByRole('option')).toHaveLength(1);
    expect(screen.getByRole('option').textContent).toContain('/deep-research');
  });

  it('Enter completes the highlighted command instead of sending "/de"', () => {
    const { box, onSend } = mount();
    act(() => {
      fireEvent.change(box, { target: { value: '/de' } });
    });
    act(() => {
      fireEvent.keyDown(box, { key: 'Enter' });
    });
    expect(box.value).toBe('/deep-research ');
    expect(onSend).not.toHaveBeenCalled();
    expect(screen.queryByRole('listbox')).toBeNull();
  });

  it('sends the question without the command, under research prefs, Salesforce off', () => {
    const { box, onSend } = mount();
    act(() => {
      fireEvent.change(box, { target: { value: '/deep-research who leads Acme now?' } });
    });
    act(() => {
      fireEvent.keyDown(box, { key: 'Enter' });
    });
    expect(onSend).toHaveBeenCalledTimes(1);
    const [text, attachments, options] = onSend.mock.calls[0];
    expect(text).toBe('who leads Acme now?');
    expect(attachments).toEqual([]);
    expect(options?.prefs?.deepResearch).toBe(true);
    expect(options?.prefs?.salesforce).toBe(false);
    expect(box.value).toBe('');
  });

  it('refuses a command with nothing after it and keeps the draft', () => {
    const { box, onSend } = mount();
    act(() => {
      fireEvent.change(box, { target: { value: '/deep-research' } });
    });
    // The picker is showing (exact name still matches) — Enter completes
    // first; a second Enter on the completed command with no question refuses.
    act(() => {
      fireEvent.keyDown(box, { key: 'Enter' });
    });
    expect(box.value).toBe('/deep-research ');
    act(() => {
      fireEvent.change(box, { target: { value: '/deep-research ' } });
    });
    act(() => {
      fireEvent.keyDown(box, { key: 'Enter' });
    });
    expect(onSend).not.toHaveBeenCalled();
    expect(box.value).toBe('/deep-research ');
  });

  it('ordinary messages are untouched', () => {
    const { box, onSend } = mount();
    act(() => {
      fireEvent.change(box, { target: { value: 'hello there' } });
    });
    act(() => {
      fireEvent.keyDown(box, { key: 'Enter' });
    });
    expect(onSend).toHaveBeenCalledWith('hello there', [], undefined);
  });
});
