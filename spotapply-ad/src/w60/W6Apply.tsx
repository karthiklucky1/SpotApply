import React from "react";
import { AbsoluteFill, useCurrentFrame } from "remotion";
import { ease, lerp, prog } from "../anim";
import { COLORS, FONTS, SHADOW } from "../brand";
import { Cursor } from "../components/Cursor";
import { CheckBadge, CheckIcon, DocIcon, LockIcon } from "../components/Icons";
import { Mark } from "../components/Logo";
import { BASE_RESUME, CANDIDATE, JOB } from "../story";
import { COL, STAGE, STAGE_VIEW_H, TYPE, WINDOW_BAR } from "./layout";
import { AppWindow, Chapter, Focus60, Note, cameraAt, toCanvas60, STAGE_WINDOW, type Focus } from "./components";
import { TailorFinalWindow } from "./W5Tailor";
import { SCENES60 } from "./timeline60";

// 33.6-44.6s. ILLUSTRATED employer form (it is the employer's page, not
// SpotApply's) with SpotApply's in-browser panel beside it: the fields fill,
// the person's own cursor reads them and clicks Submit. Then LIVE: the
// One-click submit setting, opt-in, with its own explanation.

const WIN = { x: STAGE.x, y: STAGE.y, w: STAGE.w, h: WINDOW_BAR + STAGE_VIEW_H } as const;
const FORM_X = WIN.x + 40;
const FORM_W = 600;
const PANEL = { x: WIN.x + 680, y: WIN.y + WINDOW_BAR + 28, w: 312 } as const;
const ROW_Y = (r: number) => WIN.y + WINDOW_BAR + 132 + r * 104;
const INPUT_H = 54;
const HALF = (FORM_W - 20) / 2;

type FieldDef = { label: string; value: React.ReactNode; row: number; col: 0 | 1 | 2; fill: number };
const FIELDS: FieldDef[] = [
  { label: "First name", value: CANDIDATE.name.split(" ")[0], row: 0, col: 0, fill: 28 },
  { label: "Last name", value: CANDIDATE.name.split(" ")[1], row: 0, col: 1, fill: 31 },
  { label: "Email", value: CANDIDATE.email, row: 1, col: 0, fill: 35 },
  { label: "Phone", value: CANDIDATE.phone, row: 1, col: 1, fill: 39 },
  {
    label: "Resume",
    value: (
      <span style={{ display: "inline-flex", alignItems: "center", gap: 10 }}>
        <DocIcon size={26} color={COLORS.accent} />
        {BASE_RESUME.fileName}
      </span>
    ),
    row: 2,
    col: 2,
    fill: 45,
  },
  { label: "Authorized to work in the US?", value: "Yes", row: 3, col: 2, fill: 51 },
];
const REVIEW_AT = [96, 102, 108, 114, 120, 126];
const PRESS_AT = 146;

const fieldX = (f: FieldDef) => FORM_X + (f.col === 1 ? HALF + 20 : 0);
const fieldW = (f: FieldDef) => (f.col === 2 ? FORM_W : HALF);

// The LIVE settings capture (public/live60/settings.png, 2560x1600).
const SETTINGS_FOCUS: Focus = { x: 800, y: 260, w: 1500 };
const ONE_CLICK = { x: 810, y: 590, w: 1450, h: 230 }; // re-measured after each recapture

const settingsCam = (frame: number) =>
  cameraAt(frame, [
    [0, SETTINGS_FOCUS],
    [1, SETTINGS_FOCUS],
  ]);

export const ApplyFinalWindow: React.FC<{ readonly style?: React.CSSProperties }> = ({ style }) => (
  <AppWindow src="live60/settings.png" focus={settingsCam(SCENES60.apply - 1)} style={style} />
);

