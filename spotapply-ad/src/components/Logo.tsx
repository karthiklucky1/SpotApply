import React, { useId } from "react";
import { COLORS, FONTS } from "../brand";

// The SpotApply mark, drawn from app/static/favicon.svg (same gradient,
// arrow and dot) so it stays vector-sharp at any size.
export const Mark: React.FC<{ readonly size: number }> = ({ size }) => {
  const id = useId().replace(/:/g, "");
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" style={{ flex: "none", display: "block" }}>
      <defs>
        <linearGradient id={`g${id}`} x1="0%" y1="0%" x2="100%" y2="100%">
          <stop offset="0%" stopColor={COLORS.accentSky} />
          <stop offset="100%" stopColor={COLORS.accent} />
        </linearGradient>
      </defs>
      <rect width="32" height="32" rx="8" fill={`url(#g${id})`} />
      <path
        d="M10 16h12M16 10l6 6-6 6"
        stroke="white"
        strokeWidth="2.5"
        strokeLinecap="round"
        strokeLinejoin="round"
        fill="none"
      />
      <circle cx="10" cy="16" r="2" fill="rgba(255,255,255,0.85)" />
    </svg>
  );
};

export const Wordmark: React.FC<{
  readonly size: number;
  readonly markSize?: number;
  readonly gap?: number;
  readonly color?: string;
}> = ({ size, markSize = size * 0.92, gap = size * 0.26, color = COLORS.ink }) => {
  return (
    <div style={{ display: "flex", alignItems: "center", gap }}>
      <Mark size={markSize} />
      <span
        style={{
          fontFamily: FONTS.sans,
          fontWeight: 700,
          fontSize: size,
          letterSpacing: "-0.03em",
          color,
          lineHeight: 1,
          // Outfit's caps sit a touch high against the mark.
          translate: `0px ${size * 0.02}px`,
        }}
      >
        SpotApply
      </span>
    </div>
  );
};
