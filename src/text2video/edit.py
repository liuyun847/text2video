"""剪辑层(edit 子命令):把"素材内的剪辑点"落成画面流,再把所有声音混成一条母带。

产出(与 produce 共用同一基名 base):
- {base}.mezzanine.mp4       按剪辑清单拼好的画面流(h264 / 无音轨 / 画幅与 fps 同清单)
- {base}.mixed.mp3           一条母带:配音 + 素材原声(按剪辑后时间线对齐)+ BGM(sidechain 压音)
- {base}.edit.report.json    剪辑报告:总时长 / 每段源区间与放置区间 / 转场合计 / 响度前后 / 警告
- {base}.render.json         回填为"单段 mezzanine"清单(原文件先备份为 {base}.render.json.bak)

定位:produce 负责语音、字幕轨、素材片段与清单;edit 是可选增强层 —— 只重写清单里的
audio.path 与 materials(背景/字幕/标注/fps/画幅/时长仍由 timeline 决定),不发语音、
不重烤字幕、不改 produce 行为。渲染端不读 edit.json,它只看回填后的清单。

滤镜语义:
- 视频:每段 -ss in -t (out-in) 输入 -> setpts=(PTS-STARTPTS)/speed 变速 ->
  fps + scale/crop 铺满画幅 -> 段间 xfade(转场)或 concat(硬切);
- 音频:素材原声同样先切 in/out,再 atempo 跟住变速,adelay 放到剪辑后的起点;
- BGM:用 -stream_loop -1 循环铺满,以配音为侧链做 sidechaincompress ducking
  (无配音时退化成固定音量);
- 母带:两遍 loudnorm(第一遍 print_format=json 实测,第二遍带 measured_* 应用),
  再用 ebur128 复测出报告里的"响度之后值"。

命令里不出现媒体路径(只用 [i:v] / [i:a] 标签),所以 Windows 盘符冒号与中文路径
都不需要转义;ffmpeg 参数一律按列表传(不拼字符串)。
"""

from __future__ import annotations

import json
import logging
import math
import os
import shutil
from argparse import Namespace

from . import compose
from .audio import (
    DEFAULT_TARGET_LUFS,
    DEFAULT_TRUE_PEAK,
    LUFS_RANGE,
    PEAK_RANGE,
    SAMPLE_RATE,
    apply_loudnorm,
)
from .produce import (
    AUDIO_SUFFIX,
    RENDER_SUFFIX,
    SUBS_SUFFIX,
    build_render_json,
    material_entry,
    timeline_json_to_base,
)
from .timeline import Timeline

LOG = logging.getLogger(__name__)

# ---- 产物命名与契约版本 ----
MEZZANINE_SUFFIX = ".mezzanine.mp4"
MIXED_SUFFIX = ".mixed.mp3"
REPORT_SUFFIX = ".edit.report.json"
BACKUP_SUFFIX = ".bak"
EDIT_JSON_VERSION = 1

# 变速范围:越界后画面抖动/音质崩坏,直接拒绝
SPEED_MIN = 0.25
SPEED_MAX = 4.0
# atempo 单级 tempo 允许 0.5~100;这里按 sqrt 拆到更保守的一档,0.25 两级即可覆盖
_ATEMPO_STAGE = (0.5, 2.0)

# 母带音频规格:统一 audio.SAMPLE_RATE(48 kHz)立体声 fltp 再混,免得 amix 隐式重采样
# mezzanine 是中间件(crf 20),比素材片段(23)高一档,避免二次压缩糊掉
MEZZANINE_CRF = 20
MEZZANINE_PRESET = "medium"
# 时长/越界比较的容差(秒):素材时长与 in/out 的小数误差不至于误报
EDGE_TOLERANCE = 0.05
# 画面流与 timeline 时长差异超过这个值才告警(秒)
DURATION_TOLERANCE = 0.1

# sidechaincompress 参数:阈值 0.05 约 -26 dBFS,ratio 8:1,attack 20ms,release 250ms
SIDECHAIN_ARGS = "threshold=0.05:ratio=8:attack=20:release=250:makeup=1"

# edit.json 的 transition.type 白名单 -> xfade 的 transition 名(只收常用且在本机
# ffmpeg 9.0 里实测可用的种类;未知类型直接报错,不静默降级)
TRANSITIONS: dict[str, str] = {
    "fade": "fade",
    "fadeblack": "fadeblack",
    "fadewhite": "fadewhite",
    "dissolve": "dissolve",
    "wipeleft": "wipeleft",
    "wiperight": "wiperight",
    "wipeup": "wipeup",
    "wipedown": "wipedown",
    "slideleft": "slideleft",
    "slideright": "slideright",
    "slideup": "slideup",
    "slidedown": "slidedown",
    "circleopen": "circleopen",
    "circleclose": "circleclose",
    "radial": "radial",
    "pixelize": "pixelize",
    "hblur": "hblur",
}

# --out 允许连扩展名一起写(基名会剥掉)
_MEDIA_EXTS = (".mp4", ".mkv", ".mov", ".webm")


class EditError(RuntimeError):
    """剪辑清单/输入不合法(友好报错:CLI 打成"错误: ..."并返回 1)。"""


# ---------------------------------------------------------------------------
# 路径与数值小工具
# ---------------------------------------------------------------------------

