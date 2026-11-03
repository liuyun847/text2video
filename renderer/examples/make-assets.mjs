import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

/**
 * 生成 examples/ 下的自测素材(可重复执行,幂等覆盖)。
 *
 * 产出:
 *   examples/assets/demo.subs.webm        透明字幕轨样例(vp9 + yuva420p,1080x1080@60)
 *   examples/assets/alpha-probe.webm      alpha 验证探针素材(vp9 + yuva420p,540x540@60,2 s)
 *   examples/assets/demo.material-video.mp4 视频素材段样例(1080x1080@60,6.7 s)
 *   examples/assets/shot_*.png / demo.audio.mp3  从 text2video/out 只读复制
 *   examples/manifest.json / examples/manifest-image-bg.json  两份清单样例(绝对路径)
 *   public/probe/alpha-probe.webm         探针在 public 下的副本(供 AlphaProbe 用)
 *
 * 依赖:本机 ffmpeg(env FFMPEG_PATH 可覆盖);text2video/out 里已有的 png/mp3。
 * 副作用:只写本项目目录,对 text2video 只读。
 */

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const ASSETS = path.join(ROOT, "examples", "assets");
const PUBLIC_PROBE = path.join(ROOT, "public", "probe");
const T2V_OUT = path.resolve(ROOT, "..", "text2video", "out");
const FFMPEG = process.env.FFMPEG_PATH || "ffmpeg";

const log = (msg) => console.log("[assets] " + msg);

const run = (args, cwd) => {
  log(FFMPEG + " " + args.join(" "));
  const r = spawnSync(FFMPEG, args, { cwd, stdio: ["ignore", "inherit", "inherit"] });
  if (r.error) {
    throw new Error("调用 ffmpeg 失败(可用 env FFMPEG_PATH 指定路径):" + r.error.message);
  }
  if (r.status !== 0) {
    throw new Error("ffmpeg 退出码 " + r.status + ",参数见上一行");
  }
};

/** 字幕 ASS:样式与 text2video 的 subs.ass_header 一致(1080² → fontsize 54 / outline 3 / MarginV 140) */
const demoSubsAss = (width, height, fontSize, marginV, lines) =>
  [
    "[Script Info]",
    "ScriptType: v4.00+",
    "PlayResX: " + width,
    "PlayResY: " + height,
    "WrapStyle: 2",
    "ScaledBorderAndShadow: yes",
    "",
    "[V4+ Styles]",
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
    "Style: Base,Microsoft YaHei," +
      fontSize +
      ",&H00FFFFFF,&H000000FF,&H00000000,&H00101010,0,0,0,0,100,100,0,0,1,3,1,2,40,40," +
      marginV +
      ",1",
    "",
    "[Events]",
    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ...lines,
    "",
  ].join("\r\n");

const ts = (sec) => {
  const cs = Math.round(sec * 100);
  const h = Math.floor(cs / 360000);
  const m = Math.floor((cs % 360000) / 6000);
  const s = Math.floor((cs % 6000) / 100);
  const c = cs % 100;
  const p2 = (n) => String(n).padStart(2, "0");
  return h + ":" + p2(m) + ":" + p2(s) + "." + p2(c);
};

/** 一条 Dialogue:前半亮白加粗(已念)、后半灰常规(未念),复刻 karaoke 观感 */
const karaoke = (start, end, said, todo) =>
  "Dialogue: 0," +
  ts(start) +
  "," +
  ts(end) +
  ",Base,,0,0,0,,{\\c&H00FFFFFF\\b1}" +
  said +
  "{\\c&H009A9A9A\\b0}" +
  todo;

