/** Minimal inline icon set — stroke follows currentColor, 16/18px grid. */

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

export const IconPlus = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <path d="M12 5v14M5 12h14" />
  </svg>
);

export const IconSearch = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <circle cx="11" cy="11" r="7" />
    <path d="m20 20-3.5-3.5" />
  </svg>
);

export const IconPencil = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <path d="M17 3a2.8 2.8 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z" />
  </svg>
);

export const IconTrash = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2m3 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6" />
  </svg>
);

export const IconMessage = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <path d="M21 11.5a8 8 0 0 1-8.5 8 9 9 0 0 1-3.4-.6L4 21l1.3-4a8 8 0 0 1 7.2-11.5 8 8 0 0 1 8.5 6Z" />
  </svg>
);

export const IconSun = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <circle cx="12" cy="12" r="4" />
    <path d="M12 2v2m0 16v2M4.9 4.9l1.4 1.4m11.4 11.4 1.4 1.4M2 12h2m16 0h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
  </svg>
);

export const IconMoon = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <path d="M21 12.8A9 9 0 1 1 11.2 3 7 7 0 0 0 21 12.8Z" />
  </svg>
);

export const IconMenu = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <path d="M4 6h16M4 12h16M4 18h16" />
  </svg>
);

export const IconSidebar = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <rect x="3" y="4" width="18" height="16" rx="2" />
    <path d="M9 4v16" />
  </svg>
);

export const IconPaperclip = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <path d="m21.4 11.05-9.19 9.19a6 6 0 0 1-8.49-8.49l8.57-8.57A4 4 0 1 1 18 8.84l-8.59 8.57a2 2 0 0 1-2.83-2.83l8.49-8.48" />
  </svg>
);

export const IconSend = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <path d="M12 19V5m0 0-6 6m6-6 6 6" />
  </svg>
);

export const IconMic = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <rect x="9" y="2" width="6" height="12" rx="3" />
    <path d="M5 11a7 7 0 0 0 14 0" />
    <path d="M12 18v4" />
  </svg>
);

export const IconStop = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor" stroke="none" />
  </svg>
);

export const IconCopy = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <rect x="9" y="9" width="12" height="12" rx="2" />
    <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
  </svg>
);

export const IconCheck = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <path d="m4 12.5 5 5L20 6.5" />
  </svg>
);

export const IconRefresh = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <path d="M21 12a9 9 0 1 1-2.64-6.36M21 3v6h-6" />
  </svg>
);

export const IconChevronDown = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <path d="m6 9 6 6 6-6" />
  </svg>
);

export const IconArrowDown = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <path d="M12 5v14m0 0-6-6m6 6 6-6" />
  </svg>
);

export const IconDownload = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <path d="M12 3v12m0 0-5-5m5 5 5-5M4 21h16" />
  </svg>
);

export const IconExternal = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <path d="M14 4h6v6M20 4l-9 9M10 5H6a2 2 0 0 0-2 2v11a2 2 0 0 0 2 2h11a2 2 0 0 0 2-2v-4" />
  </svg>
);

export const IconAlert = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <path d="M12 3 2.5 19.5h19L12 3Zm0 7v4m0 3.5v.5" />
  </svg>
);

export const IconX = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <path d="M6 6l12 12M18 6 6 18" />
  </svg>
);

export const IconSort = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <path d="M8 4v16m0 0-4-4m4 4 4-4M16 20V4m0 0-4 4m4-4 4 4" />
  </svg>
);

export const IconCloud = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <path d="M17.5 19H7a4 4 0 1 1 .9-7.9 6 6 0 0 1 11.4 2A3.5 3.5 0 0 1 17.5 19Z" />
  </svg>
);

export const IconSparkles = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <path d="m12 3 1.9 4.8L18.7 9.7l-4.8 1.9L12 16.4l-1.9-4.8-4.8-1.9 4.8-1.9Z" />
    <path d="m19 14 .9 2.3 2.1.7-2.1.7L19 20l-.9-2.3-2.1-.7 2.1-.7Z" />
  </svg>
);

