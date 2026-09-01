/**
 * Auth-only glyphs — the password visibility pair. They live here rather than
 * in components/icons.tsx because the auth retrofit owns only this directory;
 * same recipe as the main set (24px viewBox, currentColor stroke, width 2,
 * round caps/joins, aria-hidden) so a later merge is a copy-paste.
 */

interface IconProps {
  size?: number;
  className?: string;
}

function base(size: number | undefined, className: string | undefined) {
  return {
    width: size ?? 16,
    height: size ?? 16,
    viewBox: '0 0 24 24',
    fill: 'none',
    stroke: 'currentColor',
    strokeWidth: 2,
    strokeLinecap: 'round' as const,
    strokeLinejoin: 'round' as const,
    className,
    'aria-hidden': true,
  };
}

export const IconEye = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z" />
    <circle cx="12" cy="12" r="3" />
  </svg>
);

export const IconEyeOff = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c6.5 0 10 8 10 8a13.16 13.16 0 0 1-1.67 2.68" />
    <path d="M6.61 6.61A13.526 13.526 0 0 0 2 12s3.5 8 10 8a9.74 9.74 0 0 0 5.39-1.61" />
    <path d="M9.88 9.88a3 3 0 1 0 4.24 4.24" />
    <path d="m2 2 20 20" />
  </svg>
);
