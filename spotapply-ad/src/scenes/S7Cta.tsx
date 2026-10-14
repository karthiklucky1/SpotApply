import React from "react";
import { AbsoluteFill, Freeze, useCurrentFrame } from "remotion";
import { ease, lerp, prog } from "../anim";
import { COLORS, FONTS } from "../brand";
import { COPY } from "../copy";
import { BluePath } from "../components/BluePath";
import { ArrowRightIcon } from "../components/Icons";
import { Wordmark } from "../components/Logo";
import { Screen, type ScreenGeom } from "../components/Screen";
import { SCENES } from "../timeline";
import { TrackScene } from "./S6Track";

// 25.2-30s. The end card: a deep brand-blue sheet rises over the tracker
// (the same upward motion as the app's drawers), so the ending reads as a
// different beat from the light product scenes. The message sits in the top
// half, inside the safe area; a fan of the real app screens fills the bottom
// as product proof and may run into the platform overlay zone because it
// carries nothing the viewer has to read. Everything lands by ~2.1 s and
// then holds perfectly still: from 27.3 s to the end nothing moves.

const LOCKUP_Y = 252;
const TAGLINE_Y = 428;
const BUTTON_Y = 736;
const URL_Y = 902;
const PILLS_Y = 1046;

// The fan: real captures (public/live/), small enough to be imagery.
const FAN_W = 470;
const FAN: readonly {
  src: string;
  slice: number;
  cx: number;
  top: number;
  rotate: number;
  at: number;
}[] = [
  { src: "live/board.png", slice: 200, cx: 250, top: 1236, rotate: -9, at: 26 },
  { src: "live/submitted.png", slice: 222, cx: 830, top: 1236, rotate: 9, at: 30 },
  { src: "live/match.png", slice: 1090, cx: 540, top: 1166, rotate: 0, at: 34 },
];
const fanGeom = (cx: number, top: number, sliceY: number): ScreenGeom => ({
  x: cx - FAN_W / 2,
  y: top,
  w: FAN_W,
  slices: [{ y: sliceY, h: 1100 }],
});

const PILLS = ["⚡ Apply today", "Grounded resume", "Your final click"] as const;

const rise = (frame: number, at: number, dist = 46) => {
  const t = prog(frame, at, 20);
  return { opacity: t, translate: `0px ${(1 - t) * dist}px` };
};

