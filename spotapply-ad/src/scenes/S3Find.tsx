import React from "react";
import { AbsoluteFill, Img, staticFile, useCurrentFrame } from "remotion";
import { ease, lerp, lerpRect, prog, type Rect } from "../anim";
import { COLORS, FONTS, LAYOUT, SAFE, SHADOW } from "../brand";
import { COPY } from "../copy";
import { BluePath } from "../components/BluePath";
import { Headline } from "../components/Headline";
import { CheckBadge, SparkIcon } from "../components/Icons";
import { Avatar, BrowserBar, ContextChip, Eyebrow, InitialTile } from "../components/ui";
import { CANDIDATE, JOB } from "../story";
import { SCENES } from "../timeline";
import { ProfileCard } from "./S2Reveal";

// 6-11s. The real board (public screenshot, demo data) appears for this
// candidate; the top role opens into its match report, with the reasons
// written out as separate lines.

// The real screenshot: app/static/shots/qualified-board-narrow.png.
const SHOT = { w: 1096, h: 990 };
// Its first card (Senior Backend Engineer · Lumen Data · 94%) in image px.
const SHOT_CARD = { x: 20, y: 104, w: 1046, h: 416 };
const FRAME = { x: SAFE.x, y: 640, w: SAFE.width, bar: 60 };
const S = FRAME.w / SHOT.w;
// +2: the frame's border sits outside the bar and the image.
const FRAME_H = FRAME.bar + SHOT.h * S + 4;
const CARD_ON_SCREEN: Rect = {
  x: FRAME.x + 2 + SHOT_CARD.x * S,
  y: FRAME.y + 2 + FRAME.bar + SHOT_CARD.y * S,
  w: SHOT_CARD.w * S,
  h: SHOT_CARD.h * S,
};
// The board zooms about its own centre; anything tracking the card has to
// apply the same zoom about the same point.
const BOARD_CX = FRAME.x + FRAME.w / 2;
const BOARD_CY = FRAME.y + FRAME_H / 2;
const zoomed = (r: Rect, z: number): Rect => ({
  x: BOARD_CX + (r.x - BOARD_CX) * z,
  y: BOARD_CY + (r.y - BOARD_CY) * z,
  w: r.w * z,
  h: r.h * z,
});
export const MATCH_RECT: Rect = { x: SAFE.x, y: 628, w: SAFE.width, h: 872 };

export const CandidateChip: React.FC<{ readonly style?: React.CSSProperties }> = ({ style }) => (
  <ContextChip lead={<Avatar initials={CANDIDATE.initials} size={50} />} style={style}>
    {CANDIDATE.name}
    <span style={{ color: COLORS.muted, fontWeight: 500 }}>· {CANDIDATE.title}</span>
  </ContextChip>
);

/** The match report, in the real product's layout. `frame` drives the
 * reveal; pass a large value for the finished card. */
