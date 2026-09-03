import { describe, expect, it } from 'vitest';
import {
  applySlashCommand,
  completeCommand,
  isPickingCommand,
  matchingCommands,
  parseSlashCommand,
  SLASH_COMMANDS,
} from '../lib/slashCommands';
import { DEFAULT_PREFS, type ChatPrefs } from '../lib/prefs';

function prefs(over: Partial<ChatPrefs> = {}): ChatPrefs {
  return { ...DEFAULT_PREFS, ...over };
}

describe('slash command picker', () => {
  it('lists everything on a bare slash and narrows as the user types', () => {
    expect(matchingCommands('/').map((c) => c.id)).toEqual(
      SLASH_COMMANDS.map((c) => c.id),
    );
    expect(matchingCommands('/de').map((c) => c.id)).toEqual(['deep-research']);
    expect(matchingCommands('/re').map((c) => c.id)).toEqual(['deep-research']); // alias "research"
    expect(matchingCommands('/s').map((c) => c.id)).toEqual(['search', 'crawl']); // "search", alias "site"
    expect(matchingCommands('/zzz')).toEqual([]);
  });

  it('stops picking once the argument starts', () => {
    expect(isPickingCommand('/deep')).toBe(true);
    expect(isPickingCommand('/deep-research ')).toBe(false);
    expect(isPickingCommand('/deep-research what is new')).toBe(false);
    expect(isPickingCommand('hello /deep')).toBe(false);
    expect(isPickingCommand('')).toBe(false);
  });

  it('completes to the canonical name with a trailing space', () => {
    expect(completeCommand(SLASH_COMMANDS[0])).toBe('/deep-research ');
  });
});

describe('parsing', () => {
  it('recognises names and aliases, and nothing that merely starts with them', () => {
    expect(parseSlashCommand('/deep-research who leads Acme?')?.command.id).toBe('deep-research');
    expect(parseSlashCommand('/research who leads Acme?')?.rest).toBe('who leads Acme?');
    expect(parseSlashCommand('/DEEP who')?.command.id).toBe('deep-research');
    expect(parseSlashCommand('/deepest thoughts')).toBeNull();
    expect(parseSlashCommand('/unknown x')).toBeNull();
    expect(parseSlashCommand('deep-research x')).toBeNull();
    // Multi-line arguments survive.
    expect(parseSlashCommand('/search first line\nsecond line')?.rest).toBe('first line\nsecond line');
  });
});

describe('applying a command to a send', () => {
  it('/deep-research arms research for THIS send and turns Salesforce off', () => {
    const out = applySlashCommand(parseSlashCommand('/deep-research compare A and B')!, prefs({ salesforce: true }));
    expect(out.error).toBeUndefined();
    expect(out.text).toBe('compare A and B');
    expect(out.prefs.deepResearch).toBe(true);
    expect(out.prefs.salesforce).toBe(false);
  });

  it('/search forces the web on for the send', () => {
    const out = applySlashCommand(parseSlashCommand('/search latest release notes')!, prefs({ salesforce: true }));
    expect(out.text).toBe('latest release notes');
    expect(out.prefs.webSearch).toBe('on');
    expect(out.prefs.salesforce).toBe(false);
  });

  it('/crawl becomes the wording the server crawler detects, and needs a URL', () => {
    const ok = applySlashCommand(parseSlashCommand('/crawl https://docs.example.com/en/')!, prefs());
    expect(ok.text).toBe('crawl https://docs.example.com/en/');
    expect(ok.error).toBeUndefined();
    const bad = applySlashCommand(parseSlashCommand('/crawl the docs')!, prefs());
    expect(bad.error).toMatch(/full URL/);
    expect(bad.text).toBe('');
  });

  it('refuses a command with no argument, saying what is missing', () => {
    const out = applySlashCommand(parseSlashCommand('/deep-research')!, prefs());
    expect(out.error).toMatch(/question after \/deep-research/);
    expect(out.text).toBe('');
  });
});