def resolve_path(raw: object, base_dir: str) -> str:
    """把 edit.json 里的路径按"相对 edit.json 所在目录"解析成绝对路径。

    参数:
        raw: 原始路径字符串(可为空/None)。
        base_dir: edit.json 所在目录(绝对)。
    返回:
        绝对路径;raw 为空时返回空串。
    异常:
        无(不做存在性检查,纯函数便于单测)。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    path = os.path.expanduser(text)
    if not os.path.isabs(path):
        path = os.path.join(base_dir, path)
    return os.path.abspath(path)


def _require_file(path: str, what: str) -> str:
    """确认文件存在,否则抛 EditError(消息里带解析后的绝对路径)。"""
    if not path or not os.path.isfile(path):
        raise EditError(f"{what} 不存在: {path or '(空路径)'}")
    return path


def _as_float(raw: object, what: str, *, default: float | None = None) -> float:
    """把字段转成有限浮点数;缺失走 default,非法/非有限抛 EditError。"""
    if raw is None:
        if default is None:
            raise EditError(f"{what} 缺失")
        return float(default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise EditError(f"{what} 必须是数字,收到 {raw!r}") from None
    if not math.isfinite(value):
        raise EditError(f"{what} 必须是有限数字,收到 {raw!r}")
    return value


def _as_bool(raw: object, what: str, *, default: bool) -> bool:
    """把字段转成布尔;只接受 JSON 的布尔值(字符串 "false" 会被拒,免得误读)。"""
    if raw is None:
        return default
    if not isinstance(raw, bool):
        raise EditError(f"{what} 必须是 true/false,收到 {raw!r}")
    return raw


def xfade_name(name: str) -> str:
    """契约里的转场名 -> xfade 的 transition 名;未知类型抛 EditError。"""
    try:
        return TRANSITIONS[name]
    except KeyError:
        raise EditError(
            f"未知转场类型 {name!r};支持: {', '.join(sorted(TRANSITIONS))}"
        ) from None


# ---------------------------------------------------------------------------
# edit.json 契约解析
# ---------------------------------------------------------------------------

def _parse_transition(raw: object, where: str) -> dict | None:
    """解析一条 clips 项里的 transition;None 视为硬切。"""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise EditError(f"{where}.transition 必须是对象 {{type, duration}}")
    name = str(raw.get("type") or "").strip()
    if not name:
        raise EditError(f"{where}.transition.type 缺失")
    xfade_name(name)  # 未知类型在这里就报错
    duration = _as_float(raw.get("duration"), f"{where}.transition.duration")
    if duration <= 0:
        raise EditError(
            f"{where}.transition.duration 必须大于 0(不要转场就删掉 transition 字段)"
        )
    return {"type": name, "duration": duration}


def parse_clip(raw: object, index: int, base_dir: str) -> dict:
    """解析并校验一条 clips 项,返回规范化 dict。

    参数:
        raw: edit.json 里的 clips[i];index: 序号(仅用于报错定位);
        base_dir: edit.json 所在目录(相对路径基准)。
    返回:
        {"path", "in", "out", "speed", "volume", "audio", "transition"}。
    异常:
        EditError —— 结构/数值非法或素材文件不存在。
    """
    where = f"clips[{index}]"
    if not isinstance(raw, dict):
        raise EditError(f"{where} 必须是对象")
    path = _require_file(resolve_path(raw.get("path"), base_dir), f"{where}.path")
    in_point = _as_float(raw.get("in"), f"{where}.in", default=0.0)
    out_point = _as_float(raw.get("out"), f"{where}.out")
    speed = _as_float(raw.get("speed"), f"{where}.speed", default=1.0)
    volume = _as_float(raw.get("volume"), f"{where}.volume", default=1.0)
    if in_point < 0:
        raise EditError(f"{where}.in 不能为负: {in_point}")
    if out_point <= in_point:
        raise EditError(f"{where}.out({out_point}) 必须大于 in({in_point})")
    if not SPEED_MIN <= speed <= SPEED_MAX:
        raise EditError(
            f"{where}.speed={speed} 超出 {SPEED_MIN}~{SPEED_MAX}"
            "(超出范围画面抖动、音质也会崩)"
        )
    if volume < 0:
        raise EditError(f"{where}.volume 不能为负: {volume}")
    return {
        "path": path,
        "in": in_point,
        "out": out_point,
        "speed": speed,
        "volume": volume,
        "audio": _as_bool(raw.get("audio"), f"{where}.audio", default=True),
        "transition": _parse_transition(raw.get("transition"), where),
    }


def _as_optional_int(raw: object, what: str) -> int | None:
    """可选整数字段:None 原样返回;非法/非正抛 EditError。"""
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise EditError(f"{what} 必须是整数,收到 {raw!r}") from None
    if value <= 0:
        raise EditError(f"{what} 必须是正整数,收到 {value}")
    return value


def _parse_voice(raw: object, base_dir: str) -> dict | None:
    """解析 voice(缺省 None:此时由 render.json 记录或 produce 约定产物兜底)。"""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise EditError("voice 必须是对象 {path, offset, volume}")
    path = _require_file(resolve_path(raw.get("path"), base_dir), "voice.path")
    offset = _as_float(raw.get("offset"), "voice.offset", default=0.0)
    volume = _as_float(raw.get("volume"), "voice.volume", default=1.0)
    if offset < 0:
        raise EditError(f"voice.offset 不能为负: {offset}")
    if volume < 0:
        raise EditError(f"voice.volume 不能为负: {volume}")
    return {"path": path, "offset": offset, "volume": volume}


def _parse_bgm(raw: object, base_dir: str) -> dict | None:
    """解析 bgm(缺省 None:不混 BGM)。"""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise EditError("bgm 必须是对象 {path, volume, duck}")
    path = _require_file(resolve_path(raw.get("path"), base_dir), "bgm.path")
    volume = _as_float(raw.get("volume"), "bgm.volume", default=0.3)
    if volume <= 0:
        raise EditError(f"bgm.volume 必须大于 0: {volume}")
    return {
        "path": path,
        "volume": volume,
        "duck": _as_bool(raw.get("duck"), "bgm.duck", default=True),
    }


def _parse_master(raw: object) -> dict:
    """解析 master 母带目标(缺省 -14 LUFS / -1.5 dBTP,范围按 loudnorm 选项区间校验)。"""
    if raw is None:
        return {"target_lufs": DEFAULT_TARGET_LUFS, "true_peak": DEFAULT_TRUE_PEAK}
    if not isinstance(raw, dict):
        raise EditError("master 必须是对象 {target_lufs, true_peak}")
    lufs = _as_float(
        raw.get("target_lufs"), "master.target_lufs", default=DEFAULT_TARGET_LUFS,
    )
    peak = _as_float(
        raw.get("true_peak"), "master.true_peak", default=DEFAULT_TRUE_PEAK,
    )
    if not LUFS_RANGE[0] <= lufs <= LUFS_RANGE[1]:
        raise EditError(
            f"master.target_lufs={lufs} 超出 {LUFS_RANGE}(loudnorm 的选项范围)"
        )
    if not PEAK_RANGE[0] <= peak <= PEAK_RANGE[1]:
        raise EditError(
            f"master.true_peak={peak} 超出 {PEAK_RANGE}(loudnorm 的选项范围)"
        )
    return {"target_lufs": lufs, "true_peak": peak}


def parse_edit(data: dict, *, base_dir: str) -> dict:
    """把 edit.json 解析成规范化剪辑规格(相对路径按 base_dir 转绝对)。

    参数:
        data: edit.json 反序列化后的对象;base_dir: edit.json 所在目录。
    返回:
        {"version", "fps", "width", "height", "voice", "clips", "bgm",
          "master", "unknown"};fps/width/height 可能为 None(缺省=跟 timeline 走),
        voice 为 {path, offset, volume}|None,bgm 为 {path, volume, duck}|None,
        master 为 {target_lufs, true_peak},unknown 是未知顶层字段的提示语句。
    异常:
        EditError —— 版本不符 / clips 为空 / 数值越界 / 未知转场 / 文件不存在。
    """
    version = data.get("version", EDIT_JSON_VERSION)
    try:
        version_int = int(version)
    except (TypeError, ValueError):
        raise EditError(f"edit.json version 必须是整数,收到 {version!r}") from None
    if version_int != EDIT_JSON_VERSION:
        raise EditError(
            f"edit.json version={version_int} 不受支持,本工具只认 "
            f"version={EDIT_JSON_VERSION}"
        )
    clips_raw = data.get("clips")
    if not isinstance(clips_raw, list) or not clips_raw:
        raise EditError(
            "edit.json 的 clips 必须是非空数组:没有任何素材就没有画面流"
            "(至少给一段 {path, in, out})"
        )
    clips = [parse_clip(item, idx, base_dir) for idx, item in enumerate(clips_raw)]
    known = {"version", "fps", "width", "height", "voice", "clips", "bgm", "master"}
    unknown = [f"edit.json 含未知字段 {key!r},已忽略" for key in data if key not in known]
    return {
        "version": version_int,
        "fps": _as_optional_int(data.get("fps"), "fps"),
        "width": _as_optional_int(data.get("width"), "width"),
        "height": _as_optional_int(data.get("height"), "height"),
        "voice": _parse_voice(data.get("voice"), base_dir),
        "clips": clips,
        "bgm": _parse_bgm(data.get("bgm"), base_dir),
        "master": _parse_master(data.get("master")),
        "unknown": unknown,
    }


def load_edit(path: str) -> dict:
    """读取并规范化 edit.json(相对路径按 edit.json 所在目录解析)。

    参数:
        path: edit.json 路径。
    返回:
        parse_edit 的规范化结果。
    异常:
        EditError —— 文件不存在 / JSON 损坏 / 顶层不是对象 / 契约不合法。
    """
    if not os.path.isfile(path):
        raise EditError(f"edit.json 不存在: {path}")
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        raise EditError(f"edit.json 读取失败: {exc}") from exc
    if not isinstance(data, dict):
        raise EditError("edit.json 顶层必须是对象")
    return parse_edit(data, base_dir=os.path.dirname(os.path.abspath(path)))


# ---------------------------------------------------------------------------
# 时长与转场规划(纯函数)
# ---------------------------------------------------------------------------

def clip_placed_length(in_point: float, out_point: float, speed: float) -> float:
    """一段素材在成片里的时长:(out-in)/speed。

    参数:
        in_point/out_point: 素材内区间(秒);speed: 变速因子(>0)。
    返回:
        放置时长(秒,浮点)。
    异常:
        EditError —— out<=in 或 speed<=0。
    """
    if speed <= 0:
        raise EditError(f"speed 必须大于 0,收到 {speed!r}")
    if out_point <= in_point:
        raise EditError(f"out({out_point}) 必须大于 in({in_point})")
    return (out_point - in_point) / speed


def atempo_chain(speed: float) -> list[float]:
    """把变速因子拆成若干级 atempo(单级只允许 0.5~100,0.25 需要两级)。

    参数:
        speed: 变速因子(契约范围 0.25~4)。
    返回:
        atempo 的 tempo 列表(乘积约等于 speed);speed 约等于 1 时返回空列表。
    """
    if speed <= 0 or abs(speed - 1.0) < 1e-9:
        return []
    stages: list[float] = []
    rest = float(speed)
    while rest < _ATEMPO_STAGE[0] or rest > _ATEMPO_STAGE[1]:
        factor = math.sqrt(rest)
        stages.append(factor)
        rest /= factor
    stages.append(rest)
    return stages


def clamp_transition(requested: float, prev_length: float, cur_length: float) -> float:
    """转场时长钳到"相邻两段较短者的一半"(超出会吃掉整段,画面直接闪没)。

    参数:
        requested: 请求的转场时长(秒);prev_length/cur_length: 相邻两段的放置时长。
    返回:
        实际可用转场时长(秒);requested<=0 或任一段时长为 0 时返回 0(硬切)。
    """
    if requested <= 0 or prev_length <= 0 or cur_length <= 0:
        return 0.0
    return min(float(requested), min(prev_length, cur_length) / 2.0)


def _place_transition(
    trans: dict | None, index: int, placed: list[dict], length: float,
) -> dict | None:
    """给第 index 段定下实际使用的转场(首段忽略;其余钳到两段较短者的一半)。"""
    if not trans:
        return None
    if index == 0:
        # 画面流开头没有"上一段"可交叉:记下来,由调用方转成告警
        return {
            "type": trans["type"], "duration": 0.0,
            "requested": round(trans["duration"], 6), "clamped": True,
        }
    applied = clamp_transition(trans["duration"], placed[-1]["length"], length)
    if applied <= 0:
        return None
    return {
        "type": trans["type"],
        "duration": round(applied, 6),
        "requested": round(trans["duration"], 6),
        "clamped": applied < trans["duration"] - 1e-9,
    }


def plan_edit(clips: list[dict]) -> dict:
    """按剪辑清单算出每段的放置区间、转场重叠与总时长(纯函数,不碰 ffmpeg)。

    参数:
        clips: parse_edit 规范化后的段列表(需含 in/out/speed/transition)。
    返回:
        {"clips": [原字段 + index/length/start/end/transition],
         "transition_total": float, "duration": float};
        transition 为 None(硬切)或 {"type","duration","requested","clamped"}。
    规则:
        length = (out-in)/speed;第 i 段 transition 的重叠 d 先钳到
        min(len[i-1], len[i])/2;start_0 = 0,start_i = start_(i-1) + len_(i-1) - d;
        总时长 = 各段变速后时长之和 - 转场重叠时长。
    异常:
        EditError —— clips 为空。
    """
    if not clips:
        raise EditError("edit.json 至少要有一段 clips")
    placed: list[dict] = []
    transition_total = 0.0
    cursor = 0.0  # 上一段结束处的剪辑时间
    for index, clip in enumerate(clips):
        length = clip_placed_length(clip["in"], clip["out"], clip["speed"])
        entry = dict(clip, index=index, length=round(length, 6))
        entry["transition"] = _place_transition(
            clip.get("transition"), index, placed, length,
        )
        overlap = entry["transition"]["duration"] if entry["transition"] else 0.0
        transition_total += overlap
        entry["start"] = round(cursor - overlap, 6)
        entry["end"] = round(entry["start"] + length, 6)
        cursor = entry["end"]
        placed.append(entry)
    return {
        "clips": placed,
        "transition_total": round(transition_total, 6),
        "duration": round(cursor, 6),
    }


def plan_warnings(plan: dict) -> list[str]:
    """把 plan_edit 里"被钳制/被忽略"的转场转成人能读的告警。"""
    warnings: list[str] = []
    for clip in plan["clips"]:
        trans = clip.get("transition")
        if not trans or not trans["clamped"]:
            continue
        if clip["index"] == 0:
            warnings.append(
                "第 1 段的 transition 已忽略(画面流开头没有上一段可交叉)"
            )
            continue
        warnings.append(
            f"第 {clip['index'] + 1} 段转场 {trans['requested']:.3f}s 超过相邻两段"
            f"较短者的一半,已钳到 {trans['duration']:.3f}s"
        )
    return warnings


# ---------------------------------------------------------------------------
# 画面流(mezzanine)命令
# ---------------------------------------------------------------------------

def build_mezzanine_filtergraph(
    plan: dict, *, width: int, height: int, fps: int,
) -> str:
    """拼 mezzanine 的 filter_complex:每段变速 + 铺满画幅,段间 xfade 或 concat。

    参数:
        plan: plan_edit 的结果;width/height/fps: 输出画幅与帧率(与清单一致)。
    返回:
        可直接传给 -filter_complex 的字符串,以 [vout] 结尾。
    说明:
        每条链都先 setpts 归零再 settb=AVTB —— xfade 的 offset 按第一路输入的时基
        解读,时基不统一会让转场错位(实测 AVTB 才稳定);scale 用 cover 语义
        (等比放大到贴满再裁切),与 produce 的素材中间件一致。
    """
    parts: list[str] = []
    for clip in plan["clips"]:
        parts.append(
            f"[{clip['index']}:v]setpts=(PTS-STARTPTS)/{clip['speed']:g},"
            f"fps={fps},"
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1,format=yuv420p,settb=AVTB"
            f"[v{clip['index']}]"
        )
    current = "[v0]"
    for clip in plan["clips"][1:]:
        index = clip["index"]
        trans = clip.get("transition")
        if trans:
            parts.append(
                f"{current}[v{index}]xfade="
                f"transition={xfade_name(trans['type'])}:"
                f"duration={trans['duration']:.3f}:offset={clip['start']:.3f}"
                f"[x{index}]"
            )
        else:
            parts.append(f"{current}[v{index}]concat=n=2:v=1:a=0[x{index}]")
        current = f"[x{index}]"
    parts.append(f"{current}null[vout]")
    return ";".join(parts)


def build_mezzanine_cmd(
    ffmpeg: str,
    plan: dict,
    *,
    width: int,
    height: int,
    fps: int,
    out: str,
    crf: int = MEZZANINE_CRF,
    preset: str = MEZZANINE_PRESET,
) -> list[str]:
    """组装 mezzanine 的 ffmpeg 命令(参数按列表传,路径可含空格/中文)。

    参数:
        ffmpeg: 可执行文件;plan/width/height/fps: 同上;out: 输出 mp4;
        crf/preset: x264 参数(中间件默认 crf 20 / medium)。
    返回:
        完整 argv(最后一项是输出路径);-frames:v 锁死帧数 = round(总时长*fps),
        免得 xfade/concat 的帧取整让成片比清单多一帧或少一帧。
    """
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    for clip in plan["clips"]:
        cmd += [
            "-ss", f"{clip['in']:.3f}",
            "-t", f"{clip['out'] - clip['in']:.3f}",
            "-i", clip["path"],
        ]
    frames = max(1, round(plan["duration"] * fps))
    cmd += [
        "-filter_complex",
        build_mezzanine_filtergraph(plan, width=width, height=height, fps=fps),
        "-map", "[vout]", "-an",
        "-frames:v", str(frames),
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        out,
    ]
    return cmd


# ---------------------------------------------------------------------------
# 母带(配音 + 素材原声 + BGM)命令
# ---------------------------------------------------------------------------

def collect_clip_audio(
    plan: dict, has_audio: dict[int, bool],
) -> tuple[list[dict], list[str]]:
    """挑出要混进母带的素材原声段,返回 (列表, 告警)。

    参数:
        plan: plan_edit 的结果;has_audio: {段序号: 素材是否带音频流}(ffprobe 探测)。
    返回:
        (items, warnings);items 每项为
        {"index","path","source_in","source_duration","speed","volume","start"}。
    规则:
        clip.audio=False 不混;声明要混但素材没有音频流 -> 跳过并告警(不静默)。
    """
    items: list[dict] = []
    warnings: list[str] = []
    for clip in plan["clips"]:
        if not clip.get("audio", True):
            continue
        if not has_audio.get(clip["index"], False):
            warnings.append(
                f"第 {clip['index'] + 1} 段 {os.path.basename(clip['path'])} "
                "没有音频流,已跳过该段原声"
            )
            continue
        items.append({
            "index": clip["index"],
            "path": clip["path"],
            "source_in": clip["in"],
            "source_duration": clip["out"] - clip["in"],
            "speed": clip["speed"],
            "volume": clip["volume"],
            "start": clip["start"],
        })
    return items, warnings


def build_premix_filtergraph(
    *,
    clip_audio: list[dict],
    voice: dict | None,
    bgm: dict | None,
    duration: float,
    sample_rate: int = SAMPLE_RATE,
) -> str:
    """拼"母带前置混音"的 filter_complex(不含 loudnorm:母带化单独两遍)。

    参数:
        clip_audio: 素材原声段(每项带 index=本命令里的输入序号);
        voice: 配音 {"index","offset","volume"}|None;
        bgm: BGM {"index","volume","duck"}|None(输入需用 -stream_loop -1 循环);
        duration: 母带时长(秒);sample_rate: 混音采样率。
    返回:
        以 [premix] 结尾的 filter_complex 字符串。
    结构:
        原声:asetpts -> atempo(跟变速) -> volume -> aformat -> adelay(放到剪辑后起点);
        配音:asetpts -> adelay(offset) -> volume -> aformat;做 ducking 时再 asplit
             一路当侧链,否则直接 anull 进混音(分了不接会让 ffmpeg 报错);
        BGM:atrim -> volume -> aformat -> sidechaincompress(侧链=配音);
             无配音(或 duck=false)时退化成固定音量(anull);
        amix(normalize=0) -> apad 补到 duration(裁长交给命令里的 -t)。
    异常:
        EditError —— 一个音频源都没有。
    """
    audio_format = (
        f"aformat=sample_fmts=fltp:sample_rates={sample_rate}"
        ":channel_layouts=stereo"
    )
    parts: list[str] = []
    mixes: list[str] = []
    ducking = bool(voice and bgm and bgm.get("duck", True))
    for order, item in enumerate(clip_audio):
        chain = ["asetpts=N/SR/TB"]
        chain += [f"atempo={stage:.9g}" for stage in atempo_chain(item["speed"])]
        if abs(item.get("volume", 1.0) - 1.0) > 1e-9:
            chain.append(f"volume={item['volume']:g}")
        chain.append(audio_format)
        if item.get("start", 0.0) > 0:
            chain.append(f"adelay={round(item['start'] * 1000)}:all=1")
        parts.append(f"[{item['index']}:a]{','.join(chain)}[ca{order}]")
        mixes.append(f"[ca{order}]")
    if voice:
        chain = ["asetpts=N/SR/TB"]
        if voice.get("offset", 0.0) > 0:
            chain.append(f"adelay={round(voice['offset'] * 1000)}:all=1")
        if abs(voice.get("volume", 1.0) - 1.0) > 1e-9:
            chain.append(f"volume={voice['volume']:g}")
        chain.append(audio_format)
        parts.append(f"[{voice['index']}:a]{','.join(chain)}[voice_raw]")
        # 只有真的要做 ducking 才分出一条侧链:分了不接上,ffmpeg 会报
        # "Filter 'asplit' has output 0 (voice_sc) unconnected" 并整体失败
        if ducking:
            parts.append("[voice_raw]asplit=2[voice_mix][voice_sc]")
        else:
            parts.append("[voice_raw]anull[voice_mix]")
        mixes.append("[voice_mix]")
    if bgm:
        chain = [
            f"atrim=0:{duration:.3f}",
            "asetpts=N/SR/TB",
            f"volume={bgm.get('volume', 0.3):g}",
            audio_format,
        ]
        parts.append(f"[{bgm['index']}:a]{','.join(chain)}[bgm_raw]")
        if ducking:
            parts.append(f"[bgm_raw][voice_sc]sidechaincompress={SIDECHAIN_ARGS}[bgm]")
        else:
            parts.append("[bgm_raw]anull[bgm]")
        mixes.append("[bgm]")
    if not mixes:
        raise EditError("没有任何音频源:配音 / BGM / 素材原声 都是空的,母带无从下手")
    # 只补不裁:裁切交给输出端的 -t(实测 atrim 在这条"多输入 + adelay + 循环 BGM"的链上
    # 会按不可靠的时间戳裁,7.404s 的混音被 atrim=0:7.104 裁成 5.404s;-t 则精确到毫秒)
    tail = f"apad=whole_dur={duration:.3f}[premix]"
    if len(mixes) == 1:
        parts.append(f"{mixes[0]}{tail}")
    else:
        parts.append(
            f"{''.join(mixes)}amix=inputs={len(mixes)}:duration=longest:"
            f"normalize=0,{tail}"
        )
    return ";".join(parts)


def build_premix_cmd(
    ffmpeg: str,
    *,
    clip_audio: list[dict],
    voice: dict | None,
    bgm: dict | None,
    duration: float,
    out: str,
    sample_rate: int = SAMPLE_RATE,
) -> list[str]:
    """组装前置混音命令:输出一条未做响度处理的分轨 WAV(母带两遍 loudnorm 的输入)。

    参数:
        ffmpeg: 可执行文件;clip_audio/voice/bgm/duration: 同 filtergraph;
        out: 输出 wav(建议放输出目录,跑完由调用方删除);sample_rate: 采样率。
    返回:
        完整 argv;素材原声用 -ss/-t 切在输入侧(与画面段同一 in/out),
        BGM 用 -stream_loop -1 循环(短 BGM 也能铺满整条母带)。
    异常:
        EditError —— 一个音频源都没有。
    """
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    clip_specs: list[dict] = []
    index = 0
    for item in clip_audio:
        cmd += [
            "-ss", f"{item['source_in']:.3f}",
            "-t", f"{item['source_duration']:.3f}",
            "-i", item["path"],
        ]
        clip_specs.append(dict(item, index=index))
        index += 1
    voice_spec = None
    if voice:
        cmd += ["-i", voice["path"]]
        voice_spec = dict(voice, index=index)
        index += 1
    bgm_spec = None
    if bgm:
        cmd += ["-stream_loop", "-1", "-i", bgm["path"]]
        bgm_spec = dict(bgm, index=index)
        index += 1
    graph = build_premix_filtergraph(
        clip_audio=clip_specs, voice=voice_spec, bgm=bgm_spec,
        duration=duration, sample_rate=sample_rate,
    )
    cmd += [
        "-filter_complex", graph, "-map", "[premix]",
        "-ar", str(sample_rate), "-ac", "2", "-t", f"{duration:.3f}",
        "-c:a", "pcm_s16le", out,
    ]
    return cmd


# ---------------------------------------------------------------------------
# 时长策略 / 配音来源 / 清单回填
# ---------------------------------------------------------------------------

def mix_duration(
    timeline_duration: float, edit_duration: float, *, has_voice: bool,
) -> float:
    """母带时长:有配音时以 timeline.duration 为准,否则跟随画面流。

    参数:
        timeline_duration: 时间线总时长(秒);edit_duration: 剪辑总时长(秒);
        has_voice: 是否有配音。
    返回:
        母带时长(秒,round 3)。
    理由:
        字幕轨、标注框与清单 duration/durationInFrames 都以 timeline.duration 为
        坐标系,母带必须和它们对齐;没有配音时无从谈起,只能跟着画面走。
    """
    if has_voice and timeline_duration > 0:
        return round(float(timeline_duration), 3)
    return round(float(edit_duration), 3)


def resolve_voice(
    spec: dict, *, base: str, render_doc: dict | None, master_path: str,
) -> tuple[dict | None, list[str]]:
    """决定母带里的配音来源,返回 (voice 规格 | None, 告警)。

    参数:
        spec: parse_edit 结果(含 voice);base: 产物基名;
        render_doc: 同基名 render.json 的内容(produce 记录的配音路径);master_path: 母带路径。
    返回:
        ({"path","offset","volume"} | None, 告警列表)。
    优先级:
        edit.json 的 voice.path > render.json 的 audio.path > {base}.audio.mp3;
        都没有时返回 None(母带只含素材原声/BGM)。
    防御:
        audio.path 若正好是本次母带(上一轮 edit 的 {base}.mixed.mp3),跳过它继续往后找 ——
        否则会把母带当配音再混一遍(这是重跑 edit 的常规状态,只在最终找不到配音时提一句)。
    """
    if spec.get("voice"):
        voice = dict(spec["voice"])
        if os.path.abspath(voice["path"]) == os.path.abspath(master_path):
            raise EditError("voice.path 指向本次要产出的母带(mixed.mp3),请换一条配音")
        return voice, []
    warnings: list[str] = []
    candidates: list[tuple[str, str]] = []
    if render_doc:
        recorded = str((render_doc.get("audio") or {}).get("path") or "")
        if recorded:
            candidates.append((os.path.abspath(recorded), "render.json 里记录的 audio.path"))
    candidates.append((base + AUDIO_SUFFIX, f"{os.path.basename(base)}{AUDIO_SUFFIX}"))
    skipped: list[str] = []
    for path, origin in candidates:
        if os.path.abspath(path) == os.path.abspath(master_path):
            # 上一轮 edit 会把 audio.path 指到母带上,重跑时它是预期状态,不打扰用户
            LOG.info("配音来源 %s 就是本次母带,跳过", origin)
            skipped.append(origin)
            continue
        if os.path.isfile(path):
            return {"path": path, "offset": 0.0, "volume": 1.0}, warnings
    warnings.append(
        "没有找到配音:edit.json 未给 voice,同基名也没有 .audio.mp3"
        + ("(已跳过上一轮母带)" if skipped else "")
        + ";母带只含素材原声/BGM"
    )
    return None, warnings


def render_entry(
    mezzanine_path: str, *, edit_duration: float, timeline_duration: float,
) -> tuple[dict, list[str]]:
    """回填清单用的单段 materials 项 + 画面流与时间线时长不一致的告警。

    参数:
        mezzanine_path: 画面流路径;edit_duration: 剪辑总时长;timeline_duration: 时间线时长。
    返回:
        (materials 单段项 {type,path,start,end,fade}, 告警列表)。
    说明:
        end 取 min(剪辑时长, 时间线时长):画面比配音长时多出的部分渲染端用不到,
        画面比配音短时尾部露出背景(两种情况都会告警,不静默)。
    """
    end = round(edit_duration if timeline_duration <= 0
                else min(edit_duration, timeline_duration), 3)
    warnings: list[str] = []
    if timeline_duration > 0:
        diff = edit_duration - timeline_duration
        if diff > DURATION_TOLERANCE:
            warnings.append(
                f"画面流 {edit_duration:.3f}s 比时间线 {timeline_duration:.3f}s 长 "
                f"{diff:.3f}s:渲染端只用到前 {end:.3f}s,多余画面被丢掉"
            )
        elif diff < -DURATION_TOLERANCE:
            warnings.append(
                f"画面流 {edit_duration:.3f}s 比时间线 {timeline_duration:.3f}s 短 "
                f"{-diff:.3f}s:渲染端尾部会露出背景(可用 clips 的 in/out 补齐)"
            )
    return material_entry("video", mezzanine_path, 0.0, end, 0.0), warnings


def verify_mezzanine(
    ffprobe: str, path: str, *, width: int, height: int,
) -> list[str]:
    """自检画面流:画幅与清单一致、且确实没有音轨(不满足只告警,不阻断)。

    参数:
        ffprobe: ffprobe 路径;path: mezzanine 路径;width/height: 清单画幅。
    返回:
        告警语句列表(空 = 正常)。
    """
    warnings: list[str] = []
    try:
        got_width, got_height = compose.probe_dimensions(ffprobe, path)
    except RuntimeError as exc:
        return [f"画面流自检失败(已忽略): {exc}"]
    if (got_width, got_height) != (width, height):
        warnings.append(
            f"画面流画幅 {got_width}x{got_height} 与清单 {width}x{height} 不一致:"
            "渲染端会拉伸"
        )
    if compose.probe_has_audio(ffprobe, path):
        warnings.append(
            "画面流里带了音轨(本应无音轨):渲染端会忽略它,声音以 mixed.mp3 为准"
        )
    return warnings


def ensure_outputs_absent(paths: list[str], *, force: bool) -> None:
    """产物已存在且没给 --force 时拒绝,避免误覆盖上一轮成品。

    参数:
        paths: 本次要写的产物路径;force: 是否允许覆盖。
    异常:
        EditError —— 有已存在的产物且 force=False(消息里逐个列出)。
    """
    if force:
        return
    existing = [path for path in paths if os.path.exists(path)]
    if existing:
        raise EditError(
            "产物已存在(避免误覆盖;确认要重跑请加 --force):\n  "
            + "\n  ".join(existing)
        )


# ---------------------------------------------------------------------------
# 输入探测 / 输出路径 / 画幅
# ---------------------------------------------------------------------------

def probe_clips(ffprobe: str, clips: list[dict]) -> list[dict]:
    """逐段探测素材:视频流时长与是否带音频流,并校验 in/out 不越界。

    参数:
        ffprobe: ffprobe 路径;clips: parse_edit 的 clips。
    返回:
        与 clips 一一对应的 [{"index","source_duration","has_audio"}]。
    异常:
        EditError —— 没有视频流 / in 越界 / out 超出素材实际时长。
    """
    metas: list[dict] = []
    for index, clip in enumerate(clips):
        where = f"第 {index + 1} 段 {os.path.basename(clip['path'])}"
        try:
            source_duration = compose.probe_video_duration(ffprobe, clip["path"])
        except RuntimeError as exc:
            raise EditError(f"{where} 探测失败(可能不是视频文件): {exc}") from exc
        if clip["in"] >= source_duration - EDGE_TOLERANCE:
            raise EditError(
                f"{where} 的 in={clip['in']:.3f}s 已到/超过素材尾部"
                f"(素材只有 {source_duration:.3f}s)"
            )
        if clip["out"] > source_duration + EDGE_TOLERANCE:
            raise EditError(
                f"{where} 的 out={clip['out']:.3f}s 超出素材实际时长 "
                f"{source_duration:.3f}s"
            )
        metas.append({
            "index": index,
            "source_duration": round(source_duration, 3),
            "has_audio": compose.probe_has_audio(ffprobe, clip["path"]),
        })
    return metas


def output_base(out: object, timeline_path: str) -> str:
    """产物基名:显式 --out(允许连媒体扩展名)> timeline 路径剥后缀。

    参数:
        out: 命令行 --out(可为 None);timeline_path: timeline.json 路径。
    返回:
        绝对路径基名。
    """
    text = str(out or "").strip()
    if not text:
        return timeline_json_to_base(timeline_path)
    base = os.path.abspath(os.path.expanduser(text))
    stem, ext = os.path.splitext(base)
    return stem if ext.lower() in _MEDIA_EXTS else base


def canvas_from_timeline(tl: Timeline, spec: dict) -> tuple[int, int, int]:
    """取 timeline 的 (fps, width, height),并校验 edit.json 显式值不与之冲突。

    参数:
        tl: 时间线;spec: parse_edit 结果(可含 fps/width/height)。
    返回:
        (fps, width, height)。
    异常:
        EditError —— edit.json 的画幅/帧率与 timeline 不一致。
    说明:
        清单里的 width/height/fps 是跨两端的唯一权威(字幕轨与素材片段都按它做),
        edit 若按另一套画幅编码画面流,渲染端会把画面拉伸 —— 所以直接拒绝,
        而不是悄悄改清单。
    """
    fps = int(tl.meta.get("fps", 60) or 60)
    width = int(tl.meta.get("width", 1080) or 1080)
    height = int(tl.meta.get("height", 1080) or 1080)
    for key, value in (("fps", fps), ("width", width), ("height", height)):
        given = spec.get(key)
        if given is not None and int(given) != value:
            raise EditError(
                f"edit.json 的 {key}={given} 与 timeline 的 {value} 不一致:"
                "画面流必须与清单同画幅/帧率(否则渲染端会拉伸),"
                "请改 timeline(重跑 produce)或删掉 edit.json 里的这个字段"
            )
    return fps, width, height


def _load_timeline(path: str) -> Timeline:
    """读 timeline.json;损坏时转成可读的 EditError。"""
    try:
        return Timeline.from_json(path)
    except (OSError, ValueError) as exc:
        raise EditError(f"timeline 读取失败: {path}: {exc}") from exc


def read_render_doc(path: str) -> dict | None:
    """读同基名 render.json(produce 的旧清单):不存在/损坏都返回 None,不阻塞。"""
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8-sig") as f:
            doc = json.load(f)
    except (OSError, ValueError) as exc:
        LOG.warning("旧 render.json 读取失败(忽略): %s", exc)
        return None
    return doc if isinstance(doc, dict) else None


def subs_path_from(render_doc: dict | None, base: str) -> str:
    """字幕轨沿用 produce 的产物:优先旧清单里记的路径,否则 {base}.subs.webm。"""
    recorded = str(((render_doc or {}).get("subtitle") or {}).get("path") or "")
    return os.path.abspath(recorded) if recorded else base + SUBS_SUFFIX


def _write_json(path: str, payload: dict) -> None:
    """把 dict 写成 UTF-8 JSON(中文不转义、2 空格缩进、结尾换行)。"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------

