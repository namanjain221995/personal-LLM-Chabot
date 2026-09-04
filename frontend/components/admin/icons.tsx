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

export const IconLink = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7" />
    <path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7" />
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

// --- Analytics console (2026-09-04) -----------------------------------------

export const IconChart = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <path d="M3 3v16a2 2 0 0 0 2 2h16" />
    <path d="M7 15l4-5 3 3 4-6" />
  </svg>
);

export const IconTrophy = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <path d="M8 21h8" />
    <path d="M12 17v4" />
    <path d="M7 4h10v5a5 5 0 0 1-10 0V4Z" />
    <path d="M17 5h3v2a3 3 0 0 1-3 3" />
    <path d="M7 5H4v2a3 3 0 0 0 3 3" />
  </svg>
);

export const IconMessages = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <path d="M21 11.5a8.4 8.4 0 0 1-9 8.4 9 9 0 0 1-3.9-.9L3 21l1.9-4.6A8.4 8.4 0 0 1 4 11.5 8.5 8.5 0 0 1 12.5 3 8.4 8.4 0 0 1 21 11.5Z" />
  </svg>
);

export const IconFlask = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <path d="M10 3h4" />
    <path d="M10 3v6.5L4.6 18A2 2 0 0 0 6.3 21h11.4a2 2 0 0 0 1.7-3L14 9.5V3" />
    <path d="M7 15h10" />
  </svg>
);

export const IconWorld = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <circle cx="12" cy="12" r="9" />
    <path d="M3 12h18" />
    <path d="M12 3a15 15 0 0 1 0 18a15 15 0 0 1 0-18Z" />
  </svg>
);

export const IconCloudCog = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <path d="M17.5 19H7a4.5 4.5 0 1 1 .9-8.9A6 6 0 0 1 19 11.4a4 4 0 0 1-1.5 7.6Z" />
  </svg>
);

export const IconCpu = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <rect x="6" y="6" width="12" height="12" rx="2" />
    <path d="M10 10h4v4h-4z" />
    <path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3" />
  </svg>
);

export const IconGauge = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <path d="M4 18a9 9 0 1 1 16 0" />
    <path d="M12 15l4-4" />
  </svg>
);

export const IconMic = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <rect x="9" y="2" width="6" height="11" rx="3" />
    <path d="M5 11a7 7 0 0 0 14 0" />
    <path d="M12 18v3M8 21h8" />
  </svg>
);

export const IconServer = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <rect x="3" y="4" width="18" height="7" rx="2" />
    <rect x="3" y="13" width="18" height="7" rx="2" />
    <path d="M7 8h.01M7 17h.01" />
  </svg>
);
