'use client';

/**
 * Assistant-message markdown (§9): GFM tables, code blocks with copy button
 * in JetBrains Mono, safe external links, and syntax highlighting
 * (2026-08-06, ChatGPT-style): rehype-highlight tags tokens with hljs-*
 * classes and globals.css maps them to theme-aware colors.
 */

import { isValidElement, memo, useMemo, type ReactNode } from 'react';
import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
import { isMermaidLanguage } from '@/lib/mermaid';
import { splitMarkdown } from '@/lib/markdownSegments';
import { CopyButton } from './CopyButton';
import { MermaidBlock } from './MermaidBlock';

function extractText(node: ReactNode): string {
  if (typeof node === 'string' || typeof node === 'number') return String(node);
  if (Array.isArray(node)) return node.map(extractText).join('');
  if (isValidElement(node)) {
    return extractText(
      (node.props as { children?: ReactNode }).children ?? '',
    );
  }
  return '';
}

function CodeBlock({ children }: { children?: ReactNode }) {
  let language: string | undefined;
  if (isValidElement(children)) {
    const cls =
      (children.props as { className?: string }).className ?? '';
    language = /language-([\w-]+)/.exec(cls)?.[1];
  }
  const code = extractText(children).replace(/\n$/, '');

  // ```mermaid → render as a real diagram (preview/code toggle, zoom, PNG).
  if (isMermaidLanguage(language)) {
    return <MermaidBlock code={code} />;
  }

  return (
    <div className="code-block overflow-hidden rounded-ts border border-border bg-surface">
      <div className="flex items-center justify-between border-b border-border bg-surface-2/60 px-3 py-1.5">
        <span className="font-mono text-[11px] uppercase tracking-wide text-faint">
          {language ?? 'text'}
        </span>
        <CopyButton text={code} label="Copy code" />
      </div>
      {/* `children` is the <code> element WITH the hljs token spans —
          re-rendering the extracted plain text here would throw the
          highlighting away (it is still used for copy + mermaid above). */}
      <pre tabIndex={0}>{children}</pre>
    </div>
  );
}

const components: Components = {
  pre: ({ children }) => <CodeBlock>{children}</CodeBlock>,
  code: ({ children, className, ...props }) => (
    // Block code keeps its hljs/language-* classes (the token colors);
    // inline code — no className from the parser — gets the pill style.
    <code className={className ?? 'inline-code'} {...props}>
      {children}
    </code>
  ),
  table: ({ children }) => (
    <div className="md-table-wrap">
      <table>{children}</table>
    </div>
  ),
  a: ({ href, children }) => (
    <a href={href} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  ),
};

/**
 * One parsed piece of an answer.
 *
 * Memoized on its text, which is the whole point: `splitMarkdown` hands back
 * chunks whose text is settled, so a chunk parses ONCE and is then skipped
 * entirely — no remark, no highlighting, no React work — for the rest of the
 * answer. ReactMarkdown renders a Fragment, so several of these inside one
 * `.md` container produce exactly the block sequence a single one would, and
 * `.md > * + *` spacing is unaffected.
 */
const MarkdownChunk = memo(function MarkdownChunk({ text }: { text: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      // detect:false — only fenced blocks with a language tag get
      // highlighted; guessing on plain blocks colors prose-y output.
      // Unknown languages (```mermaid included) pass through untouched.
      rehypePlugins={[[rehypeHighlight, { detect: false }]]}
      components={components}
    >
      {text}
    </ReactMarkdown>
  );
});

/**
 * NEW-24 — an answer is parsed incrementally, not from scratch every frame.
 *
 * A streaming answer only changes at its end, but this re-parsed the whole
 * thing on every visual update, and remark-parse is superlinear: measured in
 * V8, one parse of 20 KB of prose-markdown is 30 ms and 40 KB is 65 ms, with
 * a large table reaching 173 ms. A 60 Hz frame is 16.7 ms, so past a few
 * kilobytes the parse alone overran the budget — the browser could not paint
 * on time, and could not service wheel or touch input either. That is what
 * made both the answer and scrolling jerky.
 *
 * `splitMarkdown` finds the point up to which the text can no longer change
 * meaning (see the safety rule there, and the equivalence proof in
 * tests/markdown-segments.test.tsx). Everything before it is rendered as
 * memoized chunks; only the tail — the block still being written — is
 * re-parsed. Per-frame cost stops growing with the answer.
 *
 * The chunks and the tail share ONE keyed array on purpose. When the tail
 * settles it keeps its slot and simply stops changing, so a finished block is
 * never unmounted and remounted: no flicker, no lost text selection, and no
 * DOM churn at a block boundary.
 */
export const Markdown = memo(function Markdown({ text }: { text: string }) {
  const chunks = useMemo(() => {
    const { frozen, tail } = splitMarkdown(text);
    return [...frozen, tail];
  }, [text]);
  return (
    <div className="md">
      {chunks.map((chunk, i) => (
        // eslint-disable-next-line react/no-array-index-key -- the list is
        // append-only and positional: chunk N is always the same block of the
        // answer, which is exactly the identity this slot needs.
        <MarkdownChunk key={i} text={chunk} />
      ))}
    </div>
  );
});