def _report_clip(clip: dict, meta: dict | None) -> dict:
    """报告里的一段:源区间 / 变速后时长 / 放置区间 / 转场 / 音量。"""
    meta = meta or {}
    return {
        "index": clip["index"] + 1,
        "path": clip["path"],
        "in": round(clip["in"], 3),
        "out": round(clip["out"], 3),
        "speed": clip["speed"],
        "source_duration": meta.get("source_duration"),
        "length": round(clip["length"], 3),
        "start": round(clip["start"], 3),
        "end": round(clip["end"], 3),
        "transition": None if not clip.get("transition") else {
            "type": clip["transition"]["type"],
            "duration": round(clip["transition"]["duration"], 3),
            "requested": round(clip["transition"]["requested"], 3),
            "clamped": bool(clip["transition"]["clamped"]),
        },
        "volume": clip["volume"],
        "audio": bool(clip.get("audio", True)),
        "has_audio": meta.get("has_audio"),
    }


def build_report(ctx: dict) -> dict:
    """组装 {base}.edit.report.json 的内容(纯组装,不做任何探测/落盘)。

    参数:
        ctx: 需含 plan / clip_meta / timeline_path / timeline_duration /
             fps / width / height / voice_path / mezzanine / master / report /
             render / backup / mezzanine_duration / master_duration /
             loudness / warnings。
    返回:
        报告 dict:总时长(剪辑/画面流/母带/时间线)、每段起止与源区间、转场合计、
        响度前后值、警告列表;段号 index 一律 1-based(与告警文案一致)。
    """
    plan = ctx["plan"]
    transitions = [
        {"index": clip["index"] + 1, **clip["transition"]}
        for clip in plan["clips"]
        if clip.get("transition") and clip["index"] > 0
    ]
    clips = [
        _report_clip(clip, ctx["clip_meta"][clip["index"]] if ctx.get("clip_meta") else None)
        for clip in plan["clips"]
    ]
    return {
        "version": EDIT_JSON_VERSION,
        "timeline": {
            "path": ctx["timeline_path"],
            "duration": round(float(ctx["timeline_duration"]), 3),
            "fps": ctx["fps"],
            "width": ctx["width"],
            "height": ctx["height"],
            "voice": ctx.get("voice_path"),
        },
        "outputs": {
            "mezzanine": ctx["mezzanine"],
            "master": ctx["master"],
            "report": ctx["report"],
            "render": ctx["render"],
            "backup": ctx["backup"] if os.path.exists(ctx["backup"]) else None,
        },
        "duration": {
            "edit": round(float(plan["duration"]), 3),
            "mezzanine": round(float(ctx["mezzanine_duration"]), 3),
            "master": round(float(ctx["master_duration"]), 3),
            "timeline": round(float(ctx["timeline_duration"]), 3),
        },
        "clips": clips,
        "transitions": {
            "count": len(transitions),
            "total": round(float(plan["transition_total"]), 3),
            "items": transitions,
        },
        "loudness": ctx["loudness"],
        "warnings": list(ctx["warnings"]),
    }


