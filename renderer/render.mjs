#!/usr/bin/env node
/**
 * render.mjs —— 按 render.json(version 1)清单渲染成片 / 出静帧。
 *
 * 用法:
 *   node render.mjs --manifest examples/manifest.json --out out/demo.mp4
 *   node render.mjs --manifest examples/manifest.json --stills 0,120,300
 *
 * 可选参数:
 *   --manifest <路径>      必填,render.json 清单(相对项目根或绝对路径)
 *   --out <路径>          输出 mp4,默认 out/demo.mp4
 *   --stills <帧列表>     只出 PNG,如 0,120,300;输出 out/still-<帧>.png
 *   --stills-dir <路径>   静帧输出目录,默认 out/
 *   --concurrency <n>     并行渲染数,默认 4
 *   --crf <n>             x264 CRF,默认 18
 *   --browser <路径>      浏览器可执行文件,默认本机 Edge;env REMOTION_BROWSER_EXECUTABLE 可覆盖
 *   --id <id>             Composition id,默认 TextVideo
 *
 * 做的事(按顺序):
 *   1) TEMP/TMP 指到项目内 work-tmp(Remotion 自带 ffmpeg 写系统临时目录会被拒,必须在 require 之前)
 *   2) 读清单 → 校验契约字段(路径是否绝对、文件是否存在、帧数是否自洽)
 *   3) 把清单引用的外部文件复制进 public/run/<清单名>/,并把路径改写成 public 相对路径
 *   4) 写出 out/<成片名>.props.json(渲染真正用的 props,可给 remotion studio --props 复现)
 *   5) bundle → selectComposition(传 inputProps,由 calculateMetadata 现算宽高/fps/时长)→ 渲染
 */
import { spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.dirname(fileURLToPath(import.meta.url));
const PUBLIC_DIR = path.join(ROOT, "public");
const WORK_TMP = path.join(ROOT, "work-tmp");
const OUT_DIR = path.join(ROOT, "out");

// ⚠ 本机坑:Remotion 自带的 ffmpeg 写系统 %TEMP% 会被拒(Permission denied),
// makeTempDir 参数替换不了那一处。必须在 require("@remotion/*") 之前改 TEMP/TMP。
fs.mkdirSync(WORK_TMP, { recursive: true });
process.env.TEMP = WORK_TMP;
process.env.TMP = WORK_TMP;

const require = createRequire(import.meta.url);
const { bundle } = require("@remotion/bundler");
const { renderMedia, renderStill, selectComposition } = require("@remotion/renderer");

const DEFAULT_BROWSER = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const COLOR_RE = /^(?:0x|#)?[0-9a-fA-F]{6}$/;

const log = (msg) => console.log("[render] " + msg);
const warn = (msg) => console.warn("[render] 警告:" + msg);

const die = (msg) => {
  console.error("[render] 错误:" + msg);
  process.exit(1);
};

const parseArgs = (argv) => {
  const out = {};
  for (let i = 0; i < argv.length; i += 1) {
    const token = argv[i];
    if (!token.startsWith("--")) die("无法识别的参数:" + token + "(用 --help 看用法)");
    const eq = token.indexOf("=");
    const key = (eq === -1 ? token.slice(2) : token.slice(2, eq)).trim();
    if (key === "help") { out.help = true; continue; }
    let value;
    if (eq !== -1) {
      value = token.slice(eq + 1);
    } else {
      value = argv[i + 1];
      if (value === undefined || value.startsWith("--")) die("--" + key + " 缺少参数值");
      i += 1;
    }
    out[key] = value;
  }
  return out;
};

/**
 * 契约校验:能提前发现的问题都在 Node 侧喊出来(比浏览器里的堆栈好读)。
 * @returns {errs: string[], warns: string[]} errs 致命;warns 只提醒,渲染继续
 */
const validateManifest = (m, sourcePath) => {
  const errs = [];
  const warns = [];
  if (!m || typeof m !== "object") {
    errs.push("清单不是 JSON 对象:" + sourcePath);
    return { errs, warns };
  }
  if (m.version !== 1) errs.push("version 必须是 1,收到 " + JSON.stringify(m.version));
  if (!Number.isInteger(m.fps) || m.fps <= 0) errs.push("fps 必须是正整数,收到 " + JSON.stringify(m.fps));
  for (const key of ["width", "height"]) {
    const v = m[key];
    if (!Number.isInteger(v) || v <= 0) errs.push(key + " 必须是正整数,收到 " + JSON.stringify(v));
    else if (v % 2 !== 0) errs.push(key + " 必须是偶数(h264 要求),收到 " + v);
  }
  if (typeof m.duration !== "number" || !(m.duration > 0)) errs.push("duration 必须是正数(秒),收到 " + JSON.stringify(m.duration));
  if (!Number.isInteger(m.durationInFrames) || m.durationInFrames <= 0) {
    errs.push("durationInFrames 必须是正整数,收到 " + JSON.stringify(m.durationInFrames));
  } else if (Number.isFinite(m.duration) && Number.isInteger(m.fps)) {
    const expected = Math.round(m.duration * m.fps);
    if (Math.abs(expected - m.durationInFrames) > 1) {
      warns.push("durationInFrames=" + m.durationInFrames + " 与 round(duration*fps)=" + expected + " 不一致,以 durationInFrames 为准");
    }
  }
  const bg = m.background;
  if (!bg || typeof bg !== "object") errs.push("缺少 background");
  else if (bg.type === "color") {
    if (!COLOR_RE.test(String(bg.color || ""))) errs.push("background.color 不合法(要 0xRRGGBB / RRGGBB):" + JSON.stringify(bg.color));
  } else if (bg.type === "image") {
    if (!bg.path) errs.push("background.type=image 但缺少 path");
  } else errs.push("background.type 只能是 color 或 image,收到 " + JSON.stringify(bg.type));
  for (const key of ["audio", "subtitle"]) {
    const spec = m[key];
    if (!spec || typeof spec !== "object") errs.push("缺少 " + key + " 对象(即使没有也要给 {path: \"\"})");
  }
  if (!Array.isArray(m.materials)) errs.push("materials 必须是数组");
  else {
    m.materials.forEach((mat, i) => {
      const at = "materials[" + i + "]";
      if (mat.type !== "image" && mat.type !== "video") errs.push(at + ".type 只能是 image 或 video,收到 " + JSON.stringify(mat.type));
      if (!mat.path) errs.push(at + " 缺少 path");
      if (typeof mat.start !== "number" || typeof mat.end !== "number") errs.push(at + " 的 start/end 必须是数字(秒)");
      else {
        if (!(mat.end > mat.start)) errs.push(at + " 的 end(" + mat.end + ") 必须大于 start(" + mat.start + ")");
        if (mat.start < 0) warns.push(at + ".start < 0,会夹到 0");
        if (typeof m.duration === "number" && mat.end > m.duration + 1e-6) warns.push(at + ".end(" + mat.end + ") 超过 duration(" + m.duration + "),超出部分不会出现");
      }
      if (mat.fade) {
        for (const k of ["in", "out"]) {
          const v = mat.fade[k];
          if (v !== undefined && (typeof v !== "number" || v < 0)) errs.push(at + ".fade." + k + " 必须是非负数(秒)");
        }
      }
    });
  }
  if (m.annotations !== undefined && !Array.isArray(m.annotations)) errs.push("annotations 必须是数组");
  else {
    (m.annotations || []).forEach((a, i) => {
      const at = "annotations[" + i + "]";
      for (const k of ["start", "end", "x", "y", "w", "h"]) {
        if (typeof a[k] !== "number") errs.push(at + "." + k + " 必须是数字");
      }
      if (typeof a.end === "number" && typeof a.start === "number" && !(a.end > a.start)) errs.push(at + ".end 必须大于 start");
      for (const k of ["x", "y"]) {
        if (typeof a[k] === "number" && (a[k] < 0 || a[k] > 1)) warns.push(at + "." + k + "=" + a[k] + " 超出 0~1");
      }
      if (!COLOR_RE.test(String(a.color || ""))) errs.push(at + ".color 不合法(要 0xRRGGBB):" + JSON.stringify(a.color));
    });
  }
  if (m.timing && m.timing.mode && m.timing.mode !== "real" && m.timing.mode !== "estimate") {
    warns.push("timing.mode 期望 real / estimate,收到 " + JSON.stringify(m.timing.mode));
  }
  return { errs, warns };
};

/** 清单路径 → 本次渲染的沙箱名(public/run/<runId> 与 work-tmp/bundle-<runId>) */
const runIdOf = (manifestPath) =>
  (path.basename(manifestPath, path.extname(manifestPath)) || "run").replace(/[^\w.\-]+/g, "_");

/**
 * 把清单里所有外部绝对路径文件复制到 public/run/<清单名>/,并改写为 public 相对路径。
 *
 * 为什么要复制:staticFile() 只认 package.json 同级的 public/,且明确拒绝绝对路径与 ".."。
 * 为什么不直接放 public 根:不同清单常常同名文件,按清单名分目录 = 一次渲染一个干净沙箱。
 * 每次渲染前只清本清单自己的 public/run/<runId>/,不动别人的 —— 这样两份不同清单可以同时渲染
 * (整棵清空 run/ 会让并行渲染互相删素材)。
 */
const planAssets = (manifest, runId) => {
  const runDirRel = "run/" + runId;
  const runDirAbs = path.join(PUBLIC_DIR, "run", runId);
  fs.rmSync(runDirAbs, { recursive: true, force: true });
  fs.mkdirSync(runDirAbs, { recursive: true });

  const absToRel = new Map();
  const usedNames = new Set();

  const copyOne = (absPath, hint) => {
    if (!absPath) return "";
    if (!path.isAbsolute(absPath)) die(hint + " 必须是绝对路径(契约要求),收到 " + JSON.stringify(absPath));
    const abs = path.resolve(absPath);
    const cached = absToRel.get(abs);
    if (cached) return cached;
    let stat;
    try {
      stat = fs.statSync(abs);
    } catch {
      die(hint + " 指向的文件不存在:" + abs);
    }
    if (!stat.isFile()) die(hint + " 不是文件:" + abs);
    const base = path.basename(abs);
    const ext = path.extname(base);
    const stem = base.slice(0, base.length - ext.length);
    let name = base;
    let n = 2;
    while (usedNames.has(name.toLowerCase())) {
      name = stem + "-" + n + ext;
      n += 1;
    }
    usedNames.add(name.toLowerCase());
    fs.copyFileSync(abs, path.join(runDirAbs, name));
    const rel = runDirRel + "/" + name;
    absToRel.set(abs, rel);
    log("复制 " + abs + " → public/" + rel);
    return rel;
  };

  const resolved = {
    ...manifest,
    background:
      manifest.background && manifest.background.type === "image"
        ? { type: "image", path: copyOne(manifest.background.path, "background.path") }
        : { type: "color", color: manifest.background ? manifest.background.color : "000000" },
    audio: { path: copyOne(manifest.audio && manifest.audio.path, "audio.path") },
    subtitle: { path: copyOne(manifest.subtitle && manifest.subtitle.path, "subtitle.path") },
    materials: (manifest.materials || []).map((mat, i) => ({
      ...mat,
      path: copyOne(mat.path, "materials[" + i + "].path"),
    })),
    annotations: (manifest.annotations || []).map((a) => ({ ...a })),
  };
  return { resolved, runDirRel };
};

/** 解析浏览器:--browser > env REMOTION_BROWSER_EXECUTABLE > 本机 Edge > 交给 Remotion 自带 */
const resolveBrowser = (arg) => {
  const cand = arg || process.env.REMOTION_BROWSER_EXECUTABLE || DEFAULT_BROWSER;
  if (cand && fs.existsSync(cand)) return cand;
  warn("找不到浏览器可执行文件(" + cand + "),回退 Remotion 自带 Chrome Headless Shell(首次运行会自动下载)");
  return undefined;
};

/** 尽力用 ffprobe 复核成片参数,让"时长/分辨率/帧率对不对"当场可见 */
const probeOutput = (file) => {
  const ffprobe = process.env.FFPROBE_PATH || "ffprobe";
  const r = spawnSync(
    ffprobe,
    [
      "-v", "error",
      "-select_streams", "v:0",
      "-show_entries", "stream=width,height,r_frame_rate,nb_frames,duration",
      "-show_entries", "format=duration,size",
      "-of", "json",
      file,
    ],
    { encoding: "utf8" },
  );
  if (r.error || r.status !== 0 || !r.stdout) return null;
  try {
    const parsed = JSON.parse(r.stdout);
    const stream = (parsed.streams || [])[0] || {};
    const format = parsed.format || {};
    return {
      width: stream.width,
      height: stream.height,
      fps: stream.r_frame_rate,
      frames: stream.nb_frames,
      duration: format.duration,
      size: format.size,
    };
  } catch {
    return null;
  }
};

/** ffprobe 单次探测:返回 {width,height,fps,duration};任何失败返回 null(不阻塞渲染) */
const probeMedia = (file) => {
  const ffprobe = process.env.FFPROBE_PATH || "ffprobe";
  const r = spawnSync(
    ffprobe,
    ["-v", "error", "-select_streams", "v:0",
     "-show_entries", "stream=width,height,r_frame_rate,duration",
     "-of", "json", file],
    { encoding: "utf8" },
  );
  if (r.error || r.status !== 0 || !r.stdout) return null;
  try {
    const s = (JSON.parse(r.stdout).streams || [])[0];
    if (!s || !s.width || !s.height) return null;
    const [num, den] = String(s.r_frame_rate || "0/1").split("/").map(Number);
    return {
      width: s.width,
      height: s.height,
      fps: den ? num / den : 0,
      duration: Number(s.duration) || 0,
    };
  } catch {
    return null;
  }
};

/**
 * 画幅 / 帧率一致性预检:字幕轨与视频素材必须与清单的 width/height/fps 一致。
 *
 * 为什么不 fail:上游 produce 本应保证一致,这里的用途是在"有人手工拼清单"时把
 * 接下来会发生的画面事故提前说清楚 —— 字幕轨和清单画幅不一致会被 SubtitleTrack
 * 按清单尺寸拉伸(整幅叠的代价),素材帧率不一致会让 OffthreadVideo 重复/丢帧。
 * ffprobe 不可用时静默跳过(探测是辅助,不是渲染前提)。
 *
 * @param manifest 原始清单(用绝对路径探测,不依赖 public 副本)
 * @returns 警告文案数组
 */
const checkGeometry = (manifest) => {
  const warns = [];
  const sub = manifest.subtitle && manifest.subtitle.path ? probeMedia(manifest.subtitle.path) : null;
  if (sub) {
    const s = sub;
    if (s.width !== manifest.width || s.height !== manifest.height) {
      warns.push(
        "subtitle 画幅 " + s.width + "x" + s.height + " 与清单 " + manifest.width + "x" + manifest.height +
          " 不一致:字幕轨会被拉伸到清单画幅(请重跑 text2video produce)",
      );
    }
    if (Math.abs(s.fps - manifest.fps) > 0.01) {
      warns.push(
        "subtitle 帧率 " + s.fps + " 与清单 fps " + manifest.fps + " 不一致:逐字点亮会整体错位",
      );
    }
  }
  (manifest.materials || []).forEach((mat, i) => {
    if (mat.type !== "video" || !mat.path) return;
    const info = probeMedia(mat.path);
    if (!info) return;
    if (info.width !== manifest.width || info.height !== manifest.height) {
      warns.push(
        "materials[" + i + "] 画幅 " + info.width + "x" + info.height + " 与清单 " +
          manifest.width + "x" + manifest.height + " 不一致:会用 objectFit:cover 二次裁切(上游应预切好)",
      );
    }
    if (Math.abs(info.fps - manifest.fps) > 0.01) {
      warns.push(
        "materials[" + i + "] 帧率 " + info.fps + " 与清单 fps " + manifest.fps + " 不一致:会出现重复帧或丢帧",
      );
    }
  });
  return warns;
};

const main = async () => {
  const args = parseArgs(process.argv.slice(2));
  if (args.help) {
    console.log("用法: node render.mjs --manifest <清单> [--out out/demo.mp4] [--stills 0,120,300]");
    return;
  }
  if (!args.manifest) die("缺少 --manifest(用 --help 看用法)");
  const manifestPath = path.resolve(ROOT, args.manifest);
  if (!fs.existsSync(manifestPath)) die("清单不存在:" + manifestPath);

  const raw = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
  const { errs, warns } = validateManifest(raw, manifestPath);
  for (const w of warns) warn(w);
  if (errs.length > 0) {
    for (const e of errs) console.error("[render] 校验失败:" + e);
    die("清单未通过校验(" + errs.length + " 项)");
  }
  log("清单 " + manifestPath);
  for (const w of checkGeometry(raw)) warn(w);

  fs.mkdirSync(PUBLIC_DIR, { recursive: true });
  fs.mkdirSync(OUT_DIR, { recursive: true });
  const runId = runIdOf(manifestPath);
  const { resolved } = planAssets(raw, runId);

  const outPath = path.resolve(ROOT, args.out || "out/demo.mp4");
  const outStem = path.basename(outPath, path.extname(outPath));
  // props 文件名:出片模式跟成片名走;静帧模式没有成片,跟清单名走(否则多份清单会写同一个文件互相覆盖)
  const propsStem = args.stills ? path.basename(manifestPath, path.extname(manifestPath)) : outStem;
  const propsPath = path.join(OUT_DIR, propsStem + ".props.json");
  fs.writeFileSync(propsPath, JSON.stringify({ manifest: resolved }, null, 2) + "\n", "utf8");
  log("渲染用 props 已写出:" + propsPath);

  const browserExecutable = resolveBrowser(args.browser);
  const concurrency = Number(args.concurrency || 4);
  const crf = Number(args.crf || 18);
  const compositionId = args.id || "TextVideo";
  const inputProps = { manifest: resolved };

  // bundle 目录也按清单分:并行渲染两份清单时不会互相覆盖
  const bundleDir = path.join(WORK_TMP, "bundle-" + runId);
  fs.rmSync(bundleDir, { recursive: true, force: true });
  log("bundle 中(work-tmp/" + path.basename(bundleDir) + ")…");
  const serveUrl = await bundle({
    entryPoint: path.join(ROOT, "src", "index.tsx"),
    publicDir: PUBLIC_DIR,
    outDir: bundleDir,
  });

  const composition = await selectComposition({
    serveUrl,
    id: compositionId,
    inputProps,
    browserExecutable,
  });
  log(
    "composition " + composition.id + " " + composition.width + "x" + composition.height +
      " @" + composition.fps + "fps " + composition.durationInFrames + " 帧 = " +
      (composition.durationInFrames / composition.fps).toFixed(3) + " s",
  );

  if (args.stills) {
    const frames = String(args.stills)
      .split(",")
      .map((s) => Number(s.trim()))
      .filter((n) => Number.isInteger(n) && n >= 0);
    if (frames.length === 0) die("--stills 需要至少一个非负整数帧号");
    const dir = path.resolve(ROOT, args["stills-dir"] || "out");
    fs.mkdirSync(dir, { recursive: true });
    for (const frame of frames) {
      if (frame >= composition.durationInFrames) {
        warn("帧 " + frame + " 超出总帧数 " + composition.durationInFrames + ",仍会渲染(画面停在该帧)");
      }
      const output = path.join(dir, "still-" + frame + ".png");
      await renderStill({
        composition,
        serveUrl,
        inputProps,
        frame,
        output,
        imageFormat: "png",
        browserExecutable,
      });
      log("静帧 " + frame + " → " + output);
    }
    return;
  }

  fs.mkdirSync(path.dirname(outPath), { recursive: true });
  let lastBucket = -1;
  await renderMedia({
    composition,
    serveUrl,
    inputProps,
    codec: "h264",
    audioCodec: "aac",
    crf,
    outputLocation: outPath,
    browserExecutable,
    concurrency,
    onProgress: ({ renderedFrames }) => {
      const step = Math.floor(composition.durationInFrames / 10) || 1;
      const bucket = Math.floor(renderedFrames / step);
      if (bucket !== lastBucket) {
        lastBucket = bucket;
        log("渲染 " + renderedFrames + "/" + composition.durationInFrames + " 帧");
      }
    },
  });

  const sizeMb = (fs.statSync(outPath).size / 1048576).toFixed(2);
  log("成片 " + outPath + "(" + sizeMb + " MB)");
  const probe = probeOutput(outPath);
  if (probe) {
    log(
      "ffprobe: " + probe.width + "x" + probe.height + " fps=" + probe.fps + " frames=" + probe.frames +
        " 时长=" + probe.duration + " s",
    );
  } else {
    warn("ffprobe 不可用,跳过成片参数复核(可设 env FFPROBE_PATH)");
  }
};

main().catch((err) => {
  console.error("[render] 渲染失败:" + (err && err.stack ? err.stack : err));
  process.exit(1);
});
