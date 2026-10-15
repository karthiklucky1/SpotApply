import React from "react";
import { Audio } from "@remotion/media";
import {
  AbsoluteFill,
  getStaticFiles,
  interpolate,
  OffthreadVideo,
  Sequence,
  staticFile,
} from "remotion";
import "../fonts";
import { Captions } from "./Captions";
import { CAST, type CastId, CastContext } from "./Character";
import { EndScene, LessScene } from "./scenes/Close";
import {
  HookScene,
  RetypeScene,
  RewriteScene,
  TabsScene,
} from "./scenes/Frustration";
import { MeetScene } from "./scenes/Meet";
import { FillScene, FindScene, TailorScene, WhyScene } from "./scenes/Product";
import { type SceneId, SCENES, SFX, speaking, VOICEOVER } from "./timeline";
import { DemoTag, Headline } from "./ui";

// `character` picks the illustrated job seeker (src/reel/Character.tsx CAST).
export type Reel60Props = {
  readonly voiceover: boolean;
  readonly music: boolean;
  readonly character?: CastId;
};

// Real b-roll can replace any illustrated moment: drop a vertical clip at
// public/reel/broll/<scene>.mp4 (1080x1920, see README "Shoot your own b-roll")
// and it plays under the same headline, captions and sound. Without one, the
// illustration plays.
const BROLL_TEXT: Partial<Record<SceneId, { lines: string[]; size?: number }>> =
  {
    hook: { lines: ["Job hunting", "= a *full-time job*"] },
    tabs: { lines: ["12 tabs open"], size: 70 },
    rewrite: { lines: ["Rewriting the same resume.", "*Again.*"], size: 56 },
    retype: {
      lines: ["Uploaded your resume?", "Now *type it again.*"],
      size: 60,
    },
    less: {
      lines: [
        "Less time on *repetitive work.*",
        "More time on",
        "*meaningful applications.*",
      ],
      size: 52,
    },
  };

const BRoll: React.FC<{ readonly id: SceneId; readonly src: string }> = ({
  id,
  src,
}) => (
  <AbsoluteFill style={{ background: "#08202F" }}>
    <OffthreadVideo
      src={src}
      muted
      style={{ width: 1080, height: 1920, objectFit: "cover" }}
    />
    {BROLL_TEXT[id] ? (
      <Headline
        lines={BROLL_TEXT[id]!.lines}
        at={6}
        size={BROLL_TEXT[id]!.size}
      />
    ) : null}
    <DemoTag opacity={1} dark text="Dramatization" />
  </AbsoluteFill>
);

const SCENE_COMPONENTS: Record<SceneId, React.FC> = {
  hook: HookScene,
  tabs: TabsScene,
  rewrite: RewriteScene,
  retype: RetypeScene,
  meet: MeetScene,
  find: FindScene,
  why: WhyScene,
  tailor: TailorScene,
  fill: FillScene,
  less: LessScene,
  end: EndScene,
};

// Music sits ~12 dB under the voice while anyone speaks and comes up between
// lines; the end card lets it carry the last seconds.
const musicVolume = (f: number) => {
  let near = 0;
  for (let d = -6; d <= 6; d += 3) if (speaking(f + d)) near++;
  const duck = near / 5; // 0..1, a soft ramp around each line
  const base = f >= SCENES.end[0] + 60 ? 0.42 : 0.34;
  const fadeIn = interpolate(f, [0, 6], [0, 1], { extrapolateRight: "clamp" });
  return fadeIn * (base - (base - 0.1) * duck);
};

export const Reel60: React.FC<Reel60Props> = ({
  voiceover,
  music,
  character = "maya",
}) => {
  const files = new Set(getStaticFiles().map((f) => f.name));
  return (
    <CastContext.Provider value={CAST[character]}>
      <AbsoluteFill style={{ background: "#08202F" }}>
        {(Object.keys(SCENES) as SceneId[]).map((id) => {
          const [from, len] = SCENES[id];
          const broll = `reel/broll/${id}.mp4`;
          const Comp = SCENE_COMPONENTS[id];
          return (
            <Sequence
              key={id}
              name={id}
              from={from}
              durationInFrames={len}
              premountFor={30}
            >
              {files.has(broll) && BROLL_TEXT[id] ? (
                <BRoll id={id} src={staticFile(broll)} />
              ) : (
                <Comp />
              )}
            </Sequence>
          );
        })}
        <Captions />
        {voiceover
          ? VOICEOVER.map((v) => (
              <Sequence
                key={v.id}
                name={`VO ${v.id}`}
                from={v.from}
                durationInFrames={v.frames}
                premountFor={30}
              >
                <Audio src={staticFile(`reel/vo/${v.id}.wav`)} />
              </Sequence>
            ))
          : null}
        {music ? (
          <Audio src={staticFile("reel/music.wav")} volume={musicVolume} />
        ) : null}
        {SFX.map((s, i) => (
          <Sequence
            key={i}
            name={`SFX ${s.id}`}
            from={s.at}
            durationInFrames={60}
            premountFor={30}
          >
            <Audio src={staticFile(`reel/sfx/${s.id}.wav`)} volume={s.gain} />
          </Sequence>
        ))}
      </AbsoluteFill>
    </CastContext.Provider>
  );
};