def summary_lines(report: dict) -> list[str]:
    """把报告压成给人看的几行(纯函数,便于单测)。"""
    duration = report["duration"]
    timeline = report["timeline"]
    loudness = report["loudness"]
    lines = [
        (
            f"画面流(mezzanine): {report['outputs']['mezzanine']}"
            f"({duration['mezzanine']:.3f} 秒,{timeline['width']}x"
            f"{timeline['height']}@{timeline['fps']}fps,无音轨)"
        ),
        f"母带: {report['outputs']['master']}({duration['master']:.3f} 秒)",
        (
            f"剪辑总时长: {duration['edit']:.3f} 秒 / 转场 "
            f"{report['transitions']['count']} 处合计 "
            f"{report['transitions']['total']:.3f} 秒 / 时间线 "
            f"{duration['timeline']:.3f} 秒"
        ),
    ]
    if loudness:
        before = loudness["before"]
        after = loudness["after"]
        lines.append(
            f"响度: {before['i']:.1f} → {after['i']:.1f} LUFS"
            f"(目标 {loudness['target_lufs']:g}),true peak "
            f"{before['tp']:.1f} → {after['tp']:.1f} dBFS"
            f"(目标 {loudness['true_peak']:g},{loudness['passes']} 遍 "
            f"loudnorm/{loudness['mode']})"
        )
    lines.append(f"剪辑报告: {report['outputs']['report']}")
    backup = report["outputs"]["backup"]
    lines.append(
        f"回填清单: {report['outputs']['render']}"
        + (f"(原文件已备份 {backup})" if backup else "(原本没有 render.json)")
    )
    return lines

