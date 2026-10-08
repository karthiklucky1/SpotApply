import React from "react";
import { Composition, Folder } from "remotion";
import { VIDEO } from "./brand";
import { HookScene } from "./scenes/S1Hook";
import { RevealScene } from "./scenes/S2Reveal";
import { FindScene } from "./scenes/S3Find";
import { TailorScene } from "./scenes/S4Tailor";
import { ApplyScene } from "./scenes/S5Apply";
import { TrackScene } from "./scenes/S6Track";
import { CtaScene } from "./scenes/S7Cta";
import { SocialAd30, withBackground } from "./SocialAd30";
import { SCENES, TOTAL_FRAMES } from "./timeline";
import "./fonts";

const Hook = withBackground(HookScene);
const Reveal = withBackground(RevealScene);
const Find = withBackground(FindScene);
const Tailor = withBackground(TailorScene);
const Apply = withBackground(ApplyScene);
const Track = withBackground(TrackScene);
const Cta = withBackground(CtaScene);

export const RemotionRoot: React.FC = () => {
  return (
    <>
      <Composition
        id="SpotApplySocial30"
        component={SocialAd30}
        durationInFrames={TOTAL_FRAMES}
        fps={VIDEO.fps}
        width={VIDEO.width}
        height={VIDEO.height}
      />
      <Folder name="Scenes">
        <Composition id="S1-Hook" component={Hook} durationInFrames={SCENES.hook} fps={VIDEO.fps} width={VIDEO.width} height={VIDEO.height} />
        <Composition id="S2-Reveal" component={Reveal} durationInFrames={SCENES.reveal} fps={VIDEO.fps} width={VIDEO.width} height={VIDEO.height} />
        <Composition id="S3-Find" component={Find} durationInFrames={SCENES.find} fps={VIDEO.fps} width={VIDEO.width} height={VIDEO.height} />
        <Composition id="S4-Tailor" component={Tailor} durationInFrames={SCENES.tailor} fps={VIDEO.fps} width={VIDEO.width} height={VIDEO.height} />
        <Composition id="S5-Apply" component={Apply} durationInFrames={SCENES.apply} fps={VIDEO.fps} width={VIDEO.width} height={VIDEO.height} />
        <Composition id="S6-Track" component={Track} durationInFrames={SCENES.track} fps={VIDEO.fps} width={VIDEO.width} height={VIDEO.height} />
        <Composition id="S7-EndCard" component={Cta} durationInFrames={SCENES.cta} fps={VIDEO.fps} width={VIDEO.width} height={VIDEO.height} />
      </Folder>
    </>
  );
};
