import React from "react";

type IconProps = { readonly size: number; readonly color: string; readonly stroke?: number };

const base = (size: number): React.SVGProps<SVGSVGElement> => ({
  width: size,
  height: size,
  viewBox: "0 0 24 24",
  fill: "none",
  style: { flex: "none", display: "block" },
});

export const CheckIcon: React.FC<IconProps> = ({ size, color, stroke = 3 }) => (
  <svg {...base(size)}>
    <path d="m5 13 4 4L19 7" stroke={color} strokeWidth={stroke} strokeLinecap="round" strokeLinejoin="round" />
  </svg>
);

export const LockIcon: React.FC<IconProps> = ({ size, color, stroke = 2.2 }) => (
  <svg {...base(size)}>
    <rect x="5" y="11" width="14" height="9.5" rx="2.4" stroke={color} strokeWidth={stroke} />
    <path d="M8.5 11V8a3.5 3.5 0 0 1 7 0v3" stroke={color} strokeWidth={stroke} strokeLinecap="round" />
  </svg>
);

export const PinIcon: React.FC<IconProps> = ({ size, color, stroke = 2 }) => (
  <svg {...base(size)}>
    <path d="M12 21s-6.5-5.7-6.5-11a6.5 6.5 0 0 1 13 0c0 5.3-6.5 11-6.5 11Z" stroke={color} strokeWidth={stroke} strokeLinejoin="round" />
    <circle cx="12" cy="10" r="2.3" stroke={color} strokeWidth={stroke} />
  </svg>
);

export const DocIcon: React.FC<IconProps> = ({ size, color, stroke = 2 }) => (
  <svg {...base(size)}>
    <path d="M7 3h7l5 5v12a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Z" stroke={color} strokeWidth={stroke} strokeLinejoin="round" />
    <path d="M14 3v5h5M9 13h6M9 17h6" stroke={color} strokeWidth={stroke} strokeLinecap="round" strokeLinejoin="round" />
  </svg>
);

export const SparkIcon: React.FC<IconProps> = ({ size, color }) => (
  <svg {...base(size)}>
    <path d="M12 2.5c.6 4.9 2.6 6.9 7.5 7.5-4.9.6-6.9 2.6-7.5 7.5-.6-4.9-2.6-6.9-7.5-7.5 4.9-.6 6.9-2.6 7.5-7.5Z" fill={color} transform="translate(0 2)" />
  </svg>
);

export const ArrowRightIcon: React.FC<IconProps> = ({ size, color, stroke = 2.6 }) => (
  <svg {...base(size)}>
    <path d="M5 12h14m-6-6 6 6-6 6" stroke={color} strokeWidth={stroke} strokeLinecap="round" strokeLinejoin="round" />
  </svg>
);

/** A filled circle with a check — the product's "verified / reviewed" mark. */
export const CheckBadge: React.FC<{ readonly size: number; readonly bg: string; readonly fg?: string }> = ({
  size,
  bg,
  fg = "#FFFFFF",
}) => (
  <div
    style={{
      width: size,
      height: size,
      borderRadius: size / 2,
      background: bg,
      display: "flex",
      alignItems: "center",
      justifyContent: "center",
      flex: "none",
    }}
  >
    <CheckIcon size={size * 0.6} color={fg} stroke={3.2} />
  </div>
);
