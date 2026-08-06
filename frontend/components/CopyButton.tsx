'use client';

import { useState } from 'react';
import { IconCheck, IconCopy } from './icons';

export function CopyButton({
  text,
  label,
  className = '',
  variant = 'chip',
}: {
  text: string;
  label: string;
  className?: string;
  /** "icon": ghost icon-only button for the message action row. */
  variant?: 'chip' | 'icon';
}) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // Clipboard API unavailable (non-secure context) — fallback.
      const ta = document.createElement('textarea');
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      ta.remove();
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 1600);
  }

  if (variant === 'icon') {
    return (
      <button
        type="button"
        onClick={copy}
        aria-label={copied ? 'Copied' : label}
        title={copied ? 'Copied' : label}
        className={`rounded-lg p-1.5 text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink ${className}`}
      >
        {copied ? (
          <IconCheck size={15} className="text-accent" />
        ) : (
          <IconCopy size={15} />
        )}
      </button>
    );
  }

  return (
    <button
      type="button"
      onClick={copy}
      aria-label={copied ? 'Copied' : label}
      title={copied ? 'Copied' : label}
      className={`inline-flex items-center gap-1.5 rounded-md border border-border bg-surface px-2 py-1 text-xs text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink ${className}`}
    >
      {copied ? (
        <IconCheck size={13} className="text-accent" />
      ) : (
        <IconCopy size={13} />
      )}
      {copied ? 'Copied' : 'Copy'}
    </button>
  );
}
