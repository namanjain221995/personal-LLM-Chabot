/**
 * Headless model for the composer's ChatGPT-style "+" menu (owner request
 * 2026-08-05): one button that opens a popover offering "Add photos & files",
 * "Web search" and "Salesforce" instead of a bare paperclip.
 *
 * Like `conversationMenu.ts`, every decision — which items exist, what
 * activating one does to the prefs, and what the trust footer must say for a
 * given prefs combination — lives here as pure functions so it is
 * unit-testable in node. `components/AttachMenu.tsx` is the rendering shell.
 */

import type { ChatPrefs } from './prefs';

export type ComposerMenuItemId = 'files' | 'web-search' | 'salesforce';

export interface ComposerMenuItem {
  id: ComposerMenuItemId;
  label: string;
  /** One-line explanation under the label, ChatGPT-style. */
  hint: string;
  /** Present only on toggle rows; actions like "files" have no state. */
  checked?: boolean;
  /** File pickers make no sense mid-stream; toggles stay usable. */
  disabled?: boolean;
}

export interface ComposerMenuState {
  salesforce: boolean;
  /** True when the user forced search on (prefs.webSearch === 'on'). */
  webSearchOn: boolean;
  streaming: boolean;
}

/**
 * Menu items in display order. "Add photos & files" comes first because it is
 * the only item that opens another surface (the OS picker); the source
 * toggles follow. Web search here is the FORCE toggle — "auto" (the level
 * decides) is the unlabelled default, exactly like ChatGPT's menu where an
 * unchecked "Web search" still allows the model to search on its own.
 *
 * With Salesforce ON the web-search item is HIDDEN, not just unchecked
 * (owner request 2026-08-05): Salesforce mode never touches the web at any
 * effort level — the server refuses even an explicit "on" — so showing a
 * toggle that cannot work would be a lie in a menu.
 */
export function composerMenuItems(
  state: ComposerMenuState,
): ComposerMenuItem[] {
  const items: ComposerMenuItem[] = [
    {
      id: 'files',
      label: 'Add photos & files',
      hint: 'Images, PDFs and datasets from this computer',
      disabled: state.streaming,
    },
  ];
  if (!state.salesforce) {
    items.push({
      id: 'web-search',
      label: 'Web search',
      hint: state.webSearchOn
        ? // Not "every answer": the server skips search on image/PDF turns.
          'Text answers always search the web'
        : 'Force a web search for the next answers',
      checked: state.webSearchOn,
    });
  }
  items.push({
    id: 'salesforce',
    label: 'Salesforce',
    hint: 'Answer from your synced Salesforce data · turns web search off',
    checked: state.salesforce,
  });
  return items;
}

export type ComposerMenuOutcome =
  /** Close the popover and open the OS file picker. */
  | { kind: 'pick-files' }
  /** Close the popover and apply the returned prefs. */
  | { kind: 'prefs'; prefs: ChatPrefs };

/**
 * What activating an item does. Toggling web search flips 'on' ↔ 'auto' —
 * never 'off', because "off" had no composer control since 2026-07-28 and
 * sanitize() would migrate it back to 'auto' anyway.
 *
 * Turning Salesforce ON also drops a forced web search back to 'auto'
 * (owner request 2026-08-05): Salesforce mode never searches the web, so a
 * lingering hidden "on" would silently spring back the moment Salesforce is
 * turned off again. EVERY UI path that flips Salesforce must go through
 * here — the Composer pill included — or the reset is skippable.
 */
export function activateComposerMenuItem(
  id: ComposerMenuItemId,
  prefs: ChatPrefs,
): ComposerMenuOutcome {
  switch (id) {
    case 'files':
      return { kind: 'pick-files' };
    case 'web-search':
      return {
        kind: 'prefs',
        prefs: {
          ...prefs,
          webSearch: prefs.webSearch === 'on' ? 'auto' : 'on',
        },
      };
    case 'salesforce': {
      const next = !prefs.salesforce;
      return {
        kind: 'prefs',
        prefs: {
          ...prefs,
          salesforce: next,
          webSearch: next && prefs.webSearch === 'on' ? 'auto' : prefs.webSearch,
        },
      };
    }
  }
}

/**
 * Roving focus for the popover, with disabled rows skipped: focus() on a
 * disabled <button> is a silent NO-OP, so landing on one (the files row
 * while streaming) would make ArrowUp at the top and wrap-around ArrowDown
 * look broken. From menuKeyAction's target index, walk onward in the
 * direction of travel until a usable row; if every row is disabled, stay.
 */
export function nextEnabledIndex(
  items: ReadonlyArray<Pick<ComposerMenuItem, 'disabled'>>,
  target: number,
  forward: boolean,
): number {
  let i = target;
  for (let step = 0; step < items.length; step++) {
    if (!items[i]?.disabled) return i;
    i = (i + (forward ? 1 : -1) + items.length) % items.length;
  }
  return target;
}

/**
 * The trust footer under the composer. This line is the ONLY place the
 * privacy promise is made. Salesforce ON is unconditional — the server
 * refuses web search in that mode at every effort level, even an explicit
 * web_search=="on", so "nothing leaves this machine" always holds there.
 */
export function trustLine(prefs: ChatPrefs): string {
  if (prefs.salesforce) {
    return 'Answers come from your synced Salesforce data · no web search · nothing leaves this machine.';
  }
  return prefs.webSearch === 'on'
    ? 'Salesforce is off and web search is on — search queries are sent to the internet.'
    : 'Salesforce is off — answers may use the web, and search queries are sent to the internet.';
}
