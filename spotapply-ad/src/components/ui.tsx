import React from "react";
import { COLORS, FONTS, LAYOUT, SAFE, SHADOW } from "../brand";

/** The lettered company tile used on real board cards. */
export const InitialTile: React.FC<{ readonly letter: string; readonly size: number }> = ({ letter, size }) => (
  <div
    style={{
      width: size,
      height: size,
      borderRadius: size * 0.24,
      background: COLORS.surface2,
      border: `2px solid ${COLORS.border}`,
      display: "flex",
      alignItems: "center",
      justifyContent: "center",
      fontFamily: FONTS.sans,
      fontWeight: 600,
      fontSize: size * 0.44,
      color: COLORS.muted,
      flex: "none",
    }}
  >
    {letter}
  </div>
);

export const Eyebrow: React.FC<{
  readonly children: React.ReactNode;
  readonly color?: string;
  readonly size?: number;
  readonly style?: React.CSSProperties;
}> = ({ children, color = COLORS.muted, size = 25, style }) => (
  <div
    style={{
      fontFamily: FONTS.sans,
      fontWeight: 700,
      fontSize: size,
      letterSpacing: "0.1em",
      textTransform: "uppercase",
      color,
      ...style,
    }}
  >
    {children}
  </div>
);

/** The pill under the headline that says who or what the stage is about. */
export const ContextChip: React.FC<{
  readonly lead: React.ReactNode;
  readonly children: React.ReactNode;
  readonly style?: React.CSSProperties;
}> = ({ lead, children, style }) => (
  <div
    style={{
      position: "absolute",
      left: SAFE.x,
      top: LAYOUT.chipTop,
      height: LAYOUT.chipHeight,
      display: "flex",
      alignItems: "center",
      gap: 16,
      padding: "0 30px 0 14px",
      borderRadius: LAYOUT.chipHeight / 2,
      background: COLORS.canvas,
      border: `2px solid ${COLORS.border}`,
      boxShadow: SHADOW.soft,
      fontFamily: FONTS.sans,
      fontSize: 30,
      fontWeight: 600,
      color: COLORS.ink,
      whiteSpace: "nowrap",
      ...style,
    }}
  >
    {lead}
    {children}
  </div>
);

export const Avatar: React.FC<{ readonly initials: string; readonly size: number }> = ({ initials, size }) => (
  <div
    style={{
      width: size,
      height: size,
      borderRadius: size / 2,
      background: `linear-gradient(135deg, ${COLORS.accentSky}, ${COLORS.accent})`,
      color: "#FFFFFF",
      display: "flex",
      alignItems: "center",
      justifyContent: "center",
      fontFamily: FONTS.sans,
      fontWeight: 700,
      fontSize: size * 0.38,
      letterSpacing: "0.02em",
      flex: "none",
    }}
  >
    {initials}
  </div>
);

/** Browser chrome, like the shot frames on the landing page. */
export const BrowserBar: React.FC<{ readonly children?: React.ReactNode; readonly height?: number }> = ({
  children,
  height = 60,
}) => (
  <div
    style={{
      height,
      display: "flex",
      alignItems: "center",
      gap: 10,
      padding: "0 24px",
      background: COLORS.surface2,
      borderBottom: `2px solid ${COLORS.border}`,
      flex: "none",
    }}
  >
    {[0, 1, 2].map((i) => (
      <span key={i} style={{ width: 14, height: 14, borderRadius: 7, background: COLORS.borderStrong }} />
    ))}
    <span
      style={{
        marginLeft: 14,
        fontFamily: FONTS.mono,
        fontSize: 24,
        color: COLORS.muted,
        whiteSpace: "nowrap",
      }}
    >
      {children}
    </span>
  </div>
);