const Input: React.FC<{ readonly f: FieldDef; readonly frame: number }> = ({ f, frame }) => {
  const filled = frame >= f.fill;
  const flash = filled ? 1 - prog(frame, f.fill, 16) : 0;
  const reviewed = prog(frame, REVIEW_AT[FIELDS.indexOf(f)], 8);
  return (
    <div style={{ position: "absolute", left: fieldX(f), top: ROW_Y(f.row), width: fieldW(f) }}>
      <div style={{ fontWeight: 600, fontSize: 20, color: COLORS.inkSoft, marginBottom: 8 }}>{f.label}</div>
      <div
        style={{
          height: INPUT_H,
          boxSizing: "border-box",
          borderRadius: 12,
          border: `2px solid ${flash > 0.05 ? COLORS.accent : COLORS.borderStrong}`,
          background: flash > 0 ? `rgba(224,242,254,${flash})` : COLORS.canvas,
          display: "flex",
          alignItems: "center",
          padding: "0 14px",
          fontSize: 23,
          fontWeight: 500,
          color: COLORS.ink,
          whiteSpace: "nowrap",
          overflow: "hidden",
        }}
      >
        {filled ? <span style={{ opacity: prog(frame, f.fill, 8) }}>{f.value}</span> : null}
        {reviewed > 0 ? (
          <span style={{ marginLeft: "auto", opacity: reviewed, scale: String(lerp(0.5, 1, reviewed)) }}>
            <CheckBadge size={28} bg={COLORS.ok} />
          </span>
        ) : null}
      </div>
    </div>
  );
};

