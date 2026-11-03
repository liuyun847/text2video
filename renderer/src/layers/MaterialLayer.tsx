/**
 * 素材层:每段按 start/end(秒,绝对时间)出现在素材之上、标注之下。
 *
 * 帧化统一走 round(sec*fps),段内淡入淡出用 opacity 插值(见 lib/util.ts fadeOpacity),
 * 视觉语义对齐 ffmpeg 的 fade=alpha=1。素材已由上游预切到输出画幅,
 * 这里只做 objectFit:cover 兜底,不做二次裁剪。
 */
import React from "react";
import {
  AbsoluteFill,
  Img,
  OffthreadVideo,
  Sequence,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import type { MaterialSpec } from "../manifest";
import { fadeOpacity, frameWindow } from "../lib/util";

/** 单段素材:铺满画幅 + 淡入淡出。frame 是 Sequence 内相对帧 */
const MaterialClip: React.FC<{ m: MaterialSpec; lenFrames: number; fps: number }> = ({
  m,
  lenFrames,
  fps,
}) => {
  const frame = useCurrentFrame();
  const { width, height } = useVideoConfig();
  const opacity = fadeOpacity(frame, lenFrames, m.fade, fps);
  const style: React.CSSProperties = { width, height, objectFit: "cover", opacity };
  if (m.type === "video") {
    // muted:素材自带音轨一律丢弃(与 text2video 的 -map 只取配音流一致)
    return <OffthreadVideo src={staticFile(m.path)} muted style={style} />;
  }
  return <Img src={staticFile(m.path)} style={style} />;
};

export const MaterialLayer: React.FC<{ materials: MaterialSpec[]; fps: number }> = ({
  materials,
  fps,
}) => {
  const { durationInFrames } = useVideoConfig();
  return (
    <AbsoluteFill>
      {materials.map((m, i) => {
        const { from, lenFrames } = frameWindow(m.start, m.end, fps, durationInFrames);
        if (lenFrames <= 0) {
          return null; // 空段 / 完全越界:不挂载,渲染前 render.mjs 已就此告警
        }
        return (
          <Sequence
            key={"material-" + i}
            from={from}
            durationInFrames={lenFrames}
            name={"素材 " + (i + 1) + " (" + m.type + ")"}
          >
            <MaterialClip m={m} lenFrames={lenFrames} fps={fps} />
          </Sequence>
        );
      })}
    </AbsoluteFill>
  );
};
