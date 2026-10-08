import React from "react";
import { AbsoluteFill, useCurrentFrame } from "remotion";
import { ease, lerp, prog } from "../anim";
import { COLORS, FONTS, SAFE, SHADOW } from "../brand";
import { COPY } from "../copy";
import { Headline } from "../components/Headline";
import { CheckBadge, LockIcon, SparkIcon } from "../components/Icons";
import { ContextChip, Eyebrow, InitialTile, Runs, runsText, typedRuns } from "../components/ui";
import { BASE_RESUME, CANDIDATE, JOB } from "../story";
import { SCENES } from "../timeline";
import { CandidateChip, MatchCardFinal } from "./S3Find";

// 11-16s. The summary is reworded for this role from facts already in the
// resume (the words it brings forward light up where they came from), while
// the employer, dates and degree are marked unchanged.

export const RESUME_RECT = { x: SAFE.x, y: 612, w: SAFE.width, h: 890 } as const;

export const JobChip: React.FC<{ readonly style?: React.CSSProperties }> = ({ style }) => (
  <ContextChip lead={<InitialTile letter={JOB.initial} size={50} />} style={style}>
    <span style={{ color: COLORS.muted, fontWeight: 500 }}>For:</span>
    {JOB.title}
    <span style={{ color: COLORS.muted, fontWeight: 500 }}>· {JOB.company}</span>
  </ContextChip>
);

const Unchanged: React.FC<{ readonly t: number }> = ({ t }) => (
  <span
    style={{
      display: "inline-flex",
      alignItems: "center",
      gap: 8,
      padding: "6px 16px",
      borderRadius: 999,
      background: COLORS.surface,
      border: `2px solid ${COLORS.borderStrong}`,
      color: COLORS.inkSoft,
      fontWeight: 600,
      fontSize: 25,
      opacity: t,
      scale: String(lerp(0.7, 1, t)),
      whiteSpace: "nowrap",
    }}
  >
    <LockIcon size={24} color={COLORS.inkSoft} />
    Unchanged
  </span>
);

const AFTER_LEN = runsText(BASE_RESUME.summaryAfter).length;
const TYPE_START = 54;
const TYPE_CPF = 2.1;
const TYPE_END = TYPE_START + Math.ceil(AFTER_LEN / TYPE_CPF);

/** The resume. `frame` drives the rewrite; pass a large value for the
 * finished, tailored document. */
