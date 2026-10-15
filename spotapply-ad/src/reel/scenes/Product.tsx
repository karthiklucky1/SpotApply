import React from "react";
import { AbsoluteFill, useCurrentFrame } from "remotion";
import { prog } from "../../anim";
import { COLORS } from "../../brand";
import { CARD, STICKER } from "../layout";
import { Reaction } from "../Reaction";
import { DemoTag, Headline, Marker, ScreenCard, type Seg, Sticker, toCanvas, Track, trackAt } from "../ui";

// 14.4-49.6 s. LIVE: real screen recordings of the app (fictional demo
// account) and of the Chrome extension filling a form. Recordings play at
// their own speed; a `freeze` holds one frame so a screen can be read; the
// one time cut (the studio's wait for the model) is labelled "sped up".

export const ProductBg: React.FC = () => {
  const frame = useCurrentFrame();
  return (
    <AbsoluteFill style={{ background: `linear-gradient(180deg, #F2F9FF 0%, ${COLORS.accentTint} 100%)`, overflow: "hidden" }}>
      <div style={{ position: "absolute", left: -160, top: 120 + 20 * Math.sin(frame / 40), width: 620, height: 620, borderRadius: "50%", background: "#7DD3FC", opacity: 0.32, filter: "blur(80px)" }} />
      <div style={{ position: "absolute", right: -200, top: 1180 + 20 * Math.cos(frame / 46), width: 700, height: 700, borderRadius: "50%", background: COLORS.accent, opacity: 0.16, filter: "blur(90px)" }} />
      <svg width={1080} height={1920} style={{ position: "absolute", inset: 0, opacity: 0.5 }}>
        <defs>
          <pattern id="dots" width={36} height={36} patternUnits="userSpaceOnUse">
            <circle cx={2} cy={2} r={2} fill="#BAE0F8" />
          </pattern>
        </defs>
        <rect width={1080} height={1920} fill="url(#dots)" />
      </svg>
    </AbsoluteFill>
  );
};

/** Marker on a source rect of segment `i`, while that segment plays. */
const SegMarker: React.FC<{
  readonly segs: readonly Seg[];
  readonly i: number;
  readonly rect: { x: number; y: number; w: number; h: number };
  readonly from: number; // local frame within the segment
  readonly to: number;
  readonly seed?: number;
  readonly pad?: number;
}> = ({ segs, i, rect, from, to, seed, pad }) => {
  const frame = useCurrentFrame();
  const t = trackAt(segs, frame);
  if (t.i !== i) return null;
  return (
    <Marker
      rect={toCanvas(t.focus, rect)}
      t={prog(t.local, from, 12)}
      out={prog(t.local, to - 6, 6)}
      seed={seed}
      pad={pad}
    />
  );
};

const segStart = (segs: readonly Seg[], i: number) => segs.slice(0, i).reduce((n, s) => n + s.frames, 0);

// ── find: the board ─────────────────────────────────────────────────────────
const FIND: Seg[] = [
  { src: "phone_match", from: 0.2, frames: 128, cam: [[0, { x: 0, y: 0, w: 1076 }], [24, { x: 0, y: 0, w: 1076 }], [72, { x: 0, y: 290, w: 1076 }]] },
  { src: "phone_match", from: 4.47, frames: 64, freeze: true, cam: [[0, { x: 0, y: 290, w: 1076 }], [44, { x: 30, y: 330, w: 1000 }]] },
];

export const FindScene: React.FC = () => {
  const frame = useCurrentFrame();
  return (
    <AbsoluteFill>
      <ProductBg />
      <ScreenCard kind="phone" label="app.spotapply.ai" enter={prog(frame, 0, 12)}>
        <Track segs={FIND} />
      </ScreenCard>
      {/* The loop rides high enough that its arc clears the "94% · STRONG MATCH" pill. */}
      <SegMarker segs={FIND} i={1} rect={{ x: 70, y: 516, w: 850, h: 296 }} from={-20} to={64} seed={3} />
      <SegMarker segs={FIND} i={0} rect={{ x: 70, y: 516, w: 850, h: 296 }} from={86} to={128} seed={3} />
      <Sticker text="from company career pages" at={100} outAt={186} x={STICKER.x} y={STICKER.y} size={32} tone="ink" rotate={-3} />
      <Headline lines={["Find roles that fit", "*your background.*"]} at={6} />
      <Reaction at={14} outAt={192} beats={[[14, "curious"], [96, "impressed"]]} id="pip-find" />
      <DemoTag opacity={1} />
    </AbsoluteFill>
  );
};

