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

export type ComposerMenuItemId =
  | 'files'
  | 'web-search'
  | 'deep-research'
  | 'salesforce'
  | 'sf-live';

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
  /** Live Salesforce sub-toggle (only shown while salesforce is on). */
  sfLive: boolean;
  /** True when the user forced search on (prefs.webSearch === 'on'). */
  webSearchOn: boolean;
  /** True when the next send will run the iterative research loop. */
  deepResearchOn: boolean;
  streaming: boolean;
}

/**
 * Menu items in display order. "Add photos & files" comes first because it is
 * the only item that opens another surface (the OS picker); the source
 * toggles follow. Web search here is the FORCE toggle — "auto" (the level
 * decides) is the unlabelled default, exactly like ChatGPT's menu where an
 * unchecked "Web search" still allows the model to search on its own.
 *
 * ALL FOUR rows are always visible (owner request 2026-08-06, superseding
 * the 2026-08-05 hide-what-cannot-work rule): the owner wants the menu to
 * read as the complete capability list — upload · web · Salesforce · live —
 * with one click switching mode. Activating a row whose mode conflicts with
 * the current one performs the switch instead of lying: Web search while
 * Salesforce is on turns Salesforce OFF; Live while Salesforce is off turns
 * Salesforce ON. Each hint says so before the click.
 */
export function composerMenuItems(
  state: ComposerMenuState,
): ComposerMenuItem[] {
  return [
    {
      id: 'files',
      label: 'Add photos & files',
      hint: 'Images, PDFs and datasets from this computer',
      disabled: state.streaming,
    },
    {
      id: 'web-search',
      label: 'Web search',
      hint: state.salesforce
        ? // Warn BEFORE the click: activating this abandons Salesforce mode.
          'Force a web search for the next answers · turns Salesforce off'
        : state.webSearchOn
          ? // Not "every answer": the server skips search on image/PDF turns.
            'Text answers always search the web'
          : 'Force a web search for the next answers',
      checked: state.webSearchOn && !state.salesforce,
    },
    {
      id: 'deep-research',
      label: 'Deep research',
      hint: state.salesforce
        ? // Same warning-before-the-click rule as Web search above.
          'Research the web and write a cited report · turns Salesforce off'
        : state.deepResearchOn
          ? 'The next answer is a researched, cited report — this takes minutes'
          : 'Plan, search, read many sources, then write a cited report',
      checked: state.deepResearchOn && !state.salesforce,
    },
    {
      id: 'salesforce',
      label: 'Salesforce',
      hint: 'Answer from your synced Salesforce data · turns web search off',
      checked: state.salesforce,
    },
    {
      id: 'sf-live',
      label: 'Live Salesforce',
      hint: state.salesforce
        ? state.sfLive
          ? 'Every answer queries Salesforce directly — freshest data, any object'
          : 'Query Salesforce directly instead of the synced copy'
        : // One click from any mode: this both enters Salesforce mode and
          // goes live — the hint promises exactly that.
          'Query your Salesforce org directly · turns Salesforce on',
      checked: state.salesforce && state.sfLive,
    },
  ];
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
    case 'deep-research': {
      const next = !prefs.deepResearch;
      return {
        kind: 'prefs',
        prefs: {
          ...prefs,
          deepResearch: next,
          // Research IS web work: leaving Salesforce on would make the server
          // refuse the web and answer from the warehouse instead, which is
          // not what the row promised.
          salesforce: next ? false : prefs.salesforce,
          sfLive: next ? false : prefs.sfLive,
        },
      };
    }
    case 'web-search':
      if (prefs.salesforce) {
        // Mode switch, as the row's hint promised: leave Salesforce (and its
        // live sub-toggle) and force the web on — one click, no dead ends.
        return {
          kind: 'prefs',
          prefs: { ...prefs, salesforce: false, sfLive: false, webSearch: 'on' },
        };
      }
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
          // Salesforce mode never touches the web, so a lingering Deep
          // Research would be invisible AND un-runnable — the same trap the
          // forced-web reset below exists for.
          deepResearch: next ? false : prefs.deepResearch,
          // Live Salesforce cannot outlive its parent toggle: a lingering
          // true would silently go live the next time Salesforce turns on
          // (the same trap sanitize() guards).
          sfLive: next ? prefs.sfLive : false,
          webSearch: next && prefs.webSearch === 'on' ? 'auto' : prefs.webSearch,
        },
      };
    }
    case 'sf-live':
      if (!prefs.salesforce) {
        // One click from any mode: enter Salesforce mode AND go live. The
        // forced web search drops with it — Salesforce mode never searches.
        return {
          kind: 'prefs',
          prefs: {
            ...prefs,
            salesforce: true,
            sfLive: true,
            webSearch: prefs.webSearch === 'on' ? 'auto' : prefs.webSearch,
          },
        };
      }
      return {
        kind: 'prefs',
        prefs: { ...prefs, sfLive: !prefs.sfLive },
      };
    case 'files':
      return { kind: 'pick-files' };
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
  if (prefs.salesforce && prefs.sfLive) {
    // Live queries DO leave this machine — they go to the user's own
    // Salesforce org over the read-only API. Say so; "nothing leaves this
    // machine" would be a lie with this toggle on.
    return 'Answers come straight from your live Salesforce org (read-only) · no web search.';
  }
  if (prefs.salesforce) {
    return 'Answers come from your synced Salesforce data · no web search · nothing leaves this machine.';
  }
  return prefs.webSearch === 'on'
    ? 'Salesforce is off and web search is on — search queries are sent to the internet.'
    : 'Salesforce is off — answers may use the web, and search queries are sent to the internet.';
}
