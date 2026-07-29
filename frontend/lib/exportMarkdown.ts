/**
 * Conversation export (V3 §2, menu item "Export chat").
 *
 * ChatGPT's "Share" publishes a public URL; this platform never sends
 * anything off the machine, so the local equivalent is a Markdown file built
 * ENTIRELY in the browser from the conversation the store already has, and
 * handed to the user through a Blob download. Nothing is uploaded anywhere.
 *
 * Layout: the title as an H1, then `## You` / `## TechSara` sections in
 * message order; an assistant turn carries its SQL in a fenced ```sql block
 * and its citation record IDs underneath when present.
 */

import type { ChatMessage, Conversation } from './types';

/** Displayed name of the assistant in exported files. */
const ASSISTANT_LABEL = 'TechSara';
const SLUG_MAX = 48;

/** URL/file-safe slug of a conversation title (never empty). */
export function slugifyTitle(title: string): string {
  const slug = title
    .toLowerCase()
    .normalize('NFKD')
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, SLUG_MAX)
    .replace(/-+$/g, '');
  return slug || 'conversation';
}

/** `<slug>-<id>.md` (V3 §2). */
export function exportFilename(title: string, id: string): string {
  return `${slugifyTitle(title)}-${id}.md`;
}

function messageSection(message: ChatMessage): string {
  const parts: string[] = [
    `## ${message.role === 'user' ? 'You' : ASSISTANT_LABEL}`,
  ];

  const content = message.content.trim();
  if (content) {
    parts.push(content);
  } else if (message.errorMessage) {
    parts.push(`_Error: ${message.errorMessage.trim()}_`);
  } else if (message.status === 'stopped') {
    parts.push('_Stopped before an answer arrived._');
  }

  if (message.role === 'assistant') {
    const sql = message.meta?.sql?.trim();
    if (sql) parts.push(['```sql', sql, '```'].join('\n'));

    const records = (message.meta?.citations ?? [])
      .map((c) => c.record_id)
      .filter((recordId): recordId is string => Boolean(recordId));
    if (records.length > 0) {
      parts.push(`**Records:** ${records.join(', ')}`);
    }
  }

  return parts.join('\n\n');
}

/**
 * The whole conversation as Markdown. Pure — no DOM, no network — so the
 * exact bytes the user downloads are unit-tested.
 */
export function buildConversationMarkdown(conversation: Conversation): string {
  const heading = `# ${conversation.title.trim() || 'Conversation'}`;
  const sections = conversation.messages.map(messageSection);
  return [heading, ...sections].join('\n\n') + '\n';
}

export interface ExportedConversation {
  filename: string;
  markdown: string;
}

export function buildConversationExport(
  conversation: Conversation,
): ExportedConversation {
  return {
    filename: exportFilename(conversation.title, conversation.id),
    markdown: buildConversationMarkdown(conversation),
  };
}

/**
 * Browser-only: hand `markdown` to the user as a downloaded file. Kept apart
 * from the builder above so the builder stays testable in the node
 * environment vitest runs in.
 */
export function downloadMarkdown({
  filename,
  markdown,
}: ExportedConversation): void {
  const blob = new Blob([markdown], {
    type: 'text/markdown;charset=utf-8',
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.rel = 'noopener';
  document.body.appendChild(link);
  link.click();
  link.remove();
  // Give the browser a tick to start the download before dropping the blob.
  setTimeout(() => URL.revokeObjectURL(url), 0);
}
