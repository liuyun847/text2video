/**
 * 最底层:纯色或图片背景。
 * 图片走 objectFit:cover 铺满 —— 上游已按画幅预切,这里只兜底防比例不一致。
 */
import React from "react";
import { AbsoluteFill, Img, staticFile, useVideoConfig } from "remotion";
import type { BackgroundSpec } from "../manifest";
import { normalizeColor } from "../manifest";

export const Background: React.FC<{ spec: BackgroundSpec }> = ({ spec }) => {
  const { width, height } = useVideoConfig();
  if (spec?.type === "image" && spec.path) {
    return (
      <AbsoluteFill>
        <Img
          src={staticFile(spec.path)}
          style={{ width, height, objectFit: "cover", display: "block" }}
        />
      </AbsoluteFill>
    );
  }
  const color = spec?.type === "color" ? normalizeColor(spec.color) : "#000000";
  return <AbsoluteFill style={{ backgroundColor: color }} />;
};
