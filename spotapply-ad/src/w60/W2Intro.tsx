import React from "react";
import { AbsoluteFill, useCurrentFrame } from "remotion";
import { ease, lerp, prog } from "../anim";
import { COLORS, FONTS, SHADOW } from "../brand";
import { CheckBadge, DocIcon } from "../components/Icons";
import { Wordmark } from "../components/Logo";
import { CANDIDATE } from "../story";
import { COL, STAGE } from "./layout";
import { DarkBg, LightBg } from "./components";
import { FILE_CENTER, FileChip60 } from "./W1Problem";
import { SCENES60 } from "./timeline60";

// 5.4-11s. The light opens out of the resume: upload once, then the search
// preferences the user sets (roles, where, relocation). Illustrated, in the
// product's own visual language; labelled demo data like every product scene.

const UPLOAD = { x: STAGE.x, y: 196, w: STAGE.w, h: 300 } as const;
const PREFS = { x: STAGE.x, y: 530, w: STAGE.w, h: 360 } as const;

const Chip: React.FC<{ readonly t: number; readonly children: React.ReactNode; readonly strong?: boolean }> = ({
  t,
  children,
  strong,
}) => (
  <span
    style={{
      display: "inline-flex",
      alignItems: "center",
      padding: "12px 24px",
      borderRadius: 999,
      background: strong ? COLORS.accent : COLORS.accentTint,
      border: `2px solid ${strong ? COLORS.accent : COLORS.accentLine}`,
      color: strong ? "#FFFFFF" : COLORS.accent700,
      fontWeight: 600,
      fontSize: 27,
      whiteSpace: "nowrap",
      opacity: t,
      scale: String(0.85 + 0.15 * t),
    }}
  >
    {children}
  </span>
);

const Card: React.FC<{
  readonly r: { x: number; y: number; w: number; h: number };
  readonly t: number;
  readonly children: React.ReactNode;
}> = ({ r, t, children }) => (
  <div
    style={{
      position: "absolute",
      left: r.x,
      top: r.y,
      width: r.w,
      height: r.h,
      boxSizing: "border-box",
      borderRadius: 28,
      background: COLORS.canvas,
      border: `2px solid ${COLORS.border}`,
      boxShadow: SHADOW.card,
      padding: "30px 38px",
      fontFamily: FONTS.sans,
      opacity: t,
      translate: `0px ${(1 - t) * 60}px`,
    }}
  >
    {children}
  </div>
);

const Label: React.FC<{ readonly children: React.ReactNode }> = ({ children }) => (
  <div style={{ fontWeight: 700, fontSize: 21, letterSpacing: "0.12em", textTransform: "uppercase", color: COLORS.muted }}>
    {children}
  </div>
);

export const IntroScene: React.FC = () => {
  const frame = useCurrentFrame();

  // Light opens from the resume file.
  const open = prog(frame, 0, 22, ease.inOut);
  const radius = lerp(0, 2300, open);

  const upload = prog(frame, 14, 18);
  // The file drops from the centre into the upload card's drop zone.
  const drop = prog(frame, 22, 22, ease.inOut);
  const read = prog(frame, 50, 14);
  const prefs = prog(frame, 60, 18);
  const chipsAt = [76, 82, 92, 98, 104];

  const wordmark = prog(frame, 6, 22);
  const support = prog(frame, 26, 18);

  return (
    <AbsoluteFill>
      <DarkBg />
      <AbsoluteFill style={{ clipPath: `circle(${radius}px at ${FILE_CENTER.x}px ${FILE_CENTER.y}px)` }}>
        <LightBg />

        <div style={{ position: "absolute", left: COL.x, top: 300, width: COL.w, fontFamily: FONTS.sans }}>
          <div
            style={{
              fontWeight: 600,
              fontSize: 64,
              color: COLORS.inkSoft,
              letterSpacing: "-0.02em",
              opacity: wordmark,
              translate: `0px ${(1 - wordmark) * 30}px`,
            }}
          >
            Meet
          </div>
          <div style={{ marginTop: 10, opacity: wordmark, scale: String(lerp(0.92, 1, wordmark)), transformOrigin: "left center" }}>
            <Wordmark size={104} markSize={96} />
          </div>
          <div
            style={{
              marginTop: 40,
              fontWeight: 500,
              fontSize: 33,
              lineHeight: 1.42,
              color: COLORS.inkSoft,
              opacity: support * (1 - prog(frame, SCENES60.intro - 12, 9)),
              translate: `0px ${(1 - support) * 20}px`,
            }}
          >
            Upload your resume once. Choose the roles and places you want.
          </div>
        </div>

        <Card r={UPLOAD} t={upload}>
          <Label>Your resume</Label>
          <div
            style={{
              marginTop: 18,
              height: 150,
              borderRadius: 20,
              border: `3px dashed ${drop >= 1 ? COLORS.accentLine : COLORS.borderStrong}`,
              background: drop >= 1 ? COLORS.accentTint : COLORS.surface,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              gap: 16,
              fontSize: 28,
              fontWeight: 600,
              color: COLORS.muted,
            }}
          >
            {drop < 1 ? (
              <>
                <DocIcon size={36} color={COLORS.muted} /> Drop your resume (PDF or DOCX)
              </>
            ) : (
              <span style={{ display: "inline-flex", alignItems: "center", gap: 16, color: COLORS.ink }}>
                <DocIcon size={40} color={COLORS.accent} /> Maya_Chen_Resume.pdf
                <span style={{ opacity: read, display: "inline-flex", alignItems: "center", gap: 12, marginLeft: 18, color: COLORS.ok, fontSize: 25 }}>
                  <CheckBadge size={34} bg={COLORS.ok} /> Read: {CANDIDATE.years} years · {CANDIDATE.skills.length} core skills
                </span>
              </span>
            )}
          </div>
        </Card>

        <Card r={PREFS} t={prefs}>
          <Label>Target roles</Label>
          <div style={{ display: "flex", gap: 14, marginTop: 16 }}>
            <Chip t={prog(frame, chipsAt[0], 10)} strong>
              Backend Engineer
            </Chip>
            <Chip t={prog(frame, chipsAt[1], 10)}>Platform Engineer</Chip>
          </div>
          <div style={{ marginTop: 34 }}>
            <Label>Where</Label>
          </div>
          <div style={{ display: "flex", gap: 14, marginTop: 16, flexWrap: "wrap" }}>
            <Chip t={prog(frame, chipsAt[2], 10)} strong>
              United States
            </Chip>
            <Chip t={prog(frame, chipsAt[3], 10)}>Remote</Chip>
            <Chip t={prog(frame, chipsAt[4], 10)}>Open to relocating: Seattle, WA</Chip>
          </div>
        </Card>
      </AbsoluteFill>

      {/* The resume file travels from the dark scene into the drop zone. */}
      {drop < 1 ? (
        <FileChip60
          style={{
            left: lerp(FILE_CENTER.x, UPLOAD.x + UPLOAD.w / 2, drop),
            top: lerp(FILE_CENTER.y, UPLOAD.y + 30 + 21 + 18 + 75 + 8, drop),
            scale: String(lerp(1, 0.86, drop)),
            opacity: 1 - prog(frame, 40, 4),
          }}
        />
      ) : null}
    </AbsoluteFill>
  );
};
