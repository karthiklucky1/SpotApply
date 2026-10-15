import React from "react";
import { useCurrentFrame } from "remotion";
import { FONTS } from "../brand";
import { blinkAt, type Expr, Person } from "./Character";

// The everyday setting: a bedroom desk. Night for the frustration (screen glow,
// a 1:47 AM clock, sticky notes), morning for the payoff (sun, coffee).
// Flat illustration in the brand's palette, full-bleed 1080 x 1920.

type Mode = "night" | "morning";

const PAL = {
  night: { wallTop: "#0E2338", wallBot: "#163552", sky1: "#0A1828", sky2: "#1E3D5C", frame: "#24496A", desk: "#3A291F", deskTop: "#4B3528", shelf: "#3A2A22", glow: "#7DD3FC", plant: "#2F6B4F" },
  morning: { wallTop: "#FDEFE2", wallBot: "#F6DCC8", sky1: "#9ED8F7", sky2: "#E3F4FD", frame: "#FFFFFF", desk: "#C99A6E", deskTop: "#D8AC80", shelf: "#B98A5F", glow: "#FFF5D6", plant: "#3E9A6A" },
} as const;

const StickyNote: React.FC<{ x: number; y: number; text: string; rot: number; color: string }> = ({ x, y, text, rot, color }) => (
  <g transform={`translate(${x} ${y}) rotate(${rot})`}>
    <rect x={0} y={0} width={190} height={150} rx={6} fill={color} />
    <rect x={0} y={0} width={190} height={22} rx={6} fill="rgba(0,0,0,0.08)" />
    <text x={18} y={70} fontFamily={FONTS.sans} fontWeight={700} fontSize={27} fill="#2B2B2B">
      {text.split("\n").map((l, i) => (
        <tspan key={i} x={18} dy={i === 0 ? 0 : 34}>
          {l}
        </tspan>
      ))}
    </text>
  </g>
);

