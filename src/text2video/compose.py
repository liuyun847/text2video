"""合成模块:定位 ffmpeg/ffprobe,生成并执行"背景 + 字幕 + 语音"的合成命令。

背景默认为纯色(可换背景图),字幕用过滤器的 subtitles=xxx.ass 渲染(libass),
辅助字体复制到工作目录后通过 fontsdir=. 指定,避免 Windows 路径冒号转义问题。
"""

from __future__ import annotations

import glob
import logging
import os
import shutil
import subprocess
from collections.abc import Sequence

LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 定位可执行文件
# ---------------------------------------------------------------------------

def _win_get_packages_ffmpeg() -> str | None:
    """扫描 winget 安装目录(如 Gyan.FFmpeg)下的 ffmpeg.exe。"""
    base = os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Packages"
    )
    if not os.path.isdir(base):
        return None
    for pattern in ("Gyan*", "*FFmpeg*", "*ffmpeg*"):
        for d in glob.glob(os.path.join(base, pattern)):
            for root, _dirs, files in os.walk(d):
                if "ffmpeg.exe" in files:
                    return os.path.join(root, "ffmpeg.exe")
    return None


def find_ffmpeg() -> str:
    """返回可用 ffmpeg 绝对路径。优先级:环境变量 > PATH > 常见安装目录。"""
    env = os.environ.get("FFMPEG_BIN")
    if env and os.path.isfile(env):
        return env
    for name in ("ffmpeg", "ffmpeg.exe"):
        found = shutil.which(name)
        if found:
            return found
    cand = _win_get_packages_ffmpeg()
    if cand:
        return cand
    # 手动解压到本地 Programs 的情况(如 %LOCALAPPDATA%\Programs\FFmpeg\*\bin)
    local = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "FFmpeg")
    if os.path.isdir(local):
        hits = glob.glob(os.path.join(local, "**", "bin", "ffmpeg.exe"), recursive=True)
        if hits:
            return max(hits)
    raise RuntimeError(
        "未找到 ffmpeg:请安装(如 winget install Gyan.FFmpeg)或用 FFMPEG_BIN 指定路径"
    )


def find_ffprobe(ffmpeg: str) -> str:
    """由 ffmpeg 路径推导 ffprobe(同目录),或走 PATH/环境变量。"""
    d = os.path.dirname(ffmpeg)
    probe = os.path.join(d, "ffprobe.exe" if os.name == "nt" else "ffprobe")
    if os.path.isfile(probe):
        return probe
    env = os.environ.get("FFPROBE_BIN")
    if env and os.path.isfile(env):
        return env
    found = shutil.which("ffprobe")
    if found:
        return found
    raise RuntimeError("未找到 ffprobe,无法读取音频时长")


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def probe_duration(ffprobe: str, audio: str) -> float:
    """用 ffprobe 读取音频时长(秒)。"""
    cmd = [
        ffprobe, "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        audio,
    ]
    out = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=False,
    )
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe 读取时长失败: {out.stderr.strip()}")
    try:
        return float(out.stdout.strip())
    except ValueError:
        raise RuntimeError(f"无法解析音频时长: {out.stdout.strip()!r}")


def probe_video_duration(ffprobe: str, path: str) -> float:
    """探测视频素材时长(秒);无视频流 / 不可解码时抛 RuntimeError。

    用视频流时长而非 format 时长:容器 format 时长可能含尾部 padding,
    素材裁剪(in/out)边界校验以视频流实际时长为准更可靠。
    """
    cmd = [
        ffprobe, "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path,
    ]
    out = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=False,
    )
    if out.returncode != 0 or not out.stdout.strip():
        raise RuntimeError(f"无视频流或不可解码: {out.stderr.strip() or '无输出'}")
    try:
        return float(out.stdout.strip().splitlines()[-1].strip())
    except ValueError:
        raise RuntimeError(f"无法解析视频时长: {out.stdout.strip()!r}")


def probe_dimensions(ffprobe: str, path: str) -> tuple[int, int]:
    """探测媒体主视频流(视频素材或图片)像素尺寸 (width, height);失败抛 RuntimeError。

    用于画面裁剪:素材带 crop 时按原始分辨率计算裁剪窗口像素。
    """
    cmd = [
        ffprobe, "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "default=noprint_wrappers=1",
        path,
    ]
    out = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=False,
    )
    if out.returncode != 0 or not out.stdout.strip():
        raise RuntimeError(f"无视频流或不可解码: {out.stderr.strip() or '无输出'}")
    dims: dict[str, int] = {}
    for line in out.stdout.splitlines():
        key, _, val = line.partition("=")
        if key in ("width", "height"):
            try:
                dims[key] = int(val.strip())
            except ValueError:
                raise RuntimeError(f"无法解析素材尺寸: {line!r}")
    if dims.get("width") and dims.get("height"):
        return dims["width"], dims["height"]
    raise RuntimeError(f"未读到完整尺寸: {out.stdout.strip()!r}")


