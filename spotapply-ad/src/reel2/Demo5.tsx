import React from "react";
import { Audio } from "@remotion/media";
import { AbsoluteFill, Img, interpolate, OffthreadVideo, Sequence, staticFile, useCurrentFrame, useVideoConfig } from "remotion";
import { ease, prog } from "../anim";
import { COLORS, FONTS } from "../brand";
import { Mark } from "../components/Logo";
import "../fonts";
import words from "./words.json";

// 5-second tests for the reel's new direction: the REAL app as large as the
// frame allows (1080 x 1440 = 75% of 1080 x 1920), clean cuts, a gentle zoom
// that follows the action, one short headline, one caption track, a thin
// highlight instead of a scribble. `withPhoto` opens on the owner's own photo
// (public/reel2/person.png, kept out of git) for a real human moment.

export type Demo5Props = { readonly withPhoto: boolean };

const FPS = 30;
const PRODUCT = { x: 0, y: 260, w: 1080, h: 1440 } as const; // 75.0% of the frame
const SRC_W = 1076; // phone recording, device pixels (430 CSS px at 2.5x)
const INK = "#08202F";

type Focus = { x: number; y: number; w: number };
type Seg = { from: number; frames: number; cam: readonly [number, Focus][] };

const cam = (f: number, keys: readonly [number, Focus][]): Focus => {
  if (f <= keys[0][0]) return keys[0][1];
  for (let i = 1; i < keys.length; i++) {
    if (f <= keys[i][0]) {
      const [f0, a] = keys[i - 1];
      const [f1, b] = keys[i];
      const t = ease.inOut((f - f0) / Math.max(1, f1 - f0));
      return { x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t, w: a.w + (b.w - a.w) * t };
    }
  }
  return keys[keys.length - 1][1];
};

const BOARD: Focus = { x: 0, y: 228, w: SRC_W }; // the stage tabs whole at the top edge
const DRAWER: Focus = { x: 0, y: 0, w: SRC_W };
const REPORT_CLOSE: Focus = { x: 26, y: 278, w: 1024 }; // starts under the drawer's buttons
// The 94 and "Strong fit for your resume", in the recording's pixels.
const SCORE = { x: 46, y: 466, w: 984, h: 206 };

const SegVideo: React.FC<{ readonly seg: Seg }> = ({ seg }) => {
  const frame = useCurrentFrame();
  const f = cam(frame, seg.cam);
  const s = PRODUCT.w / f.w;
  return (
    <OffthreadVideo
      src={staticFile("reel/footage/phone_match.mp4")}
      trimBefore={Math.round(seg.from * FPS)}
      muted
      style={{ position: "absolute", left: -f.x * s, top: -f.y * s, width: SRC_W * s, height: 2330 * s, maxWidth: "none" }}
    />
  );
};

/** The app, full width, in its 75% window. Segments play back to back. */
const Product: React.FC<{ readonly segs: readonly Seg[]; readonly highlight: { seg: number; at: number } }> = ({ segs, highlight }) => {
  const frame = useCurrentFrame();
  let at = 0;
  const starts = segs.map((s) => {
    const v = at;
    at += s.frames;
    return v;
  });
  const hs = starts[highlight.seg];
  const local = frame - hs;
  const f = cam(local, segs[highlight.seg].cam);
  const sc = PRODUCT.w / f.w;
  const on = frame >= hs + highlight.at ? prog(local, highlight.at, 9) : 0;
  return (
    <div style={{ position: "absolute", left: PRODUCT.x, top: PRODUCT.y, width: PRODUCT.w, height: PRODUCT.h, overflow: "hidden", background: "#EEF5FC" }}>
      {segs.map((s, i) => (
        <Sequence key={i} from={starts[i]} durationInFrames={s.frames} layout="none">
          <SegVideo seg={s} />
        </Sequence>
      ))}
      {on > 0 ? (
        <div
          style={{
            position: "absolute",
            left: (SCORE.x - f.x) * sc,
            top: (SCORE.y - f.y) * sc,
            width: SCORE.w * sc,
            height: SCORE.h * sc,
            borderRadius: 22,
            border: `3px solid ${COLORS.accentSky}`,
            boxShadow: "0 0 0 7px rgba(14,165,233,0.16)",
            opacity: on,
            boxSizing: "border-box",
          }}
        />
      ) : null}
    </div>
  );
};

type W = { w: string; s: number; e: number };
const WORDS = words as Record<string, { words: W[] }>;

