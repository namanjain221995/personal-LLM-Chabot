'use client';

/**
 * The one switch in the admin area.
 *
 * A real `<button role="switch">`, not a styled checkbox: the control is
 * 44×24 with a 20px thumb, which is a finger-sized target and reads as
 * on/off to a screen reader without a visually-hidden input to keep in sync.
 * `aria-checked` carries the state, `disabled` carries the reason it cannot
 * be moved, and the focus ring is offset from the page background so it
 * survives on a dark surface.
 *
 * The thumb moves on transform (compositor-only) for 160ms — long enough to
 * read as a state change, short enough that a run of five toggles never
 * feels like an animation sequence.
 */

export function Switch({
  checked,
  disabled = false,
  label,
  describedBy,
  onChange,
}: {
  checked: boolean;
  disabled?: boolean;
  /** Accessible name — the visible title sits in the row, not on the control. */
  label: string;
  /** Id of the description element, so the hint is announced with it. */
  describedBy?: string;
  onChange: (next: boolean) => void;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      aria-describedby={describedBy}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full border border-transparent transition-colors duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-bg ${
        disabled ? 'cursor-not-allowed opacity-40' : 'cursor-pointer'
      } ${checked ? 'bg-accent-strong' : 'bg-[var(--admin-switch-off)]'}`}
    >
      <span
        aria-hidden
        className="pointer-events-none block h-5 w-5 rounded-full bg-white shadow-sm transition-transform duration-200 ease-out"
        style={{ transform: `translateX(${checked ? 22 : 2}px)` }}
      />
    </button>
  );
}
