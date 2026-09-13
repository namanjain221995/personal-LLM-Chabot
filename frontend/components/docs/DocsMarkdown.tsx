'use client';

/**
 * The documentation renderer.
 *
 * It is a near-twin of components/Markdown.tsx — the same remark-gfm, the
 * same rehype-highlight with `detect: false`, the same `.code-block` shell
 * with a language label and a CopyButton, the same `.md` prose class — so a
 * code sample on /docs is coloured by the very same hljs token rules as a
 * code block in an answer, and nothing on this site looks like a different
 * product.
 *
 * WHAT IT ADDS, AND WHY IT IS NOT A PROP ON THE OTHER ONE.
 *
 *  - **Heading anchors.** Every `##` and `###` gets an id from
 *    `slugifyHeading` and a link to itself, so "On this page" works, so a
 *    cross-page link can point at a section, and so a reader can copy a link
 *    to the paragraph they want somebody else to read.
 *  - **Internal links go through next/link.** Moving between documentation
 *    pages is a client navigation; an answer's links are all external.
 *  - **No incremental parsing.** components/Markdown.tsx splits a streaming
 *    answer into frozen chunks because it is re-parsed on every frame. A
 *    documentation body is a constant: it parses once per mount and the
 *    chunking machinery would be pure cost.
 *
 * Editing the chat renderer to serve both would mean adding streaming
 * concerns to a static page and static concerns to the hot path of every
 * answer. The shared parts — CopyButton, the `.md` and `.code-block` styles,
 * the highlight theme — are shared; the two behaviours are not.
 */

import { isValidElement, memo, type ComponentPropsWithoutRef, type ReactNode } from 'react';
import Link from 'next/link';
import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
import { CopyButton } from '@/components/CopyButton';
import { slugifyHeading } from '@/content/docs/headings';

function extractText(node: ReactNode): string {
  if (typeof node === 'string' || typeof node === 'number') return String(node);
  if (Array.isArray(node)) return node.map(extractText).join('');
  if (isValidElement(node)) {
    return extractText((node.props as { children?: ReactNode }).children ?? '');
  }
  return '';
}

function CodeBlock({ children }: { children?: ReactNode }) {
  let language: string | undefined;
  if (isValidElement(children)) {
    const cls = (children.props as { className?: string }).className ?? '';
    language = /language-([\w-]+)/.exec(cls)?.[1];
  }
  const code = extractText(children).replace(/\n$/, '');

  return (
    <div className="code-block overflow-hidden rounded-ts border border-border bg-surface">
      <div className="flex items-center justify-between gap-2 border-b border-border bg-[color-mix(in_srgb,var(--ts-surface-2)_60%,transparent)] px-3 py-1.5">
        <span className="font-mono text-[11px] uppercase tracking-wide text-faint">
          {language ?? 'text'}
        </span>
        <CopyButton text={code} label="Copy code" />
      </div>
      {/* `children` is the <code> element WITH the hljs token spans — the
          extracted plain text above is for the clipboard only. tabIndex makes
          a wide sample scrollable by keyboard, not just by trackpad. */}
      <pre tabIndex={0}>{children}</pre>
    </div>
  );
}

/**
 * A linkable heading.
 *
 * The anchor is a real focusable link that becomes visible on hover AND on
 * keyboard focus. A "copy link" affordance that only appears on hover is
 * invisible to anyone navigating with a keyboard, which is the group most
 * likely to want a durable link in the first place.
 */
function Heading({
  level,
  children,
}: {
  level: 2 | 3;
  children?: ReactNode;
}) {
  const text = extractText(children);
  const id = slugifyHeading(text);
  const Tag = level === 2 ? 'h2' : 'h3';
  return (
    <Tag id={id} className="group scroll-mt-24">
      {children}{' '}
      <a
        href={`#${id}`}
        aria-label={`Link to this section: ${text}`}
        className="ml-1 inline-block align-baseline font-mono text-sm text-faint no-underline opacity-0 transition-opacity duration-ts hover:text-accent focus-visible:opacity-100 group-hover:opacity-100"
      >
        #
      </a>
    </Tag>
  );
}

/**
 * `props` minus react-markdown's `node` (the mdast node). Spread onto a DOM
 * element it renders as `node="[object Object]"` — 715 of them across the
 * site, about 18 KB of server-rendered junk (verifier finding, 2026-09-13).
 */
function domProps<T extends object>(props: T): Omit<T, 'node'> {
  const rest = { ...props } as Record<string, unknown>;
  delete rest.node;
  return rest as Omit<T, 'node'>;
}

const components: Components = {
  h2: ({ children }) => <Heading level={2}>{children}</Heading>,
  h3: ({ children }) => <Heading level={3}>{children}</Heading>,
  pre: ({ children }) => <CodeBlock>{children}</CodeBlock>,
  code: ({ children, className, ...props }) => (
    // Block code keeps its hljs/language-* classes (the token colours);
    // inline code — no className from the parser — gets the pill style.
    <code className={className ?? 'inline-code'} {...domProps(props)}>
      {children}
    </code>
  ),
  table: ({ children }) => (
    // The one horizontal scroller on the page. A seven-column error table
    // cannot fit 400px, and the body must never scroll sideways.
    <div className="md-table-wrap">
      <table>{children}</table>
    </div>
  ),
  a: ({ href, children, ...rest }: ComponentPropsWithoutRef<'a'> & { node?: unknown }) => {
    const props = domProps(rest);
    if (!href) return <span>{children}</span>;
    // `//host/path` starts with a slash too, and it is NOT internal: it is a
    // protocol-relative link to another site. Routed through next/link it
    // would leave without `rel="noopener noreferrer"` (verifier finding,
    // 2026-09-13), so it falls through to the external branch below.
    if (href.startsWith('/') && !href.startsWith('//')) {
      // A documentation-internal link: client navigation, no reload.
      return (
        <Link href={href} {...props}>
          {children}
        </Link>
      );
    }
    if (href.startsWith('#')) {
      return (
        <a href={href} {...props}>
          {children}
        </a>
      );
    }
    return (
      <a href={href} target="_blank" rel="noopener noreferrer" {...props}>
        {children}
      </a>
    );
  },
};

export const DocsMarkdown = memo(function DocsMarkdown({ body }: { body: string }) {
  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        // detect:false — only fenced blocks with a language tag are
        // highlighted. Guessing on an untagged block colours prose.
        rehypePlugins={[[rehypeHighlight, { detect: false }]]}
        components={components}
      >
        {body}
      </ReactMarkdown>
    </div>
  );
});
