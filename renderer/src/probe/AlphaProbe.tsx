/**
 * alpha 关键验证用的探针 Composition —— 不是生产路径,只为"肉眼可判"。
 *
 * 左半屏:<OffthreadVideo transparent /> 叠在亮青色背景上,透明区应透出青色;
 * 右半屏:同一段透明 webm 不传 transparent,用作对照。
 * 背景刻意用高饱和亮色:一旦 alpha 丢失变成黑块/黑底,截图上一眼可见。
 *
 * 素材 public/probe/alpha-probe.webm 由 scripts/probe-alpha.mjs 生成/复制。
 */
import React from "react";
import { AbsoluteFill, OffthreadVideo, staticFile } from "remotion";

export const ALPHA_PROBE = {
  id: "AlphaProbe",
  width: 1080,
  height: 1080,
  fps: 60,
  durationInFrames: 120, // 2 s
  /** public/ 下的相对路径 */
  video: "probe/alpha-probe.webm",
  /** 单块贴图边长(探针视频自身分辨率 540x540) */
  tile: 540,
  labelFont: '"Microsoft YaHei", "Microsoft YaHei UI", sans-serif',
};

export const AlphaProbe: React.FC = () => {
  const half = ALPHA_PROBE.width / 2;
  const tile = ALPHA_PROBE.tile;
  const top = (ALPHA_PROBE.height - tile) / 2;
  const label: React.CSSProperties = {
    position: "absolute",
    bottom: 48,
    fontFamily: ALPHA_PROBE.labelFont,
    fontSize: 30,
    color: "#000000",
  };
  return (
    <AbsoluteFill>
      <AbsoluteFill
        style={{ backgroundColor: "#00E0FF", width: half, height: ALPHA_PROBE.height, left: 0 }}
      />
      <AbsoluteFill
        style={{ backgroundColor: "#FFD400", width: half, height: ALPHA_PROBE.height, left: half }}
      />
      <OffthreadVideo
        src={staticFile(ALPHA_PROBE.video)}
        transparent
        muted
        style={{ position: "absolute", left: 0, top, width: tile, height: tile }}
      />
      <OffthreadVideo
        src={staticFile(ALPHA_PROBE.video)}
        muted
        style={{ position: "absolute", left: half, top, width: tile, height: tile }}
      />
      <div style={{ ...label, left: 24 }}>transparent=true(透明区应透出青色)</div>
      <div style={{ ...label, left: half + 24 }}>transparent=false(对照组)</div>
    </AbsoluteFill>
  );
};
