/**
 * Composition "TextVideo" 的根组件:层序自底向上固定为
 *
 *   Background(纯色/图片,cover)
 *     → MaterialLayer(素材段,淡入淡出)
 *       → AnnotationLayer(标注框,素材之上)
 *         → SubtitleTrack(透明字幕视频,标注之上)
 *           → AudioTrack(配音音轨)
 *
 * 尺寸 / fps / 总帧数不在这里写死,由 index.tsx 的 calculateMetadata 依清单现算。
 */
import React from "react";
import { AbsoluteFill, useVideoConfig } from "remotion";
import type { TextVideoProps } from "./manifest";
import { Background } from "./layers/Background";
import { MaterialLayer } from "./layers/MaterialLayer";
import { AnnotationLayer } from "./layers/AnnotationLayer";
import { SubtitleTrack } from "./layers/SubtitleTrack";
import { AudioTrack } from "./layers/AudioTrack";

export const TextVideo: React.FC<TextVideoProps> = ({ manifest }) => {
  const { fps } = useVideoConfig();
  return (
    // 兜底底色:成片编码为 h264 无 alpha,任何没被铺满的区域落黑而不是"透明"
    <AbsoluteFill style={{ backgroundColor: "#000000" }}>
      <Background spec={manifest.background} />
      <MaterialLayer materials={manifest.materials ?? []} fps={fps} />
      <AnnotationLayer annotations={manifest.annotations ?? []} fps={fps} />
      <SubtitleTrack spec={manifest.subtitle} />
      <AudioTrack spec={manifest.audio} />
    </AbsoluteFill>
  );
};
