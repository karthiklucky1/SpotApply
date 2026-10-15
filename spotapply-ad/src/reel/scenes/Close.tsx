import React from "react";
import { AbsoluteFill, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { ease, lerp, prog } from "../../anim";
import { COLORS, FONTS } from "../../brand";
import { Mark } from "../../components/Logo";
import { Room } from "../Room";
import { DemoTag, Sticker } from "../ui";

// 49.6-54 s: the payoff, the same desk in the morning.
export const LessScene: React.FC = () => {
  const frame = useCurrentFrame();
  return (
    <AbsoluteFill>
      <Room mode="morning" expr={frame < 60 ? "relieved" : "happy"} zoom={lerp(1.04, 1.0, prog(frame, 0, 130, ease.inOut))} focusY={900} headBob={Math.sin(frame / 13) * 3} id="less" />
      <Sticker text="Less time on *repetitive work.*" at={6} x={66} y={262} size={52} tone="white" rotate={-2} />
      <Sticker text="More time on" at={62} x={66} y={262 + 96} size={52} tone="white" rotate={1.5} />
      <Sticker text="*meaningful applications.*" at={68} x={66} y={262 + 192} size={52} tone="white" rotate={-1.5} />
      <DemoTag opacity={1} text="Illustration" />
    </AbsoluteFill>
  );
};

// 54-60 s: the end card. Lands by 54.8 s, then holds perfectly still. Kept
// left of the platforms' right-hand buttons and above their caption row.
export const EndScene: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const sheet = prog(frame, 0, 12, ease.out);
  const pop = (at: number) => spring({ frame: frame - at, fps, config: { damping: 13, stiffness: 160, mass: 0.7 }, durationInFrames: 18 });
  const rise = (at: number) => {
    const t = prog(frame, at, 12, ease.out);
    return { opacity: t, translate: `0px ${(1 - t) * 30}px` };
  };
  const cx = 500;
  return (
    <AbsoluteFill style={{ overflow: "hidden" }}>
      <AbsoluteFill
        style={{
          background: `radial-gradient(circle at 30% 30%, #12456A 0%, ${COLORS.inkDeep} 62%)`,
          clipPath: `inset(${(1 - sheet) * 100}% 0 0 0)`,
        }}
      />
      <div style={{ position: "absolute", left: cx, top: 360, translate: "-50% 0", scale: String(pop(2)) }}>
        <Mark size={132} />
      </div>
      <div style={{ position: "absolute", left: 70, width: 860, top: 560, textAlign: "center", fontFamily: FONTS.sans, color: "#FFFFFF" }}>
        <div style={{ fontWeight: 800, fontSize: 98, lineHeight: 1.02, letterSpacing: "-0.035em", ...rise(5) }}>Make your next</div>
        <div style={{ fontWeight: 800, fontSize: 98, lineHeight: 1.02, letterSpacing: "-0.035em", ...rise(8) }}>application</div>
        <div style={{ fontFamily: FONTS.serif, fontStyle: "italic", fontSize: 150, lineHeight: 1.0, color: COLORS.skyOnDark, ...rise(11) }}>count.</div>
      </div>
      <div style={{ position: "absolute", left: 70, width: 860, top: 1020, textAlign: "center", fontFamily: FONTS.sans, fontWeight: 700, fontSize: 54, letterSpacing: "-0.02em", color: "#FFFFFF", ...rise(14) }}>
        SpotApply <span style={{ color: COLORS.skyOnDark, padding: "0 10px" }}>•</span> spotapply.ai
      </div>
      <div style={{ position: "absolute", left: 70, width: 860, top: 1130, display: "flex", justifyContent: "center", ...rise(17) }}>
        <div style={{ padding: "16px 36px", borderRadius: 999, border: "2.5px solid rgba(255,255,255,0.55)", color: "rgba(255,255,255,0.9)", fontFamily: FONTS.sans, fontWeight: 600, fontSize: 36, whiteSpace: "nowrap" }}>
          Follow SpotApply
        </div>
      </div>
    </AbsoluteFill>
  );
};
