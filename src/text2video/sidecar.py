"""字幕外挂模块:把时间线的语音段导出成 SRT / VTT(纯函数 + 写文件)。

外挂字幕是平台上传要的"可编辑字幕":与烧进画面的透明字幕轨(.subs.webm)同源同断行,
只是换了一份平台能读的文本格式。

规则:
- 一句一条 cue(时间轴取 timeline 的 speech 段;produce 后即为真实轴);
- 文本断行沿用 ASS 的断行规则(`subs.wrap_words`,宽限 `meta.max_chars`),
  句内逐字轴缺失(旧版 v2/v4 档)时退化为 `subs.wrap_plain_text` 按宽度断行;
- 时间格式:SRT `HH:MM:SS,mmm`、VTT `HH:MM:SS.mmm`;UTF-8 无 BOM、LF 换行;
- cue 区间钳进 `[0, duration]`,并与 timeline 总时长对齐(不会越过片尾)。

用法:
    cues = build_cues(tl)                 # 纯函数,便于单测
    srt = render_srt(cues); vtt = render_vtt(cues)
    write_sidecars(tl, "out/demo")        # -> out/demo.srt / out/demo.vtt
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass

from .layout import auto_fontsize, auto_max_chars
from .subs import WordTiming, join_words, wrap_plain_text, wrap_words
from .timeline import TrackSegment, segment_words

LOG = logging.getLogger(__name__)

# 支持的外挂字幕格式(同时也是文件扩展名)
SUPPORTED_FORMATS: tuple[str, ...] = ("srt", "vtt")
# produce 默认导出的格式(--no-sidecar-subs 可关,--subs-formats 可改)
DEFAULT_FORMATS: tuple[str, ...] = SUPPORTED_FORMATS
# 一条 cue 的最短时长(秒):避免零时长/倒挂区间被播放器丢弃
MIN_CUE_SECONDS = 0.05


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Cue:
    """一条字幕:序号(从 1 起)、起止秒、已断行的文本行。"""

    index: int
    start: float
    end: float
    lines: tuple[str, ...]


# ---------------------------------------------------------------------------
# 时间格式
# ---------------------------------------------------------------------------

def format_time(seconds: float, *, decimal: str = ",") -> str:
    """秒 -> `HH:MM:SS<decimal>mmm`(SRT 用逗号、VTT 用点);负数钳到 0。

    参数:
        seconds: 秒数(可为浮点,按毫秒四舍五入)。
        decimal: 秒与毫秒之间的分隔符,SRT 用 ","、VTT 用 "."。
    返回:
        str: 形如 `01:02:03,456` / `01:02:03.456`;小时位固定两位,超过 99 小时照常展开。
    """
    total_ms = max(0, round(float(seconds) * 1000))
    h, rem = divmod(total_ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{decimal}{ms:03d}"


def srt_time(seconds: float) -> str:
    """SRT 时间戳 `HH:MM:SS,mmm`。"""
    return format_time(seconds, decimal=",")


def vtt_time(seconds: float) -> str:
    """WebVTT 时间戳 `HH:MM:SS.mmm`。"""
    return format_time(seconds, decimal=".")


# ---------------------------------------------------------------------------
# 格式参数解析
# ---------------------------------------------------------------------------

def parse_formats(spec: str | Sequence[str] | None) -> tuple[str, ...]:
    """解析 `--subs-formats`("srt,vtt" / ["srt","vtt"]),返回有序去重的格式元组。

    参数:
        spec: 逗号分隔的字符串、字符串序列,或 None(视为默认 srt,vtt)。
    返回:
        tuple[str, ...]: 小写格式名,保持用户给出的顺序。
    异常:
        ValueError: 出现不支持的格式,或解析结果为空。
    """
    if spec is None:
        return DEFAULT_FORMATS
    raw = [spec] if isinstance(spec, str) else list(spec)
    out: list[str] = []
    for chunk in raw:
        for item in str(chunk).split(","):
            name = item.strip().lower()
            if not name:
                continue
            if name not in SUPPORTED_FORMATS:
                raise ValueError(
                    f"不支持的字幕格式 {name!r};可选: {', '.join(SUPPORTED_FORMATS)}"
                )
            if name not in out:
                out.append(name)
    if not out:
        raise ValueError(
            f"字幕格式为空;可选: {', '.join(SUPPORTED_FORMATS)}(逗号分隔)"
        )
    return tuple(out)


# ---------------------------------------------------------------------------
# 断行与 cue 构建
# ---------------------------------------------------------------------------

def cue_lines(
    text: str, words: Sequence[dict] | None, max_chars: int,
) -> tuple[str, ...]:
    """把一句拆成"可放进一条 cue 的若干行"。

    有逐字轴时走 ASS 的断行规则(`wrap_words` + `join_words`),与画面字幕完全一致;
    无逐字轴(旧档)时按显示宽度贪心断行(`wrap_plain_text`)。

    参数:
        text: 该句原文(无逐字轴时的断行依据)。
        words: 逐字轴 [{"text","start","end"}, ...];可为空/None。
        max_chars: 一行最多中文数(半角按 1/2 折算)。
    返回:
        tuple[str, ...]: 断好的行;整句为空时返回空元组。
    """
    limit = max(1, int(max_chars))
    if words:
        timings = [
            WordTiming(str(w.get("text", "")), float(w.get("start", 0.0)),
                       float(w.get("end", 0.0)))
            for w in words
        ]
        lines = [join_words(w.text for w in line).strip()
                 for line in wrap_words(timings, limit)]
        lines = [ln for ln in lines if ln]
        if lines:
            return tuple(lines)
    return tuple(ln for ln in wrap_plain_text(text or "", limit) if ln)


def clamp_cue(
    start: float, end: float, duration: float | None,
) -> tuple[float, float]:
    """把一条 cue 的时间钳进 `[0, duration]`,并保证 `end - start >= MIN_CUE_SECONDS`。

    参数:
        start: 段起点(秒)。
        end: 段终点(秒)。
        duration: 时间线总时长(秒);0/None 表示不做上界钳制。
    返回:
        tuple[float, float]: (起, 止),止恒大于起。
    """
    s = max(0.0, float(start))
    e = max(s, float(end))
    total = float(duration or 0.0)
    if total > 0:
        s = min(s, max(0.0, total - MIN_CUE_SECONDS))
        e = min(e, total)
    e = max(e, s + MIN_CUE_SECONDS)
    return s, e


def resolve_max_chars(tl) -> int:
    """从时间线 meta 取"一行最多中文数",缺失时按画幅/字号自动推导(与 ASS 同源)。"""
    meta = tl.meta or {}
    width = int(meta.get("width", 1080) or 1080)
    height = int(meta.get("height", 1080) or 1080)
    explicit = int(meta.get("max_chars") or 0)
    if explicit > 0:
        return explicit
    fontsize = int(meta.get("font_size") or 0) or auto_fontsize(width, height)
    return auto_max_chars(width, fontsize)


def build_cues(tl, *, max_chars: int | None = None) -> list[Cue]:
    """按 timeline 的 speech 段逐句构建 cue 列表(纯函数)。

    参数:
        tl: Timeline(需有 tracks["speech"];produce 后为真实轴)。
        max_chars: 覆盖断行宽限;None 时取 meta.max_chars 或按画幅自动推导。
    返回:
        list[Cue]: 序号从 1 起,按语音段顺序;文本全空的段跳过。
    """
    speech: list[TrackSegment] = list(tl.tracks.get("speech") or [])
    if not speech:
        return []
    limit = resolve_max_chars(tl) if max_chars is None else max(1, int(max_chars))
    cues: list[Cue] = []
    for seg in speech:
        lines = cue_lines(seg.text, segment_words(seg), limit)
        if not lines:
            continue
        start, end = clamp_cue(seg.start, seg.end, tl.duration)
        cues.append(Cue(len(cues) + 1, start, end, lines))
    return cues


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def render_srt(cues: Sequence[Cue]) -> str:
    """cue 列表 -> SRT 文本(UTF-8 友好,不含 BOM;空列表返回 "")。"""
    blocks = [
        f"{cue.index}\n{srt_time(cue.start)} --> {srt_time(cue.end)}\n"
        + "\n".join(cue.lines)
        for cue in cues
    ]
    return "\n\n".join(blocks) + "\n" if blocks else ""


def render_vtt(cues: Sequence[Cue]) -> str:
    """cue 列表 -> WebVTT 文本(以 `WEBVTT` 头开始;空列表也保留文件头)。"""
    body = "\n\n".join(
        f"{vtt_time(cue.start)} --> {vtt_time(cue.end)}\n" + "\n".join(cue.lines)
        for cue in cues
    )
    return "WEBVTT\n\n" + body + "\n" if body else "WEBVTT\n"


# ---------------------------------------------------------------------------
# 写文件
# ---------------------------------------------------------------------------

def write_sidecars(
    tl, base: str, formats: Sequence[str] | None = DEFAULT_FORMATS,
) -> list[str]:
    """把外挂字幕写到 `{base}.{format}`(UTF-8 无 BOM、LF 换行)。

    参数:
        tl: Timeline(取 speech 段与 meta)。
        base: 产物基名(路径前缀,不含扩展名),目录不存在时自动创建。
        formats: 要导出的格式;空序列 = 一个都不写(相当于关闭)。
    返回:
        list[str]: 实际写出的文件路径(与 formats 同序)。
    异常:
        ValueError: formats 含不支持的格式,或时间线没有语音段。
    """
    wanted = tuple(formats or ())
    if not wanted:
        return []
    unknown = [f for f in wanted if f not in SUPPORTED_FORMATS]
    if unknown:
        raise ValueError(
            f"不支持的字幕格式 {unknown!r};可选: {', '.join(SUPPORTED_FORMATS)}"
        )
    if not (tl.tracks.get("speech") or []):
        raise ValueError("时间线没有语音段,无法导出外挂字幕")
    cues = build_cues(tl)
    out_dir = os.path.dirname(os.path.abspath(base))
    os.makedirs(out_dir, exist_ok=True)
    written: list[str] = []
    for fmt in wanted:
        path = f"{base}.{fmt}"
        text = render_srt(cues) if fmt == "srt" else render_vtt(cues)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        written.append(path)
        LOG.info("外挂字幕: %s(%d 条)", path, len(cues))
    return written