const main = () => {
  fs.mkdirSync(ASSETS, { recursive: true });
  fs.mkdirSync(PUBLIC_PROBE, { recursive: true });

  // ---- 1. 透明字幕轨样例(24.168 s,与样例清单时长一致)----
  const subsAss = demoSubsAss(1080, 1080, 54, 140, [
    karaoke(0.1, 4.2, "示例字幕层", ":由 ASS 烧成透明视频"),
    karaoke(4.2, 8.5, "Remotion 只做", "视频层叠加"),
    karaoke(8.5, 13.0, "透明区应透出", "下方素材"),
    karaoke(13.0, 18.0, "标注框置于", "素材之上、字幕之下"),
    karaoke(18.0, 24.0, "音轨与时长", "由清单决定"),
  ]);
  fs.writeFileSync(path.join(ASSETS, "demo.subs.ass"), subsAss, "utf8");
  run(
    [
      "-y", "-hide_banner", "-loglevel", "error",
      "-f", "lavfi", "-i", "color=c=black:s=1080x1080:r=60:d=24.168",
      // 透明底两件套,缺一不可:
      //   1) colorchannelmixer=aa=0 —— lavfi 的 color=c=black@0.0 实际不透明(实测 alpha 全 255)
      //   2) ass=...:alpha=1 —— ass/subtitles 滤镜默认不写 alpha(默认 false),必须显式打开
      "-vf", "format=rgba,colorchannelmixer=aa=0,ass=demo.subs.ass:alpha=1",
      "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-auto-alt-ref", "0",
      "-b:v", "0", "-crf", "30", "-row-mt", "1", "-cpu-used", "4", "-deadline", "good",
      "demo.subs.webm",
    ],
    ASSETS,
  );

  // ---- 2. alpha 验证探针素材(2 s,540x540,白字黑描边 + 全透明底)----
  const probeAss = demoSubsAss(540, 540, 96, 0, [
    "Dialogue: 0," + ts(0) + "," + ts(2) + ",Base,,0,0,0,,{\\an5\\pos(270,270)\\b1}ALPHA 透明",
  ]);
  fs.writeFileSync(path.join(ASSETS, "alpha-probe.ass"), probeAss, "utf8");
  run(
    [
      "-y", "-hide_banner", "-loglevel", "error",
      "-f", "lavfi", "-i", "color=c=black:s=540x540:r=60:d=2",
      "-vf", "format=rgba,colorchannelmixer=aa=0,ass=alpha-probe.ass:alpha=1",
      "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-auto-alt-ref", "0",
      "-b:v", "0", "-crf", "24", "-row-mt", "1", "-cpu-used", "4", "-deadline", "good",
      "alpha-probe.webm",
    ],
    ASSETS,
  );
  fs.copyFileSync(path.join(ASSETS, "alpha-probe.webm"), path.join(PUBLIC_PROBE, "alpha-probe.webm"));

  // ---- 3. 视频素材段样例(6.7 s 合成画面)----
  run(
    [
      "-y", "-hide_banner", "-loglevel", "error",
      "-f", "lavfi", "-i", "testsrc2=size=1080x1080:rate=60:duration=6.7",
      "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p",
      "demo.material-video.mp4",
    ],
    ASSETS,
  );

  // ---- 4. 从 text2video/out 只读复制图片素材 + 配音 ----
  const copies = [
    [path.join(T2V_OUT, "sample.mp3"), "demo.audio.mp3"],
    [path.join(T2V_OUT, "heat_exchanger_video", "shots", "shot_counter.png"), "shot_counter.png"],
    [path.join(T2V_OUT, "heat_exchanger_video", "shots", "shot_parallel.png"), "shot_parallel.png"],
  ];
  for (const [src, dst] of copies) {
    if (!fs.existsSync(src)) {
      throw new Error("样例素材缺失(只读引用 text2video/out):" + src);
    }
    fs.copyFileSync(src, path.join(ASSETS, dst));
    log("复制 " + src + " → examples/assets/" + dst);
  }

  // ---- 5. 两份清单样例(契约示例;路径为本机绝对路径)----
  const p = (name) => path.join(ASSETS, name);
  const manifest = {
    version: 1,
    fps: 60,
    width: 1080,
    height: 1080,
    duration: 24.168,
    durationInFrames: 1450,
    background: { type: "color", color: "1B1B2A" },
    audio: { path: p("demo.audio.mp3") },
    subtitle: { path: p("demo.subs.webm") },
    materials: [
      { type: "image", path: p("shot_counter.png"), start: 0, end: 7.3, fade: { in: 0.3, out: 0.3 } },
      { type: "video", path: p("demo.material-video.mp4"), start: 7.3, end: 14.0, fade: { in: 0.4, out: 0.4 } },
      { type: "image", path: p("shot_parallel.png"), start: 14.0, end: 24.168, fade: { in: 0.3, out: 0.3 } },
    ],
    // end 故意越过 24.168:用来验证"标注框时间窗被夹到成片末尾"
    annotations: [
      { start: 17.5, end: 24.5, x: 0.1, y: 0.3, w: 0.8, h: 0.55, color: "0xFF3B30" },
    ],
    timing: { mode: "real" },
  };
  fs.writeFileSync(path.join(ROOT, "examples", "manifest.json"), JSON.stringify(manifest, null, 2) + "\n", "utf8");

  // 背景图分支的覆盖样例
  const imageBg = {
    ...manifest,
    background: { type: "image", path: p("shot_parallel.png") },
    subtitle: { path: p("demo.subs.webm") },
    materials: [
      { type: "image", path: p("shot_counter.png"), start: 2.0, end: 12.0, fade: { in: 0.5, out: 0.5 } },
    ],
    annotations: [{ start: 4.0, end: 10.0, x: 0.2, y: 0.2, w: 0.6, h: 0.4, color: "0xFF3B30" }],
  };
  fs.writeFileSync(
    path.join(ROOT, "examples", "manifest-image-bg.json"),
    JSON.stringify(imageBg, null, 2) + "\n",
    "utf8",
  );

  log("完成:examples/assets/ + examples/manifest*.json");
};

main();
