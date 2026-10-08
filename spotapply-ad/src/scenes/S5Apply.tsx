import React from "react";
import { AbsoluteFill, useCurrentFrame } from "remotion";
import { ease, lerp, prog } from "../anim";
import { COLORS, FONTS, SAFE, SHADOW } from "../brand";
import { COPY } from "../copy";
import { Cursor } from "../components/Cursor";
import { Headline } from "../components/Headline";
import { CheckBadge, CheckIcon, DocIcon, LockIcon } from "../components/Icons";
import { Mark } from "../components/Logo";
import { BASE_RESUME, CANDIDATE, JOB } from "../story";
import { SCENES } from "../timeline";
import { JobChip, ResumeDoc } from "./S4Tailor";

// 16-22s. SpotApply fills the employer's form in the browser and attaches
// the tailored resume. Then the person reads it over and clicks Submit
// themselves; SpotApply never does. The only cursor on screen is theirs.

export const FORM = { x: SAFE.x, y: 604, w: SAFE.width, h: 846, bar: 60 } as const;
const PAD = 36;
const INNER_W = FORM.w - 2 * PAD;
// Absolute rows (global y) so the cursor and the flying file can target them.
const BANNER_Y = FORM.y + FORM.bar + 30;
const TITLE_Y = BANNER_Y + 60 + 26;
const ROW_Y = [TITLE_Y + 76, TITLE_Y + 76 + 120, TITLE_Y + 76 + 240, TITLE_Y + 76 + 360];
const INPUT_OFFSET = 34; // label height + gap
const INPUT_H = 66;
const BUTTON_Y = ROW_Y[3] + INPUT_OFFSET + INPUT_H + 32;
const BUTTON_H = 78;

const inputCenterY = (row: number) => ROW_Y[row] + INPUT_OFFSET + INPUT_H / 2;

// When SpotApply fills each field (local frames), and when the person's
// cursor reviews it.
const FILL_AT = [26, 29, 33, 37] as const; // first, last, email, phone
const ATTACH_AT = 50; // resume lands in its field
const BANNER_AT = 56;
const REVIEW_AT = [110, 118, 126, 134] as const; // rows 0-3
const PRESS_AT = 152;

const Label: React.FC<{ readonly children: React.ReactNode; readonly y: number; readonly x?: number }> = ({
  children,
  y,
  x = FORM.x + PAD,
}) => (
  <div
    style={{
      position: "absolute",
      left: x,
      top: y,
      fontFamily: FONTS.sans,
      fontWeight: 600,
      fontSize: 25,
      color: COLORS.inkSoft,
    }}
  >
    {children}
  </div>
);

const Input: React.FC<{
  readonly x: number;
  readonly y: number;
  readonly w: number;
  readonly filledAt: number;
  readonly reviewedAt: number;
  readonly frame: number;
  readonly children: React.ReactNode;
}> = ({ x, y, w, filledAt, reviewedAt, frame, children }) => {
  const filled = frame >= filledAt;
  const flash = filled ? 1 - prog(frame, filledAt, 16) : 0;
  const pop = prog(frame, filledAt, 8);
  const reviewed = prog(frame, reviewedAt, 8);
  return (
    <div
      style={{
        position: "absolute",
        left: x,
        top: y,
        width: w,
        height: INPUT_H,
        boxSizing: "border-box",
        borderRadius: 14,
        border: `2px solid ${flash > 0.05 ? COLORS.accent : COLORS.borderStrong}`,
        background: flash > 0 ? `rgba(224,242,254,${flash})` : COLORS.canvas,
        boxShadow: flash > 0 ? `0 0 0 ${6 * flash}px rgba(14,165,233,0.18)` : undefined,
        display: "flex",
        alignItems: "center",
        padding: "0 18px",
        fontFamily: FONTS.sans,
        fontSize: 31,
        fontWeight: 500,
        color: COLORS.ink,
        whiteSpace: "nowrap",
        overflow: "hidden",
      }}
    >
      {filled ? (
        <div style={{ opacity: pop, translate: `0px ${(1 - pop) * 10}px`, display: "flex", alignItems: "center", gap: 12 }}>
          {children}
        </div>
      ) : null}
      {reviewed > 0 ? (
        <div style={{ marginLeft: "auto", opacity: reviewed, scale: String(lerp(0.5, 1, reviewed)) }}>
          <CheckBadge size={36} bg={COLORS.ok} />
        </div>
      ) : null}
    </div>
  );
};

