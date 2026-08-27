"""封面模块:从文案/时间线生成 16:9 封面图(标题大字 + 背景)。

复用现成的背景与字体逻辑:纯色/背景图铺满 + libass 渲染标题 ASS,
ffmpeg 取单帧输出 PNG。标题默认取文案首句(可 --title 覆盖),可带副标题。
"""

from __future__ import annotations

import logging
import os
import shutil

from . import compose
from .layout import resolve_font
from .subs import ass_header

LOG = logging.getLogger(__name__)


def build_cover_ass(
    title: str,
    *,
    width: int = 1920,
    height: int = 1080,
    font: str = "Microsoft YaHei",
    title_fontsize: int = 0,
    subtitle: str | None = None,
    subtitle_fontsize: int = 0,
    outline: int = 4,
    shadow: int = 2,
    title_color: str = "0xFFFFFF",
) -> str:
    r"""生成封面 ASS 字幕(标题居中大字 + 可选副标题),返回完整 ASS 文本。

    title_fontsize 缺省=短边÷6;subtitle_fontsize 缺省=短边÷10。
    标题/副标题用 \pos 定位(标题在垂直 42% 处,副标题紧随其下)。
    """
    short = min(width, height)
    tsize = title_fontsize or max(16, short // 6)
    ssize = subtitle_fontsize or max(12, short // 10)
    header = ass_header(
        width, height,
        font=font, fontsize=tsize, outline=outline, shadow=shadow,
        alignment=5, margin_v=0,
        border_style=1, back_alpha="00",
    )
    hl = int(title_color, 16)
    r = (hl >> 16) & 0xFF
    g = (hl >> 8) & 0xFF
    b = hl & 0xFF
    color = f"&H00{b:02X}{g:02X}{r:02X}"
    cx = width // 2
    ty = int(height * 0.42)
    parts = [header]
    parts.append(
        f"Dialogue: 0,0:00:00.00,24:00:00.00,Base,,0,0,0,,"
        f"{{\\c{color}\\b1\\pos({cx},{ty})}}{title}"
    )
    if subtitle:
        sy = ty + int(tsize * 1.5)
        parts.append(
            f"Dialogue: 0,0:00:00.00,24:00:00.00,Base,,0,0,0,,"
            f"{{\\c&H00AAAAAA\\b0\\fs{ssize}\\pos({cx},{sy})}}{subtitle}"
        )
    return "\n".join(parts) + "\n"


def render_cover(
    text: str,
    *,
    out: str,
    width: int = 1920,
    height: int = 1080,
    bg_color: str = "1B1B2A",
    bg_image: str | None = None,
    font: str | None = None,
    subtitle: str | None = None,
    ffmpeg: str | None = None,
) -> str:
    """渲染封面 PNG,返回输出路径。

    text 为标题文字;subtitle 可选副标题;画幅默认 16:9(1920x1080),
    可用 width/height 覆盖(如方形 1080x1080)。
    """
    ffmpeg_bin = ffmpeg or compose.find_ffmpeg()
    out_abs = os.path.abspath(out)
    workdir = os.path.dirname(out_abs) or "."
    os.makedirs(workdir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(out_abs))[0]

    _font_path, font_name = resolve_font(font)
    ass_text = build_cover_ass(
        text, width=width, height=height, font=font_name, subtitle=subtitle,
    )
    ass_name = f"{stem}.cover.ass"
    with open(os.path.join(workdir, ass_name), "w", encoding="utf-8") as f:
        f.write(ass_text)

    font_in_work = None
    if _font_path:
        ext = os.path.splitext(_font_path)[1] or ".ttc"
        font_in_work = os.path.join(workdir, f"coverfont{ext}")
        shutil.copyfile(_font_path, font_in_work)

    args = [ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error"]
    if bg_image:
        args += ["-loop", "1", "-framerate", "1", "-i", bg_image]
        bg_chain = (
            f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}[bg]"
        )
    else:
        args += ["-f", "lavfi", "-i",
                 f"color=c={bg_color}:s={width}x{height}:r=1"]
        bg_chain = "[0:v]null[bg]"
    args += ["-filter_complex",
             f"{bg_chain};[bg]subtitles={ass_name}:fontsdir=.[vout]",
             "-map", "[vout]",
             "-frames:v", "1",
             out_abs]
    compose.run_ffmpeg(args, cwd=workdir)
    for tmp in (font_in_work, os.path.join(workdir, ass_name)):
        if tmp and os.path.exists(tmp):
            os.remove(tmp)
    LOG.info("封面完成: %s", out_abs)
    return out_abs