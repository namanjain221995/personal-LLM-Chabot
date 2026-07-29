'use client';

/**
 * Assistant-message markdown (§9): GFM tables, code blocks with copy button
 * in JetBrains Mono, safe external links.
 */

import { isValidElement, memo, type ReactNode } from 'react';
import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
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
      <pre tabIndex={0}>
        <code>{code}</code>
      </pre>
    </div>
  );
}

const components: Components = {
  pre: ({ children }) => <CodeBlock>{children}</CodeBlock>,
  code: ({ children, ...props }) => (
    <code className="inline-code" {...props}>
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
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {text}
      </ReactMarkdown>
    </div>
  );
});
