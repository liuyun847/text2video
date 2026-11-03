/**
 * 音轨层:配音 mp3(上游已把 BGM 混进同一条音轨,BGM 混音在 Python 侧完成)。
 * 组件不设 volume —— 绝对电平由上游决定,渲染端不二次增益。
 */
import React from "react";
import { Audio, staticFile } from "remotion";

export const AudioTrack: React.FC<{ spec: { path: string } }> = ({ spec }) => {
  const src = spec?.path ?? "";
  if (!src) {
    return null; // 占位清单(Studio 默认)不挂音轨
  }
  return <Audio src={staticFile(src)} />;
};
