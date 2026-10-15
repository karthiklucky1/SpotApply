import React from "react";
import { spring, useCurrentFrame, useVideoConfig } from "remotion";
import { COLORS, FONTS } from "../brand";
import { CAPTION } from "./layout";
import { CAPTION_WORDS, type CapWord } from "./timeline";

// Word-by-word captions: two or three words at a time, the word being said
// lit in sky blue. Timings are the forced alignment of the actual narration
// (voiceover/align_reel.py), so a word lights when it is heard.

// Lines whose words are already on screen as the scene's own text.
const ON_SCREEN = new Set(["r_less", "r_cta"]);

type Chunk = { words: CapWord[]; from: number; to: number };

const CHUNKS: Chunk[] = (() => {
  const out: Chunk[] = [];
  let cur: CapWord[] = [];
  const flush = () => {
    if (cur.length) out.push({ words: cur, from: cur[0].s - 3, to: cur[cur.length - 1].e + 12 });
    cur = [];
  };
  for (const w of CAPTION_WORDS) {
    if (ON_SCREEN.has(w.line)) continue;
    if (cur.length && cur[0].line !== w.line) flush();
    cur.push(w);
    const chars = cur.map((x) => x.w).join(" ").length;
    if (/[.,:;?!…]$/.test(w.w) || cur.length >= 3 || chars >= 15) flush();
  }
  flush();
  // A chunk gives way to the next one.
  for (let i = 0; i + 1 < out.length; i++) out[i].to = Math.min(out[i].to, out[i + 1].from);
  return out;
})();

export const Captions: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const chunk = CHUNKS.find((c) => frame >= c.from && frame < c.to);
  if (!chunk) return null;
  const pop = spring({ frame: frame - chunk.from, fps, config: { damping: 14, stiffness: 260, mass: 0.6 } });
  return (
    <div
      style={{
        position: "absolute",
        left: CAPTION.cx - CAPTION.maxW / 2,
        top: CAPTION.y,
        width: CAPTION.maxW,
        display: "flex",
        flexWrap: "wrap",
        justifyContent: "center",
        columnGap: 18,
        rowGap: 2,
        scale: String(0.86 + 0.14 * pop),
        fontFamily: FONTS.sans,
        fontWeight: 800,
        fontSize: CAPTION.size,
        lineHeight: 1.12,
        letterSpacing: "-0.01em",
      }}
    >
      {chunk.words.map((w, i) => {
        const on = frame >= w.s && frame < Math.max(w.e, w.s + 4);
        const said = frame >= w.s;
        const wp = spring({ frame: frame - w.s, fps, config: { damping: 12, stiffness: 300, mass: 0.5 } });
        return (
          <span
            key={i}
            style={{
              color: on ? COLORS.skyOnDark : "#FFFFFF",
              opacity: said ? 1 : 0.92,
              WebkitTextStroke: `13px ${COLORS.inkDeep}`,
              paintOrder: "stroke fill",
              textShadow: "0 8px 22px rgba(8,32,47,0.45)",
              display: "inline-block",
              scale: on ? String(1.04 + 0.1 * Math.max(0, 1 - wp)) : "1",
            }}
          >
            {w.w}
          </span>
        );
      })}
    </div>
  );
};
