import React from "react";
import { AbsoluteFill, useCurrentFrame } from "remotion";
import { ease, lerp, prog } from "../anim";
import { COLORS, FONTS, SAFE, SHADOW } from "../brand";
import { COPY } from "../copy";
import { BluePath } from "../components/BluePath";
import { Headline } from "../components/Headline";
import { CheckIcon } from "../components/Icons";
import { InitialTile } from "../components/ui";
import { JOB, OTHER_JOBS } from "../story";
import { SCENES } from "../timeline";
import { JobChip } from "./S4Tailor";
import { ApplyForm } from "./S5Apply";

// 22-25s. The same role moves from Shortlisted to Submitted in the tracker
// (the real board's own tab names). No reply, interview or offer is shown.

const COL_W = 408;
const GAP = 24;
const COLS = [SAFE.x, SAFE.x + COL_W + GAP] as const;
export const BOARD_TOP = 628;
const BOARD_H = 800;
const CARD_X = 14;
const CARD_W = COL_W - 2 * CARD_X;
const CARD_H = 158;
const SLOT_Y = (slot: number) => BOARD_TOP + 96 + slot * (CARD_H + 16);

// Shortlisted holds six roles (as in the real board screenshot).
const SHORTLIST_COUNT = 6;
const MOVE_AT = 26;
const LAND_AT = 50;
const COUNT_AT = 44;

const MiniCard: React.FC<{
  readonly title: string;
  readonly company: string;
  readonly initial: string;
  readonly status?: number;
  readonly style?: React.CSSProperties;
}> = ({ title, company, initial, status = 0, style }) => (
  <div
    style={{
      position: "absolute",
      width: CARD_W,
      height: CARD_H + 58 * status,
      boxSizing: "border-box",
      borderRadius: 20,
      background: COLORS.canvas,
      border: `2px solid ${COLORS.border}`,
      boxShadow: SHADOW.soft,
      padding: "18px 18px",
      fontFamily: FONTS.sans,
      overflow: "hidden",
      ...style,
    }}
  >
    <div style={{ display: "flex", gap: 14 }}>
      <InitialTile letter={initial} size={48} />
      <div style={{ minWidth: 0 }}>
        <div style={{ fontWeight: 700, fontSize: 30, lineHeight: 1.18, color: COLORS.ink }}>{title}</div>
        <div style={{ fontWeight: 500, fontSize: 26, color: COLORS.muted, marginTop: 6 }}>{company}</div>
      </div>
    </div>
    {status > 0 ? (
      <div style={{ marginTop: 14, opacity: status, display: "flex" }}>
        <span
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 8,
            padding: "6px 16px",
            borderRadius: 999,
            background: COLORS.okTint,
            border: `2px solid ${COLORS.okBorder}`,
            color: COLORS.ok,
            fontWeight: 700,
            fontSize: 26,
            whiteSpace: "nowrap",
          }}
        >
          <CheckIcon size={26} color={COLORS.ok} stroke={3.4} />
          Submitted today
        </span>
      </div>
    ) : null}
  </div>
);

const Column: React.FC<{
  readonly x: number;
  readonly title: string;
  readonly count: number;
  readonly pop: number;
  readonly active?: boolean;
  readonly children?: React.ReactNode;
}> = ({ x, title, count, pop, active, children }) => (
  <div
    style={{
      position: "absolute",
      left: x,
      top: BOARD_TOP,
      width: COL_W,
      height: BOARD_H,
      boxSizing: "border-box",
      borderRadius: 28,
      background: COLORS.surface2,
      border: `2px solid ${active ? COLORS.accentLine : COLORS.border}`,
      fontFamily: FONTS.sans,
    }}
  >
    <div style={{ display: "flex", alignItems: "center", gap: 14, padding: "24px 22px 0" }}>
      <span style={{ fontWeight: 700, fontSize: 34, color: COLORS.ink }}>{title}</span>
      <span
        style={{
          minWidth: 48,
          height: 48,
          padding: "0 12px",
          boxSizing: "border-box",
          borderRadius: 24,
          background: active ? COLORS.accent : COLORS.canvas,
          color: active ? "#FFFFFF" : COLORS.muted,
          border: `2px solid ${active ? COLORS.accent : COLORS.border}`,
          display: "inline-flex",
          alignItems: "center",
          justifyContent: "center",
          fontWeight: 700,
          fontSize: 28,
          scale: String(1 + 0.25 * Math.sin(Math.PI * pop)),
        }}
      >
        {count}
      </span>
    </div>
    {children}
  </div>
);

