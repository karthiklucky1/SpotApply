import React from "react";
import { Freeze, interpolate, OffthreadVideo, Sequence, spring, staticFile, useCurrentFrame, useVideoConfig } from "remotion";
import { ease, lerp, prog } from "../anim";
import { COLORS, FONTS } from "../brand";
import { LockIcon } from "../components/Icons";
import { CARD, CARD_VIEW, FOOTAGE, type FootageId, HEADLINE } from "./layout";

// ── camera over a recording ─────────────────────────────────────────────────
/** A window onto a recording: the source-pixel point at the card's top-left and
 *  the source width that fills the card (the visible height follows). */
export type Focus = { x: number; y: number; w: number };
export type Key = readonly [frame: number, focus: Focus];

export const cam = (frame: number, keys: readonly Key[]): Focus => {
  if (frame <= keys[0][0]) return keys[0][1];
  for (let i = 1; i < keys.length; i++) {
    const [f1, b] = keys[i];
    if (frame <= f1) {
      const [f0, a] = keys[i - 1];
      const t = ease.inOut((frame - f0) / Math.max(1, f1 - f0));
      return { x: lerp(a.x, b.x, t), y: lerp(a.y, b.y, t), w: lerp(a.w, b.w, t) };
    }
  }
  return keys[keys.length - 1][1];
};

/** One stretch of a recording. `from` is the source second it starts at;
 *  `freeze` holds that single frame (a pause, never a re-time). */
export type Seg = {
  src: FootageId;
  from: number;
  frames: number;
  freeze?: boolean;
  cam: readonly Key[]; // frames relative to the segment's start
};

/** The camera kept inside its recording: a crop never runs past the
 *  footage's edges, whatever the card's shape (zooms in if it must). */
export const fit = (f: Focus, src: FootageId): Focus => {
  const [sw, sh] = FOOTAGE[src];
  const k = CARD_VIEW.h / CARD_VIEW.w; // visible source height per source width
  const w = Math.min(f.w, sw, sh / k);
  const x = Math.max(0, Math.min(f.x + (f.w - w) / 2, sw - w));
  const y = Math.max(0, Math.min(f.y, sh - w * k));
  return { x, y, w };
};

export const trackAt = (segs: readonly Seg[], frame: number) => {
  let at = 0;
  for (let i = 0; i < segs.length; i++) {
    const s = segs[i];
    if (frame < at + s.frames || i === segs.length - 1) {
      const local = frame - at;
      return { i, seg: s, local, start: at, focus: fit(cam(local, s.cam), s.src) };
    }
    at += s.frames;
  }
  throw new Error("empty track");
};

export const trackLength = (segs: readonly Seg[]) => segs.reduce((n, s) => n + s.frames, 0);

/** Source rect -> canvas rect, through the card and the camera. */
export const toCanvas = (focus: Focus, r: { x: number; y: number; w: number; h: number }) => {
  const s = CARD_VIEW.w / focus.w;
  return { x: CARD.x + (r.x - focus.x) * s, y: CARD.y + CARD.bar + (r.y - focus.y) * s, w: r.w * s, h: r.h * s };
};

const SegVideo: React.FC<{ readonly seg: Seg }> = ({ seg }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const f = fit(cam(frame, seg.cam), seg.src);
  const [sw, sh] = FOOTAGE[seg.src];
  const s = CARD_VIEW.w / f.w;
  const video = (
    <OffthreadVideo
      src={staticFile(`reel/footage/${seg.src}.mp4`)}
      trimBefore={Math.round(seg.from * fps)}
      muted
      style={{ position: "absolute", left: -f.x * s, top: -f.y * s, width: sw * s, height: sh * s, maxWidth: "none" }}
    />
  );
  return seg.freeze ? <Freeze frame={0}>{video}</Freeze> : video;
};

/** The recordings of one scene, back to back, inside the card's screen. */
export const Track: React.FC<{ readonly segs: readonly Seg[] }> = ({ segs }) => {
  let at = 0;
  return (
    <>
      {segs.map((s, i) => {
        const from = at;
        at += s.frames;
        return (
          <Sequence key={i} from={from} durationInFrames={s.frames} layout="none">
            <SegVideo seg={s} />
          </Sequence>
        );
      })}
    </>
  );
};

