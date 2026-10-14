import React from "react";
import { AbsoluteFill, useCurrentFrame } from "remotion";
import { ease, lerp, prog } from "../anim";
import { COPY } from "../copy";
import { Headline } from "../components/Headline";
import { Ring, Screen, StageClip, stageGeom, toCanvas } from "../components/Screen";
import { SCENES } from "../timeline";
import { JobChip } from "./S4Tailor";
import { ApplyForm } from "./S5Apply";

// 20.2-25.2s. LIVE captures, same role: the Submitted tab with the app's
// earliness receipt ("Applied 1h 48m after posting", a measured clock, never
// odds), then the outreach kit's email draft with its one-click send links.
// Nothing here shows a reply, an interview or an offer.

// Source-pixel rects in public/live/submitted.png and outreach.png.
const SUBMITTED = stageGeom([{ y: 222, h: 1125 }], 820);
const RECEIPT = { x: 92, y: 698, w: 434, h: 70 };
const OUTREACH = stageGeom(
  [
    { y: 140, h: 262 }, // "Outreach kit" header + what it does
    { y: 788, h: 720 }, // the hiring-manager email draft
  ],
  840,
  690,
);
const SEND_LINKS = { x: 378, y: 1334, w: 556, h: 68 };

export const TrackScene: React.FC = () => {
  const frame = useCurrentFrame();

  // The form clears before the tracker arrives, so two UIs never overlap.
  const formOut = prog(frame, 0, 10, ease.out);
  const chipOut = prog(frame, 0, 10);
  const board = prog(frame, 9, 18);
  const receipt = prog(frame, 30, 10) * (1 - prog(frame, 66, 6));

  const drawer = prog(frame, 70, 22, ease.inOut);
  const links = prog(frame, 100, 10) * (1 - prog(frame, 134, 6));

  return (
    <AbsoluteFill>
      {chipOut < 1 ? <JobChip style={{ opacity: 1 - chipOut }} /> : null}
      {formOut < 1 ? (
        <ApplyForm frame={999} style={{ opacity: 1 - formOut, scale: String(lerp(1, 0.86, formOut)) }} />
      ) : null}

      <Screen
        src="live/submitted.png"
        geom={SUBMITTED}
        style={{
          opacity: board * (1 - 0.45 * drawer),
          translate: `0px ${(1 - board) * 80}px`,
          scale: String(lerp(1, 0.95, drawer)),
        }}
      />
      <Ring rect={toCanvas(SUBMITTED, RECEIPT)} t={receipt} radius={40} />

      {drawer > 0 ? (
        <StageClip>
          <Screen src="live/outreach.png" geom={OUTREACH} style={{ translate: `0px ${(1 - drawer) * 860}px` }} />
        </StageClip>
      ) : null}
      {drawer >= 1 ? <Ring rect={toCanvas(OUTREACH, SEND_LINKS, 1)} t={links} radius={36} /> : null}

      <Headline lines={COPY.early} enterAt={4} exitAt={68} />
      <Headline lines={COPY.followUp} enterAt={76} exitAt={SCENES.track - 9} />
    </AbsoluteFill>
  );
};
