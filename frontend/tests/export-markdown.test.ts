/**
 * V3 §2 — "Export chat" builds the Markdown file entirely client-side.
 * These tests pin the exact bytes the owner downloads.
 */

import { describe, expect, it } from 'vitest';
import {
  buildConversationExport,
  buildConversationMarkdown,
  exportFilename,
  slugifyTitle,
} from '../lib/exportMarkdown';
import type { ChatMessage, Conversation } from '../lib/types';

function conversation(messages: ChatMessage[], title = 'Pipeline review'): Conversation {
  return {
    id: 'c-123',
    title,
    createdAt: 1,
    updatedAt: 2,
    messages,
  };
}

function user(content: string): ChatMessage {
  return { id: `u-${content}`, role: 'user', content, createdAt: 1 };
}

describe('export filename (V3 §2)', () => {
  it('is <slug>-<id>.md', () => {
    expect(exportFilename('Win rate this quarter', 'abc-123')).toBe(
      'win-rate-this-quarter-abc-123.md',
    );
  });

  it('slugifies punctuation, spaces and case', () => {
    expect(slugifyTitle('  Top 10 Accounts: EMEA / APAC!  ')).toBe(
      'top-10-accounts-emea-apac',
    );
  });

  it('never produces an empty or over-long slug', () => {
    expect(slugifyTitle('!!!')).toBe('conversation');
    expect(slugifyTitle('…')).toBe('conversation');
    const long = slugifyTitle('a'.repeat(200));
    expect(long.length).toBeLessThanOrEqual(48);
    expect(exportFilename('', 'id1')).toBe('conversation-id1.md');
  });
});

describe('markdown builder (V3 §2)', () => {
  it('writes the title as H1 and the turns in order', () => {
    const md = buildConversationMarkdown(
      conversation([
        user('first question'),
        {
          id: 'a1',
          role: 'assistant',
          content: 'first answer',
          status: 'done',
          createdAt: 2,
        },
        user('second question'),
        {
          id: 'a2',
          role: 'assistant',
          content: 'second answer',
          status: 'done',
          createdAt: 4,
        },
      ]),
    );
    expect(md).toBe(
      [
        '# Pipeline review',
        '',
        '## You',
        '',
        'first question',
        '',
        '## TechSara',
        '',
        'first answer',
        '',
        '## You',
        '',
        'second question',
        '',
        '## TechSara',
        '',
        'second answer',
        '',
      ].join('\n'),
    );
  });

  it('puts assistant SQL in a fenced sql block', () => {
    const md = buildConversationMarkdown(
      conversation([
        user('open pipeline by owner'),
        {
          id: 'a1',
          role: 'assistant',
          content: 'Here are the numbers.',
          status: 'done',
          createdAt: 2,
          meta: {
            route: 'sql',
            sql: 'SELECT owner, SUM(amount)\nFROM opportunity\nGROUP BY owner',
          },
        },
      ]),
    );
    expect(md).toContain(
      '```sql\nSELECT owner, SUM(amount)\nFROM opportunity\nGROUP BY owner\n```',
    );
    // The fence belongs to the assistant section, after its answer.
    expect(md.indexOf('Here are the numbers.')).toBeLessThan(
      md.indexOf('```sql'),
    );
  });

  it('lists citation record IDs when present', () => {
    const md = buildConversationMarkdown(
      conversation([
        user('what did we agree with Acme?'),
        {
          id: 'a1',
          role: 'assistant',
          content: 'Per the meeting notes…',
          status: 'done',
          createdAt: 2,
          meta: {
            route: 'rag',
            citations: [
              {
                record_id: '0011x00000ABCde',
                object: 'Account',
                url: 'https://example.my.salesforce.com/0011x00000ABCde',
              },
              {
                record_id: '0061x00000ZYXwv',
                object: 'Opportunity',
                url: 'https://example.my.salesforce.com/0061x00000ZYXwv',
              },
            ],
          },
        },
      ]),
    );
    expect(md).toContain('**Records:** 0011x00000ABCde, 0061x00000ZYXwv');
  });

  it('omits SQL / records sections when there are none', () => {
    const md = buildConversationMarkdown(
      conversation([
        user('hello'),
        {
          id: 'a1',
          role: 'assistant',
          content: 'Hi!',
          status: 'done',
          createdAt: 2,
          meta: { route: 'chat' },
        },
      ]),
    );
    expect(md).not.toContain('```sql');
    expect(md).not.toContain('**Records:**');
  });

  it('keeps an errored or stopped turn readable instead of blank', () => {
    const md = buildConversationMarkdown(
      conversation([
        user('crash please'),
        {
          id: 'a1',
          role: 'assistant',
          content: '',
          status: 'error',
          errorMessage: 'The orchestrator is unreachable.',
          createdAt: 2,
        },
      ]),
    );
    expect(md).toContain('_Error: The orchestrator is unreachable._');
  });

  it('falls back to a usable title and always ends with a newline', () => {
    const md = buildConversationMarkdown(conversation([user('hi')], '   '));
    expect(md.startsWith('# Conversation\n')).toBe(true);
    expect(md.endsWith('\n')).toBe(true);
  });

  it('bundles the filename with the markdown', () => {
    const file = buildConversationExport(conversation([user('hi')]));
    expect(file.filename).toBe('pipeline-review-c-123.md');
    expect(file.markdown).toContain('# Pipeline review');
  });
});
