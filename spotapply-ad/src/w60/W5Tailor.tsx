import React from "react";
import { AbsoluteFill, useCurrentFrame } from "remotion";
import { prog } from "../anim";
import { AppWindow, Chapter, Focus60, Note, cameraAt, toCanvas60, STAGE_WINDOW, type Focus } from "./components";
import { SponsorFinalWindow } from "./W4Sponsor";
import { SCENES60 } from "./timeline60";

// 24.2-33.6s. LIVE: the Tailoring Studio (desktop). The camera reads the
// summary with the posting's keywords highlighted, then "Locked to your
// resume", switches to the cover letter, and pulls back to the one-page PDF
// and Word downloads.

// Source px in public/live60/tailor.png and tailor_cover.png (2560x1600).
// The lines under the resume pane (legend, Locked, Recruiter's read) start at
// the modal's left padding, so a push-in keeps that padding in frame and stops
// above the buttons: a tighter crop slices those lines mid-word.
const STUDIO: Focus = { x: 180, y: 40, w: 2220 };
const SUMMARY_FOCUS: Focus = { x: 220, y: 289, w: 1810 };
const PULLBACK: Focus = { x: 190, y: 186, w: 2200 };
const SUMMARY = { x: 544, y: 538, w: 1440, h: 136 };
const LOCKED = { x: 248, y: 1128, w: 2064, h: 130 };
const DOWNLOADS = { x: 564, y: 1436, w: 616, h: 74 };

const T = {
  summaryRing: [44, 92],
  lockedRing: [100, 140],
  cover: 150, // switch to the cover letter tab
  back: 206, // back to the resume, pulled back
  downloads: [214, 262],
} as const;

const camera = (frame: number): Focus =>
  cameraAt(frame, [
    [0, STUDIO],
    [16, STUDIO],
    [40, SUMMARY_FOCUS],
    [96, SUMMARY_FOCUS],
    [122, PULLBACK],
  ]);

export const TailorFinalWindow: React.FC<{ readonly style?: React.CSSProperties }> = ({ style }) => (
  <AppWindow src="live60/tailor.png" focus={camera(SCENES60.tailor - 1)} style={style} />
);

export const TailorScene60: React.FC = () => {
  const frame = useCurrentFrame();
  const enter = prog(frame, 4, 18);
  const prevOut = prog(frame, 0, 12);
  const cam = camera(frame);
  const cover = prog(frame, T.cover, 8) * (1 - prog(frame, T.back, 8));
  const coverOn = cover > 0.5;
  const ring = (a: readonly [number, number]) => prog(frame, a[0], 10) * (1 - prog(frame, a[1], 8));

  return (
    <AbsoluteFill>
      {prevOut < 1 ? (
        <SponsorFinalWindow style={{ opacity: 1 - prevOut, translate: `${-60 * prevOut}px 0px`, scale: String(1 - 0.03 * prevOut) }} />
      ) : null}

      <AppWindow src="live60/tailor.png" focus={cam} style={{ opacity: enter, translate: `${(1 - enter) * 80}px 0px` }} />
      {/* The cover letter tab, crossfaded over the same window. */}
      {cover > 0 ? <AppWindow src="live60/tailor_cover.png" focus={cam} style={{ opacity: cover }} /> : null}
      {!coverOn ? <Focus60 rect={toCanvas60(STAGE_WINDOW, cam, SUMMARY)} t={ring(T.summaryRing)} /> : null}
      {!coverOn ? <Focus60 rect={toCanvas60(STAGE_WINDOW, cam, LOCKED)} t={ring(T.lockedRing)} /> : null}
      {!coverOn ? <Focus60 rect={toCanvas60(STAGE_WINDOW, cam, DOWNLOADS)} t={ring(T.downloads)} radius={14} /> : null}

      <Note x={1180} y={100} t={prog(frame, 50, 10) * (1 - prog(frame, 92, 8))}>
        Keywords from the posting, highlighted
      </Note>
      <Note x={1260} y={100} t={prog(frame, 104, 10) * (1 - prog(frame, 142, 8))} tone="ok">
        Degree, employer and dates: never altered
      </Note>
      <Note x={1440} y={100} t={prog(frame, 156, 10) * (1 - prog(frame, 200, 8))} tone="ink">
        Cover letter, same rules
      </Note>
      <Note x={1290} y={100} t={prog(frame, 220, 10) * (1 - prog(frame, 262, 8))}>
        One-page PDF and Word
      </Note>

      <Chapter
        number="03"
        label="Prepare"
        headline={["Tailored,", "not invented."]}
        support="Your resume and cover letter, rewritten from what you've actually done. Degree, employers and dates stay locked."
        enterAt={6}
        exitAt={SCENES60.tailor - 10}
      />
    </AbsoluteFill>
  );
};
