import React from "react";
import { AbsoluteFill, useCurrentFrame } from "remotion";
import { ease, prog } from "../anim";
import { AppWindow, Chapter, FULL, Focus60, Note, cameraAt, toCanvas60, STAGE_WINDOW, type Focus } from "./components";
import { SCENES60 } from "./timeline60";

// 11-18.8s. LIVE (desktop): the dashboard, the "Apply today" early window
// with the Lumen Data role on top; then its match report opens and the camera
// reads the fit breakdown, which scores skills, experience, location and
// work authorization separately.

// Source px (2560x1600) in public/live60/board.png and match.png.
export const BOARD = {
  shortlist: { x: 400, y: 560, w: 1360 } as Focus, // stage tabs, the divider, the Lumen card
  lumenCard: { x: 446, y: 849, w: 1002, h: 470 }, // re-measured after each recapture
};
export const MATCH = {
  report: { x: 400, y: 540, w: 1520 } as Focus,
  fit: { x: 420, y: 680, w: 1460 } as Focus,
  fitGrid: { x: 462, y: 1124, w: 1372, h: 398 },
  why: { x: 430, y: 840, w: 1436, h: 170 },
};

const boardCam = (frame: number) =>
  cameraAt(frame, [
    [0, FULL],
    [34, FULL],
    [70, BOARD.shortlist],
  ]);
export const matchCam = (frame: number) =>
  cameraAt(frame, [
    [112, FULL],
    [124, FULL],
    [150, MATCH.report],
    [180, MATCH.report],
    [204, MATCH.fit],
  ]);

export const MatchFinalWindow: React.FC<{ readonly style?: React.CSSProperties }> = ({ style }) => (
  <AppWindow src="live60/match.png" focus={matchCam(SCENES60.find - 1)} style={style} />
);

export const FindScene60: React.FC = () => {
  const frame = useCurrentFrame();
  const enter = prog(frame, 6, 20);
  const bc = boardCam(frame);
  const drawer = prog(frame, 108, 18, ease.inOut);
  const mc = matchCam(frame);

  const cardRing = prog(frame, 74, 10) * (1 - prog(frame, 106, 6));
  const whyRing = prog(frame, 156, 10) * (1 - prog(frame, 186, 6));
  const fitRing = prog(frame, 208, 10) * (1 - prog(frame, 228, 5));

  return (
    <AbsoluteFill>
      {drawer < 1 ? (
        <AppWindow
          src="live60/board.png"
          focus={bc}
          style={{ opacity: enter * (1 - 0.6 * drawer), translate: `${(1 - enter) * 80}px 0px`, scale: String(1 - 0.03 * drawer) }}
        />
      ) : null}
      {drawer <= 0 ? <Focus60 rect={toCanvas60(STAGE_WINDOW, bc, BOARD.lumenCard)} t={cardRing} radius={26} /> : null}
      <Note x={1300} y={100} t={prog(frame, 78, 10) * (1 - prog(frame, 106, 6))}>
        ⚡ Posted 2 hours ago: apply today
      </Note>

      {drawer > 0 ? (
        <AppWindow src="live60/match.png" focus={mc} style={{ translate: `${(1 - drawer) * 1100}px 0px` }} />
      ) : null}
      <Focus60 rect={toCanvas60(STAGE_WINDOW, mc, MATCH.why)} t={whyRing} />
      <Focus60 rect={toCanvas60(STAGE_WINDOW, mc, MATCH.fitGrid)} t={fitRing} />
      <Note x={1240} y={100} t={prog(frame, 158, 10) * (1 - prog(frame, 186, 6))} tone="ink">
        Written out, so you can disagree
      </Note>
      <Note x={1140} y={100} t={prog(frame, 208, 10) * (1 - prog(frame, 228, 5))} tone="ok">
        Skills · experience · location · work auth
      </Note>

      <Chapter
        number="01"
        label="Find"
        headline={["Fresh roles", "that fit."]}
        support="Company career pages, checked around the clock. Only strong matches reach your board."
        enterAt={8}
        exitAt={104}
      />
      <Chapter
        number="02"
        label="Understand"
        headline={["See exactly", "why."]}
        support="Every match comes with the reasoning: skills, experience and location, each scored against your resume."
        enterAt={116}
        exitAt={SCENES60.find - 10}
      />
    </AbsoluteFill>
  );
};
