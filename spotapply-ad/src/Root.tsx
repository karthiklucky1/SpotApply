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
import { Explainer60 } from "./w60/Explainer60";
import { W } from "./w60/layout";
import { TOTAL60 } from "./w60/timeline60";
import { CastSheet } from "./reel/dev/CastSheet";
import { CharacterSheet } from "./reel/dev/CharacterSheet";
import { Reel60 } from "./reel/Reel60";
import { REEL } from "./reel/timeline";
import { Demo5 } from "./reel2/Demo5";

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
        defaultProps={{ voiceover: true }}
        durationInFrames={TOTAL_FRAMES}
        fps={VIDEO.fps}
        width={VIDEO.width}
        height={VIDEO.height}
      />
      <Composition
        id="SpotApplyExplainer60"
        component={Explainer60}
        defaultProps={{ voiceover: true }}
        durationInFrames={TOTAL60}
        fps={W.fps}
        width={W.width}
        height={W.height}
      />
      <Composition
        id="SpotApplyReel60"
        component={Reel60}
        defaultProps={{ voiceover: true, music: true }}
        durationInFrames={REEL.frames}
        fps={REEL.fps}
        width={REEL.width}
        height={REEL.height}
      />
      <Folder name="Reel-tests">
        <Composition id="ReelTest-Photo" component={Demo5} defaultProps={{ withPhoto: true }} durationInFrames={150} fps={30} width={1080} height={1920} />
        <Composition id="ReelTest-AppOnly" component={Demo5} defaultProps={{ withPhoto: false }} durationInFrames={150} fps={30} width={1080} height={1920} />
      </Folder>
      <Folder name="Reel-dev">
        <Composition id="Reel-Character" component={CharacterSheet} durationInFrames={1} fps={30} width={1800} height={760} />
        <Composition id="Reel-Cast" component={CastSheet} durationInFrames={1} fps={30} width={1920} height={1400} />
      </Folder>
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
