import React from "react";
import { AbsoluteFill, Series, useCurrentFrame, useVideoConfig } from "remotion";
import { prog } from "./anim";
import { FONTS } from "./brand";
import { Background } from "./components/Background";
import { DemoLabel } from "./components/DemoLabel";
import { RevealScene } from "./scenes/S2Reveal";
import { HookScene } from "./scenes/S1Hook";
import { FindScene } from "./scenes/S3Find";
import { TailorScene } from "./scenes/S4Tailor";
import { ApplyScene } from "./scenes/S5Apply";
import { TrackScene } from "./scenes/S6Track";
import { CtaScene } from "./scenes/S7Cta";
import { SCENES } from "./timeline";
import "./fonts";

// The 30-second vertical ad. Scenes play back to back; each one starts from
// the exact last frame of the one before, so the hand-offs read as one
// continuous piece of UI rather than cuts.
export const SocialAd30: React.FC = () => {
  const { fps } = useVideoConfig();
  const frame = useCurrentFrame();

  // The demo-data note covers every illustrated product scene (reveal to
  // tracker) and is gone before the end card.
  const demoStart = SCENES.hook + 10;
  const demoEnd = SCENES.hook + SCENES.reveal + SCENES.find + SCENES.tailor + SCENES.apply + SCENES.track - 6;
  const demo = prog(frame, demoStart, 10) * (1 - prog(frame, demoEnd, 6));

  return (
    <AbsoluteFill style={{ fontFamily: FONTS.sans }}>
      <Background />
      <Series>
        <Series.Sequence name="1 Hook" durationInFrames={SCENES.hook} premountFor={fps}>
          <HookScene />
        </Series.Sequence>
        <Series.Sequence name="2 Reveal" durationInFrames={SCENES.reveal} premountFor={fps}>
          <RevealScene />
        </Series.Sequence>
        <Series.Sequence name="3 Find + understand" durationInFrames={SCENES.find} premountFor={fps}>
          <FindScene />
        </Series.Sequence>
        <Series.Sequence name="4 Tailor" durationInFrames={SCENES.tailor} premountFor={fps}>
          <TailorScene />
        </Series.Sequence>
        <Series.Sequence name="5 Fill + review + submit" durationInFrames={SCENES.apply} premountFor={fps}>
          <ApplyScene />
        </Series.Sequence>
        <Series.Sequence name="6 Track" durationInFrames={SCENES.track} premountFor={fps}>
          <TrackScene />
        </Series.Sequence>
        <Series.Sequence name="7 End card" durationInFrames={SCENES.cta} premountFor={fps}>
          <CtaScene />
        </Series.Sequence>
      </Series>
      <DemoLabel opacity={demo} />
    </AbsoluteFill>
  );
};

/** A single scene on the ad's background, for previewing it alone. */
export const withBackground = (Scene: React.FC): React.FC => {
  const Framed: React.FC = () => (
    <AbsoluteFill style={{ fontFamily: FONTS.sans }}>
      <Background />
      <Scene />
    </AbsoluteFill>
  );
  Framed.displayName = `Framed(${Scene.displayName ?? Scene.name})`;
  return Framed;
};
