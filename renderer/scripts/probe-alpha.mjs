#!/usr/bin/env node
/**
 * scripts/probe-alpha.mjs —— 透明字幕视频(alpha webm)能不能被 Remotion 当视频层正确叠加。
 *
 * 做法:渲染 AlphaProbe 这个探针 Composition 的几帧 PNG,然后把像素读回来做量化统计:
 *   左半屏 = <OffthreadVideo transparent /> 叠在亮青色背景上
 *   右半屏 = 同一段 webm 不传 transparent(对照组)
 * 判定:左半屏应以青色为主、几乎无纯黑像素;一旦出现大块纯黑 = alpha 丢了(黑块)。
 *
 * 用法:node scripts/probe-alpha.mjs
 * 产物:out/probe/alpha-<帧>.png(证据图)+ 控制台统计表
 */
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const WORK_TMP = path.join(ROOT, "work-tmp");
const OUT_DIR = path.join(ROOT, "out", "probe");
const PUBLIC_DIR = path.join(ROOT, "public");
const FFMPEG = process.env.FFMPEG_PATH || "ffmpeg";

// 同 render.mjs:TEMP/TMP 必须在 require @remotion/* 之前指到项目内
fs.mkdirSync(WORK_TMP, { recursive: true });
process.env.TEMP = WORK_TMP;
process.env.TMP = WORK_TMP;

const require = createRequire(import.meta.url);
const { bundle } = require("@remotion/bundler");
const { renderStill, selectComposition } = require("@remotion/renderer");

const DEFAULT_BROWSER = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const FRAMES = [30, 60, 90];
const TILE = 540;
const FRAME_W = 1080;
const FRAME_H = 1080;
const TILE_TOP = (FRAME_H - TILE) / 2;

const log = (msg) => console.log("[probe] " + msg);

/** 设定:左半屏应透出青色背景,右半屏是对照组 */
const REGIONS = [
  { name: "左半屏 transparent=true ", x: 0, y: TILE_TOP },
  { name: "右半屏 transparent=false", x: TILE, y: TILE_TOP },
];

/** 把 PNG 的一块区域解成 RGBA 原始像素 */
const decodeRegion = (file, x, y, w, h) => {
  const r = spawnSync(
    FFMPEG,
    ["-y", "-loglevel", "error", "-i", file, "-vf", "crop=" + w + ":" + h + ":" + x + ":" + y, "-f", "rawvideo", "-pix_fmt", "rgba", "-"],
    { maxBuffer: 512 * 1024 * 1024 },
  );
  if (r.error || r.status !== 0) {
    throw new Error("ffmpeg 解码区域失败:" + (r.error ? r.error.message : r.stderr.toString()));
  }
  return r.stdout;
};

/** 统计一块区域的像素构成 */
const classify = (buf) => {
  const total = buf.length / 4;
  let cyan = 0; // 背景 #00E0FF
  let yellow = 0; // 背景 #FFD400
  let white = 0; // 字幕白字
  let black = 0; // 丢 alpha 后的黑块
  let other = 0;
  for (let i = 0; i < buf.length; i += 4) {
    const r = buf[i];
    const g = buf[i + 1];
    const b = buf[i + 2];
    if (r < 60 && g > 180 && b > 200) cyan += 1;
    else if (r > 230 && g > 180 && b < 80) yellow += 1;
    else if (r > 200 && g > 200 && b > 200) white += 1;
    else if (r < 24 && g < 24 && b < 24) black += 1;
    else other += 1;
  }
  const pct = (n) => ((n / total) * 100).toFixed(2) + "%";
  return { total, cyan: pct(cyan), yellow: pct(yellow), white: pct(white), black: pct(black), other: pct(other), blackCount: black };
};

const main = async () => {
  const probeVideo = path.join(PUBLIC_DIR, "probe", "alpha-probe.webm");
  if (!fs.existsSync(probeVideo)) {
    throw new Error("缺少探针素材 " + probeVideo + ":先跑 node examples/make-assets.mjs");
  }
  fs.mkdirSync(OUT_DIR, { recursive: true });

  const browserExecutable =
    process.env.REMOTION_BROWSER_EXECUTABLE && fs.existsSync(process.env.REMOTION_BROWSER_EXECUTABLE)
      ? process.env.REMOTION_BROWSER_EXECUTABLE
      : fs.existsSync(DEFAULT_BROWSER)
        ? DEFAULT_BROWSER
        : undefined;

  const bundleDir = path.join(WORK_TMP, "bundle");
  fs.rmSync(bundleDir, { recursive: true, force: true });
  const serveUrl = await bundle({
    entryPoint: path.join(ROOT, "src", "index.tsx"),
    publicDir: PUBLIC_DIR,
    outDir: bundleDir,
  });
  const composition = await selectComposition({ serveUrl, id: "AlphaProbe", browserExecutable });
  log("composition " + composition.id + " " + composition.width + "x" + composition.height + " @" + composition.fps + "fps " + composition.durationInFrames + " 帧");

  const summary = [];
  for (const frame of FRAMES) {
    const output = path.join(OUT_DIR, "alpha-" + frame + ".png");
    await renderStill({ composition, serveUrl, frame, output, imageFormat: "png", browserExecutable });
    log("静帧 " + frame + " → " + output);
    const row = { frame, output, regions: [] };
    for (const region of REGIONS) {
      const stats = classify(decodeRegion(output, region.x, region.y, TILE, TILE));
      row.regions.push({ name: region.name, stats });
    }
    summary.push(row);
  }

  console.log("");
  console.log("帧   区域                    青色    黄色    白字    纯黑    其他");
  for (const row of summary) {
    for (const r of row.regions) {
      const s = r.stats;
      console.log(
        String(row.frame).padEnd(5) +
          r.name.padEnd(24) +
          s.cyan.padStart(7) + " " + s.yellow.padStart(7) + " " + s.white.padStart(7) + " " +
          s.black.padStart(7) + " " + s.other.padStart(7),
      );
    }
  }
  console.log("");
  const left = summary[summary.length - 1].regions[0].stats;
  const right = summary[summary.length - 1].regions[1].stats;
  // 判据:透明侧应以背景色为主、纯黑占比很小(留下的是字幕自带的黑描边,不是黑块);
  //       对照组不传 transparent 时应出现大片纯黑。
  const leftBlackShare = left.blackCount / (TILE * TILE);
  const rightBlackShare = right.blackCount / (TILE * TILE);
  const alphaOk = parseFloat(left.cyan) > 80 && leftBlackShare < 0.05 && rightBlackShare > 0.5;
  log(
    "结论:transparent=true 一侧背景透出 " + left.cyan + "、纯黑 " + (leftBlackShare * 100).toFixed(2) +
      "%(字幕黑描边);对照 transparent=false 一侧纯黑 " + (rightBlackShare * 100).toFixed(2) + "%",
  );
  log(alphaOk ? "PASS:VP9 alpha 被正确保留,透明区透出下层画面,无黑块" : "FAIL:alpha 未按预期保留,详见上面统计表");
  if (!alphaOk) process.exitCode = 2;
};

main().catch((err) => {
  console.error("[probe] 失败:" + (err && err.stack ? err.stack : err));
  process.exit(1);
});