def pick_system_font() -> str | None:
    """挑一个含中文的字体文件,复制到工作目录供 libass 使用。"""
    candidates = (
        r"C:\Windows\Fonts\msyh.ttc",      # 微软雅黑
        r"C:\Windows\Fonts\msyh.ttf",
        r"C:\Windows\Fonts\simhei.ttf",    # 黑体
        r"C:\Windows\Fonts\msjh.ttc",      # 微软正黑(繁)
        r"C:\Windows\Fonts\Deng.ttf",      # 等线
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    )
    for f in candidates:
        if os.path.isfile(f):
            return f
    return None


# ---------------------------------------------------------------------------
# 命令构建 / 执行
# ---------------------------------------------------------------------------

def build_video_cmd(
    ffmpeg: str,
    *,
    audio: str,          # 工作目录内的相对名
    ass: str,            # 工作目录内的相对名
    out: str,
    width: int,
    height: int,
    fps: int,
    bg_color: str,       # 09BgHex,如 "1B1B2A"
    bg_image: str | None,
    duration: float,
    materials: Sequence[dict] = (),
    transition: float = 0.0,
    crf: int = 23,
    preset: str = "medium",
    audio_bitrate: str = "192k",
    annotations: Sequence[dict] = (),
) -> list[str]:
    """组装 ffmpeg 命令(在输出目录内执行,故音频/字幕用相对名)。

    materials:素材段列表,每项须含 {"path", "type": video|image, "start", "end",
    视频另含 "in"/"out"(素材内裁剪,秒)}。素材按段落全屏覆盖(overlay 铺满),
    未覆盖区间保持 bg_color/bg_image 背景;素材自带音频一律忽略。
    素材项可选带 "crop": {"w","h","x","y"}(像素裁剪窗口,由渲染端按素材分辨率
    与画幅比例、crop(x/y)算出),此时滤镜为 crop=cw:ch:cx:cy,scale 不变形放大;
    缺省则沿用等比铺满+中心裁剪。
    annotations:画面标注框列表,每项 {"start","end"(秒), "x","y","w","h"(画幅比例),
    "color"(0xRRGGBB,默认红色)};以 6px 矩形框(drawbox)叠加在**字幕之下、素材之上**
    的层级,只在该时段可见。
    transition:素材段淡入/淡出时长(秒,每段内部钳制到段长一半;0=硬切)。
    """
    args = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    fc: list[str] = []
    # ---- 背景输入(流 0)----
    if bg_image:
        args += ["-loop", "1", "-framerate", str(fps), "-i", bg_image]
        fc.append(
            f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}[bg]"
        )
        prev = "[bg]"
    else:
        args += [
            "-f", "lavfi",
            "-i", f"color=c={bg_color}:s={width}x{height}:r={fps}",
        ]
        prev = "[0:v]"
    # ---- 素材输入(流 1..N;图片需要 -loop 撑住 overlay 窗口)----
    for m in materials:
        if m["type"] == "image":
            args += ["-loop", "1", "-framerate", str(fps), "-i", m["path"]]
        else:
            args += ["-i", m["path"]]
    args += ["-i", audio]   # 流 N+1:配音(可能已含 BGM 预混)
    # ---- filter_complex:素材裁剪/铺满 → 按时间窗口 overlay 到主画面 ----
    for i, m in enumerate(materials, start=1):
        seg_len = max(0.0, m["end"] - m["start"])
        fade = min(transition, seg_len / 2) if transition > 0 else 0.0
        chain = f"[{i}:v]"
        video_clip = m["type"] == "video"
        if video_clip:
            chain += f"trim=start={m['in']:.3f}:end={m['out']:.3f},setpts=PTS-STARTPTS,"
        if fade > 0:
            # alpha 淡入/淡出:视频段在 trim 后为段内时间;图片段为全局时间
            fin_st = 0.0 if video_clip else m["start"]
            fout_st = (seg_len - fade) if video_clip else (m["end"] - fade)
            chain += (
                f"format=rgba,fade=t=in:st={fin_st:.3f}:d={fade:.3f}:alpha=1,"
                f"fade=t=out:st={max(fout_st, fin_st):.3f}:d={fade:.3f}:alpha=1,"
            )
        crop_px = m.get("crop")
        if crop_px:
            # 画面裁剪:窗口比例已=输出画幅,直接放大铺满(不变形、不二次裁切)
            chain += (
                f"crop={crop_px['w']}:{crop_px['h']}:{crop_px['x']}:{crop_px['y']},"
                f"scale={width}:{height}[m{i}]"
            )
        else:
            chain += (
                f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height}[m{i}]"
            )
        fc.append(chain)
        label = f"[v{i}]"
        fc.append(
            f"{prev}[m{i}]overlay=0:0:enable='between(t,{m['start']:.3f},{m['end']:.3f})'"
            f"{label}"
        )
        prev = label
    if annotations:
        # 有标注框:先逐个叠加 drawbox(最后一个输出 [vsub]),再渲染字幕 → 框在字幕下层。
        # 注意:filtergraph 中"纯标签转发"(如 [x][y])非法,故不能先标 [annN] 再转发
        n_ann = len(annotations)
        for i, ann in enumerate(annotations):
            try:
                ax = int(float(ann["x"]) * width)
                ay = int(float(ann["y"]) * height)
                aw = int(float(ann["w"]) * width)
                ah = int(float(ann["h"]) * height)
                a_start = float(ann["start"])
                a_end = float(ann["end"])
            except (KeyError, ValueError, TypeError):
                LOG.warning("标注框 %d 参数非法,已跳过(自检应已拦截)", i)
                continue
            color = str(ann.get("color", "0xFF3B30"))
            nxt = "[vsub]" if i == n_ann - 1 else f"[ann{i}]"
            fc.append(
                f"{prev}drawbox=x={ax}:y={ay}:w={aw}:h={ah}:color={color}:t=6:"
                f"enable='between(t,{a_start:.3f},{a_end:.3f})'{nxt}"
            )
            prev = nxt
        # 全部标注被跳过时 prev 仍是素材链末标签,直接接字幕滤镜同样合法
        fc.append(f"{prev}subtitles={ass}:fontsdir=.[vout]")
    else:
        fc.append(f"{prev}subtitles={ass}:fontsdir=.[vout]")
    args += [
        "-filter_complex", ";".join(fc),
        "-map", "[vout]", "-map", f"{1 + len(materials)}:a:0",
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", audio_bitrate,
        "-movflags", "+faststart",
        "-t", f"{duration:.3f}",
        out,
    ]
    return args


