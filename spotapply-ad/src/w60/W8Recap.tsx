import React from "react";
import { AbsoluteFill, useCurrentFrame } from "remotion";
import { prog } from "../anim";
import { COLORS, FONTS } from "../brand";
import { CHAPTERS } from "./timeline60";
import { TrackFinalWindow } from "./W7Track";

// 51-54s. The five chapters, read back one word at a time with the voice
// ("Find. Understand. Prepare. Review. Track."), on a clean white page.

// When each word lands (local frames), matched to the narration.
const AT = [6, 22, 40, 56, 70] as const;

export const RecapScene60: React.FC = () => {
  const frame = useCurrentFrame();
  const clear = prog(frame, 0, 12);

  return (
    <AbsoluteFill>
      {clear < 1 ? <TrackFinalWindow style={{ opacity: 1 - clear }} /> : null}
      <AbsoluteFill style={{ background: COLORS.canvas, opacity: clear }} />
      <AbsoluteFill style={{ display: "flex", alignItems: "center", justifyContent: "center", fontFamily: FONTS.sans }}>
        <div style={{ display: "flex", gap: 44, alignItems: "baseline", translate: "0px -40px" }}>
          {CHAPTERS.map((c, i) => {
            const t = prog(frame, AT[i], 14);
            return (
              <div key={c.n} style={{ textAlign: "left", opacity: t, translate: `0px ${(1 - t) * 40}px` }}>
                <div style={{ fontFamily: FONTS.serif, fontStyle: "italic", fontSize: 64, color: COLORS.accent, lineHeight: 1 }}>{c.n}</div>
                <div style={{ fontWeight: 800, fontSize: 70, letterSpacing: "-0.035em", color: COLORS.ink, marginTop: 6 }}>
                  {c.label}.
                </div>
              </div>
            );
          })}
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};