// ── the screen card ─────────────────────────────────────────────────────────
export const ScreenCard: React.FC<{
  readonly kind: "phone" | "laptop";
  readonly label: string;
  readonly enter?: number; // 0..1
  readonly children: React.ReactNode;
}> = ({ kind, label, enter = 1, children }) => (
  <div
    style={{
      position: "absolute",
      left: CARD.x,
      top: CARD.y,
      width: CARD.w,
      height: CARD.h,
      borderRadius: kind === "phone" ? 46 : 26,
      overflow: "hidden",
      background: "#FFFFFF",
      boxShadow: "0 50px 100px -40px rgba(8,32,47,0.55), 0 0 0 1px rgba(8,32,47,0.08)",
      border: kind === "phone" ? "10px solid #0B1620" : "2px solid #D5E3EF",
      boxSizing: "border-box",
      opacity: enter,
      translate: `0px ${(1 - enter) * 90}px`,
      scale: String(lerp(0.94, 1, enter)),
    }}
  >
    <div
      style={{
        height: CARD.bar - (kind === "phone" ? 10 : 2),
        display: "flex",
        alignItems: "center",
        gap: 12,
        padding: "0 20px",
        background: kind === "phone" ? "#F1F5F9" : "#EEF3F8",
        borderBottom: "1px solid #DCE5EE",
        fontFamily: FONTS.sans,
      }}
    >
      {kind === "laptop"
        ? [0, 1, 2].map((i) => <span key={i} style={{ width: 13, height: 13, borderRadius: 7, background: "#C5D3E0" }} />)
        : null}
      <div
        style={{
          flex: 1,
          height: 34,
          borderRadius: 17,
          background: "#FFFFFF",
          border: "1px solid #DCE5EE",
          display: "flex",
          alignItems: "center",
          justifyContent: kind === "phone" ? "center" : "flex-start",
          gap: 8,
          padding: "0 14px",
          fontSize: 20,
          fontWeight: 600,
          color: COLORS.inkSoft,
          marginLeft: kind === "laptop" ? 8 : 0,
        }}
      >
        <LockIcon size={17} color={COLORS.muted} /> {label}
      </div>
    </div>
    <div style={{ position: "absolute", left: 0, top: CARD.bar - (kind === "phone" ? 10 : 2), width: CARD_VIEW.w, height: CARD_VIEW.h, overflow: "hidden" }}>
      {children}
    </div>
  </div>
);

// ── hand-drawn marker loop ──────────────────────────────────────────────────
const rnd = (seed: number) => {
  const x = Math.sin(seed * 127.1) * 43758.5453;
  return x - Math.floor(x);
};

export const Marker: React.FC<{
  readonly rect: { x: number; y: number; w: number; h: number };
  readonly t: number; // 0..1 draw-in, then hold; fade with `out`
  readonly out?: number;
  readonly seed?: number;
  readonly color?: string;
  readonly pad?: number;
}> = ({ rect, t, out = 0, seed = 1, color = COLORS.accentSky, pad = 16 }) => {
  if (t <= 0 || out >= 1) return null;
  const cx = rect.x + rect.w / 2;
  const cy = rect.y + rect.h / 2;
  const rx = rect.w / 2 + pad;
  const ry = rect.h / 2 + pad;
  const pts: string[] = [];
  const steps = 64;
  for (let k = 0; k <= steps; k++) {
    const a = -Math.PI * 0.62 + (k / steps) * Math.PI * 2.18; // a little past a full turn
    const wob = 1 + (rnd(seed * 31 + k) - 0.5) * 0.035 + (k / steps) * 0.05;
    pts.push(`${(cx + Math.cos(a) * rx * wob).toFixed(1)},${(cy + Math.sin(a) * ry * wob).toFixed(1)}`);
  }
  const d = `M ${pts.join(" L ")}`;
  const len = 2.18 * Math.PI * Math.sqrt((rx * rx + ry * ry) / 2) * 1.05;
  return (
    <svg width={1080} height={1920} style={{ position: "absolute", left: 0, top: 0, pointerEvents: "none", opacity: 1 - out }}>
      <path d={d} fill="none" stroke="rgba(255,255,255,0.85)" strokeWidth={14} strokeLinecap="round" strokeLinejoin="round"
        strokeDasharray={len} strokeDashoffset={len * (1 - t)} />
      <path d={d} fill="none" stroke={color} strokeWidth={8} strokeLinecap="round" strokeLinejoin="round"
        strokeDasharray={len} strokeDashoffset={len * (1 - t)} />
    </svg>
  );
};

// ── stickers and headlines ──────────────────────────────────────────────────
export type Tone = "white" | "ink" | "sky" | "blue";
const TONES: Record<Tone, { bg: string; fg: string; hi: string }> = {
  white: { bg: "#FFFFFF", fg: COLORS.ink, hi: COLORS.accent },
  ink: { bg: COLORS.inkDeep, fg: "#FFFFFF", hi: COLORS.skyOnDark },
  sky: { bg: "#BAE6FD", fg: COLORS.inkDeep, hi: COLORS.accent700 },
  blue: { bg: COLORS.accent, fg: "#FFFFFF", hi: "#BAE6FD" },
};