// ── why: the match report, the fit breakdown, the gaps ──────────────────────
const WHY: Seg[] = [
  {
    src: "phone_match",
    from: 4.45,
    frames: 164,
    cam: [
      [0, { x: 0, y: 290, w: 1076 }],
      [12, { x: 0, y: 290, w: 1076 }],
      [26, { x: 0, y: 0, w: 1076 }],
      [82, { x: 0, y: 0, w: 1076 }],
      [104, { x: 0, y: 300, w: 1076 }],
    ],
  },
  { src: "phone_match", from: 10.3, frames: 57, cam: [[0, { x: 0, y: 300, w: 1076 }]] },
  { src: "phone_gap", from: 3.0, frames: 79, cam: [[0, { x: 0, y: 280, w: 1076 }], [60, { x: 20, y: 300, w: 1040 }]] },
];
const GAP_AT = segStart(WHY, 2);

export const WhyScene: React.FC = () => (
  <AbsoluteFill>
    <ProductBg />
    <ScreenCard kind="phone" label="app.spotapply.ai">
      <Track segs={WHY} />
    </ScreenCard>
    <SegMarker segs={WHY} i={0} rect={{ x: 50, y: 470, w: 920, h: 200 }} from={110} to={164} seed={5} />
    <SegMarker segs={WHY} i={1} rect={{ x: 70, y: 435, w: 930, h: 190 }} from={30} to={57} seed={7} pad={10} />
    <SegMarker segs={WHY} i={2} rect={{ x: 95, y: 648, w: 500, h: 122 }} from={18} to={79} seed={9} pad={16} />
    <Sticker text="why it fits, in writing" at={120} outAt={160} x={STICKER.x} y={STICKER.y} size={32} tone="ink" rotate={-3} />
    <Sticker text="…and the gaps" at={GAP_AT + 22} x={STICKER.x} y={STICKER.y} size={34} tone="sky" rotate={-3} />
    <Headline lines={["See why a job *fits.*"]} at={6} size={66} />
    <Reaction at={0} outAt={300} beats={[[0, "impressed"], [GAP_AT + 14, "sideeye"]]} id="pip-why" />
    <DemoTag opacity={1} />
  </AbsoluteFill>
);

// ── tailor: the Tailoring Studio ────────────────────────────────────────────
const TAILOR: Seg[] = [
  { src: "desk_tailor", from: 1.25, frames: 48, cam: [[0, { x: 20, y: 60, w: 900 }]] },
  // Crops that start right of the modal's left padding (x ~50) stop above
  // y ~975: the lines under the resume pane start there, and a crop that
  // reaches them slices their first words off.
  { src: "desk_tailor", from: 3.0, frames: 102, cam: [[0, { x: 220, y: 252, w: 772 }], [102, { x: 226, y: 252, w: 764 }]] },
  { src: "desk_tailor", from: 9.75, frames: 48, cam: [[0, { x: 20, y: 173, w: 1120 }], [8, { x: 20, y: 173, w: 1120 }], [32, { x: 20, y: 218, w: 1420 }]] },
  { src: "desk_tailor", from: 13.6, frames: 72, cam: [[0, { x: 30, y: 180, w: 1150 }]] },
];
const T_REVEAL = segStart(TAILOR, 1);
const T_LOCKED = segStart(TAILOR, 2);
const T_COVER = segStart(TAILOR, 3);

