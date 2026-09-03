// @vitest-environment jsdom
/**
 * Per-member tool access in the client (2026-09-03).
 *
 * The server is the gate — these tests pin what the CLIENT owes the person
 * looking at it: a menu that lists only what they can use, sticky prefs that
 * cannot leave them in a mode the server will refuse (and a trust footer
 * that would then be lying), and an access dialog that stores only what
 * differs from the workspace default, so a later default change still
 * reaches everyone who was never set individually.
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { Composer } from '@/components/Composer';
import { DEFAULT_PREFS } from '@/lib/prefs';
import {
  composerMenuItems,
  featureOn,
  prefsForFeatures,
} from '@/lib/composerMenu';
import { applyFeatureRules, featureMap, parseMe, type FeatureSpec } from '@/components/admin/api';

afterEach(cleanup);

const ALL_ON = {
  attachments: true,
  web_search: true,
  deep_research: true,
  salesforce: true,
  salesforce_live: true,
};

const MENU_STATE = {
  salesforce: false,
  sfLive: false,
  webSearchOn: false,
  deepResearchOn: false,
  streaming: false,
};

describe('composer menu under feature access', () => {
  it('lists every tool when access is unrestricted', () => {
    const ids = composerMenuItems({ ...MENU_STATE, features: ALL_ON }).map(
      (i) => i.id,
    );
    expect(ids).toEqual([
      'files',
      'web-search',
      'deep-research',
      'salesforce',
      'sf-live',
    ]);
  });

  it('removes the rows this account may not use', () => {
    const ids = composerMenuItems({
      ...MENU_STATE,
      features: { ...ALL_ON, salesforce: false, salesforce_live: false },
    }).map((i) => i.id);
    expect(ids).toEqual(['files', 'web-search', 'deep-research']);
  });

  it('treats an unknown feature as allowed', () => {
    // A backend that predates feature access sends nothing; hiding tools it
    // still honours would be the worse failure.
    expect(featureOn(undefined, 'web_search')).toBe(true);
    expect(featureOn({}, 'salesforce')).toBe(true);
    expect(composerMenuItems({ ...MENU_STATE })).toHaveLength(5);
  });
});

describe('sticky prefs are corrected to what the account may use', () => {
  it('leaves a member out of a mode the server would refuse', () => {
    const stale = {
      ...DEFAULT_PREFS,
      salesforce: true,
      sfLive: true,
      webSearch: 'on' as const,
      deepResearch: true,
    };
    const fixed = prefsForFeatures(stale, {
      ...ALL_ON,
      salesforce: false,
      salesforce_live: false,
      deep_research: false,
    });
    expect(fixed.salesforce).toBe(false);
    expect(fixed.sfLive).toBe(false);
    expect(fixed.deepResearch).toBe(false);
    expect(fixed.webSearch).toBe('on'); // web search was still granted
  });

  it('returns the same object when nothing needs correcting', () => {
    const prefs = { ...DEFAULT_PREFS };
    expect(prefsForFeatures(prefs, ALL_ON)).toBe(prefs);
  });
});

describe('the composer menu rendered', () => {
  it('offers only the granted tools', async () => {
    render(
      <Composer
        streaming={false}
        prefs={DEFAULT_PREFS}
        features={{ ...ALL_ON, deep_research: false, salesforce: false, salesforce_live: false }}
        onPrefsChange={vi.fn()}
        onSend={vi.fn()}
        onStop={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByLabelText(/add photos|attach|more/i));
    await waitFor(() => screen.getByText('Web search'));
    expect(screen.queryByText('Deep research')).toBeNull();
    expect(screen.queryByText('Salesforce')).toBeNull();
    expect(screen.queryByText('Live Salesforce')).toBeNull();
    expect(screen.getByText('Add photos & files')).toBeTruthy();
  });
});

describe('the admin access model', () => {
  const catalog: FeatureSpec[] = [
    { id: 'web_search', label: 'Web search', hint: '', default: true, requires: null },
    {
      id: 'deep_research',
      label: 'Deep research',
      hint: '',
      default: true,
      requires: 'web_search',
    },
    { id: 'salesforce', label: 'Salesforce', hint: '', default: true, requires: null },
    {
      id: 'salesforce_live',
      label: 'Live Salesforce',
      hint: '',
      default: true,
      requires: 'salesforce',
    },
  ];

  it('drops descendants when a parent is switched off', () => {
    const next = applyFeatureRules(
      catalog,
      { web_search: false, deep_research: true, salesforce: true, salesforce_live: true },
      'web_search',
    );
    expect(next.deep_research).toBe(false);
    expect(next.salesforce_live).toBe(true); // a different branch is untouched
  });

  it('turns a parent on when a child is switched on', () => {
    const next = applyFeatureRules(
      catalog,
      { web_search: false, deep_research: true, salesforce: false, salesforce_live: false },
      'deep_research',
    );
    expect(next.web_search).toBe(true);
  });

  it('parses the features map out of ME_PAYLOAD, ignoring junk', () => {
    expect(featureMap({ web_search: true, nope: 'yes', salesforce: false })).toEqual({
      web_search: true,
      salesforce: false,
    });
    const me = parseMe({
      user: { id: 1, name: 'Ada', email: 'a@x.test' },
      workspace: { id: 'w', name: 'W', role: 'member' },
      capabilities: [],
      features: { salesforce: false },
    });
    expect(me?.features).toEqual({ salesforce: false });
  });
});
