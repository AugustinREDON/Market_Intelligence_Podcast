/** Small inline SVG icon set (no icon dependency). All icons are decorative: label the control, not the icon. */
type P = { size?: number; className?: string };
const base = (size = 16, className?: string) => ({
  width: size,
  height: size,
  viewBox: '0 0 24 24',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.8,
  strokeLinecap: 'round' as const,
  strokeLinejoin: 'round' as const,
  'aria-hidden': true,
  focusable: false,
  className,
});

export const PlayIcon = ({ size, className }: P) => (
  <svg {...base(size, className)} fill="currentColor" stroke="none">
    <path d="M8 5.5v13a1 1 0 0 0 1.5.86l10.4-6.5a1 1 0 0 0 0-1.72L9.5 4.64A1 1 0 0 0 8 5.5Z" />
  </svg>
);
export const PauseIcon = ({ size, className }: P) => (
  <svg {...base(size, className)} fill="currentColor" stroke="none">
    <rect x="6" y="5" width="4" height="14" rx="1" />
    <rect x="14" y="5" width="4" height="14" rx="1" />
  </svg>
);
export const DownloadIcon = ({ size, className }: P) => (
  <svg {...base(size, className)}>
    <path d="M12 4v11m0 0-4.5-4.5M12 15l4.5-4.5M5 19.5h14" />
  </svg>
);
export const ChevronDown = ({ size, className }: P) => (
  <svg {...base(size, className)}>
    <path d="m6 9 6 6 6-6" />
  </svg>
);
export const ChevronUp = ({ size, className }: P) => (
  <svg {...base(size, className)}>
    <path d="m6 15 6-6 6 6" />
  </svg>
);
export const ArrowLeft = ({ size, className }: P) => (
  <svg {...base(size, className)}>
    <path d="M19 12H5m0 0 6-6m-6 6 6 6" />
  </svg>
);
export const ArrowRight = ({ size, className }: P) => (
  <svg {...base(size, className)}>
    <path d="M5 12h14m0 0-6-6m6 6-6 6" />
  </svg>
);
export const SearchIcon = ({ size, className }: P) => (
  <svg {...base(size, className)}>
    <circle cx="11" cy="11" r="6.5" />
    <path d="m20 20-4.2-4.2" />
  </svg>
);
export const VolumeIcon = ({ size, className }: P) => (
  <svg {...base(size, className)}>
    <path d="M4 9.5v5h3.5L12 18.5v-13L7.5 9.5H4Z" />
    <path d="M15.5 9a4 4 0 0 1 0 6M18 6.5a7.5 7.5 0 0 1 0 11" />
  </svg>
);
export const MuteIcon = ({ size, className }: P) => (
  <svg {...base(size, className)}>
    <path d="M4 9.5v5h3.5L12 18.5v-13L7.5 9.5H4Z" />
    <path d="m16 10 4 4m0-4-4 4" />
  </svg>
);
export const Back10 = ({ size, className }: P) => (
  <svg {...base(size, className)}>
    <path d="M4 12a8 8 0 1 0 2.4-5.7M4 4v4.5h4.5" />
    <text x="12" y="15.5" fontSize="7.5" textAnchor="middle" fill="currentColor" stroke="none" fontFamily="inherit" fontWeight="600">
      10
    </text>
  </svg>
);
export const Fwd30 = ({ size, className }: P) => (
  <svg {...base(size, className)}>
    <path d="M20 12a8 8 0 1 1-2.4-5.7M20 4v4.5h-4.5" />
    <text x="12" y="15.5" fontSize="7.5" textAnchor="middle" fill="currentColor" stroke="none" fontFamily="inherit" fontWeight="600">
      30
    </text>
  </svg>
);
export const ExternalIcon = ({ size = 12, className }: P) => (
  <svg {...base(size, className)}>
    <path d="M14 5h5v5M19 5l-8 8M17 14v4a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V8a1 1 0 0 1 1-1h4" />
  </svg>
);
export const ClockIcon = ({ size = 14, className }: P) => (
  <svg {...base(size, className)}>
    <circle cx="12" cy="12" r="8" />
    <path d="M12 8v4.5l3 1.5" />
  </svg>
);
export const Spinner = ({ size = 16, className }: P) => (
  <svg {...base(size, className)} className={`spin ${className ?? ''}`}>
    <path d="M12 3a9 9 0 1 0 9 9" />
  </svg>
);