export const TailorScene: React.FC = () => (
  <AbsoluteFill>
    <ProductBg />
    <ScreenCard kind="laptop" label="app.spotapply.ai">
      <Track segs={TAILOR} />
    </ScreenCard>
    <Sticker text="sped up" at={T_REVEAL} outAt={T_REVEAL + 36} x={CARD.x + CARD.w - 40} y={CARD.y + 112} size={30} tone="ink" rotate={4} anchor="right" />
    <SegMarker segs={TAILOR} i={2} rect={{ x: 40, y: 1100, w: 1380, h: 112 }} from={30} to={48} seed={11} pad={12} />
    <Sticker text="degree, employer, dates: never altered" at={T_LOCKED + 30} outAt={T_COVER + 6} x={STICKER.x - 20} y={STICKER.y} size={30} tone="ink" rotate={-2} />
    <Sticker text="+ the cover letter" at={T_COVER + 12} x={STICKER.x} y={STICKER.y} size={34} tone="sky" rotate={-3} />
    <Headline lines={["Your experience.", "*Tailored to the role.*"]} at={6} />
    <Reaction at={0} outAt={270} beats={[[0, "curious"], [T_REVEAL + 30, "impressed"], [T_COVER, "happy"]]} id="pip-tailor" />
    <DemoTag opacity={1} />
  </AbsoluteFill>
);

// ── fill: the extension fills, the person reviews and submits ───────────────
const FILL: Seg[] = [
  // The dashboard crop starts under the page's top bar (the recording cuts
  // through its first row).
  { src: "desk_dashboard_fill", from: 1.3, frames: 30, cam: [[0, { x: 433, y: 110, w: 944 }]] },
  { src: "desk_extension", from: 1.0, frames: 147, cam: [[0, { x: 322, y: 0, w: 1354 }], [92, { x: 322, y: 0, w: 1354 }], [114, { x: 1000, y: 335, w: 1000 }]] },
  { src: "desk_extension", from: 12.55, frames: 117, cam: [[0, { x: 300, y: 52, w: 1300 }], [96, { x: 300, y: 52, w: 1300 }], [108, { x: 300, y: 0, w: 1300 }]] },
];
const F_FORM = segStart(FILL, 1);
const F_REVIEW = segStart(FILL, 2);

export const FillScene: React.FC = () => {
  const frame = useCurrentFrame();
  const label = frame < F_FORM ? "app.spotapply.ai" : "Lumen Data · Careers";
  return (
    <AbsoluteFill>
      <ProductBg />
      <ScreenCard kind="laptop" label={label}>
        <Track segs={FILL} />
      </ScreenCard>
      <SegMarker segs={FILL} i={1} rect={{ x: 1330, y: 955, w: 640, h: 285 }} from={116} to={147} seed={13} pad={10} />
      <SegMarker segs={FILL} i={2} rect={{ x: 370, y: 915, w: 1180, h: 100 }} from={2} to={66} seed={15} pad={12} />
      <SegMarker segs={FILL} i={2} rect={{ x: 360, y: 1060, w: 400, h: 100 }} from={82} to={102} seed={17} pad={12} />
      <Sticker text="the SpotApply extension" at={F_FORM + 14} outAt={F_FORM + 100} x={STICKER.x} y={STICKER.y} size={32} tone="blue" rotate={-3} />
      <Sticker text="“10 filled · 1 need you”" at={F_FORM + 118} outAt={F_REVIEW} x={STICKER.x} y={STICKER.y} size={32} tone="ink" rotate={-2} />
      <Sticker text="the one it left for you" at={F_REVIEW + 6} outAt={F_REVIEW + 76} x={STICKER.x} y={STICKER.y} size={32} tone="sky" rotate={-3} />
      <Sticker text="your click" at={F_REVIEW + 84} outAt={F_REVIEW + 97} x={430} y={STICKER.y - 88} size={36} tone="ink" rotate={-3} />
      <Headline lines={["Less typing.", "*More control.*"]} at={6} />
      <Reaction at={0} outAt={294} beats={[[0, "curious"], [F_FORM + 100, "impressed"], [F_REVIEW + 100, "relieved"]]} id="pip-fill" />
      <DemoTag opacity={1} />
    </AbsoluteFill>
  );
};
