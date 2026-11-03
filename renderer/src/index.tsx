/**
 * Remotion 入口:注册 Composition。
 *   TextVideo  —— 生产用,尺寸/fps/时长全部由传入清单经 calculateMetadata 决定
 *   AlphaProbe —— 透明字幕视频(OffthreadVideo + alpha)的一次性验证探针
 */
import React from "react";
import { Composition, registerRoot } from "remotion";
import { TextVideo } from "./TextVideo";
import { BLANK_MANIFEST, computeMetadata } from "./manifest";
import { ALPHA_PROBE, AlphaProbe } from "./probe/AlphaProbe";

const RemotionRoot: React.FC = () => (
  <>
    <Composition
      id="TextVideo"
      component={TextVideo}
      defaultProps={{ manifest: BLANK_MANIFEST }}
      calculateMetadata={computeMetadata}
    />
    <Composition
      id={ALPHA_PROBE.id}
      component={AlphaProbe}
      durationInFrames={ALPHA_PROBE.durationInFrames}
      fps={ALPHA_PROBE.fps}
      width={ALPHA_PROBE.width}
      height={ALPHA_PROBE.height}
    />
  </>
);

registerRoot(RemotionRoot);