/** The employer's application form. `frame` drives filling, review and
 * the submit state; pass a large value for the submitted form. */
export const ApplyForm: React.FC<{
  readonly frame: number;
  readonly showAttachment?: boolean;
  readonly style?: React.CSSProperties;
}> = ({ frame, showAttachment = true, style }) => {
  const banner = prog(frame, BANNER_AT, 12);
  const press = prog(frame, PRESS_AT, 4) - prog(frame, PRESS_AT + 4, 6);
  const submitted = frame >= PRESS_AT + 3;
  const half = (INNER_W - 24) / 2;

  return (
    <div style={{ position: "absolute", inset: 0, ...style }}>
      <div
        style={{
          position: "absolute",
          left: FORM.x,
          top: FORM.y,
          width: FORM.w,
          height: FORM.h,
          borderRadius: 26,
          overflow: "hidden",
          background: COLORS.canvas,
          border: `2px solid ${COLORS.border}`,
          boxShadow: SHADOW.card,
        }}
      >
        <div
          style={{
            height: FORM.bar,
            display: "flex",
            alignItems: "center",
            gap: 10,
            padding: "0 24px",
            background: COLORS.surface2,
            borderBottom: `2px solid ${COLORS.border}`,
            fontFamily: FONTS.sans,
            fontSize: 25,
            fontWeight: 600,
            color: COLORS.muted,
          }}
        >
          {[0, 1, 2].map((i) => (
            <span key={i} style={{ width: 14, height: 14, borderRadius: 7, background: COLORS.borderStrong }} />
          ))}
          <span style={{ marginLeft: 14, display: "inline-flex", alignItems: "center", gap: 10 }}>
            <LockIcon size={24} color={COLORS.muted} />
            {JOB.company} · Careers
          </span>
        </div>
      </div>

      {/* SpotApply's in-browser note: filled, now it's the person's turn. */}
      <div
        style={{
          position: "absolute",
          left: FORM.x + PAD,
          top: BANNER_Y,
          width: INNER_W,
          height: 60,
          boxSizing: "border-box",
          borderRadius: 16,
          background: COLORS.accentTint,
          border: `2px solid ${COLORS.accentLine}`,
          display: "flex",
          alignItems: "center",
          gap: 14,
          padding: "0 18px",
          fontFamily: FONTS.sans,
          fontWeight: 600,
          fontSize: 26,
          color: COLORS.accent700,
          opacity: banner,
          translate: `0px ${(1 - banner) * -12}px`,
          whiteSpace: "nowrap",
        }}
      >
        <Mark size={36} />
        Filled by SpotApply. Review before you submit.
      </div>

      <div
        style={{
          position: "absolute",
          left: FORM.x + PAD,
          top: TITLE_Y,
          fontFamily: FONTS.sans,
          fontWeight: 700,
          fontSize: 40,
          color: COLORS.ink,
          letterSpacing: "-0.02em",
        }}
      >
        Apply: {JOB.title}
      </div>

      <Label y={ROW_Y[0]}>First name</Label>
      <Label y={ROW_Y[0]} x={FORM.x + PAD + half + 24}>
        Last name
      </Label>
      <Input x={FORM.x + PAD} y={ROW_Y[0] + INPUT_OFFSET} w={half} filledAt={FILL_AT[0]} reviewedAt={REVIEW_AT[0]} frame={frame}>
        {CANDIDATE.name.split(" ")[0]}
      </Input>
      <Input
        x={FORM.x + PAD + half + 24}
        y={ROW_Y[0] + INPUT_OFFSET}
        w={half}
        filledAt={FILL_AT[1]}
        reviewedAt={REVIEW_AT[0]}
        frame={frame}
      >
        {CANDIDATE.name.split(" ")[1]}
      </Input>

      <Label y={ROW_Y[1]}>Email</Label>
      <Input x={FORM.x + PAD} y={ROW_Y[1] + INPUT_OFFSET} w={INNER_W} filledAt={FILL_AT[2]} reviewedAt={REVIEW_AT[1]} frame={frame}>
        {CANDIDATE.email}
      </Input>

      <Label y={ROW_Y[2]}>Phone</Label>
      <Input x={FORM.x + PAD} y={ROW_Y[2] + INPUT_OFFSET} w={INNER_W} filledAt={FILL_AT[3]} reviewedAt={REVIEW_AT[2]} frame={frame}>
        {CANDIDATE.phone}
      </Input>

      <Label y={ROW_Y[3]}>Resume</Label>
      <Input
        x={FORM.x + PAD}
        y={ROW_Y[3] + INPUT_OFFSET}
        w={INNER_W}
        filledAt={showAttachment ? ATTACH_AT : 1e9}
        reviewedAt={REVIEW_AT[3]}
        frame={frame}
      >
        <DocIcon size={34} color={COLORS.accent} />
        {BASE_RESUME.fileName}
      </Input>

      <div
        style={{
          position: "absolute",
          left: FORM.x + PAD,
          top: BUTTON_Y,
          width: INNER_W,
          height: BUTTON_H,
          borderRadius: 16,
          background: submitted ? COLORS.ok : COLORS.accent,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          gap: 14,
          fontFamily: FONTS.sans,
          fontWeight: 700,
          fontSize: 32,
          color: "#FFFFFF",
          scale: String(1 - 0.03 * press),
          boxShadow: "0 14px 28px -14px rgba(0,119,194,0.6)",
        }}
      >
        {submitted ? (
          <>
            <CheckIcon size={36} color="#FFFFFF" stroke={3.4} />
            Submitted
          </>
        ) : (
          "Submit application"
        )}
      </div>
    </div>
  );
};

