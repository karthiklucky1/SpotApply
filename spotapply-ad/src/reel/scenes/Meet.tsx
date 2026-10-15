import React from "react";
import { AbsoluteFill, Freeze, OffthreadVideo, spring, staticFile, useCurrentFrame, useVideoConfig } from "remotion";
import { ease, lerp, prog } from "../../anim";
import { COLORS, FONTS } from "../../brand";
import { Wordmark } from "../../components/Logo";
import { DemoTag } from "../ui";

// 11.8-14.4 s. The drop: the brand colour floods in, the wordmark lands, and a
// phone rises with the REAL dashboard on it (a frame of the board recording).
export const MeetScene: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const flood = prog(frame, 0, 10, ease.out);
  const mark = spring({ frame: frame - 4, fps, config: { damping: 10, stiffness: 150, mass: 0.8 } });
  const phone = prog(frame, 14, 26, ease.out);
  return (
    <AbsoluteFill style={{ overflow: "hidden" }}>
      <AbsoluteFill
        style={{
          background: `radial-gradient(circle at 50% 38%, ${COLORS.accentSky} 0%, ${COLORS.accent} 48%, ${COLORS.accent700} 100%)`,
          clipPath: `circle(${lerp(0, 160, flood)}% at 50% 40%)`,
        }}
      />
      {/* light rays */}
      <svg width={1080} height={1920} style={{ position: "absolute", inset: 0, opacity: 0.16 * flood }}>
        {Array.from({ length: 14 }, (_, i) => {
          const a = (i / 14) * Math.PI * 2 + frame / 90;
          return <path key={i} d={`M540 760 L${540 + Math.cos(a) * 1600} ${760 + Math.sin(a) * 1600} L${540 + Math.cos(a + 0.12) * 1600} ${760 + Math.sin(a + 0.12) * 1600} Z`} fill="#FFFFFF" />;
        })}
      </svg>
      <div style={{ position: "absolute", left: 0, right: 0, top: 360, textAlign: "center", fontFamily: FONTS.serif, fontStyle: "italic", fontSize: 92, color: "#E0F2FE", opacity: prog(frame, 2, 8) }}>
        meet
      </div>
      <div style={{ position: "absolute", left: 0, right: 0, top: 480, display: "flex", justifyContent: "center", scale: String(mark), opacity: Math.min(1, mark * 2) }}>
        <div style={{ padding: "22px 40px", borderRadius: 40, background: "#FFFFFF", boxShadow: "0 30px 60px -24px rgba(8,32,47,0.55)" }}>
          <Wordmark size={104} markSize={98} />
        </div>
      </div>
      {/* the phone, with the real dashboard */}
      <div
        style={{
          position: "absolute",
          left: 540 - 270,
          top: lerp(1950, 760, phone),
          width: 540,
          height: 1170,
          borderRadius: 64,
          background: "#0B1620",
          padding: 14,
          boxSizing: "border-box",
          boxShadow: "0 50px 90px -30px rgba(8,32,47,0.7)",
          rotate: `${lerp(8, -3, phone)}deg`,
        }}
      >
        <div style={{ position: "relative", width: "100%", height: "100%", borderRadius: 52, overflow: "hidden", background: "#FFFFFF" }}>
          <Freeze frame={0}>
            <OffthreadVideo src={staticFile("reel/footage/phone_match.mp4")} trimBefore={15} muted style={{ width: 512, height: 512 * (2330 / 1076), maxWidth: "none" }} />
          </Freeze>
          <div style={{ position: "absolute", left: 180, top: 12, width: 152, height: 34, borderRadius: 17, background: "#0B1620" }} />
        </div>
      </div>
      <DemoTag opacity={prog(frame, 20, 10)} dark text="Demo data" />
    </AbsoluteFill>
  );
};