export const IconLogout = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9" />
  </svg>
);

export const IconBulb = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <path d="M9 18h6M10 21h4" />
    <path d="M12 3a6 6 0 0 1 3.7 10.7c-.5.4-.7 1-.7 1.6v.7H9v-.7c0-.6-.2-1.2-.7-1.6A6 6 0 0 1 12 3Z" />
  </svg>
);

/* ------------------------------------------- V3: conversation "⋯" menu */

export const IconDots = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <circle cx="5" cy="12" r="1.6" fill="currentColor" stroke="none" />
    <circle cx="12" cy="12" r="1.6" fill="currentColor" stroke="none" />
    <circle cx="19" cy="12" r="1.6" fill="currentColor" stroke="none" />
  </svg>
);

export const IconPin = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <path d="M9.5 3h5l-.6 5.3 2.6 2.5V12H7.5v-1.2l2.6-2.5L9.5 3Z" />
    <path d="M12 12v9" />
  </svg>
);

export const IconPinOff = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <path d="M9.5 3h5l-.6 5.3 2.6 2.5V12H7.5v-1.2l2.6-2.5L9.5 3Z" />
    <path d="M12 12v9" />
    <path d="M3 3l18 18" />
  </svg>
);

export const IconArchive = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <rect x="3" y="4" width="18" height="4" rx="1" />
    <path d="M5 8v11a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8" />
    <path d="M10 12h4" />
  </svg>
);

export const IconUnarchive = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <rect x="3" y="4" width="18" height="4" rx="1" />
    <path d="M5 8v11a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8" />
    <path d="M12 17v-5m0 0-2 2m2-2 2 2" />
  </svg>
);

export const IconChevronRight = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <path d="m9 6 6 6-6 6" />
  </svg>
);

export const IconFileText = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" />
    <path d="M14 3v5h5M9 13h6M9 17h6" />
  </svg>
);

export const IconGlobe = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <circle cx="12" cy="12" r="9" />
    <path d="M3 12h18M12 3c2.5 2.5 2.5 15 0 18M12 3c-2.5 2.5-2.5 15 0 18" />
  </svg>
);

/* ------------------------------- message action row (ChatGPT-style row) */

export const IconThumbUp = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <path d="M7 10v11H4a1 1 0 0 1-1-1v-9a1 1 0 0 1 1-1h3Z" />
    <path d="M7 11.5 11.4 3a2.6 2.6 0 0 1 2.5 3.2L13.2 9h5.6a2 2 0 0 1 1.9 2.6l-2.2 7a2 2 0 0 1-1.9 1.4H7" />
  </svg>
);

export const IconThumbDown = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <path d="M17 14V3h3a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1h-3Z" />
    <path d="M17 12.5 12.6 21a2.6 2.6 0 0 1-2.5-3.2l.7-2.8H5.2a2 2 0 0 1-1.9-2.6l2.2-7A2 2 0 0 1 7.4 4H17" />
  </svg>
);

export const IconBook = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" />
    <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2Z" />
  </svg>
);

/* --------------------------------------------- Mermaid diagram controls */

export const IconCode = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <path d="m9 17-5-5 5-5M15 7l5 5-5 5" />
  </svg>
);

export const IconPlay = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <path d="M7 4.5v15l12-7.5-12-7.5Z" />
  </svg>
);

export const IconExpand = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5" />
  </svg>
);

export const IconZoomIn = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <circle cx="11" cy="11" r="7" />
    <path d="m20 20-3.5-3.5M11 8v6M8 11h6" />
  </svg>
);

export const IconZoomOut = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <circle cx="11" cy="11" r="7" />
    <path d="m20 20-3.5-3.5M8 11h6" />
  </svg>
);

export const IconDiagram = ({ size, className }: IconProps) => (
  <svg {...base(size, className)}>
    <rect x="3" y="3" width="7" height="5" rx="1" />
    <rect x="14" y="3" width="7" height="5" rx="1" />
    <rect x="8.5" y="16" width="7" height="5" rx="1" />
    <path d="M6.5 8v4h11V8M12 12v4" />
  </svg>
);