export const ResumeDoc: React.FC<{ readonly frame: number; readonly style?: React.CSSProperties }> = ({
  frame,
  style,
}) => {
  const strike = prog(frame, 34, 14, ease.inOut);
  const beforeOut = prog(frame, 48, 6);
  const chars = Math.floor(Math.max(0, frame - TYPE_START) * TYPE_CPF);
  const typing = frame >= TYPE_START && chars < AFTER_LEN;
  const hl = prog(frame, TYPE_END + 2, 12);
  const lock1 = prog(frame, TYPE_END + 8, 12);
  const lock2 = prog(frame, TYPE_END + 14, 12);
  const verified = prog(frame, TYPE_END + 20, 14);
  const tag = prog(frame, TYPE_END + 2, 12);

  return (
    <div
      style={{
        position: "absolute",
        left: RESUME_RECT.x,
        top: RESUME_RECT.y,
        width: RESUME_RECT.w,
        height: RESUME_RECT.h,
        boxSizing: "border-box",
        borderRadius: 28,
        background: COLORS.canvas,
        border: `2px solid ${COLORS.border}`,
        boxShadow: SHADOW.card,
        padding: "40px 48px",
        display: "flex",
        flexDirection: "column",
        fontFamily: FONTS.sans,
        color: COLORS.ink,
        ...style,
      }}
    >
      <div style={{ fontWeight: 700, fontSize: 50, letterSpacing: "-0.02em", lineHeight: 1.1 }}>{CANDIDATE.name}</div>
      <div style={{ fontWeight: 500, fontSize: 28, color: COLORS.muted, marginTop: 6 }}>
        {CANDIDATE.title} · {CANDIDATE.homeCity}
      </div>
      <div style={{ height: 2, background: COLORS.border, marginTop: 24 }} />

      <div style={{ display: "flex", alignItems: "center", marginTop: 24, height: 40 }}>
        <Eyebrow color={COLORS.accent} size={24}>
          Summary
        </Eyebrow>
        <span
          style={{
            marginLeft: "auto",
            display: "inline-flex",
            alignItems: "center",
            gap: 8,
            fontWeight: 600,
            fontSize: 24,
            color: COLORS.accent700,
            opacity: tag,
          }}
        >
          <SparkIcon size={24} color={COLORS.accent} />
          Tailored for this role
        </span>
      </div>
      <div
        style={{
          marginTop: 10,
          minHeight: 136,
          fontSize: 33,
          lineHeight: 1.36,
          fontWeight: 400,
          position: "relative",
        }}
      >
        {beforeOut < 1 ? (
          <span style={{ position: "relative", opacity: 1 - beforeOut, color: COLORS.inkSoft }}>
            {BASE_RESUME.summaryBefore}
            <span
              style={{
                position: "absolute",
                left: 0,
                top: "54%",
                height: 3,
                width: `${strike * 100}%`,
                background: COLORS.muted,
              }}
            />
          </span>
        ) : (
          <span>
            <Runs runs={typedRuns(BASE_RESUME.summaryAfter, chars)} highlight={hl} />
            {typing ? (
              <span
                style={{
                  display: "inline-block",
                  width: 3,
                  height: 36,
                  marginLeft: 2,
                  translate: "0px 6px",
                  background: COLORS.accent,
                }}
              />
            ) : null}
          </span>
        )}
      </div>

      <Eyebrow color={COLORS.accent} size={24} style={{ marginTop: 22 }}>
        Experience
      </Eyebrow>
      <div style={{ fontWeight: 600, fontSize: 32, marginTop: 12 }}>
        {BASE_RESUME.role.title} · {BASE_RESUME.role.employer}
      </div>
      <div style={{ display: "flex", alignItems: "center", marginTop: 6, height: 44 }}>
        <span style={{ fontWeight: 500, fontSize: 28, color: COLORS.muted }}>{BASE_RESUME.role.dates}</span>
        <span style={{ marginLeft: "auto" }}>
          <Unchanged t={lock1} />
        </span>
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 6, marginTop: 10 }}>
        {[...BASE_RESUME.role.bullets, BASE_RESUME.skillsLine].map((b, i) => (
          <div key={i} style={{ display: "flex", gap: 14, fontSize: 30, lineHeight: 1.36, color: COLORS.inkSoft }}>
            <span style={{ color: COLORS.borderStrong, fontWeight: 700 }}>•</span>
            <span>
              {i === 2 ? <span style={{ color: COLORS.muted }}>Skills: </span> : null}
              <Runs runs={b} highlight={hl} />
            </span>
          </div>
        ))}
      </div>

      <Eyebrow color={COLORS.accent} size={24} style={{ marginTop: 24 }}>
        Education
      </Eyebrow>
      <div style={{ display: "flex", alignItems: "center", marginTop: 10, height: 44 }}>
        <span style={{ fontWeight: 500, fontSize: 30, color: COLORS.inkSoft }}>{BASE_RESUME.education}</span>
        <span style={{ marginLeft: "auto" }}>
          <Unchanged t={lock2} />
        </span>
      </div>

      <div style={{ marginTop: "auto", display: "flex" }}>
        <span
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 14,
            padding: "12px 26px 12px 14px",
            borderRadius: 999,
            background: COLORS.okTint,
            border: `2px solid ${COLORS.okBorder}`,
            color: COLORS.ok,
            fontWeight: 600,
            fontSize: 28,
            opacity: verified,
            translate: `0px ${(1 - verified) * 16}px`,
          }}
        >
          <CheckBadge size={40} bg={COLORS.ok} />
          Checked against your original resume
        </span>
      </div>
    </div>
  );
};

export const TailorScene: React.FC = () => {
  const frame = useCurrentFrame();

  const fold = prog(frame, 0, 18, ease.inOut);
  const candOut = prog(frame, 0, 10);
  const jobIn = prog(frame, 10, 12);
  const doc = prog(frame, 10, 22);

  return (
    <AbsoluteFill>
      {candOut < 1 ? <CandidateChip style={{ opacity: 1 - candOut }} /> : null}
      <JobChip style={{ opacity: jobIn, scale: String(lerp(0.9, 1, jobIn)) }} />

      {fold < 1 ? (
        <MatchCardFinal
          style={{
            opacity: 1 - prog(frame, 2, 12),
            scale: String(lerp(1, 0.36, fold)),
            translate: `${lerp(0, -170, fold)}px ${lerp(0, -470, fold)}px`,
          }}
        />
      ) : null}

      {doc > 0 ? (
        <ResumeDoc
          frame={frame}
          style={{ opacity: doc, translate: `0px ${(1 - doc) * 110}px` }}
        />
      ) : null}

      <Headline lines={COPY.tailor} enterAt={4} exitAt={SCENES.tailor - 9} size={92} />
    </AbsoluteFill>
  );
};
