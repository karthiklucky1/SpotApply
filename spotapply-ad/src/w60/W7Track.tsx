import React from "react";
import { AbsoluteFill, useCurrentFrame } from "remotion";
import { ease, prog } from "../anim";
import { AppWindow, Chapter, FULL, Focus60, Note, cameraAt, toCanvas60, STAGE_WINDOW, type Focus } from "./components";
import { ApplyFinalWindow } from "./W6Apply";
import { SCENES60 } from "./timeline60";

// 44.6-51s. LIVE (desktop): the Submitted tab with the earliness receipt on
// the same role, then the Outreach kit's email draft with its send links.
// No reply, interview or offer is shown.

// Source px in public/live60/submitted.png and outreach.png (re-measured
// after each recapture).
export const SUBMITTED = {
  focus: { x: 420, y: 440, w: 1300 } as Focus, // stage tabs, the early line, the Lumen card
  receipt: { x: 846, y: 758, w: 354, h: 48 },
};
export const OUTREACH = {
  top: { x: 580, y: 70, w: 1400 } as Focus, // the kit opens at its title
  email: { x: 580, y: 620, w: 1400 } as Focus, // then reads down to the email draft
  sendLinks: { x: 902, y: 1338, w: 452, h: 60 },
};

const subCam = (frame: number) =>
  cameraAt(frame, [
    [0, FULL],
    [16, FULL],
    [44, SUBMITTED.focus],
  ]);
const outCam = (frame: number) =>
  cameraAt(frame, [
    [96, OUTREACH.top],
    [112, OUTREACH.top],
    [136, OUTREACH.email],
  ]);

export const TrackFinalWindow: React.FC<{ readonly style?: React.CSSProperties }> = ({ style }) => (
  <AppWindow src="live60/outreach.png" focus={outCam(SCENES60.track - 1)} style={style} />
);

export const TrackScene60: React.FC = () => {
  const frame = useCurrentFrame();
  const prevOut = prog(frame, 0, 12);
  const enter = prog(frame, 4, 18);
  const sc = subCam(frame);
  const drawer = prog(frame, 94, 18, ease.inOut);
  const oc = outCam(frame);

  const receipt = prog(frame, 48, 10) * (1 - prog(frame, 92, 6));
  const links = prog(frame, 138, 10) * (1 - prog(frame, SCENES60.track - 12, 6));

  return (
    <AbsoluteFill>
      {prevOut < 1 ? <ApplyFinalWindow style={{ opacity: 1 - prevOut, translate: `${-60 * prevOut}px 0px` }} /> : null}
      {drawer < 1 ? (
        <AppWindow
          src="live60/submitted.png"
          focus={sc}
          style={{ opacity: enter * (1 - 0.6 * drawer), translate: `${(1 - enter) * 80}px 0px`, scale: String(1 - 0.03 * drawer) }}
        />
      ) : null}
      {drawer <= 0 ? <Focus60 rect={toCanvas60(STAGE_WINDOW, sc, SUBMITTED.receipt)} t={receipt} radius={30} /> : null}
      <Note x={1250} y={100} t={prog(frame, 52, 10) * (1 - prog(frame, 92, 6))}>
        A measured clock, not a promise
      </Note>

      {drawer > 0 ? <AppWindow src="live60/outreach.png" focus={oc} style={{ translate: `${(1 - drawer) * 1100}px 0px` }} /> : null}
      <Focus60 rect={toCanvas60(STAGE_WINDOW, oc, OUTREACH.sendLinks)} t={links} radius={30} />
      <Note x={1180} y={100} t={prog(frame, 120, 10) * (1 - prog(frame, SCENES60.track - 12, 6))} tone="ink">
        Drafts only: you send from your own account
      </Note>

      <Chapter
        number="05"
        label="Track"
        headline={["Know how early", "you applied."]}
        support="Every application in one place, with a receipt of how soon you applied and a ready-made follow-up email."
        enterAt={6}
        exitAt={SCENES60.track - 10}
      />
    </AbsoluteFill>
  );
};
