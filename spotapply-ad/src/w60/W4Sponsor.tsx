import React from "react";
import { AbsoluteFill, useCurrentFrame } from "remotion";
import { prog } from "../anim";
import { AppWindow, Chapter, Focus60, Note, cameraAt, toCanvas60, STAGE_WINDOW, type Focus } from "./components";
import { MatchFinalWindow } from "./W3Find";
import { SCENES60 } from "./timeline60";

// 18.8-24.2s. LIVE (desktop): further down the same match report, the
// sponsorship card, as the app shows it to a candidate who NEEDS sponsorship
// (the demo candidate is a citizen, so this one capture uses a sponsorship-
// needing profile, and says so on screen). A research signal with "check
// employer requirements" beside it, never a promise.

// Source px in public/live60/match_sponsor.png.
// The card spans the report's full width, so the camera eases in only as far
// as keeps the whole card (and the note under it) inside the window.
const WIDE: Focus = { x: 360, y: 560, w: 1600 };
const CLOSE: Focus = { x: 400, y: 640, w: 1500 };
const CARD = { x: 432, y: 1170, w: 1424, h: 164 };

const camera = (frame: number) =>
  cameraAt(frame, [
    [0, WIDE],
    [22, WIDE],
    [56, CLOSE],
  ]);

export const SponsorFinalWindow: React.FC<{ readonly style?: React.CSSProperties }> = ({ style }) => (
  <AppWindow src="live60/match_sponsor.png" focus={camera(SCENES60.sponsor - 1)} style={style} />
);

export const SponsorScene60: React.FC = () => {
  const frame = useCurrentFrame();
  const scroll = prog(frame, 0, 14);
  const cam = camera(frame);
  const ring = prog(frame, 56, 10) * (1 - prog(frame, SCENES60.sponsor - 12, 6));
  const card = toCanvas60(STAGE_WINDOW, cam, CARD);

  return (
    <AbsoluteFill>
      {/* The report scrolls down to the sponsorship card. */}
      {scroll < 1 ? <MatchFinalWindow style={{ opacity: 1 - scroll }} /> : null}
      <AppWindow src="live60/match_sponsor.png" focus={cam} style={{ opacity: scroll }} />
      <Focus60 rect={card} t={ring} />

      <Note x={1100} y={100} t={prog(frame, 20, 10) * (1 - prog(frame, SCENES60.sponsor - 12, 6))} tone="ink">
        Shown for a candidate who needs sponsorship
      </Note>
      <Note
        x={card.x + card.w - 470}
        y={card.y + card.h + 22}
        t={prog(frame, 70, 10) * (1 - prog(frame, SCENES60.sponsor - 12, 6))}
      >
        Check employer requirements
      </Note>

      <Chapter
        number="02"
        label="Understand"
        headline={["Sponsorship,", "researched."]}
        support="What the posting says and whether public filing records exist. A signal to check with the employer, never a promise."
        enterAt={6}
        exitAt={SCENES60.sponsor - 10}
      />
    </AbsoluteFill>
  );
};
