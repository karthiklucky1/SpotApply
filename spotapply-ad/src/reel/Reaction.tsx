import React from "react";
import { spring, useCurrentFrame, useVideoConfig } from "remotion";
import { blinkAt, type Expr, Person } from "./Character";
import { PIP } from "./layout";

// The reaction bubble: the job seeker watching the product, in the corner of
// the screen card, like a reaction cam. `beats` switch the expression.
export const Reaction: React.FC<{
  readonly at: number;
  readonly outAt: number;
  readonly beats: readonly (readonly [frame: number, expr: Expr])[];
  readonly bg?: string;
  readonly id: string;
}> = ({ at, outAt, beats, bg = "#BFE4FA", id }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  if (frame < at || frame >= outAt) return null;
  const inS = spring({ frame: frame - at, fps, config: { damping: 12, stiffness: 180, mass: 0.7 } });
  const outS = Math.min(1, Math.max(0, (outAt - frame) / 6));
  let expr: Expr = beats[0][1];
  let since = at;
  for (const [f, e] of beats) {
    if (frame >= f) {
      expr = e;
      since = f;
    }
  }
  // A small "take" when the expression changes.
  const take = spring({ frame: frame - since, fps, config: { damping: 9, stiffness: 220, mass: 0.5 } });
  const d = PIP.r * 2;
  return (
    <div
      style={{
        position: "absolute",
        left: PIP.cx - PIP.r,
        top: PIP.cy - PIP.r,
        width: d,
        height: d,
        borderRadius: "50%",
        overflow: "hidden",
        background: bg,
        border: "8px solid #FFFFFF",
        boxSizing: "border-box",
        boxShadow: "0 24px 50px -18px rgba(8,32,47,0.6)",
        scale: String(inS * outS * (1 + 0.05 * (1 - take))),
        rotate: `${(1 - take) * 4}deg`,
      }}
    >
      <div style={{ position: "absolute", left: d / 2 - 158, top: d / 2 - 150, width: 316, height: 316 }}>
        <Person expr={expr} size={316} blink={blinkAt(frame, 41)} id={id} />
      </div>
    </div>
  );
};
