/**
 * 标注框层:置于素材之上、字幕之下。
 *
 * 坐标是 0~1 归一化,换算成像素时用 round(与 ffmpeg drawbox 同一套取整),
 * 边框 6 px 且向内画(box-sizing: border-box)= drawbox 的 t=6 语义。
 */
import React from "react";
import { Sequence, useVideoConfig } from "remotion";
import type { AnnotationSpec } from "../manifest";
import { normalizeColor } from "../manifest";
import { annotationRect, frameWindow } from "../lib/util";

/** 边框宽度(px),契约固定 6 */
export const ANNOTATION_BORDER_PX = 6;

const AnnotationBox: React.FC<{ a: AnnotationSpec }> = ({ a }) => {
  const { width, height } = useVideoConfig();
  const rect = annotationRect(a, width, height);
  return (
    <div
      style={{
        position: "absolute",
        left: rect.left,
        top: rect.top,
        width: rect.width,
        height: rect.height,
        boxSizing: "border-box",
        border: ANNOTATION_BORDER_PX + "px solid " + normalizeColor(a.color),
        backgroundColor: "transparent",
      }}
    />
  );
};

export const AnnotationLayer: React.FC<{ annotations: AnnotationSpec[]; fps: number }> = ({
  annotations,
  fps,
}) => {
  const { durationInFrames } = useVideoConfig();
  return (
    <>
      {annotations.map((a, i) => {
        const { from, lenFrames } = frameWindow(a.start, a.end, fps, durationInFrames);
        if (lenFrames <= 0) {
          return null;
        }
        return (
          <Sequence
            key={"annotation-" + i}
            from={from}
            durationInFrames={lenFrames}
            name={"标注框 " + (i + 1)}
          >
            <AnnotationBox a={a} />
          </Sequence>
        );
      })}
    </>
  );
};
