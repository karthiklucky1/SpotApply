import React from "react";
import { AbsoluteFill, Img, staticFile, useCurrentFrame } from "remotion";
import { ease, lerp, prog } from "../anim";
import { COLORS, FONTS, SHADOW } from "../brand";
import { COPY } from "../copy";
import { CHAPTERS } from "./timeline60";
import { COL, RAIL_Y, SRC, STAGE, STAGE_VIEW_H, TYPE, WINDOW_BAR } from "./layout";

// ── backgrounds ─────────────────────────────────────────────────────────────

/** Light chapters: white canvas, the product stage on a soft blue panel that
 * bleeds off the right edge. */
export const LightBg: React.FC = () => (
  <AbsoluteFill style={{ background: COLORS.canvas }}>
    <div
      style={{
        position: "absolute",
        left: STAGE.x - 56,
        top: 72,
        right: -60,
        bottom: 116,
        borderRadius: 56,
        background: `linear-gradient(160deg, ${COLORS.surface2} 0%, ${COLORS.surface} 100%)`,
        border: `2px solid ${COLORS.border}`,
        overflow: "hidden",
      }}
    >
      <AbsoluteFill
        style={{
          backgroundImage: `radial-gradient(${COLORS.borderStrong} 1.6px, transparent 1.7px)`,
          backgroundSize: "36px 36px",
          opacity: 0.45,
        }}
      />
      <AbsoluteFill
        style={{ background: "radial-gradient(700px 520px at 85% 10%, rgba(14,165,233,0.14), transparent 70%)" }}
      />
    </div>
  </AbsoluteFill>
);

/** The dark bookends (problem, end card). */
export const DarkBg: React.FC<{ readonly style?: React.CSSProperties }> = ({ style }) => (
  <AbsoluteFill
    style={{
      background: [
        "radial-gradient(1000px 700px at 85% 10%, rgba(14,165,233,0.32), transparent 70%)",
        "radial-gradient(900px 800px at 5% 100%, rgba(0,119,194,0.42), transparent 70%)",
        `linear-gradient(165deg, ${COLORS.ink} 0%, ${COLORS.inkDeep} 70%)`,
      ].join(", "),
      ...style,
    }}
  >
    <AbsoluteFill
      style={{
        backgroundImage: "radial-gradient(rgba(255,255,255,0.10) 1.6px, transparent 1.7px)",
        backgroundSize: "40px 40px",
      }}
    />
  </AbsoluteFill>
);

// ── the left editorial column ───────────────────────────────────────────────

