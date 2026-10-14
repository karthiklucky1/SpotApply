import React from "react";
import { Audio } from "@remotion/media";
import { AbsoluteFill, Sequence, Series, staticFile, useCurrentFrame, useVideoConfig } from "remotion";
import { prog } from "../anim";
import { FONTS } from "../brand";
import "../fonts";
import { ChapterRail, DemoTag60, LightBg } from "./components";
import { START60, SCENES60, VOICEOVER60 } from "./timeline60";
import { ProblemScene } from "./W1Problem";
import { IntroScene } from "./W2Intro";
import { FindScene60 } from "./W3Find";
import { SponsorScene60 } from "./W4Sponsor";
import { TailorScene60 } from "./W5Tailor";
import { ApplyScene60 } from "./W6Apply";
import { TrackScene60 } from "./W7Track";
import { RecapScene60 } from "./W8Recap";
import { CtaScene60 } from "./W9Cta";

export type Explainer60Props = { readonly voiceover: boolean };

// The 60-second widescreen explainer. The light chapters (find → track) share
// one persistent background, chapter rail and demo-data label; the problem
// and end card are dark bookends that bring their own.
export const Explainer60: React.FC<Explainer60Props> = ({ voiceover }) => {
  const { fps } = useVideoConfig();
  const frame = useCurrentFrame();

  const lightOn = frame >= START60.find && frame < START60.cta;
  const rail = prog(frame, START60.find + 6, 14) * (1 - prog(frame, START60.recap + 70, 12));
  const demo = prog(frame, START60.intro + 30, 12) * (1 - prog(frame, START60.recap, 8));

  return (
    <AbsoluteFill style={{ fontFamily: FONTS.sans, background: "#FFFFFF" }}>
      {lightOn ? <LightBg /> : null}
      <Series>
        <Series.Sequence name="1 Problem" durationInFrames={SCENES60.problem} premountFor={fps}>
          <ProblemScene />
        </Series.Sequence>
        <Series.Sequence name="2 Intro" durationInFrames={SCENES60.intro} premountFor={fps}>
          <IntroScene />
        </Series.Sequence>
        <Series.Sequence name="3 Find + why" durationInFrames={SCENES60.find} premountFor={fps}>
          <FindScene60 />
        </Series.Sequence>
        <Series.Sequence name="4 Sponsorship" durationInFrames={SCENES60.sponsor} premountFor={fps}>
          <SponsorScene60 />
        </Series.Sequence>
        <Series.Sequence name="5 Tailor" durationInFrames={SCENES60.tailor} premountFor={fps}>
          <TailorScene60 />
        </Series.Sequence>
        <Series.Sequence name="6 Fill, review, submit" durationInFrames={SCENES60.apply} premountFor={fps}>
          <ApplyScene60 />
        </Series.Sequence>
        <Series.Sequence name="7 Track" durationInFrames={SCENES60.track} premountFor={fps}>
          <TrackScene60 />
        </Series.Sequence>
        <Series.Sequence name="8 Recap" durationInFrames={SCENES60.recap} premountFor={fps}>
          <RecapScene60 />
        </Series.Sequence>
        <Series.Sequence name="9 End card" durationInFrames={SCENES60.cta} premountFor={fps}>
          <CtaScene60 />
        </Series.Sequence>
      </Series>
      <ChapterRail frame={frame} opacity={rail} allDone={frame >= START60.recap} />
      <DemoTag60 opacity={demo} />
      {voiceover
        ? VOICEOVER60.map((line) => (
            <Sequence
              key={line.id}
              name={`VO ${line.id}`}
              from={START60[line.id] + line.lead}
              durationInFrames={line.frames}
              premountFor={fps}
            >
              <Audio src={staticFile(`voiceover/w_${line.id}.wav`)} />
            </Sequence>
          ))
        : null}
    </AbsoluteFill>
  );
};
