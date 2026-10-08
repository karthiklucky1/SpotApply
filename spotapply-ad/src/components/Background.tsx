import React from "react";
import { AbsoluteFill } from "remotion";
import { COLORS } from "../brand";

// Static on purpose: the end card must hold perfectly still, and a drifting
// backdrop behind readable UI only competes with it.
export const Background: React.FC = () => {
  return (
    <AbsoluteFill
      style={{
        background: `linear-gradient(180deg, ${COLORS.canvas} 0%, ${COLORS.surface} 46%, ${COLORS.surface2} 100%)`,
      }}
    >
      <AbsoluteFill
        style={{
          background:
            "radial-gradient(900px 760px at 100% 0%, rgba(14,165,233,0.13), transparent 70%)",
        }}
      />
      <AbsoluteFill
        style={{
          background:
            "radial-gradient(860px 860px at 0% 100%, rgba(0,119,194,0.10), transparent 70%)",
        }}
      />
      <AbsoluteFill
        style={{
          backgroundImage: `radial-gradient(${COLORS.borderStrong} 1.6px, transparent 1.7px)`,
          backgroundSize: "40px 40px",
          opacity: 0.32,
          maskImage:
            "linear-gradient(180deg, transparent 0%, black 22%, black 78%, transparent 100%)",
        }}
      />
    </AbsoluteFill>
  );
};
