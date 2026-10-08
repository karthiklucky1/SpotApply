import React from "react";
import { COLORS, FONTS, LAYOUT } from "../brand";
import { COPY } from "../copy";

// Shown under every illustrated product scene: the people, companies and
// scores on screen are fictional demo data, not a customer outcome.
export const DemoLabel: React.FC<{ readonly opacity: number }> = ({ opacity }) => {
  if (opacity <= 0) return null;
  return (
    <div
      style={{
        position: "absolute",
        left: 0,
        right: 0,
        top: LAYOUT.labelTop,
        display: "flex",
        justifyContent: "center",
        opacity,
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 12,
          padding: "10px 22px",
          borderRadius: 999,
          background: "rgba(255,255,255,0.82)",
          border: `2px solid ${COLORS.border}`,
          fontFamily: FONTS.sans,
          fontWeight: 600,
          fontSize: 26,
          color: COLORS.muted,
          whiteSpace: "nowrap",
        }}
      >
        <span style={{ width: 10, height: 10, borderRadius: 5, background: COLORS.borderStrong }} />
        {COPY.demoLabel}
      </div>
    </div>
  );
};
