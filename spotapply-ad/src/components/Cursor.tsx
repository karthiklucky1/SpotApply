import React from "react";
import { COLORS, FONTS } from "../brand";

// The HUMAN's pointer. It is the only cursor in the ad: everything
// SpotApply does is shown as fields lighting up on their own, so the one
// visible click on Submit is unmistakably the person's.
export const Cursor: React.FC<{
  readonly x: number;
  readonly y: number;
  readonly opacity?: number;
  /** 0 = up, 1 = fully pressed */
  readonly press?: number;
  /** 0..1 progress of the click ripple */
  readonly ripple?: number;
  readonly label?: string;
}> = ({ x, y, opacity = 1, press = 0, ripple = 0, label = "You" }) => {
  if (opacity <= 0) return null;
  const scale = 1 - 0.12 * press;
  return (
    <div style={{ position: "absolute", left: 0, top: 0, opacity, pointerEvents: "none" }}>
      {ripple > 0 && ripple < 1 ? (
        <div
          style={{
            position: "absolute",
            left: x - 70 * ripple,
            top: y - 70 * ripple,
            width: 140 * ripple,
            height: 140 * ripple,
            borderRadius: "50%",
            border: `5px solid ${COLORS.accent}`,
            opacity: 1 - ripple,
          }}
        />
      ) : null}
      <svg
        width={64}
        height={80}
        viewBox="0 0 32 40"
        style={{
          position: "absolute",
          left: x - 4,
          top: y - 2,
          scale: String(scale),
          transformOrigin: "4px 2px",
          filter: "drop-shadow(0 8px 14px rgba(12,42,62,0.35))",
        }}
      >
        <path
          d="M2 1.5v29.5l7.6-7.2 4.9 11.4 5.6-2.4-4.9-11.2H25.5Z"
          fill={COLORS.ink}
          stroke="#FFFFFF"
          strokeWidth={2.2}
          strokeLinejoin="round"
        />
      </svg>
      <div
        style={{
          position: "absolute",
          left: x + 50,
          top: y + 58,
          padding: "8px 20px",
          borderRadius: 999,
          background: COLORS.ink,
          color: "#FFFFFF",
          fontFamily: FONTS.sans,
          fontWeight: 700,
          fontSize: 28,
          whiteSpace: "nowrap",
          boxShadow: "0 10px 24px rgba(12,42,62,0.25)",
        }}
      >
        {label}
      </div>
    </div>
  );
};