/** Pop-in with a little overshoot (a sticker slapped on). */
export const usePop = (at: number, outAt?: number) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const s = spring({ frame: frame - at, fps, config: { damping: 11, stiffness: 170, mass: 0.7 } });
  const out = outAt === undefined ? 0 : prog(frame, outAt, 7, ease.in);
  return { scale: frame < at ? 0 : s * (1 - 0.15 * out), opacity: frame < at ? 0 : Math.min(1, (frame - at) / 3) * (1 - out) };
};

/** Text with *starred* words in the tone's highlight colour. */
const Rich: React.FC<{ readonly text: string; readonly hi: string }> = ({ text, hi }) => (
  <>
    {text.split(/(\*[^*]+\*)/).map((part, i) =>
      part.startsWith("*") ? (
        <span key={i} style={{ color: hi }}>
          {part.slice(1, -1)}
        </span>
      ) : (
        <React.Fragment key={i}>{part}</React.Fragment>
      ),
    )}
  </>
);

export const Sticker: React.FC<{
  readonly text: string;
  readonly at: number;
  readonly outAt?: number;
  readonly x: number;
  readonly y: number;
  readonly size?: number;
  readonly tone?: Tone;
  readonly rotate?: number;
  readonly anchor?: "left" | "center" | "right";
}> = ({ text, at, outAt, x, y, size = 34, tone = "ink", rotate = -3, anchor = "left" }) => {
  const p = usePop(at, outAt);
  if (p.opacity <= 0) return null;
  const c = TONES[tone];
  const tx = anchor === "left" ? "0%" : anchor === "center" ? "-50%" : "-100%";
  return (
    <div
      style={{
        position: "absolute",
        left: x,
        top: y,
        translate: `${tx} 0%`,
        rotate: `${rotate}deg`,
        scale: String(p.scale),
        opacity: p.opacity,
        transformOrigin: anchor === "right" ? "right center" : anchor === "center" ? "center" : "left center",
        padding: `${size * 0.32}px ${size * 0.55}px`,
        borderRadius: size * 0.55,
        background: c.bg,
        color: c.fg,
        fontFamily: FONTS.sans,
        fontWeight: 800,
        fontSize: size,
        letterSpacing: "-0.01em",
        lineHeight: 1.05,
        whiteSpace: "nowrap",
        boxShadow: "0 14px 30px -14px rgba(8,32,47,0.55)",
      }}
    >
      <Rich text={text} hi={c.hi} />
    </div>
  );
};

/** The beat's headline: one sticker per line, slapped on in turn. */
export const Headline: React.FC<{
  readonly lines: readonly string[];
  readonly at: number;
  readonly outAt?: number;
  readonly tones?: readonly Tone[];
  readonly size?: number;
  readonly y?: number;
}> = ({ lines, at, outAt, tones, size = 62, y = HEADLINE.y }) => (
  <>
    {lines.map((line, i) => (
      <Sticker
        key={i}
        text={line}
        at={at + i * 5}
        outAt={outAt}
        x={HEADLINE.x - 4}
        y={y + i * size * 1.42}
        size={size}
        tone={tones?.[i] ?? "white"}
        rotate={i % 2 === 0 ? -2 : 1.5}
      />
    ))}
  </>
);

export const DemoTag: React.FC<{ readonly opacity: number; readonly dark?: boolean; readonly text?: string }> = ({
  opacity,
  dark,
  text = "Demo data",
}) =>
  opacity <= 0 ? null : (
    <div
      style={{
        position: "absolute",
        right: 70,
        top: 236,
        padding: "6px 14px",
        borderRadius: 999,
        background: dark ? "rgba(255,255,255,0.12)" : "rgba(8,32,47,0.06)",
        border: `1.5px solid ${dark ? "rgba(255,255,255,0.28)" : "rgba(8,32,47,0.14)"}`,
        color: dark ? "rgba(255,255,255,0.86)" : COLORS.muted,
        fontFamily: FONTS.sans,
        fontWeight: 600,
        fontSize: 20,
        opacity,
      }}
    >
      {text}
    </div>
  );

/** 0..1 that rises at `a` and falls at `b` (each over `d` frames). */
export const window01 = (frame: number, a: number, b: number, d = 8) =>
  interpolate(frame, [a, a + d, b - d, b], [0, 1, 1, 0], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
