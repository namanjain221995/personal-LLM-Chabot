/**
 * Focus containment for the app's two modal surfaces — the search palette and
 * the mobile sidebar drawer.
 *
 * Both need the same three things: the list of elements Tab may land on, the
 * wrap arithmetic, and the "focus is currently outside the container" case.
 * They lived only inside SearchPalette.tsx, so the drawer (L-03) would have
 * had to grow a second copy — and two focus traps that disagree is exactly the
 * bug this module exists to prevent. The wrap itself is still
 * `trapFocusIndex`, already unit-tested next to the rest of the palette's
 * keyboard model; this only adds the DOM half around it.
 */

import { trapFocusIndex } from './searchPalette';

/**
 * Elements Tab may land on inside a trapped container.
 *
 * `[tabindex="-1"]` is excluded deliberately: a container made programmatically
 * focusable so it can receive INITIAL focus is not a tab stop, and neither is
 * the drawer's full-screen backdrop.
 */
export const FOCUSABLE_SELECTOR =
  'a[href],button:not([disabled]),input:not([disabled]),textarea:not([disabled]),select:not([disabled]),[tabindex]:not([tabindex="-1"])';

/**
 * Is this element actually reachable, or only present?
 *
 * A trap that hands focus to something the CSS has hidden simply stalls —
 * `focus()` on a `display:none` element is a no-op, so Tab appears to do
 * nothing. Both hidden kinds occur inside the sidebar panel: the Archived list
 * carries the `hidden` ATTRIBUTE while collapsed, and the panel keeps both the
 * desktop "Hide sidebar" and the mobile "Close sidebar" buttons in the markup
 * with Tailwind's `hidden`/`md:block` deciding which one the viewport shows.
 *
 * `checkVisibility()` catches both in a real browser. Where it does not exist
 * the attribute check still holds the line, and jsdom — which has no layout to
 * consult — sees every control, which is what keeps the trap's tests about the
 * trap rather than about emulated CSS.
 */
function isTabbable(el: HTMLElement): boolean {
  if (el.closest('[hidden]')) return false;
  return typeof el.checkVisibility === 'function' ? el.checkVisibility() : true;
}

/** Tab stops inside `root`, in document order. */
export function focusableWithin(root: HTMLElement | null): HTMLElement[] {
  if (!root) return [];
  return Array.from(
    root.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR),
  ).filter(isTabbable);
}

/**
 * Where Tab should move next, given the currently focused element.
 *
 * When focus is not among `nodes` — the container itself holds it right after
 * opening, or focus escaped to <body> — Tab enters at the first stop and
 * Shift+Tab at the last, which is what makes the very first keystroke after
 * the drawer opens behave. Returns undefined only when there is nothing
 * focusable at all, so the caller leaves the event alone rather than trapping
 * the user in an empty container.
 */
export function focusTrapNext(
  nodes: HTMLElement[],
  active: Element | null,
  backwards: boolean,
): HTMLElement | undefined {
  if (nodes.length === 0) return undefined;
  const current = nodes.indexOf(active as HTMLElement);
  if (current === -1) return backwards ? nodes[nodes.length - 1] : nodes[0];
  return nodes[trapFocusIndex(current, nodes.length, backwards)];
}