def split_tracks(ffmpeg: str, src: str, video_out: str) -> None:
    """从合成好的 mp4 抽离纯画面流(流拷贝,秒级,无重编码)。

    src:主输出 mp4;video_out:纯画面无音轨文件路径。
    音频分离由调用方直接复制工作目录中的配音/mix 文件实现(零编码)。
    """
    run_ffmpeg(
        [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
         "-i", src, "-an", "-c", "copy", video_out],
        cwd=os.path.dirname(os.path.abspath(video_out)) or ".",
    )


def mix_bgm(
    ffmpeg: str,
    voice: str,
    bgm_path: str,
    *,
    volume: float,
    duration: float,
    out: str,
) -> None:
    """把 BGM 低音量混进配音 mp3(预混方案,主合成命令音频部分保持简单)。

    BGM 裁剪到 duration(不足则 apad 静音补齐),降低音量后与配音 amix;
    normalize=0 保证配音不被音量归一化削掉(需 ffmpeg >= 6.1)。
    """
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", voice, "-i", bgm_path,
        "-filter_complex",
        (f"[1:a]atrim=0:{duration:.3f},asetpts=N/SR/TB,volume={volume:g},apad[bg];"
         f"[0:a][bg]amix=inputs=2:duration=first:normalize=0[aout]"),
        "-map", "[aout]",
        "-c:a", "libmp3lame", "-b:a", "192k",
        out,
    ]
    run_ffmpeg(cmd, cwd=os.path.dirname(os.path.abspath(out)))


def run_ffmpeg(cmd: Sequence[str], *, cwd: str) -> None:
    """执行 ffmpeg 命令。"""
    LOG.info("ffmpeg 命令:\n  %s", " ".join(cmd))
    proc = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg 合成失败(exit={proc.returncode})\n{proc.stderr.strip()}"
        )
    if proc.stderr.strip():
        LOG.debug("ffmpeg stderr: %s", proc.stderr.strip())


def prepare_workdir(out: str) -> tuple[str, str, str]:
    """确定输出目录,并给出音频/字幕的相对名(-i 使用的工作目录内名字)。"""
    out_dir = os.path.dirname(os.path.abspath(out)) or "."
    stem = os.path.splitext(os.path.basename(out))[0]
    os.makedirs(out_dir, exist_ok=True)
    return out_dir, f"{stem}.mp3", f"{stem}.ass"
