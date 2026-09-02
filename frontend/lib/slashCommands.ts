/**
 * Slash commands in the composer (owner request 2026-09-03: "make
 * /deep-research"), ChatGPT-style: type "/" and a short list appears; pick
 * one and the command becomes the mode for THAT send.
 *
 * Pure functions, unit-tested in node; `components/Composer.tsx` renders
 * the list and calls these. A command never changes what the server
 * accepts — each one maps onto a pref the "+" menu already exposes (Deep
 * research, forced Web search) or onto phrasing the server already routes
 * ("crawl <url>" is the whole-site crawler's trigger), so nothing here is a
 * second code path the backend has to know about.
 */

import type { ChatPrefs } from './prefs';
import { activateComposerMenuItem } from './composerMenu';

export type SlashCommandId = 'deep-research' | 'search' | 'crawl';

export interface SlashCommand {
  id: SlashCommandId;
  /** The word after the slash, as typed. */
  name: string;
  /** Other spellings that resolve to the same command. */
  aliases: string[];
  /** One line under the name in the picker. */
  hint: string;
  /** What to show after the command, e.g. "question" or "url". */
  argument: string;
}

export const SLASH_COMMANDS: SlashCommand[] = [
  {
    id: 'deep-research',
    name: 'deep-research',
    aliases: ['research', 'deep'],
    hint: 'Plan, search, open sources, verify, then write a cited report',
    argument: 'question',
  },
  {
    id: 'search',
    name: 'search',
    aliases: ['web', 'web-search'],
    hint: 'Force a web search for this answer',
    argument: 'question',
  },
  {
    id: 'crawl',
    name: 'crawl',
    aliases: ['index', 'site'],
    hint: 'Index a whole website so later questions answer from it',
    argument: 'url',
  },
];

const HEAD_RE = /^\/([a-z][a-z-]*)?(\s|$)/i;

/**
 * The commands whose name or alias starts with what was typed after "/".
 * Empty when the text does not start with a slash command shape — "/" alone
 * lists everything; "/de" lists deep-research; "/x" lists nothing.
 */
export function matchingCommands(text: string): SlashCommand[] {
  const m = HEAD_RE.exec(text);
  if (!m) return [];
  // Once a space follows the command word the picker is done; the user is
  // typing the argument.
  if (m[2] && m[2] !== '') return [];
  const typed = (m[1] ?? '').toLowerCase();
  return SLASH_COMMANDS.filter(
    (c) =>
      c.name.startsWith(typed) || c.aliases.some((a) => a.startsWith(typed)),
  );
}

/** True while the picker should be showing for this text. */
export function isPickingCommand(text: string): boolean {
  return matchingCommands(text).length > 0;
}

export interface ParsedCommand {
  command: SlashCommand;
  /** Everything after the command word, trimmed. */
  rest: string;
}

/**
 * The command at the head of a message, if it is one. A name must match
 * exactly (or an alias) and be followed by whitespace or the end — "/deep"
 * is an alias, "/deepest thoughts" is not a command.
 */
export function parseSlashCommand(text: string): ParsedCommand | null {
  const m = /^\/([a-z][a-z-]*)(?:\s+([\s\S]*))?$/i.exec(text.trim());
  if (!m) return null;
  const word = m[1].toLowerCase();
  const command = SLASH_COMMANDS.find(
    (c) => c.name === word || c.aliases.includes(word),
  );
  if (!command) return null;
  return { command, rest: (m[2] ?? '').trim() };
}

/** The text to put in the box when a command is picked from the list. */
export function completeCommand(command: SlashCommand): string {
  return `/${command.name} `;
}

export interface CommandOutcome {
  /** The message to actually send (never carries the slash). */
  text: string;
  /** The prefs the send must run under. */
  prefs: ChatPrefs;
  /** A reason the send cannot go, when the argument is missing. */
  error?: string;
}

/**
 * Turn "/command argument" into the send it means.
 *
 * Deep research and search go through `activateComposerMenuItem`, so the
 * same rules the "+" menu enforces apply (research is web work → Salesforce
 * off; a forced search does the same). "crawl" rewrites the message into
 * the phrasing the server's crawler detects and leaves prefs alone — it is
 * routed by wording, not by a flag.
 */
export function applySlashCommand(
  parsed: ParsedCommand,
  prefs: ChatPrefs,
): CommandOutcome {
  const { command, rest } = parsed;
  if (!rest) {
    return {
      text: '',
      prefs,
      error: `Type the ${command.argument} after /${command.name}.`,
    };
  }
  switch (command.id) {
    case 'deep-research': {
      const out = activateComposerMenuItem('deep-research', {
        ...prefs,
        deepResearch: false, // activate = turn ON, whatever it was
      });
      return { text: rest, prefs: out.kind === 'prefs' ? out.prefs : prefs };
    }
    case 'search': {
      const base = { ...prefs, salesforce: false, sfLive: false };
      return { text: rest, prefs: { ...base, webSearch: 'on' } };
    }
    case 'crawl': {
      if (!/^https?:\/\//i.test(rest)) {
        return { text: '', prefs, error: 'Give /crawl a full URL, like https://docs.example.com/' };
      }
      // The whole-site crawler triggers on "crawl <url>" (engines/crawl.py).
      // Salesforce mode would never reach it — web work turns it off, like
      // the other two.
      return {
        text: `crawl ${rest}`,
        prefs: { ...prefs, salesforce: false, sfLive: false },
      };
    }
  }
}