const EmployerPage: React.FC<{ readonly frame: number; readonly style?: React.CSSProperties }> = ({ frame, style }) => {
  const press = prog(frame, PRESS_AT, 4) - prog(frame, PRESS_AT + 4, 6);
  const submitted = frame >= PRESS_AT + 3;
  const panel = prog(frame, 56, 14);
  const ticks = FIELDS.map((f) => prog(frame, f.fill + 6, 8));
  return (
    <div style={{ position: "absolute", inset: 0, fontFamily: FONTS.sans, ...style }}>
      <div
        style={{
          position: "absolute",
          left: WIN.x,
          top: WIN.y,
          width: WIN.w,
          height: WIN.h,
          borderRadius: 22,
          overflow: "hidden",
          background: COLORS.canvas,
          border: `2px solid ${COLORS.border}`,
          boxShadow: SHADOW.card,
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
            fontSize: 19,
            fontWeight: 600,
            color: COLORS.muted,
          }}
        >
          {[0, 1, 2].map((i) => (
            <span key={i} style={{ width: 12, height: 12, borderRadius: 6, background: COLORS.borderStrong }} />
          ))}
          <span style={{ marginLeft: 16, display: "inline-flex", alignItems: "center", gap: 8 }}>
            <LockIcon size={20} color={COLORS.muted} /> {JOB.company} · Careers
          </span>
        </div>
      </div>

      <div style={{ position: "absolute", left: FORM_X, top: WIN.y + WINDOW_BAR + 30 }}>
        <div style={{ fontWeight: 700, fontSize: 32, color: COLORS.ink, letterSpacing: "-0.02em" }}>Apply: {JOB.title}</div>
        <div style={{ fontWeight: 500, fontSize: 21, color: COLORS.muted, marginTop: 4 }}>{JOB.company} · Remote (US)</div>
      </div>
      {FIELDS.map((f) => (
        <Input key={f.label} f={f} frame={frame} />
      ))}
      <div
        style={{
          position: "absolute",
          left: FORM_X,
          top: ROW_Y(4) + 6,
          width: FORM_W,
          height: 60,
          borderRadius: 14,
          background: submitted ? COLORS.ok : COLORS.accent,
          color: "#FFFFFF",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          gap: 12,
          fontWeight: 700,
          fontSize: 25,
          scale: String(1 - 0.03 * press),
        }}
      >
        {submitted ? (
          <>
            <CheckIcon size={28} color="#FFFFFF" stroke={3.4} /> Application submitted
          </>
        ) : (
          "Submit application"
        )}
      </div>

      {/* SpotApply's in-browser panel. */}
      <div
        style={{
          position: "absolute",
          left: PANEL.x,
          top: PANEL.y,
          width: PANEL.w,
          boxSizing: "border-box",
          borderRadius: 20,
          background: COLORS.surface,
          border: `2px solid ${COLORS.accentLine}`,
          padding: "20px 22px",
          opacity: panel,
          translate: `${(1 - panel) * 40}px 0px`,
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 10, fontWeight: 700, fontSize: 22, color: COLORS.ink }}>
          <Mark size={30} /> SpotApply
        </div>
        <div style={{ marginTop: 14, fontWeight: 700, fontSize: 21, color: COLORS.accent700 }}>Filled 6 of 6 fields</div>
        <div style={{ marginTop: 10, display: "flex", flexDirection: "column", gap: 8 }}>
          {FIELDS.map((f, i) => (
            <div key={f.label} style={{ display: "flex", alignItems: "center", gap: 10, fontSize: 18, color: COLORS.inkSoft, opacity: 0.35 + 0.65 * ticks[i] }}>
              <CheckIcon size={20} color={ticks[i] > 0.5 ? COLORS.ok : COLORS.borderStrong} stroke={3.4} />
              <span style={{ whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{f.label}</span>
            </div>
          ))}
        </div>
        <div
          style={{
            marginTop: 16,
            padding: "12px 14px",
            borderRadius: 14,
            background: COLORS.canvas,
            border: `2px solid ${COLORS.border}`,
            fontSize: 18,
            fontWeight: 600,
            lineHeight: 1.35,
            color: COLORS.ink,
          }}
        >
          Review the answers, then click Submit yourself.
        </div>
      </div>
    </div>
  );
};

export const ApplyScene60: React.FC = () => {
  const frame = useCurrentFrame();
  const prevOut = prog(frame, 0, 12);
  const enter = prog(frame, 4, 18);

  // The person's cursor reads the fields, then clicks Submit.
  const path: Array<[number, number, number]> = [
    [84, 1500, 980],
    ...FIELDS.map((f, i): [number, number, number] => [REVIEW_AT[i], fieldX(f) + fieldW(f) - 40, ROW_Y(f.row) + 30 + INPUT_H / 2 + 6]),
    [140, FORM_X + FORM_W / 2 + 20, ROW_Y(4) + 6 + 36],
  ];
  let cx = path[0][1];
  let cy = path[0][2];
  for (let i = 1; i < path.length; i++) {
    const [f0, x0, y0] = path[i - 1];
    const [f1, x1, y1] = path[i];
    if (frame >= f0) {
      const t = prog(frame, f0, f1 - f0, ease.inOut);
      cx = lerp(x0, x1, t);
      cy = lerp(y0, y1, t);
    }
  }
  const press = prog(frame, PRESS_AT, 4) - prog(frame, PRESS_AT + 4, 6);
  const ripple = prog(frame, PRESS_AT + 1, 16);
  const cursor = prog(frame, 84, 12) * (1 - prog(frame, 166, 8));

  // One-click submit: the real setting slides in over the form.
  const settings = prog(frame, 172, 20, ease.inOut);
  const sc = settingsCam(frame);
  const settingsRing = prog(frame, 196, 10) * (1 - prog(frame, SCENES60.apply - 12, 6));

  const supportA = prog(frame, 22, 18) * (1 - prog(frame, 164, 8));
  const supportB = prog(frame, 176, 16) * (1 - prog(frame, SCENES60.apply - 10, 9));

  return (
    <AbsoluteFill>
      {prevOut < 1 ? <TailorFinalWindow style={{ opacity: 1 - prevOut, translate: `${-60 * prevOut}px 0px` }} /> : null}
      {settings < 1 ? (
        <EmployerPage frame={frame} style={{ opacity: enter * (1 - 0.6 * settings), translate: `${(1 - enter) * 80}px 0px` }} />
      ) : null}
      <Cursor x={cx} y={cy} opacity={cursor} press={press} ripple={ripple} />

      {settings > 0 ? (
        <AppWindow src="live60/settings.png" focus={sc} style={{ translate: `${(1 - settings) * 1100}px 0px` }} />
      ) : null}
      <Focus60 rect={toCanvas60(STAGE_WINDOW, sc, ONE_CLICK)} t={settingsRing} />
      <Note x={1430} y={100} t={prog(frame, 200, 10) * (1 - prog(frame, SCENES60.apply - 12, 6))} tone="ink">
        Off unless you turn it on
      </Note>

      <Chapter number="04" label="Review" headline={["Filled for you.", "Sent by you."]} enterAt={6} exitAt={SCENES60.apply - 10} />
      <Support t={supportA} text="SpotApply fills the form in your own browser. You check every answer, then click Submit." />
      <Support
        t={supportB}
        text="Prefer one click? Turn on Apply & Submit: each click approves one application, on boards that need no sign-in."
      />
    </AbsoluteFill>
  );
};

const Support: React.FC<{ readonly t: number; readonly text: string }> = ({ t, text }) =>
  t <= 0 ? null : (
    <div
      style={{
        position: "absolute",
        left: COL.x,
        top: 250 + 128 + 26 + 2 * TYPE.headline * 1.04 + 30,
        width: COL.w - 20,
        fontFamily: FONTS.sans,
        fontWeight: 500,
        fontSize: TYPE.support,
        lineHeight: 1.42,
        color: COLORS.inkSoft,
        opacity: t,
        translate: `0px ${(1 - t) * 18}px`,
      }}
    >
      {text}
    </div>
  );