export const TrackScene: React.FC = () => {
  const frame = useCurrentFrame();

  const formOut = prog(frame, 0, 16, ease.in);
  const chipOut = prog(frame, 0, 10);
  const board = prog(frame, 6, 18);

  const lift = prog(frame, MOVE_AT - 4, 6);
  const move = prog(frame, MOVE_AT, LAND_AT - MOVE_AT, ease.inOut);
  const settle = prog(frame, LAND_AT, 8);
  const status = prog(frame, LAND_AT + 2, 12);
  const counted = frame >= COUNT_AT;
  const pop = prog(frame, COUNT_AT, 10);
  const closeGap = prog(frame, COUNT_AT, 14, ease.inOut);

  const from = { x: COLS[0] + CARD_X, y: SLOT_Y(0) };
  const to = { x: COLS[1] + CARD_X, y: SLOT_Y(0) };
  const arc = Math.sin(Math.PI * move) * -70;
  const cardX = lerp(from.x, to.x, move);
  const cardY = lerp(from.y, to.y, move) + arc;
  const midY = from.y + CARD_H / 2;
  const pathD = `M ${from.x + CARD_W / 2} ${midY} C ${from.x + CARD_W / 2 + 120} ${midY - 150}, ${to.x + CARD_W / 2 - 120} ${midY - 150}, ${to.x + CARD_W / 2} ${midY}`;

  return (
    <AbsoluteFill>
      {chipOut < 1 ? <JobChip style={{ opacity: 1 - chipOut }} /> : null}
      {formOut < 1 ? (
        <ApplyForm
          frame={999}
          style={{
            opacity: 1 - formOut,
            scale: String(lerp(1, 0.86, formOut)),
          }}
        />
      ) : null}

      <div style={{ position: "absolute", inset: 0, opacity: board, translate: `0px ${(1 - board) * 80}px` }}>
        <Column x={COLS[0]} title="Shortlisted" count={counted ? SHORTLIST_COUNT - 1 : SHORTLIST_COUNT} pop={pop}>
          <MiniCard
            title={OTHER_JOBS[0].title}
            company={OTHER_JOBS[0].company}
            initial={OTHER_JOBS[0].initial}
            style={{ left: CARD_X, top: lerp(SLOT_Y(1), SLOT_Y(0), closeGap) - BOARD_TOP }}
          />
          <MiniCard
            title={OTHER_JOBS[1].title}
            company={OTHER_JOBS[1].company}
            initial={OTHER_JOBS[1].initial}
            style={{ left: CARD_X, top: lerp(SLOT_Y(2), SLOT_Y(1), closeGap) - BOARD_TOP }}
          />
          <div
            style={{
              position: "absolute",
              left: CARD_X,
              top: lerp(SLOT_Y(3), SLOT_Y(2), closeGap) - BOARD_TOP + 12,
              fontWeight: 600,
              fontSize: 26,
              color: COLORS.muted,
              paddingLeft: 8,
            }}
          >
            + {SHORTLIST_COUNT - 3} more
          </div>
        </Column>
        <Column x={COLS[1]} title="Submitted" count={counted ? 1 : 0} pop={pop} active={counted}>
          <div
            style={{
              position: "absolute",
              left: CARD_X,
              top: SLOT_Y(0) - BOARD_TOP,
              width: CARD_W,
              height: CARD_H,
              boxSizing: "border-box",
              borderRadius: 20,
              border: `3px dashed ${COLORS.borderStrong}`,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              fontWeight: 600,
              fontSize: 26,
              color: COLORS.muted,
              opacity: 1 - prog(frame, MOVE_AT + 10, 10),
            }}
          >
            Nothing yet
          </div>
        </Column>

        <BluePath d={pathD} progress={prog(frame, MOVE_AT - 2, 20, ease.inOut)} opacity={1 - prog(frame, LAND_AT, 12)} dashed stroke={6} head={false} />

        <MiniCard
          title={JOB.title}
          company={JOB.company}
          initial={JOB.initial}
          status={status}
          style={{
            left: cardX,
            top: cardY,
            scale: String(1 + 0.05 * lift * (1 - settle)),
            boxShadow: lift > 0 && settle < 1 ? SHADOW.lift : SHADOW.soft,
            border: `2px solid ${lift > 0 && settle < 1 ? COLORS.accent : COLORS.border}`,
          }}
        />
      </div>

      <Headline lines={COPY.track} enterAt={4} exitAt={SCENES.track - 9} size={90} />
    </AbsoluteFill>
  );
};