export const MatchCard: React.FC<{ readonly frame: number; readonly contentOpacity?: number }> = ({
  frame,
  contentOpacity = 1,
}) => {
  const score = prog(frame, 80, 14);
  return (
    <div style={{ position: "absolute", inset: 0, fontFamily: FONTS.sans, opacity: contentOpacity }}>
      <div
        style={{
          height: 84,
          background: COLORS.accentTint,
          borderBottom: `2px solid ${COLORS.accentLine}`,
          display: "flex",
          alignItems: "center",
          gap: 14,
          padding: "0 40px",
        }}
      >
        <SparkIcon size={30} color={COLORS.ink} />
        <Eyebrow color={COLORS.ink} size={26} style={{ letterSpacing: "0.08em" }}>
          AI match report
        </Eyebrow>
        <span style={{ fontWeight: 600, fontSize: 25, color: COLORS.muted, whiteSpace: "nowrap" }}>
          scored against your actual resume
        </span>
      </div>
      <div style={{ padding: "36px 44px", display: "flex", flexDirection: "column" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 26 }}>
          <InitialTile letter={JOB.initial} size={88} />
          <div>
            <div style={{ fontWeight: 700, fontSize: 46, color: COLORS.ink, letterSpacing: "-0.02em", lineHeight: 1.1 }}>
              {JOB.title}
            </div>
            <div style={{ fontWeight: 500, fontSize: 31, color: COLORS.muted, marginTop: 6 }}>
              {JOB.company} · {JOB.location}
            </div>
          </div>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 26, marginTop: 32 }}>
          <div
            style={{
              width: 112,
              height: 112,
              borderRadius: 56,
              background: COLORS.okBright,
              boxShadow: "0 0 0 10px rgba(16,185,129,0.14)",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              fontWeight: 800,
              fontSize: 46,
              color: COLORS.ink,
              flex: "none",
              scale: String(lerp(0.6, 1, score)),
              opacity: score,
            }}
          >
            {JOB.fit}
          </div>
          <div style={{ opacity: score }}>
            <div style={{ fontWeight: 700, fontSize: 36, color: COLORS.ink }}>Strong fit for your resume</div>
            <div style={{ fontWeight: 500, fontSize: 26, color: COLORS.muted, marginTop: 6 }}>
              Fit score {JOB.fit}/100 · demo data
            </div>
          </div>
        </div>
        <div style={{ height: 2, background: COLORS.border, marginTop: 34 }} />
        <Eyebrow size={24} style={{ marginTop: 28 }}>
          Why it matches
        </Eyebrow>
        <div style={{ display: "flex", flexDirection: "column", gap: 24, marginTop: 22 }}>
          {JOB.reasons.map((r, i) => {
            const t = prog(frame, 90 + i * 11, 16);
            return (
              <div
                key={r.label}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 22,
                  opacity: t,
                  translate: `${(1 - t) * 40}px 0px`,
                }}
              >
                <div style={{ scale: String(lerp(0.5, 1, prog(frame, 92 + i * 11, 10, ease.out))) }}>
                  <CheckBadge size={54} bg={COLORS.ok} />
                </div>
                <div>
                  <Eyebrow size={23}>{r.label}</Eyebrow>
                  <div style={{ fontWeight: 600, fontSize: 34, color: COLORS.ink, marginTop: 4 }}>{r.text}</div>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
};

/** The match report in its final resting place (used by the next scene). */
export const MatchCardFinal: React.FC<{ readonly style?: React.CSSProperties }> = ({ style }) => (
  <div
    style={{
      position: "absolute",
      left: MATCH_RECT.x,
      top: MATCH_RECT.y,
      width: MATCH_RECT.w,
      height: MATCH_RECT.h,
      borderRadius: 36,
      background: COLORS.canvas,
      border: `2px solid ${COLORS.border}`,
      boxShadow: SHADOW.card,
      overflow: "hidden",
      ...style,
    }}
  >
    <MatchCard frame={999} />
  </div>
);

export const FindScene: React.FC = () => {
  const frame = useCurrentFrame();

  // Profile folds into the context chip.
  const fold = prog(frame, 0, 18, ease.inOut);
  const chipIn = prog(frame, 10, 12);

  // The real board rises in.
  const board = prog(frame, 8, 22);
  const boardOut = prog(frame, 60, 16, ease.in);
  const zoom = lerp(1, 1.025, prog(frame, 8, 60, ease.inOut));

  // Thread from the candidate to the top match, then a ring around it.
  const thread = prog(frame, 26, 22, ease.inOut);
  const threadOut = prog(frame, 56, 10);
  const ring = prog(frame, 44, 10);

  // The top card opens into the report.
  const open = prog(frame, 58, 24, ease.inOut);
  const card = zoomed(CARD_ON_SCREEN, zoom);
  const rect = lerpRect(card, MATCH_RECT, open);

  return (
    <AbsoluteFill>
      {fold < 1 ? (
        <ProfileCard
          frame={999}
          style={{
            opacity: 1 - prog(frame, 4, 12),
            scale: String(lerp(1, 0.32, fold)),
            translate: `${lerp(0, -200, fold)}px ${lerp(0, -500, fold)}px`,
          }}
        />
      ) : null}

      <CandidateChip style={{ opacity: chipIn, scale: String(lerp(0.9, 1, chipIn)) }} />

      {boardOut < 1 ? (
        <div
          style={{
            position: "absolute",
            left: FRAME.x,
            top: FRAME.y,
            width: FRAME.w,
            borderRadius: 26,
            overflow: "hidden",
            background: COLORS.surface,
            border: `2px solid ${COLORS.border}`,
            boxShadow: SHADOW.card,
            opacity: board * (1 - boardOut),
            translate: `0px ${(1 - board) * 90}px`,
            scale: String(zoom),
          }}
        >
          <BrowserBar height={FRAME.bar}>app.spotapply.ai/dashboard</BrowserBar>
          <Img
            src={staticFile("shots/qualified-board-narrow.png")}
            style={{ display: "block", width: FRAME.w, height: SHOT.h * S }}
          />
        </div>
      ) : null}

      <BluePath
        d={`M ${SAFE.x + 39} ${LAYOUT.chipTop + LAYOUT.chipHeight} C ${SAFE.x + 39} 690, ${card.x + 46} 720, ${card.x + 46} ${card.y + 34}`}
        progress={thread}
        opacity={1 - threadOut}
      />

      {ring > 0 && open <= 0 ? (
        <div
          style={{
            position: "absolute",
            left: card.x - 6,
            top: card.y - 6,
            width: card.w + 12,
            height: card.h + 12,
            borderRadius: 30,
            border: `6px solid ${COLORS.accent}`,
            boxShadow: `0 0 0 ${10 * ring}px rgba(14,165,233,0.18)`,
            opacity: ring,
          }}
        />
      ) : null}

      {open > 0 ? (
        <div
          style={{
            position: "absolute",
            left: rect.x,
            top: rect.y,
            width: rect.w,
            height: rect.h,
            borderRadius: lerp(24, 36, open),
            background: COLORS.canvas,
            border: `2px solid ${open < 1 ? COLORS.accent : COLORS.border}`,
            boxShadow: open < 1 ? SHADOW.lift : SHADOW.card,
            overflow: "hidden",
          }}
        >
          {/* The screenshot's own card, scaled with the box, hands over to the
              full report: a morph, not a blank card. */}
          <Img
            src={staticFile("shots/qualified-board-narrow.png")}
            style={{
              position: "absolute",
              left: -SHOT_CARD.x * (rect.w / SHOT_CARD.w),
              top: -SHOT_CARD.y * (rect.w / SHOT_CARD.w),
              width: SHOT.w * (rect.w / SHOT_CARD.w),
              height: SHOT.h * (rect.w / SHOT_CARD.w),
              opacity: 1 - prog(frame, 66, 10, ease.inOut),
            }}
          />
          <MatchCard frame={frame} contentOpacity={prog(frame, 70, 12)} />
        </div>
      ) : null}

      <Headline lines={COPY.find} enterAt={4} exitAt={SCENES.find - 9} />
    </AbsoluteFill>
  );
};
