/**
 * Admin-only glyphs, drawn on the same grid as components/icons.tsx (24px
 * viewBox, currentColor stroke 2, round caps/joins, rendered 15–18px,
 * aria-hidden). Kept here so the shared icon set stays the chat app's.
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

export const IconGrid = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <rect x="3" y="3" width="7" height="7" rx="1" />
    <rect x="14" y="3" width="7" height="7" rx="1" />
    <rect x="3" y="14" width="7" height="7" rx="1" />
    <rect x="14" y="14" width="7" height="7" rx="1" />
  </svg>
);

export const IconUsers = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" />
    <circle cx="9" cy="7" r="4" />
    <path d="M22 21v-2a4 4 0 0 0-3-3.87" />
    <path d="M16 3.13a4 4 0 0 1 0 7.75" />
  </svg>
);

export const IconUserPlus = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <path d="M16 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" />
    <circle cx="8.5" cy="7" r="4" />
    <path d="M20 8v6M23 11h-6" />
  </svg>
);

export const IconMail = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <rect x="2" y="4" width="20" height="16" rx="2" />
    <path d="m22 7-10 6L2 7" />
  </svg>
);

export const IconShield = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
  </svg>
);

export const IconKey = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <circle cx="7.5" cy="15.5" r="5.5" />
    <path d="m21 2-9.6 9.6M15.5 7.5l3 3L22 7l-3-3" />
  </svg>
);

export const IconBan = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <circle cx="12" cy="12" r="9" />
    <path d="m5.6 5.6 12.8 12.8" />
  </svg>
);

export const IconEye = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
    <circle cx="12" cy="12" r="3" />
  </svg>
);

export const IconMonitor = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <rect x="2" y="3" width="20" height="14" rx="2" />
    <path d="M8 21h8M12 17v4" />
  </svg>
);

export const IconArrowLeft = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <path d="M19 12H5M12 19l-7-7 7-7" />
  </svg>
);

/** Analytics export. */
export const IconDownload = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <path d="M12 3v12m0 0-5-5m5 5 5-5M4 21h16" />
  </svg>
);

/** Feature access — sliders, the "what may this person use" surface. */
export const IconSliders = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h10M18 18h2" />
    <circle cx="16" cy="6" r="2" />
    <circle cx="10" cy="12" r="2" />
    <circle cx="16" cy="18" r="2" />
  </svg>
);
