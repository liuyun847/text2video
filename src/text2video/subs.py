"""字幕模块:把逐字时间轴转换成 ASS 字幕(纯函数,便于单测)。

支持两种样式:
- karaoke(默认):逐字点亮 / 滚动高亮,当前念到的部分白字加粗,未念到部分灰色,
  整句固定在下部,是短视频口播最常见的风格。
- scroll:整段文字从下往上匀速滚动(走字幕 / 片尾滚动样式)。

时间轴来自 edge-tts 的 WordBoundary 事件(秒为单位)。
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WordTiming:
    """一个字 / 一个词的时间轴(秒)。"""

    text: str
    start: float
    end: float


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def ass_time(seconds: float) -> str:
    """秒 -> ASS 时间戳 H:MM:SS.cc(libass 用百分秒)。负数钳到 0。"""
    seconds = max(0.0, float(seconds))
    cs = round(seconds * 100)
    h, rem = divmod(cs, 3600 * 100)
    m, rem = divmod(rem, 60 * 100)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def bgr_color(rgb: int) -> str:
    """0xRRGGBB -> ASS 颜色串 &HAABBGGRR(ASS 是 BGR 序)。透明度恒 00 不透明。"""
    r = (rgb >> 16) & 0xFF
    g = (rgb >> 8) & 0xFF
    b = rgb & 0xFF
    return f"&H00{b:02X}{g:02X}{r:02X}"


def display_width(ch: str) -> int:
    """字符显示宽度:全角/宽字符按 2 格,CJK 之外大多 1 格。"""
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def join_words(tokens: Sequence[str]) -> str:
    """把 token 拼成可显示文本:CJK 无缝拼接,半角字母/数字之间补空格。"""
    out = ""
    for tok in tokens:
        if out and _needs_space(out[-1], tok[:1]):
            out += " "
        out += tok
    return out


def _needs_space(left: str, right: str) -> bool:
    """仅当左右同为半角字母/数字时才需要空格(英文单词分词)。"""
    return (
        left.isascii() and left.isalnum()
    ) and (
        right.isascii() and right.isalnum()
    )


def wrap_words(words: Sequence[WordTiming], max_chars: int) -> list[list[WordTiming]]:
    """按显示宽度把单词流贪心断行,保持原始顺序与每个词的时间。

    max_chars 含义是"一行最多容纳的中文(全角)字数",西文半角按 1/2 折算,
    即一行总宽度预算 = max_chars * 2 个半角格。
    """
    if max_chars < 1:
        raise ValueError("max_chars 必须 >= 1")
    budget = max_chars * 2
    lines: list[list[WordTiming]] = []
    cur: list[WordTiming] = []
    cur_w = 0
    for w in words:
        ww = sum(display_width(c) for c in w.text)
        if cur and cur_w + ww > budget:
            lines.append(cur)
            cur, cur_w = [], 0
        cur.append(w)
        cur_w += ww
    if cur:
        lines.append(cur)
    return lines


def wrap_plain_text(text: str, max_chars: int) -> list[str]:
    """普通文本断行(max_chars 为中文数),返回不折行、可继续拼合的若干行。"""
    if max_chars < 1:
        raise ValueError("max_chars 必须 >= 1")
    budget = max_chars * 2
    # 先按换行拆,再按宽度贪心
    out: list[str] = []
    cur = ""
    cur_w = 0
    for ch in text:
        if ch in "\r\n":
            if cur:
                out.append(cur)
                cur, cur_w = "", 0
            continue
        w = display_width(ch)
        if cur and cur_w + w > budget:
            out.append(cur)
            cur, cur_w = "", 0
        cur += ch
        cur_w += w
    if cur:
        out.append(cur)
    return out


# ---------------------------------------------------------------------------
# ASS 模板
# ---------------------------------------------------------------------------

def ass_header(
    width: int,
    height: int,
    *,
    font: str,
    fontsize: int,
    outline: int,
    shadow: int,
    alignment: int,
    margin_v: int,
    border_style: int,
    back_alpha: str,
) -> str:
    """生成 ASS 文件头 + 样式块。back_alpha 为 BackColour 的 AA 透明度(如 64)。

    公有导出:字幕(karaoke/scroll)与封面(cover)共用该头部模板。
    """
    return (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {width}\n"
        f"PlayResY: {height}\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Base,{font},{fontsize},&H00FFFFFF,&H000000FF,&H00000000,"
        f"&H{back_alpha}101010,0,0,0,0,100,100,0,0,{border_style},{outline},"
        f"{shadow},{alignment},40,40,{margin_v},1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
    )


# 向后兼容别名(旧名 _ass_header,盖层与既有调用保持可用)
_ass_header = ass_header


# ---------------------------------------------------------------------------
# 样式一:逐字点亮(karaoke)
# ---------------------------------------------------------------------------

def karaoke_events(
    words: Sequence[WordTiming],
    *,
    highlight_rgb: int = 0xFFFFFF,
    dim_rgb: int = 0x9A9A9A,
    tail_hold: float = 0.35,
    next_line_start: float | None = None,
    total_duration: float | None = None,
) -> list[tuple[float, float, str]]:
    """把一行词生成一组"逐字点亮"事件。

    每个事件 i:从第 i 个词开始显示,前 i+1 个词为高亮色加粗,其余为灰色;
    事件结束于下一个词的开始(最后事件 = 最后一个词结束 + tail_hold)。
    返回 [(start, end, ass_text), ...]。
    """
    n = len(words)
    events: list[tuple[float, float, str]] = []
    hl = bgr_color(highlight_rgb)
    dim = bgr_color(dim_rgb)
    prefix_text = ""
    for i, w in enumerate(words):
        start = w.start
        if i + 1 < n:
            end = words[i + 1].start
        else:
            end = w.end + tail_hold
            # 尾字停留不超过下一行开始 / 视频总时长
            cap = next_line_start if next_line_start is not None else total_duration
            if cap is not None:
                end = min(end, max(cap, start + 0.01))
        if end <= start:
            end = start + 0.05
        prefix_text = join_words(t.text for t in words[: i + 1])
        suffix_text = join_words(t.text for t in words[i + 1 :])
        text = f"{{\\c{hl}\\b1}}{prefix_text}"
        if suffix_text:
            text += f"{{\\c{dim}\\b0}}{suffix_text}"
        events.append((start, end, text))
    return events


def build_karaoke_ass(
    lines: Sequence[Sequence[WordTiming]],
    *,
    width: int = 1080,
    height: int = 1920,
    font: str = "Microsoft YaHei",
    fontsize: int = 64,
    highlight_rgb: int = 0xFFFFFF,
    dim_rgb: int = 0x9A9A9A,
    outline: int = 3,
    shadow: int = 1,
    margin_v: int = 140,
    box: bool = False,
    total_duration: float = 0.0,
) -> str:
    """多行词 -> 完整 ASS 字幕内容(逐字点亮样式)。"""
    header = _ass_header(
        width, height,
        font=font, fontsize=fontsize, outline=outline, shadow=shadow,
        alignment=2, margin_v=margin_v,
        border_style=3 if box else 1,
        back_alpha="64" if box else "00",
    )
    parts = [header]
    for li, line in enumerate(lines):
        next_start = lines[li + 1][0].start if li + 1 < len(lines) else None
        events = karaoke_events(
            line,
            highlight_rgb=highlight_rgb,
            dim_rgb=dim_rgb,
            next_line_start=next_start,
            total_duration=total_duration,
        )
        for start, end, text in events:
            parts.append(
                f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Base,,0,0,0,,{text}"
            )
    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# 样式二:整段上滚(scroll)
# ---------------------------------------------------------------------------

def build_scroll_ass(
    text: str,
    *,
    duration: float,
    width: int = 1080,
    height: int = 1920,
    font: str = "Microsoft YaHei",
    fontsize: int = 64,
    max_chars: int = 15,
    outline: int = 3,
    shadow: int = 1,
    box: bool = False,
    steps_per_second: int = 10,
) -> str:
    r"""整段文本从下往上匀速滚动的走字幕样式(逐行滚动墙)。

    实测发现:集成 libass 的 ffmpeg 中,\move 在起点位于屏幕外时动画失效;
    且多行整块 \pos 的顶部深入屏幕上方(深负偏移)时整块会被裁掉不渲染。
    因此这里采用"逐行独立事件"方案:每一行是单独一条 Dialogue,按 \pos 分片
    匀速上移;每行最深的负偏移只到自己的一个行高,逐行顶部露头/滚出都正常。
    行进入画面从底部开始、滚出画面到顶部结束,视觉上等价于整段滚动字幕。
    """
    lines = wrap_plain_text(text, max_chars)
    line_h = fontsize * 1.25          # 行高估算
    n_lines = len(lines)
    col_h = n_lines * line_h          # 整列高度
    v = (height + col_h) / duration if duration > 0 else 0.0  # 滚动速度 px/s
    x = width / 2.0

    header = _ass_header(
        width, height,
        font=font, fontsize=fontsize, outline=outline, shadow=shadow,
        alignment=8, margin_v=0,
        border_style=3 if box else 1,
        back_alpha="64" if box else "00",
    )

    # 分片数:常态 steps_per_second,并限制事件总量避免超长文本爆掉字幕文件
    n = max(2, round(duration * steps_per_second)) if duration > 0 else 2
    avg_visible = max(1, int(height // line_h) + 1)
    n = min(n, max(2, 30000 // avg_visible))
    dt = duration / n

    parts = [header]
    for k in range(n):
        t0 = k * dt
        t1 = duration if k == n - 1 else (k + 1) * dt
        for j, line in enumerate(lines):
            y = height + j * line_h - v * t0   # 该行此刻顶部(alignment 8)
            if -line_h <= y <= height:         # 部分/全部在画面内才渲染
                pos = f"{{\\pos({x:.1f},{y:.1f})}}"
                parts.append(
                    f"Dialogue: 0,{ass_time(t0)},{ass_time(t1)},Base,,0,0,0,,{pos}{line}"
                )
    return "\n".join(parts) + "\n"
