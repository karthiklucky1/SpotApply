import React from "react";
import { AbsoluteFill, useCurrentFrame } from "remotion";
import { ease, lerp, prog } from "../anim";
import { COLORS } from "../brand";
import { COPY } from "../copy";
import { Headline } from "../components/Headline";
import { Ring, Screen, stageGeom, toCanvas, type ScreenGeom } from "../components/Screen";
import { ContextChip, InitialTile } from "../components/ui";
import { JOB } from "../story";
import { SCENES } from "../timeline";
import { CandidateChip, MatchScreenFinal } from "./S3Find";

// 10-14.4s. LIVE capture of the Tailoring Studio for the same role: the
// summary with the job's keywords highlighted, then "Locked to your
// resume" (degree, employer and dates never altered) and the one-page PDF.

// Source-pixel rects in public/live/tailor.png (1075 px wide).
const SLICE_Y = { from: 473, to: 1110 };
const SUMMARY = { x: 164, y: 959, w: 760, h: 270 };
const LOCKED = { x: 60, y: 1479, w: 955, h: 210 };
const PDF_BUTTON = { x: 457, y: 2012, w: 430, h: 94 };

export const tailorGeom = (frame: number): ScreenGeom =>
  stageGeom([{ y: lerp(SLICE_Y.from, SLICE_Y.to, prog(frame, 60, 34, ease.inOut)), h: 1100 }]);

export const JobChip: React.FC<{ readonly style?: React.CSSProperties }> = ({ style }) => (
  <ContextChip lead={<InitialTile letter={JOB.initial} size={50} />} style={style}>
    <span style={{ color: COLORS.muted, fontWeight: 500 }}>For:</span>
    {JOB.title}
    <span style={{ color: COLORS.muted, fontWeight: 500 }}>· {JOB.company}</span>
  </ContextChip>
);

/** The studio screen exactly as this scene leaves it. */
export const TailorScreenFinal: React.FC<{ readonly style?: React.CSSProperties }> = ({ style }) => (
  <Screen src="live/tailor.png" geom={tailorGeom(SCENES.tailor - 1)} style={style} />
);

export const TailorScene: React.FC = () => {
  const frame = useCurrentFrame();

  const fold = prog(frame, 0, 18, ease.inOut);
  const candOut = prog(frame, 0, 10);
  const jobIn = prog(frame, 10, 12);
  const doc = prog(frame, 10, 22);
  const g = tailorGeom(frame);

  // Rings: the highlighted summary, then the locked facts and the PDF.
  // Every ring is gone before the hand-off to the next scene.
  const summary = prog(frame, 34, 10) * (1 - prog(frame, 56, 6));
  const locked = prog(frame, 96, 10) * (1 - prog(frame, 120, 6));
  const pdf = prog(frame, 106, 10) * (1 - prog(frame, 120, 6));

  return (
    <AbsoluteFill>
      {candOut < 1 ? <CandidateChip style={{ opacity: 1 - candOut }} /> : null}
      <JobChip style={{ opacity: jobIn, scale: String(lerp(0.9, 1, jobIn)) }} />

      {fold < 1 ? (
        <MatchScreenFinal
          style={{
            opacity: 1 - prog(frame, 2, 12),
            scale: String(lerp(1, 0.36, fold)),
            translate: `${lerp(0, -170, fold)}px ${lerp(0, -470, fold)}px`,
          }}
        />
      ) : null}

      {doc > 0 ? (
        <Screen src="live/tailor.png" geom={g} style={{ opacity: doc, translate: `0px ${(1 - doc) * 110}px` }} />
      ) : null}
      <Ring rect={toCanvas(g, SUMMARY)} t={summary} radius={18} />
      <Ring rect={toCanvas(g, LOCKED)} t={locked} radius={22} />
      <Ring rect={toCanvas(g, PDF_BUTTON)} t={pdf} radius={20} />

      <Headline lines={COPY.tailor} enterAt={4} exitAt={SCENES.tailor - 9} size={92} />
    </AbsoluteFill>
  );
};