# ---------------------------------------------------------------------------
# 母带与跑批
# ---------------------------------------------------------------------------

def voice_warnings(ffprobe: str, voice: dict | None, duration: float) -> list[str]:
    """配音(含 offset)超出母带时长时告警 —— 尾部会被截断。

    参数:
        ffprobe: ffprobe 路径;voice: 配音规格 | None;duration: 母带时长(秒)。
    返回:
        告警语句列表(配音缺失/时长探测失败/超长各一条,可能为空)。
    """
    if not voice:
        return []
    try:
        voice_length = compose.probe_duration(ffprobe, voice["path"])
    except RuntimeError as exc:
        return [f"配音时长探测失败(已忽略): {exc}"]
    total = voice.get("offset", 0.0) + voice_length
    if total > duration + EDGE_TOLERANCE:
        return [
            (
                f"配音 {total:.3f}s(含 offset)超过母带 {duration:.3f}s:"
                "尾部会被截断,请调 voice.offset 或核对 timeline 时长"
            )
        ]
    return []


def build_master(
    *,
    ffmpeg: str,
    ffprobe: str,
    master: str,
    duration: float,
    clip_audio: list[dict],
    voice: dict | None,
    bgm: dict | None,
    master_spec: dict,
    cwd: str,
) -> tuple[dict, list[str], float]:
    """混母带:前置混音 WAV(配音 + 原声 + BGM) -> 两遍 loudnorm -> mp3。

    参数:
        ffmpeg/ffprobe: 可执行文件;master: 输出 mp3;duration: 母带时长(秒);
        clip_audio/voice/bgm: 混音源(见 build_premix_cmd);
        master_spec: {"target_lufs","true_peak"};cwd: 工作目录(前置 WAV 落这里)。
    返回:
        (响度信息, 告警, 母带实测时长)。
    副作用:
        写前置混音 WAV;不论成败都删除,不留中间产物。
    """
    premix = os.path.splitext(master)[0] + ".pre.wav"
    LOG.info(
        "混母带: %.3f 秒(素材原声 %d 段 / 配音 %s / BGM %s)",
        duration, len(clip_audio), "有" if voice else "无", "有" if bgm else "无",
    )
    try:
        compose.run_ffmpeg(
            build_premix_cmd(
                ffmpeg, clip_audio=clip_audio, voice=voice, bgm=bgm,
                duration=duration, out=premix,
            ),
            cwd=cwd,
        )
        loudness, warnings = apply_loudnorm(
            ffmpeg, premix, master,
            target_lufs=master_spec["target_lufs"],
            true_peak=master_spec["true_peak"],
            cwd=cwd,
        )
        out_duration = compose.probe_duration(ffprobe, master)
    finally:
        if os.path.exists(premix):
            os.remove(premix)
    LOG.info(
        "母带完成: %s(%.3f 秒 / %.1f LUFS)",
        master, out_duration, loudness["after"]["i"],
    )
    return loudness, warnings, out_duration


