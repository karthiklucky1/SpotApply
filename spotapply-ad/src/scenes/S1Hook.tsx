import React from "react";
import { AbsoluteFill, useCurrentFrame } from "remotion";
import { prog, typed } from "../anim";
import { COLORS, FONTS, SAFE, SHADOW } from "../brand";
import { COPY } from "../copy";
import { Headline } from "../components/Headline";
import { InitialTile } from "../components/ui";
import { CANDIDATE, HOOK_FORMS } from "../story";
import { SCENES } from "../timeline";

// 0-3s. Three overlapping application forms ask for the same details; the
// same name and email get typed into each one in the same rhythm, then
// everything stops.

export const FORM_W = SAFE.width;
export const FORM_H = 352;
export const HOOK_LAYOUT = [
  { x: SAFE.x - 14, y: 612, rotate: -2.2 },
  { x: SAFE.x + 16, y: 900, rotate: 1.6 },
  { x: SAFE.x - 6, y: 1188, rotate: -1.0 },
] as const;

// When each form starts being typed into (local frames).
const STARTS = [6, 30, 54] as const;
const NAME_CPF = 1; // chars per frame
const EMAIL_START = 11;
const EMAIL_CPF = 1.6;
export const HOOK_FREEZE = 80;

const Field: React.FC<{
  readonly label: string;
  readonly value: string;
  readonly caret: boolean;
  readonly active: boolean;
}> = ({ label, value, caret, active }) => (
  <div>
    <div
      style={{
        fontFamily: FONTS.sans,
        fontWeight: 600,
        fontSize: 25,
        color: COLORS.inkSoft,
        marginBottom: 8,
      }}
    >
      {label}
    </div>
    <div
      style={{
        height: 66,
        borderRadius: 14,
        border: `2px solid ${active ? COLORS.accent : COLORS.borderStrong}`,
        background: COLORS.canvas,
        display: "flex",
        alignItems: "center",
        padding: "0 20px",
        fontFamily: FONTS.sans,
        fontSize: 31,
        fontWeight: 500,
        color: COLORS.ink,
        whiteSpace: "nowrap",
        overflow: "hidden",
      }}
    >
      {value}
      {caret ? (
        <span
          style={{
            display: "inline-block",
            width: 3,
            height: 36,
            marginLeft: 3,
            background: COLORS.accent,
          }}
        />
      ) : null}
    </div>
  </div>
);

export const HookForm: React.FC<{
  readonly index: number;
  /** Frame on the hook's own clock; past HOOK_FREEZE everything is final. */
  readonly frame: number;
  readonly style?: React.CSSProperties;
}> = ({ index, frame, style }) => {
  const form = HOOK_FORMS[index];
  const start = STARTS[index];
  const name = typed(CANDIDATE.name, frame, start, NAME_CPF);
  const email = typed(CANDIDATE.email, frame, start + EMAIL_START, EMAIL_CPF);
  const typingName = frame >= start && name.length < CANDIDATE.name.length;
  const typingEmail =
    frame >= start + EMAIL_START && email.length < CANDIDATE.email.length;
  const between =
    frame >= start && !typingName && !typingEmail && email.length === 0;
  const live = frame < HOOK_FREEZE;
  const active = live && frame >= start - 2 && frame < start + 26;
  const lift = active ? prog(frame, start - 2, 6) : 0;

  return (
    <div
      style={{
        position: "absolute",
        width: FORM_W,
        height: FORM_H,
        borderRadius: 30,
        background: COLORS.canvas,
        border: `2px solid ${active ? COLORS.accentLine : COLORS.border}`,
        boxShadow: active ? SHADOW.lift : SHADOW.card,
        padding: "30px 36px",
        boxSizing: "border-box",
        display: "flex",
        flexDirection: "column",
        gap: 18,
        scale: String(1 + 0.018 * lift),
        ...style,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 18 }}>
        <InitialTile letter={form.initial} size={58} />
        <div style={{ fontFamily: FONTS.sans, fontWeight: 700, fontSize: 32, color: COLORS.ink }}>
          {form.company}
        </div>
        <div
          style={{
            marginLeft: "auto",
            fontFamily: FONTS.sans,
            fontWeight: 600,
            fontSize: 24,
            color: COLORS.muted,
          }}
        >
          Job application
        </div>
      </div>
      <Field
        label="Full name"
        value={name}
        caret={live && (typingName || between)}
        active={live && (typingName || between)}
      />
      <Field
        label="Email"
        value={email}
        caret={live && typingEmail}
        active={live && typingEmail}
      />
    </div>
  );
};

export const HookScene: React.FC = () => {
  const frame = useCurrentFrame();

  return (
    <AbsoluteFill>
      {HOOK_LAYOUT.map((l, i) => {
        const enter = prog(frame, i * 3, 16);
        return (
          <HookForm
            key={i}
            index={i}
            frame={frame}
            style={{
              left: l.x,
              top: l.y,
              rotate: `${l.rotate}deg`,
              translate: `0px ${(1 - enter) * 60}px`,
            }}
          />
        );
      })}
      <Headline lines={COPY.hook} enterAt={0} exitAt={SCENES.hook - 9} />
    </AbsoluteFill>
  );
};

