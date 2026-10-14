import React from "react";
import { AbsoluteFill, useCurrentFrame } from "remotion";
import { ease, lerp, prog } from "../anim";
import { COLORS, LAYOUT, SAFE } from "../brand";
import { COPY } from "../copy";
import { BluePath } from "../components/BluePath";
import { Headline } from "../components/Headline";
import { Ring, Screen, StageClip, stageGeom, toCanvas, type ScreenGeom } from "../components/Screen";
import { Avatar, ContextChip } from "../components/ui";
import { CANDIDATE } from "../story";
import { SCENES } from "../timeline";
import { ProfileCard } from "./S2Reveal";

// 5.6-10s. LIVE captures (public/live/, the running app on fictional demo
// data): the board's new "Apply today" divider with the Lumen Data role on
// top, then its match report sliding up over the board, as the app's own
// drawer does.

// Source-pixel rects in public/live/board.png and match.png (1075 px wide).
const BOARD_SLICE_Y = { from: 196, to: 228 };
const LUMEN_CARD = { x: 40, y: 384, w: 984, h: 710 };
const MATCH_SLICE_Y = { from: 1090, to: 1150 };
const WHY_BLOCK = { x: 28, y: 1462, w: 1019, h: 268 };
const SLICE_H = 1100;

export const boardGeom = (frame: number): ScreenGeom =>
  stageGeom([{ y: lerp(BOARD_SLICE_Y.from, BOARD_SLICE_Y.to, prog(frame, 8, 56, ease.inOut)), h: SLICE_H }]);
export const matchGeom = (frame: number): ScreenGeom =>
  stageGeom([{ y: lerp(MATCH_SLICE_Y.from, MATCH_SLICE_Y.to, prog(frame, 84, 46, ease.inOut)), h: SLICE_H }]);

export const CandidateChip: React.FC<{ readonly style?: React.CSSProperties }> = ({ style }) => (
  <ContextChip lead={<Avatar initials={CANDIDATE.initials} size={50} />} style={style}>
    {CANDIDATE.name}
    <span style={{ color: COLORS.muted, fontWeight: 500 }}>· {CANDIDATE.title}</span>
  </ContextChip>
);

/** The match report screen exactly as this scene leaves it. */
export const MatchScreenFinal: React.FC<{ readonly style?: React.CSSProperties }> = ({ style }) => (
  <Screen src="live/match.png" geom={matchGeom(SCENES.find - 1)} style={style} />
);

export const FindScene: React.FC = () => {
  const frame = useCurrentFrame();

  // Profile folds into the context chip.
  const fold = prog(frame, 0, 18, ease.inOut);
  const chipIn = prog(frame, 10, 12);

  // The live board rises in, scrolling gently.
  const board = prog(frame, 8, 22);
  const bg = boardGeom(frame);
  const card = toCanvas(bg, LUMEN_CARD);

  // Thread from the candidate to the top match, then a ring around it.
  const thread = prog(frame, 24, 20, ease.inOut);
  const threadOut = prog(frame, 54, 8);
  const ring = prog(frame, 40, 10) * (1 - prog(frame, 62, 8));

  // The match report slides up over the board (the app's drawer).
  const drawer = prog(frame, 58, 22, ease.inOut);
  const mg = matchGeom(frame);
  // Ring the written reasons, gone again before the hand-off to tailoring.
  const why = prog(frame, 94, 10) * (1 - prog(frame, 120, 8));

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

      {drawer < 1 ? (
        <Screen
          src="live/board.png"
          geom={bg}
          style={{
            opacity: board * (1 - 0.5 * drawer),
            translate: `0px ${(1 - board) * 90}px`,
            scale: String(lerp(1, 0.94, drawer)),
          }}
        />
      ) : null}

      <BluePath
        d={`M ${SAFE.x + 41} ${LAYOUT.chipTop + LAYOUT.chipHeight} C ${SAFE.x + 41} 660, ${card.x + 40} 700, ${card.x + 40} ${card.y + 18}`}
        progress={thread}
        opacity={1 - threadOut}
      />
      <Ring rect={card} t={ring} radius={44} />

      {drawer > 0 ? (
        <StageClip>
          <Screen src="live/match.png" geom={mg} style={{ translate: `0px ${(1 - drawer) * 940}px` }} />
        </StageClip>
      ) : null}
      {drawer >= 1 ? <Ring rect={toCanvas(mg, WHY_BLOCK)} t={why} radius={20} /> : null}

      <Headline lines={COPY.find} enterAt={4} exitAt={SCENES.find - 9} />
    </AbsoluteFill>
  );
};
