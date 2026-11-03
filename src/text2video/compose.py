"""媒体中间件模块:定位 ffmpeg/ffprobe、探测媒体信息、生产交给 Remotion 的素材中间件。

定位变化(本模块不再做整片画面合成):
- 保留:可执行文件定位、ffprobe 探测(时长/尺寸/透明标记)、BGM 混音、音频转 mp3;
- 新增:透明字幕轨(ASS 烧到 alpha 画布 → VP9/yuva420p)、素材段裁切(裁到输出画幅);
- 移除:整片画面合成命令(build_video_cmd)、音画分离(split_tracks)。

所有命令在输出目录内执行(cwd=输出目录),故音频/字幕/字体一律用相对名,
避免 Windows 盘符冒号在 ffmpeg 滤镜参数里的转义问题。
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
    raise RuntimeError("未找到 ffprobe,无法读取媒体时长与尺寸")


# ---------------------------------------------------------------------------
# 探测
# ---------------------------------------------------------------------------

def _ffprobe_lines(ffprobe: str, args: Sequence[str], path: str) -> list[str]:
    """执行一次 ffprobe 并返回去空行后的 stdout;失败抛 RuntimeError(带 stderr)。"""
    cmd = [ffprobe, "-v", "error", *args, path]
    out = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=False,
    )
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe 读取失败: {out.stderr.strip() or '无输出'}")
    return [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]


def probe_duration(ffprobe: str, audio: str) -> float:
    """用 ffprobe 读取媒体时长(秒)。"""
    lines = _ffprobe_lines(
        ffprobe, ["-show_entries", "format=duration",
                  "-of", "default=noprint_wrappers=1:nokey=1"], audio,
    )
    try:
        return float(lines[-1])
    except (IndexError, ValueError):
        raise RuntimeError(f"无法解析媒体时长: {lines!r}")


def probe_video_duration(ffprobe: str, path: str) -> float:
    """探测视频素材时长(秒);无视频流 / 不可解码时抛 RuntimeError。

    用视频流时长而非 format 时长:容器 format 时长可能含尾部 padding,
    素材裁剪(in/out)边界校验以视频流实际时长为准更可靠。
    """
    lines = _ffprobe_lines(
        ffprobe, ["-select_streams", "v:0", "-show_entries", "stream=duration",
                  "-of", "default=noprint_wrappers=1:nokey=1"], path,
    )
    try:
        return float(lines[-1])
    except (IndexError, ValueError):
        raise RuntimeError(f"无法解析视频时长: {lines!r}")


def probe_dimensions(ffprobe: str, path: str) -> tuple[int, int]:
    """探测媒体主视频流(视频素材或图片)像素尺寸 (width, height);失败抛 RuntimeError。

    用于画面裁剪:素材带 crop 时按原始分辨率计算裁剪窗口像素。
    """
    lines = _ffprobe_lines(
        ffprobe, ["-select_streams", "v:0", "-show_entries", "stream=width,height",
                  "-of", "default=noprint_wrappers=1"], path,
    )
    dims: dict[str, int] = {}
    for line in lines:
        key, _, val = line.partition("=")
        if key in ("width", "height"):
            try:
                dims[key] = int(val.strip())
            except ValueError:
                raise RuntimeError(f"无法解析素材尺寸: {line!r}")
    if dims.get("width") and dims.get("height"):
        return dims["width"], dims["height"]
    raise RuntimeError(f"未读到完整尺寸: {lines!r}")


def probe_alpha_mode(ffprobe: str, path: str) -> str | None:
    """读取 WebM 的 alpha_mode 标记(有透明通道时为 "1",否则 None)。

    VP9/WebM 的 alpha 不占主画面 pix_fmt(ffprobe 仍报 yuv420p),而是另存一条
    alpha 平面并用容器级 alpha_mode 标记;因此"是否真的带透明"只能看这个标记。
    """
    lines = _ffprobe_lines(
        ffprobe, ["-select_streams", "v:0", "-show_entries", "stream_tags=alpha_mode",
                  "-of", "default=noprint_wrappers=1:nokey=1"], path,
    )
    return lines[-1] if lines else None


def pick_system_font() -> str | None:
    """挑一个含中文的字体文件(供 libass 渲染 ASS 字幕)。"""
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

def build_subtitle_track_cmd(
    ffmpeg: str,
    *,
    ass: str,                  # 输出目录内的相对名(避免 Windows 路径冒号转义)
    out: str,
    width: int,
    height: int,
    fps: int,
    duration: float,
    fontsdir: str | None = None,
    crf: int = 30,
) -> list[str]:
    """组装"ASS 烧到透明画布 → 透明字幕轨(WebM)"命令。

    透明底三步缺一不可(实测:少任何一步 alpha 恒为 255,叠到背景上就是黑块):
    1. 画布色块用 color=c=black 生成,再 format=rgba 带上 alpha 通道;
    2. colorchannelmixer=aa=0 把整幅 alpha 显式压成 0(不依赖色源的 @0 语义);
    3. subtitles/ass 滤镜必须写 alpha=1(默认 false,不处理 alpha 通道)。
    编码用 VP9 + pix_fmt=yuva420p;auto-alt-ref=0 是 libvpx-vp9 保留 alpha 平面的
    必要条件,row-mt 加速。帧数用 -frames:v 锁死(= round(duration*fps)),与
    render.json 的 durationInFrames 一致,避免 -t 的帧取整让消费端少一帧。
    fontsdir 为输出目录内的相对子目录名(字体文件独占该目录,避免把其他文件当字体)。
    """
    canvas = f"color=c=black:s={width}x{height}:r={fps}:d={duration:.3f}"
    sub = f"format=rgba,colorchannelmixer=aa=0,subtitles={ass}:alpha=1"
    if fontsdir:
        sub = (f"format=rgba,colorchannelmixer=aa=0,"
               f"subtitles={ass}:fontsdir={fontsdir}:alpha=1")
    return [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", canvas,
        "-vf", sub,
        "-frames:v", str(max(1, round(duration * fps))),
        "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
        "-auto-alt-ref", "0", "-b:v", "0", "-crf", str(crf),
        "-row-mt", "1", "-cpu-used", "4",
        out,
    ]


def build_material_cmd(
    ffmpeg: str,
    *,
    src: str,
    out: str,
    kind: str,                 # video | image
    width: int,
    height: int,
    fps: int,
    in_point: float | None = None,
    out_point: float | None = None,
    crop: dict | None = None,  # 像素裁剪窗口 {"w","h","x","y"}(已按画幅比例算好)
    crf: int = 23,
    preset: str = "medium",
) -> list[str]:
    """组装素材中间件裁切命令:裁成"输出画幅 + 输出 fps"的独立素材。

    - video:trim(in/out) → 裁窗/铺满 → scale → fps,输出 h264 **无音轨** mp4;
    - image:裁窗/铺满 → scale,输出单帧 png(用 -update 1 覆盖同名文件)。

    crop 给出时窗口比例恒等于画幅(cover 满幅窗口),直接 scale 到画幅即不变形;
    缺省时按"等比放大到贴满 + 中心裁切"处理(与旧渲染语义一致)。
    """
    chain: list[str] = []
    if kind == "video":
        if in_point is None or out_point is None:
            raise ValueError("视频素材必须给出 in_point/out_point")
        chain.append(f"trim=start={in_point:.3f}:end={out_point:.3f}")
        chain.append("setpts=PTS-STARTPTS")
    if crop:
        chain.append(f"crop={crop['w']}:{crop['h']}:{crop['x']}:{crop['y']}")
        chain.append(f"scale={width}:{height}")
    else:
        chain.append(f"scale={width}:{height}:force_original_aspect_ratio=increase")
        chain.append(f"crop={width}:{height}")
    chain.append("setsar=1")
    if kind == "image":
        return [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-i", src, "-vf", ",".join(chain),
            "-frames:v", "1", "-update", "1", out,
        ]
    chain.append(f"fps={fps}")
    return [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", src, "-vf", ",".join(chain),
        "-an", "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        out,
    ]


def mix_bgm(
    ffmpeg: str,
    voice: str,
    bgm_path: str,
    *,
    volume: float,
    duration: float,
    out: str,
    bitrate: str = "192k",
) -> None:
    """把 BGM 低音量混进配音(预混方案:最终音轨就是混好的 mp3)。

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
        "-c:a", "libmp3lame", "-b:a", bitrate,
        out,
    ]
    run_ffmpeg(cmd, cwd=os.path.dirname(os.path.abspath(out)))


def transcode_audio(ffmpeg: str, src: str, out: str, *, bitrate: str = "192k") -> None:
    """把任意音频(SAPI 的 WAV 等)转成 mp3,保证最终音轨统一为 mp3 容器。"""
    run_ffmpeg(
        [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
         "-i", src, "-vn", "-c:a", "libmp3lame", "-b:a", bitrate, out],
        cwd=os.path.dirname(os.path.abspath(out)),
    )


def run_ffmpeg(cmd: Sequence[str], *, cwd: str) -> None:
    """执行 ffmpeg 命令(cwd=产物目录,命令内用相对名)。"""
    LOG.info("ffmpeg 命令:\n  %s", " ".join(cmd))
    proc = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg 执行失败(exit={proc.returncode})\n{proc.stderr.strip()}"
        )
    if proc.stderr.strip():
        LOG.debug("ffmpeg stderr: %s", proc.stderr.strip())
