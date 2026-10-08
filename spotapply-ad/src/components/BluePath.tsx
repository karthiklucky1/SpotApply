import React, { useId } from "react";
import { getLength, getPointAtLength } from "@remotion/paths";
import { COLORS, VIDEO } from "../brand";

// The ad's recurring visual cue: a thin blue thread that draws from one
// step to the next, carrying the eye to whatever the scene explains.
// Coordinates are in full-frame pixels.
export const BluePath: React.FC<{
  readonly d: string;
  readonly progress: number;
  readonly opacity?: number;
  readonly stroke?: number;
  readonly dashed?: boolean;
  readonly head?: boolean;
}> = ({ d, progress, opacity = 1, stroke = 5, dashed = false, head = true }) => {
  const id = `bp${useId().replace(/:/g, "")}`;
  if (progress <= 0 || opacity <= 0) return null;
  const length = getLength(d);
  const p = getPointAtLength(d, length * Math.min(1, progress));
  return (
    <svg
      width={VIDEO.width}
      height={VIDEO.height}
      viewBox={`0 0 ${VIDEO.width} ${VIDEO.height}`}
      style={{ position: "absolute", left: 0, top: 0, opacity, pointerEvents: "none" }}
    >
      <defs>
        <mask id={id}>
          <path
            d={d}
            stroke="white"
            strokeWidth={stroke + 4}
            fill="none"
            strokeLinecap="round"
            strokeDasharray={`${length} ${length}`}
            strokeDashoffset={length * (1 - Math.min(1, progress))}
          />
        </mask>
      </defs>
      <path
        d={d}
        stroke={COLORS.accentLine}
        strokeWidth={stroke + 10}
        strokeOpacity={0.35}
        fill="none"
        strokeLinecap="round"
        mask={`url(#${id})`}
      />
      <path
        d={d}
        stroke={COLORS.accent}
        strokeWidth={stroke}
        fill="none"
        strokeLinecap="round"
        strokeDasharray={dashed ? `${stroke * 0.1} ${stroke * 3}` : undefined}
        mask={`url(#${id})`}
      />
      {head && p && progress < 1.0 ? (
        <>
          <circle cx={p.x} cy={p.y} r={stroke * 3.4} fill={COLORS.accentSky} opacity={0.18} />
          <circle cx={p.x} cy={p.y} r={stroke * 1.5} fill={COLORS.accent} />
        </>
      ) : null}
    </svg>
  );
};
