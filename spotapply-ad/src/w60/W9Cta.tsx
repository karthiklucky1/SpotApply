import React from "react";
import { AbsoluteFill, useCurrentFrame } from "remotion";
import { ease, lerp, prog } from "../anim";
import { COLORS, FONTS } from "../brand";
import { COPY } from "../copy";
import { BluePath } from "../components/BluePath";
import { ArrowRightIcon } from "../components/Icons";
import { Wordmark } from "../components/Logo";
import { AppWindow, DarkBg, FULL, type WindowGeom } from "./components";
import { COL } from "./layout";

// 54-60s. The end card, widescreen: the deep-blue sheet rises over the recap;
// message on the left, three real desktop screens fanned on the right
// (tagged demo data). Lands by ~1.9 s, then holds perfectly still.

const FAN: readonly { src: string; geom: WindowGeom; rotate: number; at: number }[] = [
  { src: "live60/board.png", geom: { x: 1020, y: 96, w: 740, viewH: 463 }, rotate: -6, at: 20 },
  { src: "live60/tailor.png", geom: { x: 1100, y: 470, w: 740, viewH: 463 }, rotate: 5, at: 26 },
  { src: "live60/match.png", geom: { x: 940, y: 290, w: 740, viewH: 463 }, rotate: -1, at: 32 },
];

const rise = (frame: number, at: number, dist = 40) => {
  const t = prog(frame, at, 20);
  return { opacity: t, translate: `0px ${(1 - t) * dist}px` };
};

export const CtaScene60: React.FC = () => {
  const frame = useCurrentFrame();
  const sheet = prog(frame, 0, 15, ease.inOut);
  const lockup = prog(frame, 10, 22);
  const underline = prog(frame, 34, 18, ease.inOut);

  return (
    <AbsoluteFill style={{ fontFamily: FONTS.sans }}>
      <AbsoluteFill
        style={{
          translate: `0px ${(1 - sheet) * 1080}px`,
          borderTopLeftRadius: 70 * (1 - sheet),
          borderTopRightRadius: 70 * (1 - sheet),
          overflow: "hidden",
        }}
      >
        <DarkBg />

        {FAN.map((card) => {
          const t = prog(frame, card.at, 22);
          return (
            <AppWindow
              key={card.src}
              src={card.src}
              focus={FULL}
              geom={card.geom}
              style={{
                rotate: `${lerp(card.rotate * 1.8, card.rotate, t)}deg`,
                translate: `${(1 - t) * 220}px 0px`,
                opacity: t,
                boxShadow: "0 40px 90px -30px rgba(0,0,0,0.6), 0 8px 24px rgba(0,0,0,0.25)",
                border: "2px solid rgba(255,255,255,0.18)",
              }}
            />
          );
        })}
        <div
          style={{
            position: "absolute",
            right: 120,
            top: 40,
            padding: "6px 16px",
            borderRadius: 999,
            background: "rgba(8,32,47,0.82)",
            border: "1.5px solid rgba(255,255,255,0.25)",
            color: "rgba(255,255,255,0.85)",
            fontWeight: 600,
            fontSize: 20,
            ...rise(frame, 40, 16),
          }}
        >
          Demo data
        </div>

        <div style={{ position: "absolute", left: COL.x, top: 196, opacity: lockup, scale: String(lerp(0.92, 1, lockup)), transformOrigin: "left center" }}>
          <Wordmark size={92} markSize={86} color="#FFFFFF" />
        </div>
        <div style={{ position: "absolute", left: COL.x, top: 352 }}>
          <div style={{ fontWeight: 800, fontSize: 86, letterSpacing: "-0.035em", lineHeight: 1.04, color: "#FFFFFF", ...rise(frame, 14) }}>
            {COPY.taglineA}
          </div>
          <div
            style={{
              fontFamily: FONTS.serif,
              fontStyle: "italic",
              fontSize: 118,
              lineHeight: 1.0,
              color: COLORS.skyOnDark,
              marginTop: 2,
              ...rise(frame, 19),
            }}
          >
            {COPY.taglineB}
          </div>
        </div>
        <div style={{ position: "absolute", left: COL.x, top: 640, ...rise(frame, 24) }}>
          <div
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 16,
              padding: "26px 60px",
              borderRadius: 999,
              background: "#FFFFFF",
              color: COLORS.accent700,
              fontWeight: 700,
              fontSize: 48,
              boxShadow: "0 0 0 10px rgba(255,255,255,0.08), 0 30px 60px -24px rgba(0,0,0,0.55)",
            }}
          >
            {COPY.cta}
            <ArrowRightIcon size={46} color={COLORS.accent700} stroke={2.8} />
          </div>
        </div>
        <div style={{ position: "absolute", left: COL.x, top: 798, fontWeight: 700, fontSize: 64, letterSpacing: "-0.02em", color: "#FFFFFF", ...rise(frame, 28) }}>
          {COPY.url}
        </div>
        <BluePath d={`M ${COL.x} 892 C ${COL.x + 160} 904, ${COL.x + 360} 904, ${COL.x + 530} 890`} progress={underline} stroke={6} head={false} />
      </AbsoluteFill>
    </AbsoluteFill>
  );
};