def run_edit(args: Namespace) -> int:
    """edit 入口:timeline + edit.json -> 画面流 + 母带 + 报告 -> 回填 render.json。

    参数:
        args: 命令行命名空间(--timeline / --edit / --out / --force)。
    返回:
        0 成功(时长不一致等只告警;清单/探测类问题抛 EditError)。
    异常:
        EditError —— 输入不合法;RuntimeError —— ffmpeg/ffprobe 失败。
    """
    timeline_path = os.path.abspath(os.path.expanduser(str(args.timeline)))
    if not os.path.isfile(timeline_path):
        raise EditError(f"timeline 不存在: {timeline_path}")
    edit_path = os.path.abspath(os.path.expanduser(str(args.edit)))
    spec = load_edit(edit_path)
    tl = _load_timeline(timeline_path)
    base = output_base(getattr(args, "out", None), timeline_path)
    fps, width, height = canvas_from_timeline(tl, spec)
    mezzanine = base + MEZZANINE_SUFFIX
    master = base + MIXED_SUFFIX
    report_path = base + REPORT_SUFFIX
    render_path = base + RENDER_SUFFIX
    backup_path = render_path + BACKUP_SUFFIX
    ensure_outputs_absent(
        [mezzanine, master, report_path], force=bool(getattr(args, "force", False)),
    )
    out_dir = os.path.dirname(base)
    os.makedirs(out_dir, exist_ok=True)

    ffmpeg = compose.find_ffmpeg()
    ffprobe = compose.find_ffprobe(ffmpeg)
    warnings: list[str] = list(spec["unknown"])
    clip_meta = probe_clips(ffprobe, spec["clips"])
    plan = plan_edit(spec["clips"])
    warnings += plan_warnings(plan)
    LOG.info(
        "剪辑规划: %d 段 / 转场 %d 处合计 %.3f 秒 / 总时长 %.3f 秒",
        len(plan["clips"]),
        sum(1 for clip in plan["clips"]
            if clip.get("transition") and clip["index"] > 0),
        plan["transition_total"], plan["duration"],
    )
    compose.run_ffmpeg(
        build_mezzanine_cmd(
            ffmpeg, plan, width=width, height=height, fps=fps, out=mezzanine,
        ),
        cwd=out_dir,
    )
    mezzanine_duration = compose.probe_duration(ffprobe, mezzanine)
    warnings += verify_mezzanine(ffprobe, mezzanine, width=width, height=height)
    LOG.info("画面流完成: %s(%.3f 秒 @%dfps)", mezzanine, mezzanine_duration, fps)

    render_doc = read_render_doc(render_path)
    voice, notes = resolve_voice(
        spec, base=base, render_doc=render_doc, master_path=master,
    )
    warnings += notes
    clip_audio, notes = collect_clip_audio(
        plan, {meta["index"]: meta["has_audio"] for meta in clip_meta},
    )
    warnings += notes
    duration = mix_duration(tl.duration, plan["duration"], has_voice=voice is not None)
    warnings += voice_warnings(ffprobe, voice, duration)
    loudness, notes, master_duration = build_master(
        ffmpeg=ffmpeg, ffprobe=ffprobe, master=master, duration=duration,
        clip_audio=clip_audio, voice=voice, bgm=spec["bgm"],
        master_spec=spec["master"], cwd=out_dir,
    )
    warnings += notes
    if abs(master_duration - duration) > DURATION_TOLERANCE:
        warnings.append(
            f"母带实测 {master_duration:.3f}s 与预期 {duration:.3f}s 不一致:"
            "检查混音滤镜或 -t 参数"
        )

    entry, notes = render_entry(
        mezzanine,
        edit_duration=mezzanine_duration,
        timeline_duration=tl.duration,
    )
    warnings += notes
    if os.path.exists(render_path):
        shutil.copyfile(render_path, backup_path)
    report = build_report({
        "plan": plan,
        "clip_meta": clip_meta,
        "timeline_path": timeline_path,
        "timeline_duration": tl.duration,
        "fps": fps,
        "width": width,
        "height": height,
        "voice_path": voice["path"] if voice else None,
        "mezzanine": mezzanine,
        "master": master,
        "report": report_path,
        "render": render_path,
        "backup": backup_path,
        "mezzanine_duration": mezzanine_duration,
        "master_duration": master_duration,
        "loudness": loudness,
        "warnings": warnings,
    })
    _write_json(report_path, report)
    render = build_render_json(
        tl,
        audio_path=master,
        subs_path=subs_path_from(render_doc, base),
        materials=[entry],
    )
    _write_json(render_path, render)
    for line in summary_lines(report):
        print(line)
    for note in report["warnings"]:
        print(f"警告: {note}")
    return 0

