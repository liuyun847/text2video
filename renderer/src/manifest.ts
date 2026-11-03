/**
 * render.json(version 1)清单契约的 TypeScript 端定义 + Remotion 元数据计算。
 *
 * 契约由 text2video 侧产出、本渲染端只消费。字段名冻结,一字不改;
 * 路径一律绝对路径(Windows 反斜杠原样),时间单位秒(float, round 3),
 * durationInFrames = round(duration * fps)。
 *
 * 注意:传进 <TextVideo> 的 manifest 里,媒体路径已被 render.mjs 改写成
 * "public 相对路径"(形如 run/demo/shot.png),组件内一律走 staticFile()。
 * 绝对路径 → public 的复制规则只在 render.mjs 实现一处,见 README「静态资源规则」。
 */

/** 契约版本号,render.mjs 会先卡这一条 */
export const MANIFEST_VERSION = 1;

/** 背景:纯色(RRGGBB,不带 # 前缀)或图片(绝对路径) */
export type BackgroundSpec =
  | { type: "color"; color: string }
  | { type: "image"; path: string };

/** 淡入淡出时长(秒);缺省视为 0,即硬切 */
export type FadeSpec = { in?: number; out?: number };

/** 素材段:已由上游预切到输出画幅,这里只需铺满 + 淡入淡出 */
export type MaterialSpec = {
  type: "image" | "video";
  path: string;
  start: number;
  end: number;
  fade?: FadeSpec;
};

/** 标注框:坐标为 0~1 归一化;color 形如 0xRRGGBB */
export type AnnotationSpec = {
  start: number;
  end: number;
  x: number;
  y: number;
  w: number;
  h: number;
  color: string;
};

/** 时间轴来源:real = 真实 TTS 实测;estimate = 离线估算 */
export type TimingSpec = { mode: "real" | "estimate" };

export type Manifest = {
  version: number;
  fps: number;
  width: number;
  height: number;
  duration: number;
  durationInFrames: number;
  background: BackgroundSpec;
  audio: { path: string };
  subtitle: { path: string };
  materials: MaterialSpec[];
  annotations: AnnotationSpec[];
  timing: TimingSpec;
};

export type TextVideoProps = { manifest: Manifest };

/** 秒 → 帧。全工程唯一的帧化入口,避免各组件各写一套 round。 */
export const toFrame = (sec: number, fps: number): number => Math.round(sec * fps);

/**
 * 颜色归一化:0xRRGGBB / #RRGGBB / RRGGBB 都收,统一出 #rrggbb。
 * @param input 契约里的颜色字符串
 * @returns CSS 可直接用的 #rrggbb
 * @throws 无法识别时抛错(不静默回退成黑色,避免画面悄悄变样)
 */
export const normalizeColor = (input: string): string => {
  const raw = String(input ?? "").trim();
  const m = /^(?:0x|#)?([0-9a-fA-F]{6})$/.exec(raw);
  if (!m) {
    throw new Error('无法识别的颜色 "' + input + '":只接受 0xRRGGBB / #RRGGBB / RRGGBB');
  }
  return "#" + m[1].toLowerCase();
};

/** 校验清单必备字段,给浏览器侧兜底(render.mjs 已先卡一遍,报错更好读) */
export const assertManifest = (manifest: Manifest | undefined): Manifest => {
  if (!manifest || typeof manifest !== "object") {
    throw new Error("缺少 props.manifest:请用 render.mjs 渲染,或在 Studio 里用 --props 传 resolved 清单");
  }
  if (manifest.version !== MANIFEST_VERSION) {
    throw new Error("清单 version=" + manifest.version + " 不受支持,本渲染端只认 version=" + MANIFEST_VERSION);
  }
  const { fps, width, height, duration, durationInFrames } = manifest;
  if (!Number.isInteger(fps) || fps <= 0) throw new Error("fps 必须是正整数,收到 " + fps);
  if (!Number.isInteger(width) || width <= 0) throw new Error("width 必须是正整数,收到 " + width);
  if (!Number.isInteger(height) || height <= 0) throw new Error("height 必须是正整数,收到 " + height);
  if (!Number.isFinite(duration) || duration <= 0) throw new Error("duration 必须是正数(秒),收到 " + duration);
  if (!Number.isInteger(durationInFrames) || durationInFrames <= 0) {
    throw new Error("durationInFrames 必须是正整数,收到 " + durationInFrames);
  }
  return manifest;
};

/**
 * Remotion 的 calculateMetadata:宽/高/fps/时长全部由传入的 manifest 决定。
 *
 * 为什么这么做:Composition 上写死尺寸会让同一个 TextVideo 只能出一种画幅;
 * calculateMetadata 允许在 selectComposition 阶段按 inputProps 现算,render.mjs
 * 只需换清单,不必换 composition。
 *
 * @param props 合并后的 props(defaultProps ← inputProps)
 * @returns fps/width/height/durationInFrames,供 selectComposition 与渲染器使用
 */
export const computeMetadata = ({ props }: { props: TextVideoProps }) => {
  const manifest = assertManifest(props?.manifest);
  const expected = Math.round(manifest.duration * manifest.fps);
  if (Math.abs(expected - manifest.durationInFrames) > 1) {
    // 不抛错:以清单里的 durationInFrames 为准,但把不一致喊出来(上游帧数算错时能看见)
    console.warn(
      "[manifest] durationInFrames=" + manifest.durationInFrames +
        " 与 round(duration*fps)=" + expected + " 不一致,按 " + manifest.durationInFrames + " 渲染",
    );
  }
  if (manifest.width % 2 !== 0 || manifest.height % 2 !== 0) {
    throw new Error("h264 要求宽高为偶数,收到 " + manifest.width + "x" + manifest.height);
  }
  return {
    fps: manifest.fps,
    width: manifest.width,
    height: manifest.height,
    durationInFrames: manifest.durationInFrames,
  };
};

/**
 * Studio 里没有 inputProps 时用的空白清单:纯色底、无素材/无字幕/无音轨。
 * 媒体路径给空串 = 该层不挂载(组件里统一判空),这样 remotion studio 开箱可用。
 */
export const BLANK_MANIFEST: Manifest = {
  version: MANIFEST_VERSION,
  fps: 60,
  width: 1080,
  height: 1080,
  duration: 3,
  durationInFrames: 180,
  background: { type: "color", color: "1B1B2A" },
  audio: { path: "" },
  subtitle: { path: "" },
  materials: [],
  annotations: [],
  timing: { mode: "real" },
};