export const ApplyScene: React.FC = () => {
  const frame = useCurrentFrame();

  // The tailored resume folds into a file...
  const fold = prog(frame, 0, 16, ease.inOut);
  const formIn = prog(frame, 6, 22);
  // ...which flies into the Resume field.
  const fileIn = prog(frame, 10, 10);
  const fly = prog(frame, 30, 20, ease.inOut);
  const fileGone = frame >= ATTACH_AT;

  const fileStart = { x: 540, y: 1010 };
  const fileEnd = { x: FORM.x + PAD + 18 + 210, y: inputCenterY(3) };

  // The person's cursor: enters, reads down the fields, clicks Submit.
  const enter = prog(frame, 94, 16) * (1 - prog(frame, 166, 10));
  const path: Array<[number, number, number]> = [
    // [frame, x, y]
    [94, 1010, 1660],
    [110, 900, inputCenterY(0) + 8],
    [118, 900, inputCenterY(1) + 8],
    [126, 900, inputCenterY(2) + 8],
    [134, 900, inputCenterY(3) + 8],
    [148, 560, BUTTON_Y + BUTTON_H / 2 + 6],
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
  const ripple = prog(frame, PRESS_AT + 1, 16, ease.out);

  return (
    <AbsoluteFill>
      <JobChip />

      {formIn > 0 ? (
        <ApplyForm frame={frame} style={{ opacity: formIn, translate: `0px ${(1 - formIn) * 90}px` }} />
      ) : null}

      {fold < 1 ? (
        <ResumeDoc
          frame={999}
          style={{
            opacity: 1 - prog(frame, 4, 10),
            scale: String(lerp(1, 0.4, fold)),
          }}
        />
      ) : null}

      {fileIn > 0 && !fileGone ? (
        <div
          style={{
            position: "absolute",
            left: lerp(fileStart.x, fileEnd.x, fly),
            top: lerp(fileStart.y, fileEnd.y, fly),
            translate: "-50% -50%",
            display: "flex",
            alignItems: "center",
            gap: 14,
            padding: "20px 30px",
            borderRadius: 22,
            background: COLORS.canvas,
            border: `2px solid ${COLORS.accentLine}`,
            boxShadow: SHADOW.lift,
            fontFamily: FONTS.sans,
            fontWeight: 600,
            fontSize: lerp(32, 31, fly),
            color: COLORS.ink,
            whiteSpace: "nowrap",
            opacity: fileIn,
            scale: String(lerp(1.08, 0.94, fly)),
          }}
        >
          <DocIcon size={40} color={COLORS.accent} />
          {BASE_RESUME.fileName}
        </div>
      ) : null}

      <Cursor x={cx} y={cy} opacity={enter} press={press} ripple={ripple} />

      <Headline lines={COPY.fill} enterAt={4} exitAt={84} />
      <Headline lines={COPY.review} enterAt={94} exitAt={SCENES.apply - 9} />
    </AbsoluteFill>
  );
};
