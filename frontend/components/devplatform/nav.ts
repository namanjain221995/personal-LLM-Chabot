/**
 * The console's navigation, as data.
 *
 * CONTRACT §17 names the sections and this file is the one place they are
 * listed, for the same reason `navGroups(me)` is the one place the admin rail
 * is listed: a nav spread across a rail component, a mobile header and a tab
 * dispatcher drifts into three navs that disagree about which sections exist.
 *
 * VISIBILITY IS NOT SECURITY. Every entry here is drawn from
 * ME_PAYLOAD.capabilities so an admin is not shown a Limits tab that will
 * refuse them — but the refusal itself is the orchestrator's, on every call,
 * and the page behind each entry is guarded server-side before any of this
 * runs. Hiding a link is a courtesy to the reader, never a control.
 *
 * Pure, and separate from the components, so the capability table can be
 * asserted without a DOM.
 */

import type { Me } from '@/components/admin/api';

/** The query-string value each section lives at. Overview is the bare page. */
export type TabId =
  | 'overview'
  | 'projects'
  | 'keys'
  | 'models'
  | 'playground'
  | 'usage'
  | 'logs'
  | 'webhooks'
  | 'limits'
  | 'settings';

export interface ConsoleNavItem {
  id: TabId | 'docs';
  label: string;
  /** Where the link goes. Documentation leaves the console for /docs. */
  href: string;
  /** Null when every console user sees it; otherwise the rbac.Cap string. */
  capability: string | null;
  /** True for a link that leaves the console (Documentation, Admin, Chat). */
  external?: boolean;
}

export interface ConsoleNavGroup {
  title?: string;
  items: ConsoleNavItem[];
}

/** The href for a section — Overview is `/api` itself, not `/api?tab=overview`. */
export function tabHref(id: TabId): string {
  return id === 'overview' ? '/api' : `/api?tab=${id}`;
}

const ALL_TABS: TabId[] = [
  'overview',
  'projects',
  'keys',
  'models',
  'playground',
  'usage',
  'logs',
  'webhooks',
  'limits',
  'settings',
];

/**
 * Read the section out of `?tab=`, refusing anything not on the list.
 *
 * An unknown value falls back to Overview rather than rendering nothing: a
 * pasted or truncated URL should land somewhere useful, and a blank console is
 * indistinguishable from a broken one.
 */
export function tabFromQuery(raw: string | null | undefined): TabId {
  const value = (raw ?? '').trim();
  return (ALL_TABS as string[]).includes(value) ? (value as TabId) : 'overview';
}

function can(me: Me, capability: string | null): boolean {
  return capability === null || me.capabilities.includes(capability);
}

/**
 * The sections this person may see, grouped the way the rail draws them.
 *
 * The capability each entry carries is the one the orchestrator will check on
 * the first call the section makes — Usage needs `api.usage.read`, Request
 * logs `api.logs.read`, Webhooks `api.webhooks.manage`, and Limits
 * `api.limits.manage`, which CONTRACT §6 gives to a super admin ALONE: an
 * admin may run projects, not lift a workspace's ceiling.
 */
export function consoleNav(me: Me): ConsoleNavGroup[] {
  const groups: ConsoleNavGroup[] = [
    {
      items: [
        { id: 'overview', label: 'Overview', href: tabHref('overview'), capability: null },
      ],
    },
    {
      title: 'Build',
      items: [
        { id: 'projects', label: 'Projects', href: tabHref('projects'), capability: 'api.projects.read' },
        { id: 'keys', label: 'API keys', href: tabHref('keys'), capability: 'api.projects.read' },
        { id: 'models', label: 'Models', href: tabHref('models'), capability: null },
        { id: 'playground', label: 'Playground', href: tabHref('playground'), capability: null },
      ],
    },
    {
      title: 'Operate',
      items: [
        { id: 'usage', label: 'Usage', href: tabHref('usage'), capability: 'api.usage.read' },
        { id: 'logs', label: 'Request logs', href: tabHref('logs'), capability: 'api.logs.read' },
        { id: 'webhooks', label: 'Webhooks', href: tabHref('webhooks'), capability: 'api.webhooks.manage' },
        { id: 'limits', label: 'Limits', href: tabHref('limits'), capability: 'api.limits.manage' },
      ],
    },
    {
      title: 'Reference',
      items: [
        // Documentation is a PAGE, not a section of this one: /docs is its own
        // route with its own owner (CONTRACT §17), and every signed-in person
        // may read it, console access or not.
        { id: 'docs', label: 'Documentation', href: '/docs', capability: null, external: true },
        { id: 'settings', label: 'Settings', href: tabHref('settings'), capability: null },
      ],
    },
  ];
  return groups
    .map((group) => ({
      ...group,
      items: group.items.filter((item) => can(me, item.capability)),
    }))
    .filter((group) => group.items.length > 0);
}

/**
 * Whether a section may be RENDERED, independent of whether its link is drawn.
 *
 * Someone who bookmarked `/api?tab=limits` as a super admin and was later
 * demoted still has that URL. Without this the demoted admin would land on a
 * Limits panel that renders its furniture and then collapses into an error
 * when every call 404s; with it they land on Overview, which is true.
 */
export function tabAllowed(me: Me, tab: TabId): boolean {
  for (const group of consoleNav(me)) {
    for (const item of group.items) {
      if (item.id === tab) return true;
    }
  }
  return false;
}