export const CtaScene: React.FC = () => {
  const frame = useCurrentFrame();

  // The blue sheet rises over the frozen tracker; its top corners square off
  // as it fills the frame.
  const sheet = prog(frame, 0, 15, ease.inOut);
  const lockup = prog(frame, 10, 22);
  const underline = prog(frame, 34, 18, ease.inOut);

  return (
    <AbsoluteFill style={{ fontFamily: FONTS.sans }}>
      {sheet < 1 ? (
        <AbsoluteFill style={{ scale: String(lerp(1, 0.94, sheet)), opacity: 1 - 0.4 * sheet }}>
          <Freeze frame={SCENES.track - 1}>
            <TrackScene />
          </Freeze>
        </AbsoluteFill>
      ) : null}

      <AbsoluteFill
        style={{
          translate: `0px ${(1 - sheet) * 1920}px`,
          borderTopLeftRadius: 80 * (1 - sheet),
          borderTopRightRadius: 80 * (1 - sheet),
          overflow: "hidden",
          background: [
            "radial-gradient(900px 760px at 88% 6%, rgba(14,165,233,0.38), transparent 70%)",
            "radial-gradient(900px 900px at 6% 100%, rgba(0,119,194,0.50), transparent 70%)",
            `linear-gradient(172deg, ${COLORS.ink} 0%, ${COLORS.inkDeep} 62%, ${COLORS.inkDeep} 100%)`,
          ].join(", "),
          boxShadow: "0 -30px 80px rgba(8,32,47,0.35)",
        }}
      >
        <AbsoluteFill
          style={{
            backgroundImage: "radial-gradient(rgba(255,255,255,0.11) 1.6px, transparent 1.7px)",
            backgroundSize: "40px 40px",
            maskImage: "linear-gradient(180deg, black 0%, black 55%, transparent 85%)",
          }}
        />

        {/* Product proof: the real screens, fanned. */}
        {FAN.map((card) => {
          const t = prog(frame, card.at, 22);
          return (
            <Screen
              key={card.src}
              src={card.src}
              geom={fanGeom(card.cx, card.top, card.slice)}
              style={{
                rotate: `${lerp(card.rotate * 1.6, card.rotate, t)}deg`,
                translate: `0px ${(1 - t) * 260}px`,
                opacity: t,
                boxShadow: "0 40px 90px -30px rgba(0,0,0,0.55), 0 8px 24px rgba(0,0,0,0.25)",
                border: "2px solid rgba(255,255,255,0.18)",
              }}
            />
          );
        })}
        <div
          style={{
            position: "absolute",
            // Just above the centre card's top-right corner, clear of its URL bar.
            left: 540 + FAN_W / 2 - 138,
            top: 1166 - 52,
            padding: "6px 16px",
            borderRadius: 999,
            background: "rgba(8,32,47,0.82)",
            border: "1.5px solid rgba(255,255,255,0.25)",
            color: "rgba(255,255,255,0.85)",
            fontWeight: 600,
            fontSize: 22,
            ...rise(frame, 40, 20),
          }}
        >
          Demo data
        </div>
        {/* The fan fades into the sheet at the very bottom. */}
        <div
          style={{
            position: "absolute",
            left: 0,
            right: 0,
            top: 1620,
            bottom: 0,
            background: `linear-gradient(180deg, transparent, ${COLORS.inkDeep} 92%)`,
          }}
        />

        <div
          style={{
            position: "absolute",
            top: LOCKUP_Y,
            left: 0,
            right: 0,
            display: "flex",
            justifyContent: "center",
            opacity: lockup,
            scale: String(lerp(0.9, 1, lockup)),
          }}
        >
          <Wordmark size={112} markSize={104} color="#FFFFFF" />
        </div>

        <div style={{ position: "absolute", top: TAGLINE_Y, left: 0, right: 0, textAlign: "center" }}>
          <div
            style={{
              fontWeight: 800,
              fontSize: 94,
              letterSpacing: "-0.035em",
              lineHeight: 1.06,
              color: "#FFFFFF",
              ...rise(frame, 14),
            }}
          >
            {COPY.taglineA}
          </div>
          <div
            style={{
              fontFamily: FONTS.serif,
              fontStyle: "italic",
              fontWeight: 400,
              fontSize: 132,
              letterSpacing: "-0.012em",
              lineHeight: 1.02,
              color: COLORS.skyOnDark,
              marginTop: 4,
              ...rise(frame, 19),
            }}
          >
            {COPY.taglineB}
          </div>
        </div>

        <div
          style={{
            position: "absolute",
            top: BUTTON_Y,
            left: 0,
            right: 0,
            display: "flex",
            justifyContent: "center",
            ...rise(frame, 24),
          }}
        >
          <div
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 18,
              padding: "30px 68px",
              borderRadius: 999,
              background: "#FFFFFF",
              color: COLORS.accent700,
              fontWeight: 700,
              fontSize: 54,
              boxShadow: "0 0 0 10px rgba(255,255,255,0.08), 0 30px 60px -24px rgba(0,0,0,0.55)",
            }}
          >
            {COPY.cta}
            <ArrowRightIcon size={52} color={COLORS.accent700} stroke={2.8} />
          </div>
        </div>

        <div
          style={{
            position: "absolute",
            top: URL_Y,
            left: 0,
            right: 0,
            textAlign: "center",
            fontWeight: 700,
            fontSize: 72,
            letterSpacing: "-0.02em",
            color: "#FFFFFF",
            ...rise(frame, 28),
          }}
        >
          {COPY.url}
        </div>
        <BluePath
          d="M 290 1004 C 420 1018, 660 1018, 790 1004"
          progress={underline}
          stroke={6}
          head={false}
        />

        <div
          style={{
            position: "absolute",
            top: PILLS_Y,
            left: 0,
            right: 0,
            display: "flex",
            justifyContent: "center",
            gap: 14,
          }}
        >
          {PILLS.map((p, i) => (
            <span
              key={p}
              style={{
                padding: "12px 24px",
                borderRadius: 999,
                background: "rgba(255,255,255,0.08)",
                border: "2px solid rgba(255,255,255,0.20)",
                color: "rgba(255,255,255,0.92)",
                fontWeight: 600,
                fontSize: 28,
                whiteSpace: "nowrap",
                ...rise(frame, 32 + i * 4, 24),
              }}
            >
              {p}
            </span>
          ))}
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};
