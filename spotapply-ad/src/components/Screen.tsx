import React from "react";
import { Img, staticFile } from "remotion";
import { COLORS, SAFE, SHADOW } from "../brand";
import { BrowserBar } from "./ui";

// A real capture of the running app (public/live/*.png: the dashboard at a
// 430 px phone viewport, 2.5x, so 1075 px wide), shown in a browser frame.
// Geometry is in SOURCE pixels so overlays can point at real UI elements.

export const SOURCE_W = 1075;
export const BAR_H = 52;

export type Slice = { readonly y: number; readonly h: number };

export type ScreenGeom = {
  readonly x: number;
  readonly y: number;
  readonly w: number;
  /** Source rows shown, top to bottom (one slice, or several stacked). */
  readonly slices: readonly Slice[];
};

export const scaleOf = (g: ScreenGeom) => g.w / SOURCE_W;
export const heightOf = (g: ScreenGeom) =>
  BAR_H + g.slices.reduce((a, s) => a + s.h, 0) * scaleOf(g) + 4;

/** Where a source-pixel rect inside slice `slice` lands on the canvas. */
export const toCanvas = (
  g: ScreenGeom,
  r: { x: number; y: number; w: number; h: number },
  slice = 0,
) => {
  const s = scaleOf(g);
  const above = g.slices.slice(0, slice).reduce((a, sl) => a + sl.h, 0);
  return {
    x: g.x + 2 + r.x * s,
    y: g.y + 2 + BAR_H + (above + r.y - g.slices[slice].y) * s,
    w: r.w * s,
    h: r.h * s,
  };
};

export const Screen: React.FC<{
  readonly src: string;
  readonly geom: ScreenGeom;
  readonly url?: string;
  readonly style?: React.CSSProperties;
}> = ({ src, geom, url = "app.spotapply.ai/dashboard", style }) => {
  const s = scaleOf(geom);
  return (
    <div
      style={{
        position: "absolute",
        left: geom.x,
        top: geom.y,
        width: geom.w,
        boxSizing: "content-box",
        borderRadius: 30,
        overflow: "hidden",
        background: COLORS.canvas,
        border: `2px solid ${COLORS.border}`,
        boxShadow: SHADOW.card,
        ...style,
      }}
    >
      <BrowserBar height={BAR_H}>{url}</BrowserBar>
      {geom.slices.map((sl, i) => (
        <div
          key={i}
          style={{
            position: "relative",
            height: sl.h * s,
            overflow: "hidden",
            borderTop: i > 0 ? `2px solid ${COLORS.border}` : undefined,
          }}
        >
          <Img
            src={staticFile(src)}
            style={{ position: "absolute", left: 0, top: -sl.y * s, width: SOURCE_W * s, display: "block" }}
          />
        </div>
      ))}
    </div>
  );
};

/** A blue focus ring around a real UI element. */
export const Ring: React.FC<{
  readonly rect: { x: number; y: number; w: number; h: number };
  readonly t: number;
  readonly radius?: number;
}> = ({ rect, t, radius = 24 }) => {
  if (t <= 0) return null;
  const pad = 8;
  return (
    <div
      style={{
        position: "absolute",
        left: rect.x - pad,
        top: rect.y - pad,
        width: rect.w + 2 * pad,
        height: rect.h + 2 * pad,
        borderRadius: radius,
        border: `6px solid ${COLORS.accent}`,
        boxShadow: `0 0 0 ${10 * t}px rgba(14,165,233,0.16)`,
        opacity: Math.min(1, t * 1.5),
        scale: String(1.04 - 0.04 * t),
        pointerEvents: "none",
      }}
    />
  );
};

/** Clips its children to the product stage, so a drawer sliding up enters
 * from the stage's bottom edge instead of crossing the demo label and the
 * platform's bottom overlay zone. */
export const StageClip: React.FC<{ readonly children: React.ReactNode }> = ({ children }) => (
  <div style={{ position: "absolute", inset: 0, clipPath: "inset(586px 0 384px 0)" }}>{children}</div>
);

/** The standard stage placement for a full-width screen. */
export const stageGeom = (slices: readonly Slice[], w: number = SAFE.width, y = 604): ScreenGeom => ({
  x: (1080 - w) / 2 - 2,
  y,
  w,
  slices,
});
