import { Easing, interpolate } from "remotion";
import { EASE } from "./brand";

export const ease = {
  out: Easing.bezier(...EASE.out),
  inOut: Easing.bezier(...EASE.inOut),
  in: Easing.bezier(...EASE.in),
};

const CLAMP = { extrapolateLeft: "clamp", extrapolateRight: "clamp" } as const;

/** 0 → 1 over [start, start + duration], clamped, eased. */
export const prog = (
  frame: number,
  start: number,
  duration: number,
  easing: (t: number) => number = ease.out,
): number =>
  interpolate(frame, [start, start + duration], [0, 1], { ...CLAMP, easing });

export const lerp = (a: number, b: number, t: number): number => a + (b - a) * t;

/** Characters of `text` revealed by `frame`, typing at `cps` chars/frame. */
export const typed = (
  text: string,
  frame: number,
  start: number,
  charsPerFrame: number,
): string => {
  const n = Math.floor(Math.max(0, frame - start) * charsPerFrame);
  return text.slice(0, Math.min(text.length, n));
};

/** A rectangle moving between two placements. */
export type Rect = { x: number; y: number; w: number; h: number };
export const lerpRect = (a: Rect, b: Rect, t: number): Rect => ({
  x: lerp(a.x, b.x, t),
  y: lerp(a.y, b.y, t),
  w: lerp(a.w, b.w, t),
  h: lerp(a.h, b.h, t),
});
