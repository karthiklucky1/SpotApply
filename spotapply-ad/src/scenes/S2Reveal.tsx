import React from "react";
import { AbsoluteFill, useCurrentFrame } from "remotion";
import { ease, lerp, prog } from "../anim";
import { COLORS, FONTS, SAFE, SHADOW } from "../brand";
import { COPY } from "../copy";
import { Headline } from "../components/Headline";
import { DocIcon } from "../components/Icons";
import { Wordmark } from "../components/Logo";
import { Avatar, Eyebrow } from "../components/ui";
import { BASE_RESUME, CANDIDATE } from "../story";
import { SCENES } from "../timeline";
import { HOOK_FREEZE, HOOK_LAYOUT, HookForm } from "./S1Hook";

// 3-6s. The forms fold into one resume, and the resume opens up as a
// profile SpotApply has read. The wordmark is on screen by second 4.

export const PROFILE_RECT = { x: SAFE.x, y: 690, w: SAFE.width, h: 700 } as const;

const Row: React.FC<{ readonly label: string; readonly t: number; readonly children: React.ReactNode }> = ({
  label,
  t,
  children,
}) => (
  <div style={{ opacity: t, translate: `0px ${(1 - t) * 24}px` }}>
    <Eyebrow size={24}>{label}</Eyebrow>
    <div style={{ marginTop: 12 }}>{children}</div>
  </div>
);

/** The candidate profile. `frame` drives the staggered reveal; pass a large
 * value for the fully revealed card. */
export const ProfileCard: React.FC<{ readonly frame: number; readonly style?: React.CSSProperties }> = ({
  frame,
  style,
}) => {
  const head = prog(frame, 36, 14);
  const r1 = prog(frame, 42, 14);
  const r2 = prog(frame, 48, 14);
  const r3 = prog(frame, 60, 14);
  return (
    <div
      style={{
        position: "absolute",
        left: PROFILE_RECT.x,
        top: PROFILE_RECT.y,
        width: PROFILE_RECT.w,
        height: PROFILE_RECT.h,
        boxSizing: "border-box",
        borderRadius: 36,
        background: COLORS.canvas,
        border: `2px solid ${COLORS.border}`,
        boxShadow: SHADOW.card,
        padding: "40px 48px",
        display: "flex",
        flexDirection: "column",
        gap: 30,
        fontFamily: FONTS.sans,
        ...style,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 12, opacity: head }}>
        <DocIcon size={30} color={COLORS.accent} />
        <Eyebrow color={COLORS.accent} size={24}>
          Profile, read from your resume
        </Eyebrow>
      </div>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 28,
          opacity: head,
          translate: `0px ${(1 - head) * 24}px`,
        }}
      >
        <Avatar initials={CANDIDATE.initials} size={116} />
        <div>
          <div style={{ fontWeight: 700, fontSize: 58, color: COLORS.ink, letterSpacing: "-0.02em", lineHeight: 1.1 }}>
            {CANDIDATE.name}
          </div>
          <div style={{ fontWeight: 500, fontSize: 34, color: COLORS.muted, marginTop: 6 }}>{CANDIDATE.title}</div>
        </div>
      </div>
      <div style={{ height: 2, background: COLORS.border, opacity: head }} />
      <Row label="Experience" t={r1}>
        <div style={{ fontWeight: 700, fontSize: 44, color: COLORS.ink }}>{CANDIDATE.years} years</div>
      </Row>
      <Row label="Skills" t={r2}>
        <div style={{ display: "flex", gap: 14 }}>
          {CANDIDATE.skills.map((s, i) => {
            const c = prog(frame, 50 + i * 4, 12);
            return (
              <span
                key={s}
                style={{
                  padding: "10px 22px",
                  borderRadius: 999,
                  background: COLORS.accentTint,
                  border: `2px solid ${COLORS.accentLine}`,
                  color: COLORS.accent700,
                  fontWeight: 600,
                  fontSize: 32,
                  opacity: c,
                  scale: String(0.85 + 0.15 * c),
                }}
              >
                {s}
              </span>
            );
          })}
        </div>
      </Row>
      <Row label="Looking for" t={r3}>
        <div style={{ fontWeight: 600, fontSize: 36, color: COLORS.ink }}>{CANDIDATE.lookingFor}</div>
      </Row>
    </div>
  );
};

const FileChip: React.FC<{ readonly style?: React.CSSProperties }> = ({ style }) => (
  <div
    style={{
      position: "absolute",
      display: "flex",
      alignItems: "center",
      gap: 18,
      padding: "26px 36px",
      borderRadius: 28,
      background: COLORS.canvas,
      border: `2px solid ${COLORS.accentLine}`,
      boxShadow: SHADOW.lift,
      fontFamily: FONTS.sans,
      fontWeight: 600,
      fontSize: 32,
      color: COLORS.ink,
      whiteSpace: "nowrap",
      ...style,
    }}
  >
    <DocIcon size={48} color={COLORS.accent} />
    {BASE_RESUME.fileName}
  </div>
);

export const RevealScene: React.FC = () => {
  const frame = useCurrentFrame();

  // Forms fold into one stack at the centre of the stage.
  const fold = prog(frame, 0, 20, ease.inOut);
  const formsOut = prog(frame, 8, 10, ease.in);

  // The stack becomes a single resume file...
  const fileIn = prog(frame, 16, 10);
  const fileOut = prog(frame, 30, 12, ease.in);

  // ...which opens into the profile.
  const open = prog(frame, 30, 20);

  return (
    <AbsoluteFill>
      {formsOut < 1
        ? HOOK_LAYOUT.map((l, i) => (
            <HookForm
              key={i}
              index={i}
              frame={HOOK_FREEZE + 100}
              style={{
                left: lerp(l.x, SAFE.x, fold),
                top: lerp(l.y, 864, fold),
                rotate: `${lerp(l.rotate, 0, fold)}deg`,
                scale: String(lerp(1, 0.6, fold)),
                opacity: 1 - formsOut,
              }}
            />
          ))
        : null}

      {fileIn > 0 && fileOut < 1 ? (
        <div
          style={{
            position: "absolute",
            left: 0,
            right: 0,
            top: 990,
            display: "flex",
            justifyContent: "center",
          }}
        >
          <FileChip
            style={{
              position: "relative",
              opacity: fileIn * (1 - fileOut),
              scale: String(lerp(0.82, 1, fileIn) * lerp(1, 1.2, fileOut)),
            }}
          />
        </div>
      ) : null}

      {open > 0 ? (
        <ProfileCard
          frame={frame}
          style={{
            opacity: Math.min(1, open * 1.4),
            scale: String(lerp(0.62, 1, open)),
          }}
        />
      ) : null}

      <Headline
        enterAt={2}
        exitAt={SCENES.reveal - 9}
        lines={[
          <span key="meet" style={{ fontSize: 84, fontWeight: 600, color: COLORS.inkSoft, letterSpacing: "-0.02em" }}>
            {COPY.revealLead}
          </span>,
          <div key="mark" style={{ paddingTop: 10 }}>
            <Wordmark size={120} markSize={108} />
          </div>,
        ]}
      />
    </AbsoluteFill>
  );
};
