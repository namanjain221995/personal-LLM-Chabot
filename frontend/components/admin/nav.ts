/**
 * The admin area's one navigation side effect, wrapped so tests can see it.
 *
 * The auth contract's 401 rule is a HARD redirect — window.location.assign,
 * not a router push — so the whole document reloads signed out. jsdom makes
 * window.location.assign non-configurable, which would leave that rule
 * untestable if components called it directly; they call `nav.assign`
 * instead and tests spy on this object.
 */
export const nav = {
  assign(url: string): void {
    window.location.assign(url);
  },
};
