// @vitest-environment jsdom
/**
 * The public page renders text a stranger will read, from a conversation that
 * may itself contain anything a web page said. So the question this file
 * exists to answer is narrow and absolute: can shared content EXECUTE?
 *
 * It is asked against the real `Markdown` component rather than a mock,
 * because the property belongs to that component and a mock would only prove
 * the mock is safe.
 */
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { Markdown } from '@/components/Markdown';

afterEach(cleanup);

describe('hostile content on a shared page', () => {
  it('does not put a script element in the DOM', () => {
    const { container } = render(
      <Markdown text={'Hello\n\n<script>window.__pwned = 1</script>\n'} />,
    );
    expect(container.querySelector('script')).toBeNull();
    expect((window as unknown as Record<string, unknown>).__pwned).toBeUndefined();
  });

  it('does not create an element carrying an event handler', () => {
    const { container } = render(
      <Markdown text={'<img src="x" onerror="window.__pwned = 1">'} />,
    );
    const img = container.querySelector('img');
    // Either no img at all, or one with no handler — both are safe; an img
    // WITH an onerror attribute would not be.
    expect(img?.getAttribute('onerror') ?? null).toBeNull();
    expect((window as unknown as Record<string, unknown>).__pwned).toBeUndefined();
  });

  it('does not produce a javascript: link', () => {
    const { container } = render(
      <Markdown text={'[click me](javascript:alert(1))'} />,
    );
    for (const a of container.querySelectorAll('a')) {
      expect(a.getAttribute('href') ?? '').not.toMatch(/^javascript:/i);
    }
  });

  it('does not embed an iframe', () => {
    const { container } = render(
      <Markdown text={'<iframe src="https://evil.test"></iframe>'} />,
    );
    expect(container.querySelector('iframe')).toBeNull();
  });

  it('renders hostile markup as visible TEXT instead', () => {
    // The safe outcome is not silence — a reader should see what was written.
    render(<Markdown text={'<script>alert(1)</script>'} />);
    expect(document.body.textContent).toContain('alert(1)');
  });

  it('still renders the ordinary things a conversation contains', () => {
    const { container } = render(
      <Markdown
        text={
          '# Heading\n\n- one\n- two\n\n| a | b |\n| - | - |\n| 1 | 2 |\n\n' +
          '```python\nprint("hi")\n```\n\n[a link](https://example.com)\n'
        }
      />,
    );
    expect(container.querySelector('h1')).toBeTruthy();
    expect(container.querySelectorAll('li')).toHaveLength(2);
    expect(container.querySelector('table')).toBeTruthy();
    expect(container.querySelector('code')).toBeTruthy();
    expect(container.querySelector('a')?.getAttribute('href')).toBe(
      'https://example.com',
    );
  });
});
