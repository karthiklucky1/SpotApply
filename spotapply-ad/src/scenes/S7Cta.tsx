import React from "react";
import { AbsoluteFill, Freeze, useCurrentFrame } from "remotion";
import { ease, lerp, prog } from "../anim";
import { COLORS, FONTS } from "../brand";
import { COPY } from "../copy";
import { BluePath } from "../components/BluePath";
import { ArrowRightIcon } from "../components/Icons";
import { Wordmark } from "../components/Logo";
import { SCENES } from "../timeline";
import { TrackScene } from "./S6Track";

// 25-30s. The end card. Everything lands in the first ~1.6s and then holds
// perfectly still, so the URL can be read and the last second is steady.

// Vertical positions (the stack is centred in the safe area).
const LOCKUP_Y = 590;
const TAGLINE_Y = 820;
const BUTTON_Y = 1124;
const URL_Y = 1308;

const rise = (frame: number, at: number) => {
  const t = prog(frame, at, 20);
  return { opacity: t, translate: `0px ${(1 - t) * 50}px` };
};

export const CtaScene: React.FC = () => {
  const frame = useCurrentFrame();
  const lockup = prog(frame, 7, 22);
  const underline = prog(frame, 30, 18, ease.inOut);

  const trackerOut = prog(frame, 0, 9, ease.out);

  return (
    <AbsoluteFill style={{ fontFamily: FONTS.sans }}>
      {trackerOut < 1 ? (
        <AbsoluteFill style={{ opacity: 1 - trackerOut, scale: String(lerp(1, 0.92, trackerOut)) }}>
          <Freeze frame={SCENES.track - 1}>
            <TrackScene />
          </Freeze>
        </AbsoluteFill>
      ) : null}
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
        <Wordmark size={132} markSize={120} />
      </div>

      <div
        style={{
          position: "absolute",
          top: TAGLINE_Y,
          left: 0,
          right: 0,
          textAlign: "center",
          color: COLORS.ink,
        }}
      >
        <div style={{ fontWeight: 800, fontSize: 88, letterSpacing: "-0.035em", lineHeight: 1.08, ...rise(frame, 12) }}>
          {COPY.taglineA}
        </div>
        <div
          style={{
            fontFamily: FONTS.serif,
            fontStyle: "italic",
            fontWeight: 400,
            fontSize: 124,
            letterSpacing: "-0.012em",
            lineHeight: 1.04,
            color: COLORS.accent,
            marginTop: 6,
            ...rise(frame, 18),
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
            padding: "30px 64px",
            borderRadius: 26,
            background: COLORS.accent,
            color: "#FFFFFF",
            fontWeight: 700,
            fontSize: 52,
            boxShadow: "0 30px 60px -28px rgba(0,119,194,0.75), 0 4px 12px rgba(0,119,194,0.2)",
          }}
        >
          {COPY.cta}
          <ArrowRightIcon size={50} color="#FFFFFF" stroke={2.8} />
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
          fontSize: 70,
          letterSpacing: "-0.02em",
          color: COLORS.ink,
          ...rise(frame, 30),
        }}
      >
        {COPY.url}
      </div>
      <BluePath d="M 300 1410 C 420 1424, 660 1424, 780 1410" progress={underline} stroke={6} head={false} />
    </AbsoluteFill>
  );
};
