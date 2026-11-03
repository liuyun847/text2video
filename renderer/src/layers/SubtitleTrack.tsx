/**
 * 字幕层:字幕由 Python 侧用 ASS 烧成"带 alpha 的视频"(webm/mov),
 * 本渲染端只当透明视频层叠加 —— 不重画文字、不碰字体。
 *
 * 关键点:OffthreadVideo 必须显式传 transparent,才会走"抽帧成 PNG"的分支、
 * 保住 alpha 通道;不传时抽帧产物没有 alpha,透明区会变成黑块。
 * 实测证据(素材、截图、结论)见 docs/alpha-验证.md。
 */
import React from "react";
import { AbsoluteFill, OffthreadVideo, staticFile, useVideoConfig } from "remotion";

export const SubtitleTrack: React.FC<{ spec: { path: string } }> = ({ spec }) => {
  const { width, height } = useVideoConfig();
  const src = spec?.path ?? "";
  if (!src) {
    return null; // 占位清单(Studio 默认)不挂字幕
  }
  return (
    <AbsoluteFill>
      <OffthreadVideo
        src={staticFile(src)}
        transparent
        muted
        style={{ width, height }}
      />
    </AbsoluteFill>
  );
};