export const Chapter: React.FC<{
  readonly number?: string;
  readonly label: string;
  readonly headline: readonly string[];
  readonly support?: string;
  readonly enterAt?: number;
  readonly exitAt?: number;
  readonly dark?: boolean;
  readonly top?: number;
}> = ({ number, label, headline, support, enterAt = 4, exitAt, dark = false, top = 250 }) => {
  const frame = useCurrentFrame();
  const out = exitAt === undefined ? 0 : prog(frame, exitAt, 9, ease.in);
  const t0 = prog(frame, enterAt, 18);
  const ink = dark ? "#FFFFFF" : COLORS.ink;
  return (
    <div
      style={{
        position: "absolute",
        left: COL.x,
        top,
        width: COL.w,
        fontFamily: FONTS.sans,
        opacity: 1 - out,
        translate: `${-30 * out}px 0px`,
      }}
    >
      <div style={{ display: "flex", alignItems: "baseline", gap: 18, opacity: t0, translate: `0px ${(1 - t0) * 30}px` }}>
        {number ? (
          <span
            style={{
              fontFamily: FONTS.serif,
              fontStyle: "italic",
              fontSize: TYPE.number,
              lineHeight: 0.9,
              color: dark ? COLORS.skyOnDark : COLORS.accent,
            }}
          >
            {number}
          </span>
        ) : null}
        <span
          style={{
            fontWeight: 700,
            fontSize: TYPE.eyebrow,
            letterSpacing: "0.14em",
            textTransform: "uppercase",
            color: dark ? "rgba(255,255,255,0.7)" : COLORS.muted,
          }}
        >
          {label}
        </span>
      </div>
      <div style={{ marginTop: 26 }}>
        {headline.map((line, i) => {
          const t = prog(frame, enterAt + 4 + i * 5, 20);
          return (
            <div key={i} style={{ overflow: "hidden", paddingBottom: "0.12em", marginBottom: "-0.12em" }}>
              <div
                style={{
                  fontWeight: 800,
                  fontSize: TYPE.headline,
                  lineHeight: 1.04,
                  letterSpacing: "-0.035em",
                  color: ink,
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
      {support ? (
        <div
          style={{
            marginTop: 30,
            fontWeight: 500,
            fontSize: TYPE.support,
            lineHeight: 1.42,
            color: dark ? "rgba(255,255,255,0.78)" : COLORS.inkSoft,
            maxWidth: COL.w - 20,
            ...(() => {
              const t = prog(frame, enterAt + 16, 18);
              return { opacity: t, translate: `0px ${(1 - t) * 20}px` };
            })(),
          }}
        >
          {support}
        </div>
      ) : null}
    </div>
  );
};

// ── a desktop capture with a camera ─────────────────────────────────────────

/** The camera: which source region (2560x1600 px) fills the window's width. */
export type Focus = { readonly x: number; readonly y: number; readonly w: number };
export const FULL: Focus = { x: 0, y: 0, w: SRC.w };

export const lerpFocus = (a: Focus, b: Focus, t: number): Focus => ({
  x: lerp(a.x, b.x, t),
  y: lerp(a.y, b.y, t),
  w: lerp(a.w, b.w, t),
});

/** Keyframed camera: [frame, focus] pairs, eased in and out between them. */
export const cameraAt = (frame: number, keys: readonly (readonly [number, Focus])[]): Focus => {
  let f = keys[0][1];
  for (let i = 1; i < keys.length; i++) {
    const [f0, a] = keys[i - 1];
    const [f1, b] = keys[i];
    if (frame >= f0) f = lerpFocus(a, b, prog(frame, f0, f1 - f0, ease.inOut));
  }
  return f;
};

export type WindowGeom = { readonly x: number; readonly y: number; readonly w: number; readonly viewH: number };
export const STAGE_WINDOW: WindowGeom = { x: STAGE.x, y: STAGE.y, w: STAGE.w, viewH: STAGE_VIEW_H };

/** Where a source-pixel rect lands on the canvas for a given window + focus. */
export const toCanvas60 = (g: WindowGeom, focus: Focus, r: { x: number; y: number; w: number; h: number }) => {
  const s = g.w / focus.w;
  return {
    x: g.x + 2 + (r.x - focus.x) * s,
    y: g.y + 2 + WINDOW_BAR + (r.y - focus.y) * s,
    w: r.w * s,
    h: r.h * s,
  };
};

export const AppWindow: React.FC<{
  readonly src: string;
  readonly focus: Focus;
  readonly geom?: WindowGeom;
  readonly url?: string;
  readonly style?: React.CSSProperties;
}> = ({ src, focus, geom = STAGE_WINDOW, url = "app.spotapply.ai/dashboard", style }) => {
  const s = geom.w / focus.w;
  return (
    <div
      style={{
        position: "absolute",
        left: geom.x,
        top: geom.y,
        width: geom.w,
        borderRadius: 22,
        overflow: "hidden",
        background: COLORS.canvas,
        border: `2px solid ${COLORS.border}`,
        boxShadow: SHADOW.card,
        ...style,
      }}
    >
      <div
        style={{
          height: WINDOW_BAR,
          display: "flex",
          alignItems: "center",
          gap: 9,
          padding: "0 18px",
          background: COLORS.surface2,
          borderBottom: `2px solid ${COLORS.border}`,
        }}
      >
        {[0, 1, 2].map((i) => (
          <span key={i} style={{ width: 12, height: 12, borderRadius: 6, background: COLORS.borderStrong }} />
        ))}
        <span
          style={{
            marginLeft: 16,
            padding: "4px 18px",
            borderRadius: 10,
            background: COLORS.canvas,
            border: `1.5px solid ${COLORS.border}`,
            fontFamily: FONTS.mono,
            fontSize: 19,
            color: COLORS.muted,
          }}
        >
          {url}
        </span>
      </div>
      <div style={{ position: "relative", height: geom.viewH, overflow: "hidden" }}>
        <Img
          src={staticFile(src)}
          style={{
            position: "absolute",
            left: -focus.x * s,
            top: -focus.y * s,
            width: SRC.w * s,
            display: "block",
          }}
        />
      </div>
    </div>
  );
};

/** A focus ring on a real element (canvas rect). */
export const Focus60: React.FC<{ readonly rect: { x: number; y: number; w: number; h: number }; readonly t: number; readonly radius?: number }> = ({
  rect,
  t,
  radius = 18,
}) => {
  if (t <= 0) return null;
  return (
    <div
      style={{
        position: "absolute",
        left: rect.x - 7,
        top: rect.y - 7,
        width: rect.w + 14,
        height: rect.h + 14,
        borderRadius: radius,
        border: `5px solid ${COLORS.accent}`,
        boxShadow: `0 0 0 ${9 * t}px rgba(14,165,233,0.16)`,
        opacity: Math.min(1, t * 1.5),
        scale: String(1.04 - 0.04 * t),
      }}
    />
  );
};

/** A floating annotation chip pointing at the product. */
export const Note: React.FC<{
  readonly x: number;
  readonly y: number;
  readonly t: number;
  readonly children: React.ReactNode;
  readonly tone?: "accent" | "ink" | "ok";
}> = ({ x, y, t, children, tone = "accent" }) => {
  if (t <= 0) return null;
  const bg = tone === "ink" ? COLORS.ink : tone === "ok" ? COLORS.ok : COLORS.accent;
  return (
    <div
      style={{
        position: "absolute",
        left: x,
        top: y,
        padding: "12px 24px",
        borderRadius: 999,
        background: bg,
        color: "#FFFFFF",
        fontFamily: FONTS.sans,
        fontWeight: 700,
        fontSize: 26,
        whiteSpace: "nowrap",
        boxShadow: "0 18px 36px -16px rgba(12,42,62,0.55)",
        opacity: t,
        translate: `0px ${(1 - t) * 16}px`,
      }}
    >
      {children}
    </div>
  );
};

// ── the chapter rail ────────────────────────────────────────────────────────

/** Five steps along the bottom; `frame` is the GLOBAL frame. */
export const ChapterRail: React.FC<{ readonly frame: number; readonly opacity: number; readonly allDone?: boolean }> = ({
  frame,
  opacity,
  allDone = false,
}) => {
  if (opacity <= 0) return null;
  const x0 = COL.x;
  const gap = 238;
  const active = allDone ? CHAPTERS.length : CHAPTERS.filter((c) => frame >= c.at).length - 1;
  // Each step is [dot][label][segment to the next dot], laid out by flex so a
  // segment always starts after its label: one continuous line under the row
  // struck through every label.
  return (
    <div style={{ position: "absolute", left: x0, top: RAIL_Y, height: 22, opacity, fontFamily: FONTS.sans, display: "flex" }}>
      {CHAPTERS.map((c, i) => {
        const done = i < active || allDone;
        const on = i === active && !allDone;
        const last = i === CHAPTERS.length - 1;
        return (
          <div key={c.n} style={{ width: last ? undefined : gap, display: "flex", alignItems: "center", gap: 12 }}>
            <span
              style={{
                flex: "none",
                width: 22,
                height: 22,
                borderRadius: 11,
                boxSizing: "border-box",
                background: done || on ? COLORS.accent : COLORS.canvas,
                border: `3px solid ${done || on ? COLORS.accent : COLORS.borderStrong}`,
                boxShadow: on ? "0 0 0 7px rgba(14,165,233,0.18)" : undefined,
              }}
            />
            <span
              style={{
                flex: "none",
                fontWeight: on ? 700 : 600,
                fontSize: 22,
                lineHeight: "22px",
                color: on ? COLORS.ink : done ? COLORS.inkSoft : COLORS.muted,
              }}
            >
              {c.label}
            </span>
            {last ? null : (
              <span style={{ flex: 1, height: 3, marginRight: 14, borderRadius: 2, background: done ? COLORS.accent : COLORS.border }} />
            )}
          </div>
        );
      })}
    </div>
  );
};

export const DemoTag60: React.FC<{ readonly opacity: number }> = ({ opacity }) =>
  opacity <= 0 ? null : (
    <div
      style={{
        position: "absolute",
        right: 120,
        top: RAIL_Y - 4,
        fontFamily: FONTS.sans,
        fontWeight: 600,
        fontSize: 21,
        color: COLORS.muted,
        opacity,
        display: "flex",
        alignItems: "center",
        gap: 10,
      }}
    >
      <span style={{ width: 9, height: 9, borderRadius: 5, background: COLORS.borderStrong }} />
      {COPY.demoLabel}
    </div>
  );