export const Room: React.FC<{
  readonly mode: Mode;
  readonly expr: Expr;
  readonly zoom?: number; // 1 = the whole room
  readonly focusY?: number; // where the zoom centres (canvas y)
  readonly headBob?: number;
  readonly id?: string;
}> = ({ mode, expr, zoom = 1, focusY = 960, headBob = 0, id = "room" }) => {
  const frame = useCurrentFrame();
  const P = PAL[mode];
  const night = mode === "night";
  const steam = (k: number) => {
    const t = (frame * 0.9 + k * 23) % 60;
    return { opacity: Math.sin((t / 60) * Math.PI) * 0.6, dy: -t * 1.1 };
  };
  return (
    <div style={{ position: "absolute", inset: 0, overflow: "hidden" }}>
      <div style={{ position: "absolute", inset: 0, scale: String(zoom), transformOrigin: `540px ${focusY}px` }}>
        <svg width={1080} height={1920} viewBox="0 0 1080 1920" style={{ position: "absolute", inset: 0 }}>
          <defs>
            <linearGradient id={`${id}-wall`} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={P.wallTop} />
              <stop offset="100%" stopColor={P.wallBot} />
            </linearGradient>
            <linearGradient id={`${id}-sky`} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={P.sky1} />
              <stop offset="100%" stopColor={P.sky2} />
            </linearGradient>
            <radialGradient id={`${id}-lamp`} cx="50%" cy="50%" r="50%">
              <stop offset="0%" stopColor={night ? "#FFCF8A" : "#FFF3D6"} stopOpacity={night ? 0.55 : 0.35} />
              <stop offset="100%" stopColor="#FFCF8A" stopOpacity={0} />
            </radialGradient>
            <radialGradient id={`${id}-screen`} cx="50%" cy="100%" r="80%">
              <stop offset="0%" stopColor={P.glow} stopOpacity={night ? 0.42 : 0.18} />
              <stop offset="100%" stopColor={P.glow} stopOpacity={0} />
            </radialGradient>
          </defs>
          <rect width={1080} height={1920} fill={`url(#${id}-wall)`} />
          {/* window */}
          <rect x={96} y={330} width={380} height={470} rx={18} fill={P.frame} />
          <rect x={114} y={348} width={344} height={434} rx={10} fill={`url(#${id}-sky)`} />
          {night ? (
            <g>
              <circle cx={380} cy={440} r={60} fill="#F8F1D8" opacity={0.12} />
              <circle cx={380} cy={440} r={34} fill="#F8F1D8" />
              <circle cx={394} cy={430} r={30} fill="#13304B" />
              {[[160, 400], [220, 470], [300, 380], [190, 560], [420, 560], [260, 520]].map(([x, y], i) => (
                <circle key={i} cx={x} cy={y} r={2.6} fill="#FFFFFF" opacity={0.7 + 0.3 * Math.sin(frame / 9 + i)} />
              ))}
              <path d="M114 782 L114 690 L160 690 L160 650 L210 650 L210 700 L250 700 L250 620 L300 620 L300 680 L350 680 L350 640 L400 640 L400 700 L458 700 L458 782 Z" fill="#0A1624" />
              {[[126, 708], [134, 728], [176, 666], [266, 640], [276, 664], [312, 700], [362, 660], [372, 690], [416, 720]].map(([x, y], i) => (
                <rect key={i} x={x} y={y} width={9} height={12} rx={1.5} fill="#F6C177" opacity={0.85} />
              ))}
            </g>
          ) : (
            <g>
              <circle cx={380} cy={440} r={90} fill="#FFF6C9" opacity={0.5} />
              <circle cx={380} cy={440} r={46} fill="#FFE27A" />
              <ellipse cx={220} cy={520} rx={70} ry={26} fill="#FFFFFF" opacity={0.9} />
              <ellipse cx={270} cy={505} rx={50} ry={22} fill="#FFFFFF" opacity={0.9} />
              <path d="M114 782 L114 700 Q200 660 286 700 Q372 740 458 690 L458 782 Z" fill="#8FD0A9" />
            </g>
          )}
          <rect x={96} y={565} width={380} height={12} fill={P.frame} />
          <rect x={279} y={330} width={12} height={470} fill={P.frame} />
          {/* string lights */}
          <path d="M0 260 Q540 380 1080 250" stroke={night ? "#3B5872" : "#C9A98F"} strokeWidth={3} fill="none" />
          {Array.from({ length: 13 }, (_, i) => {
            const x = 40 + i * 83;
            const tt = x / 1080;
            const y = (1 - tt) * (1 - tt) * 260 + 2 * (1 - tt) * tt * 380 + tt * tt * 250 + 14;
            return (
              <g key={i}>
                {night ? <circle cx={x} cy={y} r={22} fill="#FFD58A" opacity={0.18 + 0.08 * Math.sin(frame / 7 + i)} /> : null}
                <circle cx={x} cy={y} r={8} fill={night ? "#FFD58A" : "#F3C88F"} />
              </g>
            );
          })}
          {/* shelf with books and a plant */}
          <rect x={660} y={560} width={340} height={16} rx={4} fill={P.shelf} />
          {[["#E4795E", 40], ["#5FA8D3", 30], ["#F2C14E", 44], ["#7E6CC8", 34], ["#4CAF8C", 28]].map(([c, h], i) => (
            <rect key={i} x={680 + i * 30} y={560 - (h as number) - 30} width={24} height={(h as number) + 30} rx={3} fill={c as string} opacity={night ? 0.7 : 1} />
          ))}
          <rect x={880} y={500} width={60} height={60} rx={8} fill={night ? "#A0623E" : "#D9824F"} />
          <path d="M910 500 Q870 440 900 400 M910 500 Q930 430 960 420 M910 500 Q905 450 915 380" stroke={P.plant} strokeWidth={12} strokeLinecap="round" fill="none" />
          {/* sticky notes */}
          {night ? (
            <g>
              <StickyNote x={640} y={640} text={"apply to\n10 jobs"} rot={-4} color="#FDE68A" />
              <StickyNote x={840} y={680} text={"update\nresume"} rot={5} color="#FBCFE8" />
            </g>
          ) : (
            <StickyNote x={720} y={650} text={"interview\nprep 3pm"} rot={-3} color="#BBF7D0" />
          )}
          {/* lamp + glow */}
          <circle cx={170} cy={1120} r={300} fill={`url(#${id}-lamp)`} />
        </svg>
        {/* the person, behind the laptop */}
        <div style={{ position: "absolute", left: 540 - 300, top: 560 + headBob, width: 600, height: 600 }}>
          <Person expr={expr} size={600} blink={blinkAt(frame, 13)} glow={night ? "#7DD3FC" : undefined} id={`${id}-p`} />
        </div>
        <svg width={1080} height={1920} viewBox="0 0 1080 1920" style={{ position: "absolute", inset: 0 }}>
          {/* screen light on the scene */}
          <ellipse cx={540} cy={1180} rx={520} ry={360} fill={`url(#${id}-screen)`} />
          {/* desk */}
          <rect x={0} y={1330} width={1080} height={590} fill={P.desk} />
          <rect x={0} y={1318} width={1080} height={28} fill={P.deskTop} />
          {/* laptop, seen from behind */}
          <path d="M300 1090 L780 1090 Q796 1090 798 1106 L812 1318 L268 1318 L282 1106 Q284 1090 300 1090 Z" fill={night ? "#9AA9B8" : "#C7D2DD"} />
          <path d="M300 1090 L780 1090 Q796 1090 798 1106 L800 1130 L280 1130 L282 1106 Q284 1090 300 1090 Z" fill="#FFFFFF" opacity={0.18} />
          <circle cx={540} cy={1200} r={26} fill="#FFFFFF" opacity={0.22} />
          <rect x={230} y={1312} width={620} height={18} rx={9} fill={night ? "#7D8C9B" : "#AEBBC8"} />
          {/* clock */}
          <g transform="translate(70 1210)">
            <rect width={170} height={100} rx={16} fill={night ? "#111C27" : "#2A2F36"} />
            <text x={85} y={64} textAnchor="middle" fontFamily={FONTS.mono} fontWeight={700} fontSize={40} fill={night ? "#FF7A59" : "#9BE7B4"}>
              {night ? "1:47" : "9:12"}
            </text>
            <text x={150} y={88} textAnchor="end" fontFamily={FONTS.sans} fontWeight={700} fontSize={16} fill={night ? "#FF7A59" : "#9BE7B4"}>
              AM
            </text>
          </g>
          {/* mug + steam */}
          <g transform="translate(850 1210)">
            <rect x={0} y={0} width={110} height={112} rx={18} fill={night ? "#E4E8EE" : "#FFFFFF"} />
            <path d="M110 30 Q150 30 150 58 Q150 86 110 86" stroke={night ? "#E4E8EE" : "#FFFFFF"} strokeWidth={14} fill="none" />
            <rect x={14} y={38} width={82} height={20} rx={6} fill={night ? "#0E7FC0" : "#0EA5E9"} opacity={0.85} />
            {[0, 1, 2].map((k) => {
              const s = steam(k);
              return (
                <path key={k} d={`M${30 + k * 25} ${-10 + s.dy} q-10 -18 0 -36 q10 -18 0 -36`} stroke={night ? "#CFE2F2" : "#B8B8B8"} strokeWidth={5} strokeLinecap="round" fill="none" opacity={s.opacity} />
              );
            })}
          </g>
        </svg>
      </div>
    </div>
  );
};