/** Phrase captions for the lines placed at `lines` (id -> start frame). */
const Captions: React.FC<{ readonly lines: Record<string, number>; readonly y: number }> = ({ lines, y }) => {
  const frame = useCurrentFrame();
  const cues: { text: string; from: number; to: number }[] = [];
  for (const [id, start] of Object.entries(lines)) {
    let cur: W[] = [];
    const flush = () => {
      if (cur.length) cues.push({ text: cur.map((c) => c.w).join(" "), from: start + Math.round(cur[0].s * FPS) - 2, to: start + Math.round(cur[cur.length - 1].e * FPS) + 8 });
      cur = [];
    };
    for (const w of WORDS[id].words) {
      cur.push(w);
      if (/[.,:;?!…]$/.test(w.w) || cur.length >= 4) flush();
    }
    flush();
  }
  for (let i = 0; i + 1 < cues.length; i++) cues[i].to = Math.min(cues[i].to, cues[i + 1].from);
  const cue = cues.find((c) => frame >= c.from && frame < c.to);
  if (!cue) return null;
  return (
    <div
      style={{
        position: "absolute",
        left: 60,
        right: 60,
        top: y,
        textAlign: "center",
        fontFamily: FONTS.sans,
        fontWeight: 600,
        fontSize: 50,
        lineHeight: 1.18,
        color: "#FFFFFF",
        textShadow: "0 2px 12px rgba(0,0,0,0.35)",
        opacity: prog(frame, cue.from, 3),
      }}
    >
      {cue.text}
    </div>
  );
};

const Bands: React.FC<{ readonly headline: string }> = ({ headline }) => (
  <>
    <div style={{ position: "absolute", left: 0, top: 0, width: 1080, height: PRODUCT.y, background: INK }}>
      <div style={{ position: "absolute", left: 60, bottom: 46, display: "flex", alignItems: "center", gap: 18 }}>
        <Mark size={46} />
        <span style={{ fontFamily: FONTS.sans, fontWeight: 700, fontSize: 48, letterSpacing: "-0.02em", color: "#FFFFFF" }}>{headline}</span>
      </div>
      <span style={{ position: "absolute", right: 60, bottom: 56, fontFamily: FONTS.sans, fontWeight: 500, fontSize: 22, color: "rgba(255,255,255,0.6)" }}>Demo data</span>
    </div>
    <div style={{ position: "absolute", left: 0, top: PRODUCT.y + PRODUCT.h, width: 1080, height: 1920 - PRODUCT.y - PRODUCT.h, background: INK }} />
  </>
);

const Photo: React.FC = () => {
  const frame = useCurrentFrame();
  const { durationInFrames } = useVideoConfig();
  // A slow push-in: the photo itself, never altered or animated.
  const z = interpolate(frame, [0, durationInFrames], [1.0, 1.04], { extrapolateRight: "clamp" });
  return (
    <AbsoluteFill style={{ background: "#D8D0C4", overflow: "hidden" }}>
      <Img src={staticFile("reel2/person.png")} style={{ width: 1080, height: 1919, objectFit: "cover", scale: String(z), transformOrigin: "50% 42%" }} />
      <div style={{ position: "absolute", left: 0, right: 0, bottom: 0, height: 420, background: "linear-gradient(180deg, rgba(8,32,47,0) 0%, rgba(8,32,47,0.55) 100%)" }} />
    </AbsoluteFill>
  );
};

const PHOTO_FRAMES = 66;

export const Demo5: React.FC<Demo5Props> = ({ withPhoto }) => {
  const segs: Seg[] = withPhoto
    ? [
        { from: 4.3, frames: 24, cam: [[0, BOARD], [10, BOARD], [20, DRAWER]] },
        { from: 7.35, frames: 60, cam: [[0, DRAWER], [8, DRAWER], [60, REPORT_CLOSE]] },
      ]
    : [
        { from: 2.9, frames: 59, cam: [[0, BOARD]] },
        { from: 4.85, frames: 20, cam: [[0, BOARD], [14, DRAWER]] },
        { from: 7.35, frames: 71, cam: [[0, DRAWER], [8, DRAWER], [71, REPORT_CLOSE]] },
      ];
  // "Tap a job" lands on the recorded tap (footage 4.57 s).
  const lines: Record<string, number> = withPhoto ? { d_hook: 3 } : { d_why: 42 };
  const productFrom = withPhoto ? PHOTO_FRAMES : 0;
  const tapAt = productFrom + (withPhoto ? 8 : 50); // the recorded tap (footage 4.57 s)
  return (
    <AbsoluteFill style={{ background: INK }}>
      {withPhoto ? (
        <Sequence durationInFrames={PHOTO_FRAMES}>
          <Photo />
        </Sequence>
      ) : null}
      <Sequence from={productFrom}>
        <Bands headline="Every match, explained." />
        <Product segs={segs} highlight={{ seg: segs.length - 1, at: 10 }} />
      </Sequence>
      <Captions lines={lines} y={PRODUCT.y + PRODUCT.h + 52} />
      {Object.entries(lines).map(([id, at]) => (
        <Sequence key={id} from={at}>
          <Audio src={staticFile(`reel2/vo/${id}.wav`)} />
        </Sequence>
      ))}
      <Sequence from={tapAt} durationInFrames={20}>
        <Audio src={staticFile("reel/sfx/tap.wav")} volume={0.5} />
      </Sequence>
      <Audio
        src={staticFile("reel/music.wav")}
        trimBefore={20 * FPS}
        volume={(f) => 0.09 * Math.min(1, f / 10) * Math.min(1, (150 - f) / 10)}
      />
    </AbsoluteFill>
  );
};
