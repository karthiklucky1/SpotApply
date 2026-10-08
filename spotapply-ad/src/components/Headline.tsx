import React from "react";
import { useCurrentFrame } from "remotion";
import { ease, prog } from "../anim";
import { COLORS, FONTS, LAYOUT, SAFE } from "../brand";

type Props = {
  readonly lines: readonly React.ReactNode[];
  readonly enterAt?: number;
  readonly exitAt?: number;
  readonly size?: number;
  readonly top?: number;
};

// One headline style for the whole ad: same position, same weight, lines
// rising out of a mask one after another. Exits lift and fade so the next
// headline lands on an empty slot.
export const Headline: React.FC<Props> = ({
  lines,
  enterAt = 4,
  exitAt,
  size = 104,
  top = LAYOUT.headlineTop,
}) => {
  const frame = useCurrentFrame();
  const out = exitAt === undefined ? 0 : prog(frame, exitAt, 8, ease.in);

  return (
    <div
      style={{
        position: "absolute",
        left: SAFE.x,
        top,
        width: SAFE.width,
        fontFamily: FONTS.sans,
        fontWeight: 800,
        fontSize: size,
        lineHeight: 1.04,
        letterSpacing: "-0.035em",
        color: COLORS.ink,
        opacity: 1 - out,
        translate: `0px ${-28 * out}px`,
      }}
    >
      {lines.map((line, i) => {
        const t = prog(frame, enterAt + i * 5, 20);
        return (
          <div
            key={i}
            style={{
              overflow: "hidden",
              paddingBottom: "0.14em",
              marginBottom: "-0.14em",
            }}
          >
            <div
              style={{
                whiteSpace: "nowrap",
                opacity: Math.min(1, t * 1.6),
                translate: `0px ${(1 - t) * 105}%`,
              }}
            >
              {line}
            </div>
          </div>
        );
      })}
    </div>
  );
};
