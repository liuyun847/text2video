"""交付层模块:成片 -> 多规格 + 封面 + 响度复核 + 对位校验 + 交付报告(deliver 子命令)。

一条命令把"成片 mp4"加工成"平台可上传的交付包",全部落在 `--out` 目录:

- `{stem}-{spec}.mp4`   多规格成片:landscape 原样(或按 timeline 画幅)、portrait 1080x1920、
                         square 1080x1080;目标尺寸与源一致时走**流复制**(不重编码),
                         不一致时按 `--fit` 适配(默认 blur = 模糊背景填充,四周无黑边)
- `cover-{n}-{秒}s.png` 按 `--cover-at` 抽的封面帧(值 >= 1 视为秒,0~1 视为全片比例)
- `deliver.report.md`   交付报告:规格清单 / 文件大小 / 响度 / 对位偏差 / 警告

复核项:
- **响度**:`ffmpeg -af ebur128=peak=true` 测 integrated LUFS 与 true peak,对照目标给结论;
- **音画对位**:`ffmpeg -af silencedetect` 找语音段起点,与 timeline 的 speech 段逐句比较,
  报告最大偏差与超阈值的句子(给了 `--timeline` 才做,否则跳过)。

失败语义:规格/封面属于交付核心产物,任何一步失败都抛 RuntimeError(带 ffmpeg 原始 stderr);
响度/对位属于"复核",缺音频流等情况下退化为"跳过 + 警告",不阻塞交付,绝不静默。

本模块的纯函数(参数解析 / 规格尺寸 / 命令拼装 / 日志解析 / 报告渲染)都不碰进程,
便于单测;真正的 ffmpeg 调用集中在本文件的 `run_capture`。
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass, field

from . import compose

LOG = logging.getLogger(__name__)

# ---- 规格 ----
SUPPORTED_SPECS: tuple[str, ...] = ("landscape", "portrait", "square")
DEFAULT_SPECS: tuple[str, ...] = SUPPORTED_SPECS
# 固定尺寸的规格(landscape 例外:原样 / 按 timeline 画幅)
SPEC_SIZES: dict[str, tuple[int, int]] = {
    "portrait": (1080, 1920),
    "square": (1080, 1080),
}
# 画面适配方式:blur=模糊背景填充(默认);crop=居中裁切
FIT_MODES: tuple[str, ...] = ("blur", "crop")
DEFAULT_FIT = "blur"
# 适配方式的可读名(报告里用)
FIT_LABELS = {"blur": "模糊背景填充", "crop": "居中裁切", "copy": "原样(流复制)"}

# ---- 封面 / 响度 / 对位默认值 ----
DEFAULT_COVER_AT = "1.0"
DEFAULT_LUFS_TARGET = -14.0
DEFAULT_DBTP_TARGET = -1.5
DEFAULT_LUFS_TOLERANCE = 1.0
DEFAULT_SYNC_TOLERANCE = 0.25
# 抽帧/对位允许的时间余量(秒):避免 `--cover-at` 正好落在片尾导致抽不到帧
_TAIL_MARGIN = 0.05

REPORT_NAME = "deliver.report.md"


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MediaInfo:
    """一份媒体文件的探测结果(成片本身)。"""

    path: str
    width: int
    height: int
    fps: float
    duration: float
    size_bytes: int


@dataclass(frozen=True)
class SpecOutput:
    """一条规格的产出。"""

    name: str
    width: int
    height: int
    path: str
    size_bytes: int
    mode: str          # copy | blur | crop


@dataclass(frozen=True)
class CoverOutput:
    """一张封面的产出:用户给的原始值、实际抽帧时刻、文件路径。"""

    request: float
    at: float
    path: str


@dataclass(frozen=True)
class Loudness:
    """ebur128 的响度结论;测不到的项为 None。"""

    integrated: float | None
    true_peak: float | None
    lra: float | None


@dataclass(frozen=True)
class SyncRow:
    """一句语音的对位比较;actual/delta 为 None 表示没找到对应语音起点。"""

    index: int
    sentence: str
    expected: float
    actual: float | None
    delta: float | None


@dataclass
class DeliverResult:
    """一次交付的全部结果(报告就是它的渲染)。"""

    video: MediaInfo
    out_dir: str
    specs: list[SpecOutput] = field(default_factory=list)
    covers: list[CoverOutput] = field(default_factory=list)
    loudness: Loudness | None = None
    sync: list[SyncRow] = field(default_factory=list)
    lufs_target: float = DEFAULT_LUFS_TARGET
    dbtp_target: float = DEFAULT_DBTP_TARGET
    sync_tolerance: float = DEFAULT_SYNC_TOLERANCE
    warnings: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)   # 复核项 -> 跳过原因
    report_path: str = ""


# ---------------------------------------------------------------------------
# 参数解析(纯函数)
# ---------------------------------------------------------------------------

def parse_specs(spec: str | Sequence[str] | None) -> tuple[str, ...]:
    """解析 `--specs`("landscape,portrait,square" / 列表),返回有序去重的规格名。

    参数:
        spec: 逗号分隔字符串、字符串序列,或 None(视为全部规格)。
    返回:
        tuple[str, ...]: 规格名(小写,保持用户顺序)。
    异常:
        ValueError: 出现不支持的规格,或解析结果为空。
    """
    if spec is None:
        return DEFAULT_SPECS
    raw = [spec] if isinstance(spec, str) else list(spec)
    out: list[str] = []
    for chunk in raw:
        for item in str(chunk).split(","):
            name = item.strip().lower()
            if not name:
                continue
            if name not in SUPPORTED_SPECS:
                raise ValueError(
                    f"不支持的规格 {name!r};可选: {', '.join(SUPPORTED_SPECS)}"
                )
            if name not in out:
                out.append(name)
    if not out:
        raise ValueError(
            f"规格为空;可选: {', '.join(SUPPORTED_SPECS)}(逗号分隔)"
        )
    return tuple(out)


def parse_fit(fit: str | None) -> str:
    """校验画面适配方式(blur / crop),None 走默认。"""
    value = (fit or DEFAULT_FIT).strip().lower()
    if value not in FIT_MODES:
        raise ValueError(
            f"不支持的适配方式 {value!r};可选: {', '.join(FIT_MODES)}"
        )
    return value


def parse_cover_at(spec: str | float | Sequence[float] | None) -> tuple[float, ...]:
    """解析 `--cover-at`("1.0,0.3"),返回升序无关的原样数值元组。

    语义(见 resolve_cover_time):`0 < v < 1` 视为占全片时长的比例,`v >= 1` 视为秒;
    这也是"默认片头 1.0s"能成立的原因(1.0 走秒,不会被当成 100% 处)。

    参数:
        spec: 逗号分隔字符串 / 单个 float / float 序列,或 None(默认抽 1.0s)。
    返回:
        tuple[float, ...]: 用户给出的数值序列(保序、去重)。
    异常:
        ValueError: 空值、非数字,或出现负值。
    """
    if spec is None:
        return (float(DEFAULT_COVER_AT),)
    items: list[str] = []
    if isinstance(spec, str):
        items = [s for s in spec.split(",") if s.strip()]
    elif isinstance(spec, (int, float)):
        items = [str(spec)]
    else:
        items = [str(s) for s in spec]
    out: list[float] = []
    for item in items:
        try:
            value = float(str(item).strip())
        except (TypeError, ValueError):
            raise ValueError(f"--cover-at 不是数字: {item!r}") from None
        if value < 0:
            raise ValueError(f"--cover-at 不能为负: {value:g}")
        if value not in out:
            out.append(value)
    if not out:
        raise ValueError("--cover-at 为空:至少给一个时间点(秒或比例)")
    return tuple(out)


def resolve_cover_time(value: float, duration: float) -> float:
    """把 `--cover-at` 的一个值换算成实际抽帧秒数。

    规则:`0 < value < 1` = 全片比例,`value >= 1` = 秒;结果钳进 `[0, duration - 余量]`,
    这样 `--cover-at 1.0` 永远是"片头 1 秒",`--cover-at 0.3` 是"30% 处"。
    """
    v = float(value)
    if v < 0:
        raise ValueError(f"--cover-at 不能为负: {v:g}")
    total = max(0.0, float(duration or 0.0))
    at = v * total if v < 1.0 else v
    if total > 0:
        at = min(at, max(0.0, total - _TAIL_MARGIN))
    return max(0.0, at)


def normalize_size(width: int, height: int) -> tuple[int, int]:
    """把尺寸规整成偶数(yuv420p 编码要求宽高均为偶数),下限 16。"""
    w = max(16, int(width))
    h = max(16, int(height))
    return w + (w % 2), h + (h % 2)


def resolve_spec_size(
    name: str,
    src_size: tuple[int, int],
    timeline_meta: dict | None = None,
) -> tuple[int, int]:
    """解析一条规格的目标尺寸(偶数对齐)。

    - portrait / square:固定 1080x1920 / 1080x1080;
    - landscape:优先用 timeline 声明的画幅(`meta.width/height`),缺失时**原样**取源分辨率
      (于是多数情况下 == 源尺寸,走流复制不重编码)。
    """
    if name not in SUPPORTED_SPECS:
        raise ValueError(
            f"不支持的规格 {name!r};可选: {', '.join(SUPPORTED_SPECS)}"
        )
    if name in SPEC_SIZES:
        return SPEC_SIZES[name]
    meta = timeline_meta or {}
    try:
        w = int(meta.get("width") or 0)
        h = int(meta.get("height") or 0)
    except (TypeError, ValueError):
        w = h = 0
    if w > 0 and h > 0:
        return normalize_size(w, h)
    return normalize_size(int(src_size[0]), int(src_size[1]))


# ---------------------------------------------------------------------------
# 命令拼装(纯函数)
# ---------------------------------------------------------------------------

def blur_sigma(width: int, height: int) -> int:
    """模糊背景的高斯半径:按短边缩放(1080 短边 -> 27),下限 8。"""
    return max(8, round(min(width, height) / 40))


def fit_filter(width: int, height: int, mode: str = DEFAULT_FIT) -> str:
    """拼适配滤镜链(-filter_complex 参数)。

    blur:原片 cover 放大 + 高斯模糊铺满整幅当背景,原片再等比 contain 居中叠上去,
          四周是模糊画面而不是黑边;顺带压暗一点,避免抢字幕。
    crop:原片 cover 放大后居中裁切(不留任何边)。
    """
    mode = parse_fit(mode)
    cover = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}"
    if mode == "crop":
        return f"[0:v]{cover},setsar=1[v]"
    return (
        f"[0:v]{cover},gblur=sigma={blur_sigma(width, height)},eq=brightness=-0.05[bg];"
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[v]"
    )


def build_remux_cmd(ffmpeg: str, src: str, out: str) -> list[str]:
    """流复制(不重编码)一条规格:尺寸与源一致时走这条,最快且零损失。"""
    return [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", src, "-map", "0", "-c", "copy", "-movflags", "+faststart", out,
    ]


def build_fit_cmd(
    ffmpeg: str,
    src: str,
    out: str,
    width: int,
    height: int,
    mode: str = DEFAULT_FIT,
    *,
    crf: int = 20,
    preset: str = "medium",
) -> list[str]:
    """缩放/裁切出一条规格(blur 或 crop),音轨原样复制。"""
    return [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", src,
        "-filter_complex", fit_filter(width, height, mode),
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-c:a", "copy",
        "-movflags", "+faststart", out,
    ]


def build_cover_cmd(ffmpeg: str, src: str, out: str, at: float) -> list[str]:
    """在 `at` 秒抽一帧存成 PNG(-ss 放在 -i 前,快且仍逐帧精确到目标时间)。"""
    return [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{max(0.0, float(at)):.3f}", "-i", src,
        "-frames:v", "1", "-update", "1", out,
    ]


def build_loudness_cmd(ffmpeg: str, src: str) -> list[str]:
    """ebur128 测响度(输出全在 stderr 的 Summary 里,不产出文件)。"""
    return [
        ffmpeg, "-hide_banner", "-nostats", "-i", src,
        "-map", "0:a:0", "-af", "ebur128=peak=true", "-f", "null", os.devnull,
    ]


def build_silencedetect_cmd(
    ffmpeg: str, src: str, *, noise_db: float = -35.0, min_silence: float = 0.1,
) -> list[str]:
    """silencedetect 找静音区间(语音段起点 = 静音区间的结束点)。"""
    return [
        ffmpeg, "-hide_banner", "-nostats", "-i", src,
        "-map", "0:a:0",
        "-af", f"silencedetect=noise={noise_db:g}dB:d={min_silence:g}",
        "-f", "null", os.devnull,
    ]


# ---------------------------------------------------------------------------
# 日志解析(纯函数)
# ---------------------------------------------------------------------------

_I_RE = re.compile(r"I:\s*(-?\d+(?:\.\d+)?|-inf)\s*LUFS", re.IGNORECASE)
_PEAK_RE = re.compile(r"Peak:\s*(-?\d+(?:\.\d+)?|-inf)\s*dBFS", re.IGNORECASE)
_LRA_RE = re.compile(r"LRA:\s*(-?\d+(?:\.\d+)?|-inf)\s*LU\b", re.IGNORECASE)
_SIL_START_RE = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
_SIL_END_RE = re.compile(r"silence_end:\s*(-?\d+(?:\.\d+)?)")


def _to_float(raw: str) -> float | None:
    """日志里的数值:"-inf"/"inf" 视为无效,其余转 float。"""
    text = (raw or "").strip().lower()
    if not text or "inf" in text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _last_match(pattern: re.Pattern[str], log: str) -> float | None:
    """取最后一次匹配的值(ebur128 会边跑边打,Summary 在最后)。"""
    found = pattern.findall(log or "")
    return _to_float(found[-1]) if found else None


def parse_loudness(log: str) -> Loudness:
    """从 ebur128 的日志里取 (integrated LUFS, true peak dBTP, LRA LU)。

    参数:
        log: ffmpeg 的 stderr 全文(含 Summary 段)。
    返回:
        Loudness: 测不到的项为 None。
    """
    return Loudness(
        integrated=_last_match(_I_RE, log),
        true_peak=_last_match(_PEAK_RE, log),
        lra=_last_match(_LRA_RE, log),
    )


def parse_silences(log: str) -> list[tuple[float, float | None]]:
    """从 silencedetect 的日志里取静音区间 [(start, end 或 None)]。

    尾部静音只有 `silence_start` 没有 `silence_end`(静音持续到片尾),end 给 None。
    """
    starts = [float(x) for x in _SIL_START_RE.findall(log or "")]
    ends = [float(x) for x in _SIL_END_RE.findall(log or "")]
    pairs: list[tuple[float, float | None]] = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else None
        pairs.append((max(0.0, s), e))
    return pairs


def speech_onsets(
    silences: Sequence[tuple[float, float | None]], duration: float,
) -> list[float]:
    """由静音区间推出"语音起点"列表(静音结束处 = 说话开始处)。

    首段静音不从 0 开始时,0 秒本身算一个语音起点;尾部静音之后再无语音。
    """
    onsets: list[float] = []
    cursor = 0.0
    for start, end in silences:
        if start > cursor + 0.01:
            onsets.append(round(cursor, 3))
        if end is None:
            return onsets
        cursor = max(cursor, end)
    total = float(duration or 0.0)
    if total > 0 and cursor < total - 0.01:
        onsets.append(round(cursor, 3))
    return onsets


def align_onsets(
    expected: Sequence[float], onsets: Sequence[float],
) -> list[float | None]:
    """逐句找最近的实测语音起点,返回每句的偏差(实测 - 期望,秒);无起点时 None。

    这是一次"体检"式的粗对齐:每句取全局最近的起点(允许复用),不做刚性时钟回归,
    对"音轨整体偏移/被 BGM 抬高噪声门"这类问题足够敏感。
    """
    if not onsets:
        return [None for _ in expected]
    out: list[float | None] = []
    for want in expected:
        nearest = min(onsets, key=lambda o: abs(o - want))
        out.append(round(nearest - want, 3))
    return out


def sync_rows(
    speech: Sequence,
    onsets: Sequence[float],
    *,
    tolerance: float = DEFAULT_SYNC_TOLERANCE,
    max_sentence_chars: int = 18,
) -> list[SyncRow]:
    """把 timeline 的 speech 段与实测语音起点配对成逐句比较行。

    参数:
        speech: speech 段序列(取 .start / .text)。
        onsets: speech_onsets 的结果(实测秒)。
        tolerance: 偏差阈值(秒),写进行的标记由调用方判断。
        max_sentence_chars: 报告里句子截断长度(太长的句子占满表格)。
    """
    starts = [float(seg.start) for seg in speech]
    deltas = align_onsets(starts, onsets)
    rows: list[SyncRow] = []
    for i, seg in enumerate(speech):
        delta = deltas[i]
        text = str(getattr(seg, "text", "") or "").strip().replace("\n", " ")
        if len(text) > max_sentence_chars:
            text = text[: max_sentence_chars - 1] + "…"
        actual = None if delta is None else round(starts[i] + delta, 3)
        rows.append(SyncRow(i + 1, text, round(starts[i], 3), actual, delta))
    return rows


def sync_mean(rows: Sequence[SyncRow]) -> float | None:
    """平均偏移(秒,可为负);没测到任何句子时返回 None。

    平均偏移明显偏离 0 而各句偏差接近(如全部 +0.09s),说明是**系统性偏移**
    (时间轴基准对齐方式不同),而不是个别句子错位 —— 报告里一并给出便于判断。
    """
    measured = [r.delta for r in rows if r.delta is not None]
    return round(sum(measured) / len(measured), 3) if measured else None


def sync_conclusion(
    rows: Sequence[SyncRow], tolerance: float = DEFAULT_SYNC_TOLERANCE,
) -> tuple[float | None, list[SyncRow], str]:
    """对位结论:返回 (最大绝对偏差, 超阈值行, 结论文本)。"""
    measured = [r for r in rows if r.delta is not None]
    if not measured:
        return None, [], "没找到可比较的语音起点(成片可能没有音频流或全程静音)"
    max_abs = max(abs(r.delta) for r in measured)
    bad = [r for r in measured if abs(r.delta) > tolerance]
    mean = sum(r.delta for r in measured) / len(measured)
    if bad:
        return max_abs, bad, (
            f"❌ {len(bad)}/{len(measured)} 句偏差超 {tolerance:.2f}s,"
            f"最大 {max_abs:.3f}s,平均 {mean:+.3f}s"
        )
    return max_abs, bad, (
        f"✅ {len(measured)} 句偏差均在 {tolerance:.2f}s 内,"
        f"最大 {max_abs:.3f}s,平均 {mean:+.3f}s"
    )


def loudness_verdict(
    loud: Loudness | None,
    target_lufs: float = DEFAULT_LUFS_TARGET,
    target_dbtp: float = DEFAULT_DBTP_TARGET,
    tolerance: float = DEFAULT_LUFS_TOLERANCE,
) -> tuple[bool, str]:
    """响度结论:返回 (是否达标, 结论文本)。

    达标 = integrated 落在 `target ± tolerance` 内,且真峰值不超过 `target_dbtp`。
    """
    if loud is None or loud.integrated is None:
        return False, "未测到 integrated 响度(成片可能没有音频流或全程静音)"
    problems: list[str] = []
    diff = loud.integrated - target_lufs
    if abs(diff) > tolerance:
        direction = "偏轻" if diff < 0 else "偏响"
        problems.append(
            f"整体{direction} {abs(diff):.1f} LU"
            f"(实测 {loud.integrated:.1f} LUFS,目标 {target_lufs:.1f} ±{tolerance:.1f})"
        )
    if loud.true_peak is not None and loud.true_peak > target_dbtp:
        problems.append(
            f"真峰值 {loud.true_peak:.1f} dBTP 超过上限 {target_dbtp:.1f} dBTP"
        )
    if problems:
        return False, "不达标:" + ";".join(problems)
    peak = (
        f"真峰值 {loud.true_peak:.1f} dBTP ≤ {target_dbtp:.1f} dBTP"
        if loud.true_peak is not None else "真峰值未测到"
    )
    return True, (
        f"达标:{loud.integrated:.1f} LUFS ∈ "
        f"[{target_lufs - tolerance:.1f}, {target_lufs + tolerance:.1f}],{peak}"
    )


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------

def human_size(size: int) -> str:
    """字节数 -> 可读大小(1024 进制,保留两位)。"""
    value = float(max(0, int(size)))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.2f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.2f} GB"


def format_report(result: DeliverResult, *, generated_at: str = "") -> str:
    """把交付结果渲染成 Markdown 报告(纯函数)。"""
    v = result.video
    fps = f"{v.fps:g}fps" if v.fps else "fps 未知"
    lines = [
        "# 交付报告",
        "",
        (
            f"- 成片: `{v.path}`({v.width}x{v.height} @ {fps},{v.duration:.3f}s,"
            f"{human_size(v.size_bytes)})"
        ),
        f"- 交付目录: `{result.out_dir}`",
    ]
    if generated_at:
        lines.append(f"- 生成时间: {generated_at}")
    # ---- 规格清单 ----
    lines += [
        "",
        "## 规格清单",
        "",
        "| 规格 | 尺寸 | 适配 | 文件 | 大小 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for s in result.specs:
        lines.append(
            f"| {s.name} | {s.width}x{s.height} | {FIT_LABELS.get(s.mode, s.mode)} "
            f"| `{os.path.basename(s.path)}` | {human_size(s.size_bytes)} |"
        )
    if not result.specs:
        lines.append("| (无) | - | - | - | - |")
    # ---- 封面 ----
    lines += [
        "",
        "## 封面",
        "",
        "| 请求值 | 实际时刻 | 文件 |",
        "| --- | --- | --- |",
    ]
    for c in result.covers:
        kind = "比例" if 0 < c.request < 1 else "秒"
        lines.append(
            f"| {c.request:g}({kind}) | {c.at:.3f}s | `{os.path.basename(c.path)}` |"
        )
    if not result.covers:
        lines.append("| (无) | - | - |")
    # ---- 响度 ----
    lines += ["", "## 响度复核", ""]
    if result.loudness is None:
        reason = result.skipped.get("loudness", "未执行")
        lines += [f"- 跳过:{reason}"]
    else:
        loud = result.loudness
        ok, verdict = loudness_verdict(
            loud, result.lufs_target, result.dbtp_target,
        )
        integrated = f"{loud.integrated:.1f} LUFS" if loud.integrated is not None else "未测到"
        peak = f"{loud.true_peak:.1f} dBTP" if loud.true_peak is not None else "未测到"
        lra = f"{loud.lra:.1f} LU" if loud.lra is not None else "未测到"
        lines += [
            (
                f"- 目标: {result.lufs_target:.1f} LUFS"
                f"(容差 ±{DEFAULT_LUFS_TOLERANCE:.1f})"
                f" / 真峰值 ≤ {result.dbtp_target:.1f} dBTP"
            ),
            f"- 实测: integrated {integrated};真峰值 {peak};LRA {lra}",
            f"- 结论: {'✅' if ok else '❌'} {verdict}",
        ]
    # ---- 对位 ----
    lines += ["", "## 音画对位校验", ""]
    if not result.sync:
        reason = result.skipped.get("sync", "未执行")
        lines += [f"- 跳过:{reason}"]
    else:
        max_abs, _bad, verdict = sync_conclusion(result.sync, result.sync_tolerance)
        mean = sync_mean(result.sync)
        lines += [
            f"- 阈值: {result.sync_tolerance:.2f}s;timeline 语音段: {len(result.sync)} 句",
            f"- 最大偏差: {'未测到' if max_abs is None else f'{max_abs:.3f}s'}",
            (
                f"- 平均偏移: {'未测到' if mean is None else f'{mean:+.3f}s'}"
                "(各句接近同一值 = 系统性偏移,而非个别句子错位)"
            ),
            f"- 结论: {verdict}",
            "",
            "| # | 句子 | 期望起点 | 实测起点 | 偏差 |",
            "| --- | --- | --- | --- | --- |",
        ]
        for r in result.sync:
            flag = ""
            if r.delta is not None and abs(r.delta) > result.sync_tolerance:
                flag = " ⚠"
            actual = "-" if r.actual is None else f"{r.actual:.3f}s"
            delta = "-" if r.delta is None else f"{r.delta:+.3f}s"
            lines.append(
                f"| {r.index} | {r.sentence} | {r.expected:.3f}s | {actual} "
                f"| {delta}{flag} |"
            )
    # ---- 警告 ----
    lines += ["", "## 警告", ""]
    if result.warnings:
        lines += [f"- {w}" for w in result.warnings]
    else:
        lines += ["- 无"]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 探测 / 执行
# ---------------------------------------------------------------------------

def probe_frame_rate(ffprobe: str, path: str) -> float:
    """探测视频流帧率;读不到时返回 0.0(仅用于报告展示,不阻塞交付)。"""
    cmd = [
        ffprobe, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=r_frame_rate",
        "-of", "default=noprint_wrappers=1:nokey=1", path,
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=False,
    )
    raw = (proc.stdout or "").strip().splitlines()
    if proc.returncode != 0 or not raw:
        return 0.0
    num, _, den = raw[-1].partition("/")
    try:
        value = float(num) / (float(den) if den else 1.0)
    except (ValueError, ZeroDivisionError):
        return 0.0
    return round(value, 3) if value > 0 else 0.0


def probe_media(ffprobe: str, path: str) -> MediaInfo:
    """探测成片:尺寸 / 帧率 / 时长 / 文件大小。"""
    width, height = compose.probe_dimensions(ffprobe, path)
    duration = compose.probe_duration(ffprobe, path)
    return MediaInfo(
        path=os.path.abspath(path), width=width, height=height,
        fps=probe_frame_rate(ffprobe, path), duration=round(duration, 3),
        size_bytes=os.path.getsize(path),
    )


def run_capture(cmd: Sequence[str], *, what: str) -> str:
    """执行一条命令并返回它的 stderr+stdout 全文(ffmpeg 的滤镜输出都在 stderr)。

    参数:
        cmd: 参数列表(Windows 下不拼字符串)。
        what: 用于报错的可读步骤名,如 "响度复核"。
    返回值:
        str: 合并后的输出文本(解析用)。
    异常:
        RuntimeError: 退出码非 0,消息里带最后 8 行原始输出。
    """
    LOG.debug("%s 命令: %s", what, " ".join(cmd))
    proc = subprocess.run(
        list(cmd), capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=False,
    )
    output = (proc.stderr or "") + (proc.stdout or "")
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-8:])
        raise RuntimeError(f"{what}失败(exit={proc.returncode})\n{tail}")
    return output


# ---------------------------------------------------------------------------
# 交付主流程
# ---------------------------------------------------------------------------

def _load_timeline_speech(timeline_path: str | None):
    """读 timeline.json,返回 (meta, speech 段列表);未给路径时返回 (None, [])。"""
    if not timeline_path:
        return None, []
    from .timeline import Timeline

    tl = Timeline.from_json(timeline_path)
    return dict(tl.meta or {}), list(tl.tracks.get("speech") or [])


def _deliver_spec(
    ffmpeg: str,
    src: str,
    out_path: str,
    *,
    name: str,
    size: tuple[int, int],
    src_size: tuple[int, int],
    fit: str,
    crf: int,
    preset: str,
    warnings: list[str],
) -> SpecOutput:
    """产出一条规格;尺寸与源一致时先试流复制,失败自动回退重编码。"""
    width, height = size
    mode = "copy"
    if (width, height) != (int(src_size[0]), int(src_size[1])):
        mode = fit
    if mode == "copy":
        try:
            run_capture(build_remux_cmd(ffmpeg, src, out_path), what=f"规格 {name} 流复制")
        except RuntimeError as exc:
            warnings.append(f"规格 {name} 流复制失败,已回退重编码:{exc}".replace("\n", " "))
            LOG.warning("规格 %s 流复制失败,回退重编码: %s", name, exc)
            mode = fit
    if mode != "copy":
        run_capture(
            build_fit_cmd(ffmpeg, src, out_path, width, height, mode,
                          crf=crf, preset=preset),
            what=f"规格 {name}({FIT_LABELS.get(mode, mode)})",
        )
    return SpecOutput(
        name=name, width=width, height=height, path=os.path.abspath(out_path),
        size_bytes=os.path.getsize(out_path), mode=mode,
    )


def deliver(
    video: str,
    *,
    out: str,
    specs: str | Sequence[str] | None = None,
    cover_at: str | float | Sequence[float] | None = None,
    timeline: str | None = None,
    fit: str = DEFAULT_FIT,
    lufs_target: float = DEFAULT_LUFS_TARGET,
    dbtp_target: float = DEFAULT_DBTP_TARGET,
    sync_tolerance: float = DEFAULT_SYNC_TOLERANCE,
    crf: int = 20,
    preset: str = "medium",
) -> int:
    """交付主流程:多规格 + 封面 + 响度复核 + 对位校验 + 报告。

    参数:
        video: 成片 mp4 路径(相对当前目录或绝对)。
        out: 交付目录(不存在则创建)。
        specs: 规格参数(逗号分隔字符串或序列),缺省 landscape,portrait,square。
        cover_at: 封面时间点参数,缺省 1.0(见 parse_cover_at)。
        timeline: timeline.json 路径(可选):决定 landscape 画幅,并启用对位校验。
        fit: 画面适配方式 blur(默认)/ crop。
        lufs_target / dbtp_target: 响度目标与真峰值上限。
        sync_tolerance: 对位偏差阈值(秒)。
        crf / preset: 重编码规格的 x264 参数(流复制路径不受影响)。
    返回值:
        int: 0 = 交付完成(响度/对位不达标只写警告,不算执行失败)。
    异常:
        ValueError: 参数非法(规格/适配方式/封面时间点)。
        RuntimeError: 找不到 ffmpeg/ffprobe、成片不可读、某条规格或封面产出失败。
    """
    src = os.path.abspath(os.path.expanduser(video))
    if not os.path.isfile(src):
        raise RuntimeError(f"找不到成片: {src}")
    spec_names = parse_specs(specs)
    fit_mode = parse_fit(fit)
    cover_values = parse_cover_at(cover_at)
    out_dir = os.path.abspath(os.path.expanduser(out))
    os.makedirs(out_dir, exist_ok=True)

    ffmpeg = compose.find_ffmpeg()
    ffprobe = compose.find_ffprobe(ffmpeg)
    info = probe_media(ffprobe, src)
    LOG.info("成片: %s(%dx%d,%.3fs)", info.path, info.width, info.height, info.duration)

    tl_meta, speech = _load_timeline_speech(timeline)
    result = DeliverResult(
        video=info, out_dir=out_dir, lufs_target=float(lufs_target),
        dbtp_target=float(dbtp_target), sync_tolerance=float(sync_tolerance),
    )

    # ---- 1) 多规格 ----
    stem = os.path.splitext(os.path.basename(src))[0]
    for name in spec_names:
        size = resolve_spec_size(name, (info.width, info.height), tl_meta)
        out_path = os.path.join(out_dir, f"{stem}-{name}.mp4")
        spec_out = _deliver_spec(
            ffmpeg, src, out_path, name=name, size=size,
            src_size=(info.width, info.height), fit=fit_mode,
            crf=crf, preset=preset, warnings=result.warnings,
        )
        result.specs.append(spec_out)
        print(f"规格 {name}: {spec_out.width}x{spec_out.height} "
              f"[{FIT_LABELS.get(spec_out.mode, spec_out.mode)}] -> {spec_out.path}")
        LOG.info("规格 %s 完成: %s(%s)", name, out_path, human_size(spec_out.size_bytes))

    # ---- 2) 封面 ----
    for i, value in enumerate(cover_values, start=1):
        at = resolve_cover_time(value, info.duration)
        if abs(at - value) > 1e-9 and value >= 1.0:
            result.warnings.append(
                f"--cover-at {value:g}s 超过成片时长,已钳到 {at:.2f}s"
            )
        cover_path = os.path.join(out_dir, f"cover-{i:02d}-{at:.2f}s.png")
        run_capture(build_cover_cmd(ffmpeg, src, cover_path, at),
                    what=f"封面抽帧({at:.2f}s)")
        result.covers.append(CoverOutput(value, at, os.path.abspath(cover_path)))
        print(f"封面: {at:.3f}s -> {os.path.basename(cover_path)}")

    # ---- 3) 响度复核 ----
    try:
        log = run_capture(build_loudness_cmd(ffmpeg, src), what="响度复核")
    except RuntimeError as exc:
        reason = str(exc).replace("\n", " ")
        result.skipped["loudness"] = reason
        result.warnings.append(f"响度复核跳过:{reason}")
        LOG.warning("响度复核跳过: %s", reason)
    else:
        loud = parse_loudness(log)
        if loud.integrated is None and loud.true_peak is None:
            reason = "ebur128 没有输出 Summary(可能没有音频流)"
            result.skipped["loudness"] = reason
            result.warnings.append(f"响度复核失败:{reason}")
        else:
            result.loudness = loud
            ok, verdict = loudness_verdict(loud, result.lufs_target, result.dbtp_target)
            print(f"响度: {'✅' if ok else '❌'} {verdict}")
            if not ok:
                result.warnings.append(f"响度复核:{verdict}")

    # ---- 4) 音画对位(给了 --timeline 才做) ----
    if not timeline:
        result.skipped["sync"] = "未提供 --timeline(对位校验需要 timeline 的语音段)"
    elif not speech:
        result.skipped["sync"] = "timeline 没有语音段"
    else:
        try:
            log = run_capture(build_silencedetect_cmd(ffmpeg, src), what="音画对位校验")
        except RuntimeError as exc:
            reason = str(exc).replace("\n", " ")
            result.skipped["sync"] = reason
            result.warnings.append(f"对位校验跳过:{reason}")
            LOG.warning("对位校验跳过: %s", reason)
        else:
            onsets = speech_onsets(parse_silences(log), info.duration)
            rows = sync_rows(speech, onsets, tolerance=result.sync_tolerance)
            result.sync = rows
            _max_abs, bad, verdict = sync_conclusion(rows, result.sync_tolerance)
            print(f"对位: {verdict}")
            if bad:
                result.warnings.append(f"对位校验:{verdict}")

    # ---- 5) 报告 ----
    report_path = os.path.join(out_dir, REPORT_NAME)
    with open(report_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(format_report(result, generated_at=time.strftime("%Y-%m-%d %H:%M:%S")))
    result.report_path = report_path
    print(f"交付报告: {report_path}")
    for w in result.warnings:
        print(f"警告: {w}")
    return 0
