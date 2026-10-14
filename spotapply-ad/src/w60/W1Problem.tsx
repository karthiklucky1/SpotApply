import React from "react";
import { AbsoluteFill, useCurrentFrame } from "remotion";
import { ease, lerp, prog, typed } from "../anim";
import { COLORS, FONTS, SHADOW } from "../brand";
import { DocIcon } from "../components/Icons";
import { InitialTile } from "../components/ui";
import { BASE_RESUME, CANDIDATE } from "../story";
import { Chapter, DarkBg } from "./components";
import { SCENES60 } from "./timeline60";

// 0-5.4s. Dark open: six application forms from six (fictional) employers
// fill with the same name and email, one after another, then fold into a
// single resume, which the intro opens.

const COMPANIES = [
  { name: "Northpoint Labs", initial: "N" },
  { name: "Cadence Robotics", initial: "C" },
  { name: "Meridian Freight", initial: "M" },
  { name: "Harbor Analytics", initial: "H" },
  { name: "Vector Health", initial: "V" },
  { name: "Orchard Systems", initial: "O" },
] as const;

const FORM_W = 312;
const FORM_H = 300;
const GRID = { x: 780, y: 214, gx: 354, gy: 336 } as const;
export const FILE_CENTER = { x: 1290, y: 548 } as const;

const START = (i: number) => 8 + i * 15;
export const FREEZE = 112;

const MiniField: React.FC<{ readonly label: string; readonly value: string; readonly active: boolean }> = ({
  label,
  value,
  active,
}) => (
  <div>
    <div style={{ fontSize: 17, fontWeight: 600, color: COLORS.muted, marginBottom: 5 }}>{label}</div>
    <div
      style={{
        height: 40,
        borderRadius: 10,
        border: `2px solid ${active ? COLORS.accent : COLORS.border}`,
        display: "flex",
        alignItems: "center",
        padding: "0 12px",
        fontSize: 19,
        fontWeight: 500,
        color: COLORS.ink,
        whiteSpace: "nowrap",
        overflow: "hidden",
      }}
    >
      {value}
      {active ? <span style={{ width: 2, height: 22, marginLeft: 2, background: COLORS.accent }} /> : null}
    </div>
  </div>
);

export const MiniForm: React.FC<{ readonly i: number; readonly frame: number; readonly style?: React.CSSProperties }> = ({
  i,
  frame,
  style,
}) => {
  const c = COMPANIES[i];
  const s = START(i);
  const name = typed(CANDIDATE.name, frame, s, 1.3);
  const email = typed(CANDIDATE.email, frame, s + 8, 2.2);
  const live = frame < FREEZE;
  const typingName = live && frame >= s && name.length < CANDIDATE.name.length;
  const typingEmail = live && frame >= s + 8 && email.length < CANDIDATE.email.length;
  return (
    <div
      style={{
        position: "absolute",
        width: FORM_W,
        height: FORM_H,
        boxSizing: "border-box",
        borderRadius: 20,
        background: COLORS.canvas,
        boxShadow: SHADOW.lift,
        padding: "18px 20px",
        display: "flex",
        flexDirection: "column",
        gap: 12,
        fontFamily: FONTS.sans,
        ...style,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
        <InitialTile letter={c.initial} size={38} />
        <div style={{ fontWeight: 700, fontSize: 19, color: COLORS.ink, whiteSpace: "nowrap" }}>{c.name}</div>
      </div>
      <MiniField label="Full name" value={name} active={typingName} />
      <MiniField label="Email" value={email} active={typingEmail} />
      <div style={{ display: "flex", flexDirection: "column", gap: 7, marginTop: 2 }}>
        <div style={{ fontSize: 17, fontWeight: 600, color: COLORS.muted }}>Work history</div>
        <div style={{ height: 9, width: "92%", borderRadius: 5, background: COLORS.surface2 }} />
        <div style={{ height: 9, width: "70%", borderRadius: 5, background: COLORS.surface2 }} />
      </div>
    </div>
  );
};

export const formPos = (i: number) => ({
  x: GRID.x + (i % 3) * GRID.gx,
  y: GRID.y + Math.floor(i / 3) * GRID.gy,
});

export const FileChip60: React.FC<{ readonly style?: React.CSSProperties }> = ({ style }) => (
  <div
    style={{
      position: "absolute",
      left: FILE_CENTER.x,
      top: FILE_CENTER.y,
      translate: "-50% -50%",
      display: "flex",
      alignItems: "center",
      gap: 16,
      padding: "22px 34px",
      borderRadius: 24,
      background: COLORS.canvas,
      boxShadow: "0 30px 60px -20px rgba(0,0,0,0.5)",
      fontFamily: FONTS.sans,
      fontWeight: 600,
      fontSize: 30,
      color: COLORS.ink,
      whiteSpace: "nowrap",
      ...style,
    }}
  >
    <DocIcon size={42} color={COLORS.accent} />
    {BASE_RESUME.fileName}
  </div>
);

export const ProblemScene: React.FC = () => {
  const frame = useCurrentFrame();
  const fold = prog(frame, 124, 22, ease.inOut);
  const file = prog(frame, 136, 14);

  return (
    <AbsoluteFill>
      <DarkBg />
      <Chapter
        dark
        label="Applying today"
        headline={["Another search.", "Another form."]}
        support="The same name, email and work history, typed in again and again."
        enterAt={2}
        exitAt={SCENES60.problem - 12}
      />
      {COMPANIES.map((_, i) => {
        const p = formPos(i);
        const enter = prog(frame, i * 3, 16);
        const cx = p.x + FORM_W / 2;
        const cy = p.y + FORM_H / 2;
        return (
          <MiniForm
            key={i}
            i={i}
            frame={frame}
            style={{
              left: p.x,
              top: p.y,
              opacity: enter * (1 - prog(frame, 132, 12)),
              translate: `${lerp(0, FILE_CENTER.x - cx, fold)}px ${lerp((1 - enter) * 50, FILE_CENTER.y - cy, fold)}px`,
              scale: String(lerp(1, 0.5, fold)),
              rotate: `${lerp(0, (i - 2.5) * 4, fold)}deg`,
            }}
          />
        );
      })}
      <FileChip60 style={{ opacity: file, scale: String(lerp(0.8, 1, file)) }} />
    </AbsoluteFill>
  );
};
