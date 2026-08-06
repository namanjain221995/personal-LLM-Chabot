'use client';

/**
 * Assistant-message markdown (§9): GFM tables, code blocks with copy button
 * in JetBrains Mono, safe external links, and syntax highlighting
 * (2026-08-06, ChatGPT-style): rehype-highlight tags tokens with hljs-*
 * classes and globals.css maps them to theme-aware colors.
 */

import { isValidElement, memo, type ReactNode } from 'react';
import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
import { isMermaidLanguage } from '@/lib/mermaid';
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

export const Markdown = memo(function Markdown({ text }: { text: string }) {
  return (
    <div className="md">
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
    </div>
  );
});
