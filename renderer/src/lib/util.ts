/**
 * 纯函数工具箱:帧化、淡入淡出、标注框像素换算。
 * 全部是纯函数(不碰 React/Remotion 运行时),方便单测与在 Node 侧复用同一套公式。
 */
import { interpolate } from "remotion";
import type { AnnotationSpec } from "../manifest";

/** 淡入淡出窗口(秒),缺省键视为 0 */
export type FadeWindow = { in?: number; out?: number };

/** 把值夹到 [lo, hi] */
export const clamp = (v: number, lo: number, hi: number): number =>
  Math.min(Math.max(v, lo), hi);

/**
 * 段内淡入淡出透明度。
 *
 * 语义对齐 ffmpeg 的 fade=t=in/out:alpha=1 —— 段内时钟,首帧 0 → 淡入结束 1,
 * 淡出起点 1 → 段末 0。长度按 round(秒*fps) 帧化,与 Sequence 的帧窗一致。
 *
 * @param frame 段内帧号(Sequence 内 useCurrentFrame,从 0 起)
 * @param lenFrames 本段总帧数
 * @param fade 淡入淡出秒数,可只给一边
 * @param fps 帧率
 * @returns 0~1 的透明度;fade 缺省时恒为 1(硬切)
 */
export const fadeOpacity = (
  frame: number,
  lenFrames: number,
  fade: FadeWindow | undefined,
  fps: number,
): number => {
  if (!fade || lenFrames <= 0) {
    return 1;
  }
  const inFrames = clamp(Math.round((fade.in ?? 0) * fps), 0, lenFrames);
  const outFrames = clamp(Math.round((fade.out ?? 0) * fps), 0, lenFrames);
  const fadeIn =
    inFrames > 0
      ? interpolate(frame, [0, inFrames], [0, 1], {
          extrapolateLeft: "clamp",
          extrapolateRight: "clamp",
        })
      : 1;
  const fadeOut =
    outFrames > 0
      ? interpolate(frame, [lenFrames - outFrames, lenFrames], [1, 0], {
          extrapolateLeft: "clamp",
          extrapolateRight: "clamp",
        })
      : 1;
  return Math.min(fadeIn, fadeOut);
};

/**
 * 标注框的像素矩形。与 ffmpeg drawbox 的换算一致:
 * x=round(x*W) y=round(y*H) w=round(w*W) h=round(h*H),边框向内画。
 *
 * @param a 清单里的标注框(0~1 归一化坐标)
 * @param width 画幅宽
 * @param height 画幅高
 * @returns 绝对定位用的 left/top/width/height(整数像素)
 */
export const annotationRect = (
  a: AnnotationSpec,
  width: number,
  height: number,
): { left: number; top: number; width: number; height: number } => ({
  left: Math.round(a.x * width),
  top: Math.round(a.y * height),
  width: Math.round(a.w * width),
  height: Math.round(a.h * height),
});

/**
 * 秒级时间窗 → 帧窗口,并夹到合成总长内。
 *
 * @param start 起点(秒)
 * @param end 终点(秒)
 * @param fps 帧率
 * @param totalFrames 合成总帧数
 * @returns from = 起始帧;lenFrames = 帧数(<=0 表示该段不出现)
 */
export const frameWindow = (
  start: number,
  end: number,
  fps: number,
  totalFrames: number,
): { from: number; lenFrames: number } => {
  const from = clamp(Math.round(start * fps), 0, totalFrames);
  const to = clamp(Math.round(end * fps), 0, totalFrames);
  return { from, lenFrames: to - from };
};
