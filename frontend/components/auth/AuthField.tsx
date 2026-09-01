'use client';

/**
 * Labelled form field — the first label + input + caption pattern in the
 * codebase (everything before auth was borderless-in-container or
 * edit-in-place). Composes existing tokens only: bg-bg field on rounded-lg,
 * border-border resting, border-accent/60 focus (the Sidebar rename-input
 * precedent), text-faint captions.
 *
 * `type="password"` grows a show/hide toggle; `readOnly` renders the value on
 * bg-surface in muted ink so pre-filled facts (the invited email) read as
 * facts, not as something to edit.
 */

import { useState } from 'react';
import { IconEye, IconEyeOff } from './icons';

interface AuthFieldProps {
  id: string;
  label: string;
  type?: 'text' | 'email' | 'password';
  value: string;
  onChange?: (value: string) => void;
  autoComplete?: string;
  autoFocus?: boolean;
  placeholder?: string;
  /** Small caption under the field (e.g. the password-length rule). */
  hint?: string;
  readOnly?: boolean;
  required?: boolean;
  disabled?: boolean;
}

export function AuthField({
  id,
  label,
  type = 'text',
  value,
  onChange,
  autoComplete,
  autoFocus,
  placeholder,
  hint,
  readOnly,
  required,
  disabled,
}: AuthFieldProps) {
  const isPassword = type === 'password';
  const [visible, setVisible] = useState(false);

  // Taller and rounder than the app's inline inputs: a sign-in field is the
  // only thing on the page, so it carries the page's weight.
  const fieldClasses = readOnly
    ? 'w-full cursor-default rounded-xl border border-border bg-surface px-4 py-2.5 text-sm text-muted focus:outline-none'
    : 'w-full rounded-xl border border-border bg-bg px-4 py-2.5 text-sm text-ink placeholder:text-faint transition-colors duration-ts hover:border-icon/40 focus:border-accent focus:outline-none focus:ring-4 focus:ring-accent/12 disabled:opacity-50';

  return (
    <div>
      <label htmlFor={id} className="block text-xs font-medium text-ink">
        {label}
      </label>
      <div className="relative mt-1.5">
        <input
          id={id}
          type={isPassword && visible ? 'text' : type}
          value={value}
          onChange={(e) => onChange?.(e.target.value)}
          autoComplete={autoComplete}
          autoFocus={autoFocus}
          placeholder={placeholder}
          readOnly={readOnly}
          required={required}
          disabled={disabled}
          aria-describedby={hint ? `${id}-hint` : undefined}
          className={`${fieldClasses}${isPassword ? ' pr-10' : ''}`}
        />
        {isPassword && (
          <button
            type="button"
            onClick={() => setVisible((v) => !v)}
            aria-label={visible ? 'Hide password' : 'Show password'}
            aria-pressed={visible}
            className="absolute inset-y-0 right-0 flex w-11 items-center justify-center rounded-xl text-icon transition-colors duration-ts hover:text-ink"
          >
            {visible ? <IconEyeOff size={16} /> : <IconEye size={16} />}
          </button>
        )}
      </div>
      {hint && (
        <p id={`${id}-hint`} className="mt-1.5 text-xs text-faint">
          {hint}
        </p>
      )}
    </div>
  );
}
